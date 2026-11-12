"""產生 TX001 版面修復用的合成 PDF（SYNTHETIC，非真實文獻內容）。

模擬職訓教材的版型：每頁都有一張超出頁面的整頁底圖（浮水印），以及外框線。
  第 1 頁：說明面板——色塊標題列＋分隔線＋框線，框內是每行 40–65 字的正文步驟（應判為正文）
  第 2 頁：長條圖——填色長條＋座標軸＋短標籤（應仍判為圖表）
  第 3 頁：折線圖（斜線）＋分隔線圍成的兩欄對照表（兩者都應仍判為圖表，交人工確認）
頁面尺寸 596×786，與 TX001 相同。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

PAGE_W, PAGE_H = 596, 786
# 浮水印原始框超出頁面（TX001 實測 [-8,-39,604,826]），get_text 回傳的是裁到頁內的框
WATERMARK_RECT = pymupdf.Rect(-8, -39, 604, 826)

PANEL_TITLE = "Applying Lipstick"
PANEL_STEPS = [
    "Step 1: Outline the lips with a lip liner close to the shade.",
    "Step 2: Fill in the lips with lipstick using a clean lip brush.",
    "Step 3: Blot the lips with a tissue to remove extra product.",
    "Step 4: Apply a second coat so that the colour lasts longer",
    "through the working day (Figure 1.18).",
    "Step 5: Check both corners of the mouth and clean any edges.",
]
BODY_LINES = [
    "Colour choice depends on the occasion, the outfit and the overall look of the client.",
    "The therapist should discuss the expected result before starting the application.",
]
BAR_LABELS = ["Red", "Pink", "Nude", "Coral"]
BAR_VALUES = [40, 65, 30, 50]
AXIS_TICKS = ["0", "20", "40", "60"]
TABLE_ROWS = [
    ("Open", "Establishing the nature of the call"),
    ("Closed", "To establish or confirm information"),
    ("Probing", "Gathering specific details of needs"),
]


def _watermark_png() -> bytes:
    """淺灰色的小點陣圖，放大成整頁底圖（每頁同一份位元組 → 同一個雜湊，會被認成浮水印）。"""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 52), False)
    pix.set_rect(pix.irect, (235, 235, 235))
    return pix.tobytes("png")


def _page_chrome(page: pymupdf.Page, png: bytes) -> None:
    """每頁共同的版面：超出頁面的整頁底圖、超出頁面的外框線、頁碼。"""
    page.insert_image(WATERMARK_RECT, stream=png, keep_proportion=False, overlay=False)
    page.draw_rect(pymupdf.Rect(-5, -5, PAGE_W + 5, PAGE_H + 5), color=(0.7, 0.1, 0.4), width=2)


def _body(page: pymupdf.Page, y: float) -> float:
    """寫入一般正文（長行），回傳下一行的 y。"""
    for t in BODY_LINES:
        page.insert_text((61, y), t, fontname="helv", fontsize=10)
        y += 14
    return y


def _panel(page: pymupdf.Page) -> None:
    """說明面板：色塊標題列（純矩形填色）＋框線＋步驟間的白色分隔線，框內是正文。"""
    x0, x1, top = 61, 385, 120
    page.draw_rect(pymupdf.Rect(x0, top, x1, top + 18), color=None, fill=(0.71, 0.08, 0.43))
    page.insert_text((x0 + 100, top + 13), PANEL_TITLE, fontname="hebo", fontsize=10, color=(1, 1, 1))
    y = top + 34
    for i, step in enumerate(PANEL_STEPS):
        page.insert_text((x0 + 4, y), step, fontname="helv", fontsize=10)
        if i < len(PANEL_STEPS) - 1 and not PANEL_STEPS[i + 1].startswith("through"):
            page.draw_line((x0, y + 5), (x1, y + 5), color=(0.8, 0.8, 0.8), width=0.5)
        y += 18
    bottom = y
    page.draw_line((x0, top), (x0, bottom), color=(0.71, 0.08, 0.43), width=0.8)
    page.draw_line((x1, top), (x1, bottom), color=(0.71, 0.08, 0.43), width=0.8)
    page.draw_line((x0, bottom), (x1, bottom), color=(0.71, 0.08, 0.43), width=0.8)


def _chart_frame(page: pymupdf.Page, rect: pymupdf.Rect) -> None:
    """用四條直線畫圖框（不是矩形路徑），把刻度與標籤包在框內：
    這樣圖表只由「直線＋純矩形」組成，與說明面板的繪圖種類相同，只能靠框內文字區分。"""
    for a, b in ((rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)):
        page.draw_line(a, b, color=(0.5, 0.5, 0.5), width=0.5)


def _bar_chart(page: pymupdf.Page, top: float) -> None:
    """長條圖：座標軸（直線）、填色長條（純矩形）、短標籤與刻度，外加直線圖框。"""
    left, base, h = 120, top + 200, 180
    _chart_frame(page, pymupdf.Rect(left - 30, base - h - 10, left + 310, base + 20))
    page.draw_line((left, base), (left + 300, base), width=0.8)
    page.draw_line((left, base), (left, base - h), width=0.8)
    for i, t in enumerate(AXIS_TICKS):
        page.insert_text((left - 20, base - i * 60 + 3), t, fontname="helv", fontsize=8)
    for i, (lab, v) in enumerate(zip(BAR_LABELS, BAR_VALUES)):
        x = left + 20 + i * 70
        page.draw_rect(pymupdf.Rect(x, base - v * 3, x + 40, base), color=None, fill=(0.8, 0.2, 0.3))
        page.insert_text((x + 5, base + 12), lab, fontname="helv", fontsize=8)


def _line_chart(page: pymupdf.Page, top: float) -> None:
    """折線圖：座標軸＋斜線折線＋短標籤，外加直線圖框。"""
    left, base = 120, top + 160
    _chart_frame(page, pymupdf.Rect(left - 30, base - 150, left + 310, base + 20))
    page.draw_line((left, base), (left + 300, base), width=0.8)
    page.draw_line((left, base), (left, base - 140), width=0.8)
    pts = [(left + 20, base - 30), (left + 100, base - 90), (left + 180, base - 60), (left + 280, base - 120)]
    for a, b in zip(pts, pts[1:]):
        page.draw_line(a, b, color=(0.1, 0.3, 0.7), width=1.2)
    for i, lab in enumerate(["2019", "2020", "2021", "2022"]):
        page.insert_text((left + 20 + i * 80, base + 12), lab, fontname="helv", fontsize=8)


def _ruled_table(page: pymupdf.Page, top: float) -> None:
    """分隔線圍成的兩欄對照表（左欄類型、右欄說明，同一列左右並排）。"""
    x0, xm, x1 = 61, 180, 534
    page.draw_line((x0, top), (x1, top), width=0.6)
    y = top + 16
    for left, right in TABLE_ROWS:
        page.insert_text((x0 + 4, y), left, fontname="helv", fontsize=10)
        page.insert_text((xm + 4, y), right, fontname="helv", fontsize=10)
        page.draw_line((x0, y + 6), (x1, y + 6), width=0.6)
        y += 22
    page.draw_line((xm, top), (xm, y - 16), width=0.6)


def build_layout_pdf(path: Path) -> Path:
    """建立三頁合成教材 PDF，回傳路徑。"""
    png = _watermark_png()
    doc = pymupdf.open()
    for builder in ("panel", "bar", "mixed"):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        _page_chrome(page, png)
        y = _body(page, 90)
        if builder == "panel":
            _panel(page)
            _body(page, 330)
        elif builder == "bar":
            _bar_chart(page, y + 10)
        else:
            _line_chart(page, y + 10)
            _ruled_table(page, y + 220)
        page.insert_text((PAGE_W / 2 - 5, PAGE_H - 30), str(page.number + 1), fontname="helv", fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path
