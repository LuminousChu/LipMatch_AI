"""S9 治理測試：版本發布／切換／回復、停用與期限、可見範圍與角色變更、檢索後狀態變動、歷史存取。

SYNTHETIC／MOCK（不是正式驗收）：
- 每個測試都在全新、隨機命名的測試資料庫執行（conftest），不連任何正式資料庫、不動正式資料。
- 文件、片段、候選都是合成的（SYN_*、PUBLIC 等）；MockEmbedder 以雜湊產生向量，沒有語意，只驗證過濾與流程。
- FakeGenerator 是生成模型的測試替身（不是真實模型），用來在「送模型當下」插入狀態變動。
測試編號（G01…）對應 docs/mvp/s9-governance.md 的治理測試矩陣。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from app import recommend as rec
from app import release, retrieval
from app.release import ReleaseError
from tests.card_history import seed_card_history
from tests.conftest import login
from tests.test_qa import FakeGenerator, MockEmbedder, QUESTION, _doc, make_client, ok_output  # noqa: F401（共用 fixture）
from tests.test_recommend import NUDE, _activate_all, _make_docs, _run

OP = "pytest-s9"           # 測試用操作人標示
MULTI = "上班用的自然唇色，不要霧面"


class BrokenEmbedder(MockEmbedder):
    """MOCK：嵌入到第 fail_after 批時拋錯，模擬嵌入模型逾時或中斷。"""

    def __init__(self, fail_after: int = 0):
        self.calls, self.fail_after = 0, fail_after

    # 前 fail_after 次正常回傳向量，之後一律拋出 TimeoutError。
    def embed(self, texts):
        self.calls += 1
        if self.calls > self.fail_after:
            raise TimeoutError("SYNTHETIC embedding timeout")
        return super().embed(texts)


# ---------------------------------------------------------------------------
# 共用輔助
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(db, users):
    """autocommit＋dict_row 的測試連線（與 CLI 相同的連線型態）。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _sha(text: str) -> str:
    """片段內容指紋（測試用：只以文字計算）。"""
    return hashlib.sha256(text.encode()).hexdigest()


