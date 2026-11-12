"""reconcile-v5（2026-10-29）：拿掉「使用者提到／要求的 X」但使用者沒說過 X 的限制。

限制句取自 C-ap-v6 T4（accept_t4_v3_20261028）的保存交付（W03 詞表開、N03 詞表關）；不呼叫模型。
"""
from __future__ import annotations

from app import postprocess as PP

W03_Q = "豆沙色跟奶茶色差在哪裡？"
N03_Q = "我想要上班用、不要太亮的唇色"


def _delivered(limitations):
    return {"status": "delivered", "claims": [{"claim_no": 1, "text": "奶茶色是棕色系加入白色或米色色素。", "unit_ids": ["a"],
                                               "role": "answer"}],
            "limitations": limitations, "removed": [], "flags": [], "system_notes": []}


def _rules(out):
    return [r.get("rule") for r in out["removed"]]


def test_w03_copied_example_limitations_removed():
    lims = ["證據未提及使用者提到的特定服裝或唇色。", "證據未提及使用者要求的特定場合。"]
    out = PP.reconcile(_delivered(lims), W03_Q, [])
    assert out["limitations"] == [] and _rules(out) == ["limitation_user_term_not_said"] * 2
    assert out["removed"][0]["terms"] == ["服裝", "唇色"] and out["removed"][1]["terms"] == ["場合"]
    assert out["reconcile_version"] in ("reconcile-v5", "reconcile-v6", "reconcile-v7", "reconcile-v8")


def test_term_in_question_is_kept():
    # N03 問題有「唇色」：限制雖怪但使用者確實要唇色，不拿
    lims = ["證據未提及使用者要求的特定唇色。", "證據未提及使用者要求的特定服裝。"]
    out = PP.reconcile(_delivered(lims), N03_Q, [])
    assert out["limitations"] == ["證據未提及使用者要求的特定唇色。"]
    assert out["removed"][0]["terms"] == ["服裝"]


def test_limitations_without_user_phrase_untouched():
    lims = ["證據中沒有關於「不要太亮」具體定義的建議。", "證據未提及使用者要求的「不會太重」在特定場合下的具體呈現。"]
    out = PP.reconcile(_delivered(lims), N03_Q, [])
    assert out["limitations"] == lims and "limitation_user_term_not_said" not in _rules(out)


def test_helper():
    assert PP.user_terms_not_said("證據沒有使用者提到的服裝搭配建議。", W03_Q) == ["服裝搭配建議"]
    assert PP.user_terms_not_said("證據沒有使用者提到的豆沙色比較。", W03_Q) is None
    assert PP.user_terms_not_said("證據沒有服裝建議。", W03_Q) is None
