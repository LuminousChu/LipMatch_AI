"""修復 B：適用性選段器（程式依模型標籤真的保留／移除片段）。

MOCK：ScriptedLLM 回傳預先寫好的輸出，is_mock=True；只驗接口與程式選段規則，不代表 Gemma 會這樣判。
候選原文為 SYNTHETIC 改寫（情境仿 W05 試色、M03 冷底紅、M01 小麥色），不是語料原文。
"""
from __future__ import annotations

import json

import pytest

from app import evidence_selection as es
from app import retrieval_policy as rp
from app.retrieval_policy import Candidate


def cand(uid: str, text: str, rank: int, tokens: int = 40, conditions=None) -> Candidate:
    return Candidate(unit_id=uid, text=text, pool_rank=rank, tokens=tokens, rerank_rank=rank,
                     meta={"conditions": conditions or []})


W05_Q = "專櫃口紅的色號名字很抽象，我要怎麼知道它實際是什麼顏色？"
W05 = [
    cand("u-swatch", "SYNTHETIC: Check arm swatches and lip swatches before buying. Photos online can mislead.", 1),
    cand("u-vein", "SYNTHETIC: Look at the veins on your wrist. Blue veins suggest a cool undertone.", 2),
    cand("u-cool", "SYNTHETIC: For cool skin, try soft pinks and berry shades.", 3, conditions=["cool skin"]),
]


CODE = {v: k for k, v in es.LABEL_CODES.items()}


def out(*items) -> str:
    """測試用 evsel-v2 輸出：items 以 (id, 中文標籤, 條件, 依據, 備註) 撰寫，轉成短鍵 i／l／b／c（備註不輸出）。"""
    return json.dumps({"items": [{"i": i, "l": CODE.get(lab, lab), "b": b, "c": c} for i, lab, c, b, _ in items]},
                      ensure_ascii=False)


def _v1_out(*items) -> str:
    return json.dumps({"items": [dict(zip(("id", "label", "required_conditions", "basis", "reason"), it))
                                 for it in items]}, ensure_ascii=False)


def test_offtopic_units_are_removed_and_not_backfilled():
    llm = es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], "試色方法"),
                              ("C2", "不適用", [], ["C2.s1"], "底調判斷"),
                              ("C3", "不適用", [], ["C3.s1"], "底調選色"))])
    sel = es.select_evidence(W05_Q, [], W05, llm=llm, attempt_id="T-1")
    assert sel.status == "ok" and sel.llm_is_mock is True and sel.llm_model == "mock:scripted"
    assert sel.kept_unit_ids == ["u-swatch"]
    assert [(r["unit_id"], r["reason"]) for r in sel.removed] == [("u-vein", "label_不適用"), ("u-cool", "label_不適用")]
    assert sel.items[0]["basis_quote"] == ["SYNTHETIC: Check arm swatches and lip swatches before buying."]
    packed = rp.pack_budget([c for c in W05 if c.unit_id in sel.kept_unit_ids])
    assert [c.unit_id for c in packed] == ["u-swatch"]          # 不湊滿 7 段
    assert sel.attempt_id == "T-1" and sel.elapsed_ms is not None
    assert sel.input_tokens is None                                # mock 沒回報 → 未知，不記 0


def test_keep_label_without_valid_basis_becomes_uncertain():
    llm = es.ScriptedLLM([out(("C1", "直接", [], ["C2.s1"], "引用到別段"),
                              ("C2", "直接", [], [], "沒有依據"),
                              ("C3", "背景", [], [], "背景"))])
    sel = es.select_evidence(W05_Q, [], W05, llm=llm)
    labels = {i["unit_id"]: (i["label"], i["model_label"], i["notes"]) for i in sel.items}
    assert labels["u-swatch"] == ("不確定", "直接", ["basis_not_in_candidate", "keep_label_without_valid_basis"])
    assert labels["u-vein"][0] == "不確定"
    assert sel.kept_unit_ids == ["u-cool"]          # 背景可留，但只有 1 段且排在方法後
    assert "no_direct_or_conditional_evidence" in sel.flags and "basis_outside_candidate" in sel.flags


