"""DEC-10：session cookie 屬性、Bearer 停用、CSRF 權杖與 Origin 檢查（含規格 SEC11 跨站修改請求）。

全部在 conftest 的獨立測試資料庫上執行；evil.example 為合成的攻擊來源。
"""
from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import COOKIE, CSRF_HEADER, ORIGIN, PASSWORD, SAME_ORIGIN, login, make_settings

EVIL = "http://evil.example"


# 計算目前的對話數量，用來確認被拒絕的請求沒有產生任何資料變更。
def _conversation_count(db) -> int:
    with psycopg.connect(db) as conn:
        return conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
# 核心邏輯：SEC11 的通過條件是「無狀態變更」，所以除了狀態碼還要檢查資料庫。


# 驗證登入發出的 cookie 屬性：HttpOnly、SameSite=Strict、Path=/、有到期時間；本機設定不加 Secure。
def test_login_sets_httponly_strict_cookie(client, users):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers=SAME_ORIGIN)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "max-age=43200" in lowered  # 12 小時
    assert "secure" not in lowered
# 核心邏輯：直接檢查 Set-Cookie 標頭字串，確認瀏覽器會套用的每一個屬性。


# 驗證遠端 HTTPS 設定：cookie 加 Secure，且使用 __Host- 前綴。
def test_secure_setting_uses_host_prefix(db, users):
    with TestClient(create_app(make_settings(db, session_cookie_secure=True)), base_url="https://testserver") as c:
        r = c.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                   headers={"Origin": "https://testserver"})
    assert r.status_code == 403  # https://testserver 不在允許清單
    with TestClient(create_app(make_settings(db, session_cookie_secure=True,
                                             allowed_origins=("https://testserver",))),
                    base_url="https://testserver") as c:
        r = c.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                   headers={"Origin": "https://testserver"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-lipmatch_session=") and "secure" in cookie.lower()
# 核心邏輯：同時確認 Origin 清單必須明確包含 https 來源，以及 Secure 模式下的 cookie 名稱與屬性。


# 驗證回應本文不含 session 權杖，前端腳本只拿得到 CSRF 權杖。
def test_session_token_not_in_response_body(client, users):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers=SAME_ORIGIN)
    token = r.cookies[COOKIE]
    assert token not in r.text
    assert r.json()["csrf_token"] != token
# 核心邏輯：權杖只出現在 HttpOnly 的 Set-Cookie；本文的 csrf_token 是衍生值，不能拿來當登入憑證。


# 驗證 Bearer 權杖已停用：把 session 權杖放在 Authorization 標頭也不算登入。
def test_bearer_header_no_longer_accepted(client, users):
    token = login(client, "alice")["Cookie"].split("=", 1)[1]
    r = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
# 核心邏輯：DEC-10 要求兩套認證不混用，所以 Authorization 標頭完全不被讀取。


# SEC11：已登入但缺少 CSRF 權杖的修改請求被拒，且資料沒有變更。
def test_missing_csrf_token_rejected(client, db, users):
    headers = login(client, "alice")
    no_csrf = {k: v for k, v in headers.items() if k != CSRF_HEADER}
    r = client.post("/api/conversations", json={}, headers=no_csrf)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    assert _conversation_count(db) == 0
# 核心邏輯：模擬跨站表單或腳本帶著瀏覽器 cookie 送出請求，但拿不到 CSRF 權杖的情況。


# SEC11：錯誤的 CSRF 權杖、或另一個 session 的 CSRF 權杖，都會被拒。
def test_wrong_or_foreign_csrf_token_rejected(client, db, users):
    alice, bob = login(client, "alice"), login(client, "bob")
    for token in ("0" * 64, bob[CSRF_HEADER]):
        r = client.post("/api/conversations", json={}, headers={**alice, CSRF_HEADER: token})
        assert r.status_code == 403
    assert _conversation_count(db) == 0
    assert client.post("/api/conversations", json={}, headers=alice).status_code == 201
