"""S1 就緒檢查 /api/ready：最小權限、pgvector、遷移狀態，以及錯誤時不外洩連線字串。

真實資料庫測試沿用 conftest 的獨立測試伺服器；FAKE_DSN 與假連線（MOCK）只用來驗證錯誤處理與判斷順序。
"""
from __future__ import annotations

import logging
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app import main as main_module
from app.config import Settings
from app.main import create_app
from app.migrate import MIGRATIONS_DIR
from app.readiness import check_connection

FAKE_DSN = "postgresql://secret_user:secret_pass@db-host:5432/lipmatch"  # 合成值，非真實帳密


@pytest.fixture
def app_role_url(db, pg_admin_url):
    """在測試資料庫建立一個非超級使用者帳號，回傳以該帳號連線的 URL；結束後移除帳號。"""
    role = f"lipmatch_app_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(16)
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT SELECT ON schema_migrations TO {}").format(sql.Identifier(role)))
    yield make_conninfo(db, user=role, password=password)
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
# 核心邏輯：帳號名稱與密碼都隨機產生，以 sql.Identifier／sql.Literal 組 SQL（不做字串拼接）；
# 只授權讀取 schema_migrations，正好是就緒檢查需要的最小權限。結束時先清掉權限再刪帳號。


# 驗證以非超級使用者連線、pgvector 已安裝、遷移已套用時回 200。
def test_ready_ok_for_non_superuser(app_role_url):
    with TestClient(create_app(Settings(database_url=app_role_url, db_pool_max=2))) as client:
        res = client.get("/api/ready")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ready"
    assert body["database"]["pgvector_version"]
    # 最新遷移號；不寫死，避免每加一個遷移檔就要改這裡
    assert body["database"]["schema_version"] == sorted(MIGRATIONS_DIR.glob("*.sql"))[-1].name[:4]
# 核心邏輯：使用真實的測試資料庫與真實的非超級使用者帳號，不是 MOCK。


# 驗證以超級使用者連線時回 503（資訊安全規格 v0.1 §2：應用 DB 帳號非超級使用者）。
def test_ready_rejects_superuser(client):
    res = client.get("/api/ready")
    assert res.status_code == 503
    assert res.json()["status"] == "not_ready"
    assert "超級使用者" in res.json()["database"]["detail"]
# 核心邏輯：conftest 的 client 以測試伺服器的管理帳號連線，所以就緒檢查必須拒絕。


# 驗證就緒檢查不需要登入。
def test_ready_needs_no_login(app_role_url):
    with TestClient(create_app(Settings(database_url=app_role_url, db_pool_max=2))) as client:
        assert client.get("/api/ready").status_code == 200
# 核心邏輯：healthcheck 與狀態頁沒有登入權杖；回應只有狀態與版本，不含使用者資料。


# 驗證資料庫錯誤時回 503，且回應與日誌都不含連線字串中的帳號、密碼、主機。
def test_ready_error_does_not_leak_dsn(client, monkeypatch, caplog):
    def fail(_conn):
        raise psycopg.OperationalError(f"connection to {FAKE_DSN} failed")

    monkeypatch.setattr(main_module, "check_connection", fail)
    with caplog.at_level(logging.WARNING):
        res = client.get("/api/ready")
    assert res.status_code == 503
    assert "OperationalError" in res.json()["database"]["detail"]
    for text in (res.text, caplog.text):
        for secret in ("secret_user", "secret_pass", "db-host"):
            assert secret not in text
# 核心邏輯：故意讓例外訊息含完整的合成連線字串（MOCK），再檢查回應本文與日誌都沒有帶出來。


class _FakeConn:
    """依查詢內容回傳預設值的假連線（MOCK），用來驗證判斷順序。"""

    def __init__(self, superuser=False, pgvector="0.8.0", migrated=True):
        self._answers = {
            "SHOW server_version": "17.6",
            "pg_extension": pgvector,
            "rolsuper": superuser,
            "to_regclass": migrated,
            "max(version)": "0001",
        }

    def execute(self, query, _params=()):
        value = next(v for k, v in self._answers.items() if k in query)
        return _FakeCursor(None if value is None else (value,))


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


# 驗證缺 pgvector 與尚未遷移時，各自回報明確原因。
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"pgvector": None}, "pgvector"), ({"migrated": False}, "遷移"), ({}, None)],
)
def test_check_connection_reasons(kwargs, expected):
    status = check_connection(_FakeConn(**kwargs))
    if expected is None:
        assert status.ok is True
    else:
        assert status.ok is False
        assert expected in status.detail
# 核心邏輯：以假連線（MOCK）分別製造「沒有 pgvector」「沒有遷移」「全部正常」三種情況。
