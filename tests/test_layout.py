"""版面測試：雙欄閱讀順序、並排標題的矩陣表、中文論文規則；以及單欄文件不受影響。

雙欄 fixture：tests/fixtures/synthetic_pdf.py::build_two_column_pdf（仿 Wiley 期刊版面）。
"""

from __future__ import annotations

import re

import pymupdf
import pytest

from tests.conftest import LEEDS_PDF, ROOT
from tests.fixtures.synthetic_pdf import MATRIX_CAPTION_LINES, MATRIX_HEADER, MATRIX_ROWS, TWO_COL_TITLE


# ---------------------------------------------------------------------------
# 雙欄
# ---------------------------------------------------------------------------


def test_two_column_detected(extract_mod, two_column_pdf):
    with pymupdf.open(two_column_pdf) as d:
        _, _, layout = extract_mod.read_pdf_lines(d[0])
    assert layout.split_x is not None and 291 < layout.split_x < 305   # 欄間空白帶裡


def test_columns_never_merged_into_one_line(two_column_extracted):
    for b in two_column_extracted.blocks:
        assert not (re.search(r"\bL\d\d\b", b.raw_text) and re.search(r"\bR\d\d\b", b.raw_text)), b.raw_text[:80]


def test_reading_order_left_then_right_with_spanning_breaks(two_column_extracted):
    """標題 → 上段左欄 → 上段右欄 → 跨欄表格 → 下段左欄 → 下段右欄。"""
    seq = []
    for b in sorted(two_column_extracted.blocks, key=lambda b: b.order):
        if b.block_type == "heading" and b.raw_text.strip() == TWO_COL_TITLE:
            seq.append("TITLE")
        elif b.block_type == "table":
            seq.append("TABLE")
        else:
            seq += re.findall(r"\b([LR]\d\d)\b", b.raw_text)
    expected = (["TITLE"] + [f"L{i:02d}" for i in range(1, 21)] + [f"R{i:02d}" for i in range(1, 21)]
                + ["TABLE"] + [f"L{i:02d}" for i in range(21, 27)] + [f"R{i:02d}" for i in range(21, 27)])
    assert seq == expected


def test_column_paragraphs_kept_whole(two_column_extracted):
    paras = [b for b in two_column_extracted.blocks if b.block_type == "paragraph"]
    starts = sorted(p.raw_text[:3] for p in paras)
    assert starts == ["L01", "L21", "R01", "R21"]


def test_side_caption_matrix_table(two_column_extracted):
    t = next(b for b in two_column_extracted.blocks if b.block_type == "table")
    assert t.table.detection_method == "pymupdf_words"
    assert t.table_data == [MATRIX_HEADER] + MATRIX_ROWS          # 對角線保留空值，沒有錯位
    assert t.table.column_names == MATRIX_HEADER
    assert t.table.row_labels == [r[0] for r in MATRIX_ROWS]
    assert t.table.caption == "TABLE 1 " + " ".join(MATRIX_CAPTION_LINES)   # 三行標題完整接上
    assert any("依單字位置拆開" in r for r in t.review_reasons)    # 表頭黏字被拆開，並留下紀錄
    assert not any(i.issue_type == "table_low_confidence" for i in two_column_extracted.issues)
    assert two_column_extracted.pages[0].text_coverage == 1.0


# ---------------------------------------------------------------------------
# 單欄文件不受雙欄邏輯影響
# ---------------------------------------------------------------------------


def test_single_column_synthetic_not_split(extract_mod, synthetic_pdf):
    with pymupdf.open(synthetic_pdf) as d:
        assert all(extract_mod.read_pdf_lines(p)[2].split_x is None for p in d)


def test_leeds_single_column_not_split(extract_mod):
    if not LEEDS_PDF.exists():
        pytest.skip("找不到 TH013 原始 PDF")
    with pymupdf.open(LEEDS_PDF) as d:
        assert [i + 1 for i in range(d.page_count) if extract_mod.read_pdf_lines(d[i])[2].split_x is not None] == []


# ---------------------------------------------------------------------------
# 中文論文規則（單元測試，不需要原始 PDF）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, level", [
    ("第一章 緒論", 1), ("第 3 章 實驗", 1), ("摘要", 1), ("參考文獻", 1), ("圖目錄", 1),
    ("1.1  研究背景與動機", 2), ("2.1.5 色溫", 3),
    ("0.977 0.043 -0.028", None),            # 數字列不是標題
])
def test_cjk_heading_levels(extract_mod, text, level):
    assert extract_mod.heading_level_for(text) == level


