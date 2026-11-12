"""段落接續（SYNTHETIC）：被切斷的段落要在 RAG 匯出時合併成一筆，不可只剩半句。

對應 2026-10-15 整理首批審核片段時發現的問題：
- TH014 p.101 結論段含 Symbol 字型的 Δ（私有區字碼 U+F044），整段被當成字型對應失敗而送人工確認，
  05 匯出時鏈頭被排除，p.102 的續段只剩「部分，……」。
- AR012 摘要的灰底框被當成圖表，最後一行短句變成 figure_text，摘要停在「demonstrating its」。
- AR012 沒有書眉的頁面，正文第一行落在頁面上方 7% 內被當成頁首，換欄、換頁的段落少一行。
- AR012 未解析的表格（殘片、數字為主的表格本體、欄名）插在段落中間，續段接到表格上或把表格數值接進正文。
"""
from __future__ import annotations

import pymupdf
import pytest

from lipmatch.eligibility import classify_blocks
from lipmatch.sources import SourceInfo
from schemas import Document, SourceManifest, TextBlock
from tests.conftest import _load
from tests.fixtures.synthetic_pdf import (
    ABSTRACT_LAST_LINE, PAGE_ONE_LAST_RIGHT, PAGE_TWO_FIRST_LEFT, RUNNING_HEADER, SYMBOL_PROSE_TAIL,
    TABLE_PAGE_FIRST_RIGHT, TABLE_PAGE_LAST_LEFT, build_continuation_pdf,
)

SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")


@pytest.fixture(scope="module")
def export_mod():
    return _load("lipmatch_export", "05_export_rag_ready.py")


@pytest.fixture(scope="module")
def cont_doc(extract_mod, tmp_path_factory):
    pdf = build_continuation_pdf(tmp_path_factory.mktemp("continuation") / "continuation.pdf")
    return extract_mod.extract_pdf(pdf, "SYN_CONT")


@pytest.fixture(scope="module")
def cont_cleaned(clean_mod, cont_doc):
    return clean_mod.clean_document(cont_doc)


