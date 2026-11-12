"""研究對齊 rev1 第二輪對話接續（followup-v1）：回答上一輪本輪確認問題時，需求要接得上、不能遺失。

整支測試在檢查什麼（依語意類型，不依題號；句子都是 SYNTHETIC 開發句或開發題原句）：
1. 模糊外觀目標（N04 類型）：系統問「是想要自然、不張揚的好氣色嗎？」
   - 肯定（「是」「對，就是這個意思」）→ 暫定理解轉成確認需求（自然低調＋有氣色），意圖接回推薦，不再問；
   - 否定／修正（「不是，我想要亮眼一點」）→ 只記回答的方向，不保留被否認的暫定理解；
   - 答非所問 → 放棄待回答，照一般訊息處理，暫定理解不變成偏好。
2. 不知道平常擦法（P05 類型）：系統問「你平常紅唇是怎麼擦的？」
   - 回答（「都塗滿」等）→ 記下平常擦法＋想換的方向＋原本的「不要太誇張」，意圖接回知識問答、不重問；
     檢索用「原問題＋平常擦法」；不預設使用者要漸層；回答裡的「平常」不被當成日常場合。
3. 上下文保留：第三輪改別的條件，已確認的需求仍在；待回答只等一輪，不會卡住之後的訊息。
4. API：第二輪結果與日誌（detail.followup、qa_question）一致；證據不足時已確認的需求仍保留，不捏造回答。
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
    """依序處理多則訊息（狀態存回再讀出，等同 API 每輪的 load_state／dump_state），回傳每一輪結果。"""
    state, turns = pref.empty_state(), []
    for i, msg in enumerate(messages):
        turn = pref.process_parsed(state, P.parse(msg), request_id=f"r{i}", now=NOW, text=msg)
        state = pref.load_state(pref.dump_state(turn.state))
        turns.append(turn)
    return turns


def _effects(turn: pref.TurnResult) -> set[str]:
    """取出偏好狀態中「想要的效果」鍵。"""
    return {i["value"] for i in turn.state["effects"]}


# ---------------------------------------------------------------------------
# 1. 模糊外觀目標的回答
# ---------------------------------------------------------------------------
def test_first_turn_records_pending_but_not_preference():
    t1 = _dialog(N04)[0]
    assert t1.state["pending_turn"]["kind"] == "vitality"
    assert _effects(t1) == set()                                  # 暫定理解不是偏好
    assert all(r["field"] != "technique" for r in pref.summarize(t1.state)["rows"])


@pytest.mark.parametrize("answer", ["是", "對，就是這個意思", "嗯", "沒錯"])
def test_vitality_affirmed_becomes_confirmed_need(answer):
    t2 = _dialog(N04, answer)[1]
    assert t2.followup["result"] == "confirmed"
    assert _effects(t2) == {"natural", "healthy_look"}
    assert t2.parsed.intent == "recommend"                        # 接回原本找唇色的需求
    assert t2.clarification == []                                 # 不再問同一題
    assert t2.state["pending_turn"] is None
    raws = [i["raw"] for i in t2.state["effects"]]
    assert all("確認：" in r for r in raws)                        # 看得出是確認，不是使用者自己說的


@pytest.mark.parametrize("answer, expected", [
    ("不是，我想要亮眼一點", {"vivid"}),
    ("不對，想要鮮明一點的", {"vivid"}),
    ("不是", set()),
])
def test_vitality_denied_drops_tentative(answer, expected):
    t2 = _dialog(N04, answer)[1]
    assert t2.followup["result"] == "denied"
    assert _effects(t2) == expected
    assert not ({"natural", "healthy_look"} & _effects(t2))      # 被否認的暫定理解不能保留
    assert t2.parsed.intent in ("recommend", "update")
    assert t2.state["pending_turn"] is None


def test_vitality_corrected_without_no_uses_stated_direction():
    t2 = _dialog(N04, "想要溫柔一點的感覺")[1]
    assert t2.followup["result"] == "corrected"
    assert _effects(t2) == {"gentle"}


def test_vitality_unrelated_reply_abandons_pending():
    # followup-v2（2026-10-19）：插問改成暫存待回答，不再直接放棄；插問本身照一般訊息處理
    t2 = _dialog(N04, "霧面跟亮面差在哪？")[1]
    assert t2.followup["result"] == "parked"
    assert _effects(t2) == set()
    assert t2.parsed.intent == "compare"                          # 照一般訊息處理
    assert t2.state["pending_turn"]["kind"] == "vitality"


def test_plain_yes_without_pending_is_not_treated_as_confirmation():
    t1 = _dialog("是")[0]
    assert t1.followup is None and _effects(t1) == set()


# ---------------------------------------------------------------------------
# 2. 平常擦法的回答
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answer", ["都塗滿", "整個塗滿", "就薄薄擦一層", "平常就直接塗滿整個嘴唇",
                                  "我平常是整個嘴唇均勻塗滿，想換一種不太誇張的擦法。"])
def test_technique_answer_recorded_and_continues(answer):
    t2 = _dialog(P05, answer)[1]
    tech = t2.state["technique"]
    assert t2.followup["result"] == "baseline_recorded"
    assert tech["status"] == "user" and tech["value"]
    assert tech["keep"] == "不要太誇張"                            # 原本的限制沒遺失
    assert tech["goal"] == "想換和平常不一樣的擦法"
    assert t2.parsed.intent == "knowledge"                        # 接回原本的擦法問題
    assert t2.clarification == []                                 # 不重問平常怎麼擦
    assert t2.qa_question.startswith(P05) and answer.strip().rstrip("。") in t2.qa_question
    assert "漸層" not in t2.qa_question                            # 不預設使用者要漸層
    assert t2.state["occasion"]["status"] == "unknown"            # 「平常」是習慣，不是日常場合
    assert not t2.state["conflicts"]
    assert "vivid" not in _effects(t2)


def test_technique_summary_row_keeps_goal_and_limit():
    t2 = _dialog(P05, "都塗滿")[1]
    row = next(r for r in pref.summarize(t2.state)["rows"] if r["field"] == "technique")
    assert "都塗滿" in row["value"] and "不要太誇張" in row["value"] and row["status"] == "user"


def test_technique_unrelated_question_abandons_pending():
    t2 = _dialog(P05, "什麼是霧面？")[1]
    assert t2.followup["result"] == "parked"                      # followup-v2：插問暫存，不放棄
    assert t2.state["technique"]["value"] is None
    assert t2.qa_question is None


def test_habit_answer_with_real_occasion_keeps_occasion():
    t2 = _dialog(P05, "上班都塗滿")[1]
    assert t2.followup["result"] == "baseline_recorded"
    assert t2.state["occasion"]["value"] == "office"              # 明說的場合照記


# ---------------------------------------------------------------------------
# 3. 上下文保留
# ---------------------------------------------------------------------------
def test_confirmed_need_survives_later_change():
    t3 = _dialog(N04, "是", "改成聚會用")[2]
    assert _effects(t3) == {"natural", "healthy_look"}
    assert t3.state["occasion"]["value"] == "party"
    assert t3.followup is None                                    # 待回答只等一輪


def test_technique_baseline_survives_later_turn():
    t3 = _dialog(P05, "都塗滿", "那上班的時候呢？")[2]
    assert t3.state["technique"]["value"] == "都塗滿"
    assert t3.state["technique"]["keep"] == "不要太誇張"


def test_old_runner_without_text_keeps_rule_v6_behavior():
    state = pref.empty_state()
    t1 = pref.process_parsed(state, P.parse(N04), request_id="r0", now=NOW)
    t2 = pref.process_parsed(pref.load_state(pref.dump_state(t1.state)), P.parse("是"), request_id="r1", now=NOW)
    assert t2.followup is None and _effects(t2) == set()


# ---------------------------------------------------------------------------
# 4. API：第二輪接續、日誌、證據不足時需求不遺失
# ---------------------------------------------------------------------------
def _conversation(client, h, *messages: str) -> list[dict]:
    """在同一個新對話依序送出多則訊息，回傳每則回應本文。"""
    cid = client.post("/api/conversations", json={}, headers=h).json()["id"]
    out = []
    for text in messages:
        r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": text})
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


def _last_detail(db, user_id) -> dict:
    """讀出該使用者最後一筆請求的 detail（日誌）。"""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db, row_factory=dict_row) as conn:
        return conn.execute("SELECT detail FROM requests WHERE user_id = %s ORDER BY started_at DESC LIMIT 1",
                            (user_id,)).fetchone()["detail"]


def _summary_value(body: dict, field: str) -> str:
    """從回應的需求摘要取出某欄位的顯示值。"""
    return next(r["value"] for r in body["summary"]["rows"] if r["field"] == field)


def test_api_vitality_yes_confirms_and_logs(client, users, db):
    first, second = _conversation(client, login(client, "alice"), N04, "是")
    # route-v1（2026-10-26）：第一輪先用問答給起點（測試庫沒有核准片段→證據不足），「好氣色嗎」補問照樣附上
    assert first["status"] == "answer_insufficient_evidence"
    assert any("好氣色嗎" in q["question"] for q in first["clarification"])
    assert second["intent"] == "recommend"
    assert {"natural", "healthy_look"} <= {a["key"] for a in second["applied_changes"]}
    assert "自然低調" in _summary_value(second, "effects") and "有氣色" in _summary_value(second, "effects")
    assert not any("好氣色嗎" in q["question"] for q in second["clarification"])
    detail = _last_detail(db, users["alice"])
    assert detail["followup"]["result"] == "confirmed" and detail["followup"]["kind"] == "vitality"


def test_api_vitality_no_updates_without_tentative(client, users):
    _, second = _conversation(client, login(client, "alice"), N04, "不是，我想要亮眼一點")
    keys = {a["key"] for a in second["applied_changes"]}
    assert "vivid" in keys and not ({"natural", "healthy_look"} & keys)
    assert "自然低調" not in _summary_value(second, "effects")


def test_api_technique_answer_keeps_need_when_evidence_missing(client, users, db):
    first, second = _conversation(client, login(client, "alice"), P05, "都塗滿")
    assert first["status"] == "answer_insufficient_evidence"      # route-v1：先給起點、補問照樣附上
    assert any("是怎麼擦的" in q["question"] for q in first["clarification"])
    assert second["intent"] == "knowledge"
    assert not any("是怎麼擦的" in q["question"] for q in second["clarification"])   # 不重問
    assert "都塗滿" in _summary_value(second, "technique")                         # 證據不足也不遺失
    assert "不要太誇張" in _summary_value(second, "technique")
    detail = _last_detail(db, users["alice"])
    assert detail["followup"]["result"] == "baseline_recorded"
    assert detail["qa_question"].startswith(P05) and "都塗滿" in detail["qa_question"]
    # 測試庫沒有核准片段：不能產生有來源的回答，也不能捏造引用
    assert not second.get("citations")
    if second.get("answer"):
        assert "漸層" not in second["answer"]


def test_api_retrieval_uses_original_question_plus_baseline(client, users, monkeypatch):
    """實際送進知識問答檢索的問題，要是「原問題＋平常擦法」，不是只有「都塗滿」幾個字。"""
    from app import qa
    seen: list[str] = []
    real = qa.answer_question

    def spy(conn, question, role, config, **kw):
        """記下送進檢索的問題，再照常呼叫原本的知識問答。"""
        seen.append(question)
        return real(conn, question, role, config, **kw)

    monkeypatch.setattr(qa, "answer_question", spy)
    _conversation(client, login(client, "alice"), P05, "都塗滿")
    assert len(seen) == 2                       # route-v1：第一輪先檢索原問題給起點；第二輪檢索原問題＋平常擦法
    assert seen[0] == P05
    assert seen[1].startswith(P05) and "都塗滿" in seen[1] and "漸層" not in seen[1]