def test_source_condition_kept_when_user_stated_it():
    c = [cand("u-red", "SYNTHETIC: Blue-red suits cool skin.", 1, conditions=["cool skin"])]
    sel = es.select_evidence("我是冷底，想擦紅色", ["冷底"], c,
                             llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], "冷底選紅"))]))
    assert sel.items[0]["label"] == "直接" and sel.items[0]["required_conditions"] == []


def test_unstated_skin_tone_is_not_inferred_from_wheat():
    # D01 情境：使用者說「小麥色」，來源條件是 tan skin → 不得當成已知；程式把直接改為條件式
    c = [cand("u-tan", "SYNTHETIC: On tan skin, nude browns look natural.", 1, conditions=["tan skin"])]
    sel = es.select_evidence("我是小麥色皮膚，適合什麼裸色？", ["小麥色"], c,
                             llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], "膚色裸色"))]))
    it = sel.items[0]
    assert it["label"] == "條件式" and it["required_conditions"] == ["tan skin"]
    assert "downgraded_source_condition_unmet" in it["notes"]
    assert sel.kept_unit_ids == ["u-tan"]


@pytest.mark.parametrize("src, stated, met", [
    ("cool skin", ["冷底"], True), ("warm undertone", ["暖色調"], True), ("cool skin", ["暖底"], False),
    ("tan skin", ["小麥色"], False), ("deep skin", [], False), ("眼妝較重時", ["眼妝較重時"], True), ("", [], True)])
def test_condition_met(src, stated, met):
    assert es.condition_met(src, stated) is met


def test_background_after_methods_and_capped():
    cs = [cand(f"u{i}", f"SYNTHETIC sentence {i}.", i) for i in range(1, 6)]
    llm = es.ScriptedLLM([out(("C1", "背景", [], ["C1.s1"], ""), ("C2", "背景", [], ["C2.s1"], ""),
                              ("C3", "條件式", ["eye makeup heavy"], ["C3.s1"], ""),
                              ("C4", "直接", [], ["C4.s1"], ""), ("C5", "不確定", [], [], ""))])
    sel = es.select_evidence("q", [], cs, llm=llm)
    assert sel.kept_unit_ids == ["u4", "u3", "u1"]
    assert {r["unit_id"]: r["reason"] for r in sel.removed} == {"u5": "label_不確定", "u2": "background_cap"}
    assert sel.items[2]["required_conditions"] == ["eye makeup heavy"]


def test_input_budget_skips_whole_units_and_traces_fate():
    cs = [cand("a", "SYNTHETIC a.", 1, tokens=1500), cand("long", "SYNTHETIC long.", 2, tokens=900),
          cand("b", "SYNTHETIC b.", 3, tokens=400)]
    llm = es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""), ("C2", "直接", [], ["C2.s1"], ""))])
    sel = es.select_evidence("q", [], cs, llm=llm, max_input_tokens=2048)
    data = json.loads(llm.calls[0]["messages"][1]["content"].split("<data>\n")[1].split("\n</data>")[0])
    assert [c["id"] for c in data["candidates"]] == ["C1", "C2"]
    assert data["candidates"][1]["sentences"][0]["text"] == "SYNTHETIC b."      # 完整原文
    assert sel.not_judged == [{"unit_id": "long", "reason": "selection_input_budget", "tokens": 900, "remaining": 548}]
    assert sel.kept_unit_ids == ["a", "b"] and sel.evidence_tokens == 1900
    packed = rp.pack_budget([c for c in cs if c.unit_id in sel.kept_unit_ids])
    fates = es.unit_fates(["a", "long", "missing"], pool=cs, reranked=cs, selection=sel, packed=packed)
    assert fates["long"]["stopped_at"] == "selection_input_budget"
    assert fates["missing"]["stopped_at"] == "pool_outside"
    assert fates["a"]["stopped_at"] is None and fates["a"]["in_final_context"] is True


