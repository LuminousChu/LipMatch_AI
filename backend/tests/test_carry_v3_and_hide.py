"""carry-v3 與對話刪除（2026-11-07）。

整支測試在檢查什麼：
1. carry-v3：先「只說條件」（系統只記條件、不回答），再問「那要擦什麼顏色好」——檢索要把前面那幾句條件接在前面；
   給模型的問題仍是使用者本句。上一則是有回答的提問時不接；純說條件最多接 3 則。
2. 刪除對話：DELETE /api/conversations/{id} 只隱藏（hidden_at），不刪對話與請求紀錄；隱藏後列表、讀取、提問一律 404；
   別人的對話、已刪除的對話回 404；沒有 CSRF 權杖回 403。
句子都是 SYNTHETIC 開發句；測試庫沒有生成模型，只看路由與送進檢索的查詢。
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from .conftest import login
from .test_rec_v2_undertone import _say, _setup

COND = "聚會時想要溫柔又有氣色"              # SYNTHETIC（只記條件）
ASK = "那要擦什麼顏色好"                     # SYNTHETIC（追問）
M03 = "我是冷底，想擦紅色，要選哪一種紅比較適合？"


@pytest.fixture
def seen(monkeypatch):
    """攔截送進 qa.answer_question 的（問題, 檢索查詢），照常執行。"""
    from app import qa
    calls: list[tuple[str, str | None]] = []
    real = qa.answer_question

    def spy(conn, question, role, config, **kw):
        calls.append((question, kw.get("retrieval_query")))
        return real(conn, question, role, config, **kw)

    monkeypatch.setattr(qa, "answer_question", spy)
    return calls


@pytest.fixture
def chat(client, db, users):
    """回傳 (送訊息, 讀 detail, 對話 id, 登入標頭)。"""
    _setup(db, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]

    def detail(body):
        with psycopg.connect(db, row_factory=dict_row) as conn:
            return conn.execute("SELECT detail FROM requests WHERE id = %s", (body["request_id"],)).fetchone()["detail"]
    return (lambda text: _say(client, h, cid, text)), detail, cid, h


# ---------------------------------------------------------------------------
# 1. carry-v3
# ---------------------------------------------------------------------------
def test_condition_then_question_retrieves_with_conditions(chat, seen):
    say, detail, _, _ = chat
    first = say(COND)
    assert detail(first)["route"]["reason"] == "preferences_only" and seen == []
    body = say(ASK)
    assert seen == [(ASK, f"{COND}；{ASK}")]            # 模型看本句；檢索帶條件句
    carry = detail(body)["route"]["carry_over"]
    assert carry["method"] == "join_condition_turns" and carry["condition_questions"] == [COND]


def test_no_condition_carry_after_answered_question(chat, seen):
    say, detail, _, _ = chat
    say(M03)
    body = say(ASK)
    assert seen[-1] == (ASK, None) and detail(body)["route"].get("carry_over") is None


def test_condition_carry_stops_at_answered_question_and_caps_at_three(chat, seen):
    say, detail, _, _ = chat
    say(M03)                                             # 這一則有走問答：之前的不再接
    conds = ["我不要霧面", COND, "想要持久一點", "想要有光澤"]
    reasons = [detail(say(c))["route"]["reason"] for c in conds]
    body = say(ASK)
    only = [c for c, r in zip(conds, reasons) if r == "preferences_only"]
    assert only, "測試句至少要有一句被判成只記條件"
    # 由新往舊連續的純說條件，最多 3 則
    tail: list[str] = []
    for c, r in reversed(list(zip(conds, reasons))):
        if r != "preferences_only" or len(tail) >= 3:
            break
        tail.insert(0, c)
    carry = detail(body)["route"].get("carry_over") or {}
    assert carry.get("condition_questions", []) == tail and len(tail) <= 3
    assert M03 not in (carry.get("retrieval_question") or "")


# ---------------------------------------------------------------------------
# 2. 刪除（隱藏）對話
# ---------------------------------------------------------------------------
def test_delete_hides_but_keeps_data(client, db, chat):
    say, _, cid, h = chat
    body = say(COND)
    assert client.delete(f"/api/conversations/{cid}", headers=h).status_code == 204
    assert cid not in [c["id"] for c in client.get("/api/conversations", headers=h).json()["items"]]
    assert client.get(f"/api/conversations/{cid}", headers=h).status_code == 404
    assert client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": ASK}).status_code == 404
    assert client.patch(f"/api/conversations/{cid}", headers=h, json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/conversations/{cid}", headers=h).status_code == 404     # 重複刪除
    with psycopg.connect(db, row_factory=dict_row) as conn:      # 資料還在（規範 1、8）
        row = conn.execute("SELECT hidden_at FROM conversations WHERE id = %s", (cid,)).fetchone()
        assert row["hidden_at"] is not None
        assert conn.execute("SELECT count(*) AS n FROM requests WHERE id = %s", (body["request_id"],)).fetchone()["n"] == 1


def test_cannot_delete_other_users_conversation(client, db, chat):
    _, _, cid, _ = chat
    hb = login(client, "bob")
    assert client.delete(f"/api/conversations/{cid}", headers=hb).status_code == 404
    with psycopg.connect(db, row_factory=dict_row) as conn:
        assert conn.execute("SELECT hidden_at FROM conversations WHERE id = %s", (cid,)).fetchone()["hidden_at"] is None


def test_delete_requires_csrf(client, chat):
    _, _, cid, h = chat
    no_csrf = {k: v for k, v in h.items() if k.lower() != "x-csrf-token"}
    assert client.delete(f"/api/conversations/{cid}", headers=no_csrf).status_code == 403
