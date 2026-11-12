"""登入失敗限制（資訊安全規格 v0.1 §4）：連續失敗回 429、成功後重新計算、時間窗、來源 IP 與代理標頭。"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.login_limit import LoginLimits
from app.main import create_app

from .conftest import PASSWORD, SAME_ORIGIN, make_settings

WRONG = "wrong-password-123"


# 送出一次登入請求。
def _login(c: TestClient, username: str, password: str, **headers):
    return c.post("/api/auth/login", json={"username": username, "password": password},
                  headers={**SAME_ORIGIN, **headers})


# 建立指定限流參數的測試用戶端。
def _client(db, user=3, ip=100, trust_proxy=False) -> TestClient:
    limits = LoginLimits(max_failures_per_user=user, max_failures_per_ip=ip, window_minutes=15)
    return TestClient(create_app(make_settings(db, login_limits=limits, trust_proxy_headers=trust_proxy)))
# 核心邏輯：把上限調小，讓測試不用送幾十次請求；邏輯與正式設定相同。


# 計算登入嘗試紀錄筆數。
def _attempts(db) -> int:
    with psycopg.connect(db) as conn:
        return conn.execute("SELECT count(*) FROM auth_login_attempts").fetchone()[0]


# 驗證同一帳號連續失敗達上限後回 429，連正確密碼也被擋，且帶 Retry-After。
@pytest.mark.parametrize("username", ["alice", "nobody"])
def test_consecutive_failures_lock_username(db, users, username):
    with _client(db, user=3) as c:
        for _ in range(3):
            assert _login(c, username, WRONG).status_code == 401
        r = _login(c, username, PASSWORD)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "too_many_attempts"
    assert 1 <= int(r.headers["Retry-After"]) <= 15 * 60
# 核心邏輯：帳號存在（alice）與不存在（nobody）的行為一致，不能藉由是否被鎖來判斷帳號存在。


# 驗證成功登入後重新計算連續失敗。
def test_success_resets_consecutive_failures(db, users):
    with _client(db, user=3) as c:
        for _ in range(2):
            _login(c, "alice", WRONG)
        assert _login(c, "alice", PASSWORD).status_code == 200
        for _ in range(2):
            assert _login(c, "alice", WRONG).status_code == 401
        assert _login(c, "alice", PASSWORD).status_code == 200
# 核心邏輯：成功之前的失敗不再計入，所以總共 4 次失敗也不會被鎖。


# 驗證被 429 擋下的請求不會再新增紀錄，也不會延長封鎖。
def test_blocked_attempt_not_recorded(db, users):
    with _client(db, user=2) as c:
        _login(c, "alice", WRONG)
        _login(c, "alice", WRONG)
        before = _attempts(db)
        assert _login(c, "alice", WRONG).status_code == 429
    assert _attempts(db) == before
# 核心邏輯：若封鎖期間的嘗試也記錄，攻擊者就能持續延長受害帳號的封鎖時間。


# 驗證失敗紀錄離開時間窗後可再嘗試。
def test_failures_expire_after_window(db, users):
    with _client(db, user=2) as c:
        _login(c, "alice", WRONG)
        _login(c, "alice", WRONG)
        assert _login(c, "alice", PASSWORD).status_code == 429
        with psycopg.connect(db) as conn:
            conn.execute("UPDATE auth_login_attempts SET attempted_at = attempted_at - interval '16 minutes'")
        assert _login(c, "alice", PASSWORD).status_code == 200
# 核心邏輯：把紀錄時間往前移超過 15 分鐘，模擬時間經過。


# 驗證同一來源 IP 換不同帳號名稱猜密碼也會被擋。
def test_ip_limit_across_usernames(db, users):
    with _client(db, user=100, ip=3) as c:
        for name in ("alice", "bob", "carol"):
            assert _login(c, name, WRONG).status_code == 401
        assert _login(c, "root", PASSWORD).status_code == 429
# 核心邏輯：帳號限制擋不住「每個帳號只試幾次」的攻擊，所以另外以來源 IP 計數。


# 驗證未信任代理時，自填 X-Real-IP 無法繞過 IP 限制；信任代理時才依 X-Real-IP 分開計算。
def test_x_real_ip_only_trusted_when_configured(db, users):
    with _client(db, user=100, ip=2) as c:
        for i in range(2):
            _login(c, "alice", WRONG, **{"X-Real-IP": f"10.0.0.{i}"})
        assert _login(c, "bob", WRONG, **{"X-Real-IP": "10.0.0.9"}).status_code == 429
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM auth_login_attempts")
    with _client(db, user=100, ip=2, trust_proxy=True) as c:
        for i in range(2):
            _login(c, "alice", WRONG, **{"X-Real-IP": f"10.0.0.{i}"})
        assert _login(c, "bob", WRONG, **{"X-Real-IP": "10.0.0.9"}).status_code == 401
# 核心邏輯：沒有代理時採信標頭，等於讓攻擊者每次換一個假 IP；只有部署在 nginx 後面才開啟。


# 驗證登入紀錄不保存密碼。
def test_attempts_do_not_store_password(db, users):
    with _client(db) as c:
        _login(c, "alice", WRONG)
    with psycopg.connect(db) as conn:
        row = conn.execute("SELECT * FROM auth_login_attempts").fetchone()
    assert WRONG not in str(row) and PASSWORD not in str(row)
# 核心邏輯：紀錄只有帳號名稱、來源 IP、成功與否與時間。
