"""S10 開發題評估找到的阻斷問題修正（解析器 rule-v2、詞彙 pref-vocab-draft-2）。

1. 「不要太亮」原本被靜默丟掉 → 排斥條件遺失（B01／B04）。改為阻斷性模糊詞，先補問。
2. 「很顯眼但又不要被注意到」沒有偵測到衝突（D02，規範 5）。
3. 「下單」「哪裡有貨」沒被判為範圍外（C02／C03，規範 9）。
SYNTHETIC：端到端部分沿用 S7 合成候選。
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app import preferences as pref
from .card_history import rule_card_for_request
from .conftest import login
from .test_results_api import _new_conv, _say, _setup

PARSER = pref.RuleParser()


def test_vague_brightness_becomes_blocking_ambiguity():
    r = PARSER.parse("我想要上班用、不要太亮的唇色")
    assert [(a["kind"], a["blocking"]) for a in r.ambiguities] == [("brightness", True)]
    # 不自行選邊：沒有變成排除鮮明或排除亮面
    assert not any(c["field"] in ("avoided_effects", "disliked_finishes") for c in r.changes)


def test_brightness_question_quotes_only_the_phrase():
    """rule-v10（P03）：補問句只引用「不要太亮」，不引用整句；整句另存 clause 供追溯。"""
    for text in ("我想要上班用、不要太亮的唇色", "我想要聚會用、不要太亮的唇色"):
        a = PARSER.parse(text).ambiguities[0]
        assert (a["raw"], a["clause"]) == ("不要太亮", text)
        q = pref._ambiguity_question(a)["question"]
        assert q.startswith("「不要太亮」是指") and "上班" not in q and "聚會" not in q


def test_explicit_brightness_terms_are_not_ambiguous():
    for text in ("不要亮面", "我想要亮眼一點", "不要太亮，不要鮮豔"):
        assert PARSER.parse(text).ambiguities == [], text
    assert PARSER.parse("不要亮面").changes[0]["key"] == "gloss"


def test_noticeable_but_unnoticed_is_conflict():
    r = PARSER.parse("想要很顯眼但又不要被注意到")
    assert [(c["field"], c["key"]) for c in r.conflicts] == [("effects", "vivid")]


def test_transaction_and_inventory_are_outside_scope():
    for text in ("幫我下單 B01 的第一個顏色", "這支 XX 色號現在哪裡有貨？", "還有現貨嗎"):
        assert PARSER.parse(text).intent == "outside_scope", text


def test_brightness_asks_first_then_recommends_without_vivid(client, db, users):
    _setup(db, users)
    h = login(client, "alice")
    cid = _new_conv(client, h)
    first = _say(client, h, cid, "我想要上班用、不要太亮的唇色")
    # route-v1（2026-10-26）：先用問答給起點（本測試沒有嵌入模型→answer_failed），不出推薦卡，補問照樣附上
    assert first["status"] != "needs_clarification" and first["recommendation"] is None
    assert any("鮮豔" in q["question"] and "亮面" in q["question"] for q in first["clarification"])
    # 改情境時模糊詞仍在，仍附補問（B04：偏好修改不遺失排斥）
    second = _say(client, h, cid, "改成聚會用")
    assert second["recommendation"] is None
    assert any("鮮豔" in q["question"] and "亮面" in q["question"] for q in second["clarification"])
    # 回答「不要鮮豔」→ 模糊詞解除；route-v4 接回最初的推薦需求重新問答（不出推薦卡）
    third = _say(client, h, cid, "不要鮮豔")
    assert third["recommendation"] is None and not third["clarification"]
    with psycopg.connect(db, row_factory=dict_row) as c:
        d3 = c.execute("SELECT detail FROM requests WHERE id = %s", (third["request_id"],)).fetchone()["detail"]
    assert d3["route"]["reason"] == "update_after_recommend"
    # 規則推薦本身（離線對照用）：以這時的條件排序，不含鮮明效果的候選
    keys = [r["option_key"] for r in rule_card_for_request(db, third["request_id"], had_prior=True).recommendations]
    assert keys and "synthetic-red-gloss" not in keys