@pytest.fixture(scope="module")
def cont_records(export_mod, cont_cleaned, tmp_path_factory):
    import json
    path, _ = export_mod.export(cont_cleaned, SRC, tmp_path_factory.mktemp("rag"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _record_with(records, text):
    hits = [r for r in records if text in r["display_text"]]
    assert len(hits) == 1, f"{text!r} 出現在 {len(hits)} 筆紀錄"
    return hits[0]


def _dangling(records):
    """continues_from_block_id 指向沒有匯出的區塊 = 鏈頭被排除，這筆只剩半句。"""
    exported = {i for r in records for i in r["block_ids"]}
    return [r for r in records if r["continues_from_block_id"] and r["continues_from_block_id"] not in exported]


# ---------------------------------------------------------------------------
# Symbol 字型（TH014）
# ---------------------------------------------------------------------------

def test_symbol_font_private_use_chars_mapped(extract_mod):
    m = extract_mod.map_symbol_font
    assert m("E*94", "Symbol") == "ΔE*94"
    assert m(" P()", "SymbolMT") == "φ=∫ P(λ)"
    assert m("", "Symbol") == "•"                      # Symbol 字型的項目符號
    assert m("", "Wingdings") == ""              # Wingdings 的 ● 不是 λ，不動
    assert m("", "Symbol") == ""                 # 括號拼接零件沒有對應字元：保留，仍會被標記


class _FakePage:
    """只提供 read_pdf_lines 需要的部分：PDF 產生器無法寫出私有區字碼，用假頁面模擬 Word 轉出的 PDF。"""
    rect = pymupdf.Rect(0, 0, 595, 842)

    def __init__(self, spans):
        self._spans = spans

    def get_text(self, kind):
        return {"blocks": [{"type": 0, "lines": [{"dir": (1.0, 0.0), "spans": self._spans}]}]}


def test_symbol_delta_in_prose_is_not_garbled(extract_mod):
    span = lambda x0, x1, font, text: {"text": text, "font": font, "bbox": (x0, 100, x1, 112), "size": 12, "flags": 0}
    page = _FakePage([span(90, 300, "標楷體", "現」的部分，透過實驗結果，在"), span(300, 307, "Symbol", ""),
                      span(307, 330, "Times New Roman", "E*94 色差")])
    lines, _, _ = extract_mod.read_pdf_lines(page)
    assert lines[0].text == "現」的部分，透過實驗結果，在ΔE*94 色差"
    assert not extract_mod.SUSPECT_GLYPH_RE.search(lines[0].text)   # 不再被當成字型對應失敗
    assert not lines[0].symbol_formula


def test_symbol_prose_eligible_but_symbol_formula_needs_review(cont_cleaned):
    reasons = classify_blocks(cont_cleaned)
    prose = [b for b in cont_cleaned.blocks if SYMBOL_PROSE_TAIL[:20] in b.cleaned_text]
    assert len(prose) == 1 and reasons[prose[0].block_id] is None
    formula = [b for b in cont_cleaned.blocks if b.cleaned_text.startswith("f=Km")]
    assert len(formula) == 1 and formula[0].parse_status == "partial"
    assert any("Symbol 字型排版的公式" in r for r in formula[0].review_reasons)
    assert reasons[formula[0].block_id] == "needs_review"


# ---------------------------------------------------------------------------
# 摘要底色框（AR012 p.1）
# ---------------------------------------------------------------------------

def test_shaded_abstract_box_is_not_a_chart(cont_doc):
    p1 = [b for b in cont_doc.blocks if b.page_number == 1]
    assert not any(b.block_type in ("chart_region", "figure_text") for b in p1)


def test_abstract_last_short_line_merged(cont_records):
    r = _record_with(cont_records, ABSTRACT_LAST_LINE)
    assert r["display_text"].endswith("demonstrating its " + ABSTRACT_LAST_LINE)
    assert r["section"] == "ABSTRACT"


# ---------------------------------------------------------------------------
# 頁首區內的正文（AR012 p.2 等）
# ---------------------------------------------------------------------------

def test_running_header_with_gap_stays_header(cont_doc):
    hdr = [b for b in cont_doc.blocks if b.raw_text.strip() == RUNNING_HEADER]
    assert len(hdr) == 1 and hdr[0].block_type == "page_header"


def test_body_line_inside_top_margin_is_body(cont_doc):
    first = [b for b in cont_doc.blocks if PAGE_TWO_FIRST_LEFT in b.raw_text]
    assert len(first) == 1 and first[0].block_type == "paragraph"
    assert not any(b.block_type == "page_header" for b in cont_doc.blocks if b.page_number == 2)


def test_column_and_page_break_merged_into_one_record(cont_records):
    r = _record_with(cont_records, PAGE_ONE_LAST_RIGHT)
    assert PAGE_ONE_LAST_RIGHT + " " + PAGE_TWO_FIRST_LEFT in r["display_text"]
    assert r["pages"] == [1, 2] and len(r["block_ids"]) == 2
    assert not _dangling(cont_records)


# ---------------------------------------------------------------------------
# 跨頁鏈與未解析表格（05 匯出、02 連結）
# ---------------------------------------------------------------------------

def _blk(i, text, bt="paragraph", page=1, **kw):
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=page,
                     section="5.1 結論", raw_text=text, cleaned_text=text, search_text=text, **kw)


def _doc(blocks):
    m = SourceManifest(doc_id="t", source_path="x", source_type="pdf", sha256="0" * 64,
                       file_size_bytes=0, extractor="test")
    return Document(stage="cleaned", manifest=m, blocks=blocks)


def test_dangling_continuation_reported(export_mod, tmp_path):
    """鏈頭被排除（待確認）時，續段只剩半句：summary.md 要列出來，不可默默匯出。"""
    blocks = [
        _blk(0, "SYNTHETIC 結論段停在一半的句子，在照明光源", page=101, needs_review=True, review_reasons=["check"]),
        _blk(1, "部分，SYNTHETIC 續段。", page=102, continues_from_block_id="t_b000"),
    ]
    path, _ = export_mod.export(_doc(blocks), SRC, tmp_path)
    import json
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["block_id"] for r in export_mod.dangling_continuations(records)] == ["t_b001"]
    summary = (tmp_path / "t.summary.md").read_text(encoding="utf-8")
    assert "只剩半句，應為 0）：1 筆" in summary and "t_b001 ← t_b000" in summary


