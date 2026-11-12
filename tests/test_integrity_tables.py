"""資料完整性修復第三批（2026-10-17）：表格標題續行、附錄表標題、無格線表格的表頭與結尾、
標題與表格的對應、表格不完整時的殘片保護、續表標記。

對應問題：
- AR012 p.7／11／12／13／15：跨欄表格標題的第二行落在頁面上緣 7% 內，被當成頁首，標題只剩半句。
- AR012 附錄「TABLE A1／A2」不符合「Table＋數字」，不被認成表格標題。
- AR012 Table 5、6、A1：表頭第一列沒有欄距（「Mean value (SD)」「Undertone Brown …」「Undertone」），偵測在第一列中斷。
- AR012 Table 5：表格下方 26pt 接雙欄正文，左右欄的空隙被當成欄距，正文被收進表格。
- AR012 p.13：Table 7 的表身就在 Table 8 標題上方 22pt，Table 8 被誤判為「已有表格」而略過。
- AR001 Table 1／2：表格橫跨整頁但標題只在左欄，表身被欄位分界截斷；右半的數值原本會被當成正文匯出。
- 「(Continued)」「(Continues)」續表標記在表格解析成功後被當成正文匯出。

合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。
"""
from __future__ import annotations

import json

import pymupdf
import pytest

from lipmatch.eligibility import classify_blocks
from lipmatch.sources import SourceInfo
from schemas import TextBlock
from tests.conftest import ROOT, _load

AR012_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR012_*.pdf"))), None)
AR001_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR001_*.pdf"))), None)
SRC = SourceInfo(title="SYNTHETIC", authors=["Tester"], author_citation="Tester", year=2026, verified_from="synthetic")


def _w(x0, y0, x1, y1, text):
    return (x0, y0, x1, y1, text, 0, 0, 0)


def _line(extract_mod, text, x0, y0, x1, size=8.0):
    return extract_mod.Line(text=text, bbox=pymupdf.Rect(x0, y0, x1, y0 + 11), size=size,
                            bold_all=False, first_bold=True, col=-1)


# ---------------------------------------------------------------------------
# 標題
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, is_table", [
    ("TABLE A1 | Data of lip makeup color system inventory.", True),
    ("Table S2. SYNTHETIC supplementary values", True),
    ("Figure S3: SYNTHETIC panel", False),
    ("Table 4.1 SYNTHETIC", True),
])
def test_caption_with_letter_prefix(extract_mod, text, is_table):
    assert extract_mod.CAPTION_RE.match(text)
    assert bool(extract_mod.TABLE_CAPTION_RE.match(text)) is is_table


def test_caption_second_line_in_top_margin(extract_mod):
    """AR012 p.7：標題第二行緊貼第一行、左緣對齊、字級相同 → 是標題續行，不是頁首。"""
    cap = _line(extract_mod, "TABLE 4 | SYNTHETIC caption first line ends with the visual", 45, 31, 552)
    tail = _line(extract_mod, "assessment results differed, SYNTHETIC.", 45, 42, 521)
    assert extract_mod.caption_continuation_lines([cap, tail], 782.4) == {id(tail)}


@pytest.mark.parametrize("x0, y0, size", [
    (300, 32, 8.0),     # 緊貼但左緣不對齊（書眉在右邊）
    (45, 40, 8.0),      # 左緣對齊但與標題之間有空白（間距 9pt > 0.5 × 字級）
    (45, 32, 10.0),     # 緊貼、對齊，但字級不同
])
def test_running_header_not_caption_tail(extract_mod, x0, y0, size):
    cap = _line(extract_mod, "TABLE 4 | SYNTHETIC caption", 45, 20, 552)
    hdr = _line(extract_mod, "SYNTHETIC JOURNAL RUNNING HEADER", x0, y0, x0 + 150, size=size)
    assert extract_mod.caption_continuation_lines([cap, hdr], 782.4) == set()


# ---------------------------------------------------------------------------
# 無格線表格：表頭、結尾
# ---------------------------------------------------------------------------

CAP = pymupdf.Rect(45, 31, 291, 54)
HEADER = [_w(50, 60, 100, 73, "Undertone"), _w(103, 60, 135, 73, "Brown"), _w(138, 60, 175, 73, "Orange"),
          _w(178, 60, 200, 73, "Pink")]     # 字距小，沒有欄距
