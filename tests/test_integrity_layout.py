"""資料完整性修復第二批（2026-10-17）：雙欄頁面局部被橫向合併、無格線表格的儲存格換行。

對應問題：
- D03 AR012 p.3 頁頂、p.7／p.8 頁尾，AR002 p.8：圖表佔大半的頁面正文行太少，detect_columns 判不出雙欄，
  左右欄同高的行被合併成一行（「…performed The subsequent…」「…light source, The correlation…」）。
- D04 AR012 Table 1：第三欄置中排版的儲存格換行（「purple, pink, coral, nude」）沒有欄距，表格偵測在此中斷，
  其餘列被當成正文、還被接到上一段後面；最後一行壓到頁面底部 7% 也被排除。

合成資料標示 SYNTHETIC；真實 PDF 測試在檔案不存在時略過。
"""
from __future__ import annotations

import pymupdf
import pytest

from tests.conftest import ROOT

AR012_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR012_*.pdf"))), None)
AR002_PDF = next(iter(sorted((ROOT / "data" / "raw").rglob("AR002_*.pdf"))), None)


def _w(x0, y0, x1, y1, text):
    """get_text('words') 格式的單字（後三欄區塊／行／字序號不使用，補 0）。"""
    return (x0, y0, x1, y1, text, 0, 0, 0)


# ---------------------------------------------------------------------------
# D03：文件層級欄位分界
# ---------------------------------------------------------------------------

def test_side_by_side_pairs_two_columns(extract_mod):
    R = pymupdf.Rect
    rects = [R(45, 30, 291, 42), R(306, 30, 552, 42), R(45, 42, 291, 54), R(306, 42, 552, 54)]
    assert extract_mod.side_by_side_pairs(rects, 298.5) == 2


def test_side_by_side_pairs_full_width_lines(extract_mod):
    """單欄頁面：每一行都跨過分界，沒有並排的左右段。"""
    R = pymupdf.Rect
    rects = [R(70, 100 + 12 * i, 530, 110 + 12 * i) for i in range(10)]
    assert extract_mod.side_by_side_pairs(rects, 298.5) == 0


def test_side_by_side_pairs_far_apart_labels(extract_mod):
    """左右兩段距離 >= 40pt（例如圖表兩側的軸標籤）本來就不會被合併，不算並排。"""
    R = pymupdf.Rect
    rects = [R(45, 30, 200, 42), R(320, 30, 552, 42), R(45, 50, 200, 62), R(320, 50, 552, 62)]
    assert extract_mod.side_by_side_pairs(rects, 298.5) == 0


def test_single_column_document_has_no_split(extract_mod, synthetic_pdf):
    with pymupdf.open(synthetic_pdf) as pdf:
        assert extract_mod.document_column_split(pdf, list(range(1, pdf.page_count + 1))) is None


def test_two_column_document_has_split(extract_mod, two_column_pdf):
    with pymupdf.open(two_column_pdf) as pdf:
        assert extract_mod.document_column_split(pdf, list(range(1, pdf.page_count + 1))) is not None


def test_minority_two_column_pages_not_document_split(extract_mod, monkeypatch):
    """只有少數頁偵測為雙欄（例如單欄論文中一頁並排的圖說）：不認定整份文件是雙欄。"""
    layouts = iter([extract_mod.ColumnLayout(split_x=298.0)] + [extract_mod.ColumnLayout()] * 3)
    monkeypatch.setattr(extract_mod, "read_pdf_lines", lambda page, column_hint=None: ([], [], next(layouts)))
    assert extract_mod.document_column_split({i: None for i in range(4)}, [1, 2, 3, 4]) is None


# ---------------------------------------------------------------------------
# D04：儲存格換行
# ---------------------------------------------------------------------------

# AR012 Table 1 第 2 列的實際座標（三欄：研究者、年份、置中的色群）
ANCHOR = [_w(51.4, 630.4, 70, 643.2, "Jeong"), _w(72, 630.4, 85, 643.2, "and"), _w(87, 630.4, 105, 643.2, "Park"),
          _w(126.3, 630.4, 145, 643.2, "2018"),
          _w(161.2, 630.4, 175, 643.2, "Red,"), _w(177, 630.4, 220, 643.2, "orange"), _w(222, 630.4, 280, 643.2, "(brown),")]


def test_wrapped_cell_row_in_last_column(extract_mod):
    wrap = [_w(173.7, 641.9, 200, 654.7, "purple,"), _w(202, 641.9, 260, 654.7, "pink, nude")]
    assert extract_mod.wrapped_cell_row(wrap, ANCHOR, gap=-1.3, size=9.0)


def test_centered_wrap_left_of_cell_start(extract_mod):
    """置中排版：續行左緣（167.4）比儲存格第一行（169.3）還左，仍在該欄範圍內。"""
    anchor = ANCHOR[:4] + [_w(169.3, 692.5, 280, 705.3, "Red, orange,")]
    wrap = [_w(167.4, 715.5, 275, 728.3, "white, black, gray, and nude")]
    assert extract_mod.wrapped_cell_row(wrap, anchor, gap=-1.3, size=9.0)


