"""UI 第一版 API：對話設定與提問、來源瀏覽、管理者審核。

文件內容為 SYNTHETIC（沿用 test_ingest 的合成 ETL 輸出），非真實文獻。
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app import ingest

from .conftest import CSRF_HEADER, login
from .test_ingest import DOC, _records, _write_root


@pytest.fixture
def imported(db, users, tmp_path):
    root = _write_root(tmp_path / "repo", _records())
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        _, sources = ingest.load_registry(root)
        ingest.import_document(conn, ingest.load_document(root, DOC, sources))
    return db


def _conv(client, headers, **body):
    r = client.post("/api/conversations", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# 對話設定
# ---------------------------------------------------------------------------
def test_patch_keeps_dislikes_when_scenario_changes(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={
        "preferences": {"liked": ["玫瑰色", " "], "disliked": ["橘色"], "finish": "matte"}})
    prefs = r.json()["preferences"]  # S6 起為 pref-v1 結構：正規化鍵＋原話
    assert r.status_code == 200 and [(i["value"], i["raw"]) for i in prefs["liked_colors"]] == [("rose", "玫瑰色")]
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={"scenario": "party"})
    body = r.json()
    assert (body["scenario"], body["scenario_source"]) == ("party", "user")
    assert [i["value"] for i in body["preferences"]["disliked_colors"]] == ["orange"]  # 改情境不清掉排斥條件


def test_allergy_states(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={"allergy_status": "none_known",
                                                                    "allergy_avoid": ["羊毛脂"]})
    assert r.status_code == 422
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={"allergy_status": "has_known",
                                                                    "allergy_avoid": ["羊毛脂"]})
    assert (r.json()["allergy_status"], r.json()["allergy_avoid"]) == ("has_known", ["羊毛脂"])
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={"allergy_status": "none_known"})
    assert (r.json()["allergy_status"], r.json()["allergy_avoid"]) == ("none_known", [])


def test_patch_and_message_owner_only_and_need_csrf(client, users):
    alice, bob = login(client, "alice"), login(client, "bob")
    cid = _conv(client, alice)
    assert client.patch(f"/api/conversations/{cid}", headers=bob, json={"scenario": "office"}).status_code == 404
    assert client.post(f"/api/conversations/{cid}/messages", headers=bob, json={"question": "q"}).status_code == 404
    no_csrf = {k: v for k, v in alice.items() if k != CSRF_HEADER}
    assert client.patch(f"/api/conversations/{cid}", headers=no_csrf, json={"scenario": "office"}).status_code == 403
    assert client.patch(f"/api/conversations/{cid}", headers=alice, json={"user_id": 1}).status_code == 422


def test_message_is_logged_but_not_answered(client, db, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    client.patch(f"/api/conversations/{cid}", headers=h, json={"preferences": {"disliked": ["橘色"]}})
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": "上班用的低調唇色"})
    assert r.status_code == 201
    body = r.json()
    # route-v4：推薦需求走問答；沒有核准片段 → 缺資料，不捏造回答，也不出推薦卡
    assert body["status"] == "answer_insufficient_evidence" and body["error_code"] is None
    assert body["recommendation"] is None
    assert body["request_id"] == r.headers["X-Request-ID"]
    with psycopg.connect(db, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM requests WHERE id = %s", (body["request_id"],)).fetchone()
    assert row["status"] == "succeeded" and row["model_name"] is None
    assert row["answer"] is None                                           # 缺資料：不回答、不捏造
    assert row["input_tokens"] is None and row["output_tokens"] is None  # 未知，不記 0
    assert [i["value"] for i in row["preferences_snapshot"]["preferences"]["disliked_colors"]] == ["orange"]
    conv = client.get(f"/api/conversations/{cid}", headers=h).json()
    assert [q["question"] for q in conv["requests"]] == ["上班用的低調唇色"]


# ---------------------------------------------------------------------------
# 來源瀏覽
# ---------------------------------------------------------------------------
def _approve_and_publish(db, chunk_keys, visibility="all"):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        rid = conn.execute("SELECT id FROM users WHERE username = 'root'").fetchone()["id"]
        ingest.apply_decisions(conn, rid, [ingest.Decision(DOC, None, k, "approve", "合成測試核准", None)
                                           for k in chunk_keys])
        ingest.publish(conn, DOC, "v1")
        conn.execute("UPDATE documents SET visibility = %s WHERE doc_key = %s", (visibility, DOC))


def test_sources_only_usable_chunks(client, imported):
    h = login(client, "alice")
    assert client.get("/api/sources/chunks", headers=h).json() == {"total": 0, "items": []}
    _approve_and_publish(imported, [f"{DOC}_p0001_b001"])
    r = client.get("/api/sources/chunks", headers=h).json()
    assert r["total"] == 1 and r["items"][0]["chunk_key"] == f"{DOC}_p0001_b001"
    assert r["items"][0]["page_start"] == 1
    docs = client.get("/api/sources/documents", headers=h).json()["items"]
    assert [(d["doc_key"], d["chunks"]) for d in docs] == [(DOC, 1)]


def test_sources_respect_visibility(client, imported):
    _approve_and_publish(imported, [f"{DOC}_p0001_b001"], visibility="admin")
    assert client.get("/api/sources/chunks", headers=login(client, "alice")).json()["total"] == 0
    assert client.get("/api/sources/chunks", headers=login(client, "root")).json()["total"] == 1


def test_sources_keyword_is_literal(client, imported):
    _approve_and_publish(imported, [f"{DOC}_p0001_b001", f"{DOC}_p0001_b002"])
    h = login(client, "alice")
    assert client.get("/api/sources/chunks", params={"q": "merged"}, headers=h).json()["total"] == 1
    assert client.get("/api/sources/chunks", params={"q": "%"}, headers=h).json()["total"] == 0


# ---------------------------------------------------------------------------
# 管理者審核
# ---------------------------------------------------------------------------
def _first_pending(client, h):
    r = client.get("/api/admin/review/chunks", params={"doc": DOC}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# 送出審核時要帶回畫面上顯示的審核對象（文件、版本、片段編號），由後端比對。
def _target(item):
    return {"doc_key": item["doc_key"], "version_label": item["version_label"], "chunk_key": item["chunk_key"]}


def test_review_requires_admin(client, imported):
    h = login(client, "alice")
    assert client.get("/api/admin/review/documents", headers=h).status_code == 403
    assert client.post("/api/admin/review/chunks/1", headers=h,
                       json={"decision": "approve", "reason": "ok", "content_sha256": "0" * 64,
                             "doc_key": DOC, "version_label": "v1", "chunk_key": f"{DOC}_p0001_b001"}).status_code == 403


def test_review_flow_records_logged_in_admin(client, imported):
    h = login(client, "root")
    page = _first_pending(client, h)
    assert page["total"] == 4 and "possible_instruction" in page["flag_labels"]
    item = page["items"][0]
    assert item["review_status"] == "pending" and item["quote_text"].startswith("SYNTHETIC")
    payload = {"decision": "approve", "reason": "已對照合成原文 p.1", "content_sha256": item["content_sha256"],
               **_target(item)}
    no_csrf = {k: v for k, v in h.items() if k != CSRF_HEADER}
    assert client.post(f"/api/admin/review/chunks/{item['id']}", headers=no_csrf, json=payload).status_code == 403
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=payload)
    assert r.status_code == 200 and r.json()["result"] == "approved"
    events = client.get(f"/api/admin/review/chunks/{item['id']}/events", headers=h).json()["items"]
    assert [(e["reviewer"], e["to_status"], e["reason"]) for e in events] == [("root", "approved", "已對照合成原文 p.1")]
    assert _first_pending(client, h)["total"] == 3


def test_review_rejects_stale_content_and_missing_reason(client, imported):
    h = login(client, "root")
    item = _first_pending(client, h)["items"][0]
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h,
                    json={"decision": "approve", "reason": "ok", "content_sha256": "f" * 64, **_target(item)})
    assert r.status_code == 409 and "內容" in r.json()["error"]["message"]
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h,
                    json={"decision": "approve", "reason": "", "content_sha256": item["content_sha256"], **_target(item)})
    assert r.status_code == 422


def test_no_bulk_approval_endpoint(client, imported):
    h = login(client, "root")
    r = client.post("/api/admin/review/chunks", headers=h, json={"decision": "approve", "reason": "全部"})
    assert r.status_code in (404, 405)


def test_publish_and_visibility(client, imported):
    h = login(client, "root")
    r = client.post("/api/admin/review/publish", headers=h, json={"doc_key": DOC, "version_label": "v1"})
    assert r.status_code == 409  # 沒有任何核准片段
    item = _first_pending(client, h)["items"][0]
    assert client.post(f"/api/admin/review/chunks/{item['id']}", headers=h,
                       json={"decision": "approve", "reason": "已對照", "content_sha256": item["content_sha256"],
                             **_target(item)}).status_code == 200
    r = client.post("/api/admin/review/publish", headers=h, json={"doc_key": DOC, "version_label": "v1"})
    assert r.status_code == 200 and r.json()["approved"] == 1
    alice = login(client, "alice")
    assert client.get("/api/sources/chunks", headers=alice).json()["total"] == 0  # 預設 admin 可見
    r = client.put(f"/api/admin/documents/{DOC}/visibility", headers=h, json={"visibility": "all"})
    assert r.status_code == 200
    assert client.get("/api/sources/chunks", headers=alice).json()["total"] == 1
    assert client.put(f"/api/admin/documents/{DOC}/visibility", headers=alice,
                      json={"visibility": "all"}).status_code == 403


def test_unknown_conversation_message_404(client, users):
    h = login(client, "alice")
    r = client.post(f"/api/conversations/{uuid.uuid4()}/messages", headers=h, json={"question": "q"})
    assert r.status_code == 404
