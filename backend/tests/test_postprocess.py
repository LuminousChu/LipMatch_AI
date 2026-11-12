"""修復 C 後處理候選（C-pp-v1）測試。

真失敗回歸的主張文字取自保存的模型輸出（R4 H03:B、D13 M01／N03／P01／P05／M03，見 scripts/repair_c/replay_saved.py）；
來源單位文字是為測試寫的短句，不是來源原文（標 SYNTHETIC）。不呼叫任何模型。
"""
from __future__ import annotations

import json

from app import postprocess as PP

D4_V11_H03B_RESIDUE = "為了達到持久的單擦色效果，並且在嘴唇上感覺輕盈。"   # D4 v11 實際交付的殘句


def unit(uid, text="SYNTHETIC 來源短句", **kw):
    return {"unit_id": uid, "text": text, "conditions": [], "content_role": None, "support_scope": None,
            "product_mentions": [], **kw}


def run(claims, units, **policy_kw):
    raw = json.dumps({"insufficient": False, "claims": claims, "limitations": [], "clarifying_question": None},
                     ensure_ascii=False)
    pol = PP.Policy(kept_unit_ids=frozenset(policy_kw.pop("kept", units)), **policy_kw)
    return PP.postprocess(PP.parse_answer(raw), units, pol)


def texts(out):
    return [c["text"] for c in out["claims"]]


# ---------------------------------------------------------------------------
# H03 殘句（D04）：真失敗回歸
# ---------------------------------------------------------------------------
H03B_CLAIM1 = ("為了達到持久的單擦色效果，可以嘗試 L’Oréal Paris Infallible Matte Resistance Liquid Lipstick，"
               "它具有高色素沉澱、可持續長達 16 小時，並且在嘴唇上感覺輕盈。")


def test_h03b_product_core_claim_removed_whole_no_residue():
    units = {"seg1:346": unit("seg1:346", product_mentions=["L’Oréal Paris Infallible Matte Resistance Liquid Lipstick"]),
             "seg1:42": unit("seg1:42")}
    out = run([{"text": H03B_CLAIM1, "unit_ids": ["seg1:346"]},
               {"text": "唇部產品可以增加顏色、滋養嘴唇。", "unit_ids": ["seg1:42"]}], units)
    assert texts(out) == ["唇部產品可以增加顏色、滋養嘴唇。"]
    assert D4_V11_H03B_RESIDUE not in PP.render_delivered(out)
    gone = out["removed"][0]
    assert gone["rule"] == "product_whole_claim" and gone["claim"]["text"] == H03B_CLAIM1
    assert PP.PRODUCT_NOTE in out["system_notes"]


def test_h03b_shape_without_pronoun_still_removed_whole():
    """同 H03:B 句型但沒有「它…」子句：只刪商品子句會剩「為了…，並且…」，必須整條不交付。"""
    units = {"u": unit("u", product_mentions=["Brand X Liquid Lipstick"])}
    out = run([{"text": "為了達到持久的單擦色效果，可以嘗試 Brand X Liquid Lipstick，並且在嘴唇上感覺輕盈。", "unit_ids": ["u"]}], units)
    assert out["claims"] == [] and out["removed"][0]["rule"] == "product_whole_claim"


def test_h03b_fallback_detector_without_contract_field():
    """A 尚未給 product_mentions（None）時，由來源原文後備偵測，仍整條移除。"""
    src = "For long wear, try the L’Oréal Paris Infallible Matte Resistance Liquid Lipstick in Worth It."  # SYNTHETIC
    units = {"u": unit("u", src, product_mentions=None)}
    out = run([{"text": H03B_CLAIM1, "unit_ids": ["u"]}], units)
    assert out["claims"] == [] and out["status"] == "emptied_after_postprocess"
    assert out["removed"][0]["product_detector"] == ["fallback"]


# ---------------------------------------------------------------------------
# 正常中文省略句反例：沒有商品就不動；判斷不靠「有沒有主詞」
# ---------------------------------------------------------------------------
NORMAL_ELLIPSIS = [
    "薄擦一層，等乾後再補一層。",
    "如果需要更多遮蓋，再增加一層輕薄的疊加。",
    "為了讓顏色更持久，可以先薄擦一層再疊色。",
    "先塗護唇膏，等吸收後再上霧面唇膏。",
    "從嘴唇中央開始塗，再往嘴角推開。",
]


def test_normal_elliptical_sentences_untouched():
    units = {"u": unit("u", product_mentions=["Brand X Lipstick"])}
    out = run([{"text": t, "unit_ids": ["u"]} for t in NORMAL_ELLIPSIS], units)
    assert texts(out) == NORMAL_ELLIPSIS
    assert out["removed"] == [] and out["status"] == "delivered"


def test_elliptical_sentence_with_product_aside_keeps_main_clause():
    units = {"u": unit("u", product_mentions=["Brand X Lipstick"])}
    out = run([{"text": "先薄擦一層（例如 Brand X Lipstick），等乾後再補一層。", "unit_ids": ["u"]}], units)
    assert texts(out) == ["先薄擦一層，等乾後再補一層。"]
    assert out["claims"][0]["edit"]["removed_parts"] == ["（例如 Brand X Lipstick）"]


