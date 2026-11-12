"""產生測試用合成 PDF（SYNTHETIC，非真實文獻數據）。

第 1 頁：
  - Table 1：有格線的 CIELAB 表（欄名含單位 'h (°)'，Coral 的 b* 刻意留空）
  - Table 2：無格線、以空白對齊的表（表頭 "Cohen's d" 含空格）
第 2 頁：
  - Table 3：以點陣圖片嵌入的表格（沒有文字層）
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

RULED_HEADER = ["Shade", "L*", "a*", "b*", "h (°)"]
RULED_ROWS = [
    ["MLBB Rose", "45.32", "28.71", "12.05", "22.8"],
    ["Classic Red", "38.10", "52.36", "30.44", "30.2"],
    ["Coral", "55.87", "41.20", "", "40.8"],          # 原表空白：不得被補值
    ["Berry Purple", "30.25", "35.48", "-3.12", "355.0"],
]
BORDERLESS_HEADER = ["Shade", "Mean", "SD", "Cohen's d"]
BORDERLESS_ROWS = [
    ["Light Orange", "3.36", "0.81", "0.21"],
    ["Red", "4.32", "0.77", "0.92"],
    ["Purple", "3.65", "0.85", "0.47"],
]
RULED_CAPTION = "Table 1. CIELAB values of lipstick shades (D65/10°)"
BORDERLESS_CAPTION = "Table 2. Perceived lightness shift by shade"
IMAGE_CAPTION = "Table 3. Colour difference heatmap"


def _body(page: pymupdf.Page, y: float) -> float:
    for t in ["Lipstick colours were measured with a CS-2000 spectroradiometer under D65 illumination.",
              "The values below are averaged over five participants for each shade."]:
        page.insert_text((72, y), t, fontname="helv", fontsize=10)
        y += 14
    return y


def _ruled(page: pymupdf.Page, y0: float) -> float:
    page.insert_text((72, y0), RULED_CAPTION, fontname="hebo", fontsize=10)
    xs = [72, 190, 270, 350, 430, 520]
    top, rh = y0 + 10, 18
    rows = [RULED_HEADER] + RULED_ROWS
    for i, row in enumerate(rows):
        for j, c in enumerate(row):
            if c:
                page.insert_text((xs[j] + 4, top + i * rh + 13), c, fontname="helv", fontsize=10)
    for i in range(len(rows) + 1):
        page.draw_line((xs[0], top + i * rh), (xs[-1], top + i * rh), width=0.6)
    for x in xs:
        page.draw_line((x, top), (x, top + len(rows) * rh), width=0.6)
    return top + len(rows) * rh


def _borderless(page: pymupdf.Page, y0: float) -> float:
    page.insert_text((72, y0), BORDERLESS_CAPTION, fontname="hebo", fontsize=10)
    xs = [72, 220, 320, 420]
    y = y0 + 20
    for row in [BORDERLESS_HEADER] + BORDERLESS_ROWS:
        for j, c in enumerate(row):
            page.insert_text((xs[j], y), c, fontname="helv", fontsize=10)
        y += 16
    return y


def build_synthetic_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    y = _body(p1, 90)
    y = _ruled(p1, y + 20)
    y = _body(p1, y + 30)
    y = _borderless(p1, y + 20)
    _body(p1, y + 20)
    p1.insert_text((290, 815), "1", fontname="helv", fontsize=10)

    p2 = doc.new_page(width=595, height=842)
    y = _body(p2, 90)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 60), False)
    pix.clear_with(180)
    img_rect = pymupdf.Rect(72, y + 20, 400, y + 180)
    p2.insert_image(img_rect, pixmap=pix)
    p2.insert_text((72, img_rect.y1 + 18), IMAGE_CAPTION, fontname="hebo", fontsize=10)
    _body(p2, img_rect.y1 + 50)
    p2.insert_text((290, 815), "2", fontname="helv", fontsize=10)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# 雙欄期刊版面（仿 Wiley）：左右兩欄正文、跨欄標題、並排標題的矩陣表
# ---------------------------------------------------------------------------

TWO_COL_TITLE = "SYNTHETIC TWO-COLUMN ARTICLE"
MATRIX_CAPTION_LINES = ["Overlap of lip tones", "between groups in a", "paired manner."]
MATRIX_HEADER = [None, "African American", "Asian", "Caucasian", "Hispanic"]
MATRIX_ROWS = [
    ["African American", None, "10", "4", "15"],
    ["Asian", "10", None, "15", "21"],
    ["Caucasian", "4", "15", None, "17"],
    ["Hispanic", "15", "21", "17", None],
]


def _col_line(tag: str, n: int) -> str:
    return f"{tag}{n:02d} column text about natural lip colour and tone."


def build_two_column_pdf(path: Path) -> Path:
    """一頁雙欄：上段 L01–L20／R01–R20，中間是跨欄的矩陣表（標題在左、表格在右），下段 L21–L26／R21–R26。
    閱讀順序應為：標題 → L01–L20 → R01–R20 → 表格 → L21–L26 → R21–R26。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=782)
    page.insert_text((46, 62), TWO_COL_TITLE, fontname="hebo", fontsize=14)
    left_x, right_x, step = 46, 305, 12
    for i in range(20):
        page.insert_text((left_x, 100 + i * step), _col_line("L", i + 1), fontname="helv", fontsize=9)
        page.insert_text((right_x, 100 + i * step), _col_line("R", i + 1), fontname="helv", fontsize=9)

    # 並排標題（左欄窄區）＋ 矩陣表（右側、橫跨欄間）；表格底色讓它成為一個繪圖叢集
    top = 360
    page.draw_rect(pymupdf.Rect(215, top - 12, 550, top + 66), color=None, fill=(0.93, 0.93, 0.93))
    page.insert_text((46, top), "TABLE 1", fontname="hebo", fontsize=8.5)
    page.insert_text((90, top), MATRIX_CAPTION_LINES[0], fontname="helv", fontsize=8.5)
    for k, t in enumerate(MATRIX_CAPTION_LINES[1:], start=1):
        page.insert_text((46, top + 12 * k), t, fontname="helv", fontsize=8.5)
    xs = [221, 319, 395, 445, 506]   # "African American" 與 "Asian" 靠得很近 → 表頭會黏成一段
    for r, row in enumerate([MATRIX_HEADER] + MATRIX_ROWS):
        for j, cell in enumerate(row):
            if cell:
                page.insert_text((xs[j], top + 14 * r), cell, fontname="helv", fontsize=8.5)

    for i in range(6):
        y = 470 + i * step
        page.insert_text((left_x, y), _col_line("L", 21 + i), fontname="helv", fontsize=9)
        page.insert_text((right_x, y), _col_line("R", 21 + i), fontname="helv", fontsize=9)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


