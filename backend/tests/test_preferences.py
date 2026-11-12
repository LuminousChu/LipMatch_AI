"""S6 偏好模組單元測試（不連資料庫）：成對案例、表單規則、舊格式升級、摘要。

所有語句為 SYNTHETIC 開發案例，不是正式驗收題。
"""
from __future__ import annotations

import pytest

from app import pref_cases
from app import preferences as pref

NOW = "2026-10-15T12:00:00+00:00"


# 每個成對案例各自一個測試，失敗時直接看到是哪一案、哪個欄位不符。
@pytest.mark.parametrize("case", pref_cases.load_cases()["cases"], ids=lambda c: c["id"])
def test_pair_case(case):
    ok, failures, _ = pref_cases.check_case(case)
    assert ok, f"{case['id']} {case['title']}：" + "；".join(failures)


def _turn(state, text):
    return pref.process_parsed(state, pref.RuleParser().parse(text), request_id="r", now=NOW)


def test_raw_text_and_metadata_are_kept():
    turn = _turn(pref.empty_state(), "我是小麥色，上班用的唇色，不要霧面")
    s = turn.state
    assert s["skin_tone"] == {"value": "小麥色", "status": "user", "raw": "我是小麥色", "source": "chat",
                              "request_id": "r", "updated_at": NOW}
    item = s["disliked_finishes"][0]
    assert (item["value"], item["raw"], item["request_id"]) == ("matte", "不要霧面", "r")
    assert s["undertone"]["status"] == "unknown"  # 不推定底調


def test_unmentioned_fields_are_untouched():
    s1 = _turn(pref.empty_state(), "想要玫瑰色，不要橘色，我對羊毛脂過敏").state
    s2 = _turn(s1, "改成聚會").state
    for f in ("liked_colors", "disliked_colors", "allergy"):
        assert s2[f] == s1[f]


def test_cancel_does_not_touch_other_dislikes():
    s = _turn(pref.empty_state(), "不要霧面跟橘色").state
    assert [i["value"] for i in s["disliked_finishes"]] == ["matte"]
    assert [i["value"] for i in s["disliked_colors"]] == ["orange"]
    s = _turn(s, "取消不要霧面").state
    assert s["disliked_finishes"] == [] and [i["value"] for i in s["disliked_colors"]] == ["orange"]


def test_lipstick_word_is_not_red():
    # 「口紅」裡的「紅」不能被當成紅色系
    s = _turn(pref.empty_state(), "不要口紅，想要唇釉").state
    assert s["disliked_colors"] == []


def test_conflict_not_applied_then_resolved_by_replace():
    s = _turn(pref.empty_state(), "不要霧面").state
    t = _turn(s, "我要霧面")
    assert [i["value"] for i in t.state["disliked_finishes"]] == ["matte"] and t.state["liked_finishes"] == []
    assert t.clarification and t.clarification[0]["blocking"]
    t = _turn(t.state, "改成要霧面")
    assert [i["value"] for i in t.state["liked_finishes"]] == ["matte"]
    assert t.state["disliked_finishes"] == [] and t.state["conflicts"] == []


def test_form_uses_same_rules():
    s = pref.apply_form(pref.empty_state(), {"liked": ["玫瑰色"], "disliked": ["橘色", "霧面"]}, now=NOW).state
    assert [i["value"] for i in s["disliked_finishes"]] == ["matte"]
    assert [i["value"] for i in s["disliked_colors"]] == ["orange"]
    s = pref.apply_form(s, {"scenario": "party"}, now=NOW).state      # 改情境保留排斥
    assert s["occasion"]["value"] == "party" and [i["value"] for i in s["disliked_finishes"]] == ["matte"]
    s = pref.apply_form(s, {"disliked": ["橘色"]}, now=NOW).state     # 表單把霧面拿掉 → 解除
    assert s["disliked_finishes"] == [] and s["liked_finishes"] == []
    with pytest.raises(pref.FormConflict):
        pref.apply_form(s, {"liked": ["霧面"], "disliked": ["霧面"]}, now=NOW)
    with pytest.raises(pref.FormInvalid):
        pref.apply_form(s, {"allergy_status": "none_known", "allergy_avoid": ["羊毛脂"]}, now=NOW)


def test_form_edit_resolves_pending_conflict():
    s = _turn(pref.empty_state(), "要霧面，不要霧面").state
    assert len(s["conflicts"]) == 1
    s = pref.apply_form(s, {"disliked": ["霧面"]}, now=NOW).state
    assert s["conflicts"] == [] and [i["value"] for i in s["disliked_finishes"]] == ["matte"]


