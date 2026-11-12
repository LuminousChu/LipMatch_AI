"""修復 A：來源支持單位（lipmatch/support_units.py）。

測試用片段是合成的（SYNTHETIC），只驗規則與欄位流向，不作正式驗收；真資料的 round-trip 由
scripts/repair_a/roundtrip_check.py 對隔離匯出執行。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from lipmatch import support_units as su

SYNTHETIC_PDF = {
    "manifest": {"doc_id": "ZZ01", "source_type": "pdf", "sha256": "a" * 64, "source_path": "data/raw/x.pdf"},
    "blocks": [],
}
SYNTHETIC_HTML = {
    "manifest": {"doc_id": "ZZ02", "source_type": "html", "sha256": "b" * 64, "title": "Lip Tips | Acme Beauty",
                 "source_url": "https://example.test/lips"},
    "blocks": [
        {"block_id": "ZZ02_h0001", "block_type": "heading", "heading_level": 1, "cleaned_text": "Lip Tips",
         "source_locator": "/html/body/main/h1"},
        {"block_id": "ZZ02_h0002", "block_type": "paragraph", "cleaned_text": "x", "source_locator": "/html/body/main/p[1]"},
        {"block_id": "ZZ02_h0003", "block_type": "paragraph", "cleaned_text": "y", "source_locator": "/html/body/main/p[2]"},
    ],
}


def _rec(doc_id, bid, text, **kw):
    return {"doc_id": doc_id, "block_id": bid, "block_ids": kw.pop("block_ids", [bid]), "block_type": "paragraph",
            "text": kw.pop("search", text), "display_text": text, "pages": kw.pop("pages", []),
            "section": kw.pop("section", "S"), "continues_from_block_id": kw.pop("cont", None),
            "citation": "(X, 2026)", "cited_works": [], "attribution": "no_citation_marker", **kw}


def test_full_text_sha_and_locators_kept():
    text = "Cover the eyelids and lips.\n2. | Match the foundation with the skin tone."
    rec = _rec("ZZ01", "ZZ01_p0026_b007", text, pages=[26], corroborating_units=[{
        "kind": "practical_activity", "relation": "repeats_steps_of", "title": "Activity 1",
        "block_ids": ["ZZ01_p0028_b015"], "step_block_ids": ["ZZ01_p0029_b003"], "pages": [28, 29],
        "steps_matched": "9/9", "display_text": "Activity 1\n• Cover the eyelids and lips."}])
    nxt = _rec("ZZ01", "ZZ01_p0027_b001", "continued", pages=[27], cont="ZZ01_p0026_b007")
    units = su.build_units(su.DocInputs("ZZ01", [rec, nxt], SYNTHETIC_PDF, {"doc_type": "vocational textbook"}, "e" * 64))
    u = units[0]
    assert u["text"] == text and u["text_sha256"] == su.sha256_text(text)
    assert u["locator_primary"] == {"type": "pdf_page", "pages": [26], "text": "p.26", "block_ids": ["ZZ01_p0026_b007"]}
    sec = u["locator_secondary"][0]
    assert sec["unit_id"] == "ZZ01_p0026_b007#c1" and sec["text"] == "p.28–29"
    assert sec["display_text_sha256"] == su.sha256_text("Activity 1\n• Cover the eyelids and lips.")
    assert u["links"] == {"prev": None, "next": "ZZ01_p0027_b001", "parent": None,
                          "corroborating_units": ["ZZ01_p0026_b007#c1"]}
    assert units[1]["links"]["parent"] == "ZZ01_p0026_b007" and units[1]["links"]["prev"] == "ZZ01_p0026_b007"
    assert u["eligibility"] == "pending"  # 匯出不是核准
    assert u["source_version"] == "a" * 64 and u["export_sha256"] == "e" * 64


def test_search_terms_only_from_own_text_and_base_kept():
    text = "Blot a lighter shade onto the centre of the lips, then line them with a darker lip liner."
    u = su.build_units(su.DocInputs("ZZ01", [_rec("ZZ01", "ZZ01_p0001_b001", text, pages=[1])], SYNTHETIC_PDF,
                                    {"doc_type": "vocational textbook"}, "e" * 64))[0]
    base, _, added = u["search_text"].partition("\n" + su.SEARCH_TERMS_MARK + " ")
    assert base == text and u["search_text_base_sha256"] == su.sha256_text(text)
    for t in ("按壓", "唇線筆", "唇中央"):
        assert t in added
    for t in ("遮瑕", "冷底", "上班", "乾燥玫瑰"):  # 原文沒有的不加
        assert t not in added


def test_tan_and_pout_not_translated_to_user_descriptions():
    text = "This shade is made for tan skin and gives your pout a soft slip."
    u = su.build_units(su.DocInputs("ZZ01", [_rec("ZZ01", "ZZ01_p0001_b001", text, pages=[1])], SYNTHETIC_PDF,
                                    {"doc_type": "vocational textbook"}, "e" * 64))[0]
    for bad in ("小麥", "古銅", "暖底", "嘟", "光澤"):
        assert bad not in u["search_text"]
    assert "for tan skin" in u["conditions"]  # 條件逐字保留，不翻譯


def test_conditions_verbatim_and_chinese_false_positives():
    conds = su.explicit_conditions("Blue-pinks are best suited for those with cool skin. If you have neutral undertones, "
                                   "you can rock any shade. When it comes to a matte lip, prep matters.")
    assert "for those with cool skin" in conds
    assert any(c.startswith("If you have neutral undertones") for c in conds)
    assert not any(c.lower().startswith("when it comes") for c in conds)
    assert su.explicit_conditions("這相當重要，當時的實驗顯示；當然可以。") == []
    assert su.explicit_conditions("如果眼妝很重，選柔和唇色。") == ["如果眼妝很重"]


def test_product_mentions_web_vs_academic():
    text = "Try the Acme Beauty Velvet Matte Lipstick by Acme Beauty for 16-hour wear."
    brands = su.brand_names("ZZ02", SYNTHETIC_HTML, {})
    assert brands == ["Acme Beauty"]
    web = su.product_mentions(text, brands)
    assert "Acme Beauty Velvet Matte Lipstick" in web
    acad = su.product_mentions("Measured by Kiritani with a SpectraFace® camera.", [], academic=True)
    assert acad == ["SpectraFace®"]  # 學術來源不把「by 作者」當品牌


def test_html_locator_and_product_claim_role():
    recs = [_rec("ZZ02", "ZZ02_h0002", "Our Acme Beauty Velvet Matte Lipstick formula lasts 16 hours."),
            _rec("ZZ02", "ZZ02_h0003", "Apply concealer around the edges of your lips for a crisp outline.")]
    units = su.build_units(su.DocInputs("ZZ02", recs, SYNTHETIC_HTML, {"doc_type": "web article（品牌）"}, "e" * 64))
    assert units[0]["locator_primary"]["dom_paths"] == ["/html/body/main/p[1]"]
    assert units[0]["locator_primary"]["heading_path"] == ["Lip Tips"]
    assert units[0]["content_role"] == "商品宣稱" and units[0]["product_mentions"]
    assert units[1]["content_role"] == "方法" and units[1]["product_mentions"] == []


def test_portable_manifest_survives_relocation(tmp_path):
    a = tmp_path / "a"
    (a / "data").mkdir(parents=True)
    f = a / "data" / "u.jsonl"
    f.write_text(json.dumps({"x": 1}) + "\n", encoding="utf-8")
    m = su.build_manifest(a, [f], {"schema": "t"})
    assert m["files"][0]["path"] == "data/u.jsonl" and m["files"][0]["lines"] == 1
    (a / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    b = tmp_path / "elsewhere" / "b"
    shutil.copytree(a, b)
    assert su.verify_manifest(b / "manifest.json") == []
    (b / "data" / "u.jsonl").write_text("tampered\n", encoding="utf-8")
    assert su.verify_manifest(b / "manifest.json") == ["data/u.jsonl：SHA-256 不符"]


def test_lexicon_has_no_question_ids_or_answers():
    blob = json.dumps(su.LEXICON, ensure_ascii=False)
    import re
    assert not re.search(r"\b[WMNPH]\d{2}\b", blob)
    assert "小麥" not in blob


def test_brand_voice_products_and_comma_method():
    text = ("Icon Baby is a perfect shade for blotting onto the centre of pigmented lips; pair it with my "
            "Lip Cheat Contour Duo for a contour effect.")
    found = su.product_mentions(text, [])
    assert "Lip Cheat Contour Duo" in found
    assert any(p.startswith("Icon Baby") for p in su.product_mentions("Try the Icon Baby lipstick today.", []))
    assert su.product_mentions("Red lipstick is classic.", []) == []  # 句首一個大寫詞不算商品
    method = "After swiping on your lipstick, use a small lip brush to apply concealer around the edges."
    assert su.content_role(method, academic=False, products=[]) == "方法"
    long_if = "If you have neutral undertones, you can rock just about any shade, so select one based on your skin tone and desired result."
    assert su.explicit_conditions(long_if) == ["If you have neutral undertones"]  # v2：只取條件子句，不帶主要子句


# ---- 第二批（contract-v1：C-REQ-5 條件、多情境 #r、中文品名／色號、tokens_cl100k、D 格式 manifest） ----

def test_conditions_v2_rejects_comparatives_findings_truncation_and_products():
    text = ("One of the most common mistakes when choosing a lipstick for tan and deep skin tones is choosing a shade "
            "that’s too light. Tan and deep complexions are naturally richer than fair and medium skin. Experiment "
            "freely—even if it’s against conventional “rules.” If you’re unsure whether your skin is warm, cool, or "
            "neutral, check your veins. If you want the Acme Velvet Matte Lipstick look, line first.")
    conds = su.explicit_conditions(text, products=["Acme Velvet Matte Lipstick"])
    assert "for tan and deep skin tones" in conds
    assert not any("medium" in c for c in conds)                 # 比較句 than 後的膚色
    assert not any(c.lower().startswith("when choosing") for c in conds)  # 句中 when 不是條件子句
    assert not any("rules" in c for c in conds)                  # even if＋截斷引號
    assert "If you’re unsure whether your skin is warm, cool, or neutral" in conds  # 列舉逗號不截斷
    assert not any("Acme" in c for c in conds)                   # 含商品名的條件不收
    assert su.explicit_conditions("Results were significant when observed by Chinese females.", academic=True) == []


def _units_for(text, doc_type="web article（品牌）"):
    rec = _rec("ZZ02", "ZZ02_h0002", text)
    return su.build_units(su.DocInputs("ZZ02", [rec], SYNTHETIC_HTML, {"doc_type": doc_type}, "e" * 64),
                          count_tokens=lambda t: len(t.split()))


def test_multi_scenario_split_contiguous_substrings():
    text = ("You can pair matte lipstick with almost anything. For a fresh, daytime look, pair a muted pink with "
            "light blush. This keeps it effortless. Or, for nighttime, opt for a nude matte lip with smoky eyes.")
    units = _units_for(text)
    parent, g, r_day, r_night = units
    assert parent["unit_id"] == "ZZ02_h0002" and parent["text"] == text  # both 模式母段保留
    assert [u["unit_id"] for u in units[1:]] == ["ZZ02_h0002#r1", "ZZ02_h0002#r2", "ZZ02_h0002#r3"]
    for r in units[1:]:
        s, e = r["sub_range"]["start"], r["sub_range"]["end"]
        assert text[s:e] == r["text"] and r["chunk_id"] == "ZZ02_h0002" and r["links"]["parent"] == "ZZ02_h0002"
        assert r["text_sha256"] == su.sha256_text(r["text"]) and r["tokens_cl100k"] == len(r["text"].split())
    # v3：導言／通則自成 general 範圍（replace 模式母段不進檢索，通則不能遺失）
    assert g["text"] == "You can pair matte lipstick with almost anything." and g["sub_range"]["kind"] == "general"
    assert r_day["text"].startswith("For a fresh, daytime look") and r_day["text"].endswith("This keeps it effortless.")
    assert r_day["conditions"] == ["For a fresh, daytime look"] and r_day["sub_range"]["kind"] == "scenario"
    assert r_night["text"].startswith("Or, for nighttime") and "for nighttime" in r_night["conditions"]
    assert r_day["links"]["next"] == r_night["unit_id"] and r_night["links"]["prev"] == r_day["unit_id"]


def test_split_v3_general_sentences_not_bound_to_scenario():
    """情境句後面不以承接詞開頭的一般句另成通則範圍，條件（含無情境類別的 When 子句）不錯綁到前一個情境。"""
    text = ("When working on skin with warm undertones, warm colors flatter. However, cool blues can contrast. "
            "Skin with cool undertones is flattered with blue-toned red. There’s also a classic red for all undertones. "
            "When creating any type of make-up look, consider color harmony. Trust your own judgment.")
    ranges = su.split_ranges(text, su.explicit_conditions(text))
    kinds = [(k, text[s:e][:25], c) for s, e, c, k, _ in ranges]
    assert [k for k, *_ in kinds] == ["scenario", "scenario", "general"]  # 連續的一般句合成同一個通則範圍
    assert text[ranges[0][0]:ranges[0][1]].endswith("However, cool blues can contrast.")  # 承接詞併入
    assert text[ranges[2][0]:ranges[2][1]].startswith("There’s also")
    assert ranges[2][2] == ["When creating any type of make-up look"]  # 不綁到 cool
    assert ranges[1][2] == ["with cool undertones"]


def test_split_v3_abbreviation_not_sentence_boundary():
    text = "This is color theory (see M.U.I. Issue 3) at work. If you have warm skin, opt for corals."
    assert su.sentences(text) == [(0, 50), (51, len(text))]


def test_split_v3_alternative_branches_marked_not_cut():
    text = ("If you’ve dialled up your blush or eye makeup, try a softer lipstick. Whether you’re matching a brown "
            "smokey eye with a nude lipstick or mirroring the magic of a red dress with a classic red lip, take cues "
            "from your look.")
    ranges = su.split_ranges(text, su.explicit_conditions(text))
    assert len(ranges) == 2
    s, e, conds, kind, alts = ranges[1]
    assert text[s:e].startswith("Whether") and text[s:e].endswith("take cues from your look.")  # 整句不截斷
    assert [a["conditions"] for a in alts] == [["matching a brown smokey eye"], ["mirroring the magic of a red dress"]]
    assert text[alts[1]["start"]:alts[1]["end"]].startswith("or mirroring")


def test_compatible_scenes_and_single_scene_not_split():
    office = ("To make the look work for the office, trade your lipstick for a sheer gloss. "
              "This look is perfect for daytime wear.")
    assert len(_units_for(office)) == 1  # 上班與白天相容
    bold = ("If you’re going bold with your matte lip, keep the rest light. Pairing a statement lip with subtle eye "
            "makeup keeps it polished.")
    assert len(_units_for(bold)) == 1
    assert su.explicit_conditions(bold) == ["If you’re going bold with your matte lip"]


def test_split_off_keeps_parent_only():
    text = "For a fresh, daytime look, wear pink. Or, for nighttime, wear nude."
    rec = _rec("ZZ02", "ZZ02_h0002", text)
    units = su.build_units(su.DocInputs("ZZ02", [rec], SYNTHETIC_HTML, {}, "e" * 64), split=False)
    assert [u["unit_id"] for u in units] == ["ZZ02_h0002"] and units[0]["tokens_cl100k"] is None


def test_chinese_product_and_shade_names():
    zh = "使用煙燻裸色眼影盤、終極濃密纖長睫毛膏和絕色霧感唇膏中的 「激裸色」色號，打造派對妝效。"
    found = su.product_mentions(zh, [])
    for name in ("煙燻裸色眼影盤", "絕色霧感唇膏", "激裸色"):
        assert name in found
    assert su.product_mentions("向您介紹 超持久霧感液態唇膏™ 10 種明豔唇色。", []) == ["超持久霧感液態唇膏™"]
    en = su.product_mentions("Try the Acme Colour Riche Satin Lipstick in Le Rouge Paris for bold lips.", [])
    assert "Le Rouge Paris" in en
    assert su.product_mentions("Pair it with my Lip Cheat Contour Duo in Tan or Deep.", []) == ["Lip Cheat Contour Duo"]


def test_d_format_manifest(tmp_path):
    out_dir = tmp_path / "support_units"
    out_dir.mkdir()
    f = out_dir / "support_units.jsonl"
    f.write_text('{"unit_id":"a"}\n\n{"unit_id":"b"}\n', encoding="utf-8")
    m = su.build_manifest(out_dir, [f], {"generator_commit": "x", "input_export_sha256": "y"})
    assert m["files"] == [{"path": "support_units.jsonl", "sha256": su.sha256_file(f), "bytes": f.stat().st_size,
                           "lines": 2}]
    (out_dir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    assert su.verify_manifest(out_dir / "manifest.json") == []


def test_replace_mode_drops_parent_only_for_split_chunks():
    split_text = "For a fresh, daytime look, wear pink. Or, for nighttime, wear nude."
    recs = [_rec("ZZ02", "ZZ02_h0002", split_text), _rec("ZZ02", "ZZ02_h0003", "Apply balm first.")]
    units = su.build_units(su.DocInputs("ZZ02", recs, SYNTHETIC_HTML, {}, "e" * 64), keep_split_parent=False)
    assert [u["unit_id"] for u in units] == ["ZZ02_h0002#r1", "ZZ02_h0002#r2", "ZZ02_h0003"]
    assert all(u["links"]["parent"] == "ZZ02_h0002" for u in units[:2])


# ---- contract-v1.1 search_text_rule：search_text 去掉〔原文術語〕段後＝normalize(text) ----

def test_normalize_search_matches_02_clean_rule(clean_mod):
    raw = "∆E  of the ∗sample −2\nﬁne  L∗a∗b∗"
    assert su.normalize_search(raw) == "ΔE of the *sample -2 fine L*a*b*"
    # 與 02_clean.build_search_text 同規則（沒有黏字可拆時完全相同）
    assert su.normalize_search(raw) == clean_mod.build_search_text(raw, __import__("collections").Counter())[0]


def test_every_unit_search_base_equals_normalized_text():
    text = "For a fresh, daytime look, wear the ∆-pink  shade. Or, for nighttime, wear ∗nude∗."
    recs = [_rec("ZZ02", "ZZ02_h0002", text, search=su.normalize_search(text))]
    units = su.build_units(su.DocInputs("ZZ02", recs, SYNTHETIC_HTML, {}, "e" * 64))
    assert len(units) == 3  # 母段＋2 個 #r
    for u in units:
        assert su.search_base(u["search_text"]) == su.normalize_search(u["text"]), u["unit_id"]


FROZEN_UNITS = Path(os.environ.get(
    "LIPMATCH_SUPPORT_UNITS",
    "data/review/parallel-repair-20261023/A/units_v2_replace_435fb3f/data/processed/support_units/"
    "support_units.jsonl"))


@pytest.mark.skipif(not FROZEN_UNITS.is_file(), reason="找不到支持單位檔（Git 外；可用 LIPMATCH_SUPPORT_UNITS 指定）")
def test_real_units_search_base_equals_normalized_text():
    units = [json.loads(line) for line in FROZEN_UNITS.read_text(encoding="utf-8").splitlines() if line.strip()]
    bad = [u["unit_id"] for u in units if su.search_base(u["search_text"]) != su.normalize_search(u["text"])]
    assert units and bad == []


def test_brand_variants_order_is_deterministic():
    """品牌名變體順序固定（原 set 迭代順序隨 PYTHONHASHSEED 變，同程式重跑 SHA 不同；2026-10-23 重建比對發現）。"""
    doc = {"manifest": {"title": "Tips | L’Oréal Paris"}}
    assert su.brand_names("X", doc, {"doc_type": "web（品牌）", "institution": "L'Oréal Paris Beauty Magazine"}) == \
        ["L’Oréal Paris", "L'Oréal Paris"]


# ---- 切片整修 F 版：長段切（#s）、參考文獻不進檢索、碎片併入（#m） ----

def _words(text: str) -> int:
    """測試用 token 計數：以空白分詞（真資料用 cl100k）。"""
    return len(text.split())


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(su, "LONG_SPLIT_OVER", 20)
    monkeypatch.setattr(su, "LONG_PIECE_MAX", 20)      # 每句 9 個詞：一片兩句、重疊一句
    monkeypatch.setattr(su, "LONG_PIECE_OVERLAP", 10)
    monkeypatch.setattr(su, "MERGE_MAX", 30)


def _pdf_doc(recs):
    return su.DocInputs("ZZ01", recs, SYNTHETIC_PDF, {"doc_type": "journal article"}, "e" * 64)


LONG = " ".join(f"Sentence number {i} keeps the measured lip colour stable." for i in range(1, 7))


def test_f_rules_off_by_default_and_need_token_counter():
    recs = [_rec("ZZ01", "ZZ01_p0001_b001", LONG)]
    assert [u["unit_id"] for u in su.build_units(_pdf_doc(recs), _words)] == ["ZZ01_p0001_b001"]
    with pytest.raises(ValueError):
        su.build_units(_pdf_doc(recs), long_split=True)


def test_long_split_pieces_within_limit_overlap_and_cover(small_limits):
    recs = [_rec("ZZ01", "ZZ01_p0001_b001", LONG, pages=[1]), _rec("ZZ01", "ZZ01_p0001_b002", "Short tail sentence here.")]
    units = su.build_units(_pdf_doc(recs), _words, keep_split_parent=False, long_split=True)
    pieces = [u for u in units if "#s" in u["unit_id"]]
    assert [u["unit_id"] for u in pieces] == [f"ZZ01_p0001_b001#s{n}" for n in range(1, len(pieces) + 1)]
    assert len(pieces) >= 3 and "ZZ01_p0001_b001" not in [u["unit_id"] for u in units]   # replace：母段不輸出
    for u in pieces:
        r = u["sub_range"]
        assert LONG[r["start"]:r["end"]] == u["text"] and _words(u["text"]) <= 20
        assert r["rule"] == su.LONG_SPLIT_VERSION and r["kind"] == "long_piece" and u["links"]["parent"] == "ZZ01_p0001_b001"
        assert su.search_base(u["search_text"]) == su.normalize_search(u["text"])
        assert u["text"].endswith(".")                                                    # 切在句界
    ranges = [(u["sub_range"]["start"], u["sub_range"]["end"]) for u in pieces]
    assert ranges[0][0] == 0 and ranges[-1][1] == len(LONG)
    assert all(b[0] < a[1] for a, b in zip(ranges, ranges[1:]))                           # 相鄰重疊、沒有缺口


def test_long_split_skips_tables_and_multi_scenario_ranges(small_limits):
    table = _rec("ZZ01", "ZZ01_p0002_b001", LONG, block_type="table")
    units = su.build_units(_pdf_doc([table]), _words, keep_split_parent=False, long_split=True)
    assert [u["unit_id"] for u in units] == ["ZZ01_p0002_b001"]


def test_long_piece_breaks_oversized_sentence_at_spaces():
    text = " ".join(f"w{i}" for i in range(30)) + "."
    pieces = su.long_pieces(text, _words, 10, 2)
    assert all(_words(text[a:b]) <= 10 for a, b in pieces) and pieces[-1][1] == len(text)


def test_reference_list_excluded_not_split(small_limits):
    refs = " ".join(f"Smith, J. A. ({2000 + i}). Title {i}. J. Col. 1, 1–9. doi: 10.1/{i}" for i in range(8))
    recs = [_rec("ZZ01", "ZZ01_p0013_b002", refs, section="Publisher’s note"),
            _rec("ZZ01", "ZZ01_p0013_b003", "Lipstick colour shifts perceived skin lightness in daylight.")]
    units = su.build_units(_pdf_doc(recs), _words, long_split=True, exclude_references=True)
    assert units[0]["unit_id"] == "ZZ01_p0013_b002" and units[0]["retrieval"]["exclude"] is True
    assert units[0]["retrieval"]["rule"] == su.REFERENCE_RULE and "retrieval" not in units[1]
    assert not su.is_reference_list(recs[1]["display_text"])


def test_merge_captions_and_list_items_into_neighbour(small_limits):
    recs = [_rec("ZZ01", "ZZ01_p0003_b001", "Choose a shade that suits the occasion:", pages=[3]),
            _rec("ZZ01", "ZZ01_p0003_b002", "• soft pink for daytime", block_type="list_item", pages=[3]),
            _rec("ZZ01", "ZZ01_p0004_b001", "• deep berry for evenings", block_type="list_item", pages=[4]),
            _rec("ZZ01", "ZZ01_p0004_b002", "Figure 2: Shade chart", block_type="figure_caption", pages=[4]),
            _rec("ZZ01", "ZZ01_p0004_b003", "A new section starts with a full sentence here.", section="T", pages=[4])]
    units = su.build_units(_pdf_doc(recs), _words, merge_fragments=True)
    assert [u["unit_id"] for u in units] == ["ZZ01_p0003_b001#m", "ZZ01_p0004_b003"]   # 換節不併
    m = units[0]
    assert m["chunk_id"] == "ZZ01_p0003_b001" and m["merge_range"]["main"] == "ZZ01_p0003_b001"
    assert m["merge_range"]["members"] == [r["block_id"] for r in recs[:4]]
    assert m["text"] == "\n\n".join(r["display_text"] for r in recs[:4])
    assert m["locator_primary"]["pages"] == [3, 4] and m["locator_primary"]["text"] == "p.3–4"
    assert su.search_base(m["search_text"]) == su.normalize_search(m["text"])
    assert m["links"]["next"] == "ZZ01_p0004_b003" and units[1]["links"]["prev"] == "ZZ01_p0003_b001#m"


def test_merge_section_head_fragment_goes_forward_and_respects_limit(small_limits):
    recs = [_rec("ZZ01", "ZZ01_p0005_b001", "Table 1: Mean values", block_type="table_caption"),
            _rec("ZZ01", "ZZ01_p0005_b002", "The values were measured under daylight conditions."),
            _rec("ZZ01", "ZZ01_p0005_b003", " ".join(["word"] * 29) + ".")]
    units = su.build_units(_pdf_doc(recs), _words, merge_fragments=True)
    assert units[0]["unit_id"] == "ZZ01_p0005_b002#m"
    assert units[0]["merge_range"]["members"] == ["ZZ01_p0005_b001", "ZZ01_p0005_b002"]
    assert units[1]["unit_id"] == "ZZ01_p0005_b003" and "merge_range" not in units[1]


def test_merge_never_touches_sub_ranges_or_excluded(small_limits):
    split_text = "For a fresh, daytime look, wear pink. Or, for nighttime, wear nude."
    recs = [_rec("ZZ02", "ZZ02_h0002", split_text), _rec("ZZ02", "ZZ02_h0003", "• gloss", block_type="list_item")]
    units = su.build_units(su.DocInputs("ZZ02", recs, SYNTHETIC_HTML, {}, "e" * 64), _words,
                           keep_split_parent=False, merge_fragments=True)
    assert [u["unit_id"] for u in units] == ["ZZ02_h0002#r1", "ZZ02_h0002#r2", "ZZ02_h0003"]

# ---- 長段切重疊 10～20%（long_split-v1-ov10-20，老師的切片原則 2） ----

def _overlap_ratios(text, pieces):
    return [max(0, a[1] - b[0]) and _words(text[b[0]:a[1]]) / _words(text[a[0]:a[1]]) for a, b in zip(pieces, pieces[1:])]


def test_overlap_pieces_within_10_to_20_percent_of_previous():
    text = " ".join(f"Short sentence {i} here." for i in range(1, 41))       # 每句 4 詞
    pieces = su.long_pieces_overlap(text, _words, 40, 0.10, 0.20, 0.15)
    assert len(pieces) >= 4 and pieces[0][0] == 0 and pieces[-1][1] == len(text)
    assert all(_words(text[a:b]) <= 40 for a, b in pieces)
    assert all(0.10 <= r <= 0.20 for r in _overlap_ratios(text, pieces))
    assert all(text[b[0]:b[0] + 5] == "Short" for b in pieces[1:])          # 句首開始


def test_overlap_falls_back_to_word_boundary_when_sentences_too_long():
    text = " ".join(f"This long sentence number {i} has exactly twelve words in it today ok." for i in range(8))
    pieces = su.long_pieces_overlap(text, _words, 30, 0.10, 0.20, 0.15)
    ratios = _overlap_ratios(text, pieces)
    assert ratios and all(0.10 <= r <= 0.20 for r in ratios)                # 一句 12 詞超過 20%：改在詞的邊界取 15%
    assert all(_words(text[a:b]) <= 30 for a, b in pieces) and pieces[-1][1] == len(text)


def test_build_units_overlap_rule_recorded(small_limits):
    recs = [_rec("ZZ01", "ZZ01_p0001_b001", LONG, pages=[1])]
    units = su.build_units(_pdf_doc(recs), _words, keep_split_parent=False, long_split=True, long_split_overlap=True)
    assert units and all(u["sub_range"]["rule"] == su.LONG_SPLIT_OVERLAP_VERSION for u in units)
    rs = [(u["sub_range"]["start"], u["sub_range"]["end"]) for u in units]
    assert all(0.10 <= r <= 0.20 for r in _overlap_ratios(LONG, rs))

def test_overlap_chinese_text_without_spaces():
    """中文沒有空白：每個字都是詞的邊界，重疊仍落在 10～20%（TH014 曾出現 0% 與 57%）。"""
    text = "".join(f"第{i}句說明照明光源對膚色顯示的影響以及使用者的觀察結果。" for i in range(12))
    chars = lambda s: len(s.strip())
    pieces = su.long_pieces_overlap(text, chars, 80, 0.10, 0.20, 0.15)
    assert len(pieces) >= 3 and pieces[-1][1] == len(text)
    for a, b in zip(pieces, pieces[1:]):
        assert 0.10 <= (a[1] - b[0]) / chars(text[a[0]:a[1]]) <= 0.20

# ---- 已核准範圍單位（approved_range-v1）：只取核准範圍，不擴大 ----

def _parent(text, uid="ZZ02_h0010"):
    return {"unit_id": uid, "chunk_id": uid, "source_id": "ZZ02", "text": text, "text_sha256": su.sha256_text(text),
            "conditions": [], "product_mentions": [], "locator_primary": {"type": "html_dom", "block_ids": [uid]},
            "links": {"prev": None, "next": None, "parent": None, "corroborating_units": []}, "rules": {}}


def test_approved_spans_merge_whitespace_gaps_only():
    text = "Step one.\nStep two.\nBrand X is the best.\nStep three."
    assert su.approved_spans(text, [[0, 9], [10, 19]]) == [(0, 19)]                 # 只隔換行：合併
    assert su.approved_spans(text, [[0, 9], [41, 52]]) == [(0, 9), (41, 52)]        # 中間有未核准文字：分開


def test_approved_range_units_only_approved_text():
    text = "Use a non-latex sponge.\nBrand X lipstick is gorgeous.\nPress translucent powder."
    p = _parent(text)
    units = su.approved_range_units(p, [[0, 23], [54, 79]], "DEC-1")
    assert [u["unit_id"] for u in units] == ["ZZ02_h0010#a1", "ZZ02_h0010#a2"]
    assert [u["text"] for u in units] == ["Use a non-latex sponge.", "Press translucent powder."]
    assert all("Brand X" not in u["text"] for u in units)                           # 未核准的句子不在任何單位
    for u in units:
        r = u["sub_range"]
        assert text[r["start"]:r["end"]] == u["text"] and r["approval_decision_id"] == "DEC-1"
        assert u["links"]["parent"] == "ZZ02_h0010" and u["text_sha256"] == su.sha256_text(u["text"])
        assert su.search_base(u["search_text"]) == su.normalize_search(u["text"])


def test_approved_range_covering_whole_unit_needs_no_excerpt():
    text = "Line one.\nLine two."
    assert su.approved_range_units(_parent(text), [[0, 9], [10, 19]], "DEC-1") == []

