"""rule-v11（2026-10-27）：否定說法的對象不在詞表時，留下原話交給問答。

起因：N02「想找不會太搶戲的唇色」——「不會」不在否定詞清單、「搶戲」不在詞表，限制整句消失，
模型回了「選一個鮮豔的唇膏當焦點」。處理原則：認得的照常處理，不認得的也不要丟；不用提醒使用者。
"""
from __future__ import annotations

from app import answer_policy as AP, main as M, preferences as pref

PARSER = pref.RuleParser()


def _raws(text: str) -> list[str]:
    """解析一句話，回傳未分類否定說法的原話清單。"""
    return [u["raw"] for u in PARSER.parse(text).unclassified_avoid]


def test_unknown_object_is_kept_as_raw():
    """詞表外的對象留下原話（含「不會太」）；對象停在「的」之前，品類詞不算對象。"""
    assert _raws("我平常穿得很簡約、偏中性風，想找不會太搶戲的唇色。") == ["不會太搶戲"]
    assert _raws("眼妝想畫比較濃的煙燻，唇色要怎麼配才不會太重？") == ["不會太重"]
    assert _raws("我嘴唇容易乾，但又想擦霧面，有什麼辦法讓它不要看起來乾乾的？") == ["不要看起來乾乾"]


def test_known_or_specially_handled_phrases_are_not_duplicated():
    """詞表裡有的詞、已有專門規則的說法、不是否定的「要不要」「特別」都不記。"""
    for text in ("不要霧面", "不要太紅", "我想要上班用、不要太亮的唇色", "不要太誇張的紅唇",
                 "不要那種偏粉或偏橘的紅", "要不要擦紅色", "我特別喜歡豆沙色", "我不會畫唇線", "不要了"):
        assert _raws(text) == [], text


def test_does_not_change_intent_or_ask_questions():
    """多一個未分類說法不改變意圖，也不產生補問或模糊詞（不提醒使用者）。"""
    r = PARSER.parse("我平常穿得很簡約、偏中性風，想找不會太搶戲的唇色。")
    assert r.ambiguities == [] and r.turn_questions == []
    plain = PARSER.parse("我平常穿得很簡約、偏中性風，想找唇色。")
    assert (r.intent, r.secondary_intents) == (plain.intent, plain.secondary_intents)


def test_state_accumulates_and_conditions_reach_model_but_not_evidence_phrases():
    """存進對話狀態（跨輪保留、不重複），交給模型的條件有原話；選段片語不放排除說法。"""
    state = pref.empty_state()
    for i, text in enumerate(["想找不會太搶戲的唇色", "改成聚會用，一樣不會太搶戲"]):
        state = pref.apply_parse(state, PARSER.parse(text), request_id=f"r{i}", now="t").state
    assert [u["raw"] for u in state["unclassified_avoid"]] == ["不會太搶戲"]
    conds = M._explicit_conditions(state)
    assert {"field": "unclassified_avoid", "raw": "不會太搶戲"}.items() <= next(
        c for c in conds if c["field"] == "unclassified_avoid").items()
    view = AP._conditions_view(conds)
    assert any(x["使用者原話"] == "不會太搶戲" and "使用者說不要" in x["條件"] for x in view["使用者明說"])
    assert "不會太搶戲" not in AP.explicit_condition_phrases(conds)
    assert "unclassified_avoid" not in str(pref.summarize(state))   # 需求摘要（前端）看不到


def test_old_state_without_field_still_works():
    """舊對話狀態沒有 unclassified_avoid 欄位，套用時自動補上。"""
    state = pref.empty_state()
    del state["unclassified_avoid"]
    out = pref.apply_parse(state, PARSER.parse("想找不會太搶戲的唇色"), request_id="r", now="t").state
    assert out["unclassified_avoid"][0]["raw"] == "不會太搶戲"
    assert M._explicit_conditions(state) is not None
