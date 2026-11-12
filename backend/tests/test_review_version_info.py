"""審核頁版本提示（2026-10-18）：版本資訊、最新草稿與目前發布版本、送出對象與顯示對象一致。

文件內容一律是 SYNTHETIC（沿用 test_ingest 的合成 ETL 輸出），非真實文獻。
檢查重點：
- 清單與單一版本都回報版本狀態、是否最新、最新草稿、目前發布版本（沒有發布版本為 None）。
- 送出審核時帶的文件／版本／片段編號必須是 chunk_id 實際所屬，否則 409 且不寫入任何紀錄。
- 核准非最新版本須明確確認；畫面載入後才被新版本取代的情況也會擋下。
- 不自動轉移核准：新版本片段一律 pending，舊版本既有核准不變。
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from app import ingest

from .conftest import login
from .test_ingest import DOC, _records, _write_root


# 匯入一個 SYNTHETIC 版本；text 不同就會產生新版本（v1、v2…），回傳匯入結果。
def _import(db, tmp_path, name: str, text: str | None = None) -> dict:
    recs = _records()
    if text is not None:
        recs[0]["display_text"] = text
    root = _write_root(tmp_path / name, recs)
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        _, sources = ingest.load_registry(root)
        return ingest.import_document(conn, ingest.load_document(root, DOC, sources))


# 取審核清單（可指定版本與狀態），斷言成功並回傳 JSON。
def _page(client, h, version: str | None = None, status: str = "pending") -> dict:
    params = {"doc": DOC, "status": status, **({"version": version} if version else {})}
    r = client.get("/api/admin/review/chunks", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# 依清單裡的一筆片段組出送審內容（就是審核頁送出的欄位）；extra 可覆寫欄位做錯誤情境。
def _payload(item: dict, decision: str = "approve", **extra) -> dict:
    return {"decision": decision, "reason": "已對照合成原文", "content_sha256": item["content_sha256"],
            "doc_key": item["doc_key"], "version_label": item["version_label"], "chunk_key": item["chunk_key"],
            **extra}


# 直接從資料庫讀一筆片段的審核狀態與事件數，用來確認被拒絕的請求沒有寫入任何東西。
def _db_state(db, chunk_id: int) -> tuple[str, int]:
    with psycopg.connect(db, row_factory=dict_row) as conn:
        status = conn.execute("SELECT review_status FROM chunks WHERE id = %s", (chunk_id,)).fetchone()["review_status"]
        n = conn.execute("SELECT count(*) AS n FROM chunk_review_events WHERE chunk_id = %s", (chunk_id,)).fetchone()["n"]
    return status, n


# 審核清單「文件版本表」中，某文件的版本標籤 → 該列。
def _versions(client, h) -> dict[str, dict]:
    rows = client.get("/api/admin/review/documents", headers=h).json()["items"]
    return {r["version_label"]: r for r in rows if r["doc_key"] == DOC}


@pytest.fixture
def h(client, users):
    return login(client, "root")


# ---------------------------------------------------------------------------
# 版本資訊
# ---------------------------------------------------------------------------
def test_single_draft_is_latest_and_has_no_published_version(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    v = _versions(client, h)["v1"]
    assert (v["status"], v["is_latest"], v["latest_label"]) == ("draft", True, "v1")
    assert (v["latest_draft_label"], v["published_label"]) == ("v1", None)
    page = _page(client, h)
    assert page["version"] == {"doc_key": DOC, "version_label": "v1", "status": "draft", "is_active": True,
                               "is_latest": True, "latest_label": "v1", "latest_draft_label": "v1",
                               "published_label": None}
    assert all((it["doc_key"], it["version_label"], it["version_status"]) == (DOC, "v1", "draft")
               for it in page["items"])


def test_latest_draft_and_published_version_are_reported_separately(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    vs = _versions(client, h)
    assert (vs["v1"]["is_latest"], vs["v2"]["is_latest"]) == (False, True)
    assert {r["latest_label"] for r in vs.values()} == {"v2"}
    # 未指定版本 → 列最新版本，版本資訊與清單是同一版
    page = _page(client, h)
    assert page["version"]["version_label"] == "v2" and {it["version_label"] for it in page["items"]} == {"v2"}
    # 指定舊版 → 回報非最新與最新版本標籤
    old = _page(client, h, "v1")["version"]
    assert (old["version_label"], old["is_latest"], old["latest_label"]) == ("v1", False, "v2")

    # 核准 v2 一筆後發布：目前發布版本 v2；v1 從未發布、仍是草稿，但比發布版本舊，不算「最新草稿」
    item = _page(client, h, "v2")["items"][0]
    assert client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item)).status_code == 200
    assert client.post("/api/admin/review/publish", headers=h,
                       json={"doc_key": DOC, "version_label": "v2"}).status_code == 200
    vs = _versions(client, h)
    assert (vs["v1"]["status"], vs["v2"]["status"]) == ("draft", "published")
    assert all((r["published_label"], r["latest_draft_label"]) == ("v2", None) for r in vs.values())

    # 再匯入 v3：最新草稿 v3 與目前發布版本 v2 分開；v2 不再是最新
    _import(db, tmp_path, "v3", "SYNTHETIC: v3 text.")
    vs = _versions(client, h)
    assert {(k, r["is_latest"]) for k, r in vs.items()} == {("v1", False), ("v2", False), ("v3", True)}
    assert all((r["latest_draft_label"], r["published_label"]) == ("v3", "v2") for r in vs.values())


def test_unknown_version_is_404(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    r = client.get("/api/admin/review/chunks", params={"doc": DOC, "version": "v9"}, headers=h)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 顯示對象與送出對象一致
# ---------------------------------------------------------------------------
def test_every_listed_item_can_be_submitted_as_displayed(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    for item in _page(client, h)["items"]:
        r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item, "reject"))
        assert r.status_code == 200, r.text
        assert (r.json()["doc_key"], r.json()["version_label"], r.json()["chunk_key"]) == (
            item["doc_key"], item["version_label"], item["chunk_key"])


@pytest.mark.parametrize("override", [
    {"version_label": "v2"},                       # 畫面是 v1，送出寫 v2
    {"doc_key": "OTHER_DOC"},                      # 文件不同
    {"chunk_key": f"{DOC}_p0003_b001"},            # 同版本的另一筆
])
def test_mismatched_target_is_rejected_without_writing(client, db, h, tmp_path, override):
    _import(db, tmp_path, "v1")
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    item = _page(client, h, "v1")["items"][0]
    assert item["chunk_key"] != f"{DOC}_p0003_b001"
    before = _db_state(db, item["id"])
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h,
                    json=_payload(item, confirm_not_latest=True, **override))
    assert r.status_code == 409 and r.json()["error"]["code"] == "review_target_mismatch"
    assert _db_state(db, item["id"]) == before == ("pending", 0)


def test_target_fields_are_required(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    item = _page(client, h)["items"][0]
    body = _payload(item)
    for missing in ("doc_key", "version_label", "chunk_key"):
        r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h,
                        json={k: v for k, v in body.items() if k != missing})
        assert r.status_code == 422, missing
    assert _db_state(db, item["id"]) == ("pending", 0)


# ---------------------------------------------------------------------------
# 非最新版本
# ---------------------------------------------------------------------------
def test_approving_old_version_needs_explicit_confirmation(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    item = _page(client, h, "v1")["items"][0]
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item))
    assert r.status_code == 409 and r.json()["error"]["code"] == "review_not_latest_version"
    assert "v2" in r.json()["error"]["message"]
    assert _db_state(db, item["id"]) == ("pending", 0)
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item, confirm_not_latest=True))
    assert r.status_code == 200 and r.json()["result"] == "approved"


def test_rejecting_old_version_does_not_need_confirmation(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    item = _page(client, h, "v1")["items"][0]
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item, "reject"))
    assert r.status_code == 200 and r.json()["result"] == "rejected"


def test_version_superseded_after_page_load_is_caught(client, db, h, tmp_path):
    # 審核人載入 v1（當時是最新）後，有人匯入 v2；沒看到提示的核准要被擋下
    _import(db, tmp_path, "v1")
    page = _page(client, h)
    assert page["version"]["is_latest"] is True
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    item = page["items"][0]
    r = client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item))
    assert r.status_code == 409 and r.json()["error"]["code"] == "review_not_latest_version"
    assert _db_state(db, item["id"]) == ("pending", 0)


# ---------------------------------------------------------------------------
# 不自動轉移核准、不改既有核准
# ---------------------------------------------------------------------------
def test_no_automatic_transfer_between_versions(client, db, h, tmp_path):
    _import(db, tmp_path, "v1")
    item = _page(client, h)["items"][1]
    assert client.post(f"/api/admin/review/chunks/{item['id']}", headers=h, json=_payload(item)).status_code == 200
    _import(db, tmp_path, "v2", "SYNTHETIC: v2 text.")
    # v1 已核准的那筆仍是已核准；v2 同編號片段仍待審（只會在 prior_decision 提示，不沿用）
    assert [it["chunk_key"] for it in _page(client, h, "v1", "approved")["items"]] == [item["chunk_key"]]
    v2 = {it["chunk_key"]: it for it in _page(client, h, "v2")["items"]}
    assert item["chunk_key"] in v2 and v2[item["chunk_key"]]["review_status"] == "pending"
    assert _versions(client, h)["v2"]["approved"] == 0
    # 讀取版本資訊不改變任何狀態
    _page(client, h, "v1")
    _versions(client, h)
    assert _db_state(db, item["id"]) == ("approved", 1)