def _new_version(conn, doc_key: str, label: str, admin_id: int, *, changes: dict[str, str] | None = None,
                 approve: bool = True, extra: dict[str, str] | None = None, pending: set[str] = frozenset()) -> int:
    """複製文件目前 published 版本的片段，建立新的 draft 版本（可改寫部分片段、加新片段）；回傳版本 id。

    changes：{chunk_key: 新文字}；extra：新增的 {chunk_key: 文字}；
    approve=True 時新版本片段逐筆以管理者核准，但 pending 內的片段留待審。
    """
    doc = conn.execute("SELECT id FROM documents WHERE doc_key = %s", (doc_key,)).fetchone()
    vid = conn.execute(
        """INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version)
           VALUES (%s, %s, %s, 'test-s9') RETURNING id""", (doc["id"], label, _sha(label))).fetchone()["id"]
    rows = conn.execute(
        """SELECT c.chunk_key, c.ordinal, c.page_start, c.quote_text FROM chunks c
             JOIN document_versions dv ON dv.id = c.document_version_id
            WHERE dv.document_id = %s AND dv.status = 'published' ORDER BY c.ordinal""", (doc["id"],)).fetchall()
    items = [(r["chunk_key"], r["ordinal"], r["page_start"], (changes or {}).get(r["chunk_key"], r["quote_text"]))
             for r in rows]
    items += [(k, 100 + i, 9, t) for i, (k, t) in enumerate((extra or {}).items())]
    for key, ordinal, page, text in items:
        cid = conn.execute(
            """INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text,
                                   content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (vid, key, ordinal, page, text, text, _sha(text))).fetchone()["id"]
        if approve and key not in pending:
            conn.execute("SELECT review_chunk(%s, %s, 'approve', 'S9 合成測試核准', %s)", (cid, admin_id, _sha(text)))
    return vid


def _status(conn, doc_key: str) -> dict[str, str]:
    """{版本標籤: 狀態}。"""
    return {r["version_label"]: r["status"] for r in conn.execute(
        """SELECT dv.version_label, dv.status FROM document_versions dv JOIN documents d ON d.id = dv.document_id
            WHERE d.doc_key = %s""", (doc_key,))}


def _active_label(conn) -> str | None:
    """目前 active 索引的標籤。"""
    row = retrieval.active_index(conn)
    return row["label"] if row else None


def _events(conn, doc_key: str) -> list[str]:
    """一份文件的事件類型（依時間）。"""
    return [e["event_type"] for e in release.history(conn, doc_key)]


def _searchable_versions(conn, doc_key: str) -> set[str]:
    """這份文件目前可被檢索（active 索引中）的版本。"""
    return {r["version_label"] for r in conn.execute(
        "SELECT DISTINCT version_label FROM searchable_chunks WHERE doc_key = %s", (doc_key,))}


@pytest.fixture
def indexed(conn, users):
    """兩份公開合成文件 PUBLIC、OTHER（v1 已發布）＋ active MOCK 索引 idx0。"""
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _doc(conn, "OTHER", admin, "SYNTHETIC other document text")
    retrieval.build_index(conn, "idx0", MockEmbedder())
    retrieval.activate_index(conn, "idx0")
    return admin


# ===========================================================================
# A. 發布與切換（待發布 → 審核 → 向量 → 驗證 → 切換）
# ===========================================================================
def test_g01_publish_without_active_index_records_vectors_not_applicable(conn, users):
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _new_version(conn, "PUBLIC", "v2", admin, changes={"PUBLIC_p0004_b001": "SYNTHETIC v2 text"})
    p = release.plan(conn, "PUBLIC", "v2")
    assert p["problems"] == [] and p["vectors"] == "not_applicable_no_active_index"
    out = release.switch(conn, "PUBLIC", "v2", actor=OP)
    assert (out["published"], out["retired"], out["vectors"]) == ("v2", "v1", "not_applicable_no_active_index")
    assert _status(conn, "PUBLIC") == {"v1": "retired", "v2": "published"}
    ev = release.history(conn, "PUBLIC")[-1]
    assert ev["event_type"] == "publish" and ev["from_version"] == "v1" and ev["to_version"] == "v2"
    assert ev["actor"] == OP


def test_g02_full_pipeline_with_index_reuses_unchanged_vectors(conn, users, indexed):
    admin = indexed
    _new_version(conn, "PUBLIC", "v2", admin, extra={"PUBLIC_p0009_b002": "SYNTHETIC new paragraph in v2"})
    prep = release.prepare(conn, "PUBLIC", "v2", MockEmbedder(), actor=OP, index_label="idx1")
    # 目標集合：OTHER v1（沿用）＋ PUBLIC v2 的舊段落（內容相同 → 沿用）＋ 新段落（嵌入）
    assert (prep["reused"], prep["embedded"]) == (2, 1)
    assert _active_label(conn) == "idx0" and _status(conn, "PUBLIC")["v2"] == "draft"   # 準備不影響線上
    out = release.switch(conn, "PUBLIC", "v2", actor=OP, index_label="idx1")
    assert out["vectors"] == "activated" and _active_label(conn) == "idx1"
    assert _searchable_versions(conn, "PUBLIC") == {"v2"} and _searchable_versions(conn, "OTHER") == {"v1"}
    hits = retrieval.search(conn, "SYNTHETIC new paragraph in v2", "user", MockEmbedder())
    assert hits[0].chunk_key == "PUBLIC_p0009_b002" and hits[0].version_label == "v2"
    assert _events(conn, "PUBLIC") == ["prepare", "publish"]


def test_g03_switch_without_prepared_index_keeps_old_version(conn, users, indexed):
    _new_version(conn, "PUBLIC", "v2", indexed)
    with pytest.raises(ReleaseError, match="須先 prepare"):
        release.switch(conn, "PUBLIC", "v2", actor=OP)
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"}
    assert _active_label(conn) == "idx0" and _searchable_versions(conn, "PUBLIC") == {"v1"}
    refused = release.history(conn, "PUBLIC")[-1]
    assert refused["event_type"] == "refused" and refused["detail"]["action"] == "publish"


def test_g04_embedding_failure_leaves_no_half_index_and_old_version_serves(conn, users, indexed):
    _new_version(conn, "PUBLIC", "v2", indexed, changes={"PUBLIC_p0004_b001": "SYNTHETIC changed"})
    with pytest.raises(ReleaseError, match="嵌入模型呼叫失敗"):
        release.prepare(conn, "PUBLIC", "v2", BrokenEmbedder(), actor=OP, index_label="idx-bad")
    assert conn.execute("SELECT count(*) AS n FROM index_versions WHERE label = 'idx-bad'").fetchone()["n"] == 0
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"} and _active_label(conn) == "idx0"
    assert retrieval.search(conn, QUESTION, "user", MockEmbedder())[0].version_label == "v1"
    assert _events(conn, "PUBLIC") == ["refused"]


def test_g05_stale_prepared_index_is_refused(conn, users, indexed):
    _new_version(conn, "PUBLIC", "v2", indexed)
    release.prepare(conn, "PUBLIC", "v2", MockEmbedder(), actor=OP, index_label="idx1")
    retrieval.build_index(conn, "idx-other", MockEmbedder())   # 另一個流程先換了 active 索引
    retrieval.activate_index(conn, "idx-other")
    with pytest.raises(ReleaseError, match="基準索引已不是目前 active"):
        release.switch(conn, "PUBLIC", "v2", actor=OP, index_label="idx1")
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"} and _active_label(conn) == "idx-other"


def test_g06_change_after_prepare_rolls_back_whole_switch(conn, users, indexed):
    admin = indexed
    # 先讓 OTHER 上線 v2，其中一個片段留待審（已發布版本可以繼續審核）
    text = "SYNTHETIC late approved paragraph"
    _new_version(conn, "OTHER", "v2", admin, extra={"OTHER_p0009_b002": text}, pending={"OTHER_p0009_b002"})
    release.prepare(conn, "OTHER", "v2", MockEmbedder(), actor=OP, index_label="idx-o2")
    release.switch(conn, "OTHER", "v2", actor=OP, index_label="idx-o2")
    # PUBLIC v2 準備好索引之後，OTHER 那一筆才被核准 → PUBLIC 的新索引缺這一筆向量
    _new_version(conn, "PUBLIC", "v2", admin)
    release.prepare(conn, "PUBLIC", "v2", MockEmbedder(), actor=OP, index_label="idx-p2")
    late = conn.execute("SELECT id FROM chunks WHERE chunk_key = 'OTHER_p0009_b002'").fetchone()["id"]
    conn.execute("SELECT review_chunk(%s, %s, 'approve', '晚到的核准', %s)", (late, admin, _sha(text)))
    with pytest.raises(ReleaseError, match="新索引未涵蓋"):
        release.switch(conn, "PUBLIC", "v2", actor=OP, index_label="idx-p2")
    # 版本狀態在同一交易內已改過又整個回滾：舊版仍上線、新版仍是 draft、索引不變
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"} and _active_label(conn) == "idx-o2"


def test_g07_published_content_never_overwritten_and_events_append_only(conn, users):
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _new_version(conn, "PUBLIC", "v2", admin)
    release.switch(conn, "PUBLIC", "v2", actor=OP)
    for label in ("v1", "v2"):   # retired 與 published 版本的片段內容都不可原地改
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("""UPDATE chunks SET quote_text = 'tampered' WHERE document_version_id =
                            (SELECT dv.id FROM document_versions dv JOIN documents d ON d.id = dv.document_id
                              WHERE d.doc_key = 'PUBLIC' AND dv.version_label = %s)""", (label,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE document_versions SET etl_version = 'x' WHERE version_label = 'v2'")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE document_release_events SET actor = 'someone-else'")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM document_release_events")


def test_g08_draft_without_approved_chunks_is_refused(conn, users):
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _new_version(conn, "PUBLIC", "v2", admin, approve=False)
    with pytest.raises(ReleaseError, match="沒有任何已核准"):
        release.switch(conn, "PUBLIC", "v2", actor=OP)
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"}


# ===========================================================================
# B. 安全切回舊版
# ===========================================================================
def test_g09_rollback_restores_old_version_and_index_reusing_vectors(conn, users, indexed):
    admin = indexed
    _new_version(conn, "PUBLIC", "v2", admin, changes={"PUBLIC_p0004_b001": "SYNTHETIC wrong v2 text"})
    release.prepare(conn, "PUBLIC", "v2", MockEmbedder(), actor=OP, index_label="idx1")
    release.switch(conn, "PUBLIC", "v2", actor=OP, index_label="idx1")
    with pytest.raises(ReleaseError, match="原因"):
        release.rollback(conn, "PUBLIC", "v1", actor=OP, reason="")
    with pytest.raises(psycopg.errors.CheckViolation):   # 一般 UPDATE 不能把 retired 切回 published
        conn.execute("""UPDATE document_versions SET status = 'published', retired_at = NULL
                         WHERE version_label = 'v1' AND document_id = (SELECT id FROM documents WHERE doc_key = 'PUBLIC')""")
    prep = release.prepare(conn, "PUBLIC", "v1", BrokenEmbedder(fail_after=0), actor=OP, mode="rollback",
                           index_label="idx-rb")
    assert prep["embedded"] == 0     # 舊版向量全部沿用舊索引，不需呼叫嵌入模型（BrokenEmbedder 沒被呼叫）
    out = release.rollback(conn, "PUBLIC", "v1", actor=OP, reason="v2 內容有誤，切回 v1", index_label="idx-rb")
    assert (out["published"], out["retired"]) == ("v1", "v2")
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "retired"} and _active_label(conn) == "idx-rb"
    assert retrieval.search(conn, QUESTION, "user", MockEmbedder())[0].version_label == "v1"
    assert _events(conn, "PUBLIC")[-3:] == ["publish", "prepare", "rollback"]   # 缺原因屬輸入檢查，未開始切換、不記事件
    assert release.history(conn, "PUBLIC")[-1]["reason"] == "v2 內容有誤，切回 v1"


def test_g10_candidate_rules_invalidate_with_version_and_return_after_rollback(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    assert NUDE in [r["option_key"] for r in _run(conn, MULTI).recommendations]
    _new_version(conn, "SYN_S7_B", "v2", admin)            # 內容相同，但候選證據指向 v1 的片段
    plan = release.plan(conn, "SYN_S7_B", "v2")
    assert [c["option_key"] for c in plan["candidate_impact"]] == [NUDE]
    with pytest.raises(ReleaseError, match="accept-candidate-impact"):
        release.switch(conn, "SYN_S7_B", "v2", actor=OP)
    assert _status(conn, "SYN_S7_B") == {"v1": "published", "v2": "draft"}
    release.switch(conn, "SYN_S7_B", "v2", actor=OP, accept_candidate_impact=True)
    after = _run(conn, MULTI)
    assert NUDE not in [r["option_key"] for r in after.recommendations]       # 依據同步失效
    trace = next(t for t in after.trace if t["option_key"] == NUDE)
    assert "version_not_published" in json.dumps(trace, ensure_ascii=False)
    release.rollback(conn, "SYN_S7_B", "v1", actor=OP, reason="候選證據尚未改指新版本",
                     accept_candidate_impact=True)
    assert NUDE in [r["option_key"] for r in _run(conn, MULTI).recommendations]


# ===========================================================================
# C. 停用、未生效、到期、可見範圍（檢索與候選立即生效）
# ===========================================================================
@pytest.mark.parametrize("change, undo", [
    (lambda c: release.deactivate(c, "PUBLIC", actor=OP, reason="來源授權待確認"),
     lambda c: release.reactivate(c, "PUBLIC", actor=OP, reason="授權已確認")),
    (lambda c: release.set_validity(c, "PUBLIC", datetime.now(timezone.utc) + timedelta(days=1), None, actor=OP),
     lambda c: release.set_validity(c, "PUBLIC", None, None, actor=OP)),
    (lambda c: release.set_validity(c, "PUBLIC", None, datetime.now(timezone.utc) - timedelta(seconds=1), actor=OP),
     lambda c: release.set_validity(c, "PUBLIC", None, None, actor=OP)),
], ids=["G11-deactivate", "G12-not-yet-valid", "G13-expired"])
def test_g11_g13_document_state_blocks_retrieval_immediately(conn, users, indexed, change, undo):
    assert "PUBLIC" in {h.doc_key for h in retrieval.search(conn, QUESTION, "user", MockEmbedder())}
    change(conn)
    assert "PUBLIC" not in {h.doc_key for h in retrieval.search(conn, QUESTION, "admin", MockEmbedder())}
    undo(conn)
    assert "PUBLIC" in {h.doc_key for h in retrieval.search(conn, QUESTION, "user", MockEmbedder())}
    assert set(_events(conn, "PUBLIC")) <= {"deactivate", "reactivate", "validity"}


def test_g14_visibility_change_applies_per_role(conn, users, indexed):
    release.set_visibility(conn, "PUBLIC", "admin", actor=OP, reason="改為管理者限定")
    assert "PUBLIC" not in {h.doc_key for h in retrieval.search(conn, QUESTION, "user", MockEmbedder())}
    assert "PUBLIC" in {h.doc_key for h in retrieval.search(conn, QUESTION, "admin", MockEmbedder())}
    ev = release.history(conn, "PUBLIC")[-1]
    assert ev["event_type"] == "visibility" and ev["detail"]["before"] == {"visibility": "all"}


# ===========================================================================
# D. 檢索後、送模型與回傳前的狀態變動（API 層）
# ===========================================================================
def _ask(client, user: str, question: str = QUESTION) -> tuple[dict, dict]:
    """以指定使用者在新對話提問；回傳（送出回應, 請求標頭）。"""
    h = login(client, user)
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": question})
    assert r.status_code == 201, r.text
    return r.json(), h


def _sql(db, query, params=()):
    """以獨立連線執行一段 SQL（模擬另一個管理者／流程在同一時間操作）。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        return c.execute(query, params)


def test_g16_source_restricted_during_model_call_stops_answer(make_client, db, indexed):
    gen = FakeGenerator(ok_output("E1"),
                        on_call=lambda: _sql(db, "UPDATE documents SET visibility = 'admin' WHERE doc_key = 'PUBLIC'"))
    with make_client(gen) as client:
        body, _ = _ask(client, "alice")
    assert body["status"] == "answer_failed" and body["error_code"] == "evidence_changed"
    assert body["answer"] is None and QUESTION not in json.dumps(body, ensure_ascii=False)


def test_g17_version_switch_during_model_call_stops_answer(make_client, db, conn, indexed):
    _new_version(conn, "PUBLIC", "v2", indexed)
    release.prepare(conn, "PUBLIC", "v2", MockEmbedder(), actor=OP, index_label="idx1")

    # 模型呼叫當下，另一個流程把 PUBLIC 切到 v2（引用的 v1 片段變成 retired）
    def switch_now():
        with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
            release.switch(c, "PUBLIC", "v2", actor=OP, index_label="idx1")
    with make_client(FakeGenerator(ok_output("E1"), on_call=switch_now)) as client:
        body, _ = _ask(client, "alice")
    assert body["error_code"] == "evidence_changed" and body["answer"] is None
    assert _status(conn, "PUBLIC") == {"v1": "retired", "v2": "published"}


def test_g18_role_demoted_during_request_masks_response(make_client, db, users, conn):
    admin = users["root"]
    _doc(conn, "ADMIN_DOC", admin, QUESTION, visibility="admin")
    retrieval.build_index(conn, "idx0", MockEmbedder())
    retrieval.activate_index(conn, "idx0")
    demote = lambda: _sql(db, "UPDATE users SET role = 'user' WHERE username = 'root'")  # noqa: E731
    with make_client(FakeGenerator(ok_output("E1"), on_call=demote)) as client:
        body, h = _ask(client, "root")
        assert body["status"] == "answered"                     # 日誌照實記錄當時的結果
        assert body["masked"] is True and body["answer"] is None and body["qa"]["citations"] == []
        assert QUESTION not in json.dumps(body, ensure_ascii=False)
        assert client.get("/api/admin/users", headers=h).status_code == 403   # 角色每次請求重查


def test_g19_recommendation_evidence_changed_before_return_is_recomputed(conn, users, monkeypatch):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    real_load, calls = rec.load_candidates, []

    # 第一次載入候選後，SYN_S7_B 被改成管理者限定（判斷與回傳之間）
    def racing_load(c, role, statuses=("active",)):
        out = real_load(c, role, statuses)
        calls.append(1)
        if len(calls) == 1:
            release.set_visibility(conn, "SYN_S7_B", "admin", actor=OP)
        return out
    monkeypatch.setattr(rec, "load_candidates", racing_load)
    d = _run(conn, MULTI)
    assert d.rechecks == 1 and NUDE not in [r["option_key"] for r in d.recommendations]


# ===========================================================================
# E. 歷史回答、引用與上下文查看依目前權限
# ===========================================================================
def _view(client, h, request_id: str) -> dict:
    """讀回一次請求的結果檢視。"""
    r = client.get(f"/api/requests/{request_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _rec_setup(db, users):
    """合成文件 SYN_S7_A／B（v1 已發布）＋合成候選全部啟用。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        _make_docs(c, users["root"])
        _activate_all(c, users["root"])


def _b_citations(body: dict) -> list[dict]:
    """推薦結果中引用 SYN_S7_B 的引用物件。"""
    return [c for r in body["recommendation"]["recommendations"] for cl in r["claims"]
            for c in cl["citations"] if c.get("doc_key") == "SYN_S7_B" or c.get("cited_version_label")]


def _card_history(client, db, user: str) -> tuple[dict, dict]:
    """route-v4 起正式路由不出推薦卡：送出 MULTI 後把這則請求改寫成舊版推薦卡紀錄，回傳（當時的結果檢視, 標頭）。"""
    body, h = _ask(client, user, MULTI)
    seed_card_history(db, body["request_id"])
    return _view(client, h, body["request_id"]), h


def test_g20_superseded_same_content_keeps_answer_and_traces_version(client, db, users, conn):
    _rec_setup(db, users)
    body, h = _card_history(client, db, "alice")
    assert body["status"] == "recommended" and _b_citations(body)
    _new_version(conn, "SYN_S7_B", "v2", users["root"])          # 內容完全相同的新版本
    release.switch(conn, "SYN_S7_B", "v2", actor=OP, accept_candidate_impact=True)
    view = _view(client, h, body["request_id"])
    assert view["masked"] is False and view["answer"] == body["answer"]
    cite = _b_citations(view)[0]
    assert cite["available"] is True and cite["superseded"] is True
    assert (cite["version_label"], cite["cited_version_label"]) == ("v2", "v1")
    assert client.get("/api/sources/citation", headers=h, params={
        "doc": "SYN_S7_B", "version": "v2", "chunk": cite["chunk_key"]}).status_code == 200


def test_g21_superseded_changed_content_masks_answer_and_admin_can_trace(client, db, users, conn):
    _rec_setup(db, users)
    body, h = _card_history(client, db, "alice")
    old_key = _b_citations(body)[0]["chunk_key"]
    _new_version(conn, "SYN_S7_B", "v2", users["root"], changes={old_key: "SYNTHETIC corrected evidence"})
    release.switch(conn, "SYN_S7_B", "v2", actor=OP, accept_candidate_impact=True)
    view = _view(client, h, body["request_id"])
    assert view["masked"] is True and view["answer"] is None and view["recommendation"] is None
    assert view["limitations"] == [] and "SYN_S7_B" not in json.dumps(view, ensure_ascii=False)
    conv = client.get(f"/api/conversations/{view['conversation_id']}", headers=h).json()
    row = conv["requests"][0]
    assert row["answer"] is None and row["answer_masked"] is True and "detail" not in row
    # 管理者追溯：當時用的版本 v1 與內容指紋仍在，並說明此刻為何不可用；不含問題與回答原文
    ha = login(client, "root")
    trace = client.get(f"/api/admin/requests/{body['request_id']}/trace", headers=ha).json()
    b = next(c for c in trace["citations"] if c["doc_key"] == "SYN_S7_B")
    assert (b["version_label"], b["version_status_now"], b["state_for_owner_now"]) == ("v1", "retired", "unavailable")
    assert b["reason_for_admin"] == "superseded_content_changed" and b["content_sha256"]
    assert trace["owner_view_masked"] is True and MULTI not in json.dumps(trace, ensure_ascii=False, default=str)
    assert client.get(f"/api/admin/requests/{body['request_id']}/trace", headers=h).status_code == 403


@pytest.mark.parametrize("change", ["deactivate", "expire", "admin_only", "reject_chunk"])
def test_g22_history_masked_after_source_state_change(client, db, users, conn, change):
    _rec_setup(db, users)
    body, h = _card_history(client, db, "alice")
    key = _b_citations(body)[0]["chunk_key"]
    if change == "deactivate":
        release.deactivate(conn, "SYN_S7_B", actor=OP, reason="SYNTHETIC 停用")
    elif change == "expire":
        release.set_validity(conn, "SYN_S7_B", None, datetime.now(timezone.utc) - timedelta(seconds=1), actor=OP)
    elif change == "admin_only":
        release.set_visibility(conn, "SYN_S7_B", "admin", actor=OP)
    else:
        cid = conn.execute("SELECT id, content_sha256 FROM chunks WHERE chunk_key = %s", (key,)).fetchone()
        conn.execute("SELECT review_chunk(%s, %s, 'reject', 'SYNTHETIC 退回', %s)",
                     (cid["id"], users["root"], cid["content_sha256"]))
    view = _view(client, h, body["request_id"])
    assert view["masked"] is True and view["answer"] is None
    assert key not in json.dumps(view, ensure_ascii=False)
    assert client.get("/api/sources/citation", headers=h, params={
        "doc": "SYN_S7_B", "version": "v1", "chunk": key}).status_code == 404


def test_g23_uncited_context_chunk_restricted_masks_qa_history(make_client, db, conn, indexed):
    with make_client(FakeGenerator(ok_output("E1"))) as client:
        body, h = _ask(client, "alice")
        assert body["status"] == "answered" and body["masked"] is False
        log = _sql(db, "SELECT detail FROM requests WHERE id = %s", (body["request_id"],)).fetchone()["detail"]
        assert {e["doc_key"] for e in log["qa"]["context_sent"]["evidence"]} == {"PUBLIC", "OTHER"}
        release.set_visibility(conn, "OTHER", "admin", actor=OP)    # 只在上下文、沒被引用的片段
        view = _view(client, h, body["request_id"])
    assert view["masked"] is True and view["answer"] is None and view["qa"]["citations"] == []


def test_g24_role_change_applies_to_own_history(make_client, db, users, conn):
    admin = users["root"]
    _doc(conn, "ADMIN_DOC", admin, QUESTION, visibility="admin")
    retrieval.build_index(conn, "idx0", MockEmbedder())
    retrieval.activate_index(conn, "idx0")
    with make_client(FakeGenerator(ok_output("E1"))) as client:
        body, h = _ask(client, "root")
        assert body["status"] == "answered" and body["masked"] is False
        conn.execute("UPDATE users SET role = 'user' WHERE username = 'root'")
        assert _view(client, h, body["request_id"])["masked"] is True        # 降級：看不到當時的管理者文件
        conn.execute("UPDATE users SET role = 'admin' WHERE username = 'root'")
        assert _view(client, h, body["request_id"])["masked"] is False       # 恢復：依目前權限又可看
        conn.execute("UPDATE users SET is_active = FALSE WHERE username = 'root'")
        assert client.get(f"/api/requests/{body['request_id']}", headers=h).status_code == 401


def test_g25_api_publish_refused_is_recorded_and_old_version_kept(client, db, users, conn):
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _new_version(conn, "PUBLIC", "v2", admin, approve=False)
    h = login(client, "root")
    r = client.post("/api/admin/review/publish", headers=h, json={"doc_key": "PUBLIC", "version_label": "v2"})
    assert r.status_code == 409 and "沒有任何已核准" in r.json()["error"]["message"]
    assert _status(conn, "PUBLIC") == {"v1": "published", "v2": "draft"}
    hist = client.get("/api/admin/documents/PUBLIC/releases", headers=h).json()
    assert hist["events"][-1]["event_type"] == "refused" and hist["events"][-1]["actor"] == "admin:root"
    assert client.get("/api/admin/documents/PUBLIC/releases", headers=login(client, "alice")).status_code == 403
    # API 改可見範圍也寫事件
    assert client.put("/api/admin/documents/PUBLIC/visibility", headers=h,
                      json={"visibility": "admin"}).json()["visibility"] == "admin"
    assert _events(conn, "PUBLIC")[-1] == "visibility"