WATERMARK_TEXT = "SYNTHETIC 2025, Downloaded from https://example.org/doi/10.0000/synthetic by Reader on [01/01/2026]"
TOP_CAPTION = "TABLE 5 | Synthetic samples placed at the very top of the page."
FRAGMENTS = ["108", "37.86 35.86", "Red lip", "1.1", "109", "37.51 33.18", "Subgroup A", "1.0", "165", "34.35"]


def build_publisher_margin_pdf(path: Path) -> Path:
    """三頁雙欄（SYNTHETIC）。每頁右緣有一行直排的下載浮水印（仿 Wiley）。
    第 2 頁：表格標題排在頁面最上緣。
    第 3 頁：表格標題下方只有零散短行（表格本體無法解析），另有一行位於版面中間的直排文字。"""
    doc = pymupdf.open()
    for pno in range(1, 4):
        page = doc.new_page(width=595, height=782)
        page.insert_text((582, 20), WATERMARK_TEXT, fontname="helv", fontsize=5.3, rotate=270)
        top = 100
        if pno == 2:
            # 仿 Wiley：「TABLE 5」粗體，其後一般字型
            page.insert_text((46, 38), "TABLE 5", fontname="hebo", fontsize=8.5)
            page.insert_text((84, 38), TOP_CAPTION[len("TABLE 5"):], fontname="helv", fontsize=8.5)
        if pno == 3:
            page.insert_text((46, 70), "TABLE 6", fontname="hebo", fontsize=8.5)
            page.insert_text((84, 70), " | Values that cannot be aligned into columns.", fontname="helv", fontsize=8.5)
            # 表格本體解析失敗時的樣子：儲存格各自成塊、上下距離大、左右不對齊
            for i, frag in enumerate(FRAGMENTS):
                page.insert_text((46 + (i % 4) * 61, 100 + i * 25), frag, fontname="helv", fontsize=8.5)
            page.insert_text((300, 560), "Rotated axis title", fontname="helv", fontsize=8, rotate=90)
            top = 360
        for i in range(12):
            page.insert_text((46, top + i * 12), _col_line(f"P{pno}L", i + 1), fontname="helv", fontsize=9)
            page.insert_text((305, top + i * 12), _col_line(f"P{pno}R", i + 1), fontname="helv", fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


ABSTRACT_LONG_LINES = [
    "SYNTHETIC abstract line one describes a standardized colour system for practical communication",
    "SYNTHETIC abstract line two explains how lip makeup samples were measured and then classified",
    "SYNTHETIC abstract line three reports the visual assessment that grouped the red variations",
    "SYNTHETIC abstract line four introduces subgroups using chroma over lightness and undertone",
    "and finally we propose a tailored colour system for red lip makeup, demonstrating its",
]
ABSTRACT_LAST_LINE = "efficacy through comprehensive SYNTHETIC colour analysis."
RUNNING_HEADER = "SYNTHETIC Journal of Colour"
PAGE_ONE_LAST_RIGHT = "the measured samples were then compared with the"
PAGE_TWO_FIRST_LEFT = "reference chart, which completes the SYNTHETIC sentence."
SYMBOL_PROSE_TAIL = "E*94 stayed below 1.5 in every SYNTHETIC trial."
TABLE_PAGE_LAST_LEFT = "Eleven SYNTHETIC shades were then labelled. The study"
TABLE_PAGE_FIRST_RIGHT = "plans were approved by a SYNTHETIC ethics board."
UNPARSED_TABLE_ROWS = ["Dark red", "25", "Deep red", "1", "Light red", "2", "Strong red", "77", "Weak red", "36"]


def _spans(page: pymupdf.Page, x: float, y: float, parts: list[tuple[str, str]], size: float = 9) -> None:
    """同一行依序寫入不同字型的片段（例如 Symbol 字型的 Δ、λ 夾在一般文字中）。"""
    for font, text in parts:
        page.insert_text((x, y), text, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=size)


def build_continuation_pdf(path: Path) -> Path:
    """兩頁雙欄期刊版面（SYNTHETIC），仿 AR012／TH014 的段落接續問題。

    第 1 頁：頁首書眉（與正文有明顯距離）；灰底摘要框，最後一行是短句（< 60 字）；
             雙欄正文，右欄最後一行句子沒結束。
    第 2 頁：沒有書眉，左欄第一行就在頁面上方 7% 內，接續第 1 頁右欄的句子；
             左欄下方一行正文夾一個 Symbol 字型的 Δ，另一段是 Symbol 字型排的積分公式。
    第 3 頁：左欄段落沒結束，中間隔著未解析的表格本體與欄名，右欄頂端接續。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=782)
    page.insert_text((45, 40), RUNNING_HEADER, fontname="helv", fontsize=8)
    page.insert_text((45, 80), "SYNTHETIC CONTINUATION ARTICLE", fontname="hebo", fontsize=14)
    page.draw_rect(pymupdf.Rect(45, 100, 550, 205), color=None, fill=(0.93, 0.93, 0.93))
    page.insert_text((54, 116), "ABSTRACT", fontname="hebo", fontsize=9)
    for i, t in enumerate(ABSTRACT_LONG_LINES + [ABSTRACT_LAST_LINE]):
        page.insert_text((54, 130 + i * 12), t, fontname="helv", fontsize=9)
    page.insert_text((45, 236), "1 | Introduction", fontname="hebo", fontsize=11)
    for i in range(14):
        page.insert_text((45, 256 + i * 12), _col_line("L", i + 1), fontname="helv", fontsize=9)
        right = PAGE_ONE_LAST_RIGHT if i == 13 else _col_line("R", i + 1)
        page.insert_text((306, 256 + i * 12), right, fontname="helv", fontsize=9)

    page = doc.new_page(width=595, height=782)
    left = [PAGE_TWO_FIRST_LEFT] + [_col_line("Q", i) for i in range(2, 12)]
    for i, t in enumerate(left):
        page.insert_text((45, 40 + i * 12), t, fontname="helv", fontsize=9)
        page.insert_text((306, 40 + i * 12), _col_line("S", i + 1), fontname="helv", fontsize=9)
    # Symbol 字型：'D' = Δ、'f' = φ、'=' 、'ò' = ∫、'l' = λ（Adobe Symbol 編碼）
    _spans(page, 45, 200, [("helv", "In SYNTHETIC trial 3 the colour difference "), ("symb", "D"),
                           ("helv", SYMBOL_PROSE_TAIL)])
    _spans(page, 60, 240, [("symb", "f="), ("helv", "Km"), ("symb", "ò"), ("helv", "P("), ("symb", "l"),
                           ("helv", ")V("), ("symb", "l"), ("helv", ")d"), ("symb", "l")])

    # 第 3 頁：左欄段落沒結束，下方是沒被解析成表格的表格本體（名稱、數字各占一行）與欄名，
    # 右欄頂端才接續句子（仿 AR012 p.4 表 3）
    page = doc.new_page(width=595, height=782)
    for i in range(10):
        page.insert_text((45, 80 + i * 12), _col_line("T", i + 1), fontname="helv", fontsize=9)
        page.insert_text((306, 52 + i * 12), _col_line("U", i + 1), fontname="helv", fontsize=9)
    page.insert_text((45, 80 + 10 * 12), TABLE_PAGE_LAST_LEFT, fontname="helv", fontsize=9)
    page.insert_text((306, 40), TABLE_PAGE_FIRST_RIGHT, fontname="helv", fontsize=9)
    page.insert_text((45, 260), "TABLE 3", fontname="hebo", fontsize=8.5)
    page.insert_text((83, 260), " | SYNTHETIC classification of samples.", fontname="helv", fontsize=8.5)
    page.insert_text((51, 282), "Designated red", fontname="hebo", fontsize=8.5)
    for i, t in enumerate(UNPARSED_TABLE_ROWS):
        page.insert_text((51, 298 + i * 11), t, fontname="helv", fontsize=8.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path
