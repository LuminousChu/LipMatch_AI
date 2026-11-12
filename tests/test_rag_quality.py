"""RAG 內容品質：非知識內容排除、被切斷的段落連結與合併、圖說不等於數據、編號引用歸屬。"""

from __future__ import annotations

import json

import pytest

from lipmatch.citations import find_numeric_citations, parse_reference_list
from lipmatch.eligibility import classify_blocks
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import ROOT, _load

CHARTON_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR015_*.pdf"))), None)


@pytest.fixture(scope="module")
def export_mod():
    return _load("lipmatch_export", "05_export_rag_ready.py")


def _blk(i, text, bt="paragraph", page=1, section="RESULTS", **kw):
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=page,
                     section=section, raw_text=text, cleaned_text=text, search_text=text,
                     heading_level=2 if bt == "heading" else None, **kw)


def _doc(blocks):
    m = SourceManifest(doc_id="t", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    return Document(stage="cleaned", manifest=m, blocks=blocks)


# ---------------------------------------------------------------------------
# 編號引用
# ---------------------------------------------------------------------------

REFS = parse_reference_list(["1. Schaffer SE. Reading our lips. 2019.", "2. Ogilvie M. Lipstick. 2011.",
                             "6. Engasser PG. Lip cosmetics. 2000."] + [f"{n}. Author{n}. Title." for n in range(30, 40)])


def test_parse_reference_list_both_formats():
    assert REFS[1].startswith("Schaffer SE") and REFS[6].startswith("Engasser")
    zh = parse_reference_list(["[1] 遠見雜誌網站，舊照明科技 [2] 日本資生堂網站，Pick up 3", "[12] M. Doi, N. Tanaka"])
    assert zh == {1: "遠見雜誌網站，舊照明科技", 2: "日本資生堂網站，Pick up 3", 12: "M. Doi, N. Tanaka"}


@pytest.mark.parametrize("text, expected_numbers", [
    ("red stones [1, 2]. Lip makeup", [1, 2]),
    ("vermillion height [30, 36–39]. Regarding", [30, 36, 37, 38, 39]),
    ("view angle of 10° [31].", [31]),
    ("RGB range [0, 255] was used", []),          # 不在參考文獻清單 → 不是引用
    ("matrix [2, 99]", []),                        # 只要有一個編號不在清單就整組不算
    ("no brackets here", []),
])
def test_numeric_citations_resolved_against_reference_list(text, expected_numbers):
    found = find_numeric_citations(text, REFS)
    assert [int(c[1:c.index("]")]) for c in found] == expected_numbers
    assert all(" " in c for c in found)            # "[n] 條目開頭"，看得出引用的是哪篇


def test_no_reference_list_means_no_numeric_citations():
    assert find_numeric_citations("as shown [6].", {}) == []


# ---------------------------------------------------------------------------
# 被切斷的段落
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prev, cur, linked", [
    ("recruited by self-declaration of the", "origin of the parents and grandparents.", True),
    ("The panel consisted of 514 women.", "Their natural lip colour was measured.", False),   # 前段已結束
    ("the variance was compared between each ethnic group", "For three parameters, …", False),  # 下一段大寫開頭
    ("用 LED 路燈、交通號誌燈及家用檯燈等，LED 的發光用途未來將廣泛使用，則是可預期的情", "況，同時其優點也是", True),
    ("指導教授：陳鴻興博士", "中華民國一百零三年一月十五日", False),                         # 封面短行
])
def test_continuation_rule(clean_mod, prev, cur, linked):
    assert clean_mod._is_continuation(prev, cur) == linked


def test_continuation_skips_figures_and_stops_at_headings(clean_mod):
    blocks = [
        _blk(0, "Lip colour was measured with a device calibrated for", page=4),
        _blk(1, "FIGURE 1 Regions of interest.", bt="figure_caption", page=4),
        _blk(2, "colorimetric measurement under D65.", page=5),          # 跳過圖說接回第 0 段
        _blk(3, "Statistics", bt="heading", page=5),
        _blk(4, "variance was compared", page=5),                        # 標題後不接
    ]
    clean_mod.link_cross_page_continuations(blocks)
    assert blocks[2].continues_from_block_id == "t_b000"
    assert blocks[4].continues_from_block_id is None


def test_export_merges_chain_and_keeps_traceability(export_mod):
    from lipmatch.sources import SourceInfo
    blocks = [_blk(0, "The panel was recruited by self-declaration of the", page=3, section="METHODS"),
              _blk(1, "origin of the parents.", page=4, section="METHODS", continues_from_block_id="t_b000")]
    doc = _doc(blocks)
    reasons = classify_blocks(doc)
    chains = export_mod.build_chains(doc, reasons)
    assert len(chains) == 1
    src = SourceInfo(title="T", authors=["A"], author_citation="Charton, Z., et al.", year=2026, verified_from="p.1")
    rec = export_mod.to_record(chains[0], src, {b.block_id: b for b in blocks}, reasons)
    assert rec["display_text"] == "The panel was recruited by self-declaration of the origin of the parents."
    assert rec["block_ids"] == ["t_b000", "t_b001"] and rec["pages"] == [3, 4]
    assert rec["citation"] == "(Charton, 2026, pp.3–4)"
    assert rec["attribution"] == "no_citation_marker"


def test_chain_breaks_when_middle_part_excluded(export_mod):
    blocks = [_blk(0, "first part of the", page=3),
              _blk(1, "second part that needs", page=3, continues_from_block_id="t_b000",
                   needs_review=True, review_reasons=["check"]),
              _blk(2, "review and a third part.", page=4, continues_from_block_id="t_b001")]
    doc = _doc(blocks)
    chains = export_mod.build_chains(doc, classify_blocks(doc))
    assert [[b.block_id for b in c] for c in chains] == [["t_b000"], ["t_b002"]]