DATA = [[_w(50, 77 + 17 * i, 75, 90 + 17 * i, n), _w(119, 77 + 17 * i, 130, 90 + 17 * i, a),
         _w(161, 77 + 17 * i, 172, 90 + 17 * i, b), _w(199, 77 + 17 * i, 210, 90 + 17 * i, c)]
        for i, (n, a, b, c) in enumerate([("Cool", "7", "1", "14"), ("Warm", "35", "38", "14"), ("Total", "42", "39", "28")])]
PAGE = pymupdf.Rect(0, 0, 595, 782)


def test_header_without_column_gap_kept(extract_mod):
    rows = extract_mod.find_text_table_rows(CAP, HEADER + sum(DATA, []), 9.0, PAGE)
    assert len(rows) == 4 and rows[0][1][0][4] == "Undertone"


def test_header_alone_is_not_a_table(extract_mod):
    """標題下只有沒有欄距的文字（正文）：暫存的表頭不會單獨變成表格。"""
    body = [_w(50, 60 + 12 * i, 280, 71 + 12 * i, f"SYNTHETIC body line {i}") for i in range(4)]
    assert extract_mod.find_text_table_rows(CAP, body, 9.0, PAGE) == []


def test_header_wider_than_table_dropped(extract_mod):
    """「表頭」比表格寬很多（其實是正文行）→ 不收進表格。"""
    wide = [_w(30, 60, 290, 73, "SYNTHETIC wide sentence that is not a header")]
    rows = extract_mod.find_text_table_rows(CAP, wide + sum(DATA, []), 9.0, PAGE)
    assert len(rows) == 3 and all(w[4] != "SYNTHETIC" for _, ws in rows for w in ws)


def test_two_column_prose_below_table_not_absorbed(extract_mod):
    """AR012 Table 5：表格結束後 26pt 是雙欄正文，左右欄之間的空隙不可當成欄距。"""
    last_y1 = 90 + 17 * 2
    prose = [_w(45, last_y1 + 26, 291, last_y1 + 38, "each scatter plot to identify SYNTHETIC patterns"),
             _w(306, last_y1 + 26, 552, last_y1 + 38, "likely contributes to the SYNTHETIC differentiation")]
    rows = extract_mod.find_text_table_rows(pymupdf.Rect(45, 31, 552, 54), sum(DATA, []) + prose, 9.0, PAGE)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 表格外殘片、續表標記
# ---------------------------------------------------------------------------

def _tb(i, text, bbox, bt="paragraph"):
    extra = {"table_data": [["L*", "a*"]]} if bt == "table" else {}
    return TextBlock(doc_id="t", block_id=f"t_b{i:03d}", block_type=bt, order=i, page_number=6, bbox=bbox,
                     raw_text=text, **extra)


def test_side_fragments_flagged(extract_mod):
    table = _tb(0, "L* | a*", (52, 454, 300, 559), bt="table")
    numeric = _tb(1, "1.815 21.948 c 2.513 31.829 a 3.876 4.064 23.684", (333, 508, 522, 515))   # >= 40 字，靠數字比例判斷
    short = _tb(2, "11.546 b", (293, 508, 322, 515))
    prose = _tb(3, "SYNTHETIC right column prose that is long enough to be normal text and not a table cell.",
                (307, 460, 550, 540))
    caption = _tb(4, "FIGURE 7", (320, 470, 360, 480))
    below = _tb(5, "4.235", (293, 600, 312, 610))
    got = extract_mod.table_side_fragments(table, [table, numeric, short, prose, caption, below])
    assert [b.block_id for b in got] == ["t_b001", "t_b002"]


@pytest.mark.parametrize("text, expected", [
    ("(Continued)", True), ("(Continues)", True), ("(Continues) (Continues)", True),
    ("Results (continued) from Table 2", False), ("Continued", False),
])
def test_continued_marker(extract_mod, text, expected):
    assert bool(extract_mod.CONTINUED_MARKER_RE.match(text)) is expected


# ---------------------------------------------------------------------------
# 真實文件
# ---------------------------------------------------------------------------

