"""followup-v2：含糊回答、題型不符、插問暫存、取消、過期、對話隔離的完整對話測試。

整支測試在檢查什麼（句子都是 SYNTHETIC 開發句或開發題原句；依語意類型，不依題號）：
1. 明確肯定／明確否定／含糊分開：「好像是」「應該不是」保留待確認、不升級成偏好也不丟掉原需求；
   「好，但不要自然妝」以明說的排除為準。
2. 回答要符合題型：「是／沒錯」不能存成平常擦法；「今天好冷」不能只因短就存入。
3. 插問：待回答暫存一題，插問照一般訊息處理（檢索的是插問本身）；之後明確返回就接回原問題；
   插問後只說「對」先簡短確認在回答哪一題，不猜。
4. 明確取消清掉舊任務、不推薦也不檢索；新的本輪問題取代舊任務；排除條件與後續修改保留。
5. 輪數上限（LIPMATCH_PENDING_MAX_TURNS，預設 3）到了就過期；不合法設定用預設。
6. API：檢查實際送進 qa.answer_question 的查詢；不同對話、不同使用者的待回答不互相串用。
"""
from __future__ import annotations

import pytest

from app import preferences as pref

from .conftest import login

P = pref.RuleParser()
NOW = "2026-10-19T00:00:00+00:00"
N04 = "最近心情很低落，想擦一個讓自己看起來有精神一點的顏色。"
P05 = "我想試試看不一樣的紅唇，但不要太誇張，有什麼擦法？"


def _dialog(*messages: str) -> list[pref.TurnResult]:
    """依序處理多則訊息（狀態存回再讀出，等同 API 每輪），回傳每一輪結果。"""
    state, turns = pref.empty_state(), []
    for i, msg in enumerate(messages):
        turn = pref.process_parsed(state, P.parse(msg), request_id=f"r{i}", now=NOW, text=msg)
        state = pref.load_state(pref.dump_state(turn.state))
        turns.append(turn)
    return turns


def _effects(turn: pref.TurnResult) -> set[str]:
    """取出偏好狀態中「想要的效果」鍵。"""
    return {i["value"] for i in turn.state["effects"]}


def _pending(turn: pref.TurnResult) -> str | None:
    """取出這一輪結束後還在等的待回答題型（沒有就是 None）。"""
    return (turn.state["pending_turn"] or {}).get("kind")


def _blocking(turn: pref.TurnResult) -> list[str]:
    """取出這一輪的阻斷性補問問句。"""
    return [q["question"] for q in turn.clarification if q["blocking"]]


# ---------------------------------------------------------------------------
# 1. 明確肯定／否定／含糊
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answer", ["好像是", "應該是吧", "可能吧", "應該不是", "我也不確定"])
def test_vague_answer_keeps_pending_without_upgrading(answer):
    t2 = _dialog(N04, answer)[1]
    assert t2.followup["result"] == "vague"
    assert _effects(t2) == set()                                  # 不升級成確認偏好
    assert _pending(t2) == "vitality"                             # 也不丟掉原需求
    assert len(_blocking(t2)) == 1 and "好氣色" in _blocking(t2)[0]
    assert t2.parsed.intent == "recommend"                        # 接回原需求，但這輪先問、不推薦


def test_vague_then_clear_yes_confirms():
    t3 = _dialog(N04, "好像是", "是")[2]
    assert t3.followup["result"] == "confirmed"
    assert _effects(t3) == {"natural", "healthy_look"}


def test_explicit_exclusion_wins_over_ok():
    t2 = _dialog(N04, "好，但不要自然妝")[1]
    assert t2.followup["result"] == "corrected"
    assert "natural" not in _effects(t2)
    assert {i["value"] for i in t2.state["avoided_effects"]} == {"natural"}
    assert _pending(t2) is None


@pytest.mark.parametrize("answer", ["好看嗎", "好冷"])
def test_hao_inside_other_word_is_not_affirmation(answer):
    t2 = _dialog(N04, answer)[1]
    assert t2.followup["result"] != "confirmed" and _effects(t2) == set()


# ---------------------------------------------------------------------------
# 2. 回答要符合題型
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answer, result", [("是", "type_mismatch"), ("沒錯", "type_mismatch"),
                                             ("不是", "type_mismatch"), ("今天好冷", "unclear")])
