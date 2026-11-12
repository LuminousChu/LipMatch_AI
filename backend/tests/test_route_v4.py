"""route-v4（2026-11-01，方案第二版）：正式路由預設走問答，不跑規則推薦、不出推薦卡。

整支測試在檢查什麼（句子是 40 題原句、加「我是冷底，」的前綴句，或標 SYNTHETIC 的開發句）：
1. 推薦需求走問答，規則推薦一次都沒有被呼叫；detail 不寫 recommendation，route 記 skipped_by_route。
2. 第 1 層攔截：問的對象不是唇彩（O01 裙子顏色）→ 範圍外出口，不進問答；穿搭＋唇彩仍是唇彩問題。
3. Q5：原本推薦卡用的補問（有精神、希望效果）附在問答回答之後，改成非阻斷；知識為主的題不附「希望效果」。
4. 只改條件：沒有推薦需求在前 → 只記條件；推薦需求之後（中間隔著幾則改條件也一樣）→ 用新條件重新問答。
"""
from __future__ import annotations

import pytest

from app import preferences as pref
from app import recommend as rec

from .conftest import login
from .test_qa import QUESTION, FakeGenerator, _doc, make_client, ok_output  # noqa: F401（共用 fixture）
from .test_rec_v2_undertone import _say

P = pref.RuleParser()
COOL = "我是冷底，"
M03 = "我是冷底，想擦紅色，要選哪一種紅比較適合？"
N04 = "最近心情很低落，想擦一個讓自己看起來有精神一點的顏色。"
O01 = "我是冷膚色，該穿什麼顏色的裙子？"


@pytest.fixture
def answered(make_client, conn, users, monkeypatch):
    """一份公開合成文件＋MOCK 索引＋固定成功的生成替身：問答一定回 answered（引用 E1）。規則推薦被呼叫就記下來。"""
    from app import retrieval
    from .test_qa import MockEmbedder
    _doc(conn, "PUBLIC", users["root"], QUESTION)
    retrieval.build_index(conn, "idx0", MockEmbedder())
    retrieval.activate_index(conn, "idx0")
    rule_calls: list[str] = []
    monkeypatch.setattr(rec, "run", lambda *a, **k: rule_calls.append("run"))
    client = make_client(FakeGenerator(*[ok_output("E1")] * 6)).__enter__()
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    yield (lambda text: _say(client, h, cid, text)), rule_calls
    client.__exit__(None, None, None)


@pytest.fixture
def conn(db, users):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. 推薦需求走問答，不跑規則推薦
# ---------------------------------------------------------------------------
def test_recommend_need_answered_by_qa_without_rule_card(answered):
    say, rule_calls = answered
    body = say(M03)
    assert body["status"] == "answered" and body.get("recommendation") in (None, {}) and rule_calls == []


# ---------------------------------------------------------------------------
# 2. 問的對象不是唇彩 → 範圍外
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [O01, COOL + O01, "頭髮要染什麼顏色？", "指甲擦什麼顏色比較顯白"])   # 後兩句 SYNTHETIC
def test_non_lip_object_is_outside_scope(text):
    assert P.parse(text).intent == "outside_scope"


@pytest.mark.parametrize("text", ["今天穿黑色洋裝，搭配什麼唇色好？", "我穿綠色裙子，口紅要什麼顏色"])   # SYNTHETIC
def test_outfit_with_lip_item_is_still_lip_question(text):
    assert P.parse(text).intent != "outside_scope"


def test_non_lip_object_exit_no_lip_recommendation(answered, conn):
    """exit-assist-v4（2026-11-08，O01／H10 違反「不可以出現」）：範圍外出口、不跑規則推薦，
    也不再改問唇彩代答——只回範圍說明，沒有唇色建議。"""
    say, rule_calls = answered
    body = say(O01)
    assert body["status"] == "outside_scope" and not body.get("answer") and rule_calls == []
    assert body["message"] == "這個問題比較偏向裙子的搭配，我能協助的是唇彩相關的問題。"
    d = conn.execute("SELECT detail FROM requests ORDER BY started_at DESC LIMIT 1").fetchone()["detail"]
    assert (d["route"].get("exit_assist") or {}).get("question") is None


# ---------------------------------------------------------------------------
# 3. Q5：補問附在回答後、非阻斷
# ---------------------------------------------------------------------------
def test_card_questions_become_non_blocking_after_answer(answered):
    say, _ = answered
    body = say(N04)                                  # 有精神：原本是阻斷補問
    assert body["status"] == "answered" and body["answer"]
    assert body["clarification"] and not any(q["blocking"] for q in body["clarification"])


def test_effect_question_kept_for_recommend_need_not_for_knowledge(answered):
    say, _ = answered
    rec_body = say("我是冷底，推薦唇色")                                                   # SYNTHETIC
    assert any(q["field"] == "effects" for q in rec_body["clarification"])
    kn_body = say("我想找「紅色」口紅，可是不要那種偏粉或偏橘的紅，這樣要怎麼說比較準？")    # W01：知識為主
    assert not any(q["field"] == "effects" for q in kn_body["clarification"])


# ---------------------------------------------------------------------------
# 4. 只改條件
# ---------------------------------------------------------------------------
def test_update_without_recommend_need_only_records(answered):
    say, _ = answered
    body = say("我不要霧面")
    assert body["status"] == "preferences_updated" and body.get("answer") is None


def test_update_chain_after_recommend_need_reanswers(answered):
    say, rule_calls = answered
    say("我想要上班用、不要太亮的唇色")             # 推薦需求
    say("改成聚會用")                                # 改條件 → 重新問答
    third = say("不要鮮豔")                          # 回答補問，仍接回最初的推薦需求
    assert third["status"] == "answered" and rule_calls == []