def test_pack_after_selection_reports_budget_stop():
    cs = [cand("a", "SYNTHETIC a.", 1, tokens=1200), cand("b", "SYNTHETIC b.", 2, tokens=1000)]
    sel = es.select_evidence("q", [], cs, llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""),
                                                                    ("C2", "直接", [], ["C2.s1"], ""))]),
                             max_input_tokens=4096)
    packed = rp.pack_budget([c for c in cs if c.unit_id in sel.kept_unit_ids], max_tokens=2048)
    assert es.unit_fates(["b"], pool=cs, reranked=cs, selection=sel, packed=packed)["b"]["stopped_at"] == "pack_budget"


@pytest.mark.parametrize("raw, err", [
    ("not json", "not_json"), (json.dumps({"items": [], "x": 1}), "unexpected_shape"),
    (out(("C9", "直接", [], [], "")), "unknown_candidate_id"),
    (out(("C1", "好", [], [], "")), "unknown_label"),
    (out(("C1", "直接", [], ["C1.s1"], ""), ("C1", "背景", [], [], "")), "duplicate_candidate_id")])
def test_invalid_output_keeps_nothing(raw, err):
    sel = es.select_evidence(W05_Q, [], W05, llm=es.ScriptedLLM([raw]))
    assert sel.status == "invalid_output" and sel.error == err
    assert sel.kept_unit_ids == [] and sel.raw_llm_output == raw


def test_missing_item_counts_as_uncertain():
    sel = es.select_evidence(W05_Q, [], W05, llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""))]))
    assert sel.kept_unit_ids == ["u-swatch"]
    assert [i["notes"] for i in sel.items[1:]] == [["missing_in_output"], ["missing_in_output"]]


def test_llm_error_keeps_nothing():
    sel = es.select_evidence(W05_Q, [], W05, llm=es.ScriptedLLM([TimeoutError("x")]))
    assert sel.status == "llm_error" and sel.error == "TimeoutError" and sel.kept_unit_ids == []


def test_no_candidates():
    sel = es.select_evidence("q", [], [], llm=es.ScriptedLLM([]))
    assert sel.status == "no_candidates" and sel.kept_unit_ids == []


def test_schema_fixes_one_item_per_candidate_with_own_sentences():
    si = es.build_selection_input(W05_Q, [], W05)
    items = si.json_schema["properties"]["items"]
    assert "items" not in items and len(items["prefixItems"]) == items["minItems"] == items["maxItems"] == 3
    first = items["prefixItems"][0]["properties"]
    assert first["i"] == {"type": "string", "const": "C1"}
    assert first["b"]["items"]["enum"] == ["C1.s1", "C1.s2"] and first["b"]["maxItems"] == 2
    assert first["l"]["enum"] == ["D", "C", "B", "X", "U"]
    assert set(first) == {"i", "l", "b", "c"}                          # 沒有理由欄
    data = si.messages[1]["content"]
    assert "source_conditions" in data and "cool skin" in data
    assert W05_Q not in si.messages[0]["content"]                     # 使用者文字不進 system


def test_split_sentences_keeps_original_text():
    assert es.split_sentences("A b. C d!  E\nF。G") == ["A b.", "C d!", "E", "F。", "G"]
    assert es.split_sentences("   ") == []


def test_log_view_has_no_candidate_text():
    sel = es.select_evidence(W05_Q, [], W05, llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""))]))
    view = json.dumps(sel.log_view(), ensure_ascii=False)
    assert "Blue veins" not in view and sel.log_view()["prompt_version"] == es.SELECTION_PROMPT_VERSION


D_WHEAT = [{"field": "skin_tone", "value": "tan", "values": ["tan"], "label": "膚色", "raw": "小麥色", "source": "user"}]
D_COOL = [{"field": "undertone", "value": "cool", "values": [], "label": "底調", "raw": "我是冷底", "source": "user"}]