# ---------------------------------------------------------------------------
# 附帶舉例 vs 主張成分（D13 真輸出）
# ---------------------------------------------------------------------------
def test_n03_example_with_pronoun_chain_removed_to_sentence_end():
    raw = ("為了辦公室造型，可以將鮮豔的粉色眼影換成較柔和的色調，並將口紅換成清透唇蜜，"
           "例如 L’Oréal Paris Plump Ambition Hyaluron Lip Oil，它富含透明質酸和三肽，能滋潤嘴唇並增加柔和、襯托的色彩，非常適合日間佩戴。")
    units = {"u": unit("u", product_mentions=["L’Oréal Paris Plump Ambition Hyaluron Lip Oil in Worth It"])}
    out = run([{"text": raw, "unit_ids": ["u"]}], units)
    assert texts(out) == ["為了辦公室造型，可以將鮮豔的粉色眼影換成較柔和的色調，並將口紅換成清透唇蜜。"]


def test_n03_inline_example_keeps_following_effect_clause():
    raw = "對於小麥色或深膚色，選擇較深、更飽和的口紅顏色，如熱粉色的 Velvet Underground 或深紅的 Cinematic Red，可以顯得突出而不過分。"
    units = {"u": unit("u", product_mentions=["Velvet Underground", "Cinematic Red"])}
    out = run([{"text": raw, "unit_ids": ["u"]}], units)
    assert texts(out) == ["對於小麥色或深膚色，選擇較深、更飽和的口紅顏色，可以顯得突出而不過分。"]


def test_p01_product_sentence_removed_other_sentence_kept():
    raw = ("對於暖膚色，珊瑚色、磚紅色、豐富的巧克力色和深酒紅能美麗地襯托暖膚色。"
           "可以嘗試使用 L’Oréal Paris Colour Riche Intense Volume Matte Lipstick in Le Nude Assertive 試試看。")
    units = {"u": unit("u", product_mentions=["L’Oréal Paris Colour Riche Intense Volume Matte Lipstick in Le Nude Assertive"])}
    out = run([{"text": raw, "unit_ids": ["u"]}], units)
    assert texts(out) == ["對於暖膚色，珊瑚色、磚紅色、豐富的巧克力色和深酒紅能美麗地襯托暖膚色。"]


def test_dependent_sentence_after_removed_product_sentence_also_removed():
    units = {"u": unit("u", product_mentions=["Brand X Lipstick"])}
    out = run([{"text": "可以嘗試 Brand X Lipstick。它能讓唇色更持久。另外，薄擦會更自然。", "unit_ids": ["u"]}], units)
    assert texts(out) == ["另外，薄擦會更自然。"]


def test_p05_chinese_trademark_product_as_subject_removed():
    raw = "使用超持久霧感液態唇膏可以打造這款妝容，可擁有長達 16 小時不褪色的唇妝。"
    src = "向您介紹 超持久霧感液態唇膏™ 10 種明豔唇色。"   # SYNTHETIC（形式同保存證據）
    units = {"u": unit("u", src, product_mentions=None)}
    out = run([{"text": raw, "unit_ids": ["u"]}], units)
    assert out["claims"] == [] and out["removed"][0]["rule"] == "product_whole_claim"


def test_residue_with_only_subordinate_clause_removed():
    units = {"u": unit("u", product_mentions=["Brand X Lipstick"])}
    out = run([{"text": "為了讓顏色持久，例如 Brand X Lipstick。", "unit_ids": ["u"]}], units)
    assert out["claims"] == []
    assert "從屬子句" in out["removed"][0]["reason"] or "不足" in out["removed"][0]["reason"]


def test_unmatched_latin_name_is_suspect_flag_not_removed():
    units = {"u": unit("u", product_mentions=[])}
    out = run([{"text": "可以參考 Velvet Rose 這種乾燥玫瑰色。", "unit_ids": ["u"]}], units)
    assert texts(out) == ["可以參考 Velvet Rose 這種乾燥玫瑰色。"]
    assert any(f["flag"] == "suspect_product_name" for f in out["flags"])


# ---------------------------------------------------------------------------
# 使用者條件（D01 M01）
# ---------------------------------------------------------------------------
M01_UNITS = {
    "seg1:407": unit("seg1:407", "SYNTHETIC: best lipstick colours for tan skin are pigmented shades.", conditions=["for tan skin"]),
    "seg1:383": unit("seg1:383", "SYNTHETIC: bold shades look sensational on dark skin.", conditions=["for dark skin"]),
}
M01_CLAIMS = [
    {"text": "對於小麥色皮膚，建議選擇有色澤、色彩豐富的色調，這些色調不會被古銅色膚色掩蓋。", "unit_ids": ["seg1:407"]},
    {"text": "紅色、粉紅色和裸色都可以是襯托小麥色膚色的完美唇彩色。", "unit_ids": ["seg1:407"]},
    {"text": "亮紅色、桃紅色或珊瑚色在深色膚色上看起來非常生動。", "unit_ids": ["seg1:383"]},
]


# contract-v1 格式（與應用 preferences 解析結果相同）
M01_CONDS = [{"field": "skin_tone", "value": "小麥色", "values": None, "label": None, "raw": "我是小麥色皮膚", "source": "chat"}]


def test_m01_user_self_description_treated_as_source_synonym():
    # C-pp-v4（2026-10-27）：來源寫 tan skin → 小麥色當同義詞，照原文交付；第一次出現加「（來源寫 tan skin）」，不註記、不補問
    out = run(M01_CLAIMS, M01_UNITS, explicit_conditions=M01_CONDS, display_source_conditions=True)
    assert texts(out) == ["對於小麥色皮膚（來源寫 tan skin），建議選擇有色澤、色彩豐富的色調，這些色調不會被古銅色膚色掩蓋。",
                          "紅色、粉紅色和裸色都可以是襯托小麥色膚色的完美唇彩色。",
                          "亮紅色、桃紅色或珊瑚色在深色膚色上看起來非常生動。"]
    assert out["removed"] == []
    assert out["claims"][0]["condition_rewrite"]["source_term"] == "tan skin"
    assert out["claims"][2]["source_conditions"] == ["for dark skin"]
    assert out["clarifying_question"] is None and out["system_notes"] == []
    assert "for dark skin" in PP.render_delivered(out)


