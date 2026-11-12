"""測試資料庫：每個測試建立一個全新、隨機命名的資料庫並套用遷移，結束後刪除。

預設用 pgserver 在暫存目錄啟動獨立 PostgreSQL（含 pgvector），不連任何既有資料庫。
若要改用其他測試用伺服器（例如 Docker 內的 pgvector），設定
LIPMATCH_TEST_PG_ADMIN_URL=postgresql://user:pass@host:port/postgres；
測試只會建立／刪除 lipmatch_test_ 開頭的資料庫，不會清空既有資料庫。
"""
from __future__ import annotations

import os
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app.cli import create_user
from app.config import Settings
from app.main import create_app
from app.migrate import migrate

PASSWORD = "correct-horse-battery"  # 測試專用，非任何真實帳號
ORIGIN = "http://testserver"  # TestClient 的預設主機；測試設定只允許這個來源
SAME_ORIGIN = {"Origin": ORIGIN}
COOKIE = "lipmatch_session"  # session_cookie_secure=False 時的 cookie 名稱
CSRF_HEADER = "X-CSRF-Token"


@pytest.fixture(scope="session")
def pg_admin_url(tmp_path_factory):
    url = os.environ.get("LIPMATCH_TEST_PG_ADMIN_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
def empty_db(pg_admin_url):
    name = f"lipmatch_test_{secrets.token_hex(6)}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(pg_admin_url, dbname=name)
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(empty_db):
    migrate(empty_db)
    return empty_db


@pytest.fixture
def client(db):
    with TestClient(create_app(make_settings(db))) as c:
        yield c


# 測試用設定：本機 http（cookie 不加 Secure）、只允許 ORIGIN 發出會修改資料的請求。
def make_settings(database_url: str, **overrides) -> Settings:
    base = dict(database_url=database_url, db_pool_max=4, session_cookie_secure=False, allowed_origins=(ORIGIN,))
    return Settings(**{**base, **overrides})
# 核心邏輯：與 docker compose 的本機設定一致；個別測試可用 overrides 調整（例如登入限流次數）。


@pytest.fixture
def users(db):
    """alice、bob 為一般使用者，root 為管理者；回傳 username → id。"""
    return {
        name: create_user(db, name, role, PASSWORD)
        for name, role in [("alice", "user"), ("bob", "user"), ("root", "admin")]
    }


# 以指定帳號登入，回傳之後請求要帶的標頭（session cookie、CSRF 權杖、Origin）。
# 用途：同一個 TestClient 需要輪流扮演多個使用者，所以不依賴 client 內建的 cookie jar。
def login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers=SAME_ORIGIN)
    assert r.status_code == 200, r.text
    token = r.cookies[COOKIE]
    client.cookies.clear()
    return {"Cookie": f"{COOKIE}={token}", CSRF_HEADER: r.json()["csrf_token"], **SAME_ORIGIN}
# 核心邏輯：登入回應的 Set-Cookie 會自動存進 client 的 cookie jar；取出權杖後立刻清空 jar，
# 讓「沒帶標頭的請求」確實是未登入狀態，每個使用者的身分都只由明確傳入的標頭決定。