def test_non_technique_reply_not_stored_as_baseline(answer, result):
    t2 = _dialog(P05, answer)[1]
    assert t2.followup["result"] == result
    assert t2.state["technique"]["value"] is None
    assert t2.qa_question is None
    assert _pending(t2) == "technique_baseline"
    assert "平常紅唇是怎麼擦的" in _blocking(t2)[0]


def test_mismatch_then_real_answer_recorded():
    t3 = _dialog(P05, "是", "都塗滿")[2]
    assert t3.followup["result"] == "baseline_recorded"
    assert t3.state["technique"]["value"] == "都塗滿"


# ---------------------------------------------------------------------------
# 3. 插問
# ---------------------------------------------------------------------------
def test_interjection_parks_then_explicit_return_continues():
    t2, t3 = _dialog(P05, "什麼是霧面？", "我平常都塗滿")[1:]
    assert t2.followup["result"] == "parked"
    assert t2.parsed.intent == "knowledge" and t2.qa_question is None      # 插問照一般訊息處理
    assert _blocking(t2) == []                                             # 不擋插問
    assert any("先前的問題還在" in q["question"] for q in t2.clarification)
    assert t3.followup["result"] == "baseline_recorded"
    assert t3.qa_question.startswith(P05) and "我平常都塗滿" in t3.qa_question
    assert t3.state["occasion"]["status"] == "unknown"


def test_bare_yes_after_interjection_asks_which_question():
    t3, t4 = _dialog(N04, "霧面跟亮面差在哪？", "對", "是")[2:]
    assert t3.followup["result"] == "target_unclear"
    assert _effects(t3) == set()                                           # 不猜
    assert "是在回答先前的問題" in _blocking(t3)[0]
    assert t4.followup["result"] == "confirmed" and _effects(t4) == {"natural", "healthy_look"}


@pytest.mark.parametrize("answer", ["剛剛的問題，是", "回到上一題：是", "剛才那題，對"])
def test_explicit_return_after_interjection_confirms(answer):
    t3 = _dialog(N04, "霧面跟亮面差在哪？", answer)[2]
    assert t3.followup["result"] == "confirmed" and _effects(t3) == {"natural", "healthy_look"}


def test_supplement_keeps_condition_and_pending():
    t2, t3 = _dialog(N04, "要上班用的", "是")[1:]
    assert t2.followup["result"] == "supplemented"
    assert t2.state["occasion"]["value"] == "office" and _pending(t2) == "vitality"
    assert t3.state["occasion"]["value"] == "office" and _effects(t3) == {"natural", "healthy_look"}


# ---------------------------------------------------------------------------
# 4. 取消、取代、保留排除條件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answer", ["算了", "算了，不用找了", "先不用"])
def test_cancel_clears_task_and_holds_answer(answer):
    t2 = _dialog(N04, answer)[1]
    assert t2.followup["result"] == "cancelled"
    assert t2.hold_answer is True and _pending(t2) is None
    assert t2.clarification == []


def test_cancel_with_new_question_processes_new_question():
    t2 = _dialog(P05, "不用了，什麼是霧面？")[1]
    assert t2.followup["result"] == "cancelled" and t2.hold_answer is False
    assert t2.parsed.intent == "knowledge" and _pending(t2) is None


def test_new_turn_question_replaces_old_task():
    t2, t3 = _dialog(N04, P05, "都塗滿")[1:]
    assert _pending(t2) == "technique_baseline"
    assert not any("先前的問題還在" in q["question"] for q in t2.clarification)
    assert t3.followup["result"] == "baseline_recorded"


def test_exclusions_and_later_changes_survive_followup():
    turns = _dialog("我不要霧面，想擦一個讓自己看起來有精神一點的顏色", "什麼是緞面？", "剛剛的問題，是", "改成聚會用")
    assert turns[2].followup["result"] == "confirmed"             # 明確返回：不用再確認在回答哪一題
    last = turns[-1]
    assert {i["value"] for i in last.state["disliked_finishes"]} == {"matte"}
    assert _effects(last) == {"natural", "healthy_look"}
    assert last.state["occasion"]["value"] == "party"