def test_condition_phrases_from_d_format():
    assert es.condition_phrases(D_WHEAT) == ["小麥色"]                  # 不取 value=tan（D01）
    assert es.condition_phrases(D_COOL) == ["我是冷底", "冷底"]
    assert es.condition_phrases([{"field": "undertone", "value": "warm", "raw": "暖", "source": "inferred"}]) == []
    assert es.condition_phrases(["冷底", "冷底", " "]) == ["冷底"]


def test_d_format_wheat_still_not_tan():
    c = [cand("u-tan", "SYNTHETIC: On tan skin, nude browns look natural.", 1, conditions=["tan skin"])]
    sel = es.select_evidence("我是小麥色皮膚，適合什麼裸色？", D_WHEAT, c,
                             llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""))]))
    assert sel.items[0]["label"] == "條件式" and sel.items[0]["required_conditions"] == ["tan skin"]
    content = es.build_selection_input("q", D_WHEAT, c).messages[1]["content"]
    data = json.loads(content.split("<data>\n")[1].split("\n</data>")[0])
    assert data["user_stated_conditions"] == ["小麥色"]


def test_d_format_cool_meets_cool_skin():
    c = [cand("u-red", "SYNTHETIC: Blue-red suits cool skin.", 1, conditions=["cool skin"])]
    sel = es.select_evidence("我是冷底，想擦紅色", D_COOL, c,
                             llm=es.ScriptedLLM([out(("C1", "直接", [], ["C1.s1"], ""))]))
    assert sel.items[0]["label"] == "直接"


def test_no_rerank_order_is_vector_order():
    cs = [Candidate(unit_id=f"u{i}", text=f"SYNTHETIC {i}.", pool_rank=i, tokens=700) for i in range(1, 5)]
    si = es.build_selection_input("q", [], cs)
    assert [c.unit_id for c in si.judged] == ["u1", "u2"] and [x["unit_id"] for x in si.not_judged] == ["u3", "u4"]


# ---------------------------------------------------------------------------
# evsel-v2（E-016 定位補修）
# ---------------------------------------------------------------------------
def test_v1_format_is_rejected():
    raw = json.dumps({"items": [{"id": "C1", "label": "直接", "required_conditions": [], "basis": ["C1.s1"],
                                 "reason": "x"}]}, ensure_ascii=False)
    sel = es.select_evidence(W05_Q, [], W05[:1], llm=es.ScriptedLLM([raw]))
    assert sel.status == "invalid_output" and sel.error == "item_shape"


def test_model_condition_not_literally_stated_downgrades_direct():
    # S01 W05 型態：使用者沒說底調，模型卻寫「使用者膚色為冷色調」並判直接 → 程式改條件式
    c = [cand("u-cool", "SYNTHETIC: For cool skin, try soft pinks.", 1)]
    sel = es.select_evidence(W05_Q, [], c, llm=es.ScriptedLLM([out(("C1", "直接", ["使用者膚色為冷色調"], ["C1.s1"], ""))]))
    it = sel.items[0]
    assert it["label"] == "條件式" and it["required_conditions"] == ["使用者膚色為冷色調"]
    assert "downgraded_model_condition_unmet" in it["notes"] and it["model_conditions"] == ["使用者膚色為冷色調"]


def test_model_condition_literally_stated_is_dropped_and_stays_direct():
    c = [cand("u-red", "SYNTHETIC: Blue-red suits you.", 1)]
    sel = es.select_evidence("我是冷底，想擦紅色", ["冷底", "想擦紅色"], c,
                             llm=es.ScriptedLLM([out(("C1", "直接", ["冷底"], ["C1.s1"], ""))]))
    assert sel.items[0]["label"] == "直接" and sel.items[0]["required_conditions"] == []