def test_cross_page_chinese_paragraph_merged(clean_mod, export_mod, tmp_path):
    """TH014 p.101→102 的形狀：前段停在「在照明光源」，頁尾頁碼，續段以「部分，」開頭。"""
    blocks = [
        _blk(0, "本 SYNTHETIC 論文在ΔE*94 色差平均值已經達到 1.5 以下，在數據分析結果顯示受測者偏好較高明度。在照明光源", page=101),
        _blk(1, "89", bt="page_footer", page=101, is_removed=True, removal_reason="page_footer:page_number"),
        _blk(2, "部分，其結果顯示受測者在 SYNTHETIC 色溫條件下沒有明顯差異。", page=102),
    ]
    assert clean_mod.link_cross_page_continuations(blocks) == 1
    assert blocks[2].continues_from_block_id == "t_b000"
    path, _ = export_mod.export(_doc(blocks), SRC, tmp_path)
    import json
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["block_ids"] == ["t_b000", "t_b002"] and records[0]["pages"] == [101, 102]
    assert "在照明光源部分，其結果" in records[0]["display_text"]
    assert not export_mod.dangling_continuations(records)


@pytest.mark.parametrize("reason_attr", ["TABLE_FRAGMENT_REASON", "TABLE_TEXT_REASON", "TABLE_TEXT_FRAGMENT_REASON"])
def test_table_reasons_shared_between_extract_and_clean(extract_mod, clean_mod, reason_attr):
    assert getattr(extract_mod, reason_attr) in clean_mod.TABLE_FRAGMENT_REASONS


@pytest.mark.parametrize("reason_attr", ["TABLE_FRAGMENT_REASON", "TABLE_TEXT_REASON", "TABLE_TEXT_FRAGMENT_REASON"])
def test_continuation_skips_unparsed_table_pieces(extract_mod, clean_mod, reason_attr):
    """AR012 p.10→11：「A pair of trend lines was fitted to」→ 未解析表格 →「each scatter plot …」。"""
    reason = getattr(extract_mod, reason_attr)
    blocks = [
        _blk(0, "A pair of SYNTHETIC trend lines was fitted to", page=10),
        _blk(1, "Designated red", bt="label", page=11, needs_review=True, review_reasons=[reason]),
        _blk(2, "0.6", page=11, needs_review=True, review_reasons=[reason]),
        _blk(3, "each scatter plot to identify SYNTHETIC patterns.", page=11),
    ]
    clean_mod.link_cross_page_continuations(blocks)
    assert blocks[3].continues_from_block_id == "t_b000"
    assert blocks[2].continues_from_block_id is None


def test_unparsed_table_body_flagged_and_skipped(cont_cleaned, cont_records):
    """AR012 p.4 的形狀：左欄「The study」→ 表格標題、欄名、未解析的表格本體 → 右欄頂端「plans were approved」。"""
    rows = [b for b in cont_cleaned.blocks if b.page_number == 3 and b.cleaned_text.startswith("Dark red 25")]
    assert len(rows) == 1 and rows[0].needs_review
    assert classify_blocks(cont_cleaned)[rows[0].block_id] == "needs_review"
    r = _record_with(cont_records, TABLE_PAGE_FIRST_RIGHT)
    assert TABLE_PAGE_LAST_LEFT + " " + TABLE_PAGE_FIRST_RIGHT in r["display_text"]
    assert "Dark red" not in r["display_text"] and "Designated red" not in r["display_text"]


@pytest.mark.parametrize("raw, expected", [
    # AR012 表 3：名稱、數字各占一行
    ("Dark purplish red\n11\nDark red\n25\nDeep red\n1\nDusky red\n38\nLight red\n2\nWeak red\n36", True),
    # AR012 附錄 A1：數字為主
    ("0.7 −6.5 60 aC4L1\n0.7 −6.5 49.1 aC4L2\n0.7 −6.5 43.1 aC4L3\n0.7 −4 60 aC3L1\n0.7 −4 49.1 aC3L2", True),
    # 每行以數字結尾
    ("Cool 7\nWarm 35\nTotal 42\nNeutral 3\nMixed 11", True),
    # 一般正文（含少量數字）不算
    ("We collected 657 lip makeup samples and measured\nthe L*, a*, and b* values of the CIE1976 space\n"
     "with a spectrophotometer under a D65 illuminant\nat a 10° viewing condition; each sample was\nmeasured three times.", False),
    ("Short\n12\nlist", False),    # 行數不夠
])
def test_table_like_text(extract_mod, raw, expected):
    assert extract_mod.is_table_like_text(raw) is expected