def test_m01_user_term_removed_when_source_has_no_matching_skin_term():
    """來源沒有對應的膚色分級（只寫 dark skin）→ 照舊整條移除，問膚色深淺。"""
    out = run([{"text": "小麥色皮膚適合亮紅色。", "unit_ids": ["seg1:383"]}], M01_UNITS, explicit_conditions=M01_CONDS)
    assert texts(out) == [] and [r["rule"] for r in out["removed"]] == ["user_condition_inference"]
    assert out["clarifying_question"].startswith("來源的建議是依膚色深淺分開寫的")


def test_user_term_allowed_when_source_itself_uses_it():
    units = {"u": unit("u", "SYNTHETIC：小麥色肌膚可選焦糖裸色。")}
    out = run([{"text": "小麥色肌膚可選焦糖裸色。", "unit_ids": ["u"]}], units, explicit_conditions=M01_CONDS)
    assert texts(out) == ["小麥色肌膚可選焦糖裸色。"]


def test_model_clarifying_question_kept_over_template():
    raw = json.dumps({"insufficient": False, "claims": [M01_CLAIMS[0]], "limitations": [],
                      "clarifying_question": "你的膚色比較接近 tan 還是 deep？"}, ensure_ascii=False)
    out = PP.postprocess(PP.parse_answer(raw), M01_UNITS,
                         PP.Policy(frozenset(M01_UNITS), explicit_conditions=M01_CONDS))
    assert out["clarifying_question"] == "你的膚色比較接近 tan 還是 deep？"
    assert out["clarifying_question_source"] == "model"


def test_undertone_assertion_only_when_unknown_and_not_conditional():
    units = {"u": unit("u")}
    claims = [{"text": "你是暖底，適合珊瑚色。", "unit_ids": ["u"]},
              {"text": "如果你是暖底，可以選珊瑚色。", "unit_ids": ["u"]},
              {"text": "對於冷底，可選藍調紅。", "unit_ids": ["u"]}]
    out = run(claims, units, explicit_conditions=M01_CONDS)
    assert texts(out) == ["如果你是暖底，可以選珊瑚色。", "對於冷底，可選藍調紅。"]
    cool = [{"field": "undertone", "value": "cool", "values": None, "label": None, "raw": "我是冷底", "source": "chat"}]
    out2 = run([{"text": "您是冷底，可選藍調紅。", "unit_ids": ["u"]}], units, explicit_conditions=cool)
    assert texts(out2) == ["您是冷底，可選藍調紅。"]


# ---------------------------------------------------------------------------
# 來源條件、引用、內容角色、選段標籤
# ---------------------------------------------------------------------------
def test_source_conditions_displayed_from_data_and_model_mismatch_flagged():
    units = {"u": unit("u", conditions=["Cool skin"])}
    out = run([{"text": "可以嘗試柔和的粉色。", "unit_ids": ["u"], "conditions": ["warm skin"]}], units,
              display_source_conditions=True)
    c = out["claims"][0]
    assert c["source_conditions"] == ["Cool skin"]
    flags = {f["flag"] for f in out["flags"]}
    assert {"model_condition_not_in_source", "source_condition_not_restated_by_model"} <= flags
    assert "來源適用條件：Cool skin" in PP.render_delivered(out)


def test_unit_with_multiple_conditions_flagged():
    units = {"u": unit("u", conditions=["For a fresh, daytime look", "for nighttime"])}
    out = run([{"text": "白天配柔粉霧面唇，晚上配裸色霧面唇與煙燻眼。", "unit_ids": ["u"]}], units)
    assert any(f["flag"] == "unit_has_multiple_conditions" for f in out["flags"])


def test_unit_ids_outside_kept_set():
    units = {"a": unit("a"), "b": unit("b")}
    out = run([{"text": "主張一。", "unit_ids": ["b"]}, {"text": "主張二是另一件事。", "unit_ids": ["a", "b"]}], units, kept={"a"})
    assert texts(out) == ["主張二是另一件事。"]
    assert out["removed"][0]["rule"] == "trace"
    assert out["claims"][0]["unit_ids"] == ["a"]
    assert out["claims"][0]["trace_check"] == "unit_ids_in_kept_set" and out["claims"][0]["content_support"] == "not_verified"


def test_product_claim_role_is_flag_only_method_sentence_kept():
    """A 把 PR015_h0139（薄擦補層做法＋商品推薦同段）整段標商品宣稱；R4 M02:B 的做法主張是正確的，不能因角色被刪。"""
    units = {"p": unit("p", content_role="商品宣稱", product_mentions=["Brand X Liquid Lipstick"])}
    method = "對於霧面唇彩，應從薄而均勻的塗抹開始，讓其定型，如果需要更多遮蓋，再增加一層輕薄的疊加。"   # R4 M02:B 主張 2
    out = run([{"text": method, "unit_ids": ["p"]},
               {"text": "可以嘗試 Brand X Liquid Lipstick，持妝 16 小時。", "unit_ids": ["p"]}], units)
    assert texts(out) == [method]
    assert out["removed"][0]["rule"] == "product_whole_claim"
    assert any(f["flag"] == "cites_only_product_claim_units" for f in out["flags"])


