"""修復 C 回答提示組裝候選（C-ap-v1）測試。模型輸出一律是手寫 MOCK，不呼叫任何模型。"""
from __future__ import annotations

import json

from app import answer_policy as AP
from app import postprocess as PP


def unit(uid, text, **kw):
    return {"unit_id": uid, "text": text, "title": "SYNTHETIC doc", "locator_primary": "p.1", "conditions": [],
            "content_role": None, "support_scope": None, "product_mentions": [], **kw}


UNITS = {
    "a": unit("a", "SYNTHETIC: look at arm and lip swatches before buying.", conditions=["When shopping for lipstick online"]),
    "b": unit("b", "SYNTHETIC: warm undertones suit corals.", content_role="選色"),
    "c": unit("c", "SYNTHETIC: unrelated text that was not selected."),
}
SELECTION = {"kept_unit_ids": ["b", "a"], "items": [
    {"unit_id": "b", "label": "背景", "required_conditions": [], "basis_quote": "warm undertones"},
    {"unit_id": "a", "label": "直接", "required_conditions": [], "basis_quote": "arm and lip swatches"},
    {"unit_id": "c", "label": "不適用", "required_conditions": [], "basis_quote": ""},
]}


def data_of(prompt):
    user = prompt.messages[1]["content"]
    return json.loads(user.split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0])


def test_system_prompt_has_no_fixed_tan_or_pout_mapping():
    # C-ap-v4（2026-10-27）：決定小麥色＝tan skin（只這一組）；古銅色等其他對照、pouty 譯名仍不放
    s = AP.SYSTEM_PROMPT
    assert "小麥色或古銅色" not in s and "古銅" not in s
    assert "「小麥色」和證據的「tan skin」是同一種膚色" in s
    assert "嘟唇感" not in s and "pouty＝" not in s
    assert "unit_ids" in s and "clarifying_question" in s


def test_system_prompt_separates_skin_depth_and_undertone():
    s = AP.SYSTEM_PROMPT
    assert "膚色是皮膚表面的深淺" in s and "底調是皮膚底下的冷、暖、中性色調" in s
    assert "不要從膚色推定底調" in s


def test_only_kept_units_background_last_and_unknown_conditions_marked():
    p = AP.build_answer_prompt("色號名很抽象，怎麼知道實際顏色？", AP.extract_explicit_conditions("色號名很抽象，怎麼知道實際顏色？"),
                               SELECTION, UNITS)
    d = data_of(p)
    assert [e["id"] for e in d["evidence"]] == ["a", "b"]           # 直接在前、背景在後
    assert "unrelated text" not in p.messages[1]["content"]
    assert d["evidence"][0]["conditions"] == ["When shopping for lipstick online"]
    assert d["evidence"][1]["selection_label"] == "背景"
    assert d["user_conditions"]["使用者明說"] == []
    assert d["user_conditions"][AP.UNKNOWN] == ["自述膚色", "底調", "場合"]
    assert p.unit_order == ["a", "b"] and p.excluded_unit_ids == []


def test_prompt_sha_is_deterministic_and_missing_units_excluded():
    sel = {"kept_unit_ids": ["a", "zzz"], "items": []}
    p1 = AP.build_answer_prompt("q", {}, sel, UNITS)
    p2 = AP.build_answer_prompt("q", {}, sel, UNITS)
    assert p1.sha256 == p2.sha256 and len(p1.sha256) == 64
    assert p1.excluded_unit_ids == ["zzz"] and p1.notes