def test_legacy_ui_v1_preferences_are_upgraded():
    conv = {"preferences": {"liked": ["玫瑰色"], "disliked": ["橘色"], "finish": "matte", "notes": "上班用"},
            "scenario": "office", "scenario_source": "user", "allergy_status": "has_known", "allergy_avoid": ["羊毛脂"]}
    s = pref.load_state(conv)
    assert [i["value"] for i in s["liked_colors"]] == ["rose"]
    assert [i["value"] for i in s["disliked_colors"]] == ["orange"]
    assert [i["value"] for i in s["liked_finishes"]] == ["matte"]
    assert s["notes"]["value"] == "上班用" and s["occasion"] == {**s["occasion"], "value": "office", "status": "user"}
    assert (s["allergy"]["status"], [i["value"] for i in s["allergy"]["avoid"]]) == ("has_known", ["羊毛脂"])


def test_dump_and_load_roundtrip():
    s = _turn(pref.empty_state(), "改成聚會，我對香料過敏，不要橘色").state
    cols = pref.dump_state(s)
    assert (cols["scenario"], cols["scenario_source"], cols["allergy_status"], cols["allergy_avoid"]) == \
        ("party", "user", "has_known", ["香料"])
    assert pref.load_state(cols) == s


# 2026-10-18 K1 修訂：舊版斷言場合是「預設（日常）」並附說明；現在場合未提供就是未知、不填日常。
def test_summary_marks_unknown_and_default():
    rows = {r["field"]: r for r in pref.summarize(pref.empty_state())["rows"]}
    assert rows["occasion"]["status"] == "unknown" and rows["occasion"]["value"] == "未指定"
    assert rows["allergy"]["value"] == "尚未回答" and "不等於" in rows["allergy"]["note"]
    assert rows["undertone"]["value"] == "未知" and "不從膚色推定" in rows["undertone"]["note"]
    rows = {r["field"]: r for r in pref.summarize(_turn(pref.empty_state(), "不要霧面").state)["rows"]}
    assert rows["disliked_finishes"]["hard"] is True and rows["disliked_finishes"]["raw"] == ["不要霧面"]


def test_parse_result_is_serialisable():
    import json
    out = pref.RuleParser().parse("上班用的唇色，不要霧面").to_dict()
    assert json.loads(json.dumps(out, ensure_ascii=False))["parser_version"] == pref.PARSER_VERSION


def test_form_resave_keeps_original_words():
    # 表單不改動直接儲存：值相同時，使用者原話與來源不能被顯示名稱覆蓋
    s = _turn(pref.empty_state(), "想要裸色，不要橘色，我是小麥色，唇色偏深，我對羊毛脂過敏").state
    assert s["natural_lip"]["value"] == "偏深"
    form = {"liked": [i["label"] for i in s["liked_colors"]],
            "disliked": [i["label"] for i in s["disliked_colors"]],
            "skin_tone": s["skin_tone"]["value"], "natural_lip": s["natural_lip"]["value"],
            "allergy_status": "has_known", "allergy_avoid": ["羊毛脂"]}
    out = pref.apply_form(s, form, now=NOW)
    assert out.applied == [] and out.state == s


def test_form_resave_does_not_drop_pending_conflict():
    s = _turn(pref.empty_state(), "不要橘色，要霧面，不要霧面").state
    out = pref.apply_form(s, {"liked": [], "disliked": ["橘色系"]}, now=NOW)
    assert len(out.state["conflicts"]) == 1   # 只是重新儲存，衝突仍待使用者決定


# rule-v7（2026-10-25）：程度副詞「比較深」、求做法與遮蓋問題算知識、描述原生唇色的色詞不算喜好
@pytest.mark.parametrize("text", ["我的唇色比較深、偏紅，擦淡色唇膏都蓋不住", "嘴唇天生很深還帶紫，淺色口紅被吃掉怎麼辦？",
                                  "唇色偏深，有什麼辦法讓淺色口紅顯色？"])
def test_rule_v7_coverage_problem_is_knowledge_and_lip_color_not_liked(text):
    r = pref.RuleParser().parse(text)
    assert r.intent == "knowledge" and "compare" not in r.secondary_intents
    assert not any(c["field"] == "liked_colors" for c in r.changes)
    assert any(c["field"] == "natural_lip" for c in r.changes)


@pytest.mark.parametrize("text,intent", [("比較深色和淺色哪個適合我", "compare"), ("霧面和亮面比較一下", "compare"),
                                         ("上班用的低調唇色", "recommend")])
def test_rule_v7_keeps_real_compare_and_recommend(text, intent):
    assert pref.RuleParser().parse(text).intent == intent


def test_rule_v7_liked_color_outside_lip_description_still_counts():
    r = pref.RuleParser().parse("我的唇色偏紫，想找粉紅色的口紅")
    assert any(c["field"] == "liked_colors" and c["key"] == "pink" for c in r.changes)
    assert not any(c["field"] == "liked_colors" and c["key"] == "purple" for c in r.changes)