def test_model_condition_synonym_is_not_literal():
    # 模型寫「冷色調」、使用者說「冷底」：字面不符 → 保守改條件式（同義詞只用在 A 的來源條件）
    c = [cand("u-red", "SYNTHETIC: Blue-red suits you.", 1)]
    sel = es.select_evidence("我是冷底", ["冷底"], c, llm=es.ScriptedLLM([out(("C1", "直接", ["冷色調"], ["C1.s1"], ""))]))
    assert sel.items[0]["label"] == "條件式"


def test_non_undertone_condition_also_guarded():
    c = [cand("u-eye", "SYNTHETIC: Keep lips sheer when eyes are bold.", 1)]
    sel = es.select_evidence("約會妝唇色？", [], c, llm=es.ScriptedLLM([out(("C1", "直接", ["眼妝較重時"], ["C1.s1"], ""))]))
    assert sel.items[0]["label"] == "條件式" and sel.items[0]["required_conditions"] == ["眼妝較重時"]


def test_method_like_background_is_not_capped():
    # S01 W05 型態：試色方法被判背景、又被背景上限擠掉 → v2 方法型背景不受上限
    cs = [cand("u-research", "SYNTHETIC: A study grouped lipstick samples by color name.", 1),
          cand("u-swatch", "SYNTHETIC intro. Always look at arm and lip swatches before buying.", 2),
          cand("u-role", "SYNTHETIC: Notes about texture.", 3).__class__(
              unit_id="u-role", text="SYNTHETIC: Notes about texture.", pool_rank=3, tokens=40,
              meta={"conditions": [], "content_role": "方法"}),
          cand("u-other", "SYNTHETIC: Lip color communication matters.", 4)]
    llm = es.ScriptedLLM([out(*[(f"C{i}", "背景", [], [f"C{i}.s1"], "") for i in range(1, 5)])])
    sel = es.select_evidence(W05_Q, [], cs, llm=llm)
    assert sel.kept_unit_ids == ["u-swatch", "u-role", "u-research"]
    assert sel.removed == [{"unit_id": "u-other", "label": "背景", "reason": "background_cap"}]
    assert "background_method_kept" in sel.items[1]["notes"]


def test_method_background_still_after_direct_and_conditional():
    cs = [cand("u-step", "SYNTHETIC. Apply balm first.", 1), cand("u-direct", "SYNTHETIC answer.", 2)]
    sel = es.select_evidence("q", [], cs, llm=es.ScriptedLLM([out(("C1", "背景", [], [], ""), ("C2", "直接", [], ["C2.s1"], ""))]))
    assert sel.kept_unit_ids == ["u-direct", "u-step"]


@pytest.mark.parametrize("text, step", [
    ("When shopping online, always look at arm and lip swatches.", True),
    ("Apply a thin layer of lip balm, blot away the excess.", True),
    ("First, exfoliate gently.", True),
    ("先塗護唇膏，再擦霧面。", True),
    ("Cool skin: soft pinks and berries.", False),
    ("研究顯示紅唇使臉看起來更紅。", False),
    ("The results suggest that lip color affects perception.", False)])
def test_has_operation_step(text, step):
    assert es.has_operation_step(text) is step


def test_output_cap_scales_with_items_and_is_passed():
    class Rec(es.ScriptedLLM):
        def complete(self, messages, *, json_schema, max_output_tokens):
            self.cap = max_output_tokens
            return super().complete(messages, json_schema=json_schema, max_output_tokens=max_output_tokens)
    llm = Rec([out(("C1", "直接", [], ["C1.s1"], ""), ("C2", "不適用", [], [], ""), ("C3", "不適用", [], [], ""))])
    es.select_evidence(W05_Q, [], W05, llm=llm)
    assert llm.cap == es.output_cap(3) == es.OUT_BASE + 3 * es.OUT_PER_ITEM


def test_prompt_has_no_dev_question_examples():
    # 規範 7：題組不進提示範例（v1 曾用試色／底調、整唇遮蓋／唇緣修邊當例子）
    for word in ("色號", "底調選色", "遮蓋", "唇緣", "小麥色"):
        assert word not in es.SYSTEM_PROMPT