@pytest.mark.parametrize("text, is_table", [("表 2-1 常見燈源色溫", True), ("圖 4-2 膚色散佈圖", False),
                                            ("Table 4.1 Averaged", True), ("Figure 2.1 The", False)])
def test_cjk_and_english_captions(extract_mod, text, is_table):
    assert extract_mod.CAPTION_RE.match(text)
    assert bool(extract_mod.TABLE_CAPTION_RE.match(text)) == is_table


@pytest.mark.parametrize("text, ok", [("(2-5)", True), ("(3.1)", True), ("Equation 2.1", True),
                                      ("(1964)", False), ("(2008)", False)])
def test_equation_labels_exclude_years(extract_mod, text, ok):
    assert bool(extract_mod.EQUATION_LABEL_RE.match(text)) == ok


def test_word_bullet_is_not_garbled(extract_mod, clean_mod):
    assert extract_mod.PUA_BULLET_RE.match(" RGB 色度系統")
    assert not extract_mod.SUSPECT_GLYPH_RE.search(extract_mod.PUA_BULLET_RE.sub("", " RGB 色度系統"))
    assert extract_mod.SUSPECT_GLYPH_RE.search("在E*")                 # 行中的 Symbol 字型 Δ 仍要標記
    out, ops = clean_mod.normalize_pua_bullets(" RGB 色度系統\n內文")
    assert out == "• RGB 色度系統\n內文" and ops == ["normalized_pua_bullet"]


@pytest.mark.parametrize("text, garbled", [
    ("ℎ∗ = (𝑎∗)* + (𝑏∗)*", True),        # Leeds：上標 2 變成 '*'
    ("𝑋= # 𝑆(𝜆)", True),
    ("L*= (L*test - L*std.)", False),    # TH014：CIELAB 星號
])
def test_garbled_math_vs_cielab_star(extract_mod, text, garbled):
    assert bool(extract_mod.GARBLED_MATH_RE.search(text)) == garbled


@pytest.mark.parametrize("cell", ["3000 K", "1950-2250 K", "53.38*", "-3.12", "45.32", "12%"])
def test_numeric_cells_with_units(extract_mod, cell):
    assert extract_mod.is_numeric_cell(cell)


def test_cjk_eligibility_sections():
    from lipmatch.eligibility import BODY_START_RE, EXCLUDED_SECTION_RE
    for s in ["參考文獻", "目錄", "圖目錄", "表目錄", "誌謝", "References"]:
        assert EXCLUDED_SECTION_RE.match(s), s
    for s in ["第二章 文獻探討", "附錄"]:
        assert not EXCLUDED_SECTION_RE.match(s), s
    for s in ["摘要", "第一章 緒論", "Abstract", "Chapter 1. Introduction"]:
        assert BODY_START_RE.match(s), s


def test_sparse_table_accepted_but_chart_rejected(extract_mod):
    """事後檢定表（稀疏但文字都在格子裡）要接受；向量圖表（幾乎全空、字落在格外）要拒絕。"""
    rect = pymupdf.Rect(100, 100, 400, 300)
    # 仿 TH014 表 4-11：每個樣本只在 1～2 個子集欄有值，非空格約 40%
    sparse = [[None, "樣本", "子集", None, None, None], [None, "編號", "1", "2", "3", "4"],
              ["Scheffe", "P5", "3.10", "", "", ""], [None, "P6", "3.25", "3.25", "", ""],
              [None, "C3", "", "", "3.63", ""], [None, "C2", "", "", "", "4.24"]]
    texts = [c for r in sparse for c in r if c]
    words = [(150, 110 + 6 * i, 170, 114 + 6 * i, w) for i, w in enumerate(texts)]
    assert not extract_mod.table_quality(sparse)[0]                       # 非空格 < 50%
    cov = extract_mod.table_text_coverage(rect, words, sparse)
    assert extract_mod.sparse_table_ok(sparse, rect, words, cov)
    chart = [[None] * 6 for _ in range(5)]
    chart[1][2] = "3.48"
    assert not extract_mod.sparse_table_ok(chart, rect, words, extract_mod.table_text_coverage(rect, words, chart))
    assert not extract_mod.sparse_table_ok([[None] * 4] * 4, rect, [], 1.0)  # 範圍內沒有字（覆蓋率預設 1.0）也不行