# ---------------------------------------------------------------------------
# 圖說不等於數據
# ---------------------------------------------------------------------------


def test_caption_data_status(export_mod):
    fig_region = TextBlock(doc_id="t", block_id="t_b001", block_type="image_region", order=1, page_number=7,
                           raw_text="", parse_status="failed", failure_reason="image", needs_review=True,
                           review_reasons=["image"], linked_block_ids=["t_b000"], cleaned_text="", search_text="")
    caption = _blk(0, "FIGURE 4 Global lip colour chart of 53 tones.", bt="figure_caption", page=7,
                   linked_block_ids=["t_b001"])
    doc = _doc([caption, fig_region])
    reasons = classify_blocks(doc)
    by_id = {b.block_id: b for b in doc.blocks}
    assert export_mod.data_status(caption, by_id, reasons) == "not_extracted"
    rec = export_mod.to_record([caption], _src(), by_id, reasons)
    assert "不可視為已取得完整數據" in rec["data_note"]


def _src():
    from lipmatch.sources import SourceInfo
    return SourceInfo(title="T", authors=["A"], author_citation="A, B.", year=2026, verified_from="p.1")


# ---------------------------------------------------------------------------
# 非知識內容
# ---------------------------------------------------------------------------


def test_boilerplate_and_keywords_excluded_but_research_text_kept():
    long_research = ("We measured lip colour under a licensed D65 light source. " * 20).strip()  # > 800 字、提到 license
    doc = _doc([
        _blk(0, "Abstract", bt="heading", section="Abstract"),
        _blk(1, "Lips play a key role in makeup.", section="Abstract"),
        _blk(2, "K E Y W O R D S", bt="label", section="Abstract"),
        _blk(3, "colour cosmetics, ethnic diversity, lip colour", section="Abstract"),
        _blk(4, "This is an open access article under the terms of the Creative Commons License. © 2025 The Authors.",
             section="Abstract"),
        _blk(5, "關鍵詞：膚色，數位彩妝，白光 LED", section="摘要"),
        _blk(6, "Correspondence: Zoé Charton, LVMH Recherche. Email: someone@example.com", section="Abstract"),
        _blk(7, long_research, section="METHODS"),
    ])
    r = classify_blocks(doc)
    assert r["t_b001"] is None and r["t_b007"] is None
    assert r["t_b003"] == "keywords" and r["t_b005"] == "keywords"
    assert r["t_b004"] == "boilerplate" and r["t_b006"] == "boilerplate"


def test_plain_subheading_rule(extract_mod):
    import pymupdf
    line = lambda text, size: extract_mod.Line(text=text, bbox=pymupdf.Rect(46, 0, 200, 14), size=size,
                                               bold_all=False, first_bold=False)
    assert extract_mod._is_plain_subheading([line("Data acquisition device", 12.0)], 9.5)
    assert not extract_mod._is_plain_subheading([line("The table above has demonstrated the", 14.0)] * 2, 12.0)
    assert not extract_mod._is_plain_subheading([line("國立台灣科技大學", 22.0)], 12.0)       # 封面特大字
    assert not extract_mod._is_plain_subheading([line("|   Héloïse Vergnaud1", 12.0)], 9.5)  # 作者列
    assert not extract_mod._is_plain_subheading([line("test was 5%.", 12.0)], 9.5)          # 有句尾標點


# ---------------------------------------------------------------------------
# 真實文件：AR015（Wiley 雙欄）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def charton_records(extract_mod, clean_mod, export_mod):
    if CHARTON_PDF is None:
        pytest.skip("找不到 CHARTON 論文 PDF")
    doc = clean_mod.clean_document(extract_mod.extract_pdf(CHARTON_PDF, "AR015"))
    reasons = classify_blocks(doc)
    by_id = {b.block_id: b for b in doc.blocks}
    from lipmatch.sources import load_sources
    src = load_sources()["AR015"]
    return doc, [export_mod.to_record(c, src, by_id, reasons) for c in export_mod.build_chains(doc, reasons)]


def test_charton_no_fragment_or_noise_records(charton_records):
    _, records = charton_records
    texts = [r["display_text"] for r in records]
    assert all(len(t) >= 40 for t in texts), [t for t in texts if len(t) < 40]
    assert not any("open access article" in t or t.startswith("colour cosmetics, ethnic") for t in texts)


def test_charton_caption_tails_joined_and_flagged(charton_records):
    _, records = charton_records
    fig4 = next(r for r in records if r["display_text"].startswith("FIGURE 4"))
    assert fig4["display_text"].endswith("lightness).") and fig4["data_status"] == "not_extracted"
    table1 = next(r for r in records if r["display_text"].startswith("TABLE 1"))
    assert table1["data_status"] in ("extracted", "extracted_pending_review")


def test_charton_numeric_citations_and_subheadings(charton_records):
    doc, records = charton_records
    assert any(c.startswith("[31] ") for r in records for c in r["cited_works"])
    assert any(r["attribution"] == "cites_other_work" for r in records)
    sections = {b.section for b in doc.blocks if b.section}
    assert "MATERIALS AND METHODS > Measurement > Data acquisition device" in sections
    assert not any("­" in s for s in sections)           # 章節路徑沒有軟連字號
    assert any(len(r["block_ids"]) > 1 for r in records)        # 被切斷的段落已合併