# 核心邏輯：CSRF 權杖綁定 session，不能拿別人的權杖替代；最後確認正確權杖仍可正常建立。


# SEC11：來自其他網站的 Origin 即使帶著正確 cookie 與 CSRF 權杖也被拒；沒有 Origin 也沒有 Referer 時同樣拒絕。
def test_cross_site_or_missing_origin_rejected(client, db, users):
    headers = login(client, "alice")
    r = client.post("/api/conversations", json={}, headers={**headers, "Origin": EVIL})
    assert r.status_code == 403
    no_origin = {k: v for k, v in headers.items() if k != "Origin"}
    assert client.post("/api/conversations", json={}, headers=no_origin).status_code == 403
    assert _conversation_count(db) == 0
    r = client.post("/api/conversations", json={}, headers={**no_origin, "Referer": f"{ORIGIN}/index.html"})
    assert r.status_code == 201
# 核心邏輯：Origin 是第一層、CSRF 權杖是第二層，兩層都要通過；只有 Referer 時取其來源比對。


# 驗證登入本身也檢查 Origin（防止跨站替使用者登入攻擊者的帳號）。
def test_login_from_other_site_rejected(client, db, users):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers={"Origin": EVIL})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] == 0
# 核心邏輯：被拒時不發 cookie、資料庫也沒有新 session。


# 驗證沒有設定 APP_ORIGINS 時，所有修改請求都被拒（預設拒絕）。
def test_empty_allowed_origins_fails_closed(db, users):
    with TestClient(create_app(make_settings(db, allowed_origins=()))) as c:
        r = c.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers=SAME_ORIGIN)
    assert r.status_code == 403
# 核心邏輯：設定遺漏時寧可無法登入，也不要變成不檢查來源。


# 驗證 /api/me 回傳的 CSRF 權杖與登入時相同，前端重新整理後可取回。
def test_me_returns_same_csrf_token(client, users):
    headers = login(client, "alice")
    r = client.get("/api/me", headers={"Cookie": headers["Cookie"]})
    assert r.json()["csrf_token"] == headers[CSRF_HEADER]
# 核心邏輯：CSRF 權杖由 session 權杖衍生，同一個 session 的值固定。


# 驗證登入時帶著舊 cookie，舊 session 會被撤銷（防 session fixation）。
def test_login_rotates_existing_session(client, users):
    old = login(client, "alice")
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD},
                    headers={"Cookie": old["Cookie"], **SAME_ORIGIN})
    assert r.status_code == 200
    new_token = r.cookies[COOKIE]
    client.cookies.clear()
    assert new_token != old["Cookie"].split("=", 1)[1]
    assert client.get("/api/me", headers={"Cookie": old["Cookie"]}).status_code == 401
    assert client.get("/api/me", headers={"Cookie": f"{COOKIE}={new_token}"}).status_code == 200
# 核心邏輯：攻擊者若事先在受害者瀏覽器放了已知的 session，登入後那個 session 也會失效。


# 驗證登出需要 CSRF 權杖，成功後清除 cookie 並撤銷 session。
def test_logout_requires_csrf_and_clears_cookie(client, users):
    headers = login(client, "alice")
    no_csrf = {k: v for k, v in headers.items() if k != CSRF_HEADER}
    assert client.post("/api/auth/logout", headers=no_csrf).status_code == 403
    assert client.get("/api/me", headers={"Cookie": headers["Cookie"]}).status_code == 200
    r = client.post("/api/auth/logout", headers=headers)
    assert r.status_code == 204
    assert f'{COOKIE}=""' in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/api/me", headers={"Cookie": headers["Cookie"]}).status_code == 401
# 核心邏輯：沒有 CSRF 權杖時登出被拒（避免跨站強制登出）；登出後即使瀏覽器還留著舊值，資料庫也已撤銷。