def test_empty_contract_mentions_still_checked_by_fallback():
    """A 第一批 PR025_h0055 的 product_mentions 是 []，原文有「超持久霧感液態唇膏™」。"""
    units = {"u": unit("u", "向您介紹 超持久霧感液態唇膏™ 10 種明豔唇色。", product_mentions=[])}
    out = run([{"text": "使用超持久霧感液態唇膏可以打造這款妝容。", "unit_ids": ["u"]}], units)
    assert out["claims"] == [] and out["removed"][0]["product_detector"] == ["contract+fallback"]


def test_condition_with_product_name_not_displayed():
    cond = "If you need a recommendation, we love the Brand X Liquid Lipstick in Crush Alert"
    units = {"u": unit("u", conditions=["Warm skin", "warm skin", cond], product_mentions=["Brand X Liquid Lipstick"])}
    out = run([{"text": "暖膚色可選珊瑚色。", "unit_ids": ["u"]}], units, display_source_conditions=True)
    assert out["claims"][0]["source_conditions"] == ["Warm skin"]
    assert "Brand X" not in PP.render_delivered(out)
    assert any(f["flag"] == "source_condition_not_displayed" for f in out["flags"])


def test_background_only_answer_is_not_direct_answer():
    """W05 型：只引用背景段（例如底調選色）時，不算回答試色方法。"""
    units = {"bg": unit("bg"), "m": unit("m")}
    out = run([{"text": "可以依冷暖底調選色。", "unit_ids": ["bg"]}], units, selection_labels={"bg": "背景", "m": "直接"})
    assert out["status"] == "no_direct_answer" and out["claims"][0]["role"] == "background"
    out2 = run([{"text": "看手臂和嘴唇上的試色。", "unit_ids": ["m"]}, {"text": "可以依冷暖底調選色。", "unit_ids": ["bg"]}],
               units, selection_labels={"bg": "背景", "m": "直接"})
    assert out2["status"] == "delivered" and [c["role"] for c in out2["claims"]] == ["answer", "background"]
    assert "背景（不是本題的直接做法）" in PP.render_delivered(out2)


def test_m03_near_duplicates_removed():
    a = "如果您是冷底，選擇藍色-紅調的紅色，或者暖色調的紅色，例如暖紅、橙色和黃色，可以做出大膽的陳述，或者選擇藍紅（blue-red）作為經典紅色。"
    b = "如果您的底調是冷底，可以選擇藍色-紅調的紅色，或者暖色調的紅色，例如暖紅、橙色和黃色，可以做出大膽的陳述。"
    out = run([{"text": a, "unit_ids": ["u"]}, {"text": b, "unit_ids": ["u"]}], {"u": unit("u")})
    assert texts(out) == [a] and out["removed"][0]["rule"] == "duplicate"
    assert not PP.near_duplicate("先塗護唇膏，等吸收後再上霧面唇膏。", "看手臂和嘴唇上的試色。")


# ---------------------------------------------------------------------------
# 解析與狀態
# ---------------------------------------------------------------------------
def test_parse_legacy_citations_with_label_map():
    raw = '{"insufficient": false, "claims": [{"text": "主張", "citations": ["E1", "E2"]}], "limitations": ["限"]}'
    p = PP.parse_answer(raw, label_map={"E1": "seg1:1", "E2": "seg1:2"})
    assert p.errors == [] and p.claims[0].unit_ids == ["seg1:1", "seg1:2"]


def test_parse_errors_mean_validation_failed():
    out = PP.postprocess(PP.parse_answer("not json"), {}, PP.Policy(frozenset()))
    assert out["status"] == "validation_failed" and out["claims"] == []
    p = PP.parse_answer('{"insufficient": false, "claims": [], "extra": 1}')
    assert "unexpected_field:extra" in p.errors


def test_insufficient_drops_claims():
    # C-pp-v3（2026-10-27）：附帶主張改為照一般規則檢查；這裡引用不在選用單位內 → 仍是來源不足（新行為見 test_pipeline_fixes_20261027）
    raw = '{"insufficient": true, "claims": [{"text": "x", "unit_ids": ["gone"]}], "limitations": ["缺資料"]}'
    out = PP.postprocess(PP.parse_answer(raw), {"u": unit("u")}, PP.Policy(frozenset({"u"})))
    assert out["status"] == "source_insufficient" and out["claims"] == [] and out["limitations"] == ["缺資料"]
    assert out["removed"][0]["rule"] == "trace"


def test_raw_preserved_and_mode_generated():
    raw = '{"insufficient": false, "claims": [{"text": "薄擦一層。", "unit_ids": ["u"]}], "limitations": []}'
    out = PP.postprocess(PP.parse_answer(raw), {"u": unit("u")}, PP.Policy(frozenset({"u"})))
    assert out["raw"] == raw and out["mode"] == "generated" and out["version"] == PP.POSTPROCESS_VERSION


def test_source_conditions_not_rendered_by_default():
    units = {"u": unit("u", conditions=["medium skin"])}
    out = run([{"text": "較深膚色可選飽和色。", "unit_ids": ["u"]}], units)
    assert out["claims"][0]["source_conditions"] == ["medium skin"]       # 資料保留
    assert "medium skin" not in PP.render_delivered(out)                  # 預設不顯示給使用者