def test_explicit_conditions_contract_v1_format_without_inference():
    """離線抽取用應用同一個偏好解析器，格式 [{field, value, values, label, raw, source}]，小麥色只是自述膚色。"""
    c = AP.extract_explicit_conditions("我是小麥色皮膚，推薦什麼？")
    assert [x["field"] for x in c] == ["skin_tone"]
    assert c[0]["value"] == "小麥色" and c[0]["raw"] == "我是小麥色皮膚"
    assert set(c[0]) == {"field", "value", "values", "label", "raw", "source"} and c[0]["source"] == "offline_runner"
    assert not any(x["field"] == "undertone" for x in c)                 # 不把小麥色推定成暖底
    cool = AP.extract_explicit_conditions("我是冷底，想擦紅色")
    # avoid-v1：離線抽取和應用（main._explicit_conditions）一樣也帶清單欄位（想擦紅色 → 喜歡的色系）
    assert [(x["field"], x["value"]) for x in cool] == [("undertone", "cool"), ("liked_colors", "red")]
    view = AP._conditions_view(c)
    assert view["使用者明說"] == [{"條件": "自述膚色", "值": "小麥色", "使用者原話": "我是小麥色皮膚"}]
    assert view[AP.UNKNOWN] == ["底調", "場合"]


def test_round_trip_with_mock_output():
    """MOCK 模型輸出：組提示 → 解析 → 後處理，unit_id 與條件可追溯；背景主張標背景。"""
    cond = AP.extract_explicit_conditions("色號名很抽象，怎麼知道實際顏色？")
    p = AP.build_answer_prompt("色號名很抽象，怎麼知道實際顏色？", cond, SELECTION, UNITS)
    mock = json.dumps({"insufficient": False, "claims": [
        {"text": "網購時先看手臂和嘴唇上的試色。", "unit_ids": ["a"], "conditions": ["When shopping for lipstick online"],
         "support_scope": None},
        {"text": "暖底可以選珊瑚色。", "unit_ids": ["b"], "conditions": [], "support_scope": None},
        {"text": "不在選段內的主張。", "unit_ids": ["c"], "conditions": [], "support_scope": None},
    ], "limitations": [], "clarifying_question": None}, ensure_ascii=False)
    out = PP.postprocess(PP.parse_answer(mock), UNITS, AP.policy_from(SELECTION, cond))
    assert [c["role"] for c in out["claims"]] == ["answer", "background"]
    assert out["removed"][0]["rule"] == "trace"
    assert not any(f["flag"].startswith("source_condition") for f in out["flags"] if f.get("claim_no") == 1)
    assert set(p.response_schema["properties"]) == {"insufficient", "claims", "limitations", "clarifying_question"}
    assert p.response_schema["properties"]["claims"]["items"]["properties"]["unit_ids"]["items"]["enum"] == ["a", "b"]
    assert "enum" not in AP.RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]["unit_ids"]["items"]


def test_phrases_for_selection_and_locator_object():
    c = AP.extract_explicit_conditions("我是冷底，想要上班用")
    assert AP.explicit_condition_phrases(c) == ["冷底", "想要上班用"]                  # 場合代碼 office 不外露，用原話
    assert AP.explicit_condition_phrases(AP.extract_explicit_conditions("我是小麥色皮膚")) == ["小麥色"]
    units = {"h": unit("h", "SYNTHETIC html text", locator_primary={"type": "html_dom", "url": "https://x.example/a",
                                                                    "heading_path": ["Lip", "Matte"], "dom_paths": ["/p[3]"]})}
    p = AP.build_answer_prompt("q", {}, {"kept_unit_ids": ["h"], "items": []}, units)
    assert data_of(p)["evidence"][0]["locator"] == "Lip > Matte"


def test_prompt_conditions_drop_product_and_long_sentences():
    long = "If you’re going bold with your matte lip—think vibrant pinks, crimsons, or deep burgundy shades—it’s best to keep the rest light"
    units = {"u": unit("u", "SYNTHETIC", conditions=["Cool skin", "cool skin", "we love the Brand X Lipstick in Red", long],
                       product_mentions=["Brand X Lipstick"])}
    p = AP.build_answer_prompt("q", {}, {"kept_unit_ids": ["u"], "items": []}, units)
    assert data_of(p)["evidence"][0]["conditions"] == ["Cool skin"]