@pytest.mark.parametrize("wrap, gap", [
    ([_w(51.4, 658.8, 90, 671.6, "Oh et al.")], 2.0),                       # 落在第一欄：新的列名
    ([_w(120, 641.9, 260, 654.7, "SYNTHETIC text across columns")], -1.3),   # 跨到年份欄：不是單一儲存格
    ([_w(173.7, 660, 200, 672, "purple,")], 8.0),                            # 間距太大：不是緊貼的續行
])
def test_not_wrapped_cell_row(extract_mod, wrap, gap):
    assert not extract_mod.wrapped_cell_row(wrap, ANCHOR, gap=gap, size=9.0)


def test_body_text_below_table_ends_table(extract_mod):
    """表格下方空一行接正文：正文行沒有欄距、和表格之間有空白，不可被吞進表格。"""
    cap = pymupdf.Rect(45, 595, 204, 607)
    header = [_w(51, 613, 100, 626, "Researchers"), _w(125, 613, 145, 626, "Year"), _w(180, 613, 260, 626, "Groups")]
    row = [_w(51, 630, 100, 643, "Oh"), _w(126, 630, 145, 643, "2017"), _w(180, 630, 230, 643, "Red")]
    body = [_w(45, 655, 90, 667, "SYNTHETIC"), _w(92, 655, 140, 667, "body"), _w(142, 655, 200, 667, "text")]
    rows = extract_mod.find_text_table_rows(cap, header + row + body, 9.0, pymupdf.Rect(0, 0, 595, 782))
    assert len(rows) == 2 and all(w[4] != "SYNTHETIC" for _, ws in rows for w in ws)


def test_footer_page_number_not_absorbed(extract_mod):
    """頁尾頁碼落在底部邊緣區（頁高 782 的 93% = 727.3 以下）、與表格空 15pt：不收進表格。
    15pt 小於一般列距門檻（3 × 字級 = 27pt），只有「邊緣區必須緊貼」這條規則能擋下。"""
    cap = pymupdf.Rect(45, 660, 204, 672)
    header = [_w(51, 678, 100, 690, "Name"), _w(125, 678, 145, 690, "Year"), _w(180, 678, 260, 690, "Groups")]
    row = [_w(51, 700, 100, 712, "Oh"), _w(126, 700, 145, 712, "2017"), _w(180, 700, 230, 712, "Red")]
    footer = [_w(45, 727.5, 60, 739, "543"), _w(448, 727.5, 550, 739, "Journal")]
    rows = extract_mod.find_text_table_rows(cap, header + row + footer, 9.0, pymupdf.Rect(0, 0, 595, 782))
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 真實文件
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ar012_doc(extract_mod):
    if AR012_PDF is None:
        pytest.skip("找不到 AR012 原始 PDF")
    return extract_mod.extract_pdf(AR012_PDF, "AR012")


def _texts(doc, page):
    return [" ".join(b.raw_text.split()) for b in doc.blocks if b.page_number == page and not b.table_data]


def test_ar012_d03_page3_columns_not_interleaved(ar012_doc):
    p3 = _texts(ar012_doc, 3)
    assert not any("performed The subsequent" in t for t in p3)
    assert any(t.endswith("as depicted in Figure 2.") for t in p3)
    assert any(t.startswith("The subsequent matching procedure was carried out") for t in p3)


def test_ar012_d03_page7_columns_not_interleaved(ar012_doc):
    assert any("the a*-­b* plane. The subgroups within the 14 samples" in t for t in _texts(ar012_doc, 7))


def test_ar012_d04_table1_complete(ar012_doc):
    tables = [b for b in ar012_doc.blocks if b.page_number == 2 and b.block_type == "table"]
    assert len(tables) == 1
    assert tables[0].table_data == [
        ["Researchers", "Year", "Color shade groups"],
        ["Jeong and Park", "2018", "Red, orange (including brown), purple, pink, coral, nude"],
        ["Oh et al.", "2017", "Red, orange, purple, coral, nude"],
        ["Piccinini et al.", "2013", "Red, brown, purple, pink"],
        ["Wu et al.", "2024", "Red, orange, yellow, green, blue, brown, purple, pink, white, black, gray, and nude"],
    ]
    # 表身不再被當成正文
    assert not any("Oh et al. 2017" in t for t in _texts(ar012_doc, 2))


def test_ar002_d03_page8_columns_not_interleaved(extract_mod):
    if AR002_PDF is None:
        pytest.skip("找不到 AR002 原始 PDF")
    doc = extract_mod.extract_pdf(AR002_PDF, "AR002", "8-9")
    joined = [" ".join(b.raw_text.split()) for b in doc.blocks]
    assert not any("light source, The correlation" in t for t in joined)
    assert any("Furthermore, under the 2,500 K light source, females exhibited" in t for t in joined)