def test_legacy_dict_conditions_still_normalized():
    assert PP.user_terms({"self_descriptions": ["小麥色"], "undertone": None}) == ["小麥色"]


def test_d_call_path_parse_claims_db_rows_default_policy():
    """D 的 qa.py：parse_claims(text) → postprocess(claims, units＝資料庫列, DEFAULT_POLICY)；限制與澄清問題不能丟。"""
    rows = {"PR012_h0151": {"chunk_key": "PR012_h0151", "quote_text": "SYNTHETIC",
                            "support_unit": {"product_mentions": ["Brand X Liquid Lipstick"], "conditions": []}},
            "PR015_h0137": {"chunk_key": "PR015_h0137", "quote_text": "SYNTHETIC", "support_unit": {}}}
    raw = json.dumps({"insufficient": False, "claims": [
        {"text": "為了持久，可以嘗試 Brand X Liquid Lipstick，並且感覺輕盈。", "unit_ids": ["PR012_h0151"]},
        {"text": "先薄擦一層護唇膏，等吸收後再上霧面唇膏。", "unit_ids": ["PR015_h0137"]}],
        "limitations": ["缺深紫唇遮蓋做法"], "clarifying_question": "你想要遮住原本唇色，還是只要自然？"}, ensure_ascii=False)
    claims = PP.parse_claims(raw)
    assert [c.text for c in claims][1].startswith("先薄擦")                      # 仍是 Claim 清單
    out = PP.postprocess(claims, rows, PP.DEFAULT_POLICY)
    assert [c["text"] for c in out["claims"]] == ["先薄擦一層護唇膏，等吸收後再上霧面唇膏。"]
    assert out["limitations"] == ["缺深紫唇遮蓋做法"] and out["clarifying_question"].startswith("你想要")
    assert out["raw"] == raw and out["claims"][0]["conditions"] == [] and "support_scope" in out["claims"][0]
    assert PP.postprocess(claims, rows)["status"] == out["status"]               # policy 省略＝DEFAULT_POLICY


def test_selector_none_claims_are_unjudged_not_direct():
    """E-028：選段器不接入，selection＝{selector: none, kept_unit_ids: 裝箱順序, items: []}；主張不能被當成「直接」。"""
    from app.answer_policy import policy_from

    units = {"a": unit("a"), "b": unit("b")}
    sel = {"selector": "none", "kept_unit_ids": ["b", "a"], "items": [], "status": "ok"}
    out = PP.postprocess(PP.parse_answer(json.dumps({"insufficient": False, "claims": [
        {"text": "看手臂和嘴唇上的試色。", "unit_ids": ["a"]}], "limitations": []}, ensure_ascii=False)), units, policy_from(sel, []))
    assert out["claims"][0]["role"] == "unjudged" and out["selection_mode"] == "none"
    assert out["status"] == "delivered"                                         # 不是 no_direct_answer
    assert "看手臂和嘴唇上的試色。" in PP.render_delivered(out)
    # selector＝none 時即使 items 有標籤也不讀
    sel2 = {**sel, "items": [{"unit_id": "a", "label": "直接"}]}
    assert policy_from(sel2, []).selection_labels == {}


def test_missing_label_is_unjudged_with_selector_labels():
    units = {"a": unit("a"), "b": unit("b")}
    out = run([{"text": "主張甲是一件事。", "unit_ids": ["a"]}, {"text": "主張乙是另一件事。", "unit_ids": ["b"]}], units,
              selection_labels={"a": "直接"})
    assert [c["role"] for c in out["claims"]] == ["answer", "unjudged"]


# ---------------------------------------------------------------------------
# reconcile-v1（2026-10-26）：與自己主張矛盾的限制、做法順序。句子是 SYNTHETIC，句型取自實機輸出。
# ---------------------------------------------------------------------------
def _delivered(texts, limitations):
    return {"status": "delivered", "claims": [{"claim_no": i, "text": t, "unit_ids": ["u"], "role": "unjudged",
                                               "source_conditions": []} for i, t in enumerate(texts, 1)],
            "limitations": list(limitations), "removed": [], "flags": [], "system_notes": [],
            "clarifying_question": None, "display_source_conditions": False}


def test_reconcile_removes_limitation_contradicting_own_claim_only():
    d = _delivered(["先塗一層薄薄的護唇膏，再擦霧面唇膏。", "為了持久，可以塗一層保濕的唇妝前乳。",
                    "選擇含有油的滋養型霧面唇膏，可以讓嘴唇舒適。"],
                   ["證據未說明如何選擇適合的霧面唇膏配方。",      # 主張 3 已在講 → 矛盾
                    "證據未說明如何選擇適合的唇妝前乳。",          # 主張只說用，沒說怎麼選 → 保留
                    "證據未說明使用定妝粉的具體步驟。"])            # 主張沒提 → 保留
    r = PP.reconcile(d, "嘴唇容易乾，有什麼辦法讓霧面不乾？")
    assert r["limitations"] == ["證據未說明如何選擇適合的唇妝前乳。", "證據未說明使用定妝粉的具體步驟。"]
    gone = [x for x in r["removed"] if x["rule"] == "limitation_contradicts_claim"]
    assert [x["limitation"] for x in gone] == ["證據未說明如何選擇適合的霧面唇膏配方。"]
    assert gone[0]["covered_by"].startswith("選擇含有油") and d["limitations"][0].startswith("證據未說明如何選擇適合的霧面")
    assert r["reconcile_version"] in ("reconcile-v5", "reconcile-v6", "reconcile-v7", "reconcile-v8")   # v6 只改問怎麼畫時的背景判斷