PLANES = {0.7: "a", 0.9: "b", 1.08: "c", 1.19: "d", 1.34: "e", 2.0: "f"}
UTS = {-6.5: "C4", -4: "C3", -2: "C2", 0.25: "C1", 2.5: "W1", 4.75: "W2", 6.75: "W3", 20: "W4"}
LVS = {60: "L1", 49.1: "L2", 43.1: "L3", 37.9: "L4"}


def _num(s):
    try:
        return float(str(s).replace("−", "-"))
    except (TypeError, ValueError):
        return None


@pytest.fixture(scope="module")
def ar012_cleaned(extract_mod, clean_mod):
    if AR012_PDF is None:
        pytest.skip("找不到 AR012 原始 PDF")
    return clean_mod.clean_document(extract_mod.extract_pdf(AR012_PDF, "AR012"))


def _tables(doc):
    return [b for b in doc.blocks if b.block_type == "table"]


def test_ar012_captions_complete(ar012_cleaned):
    caps = {b.page_number: " ".join(b.cleaned_text.split()) for b in ar012_cleaned.blocks if b.block_type == "table_caption"}
    assert caps[7].endswith("subdivide some samples beyond the simple classification into the red group further.")
    assert caps[12].endswith("each color group determined in Study I.")
    assert not any(b.block_type == "page_header" and not b.is_removed for b in ar012_cleaned.blocks
                   if b.page_number in (7, 11, 12, 13, 15))


def test_ar012_appendix_a1_codes_consistent(ar012_cleaned):
    """附錄 A1 每列的代碼都能由同列三個上限值依論文門檻推得：欄位錯位就會對不上。"""
    checked = 0
    for t in _tables(ar012_cleaned):
        for r in t.table_data:
            v = [_num(c) for c in r[:3]] if len(r) == 4 else [None]
            if len(r) == 4 and None not in v and r[3] and len(r[3]) in (5, 6):
                assert PLANES[v[0]] + UTS[v[1]] + LVS[v[2]] == r[3], r
                checked += 1
    assert checked >= 180


def test_ar012_table6_values(ar012_cleaned):
    t6 = [t for t in _tables(ar012_cleaned) if t.page_number == 12]
    assert len(t6) == 1
    rows = {r[0]: r[1:] for r in t6[0].table_data if r[0]}
    assert rows["Cool"] == ["7", "1", "14", "36", "18"]
    assert rows["Warm"] == ["35", "38", "14", "1", "28"]
    assert rows["Total"] == ["42", "39", "28", "37", "46"]
    assert t6[0].needs_review     # 表頭沒有拆成各欄，交人工確認，不冒充完整


def test_ar012_table8_detected_despite_table7_above(ar012_cleaned):
    p13 = [t for t in _tables(ar012_cleaned) if t.page_number == 13]
    assert len(p13) == 2 and len(p13[1].table_data) >= 30


def test_ar012_no_continued_marker_exported(ar012_cleaned):
    reasons = classify_blocks(ar012_cleaned)
    leaked = [b.block_id for b in ar012_cleaned.blocks if reasons.get(b.block_id) is None
              and b.block_type in ("paragraph", "label") and b.cleaned_text.strip() in ("(Continued)", "(Continues)")]
    assert leaked == []


def test_ar001_partial_table_fragments_not_exported(extract_mod, clean_mod):
    """AR001 Table 1／2 只解析出左半：表格標記可能不完整，右半的數值不可當正文匯出。"""
    if AR001_PDF is None:
        pytest.skip("找不到 AR001 原始 PDF")
    doc = clean_mod.clean_document(extract_mod.extract_pdf(AR001_PDF, "AR001", "6-8"))
    reasons = classify_blocks(doc)
    exported = [b for b in doc.blocks if reasons.get(b.block_id) is None and b.block_type in ("paragraph", "label")]
    for frag in ("1.815 21.948 c 2.513", "11.546 b", "Group 1 (9 women)"):
        assert not any(frag in b.cleaned_text for b in exported), frag
    tables = [b for b in doc.blocks if b.block_type == "table"]
    assert tables and all(t.needs_review for t in tables)
    assert any("可能被欄位分界或對齊截斷" in r for t in tables for r in t.review_reasons)
