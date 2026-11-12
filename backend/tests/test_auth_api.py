"""登入、密碼雜湊、錯誤格式與越權測試。"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from app.security import hash_password, verify_password

from .conftest import COOKIE, PASSWORD, SAME_ORIGIN, login


# ---------------------------------------------------------------------------
# 密碼與登入
# ---------------------------------------------------------------------------
def test_password_hash_is_salted_and_verifiable():
    h1, h2 = hash_password(PASSWORD), hash_password(PASSWORD)
    assert h1 != h2 and PASSWORD not in h1
    assert verify_password(PASSWORD, h1)
    assert not verify_password(PASSWORD + "x", h1)
    assert not verify_password(PASSWORD, "garbage")
    with pytest.raises(ValueError):
        hash_password("short")


def test_password_stored_hashed_in_db(db, users):
    with psycopg.connect(db) as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()[0]
    assert stored.startswith("scrypt$") and PASSWORD not in stored


def test_login_returns_role_from_database(client, users):
    r = client.post("/api/auth/login", json={"username": "Alice ", "password": PASSWORD}, headers=SAME_ORIGIN)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "user" and body["csrf_token"]
    me = client.get("/api/me")  # 登入回應的 Set-Cookie 已存進 client 的 cookie jar
    assert me.json() == {"id": users["alice"], "username": "alice", "role": "user",
                         "csrf_token": body["csrf_token"]}


@pytest.mark.parametrize("username,password", [("alice", "wrong-password-123"), ("nobody", PASSWORD)])
def test_bad_credentials_same_error(client, users, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers=SAME_ORIGIN)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "invalid_credentials" and err["message"] == "帳號或密碼錯誤"


def test_error_envelope_has_request_id(client):
    r = client.get("/api/me")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthorized"
    assert err["request_id"] == r.headers["X-Request-ID"]
    uuid.UUID(err["request_id"])


def test_client_request_id_is_not_trusted(client):
    r = client.get("/api/health", headers={"X-Request-ID": "attacker-chosen"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] != "attacker-chosen"


def test_validation_error_does_not_echo_password(client):
    r = client.post("/api/auth/login", json={"username": "", "password": "secret-value-here"}, headers=SAME_ORIGIN)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert "secret-value-here" not in r.text


def test_logout_revokes_token(client, users):
    headers = login(client, "alice")
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/me", headers=headers).status_code == 401


def test_expired_and_deactivated_sessions_rejected(client, db, users):
    headers = login(client, "alice")
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE auth_sessions SET created_at = now() - interval '2 day', "
                     "expires_at = now() - interval '1 day'")
    assert client.get("/api/me", headers=headers).status_code == 401

    headers = login(client, "bob")
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE users SET is_active = FALSE WHERE username = 'bob'")
    assert client.get("/api/me", headers=headers).status_code == 401
    r = client.post("/api/auth/login", json={"username": "bob", "password": PASSWORD}, headers=SAME_ORIGIN)
    assert r.status_code == 401


def test_token_not_stored_in_plaintext(client, db, users):
    token = login(client, "alice")["Cookie"].split("=", 1)[1]
    with psycopg.connect(db) as conn:
        stored = [r[0] for r in conn.execute("SELECT token_sha256 FROM auth_sessions")]
    assert token not in stored and len(stored[0]) == 64


# ---------------------------------------------------------------------------
# 角色不能由請求欄位冒用
# ---------------------------------------------------------------------------
def test_role_in_login_body_is_rejected(client, users):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD, "role": "admin"},
                    headers=SAME_ORIGIN)
    assert r.status_code == 422
    assert "csrf_token" not in r.text and COOKIE not in r.headers.get("set-cookie", "")


@pytest.mark.parametrize("path", ["/api/admin/users", "/api/admin/requests"])
def test_user_cannot_reach_admin_endpoints_even_with_spoofed_role(client, users, path):
    headers = login(client, "alice")
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers).status_code == 403
    spoofed = {**headers, "X-Role": "admin", "X-User-Role": "admin", "X-User-Id": str(users["root"]),
               "Cookie": headers["Cookie"] + "; role=admin"}
    r = client.get(path, params={"role": "admin"}, headers=spoofed)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_admin_can_reach_admin_endpoints(client, users):
    headers = login(client, "root")
    r = client.get("/api/admin/users", headers=headers)
    assert r.status_code == 200
    assert {u["username"] for u in r.json()["items"]} == {"alice", "bob", "root"}
    assert all("password_hash" not in u for u in r.json()["items"])
    assert client.get("/api/admin/requests", headers=headers).status_code == 200


def test_admin_request_log_omits_content(client, db, users):
    alice = login(client, "alice")
    conv = client.post("/api/conversations", json={}, headers=alice).json()["id"]
    with psycopg.connect(db) as conn:
        conn.execute("INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s,%s,%s,%s)",
                     (uuid.uuid4(), users["alice"], conv, "我有過敏，想要日常色"))
    r = client.get("/api/admin/requests", headers=login(client, "root"))
    item = r.json()["items"][0]
    assert item["input_tokens"] is None and "question" not in item
    assert "過敏" not in r.text


# ---------------------------------------------------------------------------
# 對話與回饋所有權
# ---------------------------------------------------------------------------
def _request_for(db, user_id, conv_id):
    rid = uuid.uuid4()
    with psycopg.connect(db) as conn:
        # S8：回饋只接受已完成的請求，所以測試用請求直接標成已完成
        conn.execute("INSERT INTO requests (id, user_id, conversation_id, question, status, completed_at) "
                     "VALUES (%s,%s,%s,'q','succeeded',now())", (rid, user_id, conv_id))
    return rid


def test_conversation_defaults_are_transparent(client, users):
    headers = login(client, "alice")
    r = client.post("/api/conversations", json={"title": "日常"}, headers=headers)
    assert r.status_code == 201
    c = r.json()
    assert (c["scenario"], c["scenario_source"], c["allergy_status"]) == ("daily", "default", "unanswered")
    r = client.post("/api/conversations", json={"scenario": "party"}, headers=headers)
    assert (r.json()["scenario"], r.json()["scenario_source"]) == ("party", "user")


def test_user_cannot_create_conversation_for_someone_else(client, users):
    headers = login(client, "alice")
    r = client.post("/api/conversations", json={"user_id": users["bob"]}, headers=headers)
    assert r.status_code == 422


def test_user_only_sees_own_conversations(client, db, users):
    alice, bob, root = login(client, "alice"), login(client, "bob"), login(client, "root")
    conv = client.post("/api/conversations", json={"title": "alice 的對話"}, headers=alice).json()["id"]
    _request_for(db, users["alice"], conv)

    r = client.get(f"/api/conversations/{conv}", headers=alice)
    assert r.status_code == 200 and len(r.json()["requests"]) == 1

    for other in (bob, root):  # 管理者也不能經由一般端點讀別人的對話
        r = client.get(f"/api/conversations/{conv}", headers=other)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
        assert client.get("/api/conversations", headers=other).json()["items"] == []
    r = client.get(f"/api/conversations/{uuid.uuid4()}", headers=bob)
    assert r.status_code == 404  # 不存在與無權限回應相同


def test_user_cannot_give_feedback_on_others_request(client, db, users):
    alice, bob = login(client, "alice"), login(client, "bob")
    conv = client.post("/api/conversations", json={}, headers=alice).json()["id"]
    rid = _request_for(db, users["alice"], conv)

    r = client.put(f"/api/requests/{rid}/feedback", json={"rating": -1}, headers=bob)
    assert r.status_code == 404
    r = client.put(f"/api/requests/{rid}/feedback", json={"rating": 1, "user_id": users["bob"]}, headers=alice)
    assert r.status_code == 422

    r = client.put(f"/api/requests/{rid}/feedback",
                   json={"rating": 1, "reason_codes": ["fits_preference"], "comment": "好"}, headers=alice)
    assert r.status_code == 200 and r.json()["rating"] == 1
    r = client.put(f"/api/requests/{rid}/feedback", json={"rating": -1}, headers=alice)
    assert r.json()["rating"] == -1
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 1


def test_unknown_route_uses_error_envelope(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