def test_reconcile_orders_method_steps_and_keeps_choice_claims_last():
    d = _delivered(["在開始上唇膏前，先塗一層薄薄的護唇膏，輕壓吸走多餘，再塗霧面唇膏。",
                    "選擇含有油的滋養型霧面唇膏。",
                    "如果嘴唇乾燥，應先去角質。",
                    "在去角質後塗一層護唇膏，等吸收再擦霧面唇膏。"], [])
    r = PP.reconcile(d, "嘴唇容易乾，有什麼辦法？")
    assert [c["claim_no"] for c in r["claims"]] == [3, 1, 4, 2]   # 去角質 → 護唇膏（原順序）→ 選產品排後
    flag = next(f for f in r["flags"] if f["flag"] == "reordered_by_procedure")
    assert flag["before"] == [1, 2, 3, 4] and flag["stages"] == [1, None, 0, 1]
    assert [c["text"] for c in d["claims"]][0].startswith("在開始上唇膏前")      # 原物件不改


def test_reconcile_does_not_reorder_non_method_or_single_step_answers():
    d = _delivered(["如果嘴唇乾燥，應先去角質。", "先塗一層護唇膏。"], [])
    assert [c["claim_no"] for c in PP.reconcile(d, "霧面唇膏是什麼？")["claims"]] == [1, 2]   # 不是問做法
    d = _delivered(["夜間可以用裸色霧面唇配煙燻眼。", "棕色煙燻眼可以配裸色唇膏。"],
                   ["證據未提供關於如何選擇特定唇色來避免過重效果的直接建議。"])
    r = PP.reconcile(d, "眼妝很濃，唇色要怎麼配？")
    assert [c["claim_no"] for c in r["claims"]] == [1, 2] and r["limitations"] == d["limitations"]
    assert not any(f["flag"] == "reordered_by_procedure" for f in r["flags"])


def test_reconcile_v2_off_target_claims_become_background():
    """reconcile-v2：問唇色時，完全沒提到唇的主張降為背景（另列、不刪）；不問唇的題目不動。"""
    d = _delivered(["夜間可以用裸色霧面唇配煙燻眼。", "白天煙燻眼可以把深灰換成灰褐、淺棕。",
                    "如果用鮮豔唇色，其他妝容保持輕薄。"], [])
    r = PP.reconcile(d, "眼妝想畫比較濃的煙燻，唇色要怎麼配才不會太重？")
    assert [c["role"] for c in r["claims"]] == ["unjudged", "background", "unjudged"]
    assert next(f for f in r["flags"] if f["flag"] == "off_target_claim")["claim_nos"] == [2]
    text = PP.render_delivered(r)
    assert text.index("背景（不是本題的直接做法）") < text.index("白天煙燻眼") and r["status"] == "delivered"
    assert PP.reconcile(_delivered(["MLBB 是中等亮度、低彩度的顏色。"], []), "MLBB 和乾燥玫瑰色差在哪？")["claims"][0]["role"] == "unjudged"
    only_eye = PP.reconcile(_delivered(["眼影換成柔和色。"], []), "唇色要怎麼配？")
    assert only_eye["status"] == "no_direct_answer"
    assert PP.RECONCILE_VERSION in ("reconcile-v5", "reconcile-v6", "reconcile-v7", "reconcile-v8")   # v6：問整個妝怎麼畫時不降背景（M09）


# ---------------------------------------------------------------------------
# C-pp-v2／reconcile-v3（2026-10-26，Gemini 3.5 保存輸出：P05、N03、W01、W04）
# ---------------------------------------------------------------------------
P05_STEP2 = "接著用櫻桃紅色唇膏填充嘴唇中央，並將兩種色調輕輕融合至無明顯分界線。"   # Gemini 3.5 P05 主張 3（保存輸出）


def test_p05_generic_color_lipstick_is_not_a_product():
    """「櫻桃紅色唇膏」是顏色＋品類的一般描述（A 契約列成商品指涉）：步驟保留，只記旗標。"""
    units = {"PR024_h0058": unit("PR024_h0058", content_role="商品宣稱", product_mentions=["櫻桃紅色唇膏", "超持久霧感液態唇膏"])}
    out = run([{"text": P05_STEP2, "unit_ids": ["PR024_h0058"]}], units)
    assert texts(out) == [P05_STEP2] and out["removed"] == []
    assert next(f for f in out["flags"] if f["flag"] == "generic_color_category_mention")["names"] == ["櫻桃紅色唇膏"]
    assert PP.generic_color_mention("深棕紅色液態唇膏") and not PP.generic_color_mention("超持久霧感液態唇膏")
    assert not PP.generic_color_mention("Ruby Woo 唇膏")


def test_product_as_step_tool_keeps_step_and_drops_product_claim():
    """具體商品是步驟工具：商品名換成品類、刪商品宣稱子句（16 小時不褪色），步驟保留；代詞指回商品的句子一併刪。"""
    units = {"u": unit("u", product_mentions=["超持久霧感液態唇膏"])}
    raw = "接著用超持久霧感液態唇膏填滿嘴唇中央，再輕輕融合兩種色調，可擁有長達 16 小時不褪色的唇妝。"
    out = run([{"text": raw, "unit_ids": ["u"]},
               {"text": "用超持久霧感液態唇膏填滿嘴唇中央。它可維持 16 小時。另外薄擦較自然。", "unit_ids": ["u"]}], units)
    assert texts(out) == ["接著用唇膏填滿嘴唇中央，再輕輕融合兩種色調。", "用唇膏填滿嘴唇中央。另外薄擦較自然。"]
    assert "16 小時" not in PP.render_delivered(out)
    assert out["claims"][0]["edit"]["reason"].startswith("商品是步驟的工具")


