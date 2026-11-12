"""產生 TX001 粗體引言／詞彙表修復用的合成 PDF（SYNTHETIC，非真實文獻內容）。

模擬職訓教材的兩種版式（文字都是自擬，不取自任何文獻）：
  第 1 頁：編號條列的粗體引言「**1. Wrist check:** Look at …」，第一行很短（<= 60 字）；
          左側頁邊欄有粗體「Notes」與第一行同高，閱讀順序會排在第一行與第二行之間。
  第 2 頁（反例）：結構式摘要（每行都是「**Aim:** …」，但不在詞彙表區）、內文字級未置中的
          「Glossary」字樣、只有編號是粗體的一般小標「**2.** Colour basics」。
          放在詞彙表之前：詞彙表區會延續到下一頁的第一個標題（跨頁詞彙表），反例不能落在那裡。
  第 3 頁：首字放大、未置中的「Glossary」標題（G 16pt＋其餘 11pt），下面是「**詞條:** 定義」逐條排列，
          有單行也有兩行的詞條，行距與一般段落相同。
  第 4 頁：詞彙表續頁（沒有標題，頁首直接接詞條），之後是一般標題與結構式摘要（詞彙表區到此結束）。
頁面尺寸 596×786，與 TX001 相同。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

PAGE_W, PAGE_H = 596, 786
BODY_X = 120          # 正文左緣
SIDE_X = 20           # 頁邊欄左緣（與正文水平距離 > 40pt，不會被併成同一行）
LINE_STEP = 14        # 10pt 正文的行距

# 第 1 頁：編號粗體引言（第一行 <= 60 字，舊版會因「1.」編號＋行首粗體被判成標題）
STEP_ITEMS = [
    ("1. Wrist check:", " Look at the lines under the",
     ["inner arm in daylight and note whether they look blue or",
      "green before choosing the base shade."]),
    ("2. Metal check:", " Hold gold and silver fabric",
     ["near the face and compare which one makes the skin look",
      "more even."]),
]
SIDE_LABEL = "Notes"

# 第 3 頁：詞彙表（詞條, 定義第一行, 其餘行）
GLOSSARY_ENTRIES = [
    ("Blusher:", " Powder or cream that adds colour to the cheeks.", []),
    ("Base shade:", " The first layer of colour applied to even out the", ["skin before other products."]),
    ("Gloss:", " A shiny finish that reflects light on the lips.", []),
    ("Liner:", " A pencil used to draw the outer edge of the lips so", ["that colour stays in place."]),
]
GLOSSARY_CONTINUED = [
    ("Primer:", " A thin layer that helps make-up stay on the skin.", []),
    ("Tint:", " A light wash of colour that lets the skin show", ["through."]),
]
AFTER_GLOSSARY_HEADING = "Answer Key"

# 第 2 頁（反例）
ABSTRACT_LINES = [
    ("Aim:", " To compare two ways of choosing a lip colour in a small group."),
    ("Methods:", " Ten volunteers tried both ways under the same lamp."),
    ("Results:", " Most volunteers preferred the second way for daily use."),
]
PLAIN_GLOSSARY_WORD = "Glossary"
NUMBER_ONLY_BOLD_HEADING = ("2.", " Colour basics")


def _run_in(page: pymupdf.Page, x: float, y: float, bold: str, regular: str, size: float = 10) -> None:
    """同一行寫入粗體引言＋非粗體正文（兩個 span，緊接在一起）。"""
    page.insert_text((x, y), bold, fontname="hebo", fontsize=size)
    page.insert_text((x + pymupdf.get_text_length(bold, "hebo", size), y), regular, fontname="helv", fontsize=size)


def _plain(page: pymupdf.Page, x: float, y: float, text: str, size: float = 10) -> None:
    """寫入一行非粗體正文。"""
    page.insert_text((x, y), text, fontname="helv", fontsize=size)


def _steps_page(page: pymupdf.Page) -> None:
    """第 1 頁：頁邊欄 Notes＋兩個編號粗體引言條列。"""
    y = 200.0
    # Notes 的基線比第一行低 4pt：外框仍與第一行重疊過半，但上緣較低，閱讀順序排在第一行之後
    page.insert_text((SIDE_X, y + 4), SIDE_LABEL, fontname="hebo", fontsize=12)
    for bold, first, rest in STEP_ITEMS:
        _run_in(page, BODY_X, y, bold, first)
        for t in rest:
            y += LINE_STEP
            _plain(page, BODY_X + 18, y, t)
        y += LINE_STEP * 2


def _glossary_page(page: pymupdf.Page) -> None:
    """第 3 頁：首字放大、未置中的 Glossary 標題＋逐條詞條（行距與段落相同）。"""
    x, y = 330.0, 90.0   # 行中心離頁面中心 > 30pt：不算置中
    page.insert_text((x, y), "G", fontname="helv", fontsize=16)
    page.insert_text((x + pymupdf.get_text_length("G", "helv", 16), y), "lossary", fontname="helv", fontsize=11)
    y += 30
    for bold, first, rest in GLOSSARY_ENTRIES:
        _run_in(page, BODY_X, y, bold, first)
        for t in rest:
            y += LINE_STEP
            _plain(page, BODY_X, y, t)
        y += LINE_STEP


def _counter_page(page: pymupdf.Page) -> None:
    """第 2 頁（反例）：結構式摘要、內文字級的 Glossary 字樣、只有編號粗體的小標。"""
    y = 90.0
    for bold, regular in ABSTRACT_LINES:
        _run_in(page, BODY_X, y, bold, regular)
        y += LINE_STEP
    y += 30
    _plain(page, BODY_X, y, PLAIN_GLOSSARY_WORD)
    y += 30
    bold, regular = NUMBER_ONLY_BOLD_HEADING
    _run_in(page, BODY_X, y, bold, regular, size=12)
    y += 20
    _plain(page, BODY_X, y, "Warm and cool colours sit on opposite sides of the colour wheel.")


def _glossary_continued_page(page: pymupdf.Page) -> None:
    """第 4 頁：詞彙表續頁（頁首直接是詞條），之後的標題結束詞彙表區，標題下的粗體引言不拆。"""
    y = 90.0
    for bold, first, rest in GLOSSARY_CONTINUED:
        _run_in(page, BODY_X, y, bold, first)
        for t in rest:
            y += LINE_STEP
            _plain(page, BODY_X, y, t)
        y += LINE_STEP
    y += 30
    page.insert_text((BODY_X, y), AFTER_GLOSSARY_HEADING, fontname="hebo", fontsize=14)
    y += 30
    for bold, regular in ABSTRACT_LINES:
        _run_in(page, BODY_X, y, bold, regular)
        y += LINE_STEP


def build_runin_pdf(path: Path) -> Path:
    """建立四頁合成 PDF（SYNTHETIC），回傳路徑。"""
    doc = pymupdf.open()
    for builder in (_steps_page, _counter_page, _glossary_page, _glossary_continued_page):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        builder(page)
        _plain(page, PAGE_W / 2 - 5, PAGE_H - 30, str(page.number + 1), size=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path