# ---------------------------------------------------------------------------
# 5. 輪數上限
# ---------------------------------------------------------------------------
def test_pending_expires_after_max_turns():
    turns = _dialog(P05, "什麼是霧面？", "什麼是緞面？", "什麼是唇釉？", "我平常都塗滿")
    assert [t.followup["result"] for t in turns[1:4]] == ["parked"] * 3
    assert turns[4].followup["result"] == "expired" and turns[4].followup["max_turns"] == 3
    assert turns[4].state["technique"]["value"] is None and _pending(turns[4]) is None


def test_max_turns_configurable(monkeypatch):
    monkeypatch.setenv("LIPMATCH_PENDING_MAX_TURNS", "1")
    turns = _dialog(P05, "什麼是霧面？", "我平常都塗滿")
    assert turns[2].followup["result"] == "expired"


@pytest.mark.parametrize("raw", ["0", "99", "abc", ""])
def test_invalid_max_turns_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("LIPMATCH_PENDING_MAX_TURNS", raw)
    assert pref._pending_max_turns() == 3


# ---------------------------------------------------------------------------
# 6. API：實際檢索查詢、對話隔離
# ---------------------------------------------------------------------------
@pytest.fixture
def seen_queries(monkeypatch):
    """攔截送進 qa.answer_question 的查詢，回傳記錄清單（仍呼叫原本的知識問答）。"""
    from app import qa
    seen: list[str] = []
    real = qa.answer_question

    def spy(conn, question, role, config, **kw):
        """記下查詢後照常執行。"""
        seen.append(question)
        return real(conn, question, role, config, **kw)

    monkeypatch.setattr(qa, "answer_question", spy)
    return seen


def _new_conv(client, h) -> str:
    """建立新對話，回傳對話 ID。"""
    return client.post("/api/conversations", json={}, headers=h).json()["id"]


def _say(client, h, cid: str, text: str) -> dict:
    """在指定對話送出一則訊息，回傳回應本文。"""
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_api_queries_for_direct_vague_interjection_return(client, users, seen_queries):
    h = login(client, "alice")
    cid = _new_conv(client, h)
    _say(client, h, cid, P05)                   # route-v1：先檢索原問題給起點，同時問平常擦法
    _say(client, h, cid, "是")                   # 題型不符：再問，不檢索（起點第一輪已給）
    _say(client, h, cid, "什麼是霧面？")          # 插問：檢索插問本身
    _say(client, h, cid, "我平常都塗滿")          # 明確返回：檢索原問題＋平常擦法
    assert seen_queries[0] == P05
    assert seen_queries[1] == "什麼是霧面？"
    assert seen_queries[2].startswith(P05) and "我平常都塗滿" in seen_queries[2]
    assert len(seen_queries) == 3
    assert not any(q in ("是", "我平常都塗滿") for q in seen_queries)


def test_api_cancel_does_not_retrieve_or_recommend(client, users, seen_queries):
    h = login(client, "alice")
    cid = _new_conv(client, h)
    _say(client, h, cid, P05)
    body = _say(client, h, cid, "算了")
    assert seen_queries == [P05] and body.get("recommendation") in (None, {})   # 只有第一輪的起點檢索
    assert body["clarification"] == []


def test_api_pending_not_shared_between_conversations_or_users(client, users, seen_queries):
    alice, bob = login(client, "alice"), login(client, "bob")
    c1, c2, c3 = _new_conv(client, alice), _new_conv(client, alice), _new_conv(client, bob)
    _say(client, alice, c1, P05)                # 只有 c1 在等平常擦法（route-v1：這一輪會檢索原問題給起點）
    n_first = len(seen_queries)
    b2 = _say(client, alice, c2, "都塗滿")       # 同使用者另一個對話：不能接到 c1
    b3 = _say(client, bob, c3, "都塗滿")         # 另一個使用者：也不能
    assert all(not any(r["field"] == "technique" for r in b["summary"]["rows"]) for b in (b2, b3))
    assert all(not q.startswith(P05) for q in seen_queries[n_first:])
    b1 = _say(client, alice, c1, "都塗滿")       # c1 自己還接得上
    assert any(r["field"] == "technique" for r in b1["summary"]["rows"])
    assert seen_queries[-1].startswith(P05)