def test_product_not_a_step_tool_still_removed_whole():
    """「使用 X 可以打造這款妝容」「可以嘗試 X」不是步驟工具，仍整句不交付（同 test_p05_chinese_trademark…）。"""
    units = {"u": unit("u", product_mentions=["超持久霧感液態唇膏"])}
    out = run([{"text": "使用超持久霧感液態唇膏可以打造這款妝容。", "unit_ids": ["u"]},
               {"text": "可以嘗試超持久霧感液態唇膏來填滿唇色。", "unit_ids": ["u"]}], units)
    assert out["claims"] == [] and {r["rule"] for r in out["removed"]} == {"product_whole_claim"}


def test_condition_copied_into_text_is_flagged():
    """N03、P05 保存輸出：英文條件原樣貼進文字、「對於如果…」→ 旗標（文字不改）。"""
    units = {"a": unit("a", conditions=["for the office", "for daytime wear"]),
             "b": unit("b", conditions=["if you choose to emphasize your lips"])}
    out = run([{"text": "對於 for the office 或 for daytime wear，可將唇膏換成透亮唇蜜。", "unit_ids": ["a"],
                "conditions": ["for the office", "for daytime wear"]},
               {"text": "對於如果您選擇強調雙唇，可選擇大膽鮮豔的唇膏。", "unit_ids": ["b"],
                "conditions": ["if you choose to emphasize your lips"]},
               {"text": "如果是上班或白天，可以把唇膏換成透亮唇蜜。", "unit_ids": ["a"], "conditions": ["for the office"]}], units)
    got = {f["claim_no"]: f["conditions"] for f in out["flags"] if f["flag"] == "condition_copied_into_text"}
    assert got[1] == ["for the office", "for daytime wear"] and got[2][0].startswith("對於如果") and 3 not in got
    assert texts(out)[0].startswith("對於 for the office")


def test_reconcile_v3_color_only_claim_is_not_off_target():
    """W01：問口紅、主張只講「正紅這個色名」→ 不降背景；講眼影的仍降。"""
    w01 = "若想精確表達不偏粉也不偏橘的紅色，可以使用正紅（true red）這個色彩名稱來溝通。"
    r = PP.reconcile(_delivered([w01, "眼影換成柔和色。"], []), "我想找「紅色」口紅，可是不要那種偏粉或偏橘的紅，這樣要怎麼說比較準？")
    assert [c["role"] for c in r["claims"]] == ["unjudged", "background"] and r["status"] == "delivered"


def test_reconcile_v3_limitation_contradicting_evidence_removed():
    """W04：限制說「證據未提及 MLBB」，但送入片段原文有 MLBB → 拿掉限制、記旗標與系統註記；片段沒有的詞不動。"""
    lim = "證據未提及 MLBB 的詳細定義與色調比較，僅提供乾燥玫瑰色的適用膚色。"
    ev = ["SYNTHETIC: MLBB means my lips but better, a shade close to your natural lip color."]
    r = PP.reconcile(_delivered(["如果是冷色調肌膚，乾燥玫瑰色最適合。"], [lim, "證據未提及 HEX 色碼。"]), "MLBB 和乾燥玫瑰色差在哪？", ev)
    assert r["limitations"] == ["證據未提及 HEX 色碼。"]
    gone = next(x for x in r["removed"] if x.get("rule") == "limitation_contradicts_evidence")
    assert gone["terms"] == ["MLBB"] and gone["limitation"] == lim
    assert next(f for f in r["flags"] if f["flag"] == "evidence_term_unused")["terms"] == ["MLBB"]
    assert any("MLBB" in n for n in r["system_notes"])
    used = PP.reconcile(_delivered(["MLBB 是接近原本唇色的顏色。"], [lim]), "MLBB 是什麼？", ev)
    assert used["limitations"] == [] and not any(f["flag"] == "evidence_term_unused" for f in used["flags"])
    assert PP.reconcile(_delivered(["x"], [lim]), "q")["limitations"] == [lim]       # 沒給片段原文就不判斷
    assert PP.limitation_terms_in_evidence("證據未提及 lip 的比較", ["slippery"]) == []   # 英文詞要整字相符


def test_n01_bare_category_mention_is_not_a_product():
    """N01（Gemini 3.5 第一輪保存輸出）：A 契約把「護唇膏」列成商品指涉，濕敷步驟被整條刪；只有品類名不算具體商品。"""
    claim = "塗抹霧面唇膏前，可先使用含神經醯胺或荷荷巴油的護唇膏進行短暫濕敷，並擦掉多餘油脂。"
    units = {"PR031_h0026": unit("PR031_h0026", product_mentions=["護唇膏"])}
    out = run([{"text": claim, "unit_ids": ["PR031_h0026"]}], units)
    assert texts(out) == [claim] and out["removed"] == []
    assert PP.generic_color_mention("護唇膏") and not PP.generic_color_mention("超持久霧感液態唇膏")


# ---------------------------------------------------------------------------
# avoid-v1（2026-10-26）：主張違反使用者說的「不要太誇張／不要太鮮豔／不要太亮」
# ---------------------------------------------------------------------------
AVOID_VIVID = [{"field": "avoided_effects", "value": "vivid", "label": "鮮明亮眼", "raw": "但不要太誇張"}]
PENDING_BRIGHT = [{"field": "pending_ambiguity", "value": "brightness", "label": "不要太亮", "raw": "我想要上班用、不要太亮的唇色"}]


