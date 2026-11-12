"""C-ap-v8／C-pp-v5 轉述（restatement）測試（2026-10-28）。

轉述只重組使用者原話的條件、不引用來源；交付時第一行標「依你的描述整理（不是來源內容）」。
問題與輸出文字是為測試寫的（SYNTHETIC），不是評估題的保存回答。不呼叫任何模型。
"""
from __future__ import annotations

import json

from app import answer_policy as AP
from app import postprocess as PP

Q = "我想找粉色口紅，可是不要偏紫也不要太亮的那種，要怎麼說比較準？"   # SYNTHETIC


def unit(uid, text="SYNTHETIC 來源短句"):
    return {"unit_id": uid, "text": text, "conditions": [], "content_role": None, "support_scope": None,
            "product_mentions": []}


def deliver(restatement, claims=None, insufficient=False, question=Q):
    units = {"a": unit("a", "SYNTHETIC: muted pinks suit daytime looks.")}
    raw = json.dumps({"insufficient": insufficient, "restatement": restatement,
                      "claims": claims if claims is not None else [{"text": "白天可以選低調的粉色。", "unit_ids": ["a"], "conditions": []}],
                      "limitations": [], "clarifying_question": "想要偏深還是偏淺？"}, ensure_ascii=False)
    out = PP.postprocess(PP.parse_answer(raw), units, PP.Policy(kept_unit_ids=frozenset(units)))
    return PP.reconcile(out, question, [u["text"] for u in units.values()])



def test_restatement_from_user_words_is_delivered_first_and_labelled():
    out = deliver("粉色，不偏紫、不要太亮")
    assert out["restatement"] == "粉色，不偏紫、不要太亮"
    text = PP.render_delivered(out)
    assert text.splitlines()[0] == "依你的描述整理（不是來源內容）：粉色，不偏紫、不要太亮"
    assert "〔來源 a〕" in text                                        # 有證據的主張照樣帶來源


def test_restatement_adding_facts_is_removed():
    out = deliver("粉色選帶藍調的冷粉最顯白")                          # 「藍調、冷、最顯白」不是使用者說的
    assert out["restatement"] is None
    rm = [r for r in out["removed"] if r.get("rule") == "restatement_not_from_user"]
    assert rm and rm[0]["restatement"] == "粉色選帶藍調的冷粉最顯白"
    assert not PP.render_delivered(out).startswith(PP.RESTATEMENT_HEAD)


def test_null_restatement_and_old_outputs_unchanged():
    out = deliver(None)
    assert out["restatement"] is None and not PP.render_delivered(out).startswith(PP.RESTATEMENT_HEAD)
    old = json.dumps({"insufficient": False, "claims": [], "limitations": [], "clarifying_question": None})
    p = PP.parse_answer(old)                                            # 舊格式（沒有 restatement）照常解析
    assert not p.errors and p.restatement is None


def test_restatement_wrong_type_is_parse_error():
    p = PP.parse_answer(json.dumps({"insufficient": False, "restatement": ["x"], "claims": [], "limitations": [],
                                    "clarifying_question": None}))
    assert "restatement_type" in p.errors


def test_new_ratio_helper():
    assert PP.restatement_new_ratio("不偏紫的粉色", Q) == 0
    assert PP.restatement_new_ratio("", Q) == 0
    assert PP.restatement_new_ratio("藍調冷粉", Q) > PP.RESTATEMENT_MAX_NEW


def test_prompt_back_to_v6_without_restatement():
    """第五十八批：提示回到 C-ap-v6（已判讀最高分）；提示與輸出格式沒有 restatement，後處理照樣接受沒有轉述的輸出。"""
    assert AP.ANSWER_POLICY_VERSION in ("C-ap-v6", "C-ap-v10", "C-ap-v11", "C-ap-v12")   # C-ap-v10（2026-11-07）只改主張上限與限制規則
    assert "restatement" not in AP.SYSTEM_PROMPT and "restatement" not in AP.RESPONSE_SCHEMA["properties"]