def test_db_row_units_from_d_are_accepted():
    """D 傳的是資料庫列：quote_text＋support_unit 子物件＋page／html 定位（D-REQ-04）。"""
    row = {"chunk_key": "PR015_h0123", "quote_text": "SYNTHETIC: Cool skin: soft pinks.", "title": "SYNTHETIC doc",
           "section": "Lip", "page_start": None, "page_end": None, "html_locator": "#main > p:nth-of-type(3)",
           "support_unit": {"conditions": ["Cool skin"], "product_mentions": [], "content_role": "方法", "support_scope": None}}
    p = AP.build_answer_prompt("q", [], {"kept_unit_ids": ["PR015_h0123"], "items": []}, {"PR015_h0123": row})
    ev = data_of(p)["evidence"][0]
    assert ev["text"] == row["quote_text"] and ev["conditions"] == ["Cool skin"] and ev["locator"] == row["html_locator"]


def test_v2_contract_is_compact_and_conditions_come_from_evidence():
    """C-ap-v2（2026-10-26，修輸出截斷）：證據不附 support_scope、輸出不要 support_scope；conditions 只能挑本次原文條件；
    主張／限制有數量與長度上限；意思唯一的譯名（lip balm＝護唇膏）寫在提示。"""
    assert AP.ANSWER_POLICY_VERSION in ("C-ap-v6", "C-ap-v10", "C-ap-v11", "C-ap-v12")   # C-ap-v10（2026-11-07）只改主張上限與限制規則
    units = {**UNITS, "b": unit("b", "SYNTHETIC: warm undertones suit corals.", content_role="選色",
                                support_scope="部位：嘴唇；操作：按壓、疊擦")}
    p = AP.build_answer_prompt("q", {}, SELECTION, units)
    d = data_of(p)
    assert all("support_scope" not in e and "required_conditions" not in e for e in d["evidence"])
    assert "按壓、疊擦" not in p.messages[1]["content"]                     # 內部屬性標籤不送給模型
    item = p.response_schema["properties"]["claims"]["items"]
    assert set(item["properties"]) == {"text", "unit_ids", "conditions"} and "support_scope" not in item["required"]
    assert item["properties"]["conditions"]["items"]["enum"] == ["When shopping for lipstick online"]
    assert item["properties"]["conditions"]["maxItems"] == 2 and item["properties"]["text"]["maxLength"] == 120
    assert p.response_schema["properties"]["claims"]["maxItems"] == 6   # C-ap-v10
    assert p.response_schema["properties"]["limitations"]["maxItems"] == 2
    none = AP.build_answer_prompt("q", {}, {"kept_unit_ids": ["b"], "items": []}, units)
    assert none.response_schema["properties"]["claims"]["items"]["properties"]["conditions"] == {"type": "array", "maxItems": 0}
    assert "lip balm 是「護唇膏」" in AP.SYSTEM_PROMPT and "support_scope" not in AP.SYSTEM_PROMPT


def test_v6_no_generic_limitations_comparison_and_no_dusty_rose_rule():
    """C-ap-v5（2026-10-28）：限制名額留給本題需求，不寫產品／品牌／色號泛用句（除非使用者問）。
    C-ap-v6：dusty rose 對照不寫在規則（交給詞表 term_lookup，避免詞表關也拿到對照）；比較題用各自證據分別說明。"""
    s = AP.SYSTEM_PROMPT
    assert "dusty rose" not in s and "乾燥玫瑰" not in s
    assert "不要寫「證據沒有產品名稱／品牌／色號」" in s and "除非使用者問的就是產品、品牌或色號" in s
    assert "使用者問兩者差在哪時" in s and "只有其中一方完全沒有證據時才算不足" in s and "不要自行推論原文沒寫的優劣" in s
    assert "古銅" not in s                                              # 其他膚色對照照舊不放


def test_blot_translation_depends_on_usage():
    """C-ap-v11：blot 譯名要依用法區分——單獨 blot 是吸掉多餘，blot 唇膏 onto 某處是輕點，不能一律譯成吸掉。"""
    assert "把唇膏輕點在某處" in AP.SYSTEM_PROMPT
    assert "用面紙輕壓吸掉多餘" in AP.SYSTEM_PROMPT
    assert 'blot 是「用面紙輕壓吸掉多餘」；' not in AP.SYSTEM_PROMPT