def test_p05_vivid_claim_removed_when_user_avoids_vivid():
    """P05（Gemini 3.5 保存輸出）：「不要太誇張」卻給「大膽鮮豔」→ 不列出；「中和誇張效果」是減弱，不算違反。"""
    units = {"a": unit("a"), "b": unit("b")}
    bold = "對於如果您選擇強調雙唇，可選擇大膽鮮豔的唇膏，並保持臉部其餘部分素顏。"
    soft = "融合較淺與較深的兩種紅色能使妝容更具現代感與豐滿，且紅棕色調能中和誇張效果。"
    out = run([{"text": soft, "unit_ids": ["a"]}, {"text": bold, "unit_ids": ["b"]}], units, explicit_conditions=AVOID_VIVID)
    assert texts(out) == [soft]
    gone = out["removed"][0]
    assert gone["rule"] == "user_avoidance" and gone["user_said"] == "不要太誇張" and gone["term"] == "大膽"
    assert "有內容和你說的「不要太誇張」相反，沒有列出。" in out["system_notes"]
    assert texts(run([{"text": bold, "unit_ids": ["b"]}], units)) == [bold]          # 使用者沒說就不動


def test_p03_pending_brightness_removes_vivid_and_notes_gloss():
    """P03：「不要太亮」意思未確認 → 濃豔紅唇不列出（兩種意思都不會想要）；透亮唇蜜保留並註記。"""
    units = {"a": unit("a"), "b": unit("b")}
    gloss = "如果是為了辦公室或白天配戴，可以將唇膏換成透亮唇蜜。"
    out = run([{"text": gloss, "unit_ids": ["a"]}, {"text": "假日到了是塗抹濃豔紅唇的好時刻。", "unit_ids": ["b"]}],
              units, explicit_conditions=PENDING_BRIGHT)
    assert texts(out) == [gloss] and out["removed"][0]["rule"] == "user_avoidance"
    assert any(f["flag"] == "may_conflict_pending_ambiguity" for f in out["flags"])
    assert any("是指不要亮面質地" in n for n in out["system_notes"])
    disliked_gloss = [{"field": "disliked_finishes", "value": "gloss", "label": "亮面", "raw": "我不要亮面"}]
    assert run([{"text": gloss, "unit_ids": ["a"]}], units, explicit_conditions=disliked_gloss)["claims"] == []


def test_dup_v2_keeps_later_claim_with_new_content():
    """dup-v2：後一條帶來新內容就不算重複（M08 輕點唇中央、N11 紫藍色→冷底）；M03 那種幾乎全被包含的仍刪。"""
    m08_a = "如果是深膚色且喜歡淺色口紅，可以搭配深色唇線來使用。"
    m08_b = "如果是深膚色且喜歡淺色口紅，可以搭配深色唇線，並將唇膏輕點在唇部中心以產生修飾效果。"
    n11_a = "在自然光下觀察手腕血管，若呈現綠色則可能為暖底。"
    n11_b = "在自然光下觀察手腕血管，若呈現紫色或藍色則可能為冷底。"
    assert not PP.repeats_delivered(m08_b, m08_a)
    assert not PP.repeats_delivered(n11_b, n11_a)
    out = run([{"text": n11_a, "unit_ids": ["u"]}, {"text": n11_b, "unit_ids": ["u"]}], {"u": unit("u")})
    assert texts(out) == [n11_a, n11_b] and not out["removed"]
    assert out["duplicate_version"] == "dup-v3"


def test_dup_v3_keeps_longer_version_of_same_claim():
    """dup-v3：前一條只是後一條的縮短版（M08）→ 留長的；N11 冷暖兩句都留。"""
    short = "如果是深膚色且喜歡淺色口紅，可以搭配深色唇線來使用。"
    long = "如果是深膚色且喜歡淺色口紅，可以搭配深色唇線，並將唇膏輕點在唇部中心以產生修飾效果。"
    assert PP.superseded_by(short, long)
    assert not PP.superseded_by("在自然光下觀察手腕血管，若呈現綠色則可能為暖底。", "在自然光下觀察手腕血管，若呈現紫色或藍色則可能為冷底。")
    out = run([{"text": short, "unit_ids": ["u"]}, {"text": long, "unit_ids": ["u"]}], {"u": unit("u")})
    assert texts(out) == [long] and out["removed"][0]["rule"] == "duplicate_superseded"


def test_vague_v1_drops_empty_pairing_and_lost_exception():
    """vague-v1：「搭配與其匹配的唇線筆」拿掉；來源有 apart from 例外、主張寫「所有」拿掉；一般句不動。"""
    assert PP.vague_or_overgeneral("如果是小麥色膚色，建議搭配與其匹配的唇線筆。", ["Pair it with the matching lip liner"]) == "vague_product_pairing"
    src = "Apart from light orange and light pink, all groups demonstrate a statistically significant difference."
    assert PP.vague_or_overgeneral("研究觀察發現，與裸唇相比，所有測試組別在感知亮度上都有顯著差異。", [src]) == "drops_source_exception"
    assert PP.vague_or_overgeneral("研究觀察發現，紅色唇膏會增加臉部亮度。", [src]) is None
    assert PP.vague_or_overgeneral("所有膚色都適合紅色。", ["Red suits every skin tone."]) is None
