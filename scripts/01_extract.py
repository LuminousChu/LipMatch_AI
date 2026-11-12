"""01_extract.py — 從 PDF / HTML 擷取章節、段落、表格、公式與來源頁碼，並做解析完整性檢查。

用法：
    python scripts/01_extract.py data/raw/academic_papers/TH013_唇彩影響臉部色彩感知_英文文獻_2024.pdf --doc-id TH013 --pages 1-12
    python scripts/01_extract.py data/raw/            # 處理資料夾內所有 .pdf/.html

輸出：
    data/interim/extracted/<doc_id>.json        Document(stage="extracted")
    data/review/<doc_id>.extract_review.json     待人工確認清單

原則：
- 原始檔只讀不寫；輸出路徑若落在 data/raw 底下會直接報錯。
- 這一步「只擷取不清洗」：raw_text 保留 PDF 斷行與 HTML 原始空白。
- 解析失敗的區域（圖片表格、向量圖表、結構遺失的公式）一律建立 block，
  parse_status=failed/partial + failure_reason + bbox + needs_review，絕不當成空白略過。
- 表格只取表格本身的文字；欄名、列名、單位找不到就留空並說明，不推測、不補值。
- 同樣輸入重跑會得到相同的 block_id 與內容（只有 extracted_at 時間戳不同）。
- PDF 目前假設單欄版面。
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bs4  # noqa: E402
import pymupdf  # noqa: E402
from bs4 import BeautifulSoup, Comment, NavigableString, Tag  # noqa: E402

from lipmatch.eligibility import GLOSSARY_MARKER_RE  # noqa: E402
from schemas import Document, ExtractionIssue, PageReport, SourceManifest, TableStructure, TextBlock  # noqa: E402

if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()  # 關閉 find_tables 印到 stdout 的套件推薦訊息

RAW_DIR = ROOT / "data" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "interim" / "extracted"
DEFAULT_REVIEW_DIR = ROOT / "data" / "review"

# ---------------------------------------------------------------------------
# 共用規則
# ---------------------------------------------------------------------------

# 英文 "Figure 2.1" / "Table 4.1"；中文 "圖 2-1" / "表 3-3"
# 英文編號可帶一個大寫字母前綴：附錄「TABLE A1」、補充資料「Figure S2」（AR012 附錄表原本不被認成標題）
CAPTION_RE = re.compile(r"^\s*((Figure|Fig\.|Table)\s*[A-Z]?\d+(\.\d+)*\b|[圖表]\s*\d+([-.]\d+)*)", re.I)
TABLE_CAPTION_RE = re.compile(r"^\s*(Table\s*[A-Z]?\d+(\.\d+)*\b|表\s*\d+([-.]\d+)*)", re.I)
CJK_CAPTION_RE = re.compile(r"^\s*[圖表]\s*\d")
CHAPTER_RE = re.compile(r"^\s*(Chapter\s+\d+\b|第\s*[一二三四五六七八九十百\d]+\s*章)", re.I)
FRONT_MATTER_RE = re.compile(
    r"^\s*(Abstract|Acknowledg(e)?ments?|Table of Contents|Contents|List of (Figures|Tables|Abbreviations)"
    r"|References?|Reference List|Bibliography|Appendi(x|ces)( [A-Z0-9]+)?|Glossary"
    r"|Declaration( of (Conflicting|Competing) Interests?)?|Conflicts? of Interests?|Funding|Author Biograph(y|ies)"
    r"|中文摘要|英文摘要|摘\s*要|誌\s*謝|致\s*謝|目\s*錄|圖目錄|表目錄|符號說明|參考文獻|附\s*錄(\s*[一二三四五六七八九十A-Z\d]+)?)\s*:?\s*$",
    re.I,
)
NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
TOC_LINE_RE = re.compile(r"\.{4,}\s*\d+\s*$")
LIST_ITEM_RE = re.compile(r"^\s*(\d{1,2}[.)]\s+|[\u2022\u25cf\u25aa\u25e6\u25a0\uf000-\uf0ff]\s*|[a-z][.)]\s+)")
# Word 的 Symbol／Wingdings 項目符號落在私有區 U+F000–F0FF（例如 U+F06C），是符號不是亂碼
PUA_BULLET_RE = re.compile(r"^\s*[\uf000-\uf0ff]\s*")
SUBCAPTION_RE = re.compile(r"^\s*\(([a-z]|[ivx]+|\d)\)\s*\S")
EQUATION_LABEL_RE = re.compile(r"^\s*((Equation|Eq\.)\s*\(?\d{1,3}([.-]\d{1,3})*\)?|[（(]\d{1,3}([.-]\d{1,3})*[）)])\s*$", re.I)
MATH_CHAR_RE = re.compile(r"[\U0001D400-\U0001D7FF∑∏√∫∂∆Δ≤≥≈≠±×÷²³⁰¹⁴-⁹₀-₉]")
# Symbol 字型（Word 插入的希臘字母與數學符號）的字碼落在私有區 U+F020–F0FE，低位元組即 Adobe Symbol 編碼。
# 不轉換的話「ΔE*94」會變成私有區字元，整段被當成字型對應失敗而送人工確認（TH014 p.101 結論段）。
# 括號／積分的拼接零件（0xE6–0xEF、0xF3–0xFE 等）沒有對應的單一字元，保留原字碼，仍會被標記待確認。
SYMBOL_FONT_CHARS = {
    **{c: chr(c) for c in b" !#%&(),./0123456789:;<=>?[]_{|}"},
    0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2B: "+", 0x2D: "−", 0x40: "≅", 0x5C: "∴", 0x5E: "⊥", 0x7E: "∼",
    **dict(zip(range(0x41, 0x5B), "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ")),
    **dict(zip(range(0x61, 0x7B), "αβχδεφγηιϕκλμνοπθρστυϖωξψζ")),
    **dict(zip(range(0xA1, 0xBD), "ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…")),
    **dict(zip(range(0xBF, 0xE6), "↵ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓◊〈®©™∑")),
    0xF1: "〉", 0xF2: "∫",
}
SYMBOL_PUA_RE = re.compile(r"[\uf020-\uf0fe]")
# SAGE 期刊的 Advent 數學字型沒有正確的 Unicode 對應：等號存成「¼」、乘號存成 \x02、η 存成「Z」
# （AR003 p.4「F(3, 57) ¼ 15.07」「Zp2¼ .44」、p.3「518 \x02 324 mm」）。只認這兩個字型名，其他字型的 ¼ 不動。
ADVENT_FONT_CHARS = {"AdvMT_SY": {"\u00bc": "=", "\x02": "×"}, "AdvPi1": {"Z": "η"}}
# η 後面接下標 p、上標 2 是偏 η 平方（partial eta squared），上下標被抽成同一行的普通字「p2」
PARTIAL_ETA_SQUARED_RE = re.compile(r"η\s?p\s?2(?![0-9])")
# 一行裡 Symbol 字型的字 >= 30% → 是用 Symbol 字型排的公式（例如「φ=Km∫(Pλ)V(λ)dλ」與單獨成行的積分上下限），
# 純文字留不住上下限、分數等版面，仍交人工確認；正文中夾一個 Δ 的句子不受影響
SYMBOL_FORMULA_SHARE = 0.3


def map_symbol_font(text: str, font: str) -> str:
    """Symbol 字型的私有區字碼 → Unicode（例如 U+F044 → Δ）；Advent 數學字型的錯誤字碼 → 正確符號。
    其他字型（含 Wingdings 項目符號）不動。字型名可能帶子集前綴（ABCDEF+AdvMT_SY），比對時去掉。"""
    table = ADVENT_FONT_CHARS.get(font.split("+")[-1])
    if table:
        return "".join(table.get(ch, ch) for ch in text)
    if "symbol" not in font.lower():
        return text
    return SYMBOL_PUA_RE.sub(lambda m: SYMBOL_FONT_CHARS.get(ord(m.group()) - 0xF000, m.group()), text)


SUSPECT_GLYPH_RE = re.compile(r"[\uFFFD\uE000-\uF8FF]")  # 替代字元 / 私有區字元 = 字型對應失敗
GARBLED_MATH_RE = re.compile(r"[!\"#$%&']|(?<=[)\u2217*])\*")

NUMERIC_CELL_RE = re.compile(
    r"^[<>≤≥~±+\-−]?\s*\d+(?:[.,]\d+)*(\s*[-–~]\s*\d+(?:[.,]\d+)*)?"
    r"\s*(%|°|K|nm|mm|lx|lux|cd/m²|cd/m2|\*{1,3})?$|^(n\.?s\.?|[-–—])$", re.I)
UNIT_TOKEN = r"(nm|mm|cm|m|K|%|°|deg|cd/m²|cd/m2|lx|lux|s|ms|Hz|units?|a\.u\.|mg|g|ml|mL)"
UNIT_ONLY_RE = re.compile(rf"^[\(\[]?\s*{UNIT_TOKEN}\s*[\)\]]?$", re.I)
UNIT_IN_HEADER_RE = re.compile(rf"[\(\[]\s*{UNIT_TOKEN}\s*[\)\]]\s*$", re.I)

MARGIN_RATIO = 0.07          # 頁面上下 7% 視為頁首／頁尾區
SIDE_MARGIN_RATIO = 0.06     # 頁面左右 6% 內的直排文字 = 邊緣文字（例如出版商的下載浮水印）
ORPHAN_TABLE_FRAGMENT_CHARS = 40   # 表格本體沒解析出來的頁面：短於此字數的段落視為可能的儲存格殘片
ORPHAN_TABLE_MIN_FRAGMENTS = 8    # 同頁至少這麼多短段落才標記（避免正常頁面的零星短句）
# 02_clean 判斷段落接續時以這句話辨認殘片並略過（兩邊字串必須一致，見 tests/test_continuation.py）
TABLE_FRAGMENT_REASON = "同頁有找不到本體的表格標題，此短段落疑似未解析表格的殘片，請對照 PDF"
TABLE_TEXT_REASON = "多行且以數字為主，疑似未解析的表格內容（欄名與數值的對應已遺失），請對照 PDF"
TABLE_TEXT_MIN_LINES = 5           # 至少這麼多行才判斷（避免「共 3 組，各 12 人」這類短句）
TABLE_TEXT_NUMERIC_SHARE = 0.5     # 數字詞占全部詞的比例
TABLE_TEXT_NUMERIC_END_SHARE = 0.8  # 或：這麼多行都以數字結尾（「Dark red 25」「Deep red 1」）
TABLE_TEXT_NUMERIC_ROW_SHARE = 0.4  # 或：這麼多行整行只有數字（名稱、數字各占一行的兩欄表）
TABLE_TEXT_FRAGMENT_REASON = "同頁有疑似未解析的表格內容，此短段落疑似表頭或表格殘片，請對照 PDF"
TABLE_TEXT_FRAGMENT_CHARS = 100    # 上述頁面中，短於此字數且沒有句尾標點的段落視為表頭／殘片
NUMERIC_TOKEN_RE = re.compile(r"^[(（]?[<>≤≥~±+\-−]?\d+(?:[.,]\d+)*%?[)）]?$")
PARA_GAP_FACTOR = 1.4        # 行距空白 > 1.4 × 字級 → 新段落
FORMULA_GAP_FACTOR = 2.5     # 公式行（含上下標、積分上下限）之間允許較大間距
FIGURE_MIN_SIZE = (100, 60)  # 向量繪圖叢集最小寬高 (pt)，用來排除逐行底色框
IMAGE_MIN_SIZE = 30          # 小於 30pt 的圖片（圖示、項目符號）不建立區域
FIGURE_TEXT_MAX_LEN = 60     # 圖表區內長度 <= 60 的行視為圖表文字；更長的仍當正文
TEXT_BOX_LONG_SHARE = 0.75   # 繪圖區內 >= 75% 的字在長行（> FIGURE_TEXT_MAX_LEN）裡 → 是正文底色框，不是圖表
TEXT_BOX_MIN_LONG_LINES = 3
# 寫滿框寬的「文句行」（TX001 教材面板寬約 320pt，每行只有 55–65 字，達不到 > 60 字的長行門檻）：
# 至少 20 字、行寬 >= 區域寬 70%、字母占非空白字元 >= 60%（數字表格列不算）、字級 >= 正文 90%（註腳、引用框不算）
PROSE_MIN_CHARS = 20
PROSE_WIDE_SHARE = 0.7
PROSE_LETTER_SHARE = 0.6
PROSE_MIN_SIZE_RATIO = 0.9
# 版面裝飾（不參與圖表叢集）：面積 >= 頁面 85% 或超出頁面 1pt 以上的底色／外框；
# 純矩形填色且框內有 >= 3 行、>= 100 字的文字底色框
DECOR_PAGE_AREA_SHARE = 0.85
DECOR_BLEED = 1.0
DECOR_FILL_MIN_LINES = 3
DECOR_FILL_MIN_CHARS = 100
WATERMARK_BOX_TOLERANCE = 4  # 圖片與浮水印四邊誤差總和 < 4pt → 同一張浮水印
MARGIN_BODY_GAP = 0.5        # 頁首／頁尾區的行與相鄰正文行距 <= 0.5 × 字級 → 同一段正文，不是頁首頁尾
TABLE_MIN_FILL = 0.5         # 表格非空儲存格比例低於此值 → 判定無法辨識
TABLE_SPARSE_MIN_FILL = 0.3  # 稀疏表格（事後檢定等）的下限：還需文字 100% 落在儲存格中，見 sparse_table_ok
TABLE_MIN_COVERAGE = 0.98    # 表格範圍內文字落入儲存格的最低比例
PAGE_MIN_COVERAGE = 0.98     # 頁面文字層被 block 收錄的最低比例
CAPTION_LINK_DIST = 60       # 標題與表格／圖片的最大垂直距離 (pt)
CENTER_TOLERANCE = 30        # 非粗體的中文圖表標題必須置中（行中心與頁面中心差 <= 30pt）
PAGE_NUMBER_ZONE = 0.15      # 置中的純頁碼若落在頁面底部 15% 內 → 頁尾
PAGE_NUMBER_ONLY_RE = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]{1,7})\s*$", re.I)
WATERMARK_MIN_SHARE = 0.5    # 同一張圖片出現在 >= 50% 的頁面（且 >= 3 頁）→ 浮水印
# 頁面標籤（TX001 每頁頁邊欄的粗體「Notes」）：<= 30 字、在頁首頁尾區以外、同一文字在 >= 30% 的頁面
# 出現在同一位置（四邊誤差 < 6pt；奇偶頁可各有一個位置，每個位置至少 3 頁）。不是章節標題
RUNNING_LABEL_MAX_CHARS = 30
RUNNING_LABEL_MIN_SHARE = 0.3
RUNNING_LABEL_TOLERANCE = 6.0
# 句子沒寫完的結尾虛詞：標題不會停在這些字上（「…satisfaction with」「…appeal pleasing to」）
UNFINISHED_TAIL_WORDS = frozenset({
    "a", "an", "the", "to", "of", "and", "or", "but", "with", "for", "in", "on", "at", "by", "from",
    "as", "if", "that", "which", "is", "are", "was", "were", "be", "can", "will", "should", "than", "into"})
COLUMN_GAP_FACTOR = 1.5      # 同一列字與字之間空隙 > 1.5 × 字級 → 視為欄距
RUN_IN_LABEL_MAX_LEN = 60    # 行首粗體引言（「1. Vein test:」「Blusher:」）最長字數；更長的粗體不是引言
DISPLAY_HEADING_DELTA = 1.5  # 章名（Glossary 等）的最大字級 >= 內文 + 1.5pt → 雖未置中仍是標題（首字放大的版式）


def make_doc_id(path: Path) -> str:
    """由檔名產生穩定的 doc_id（只保留英數、底線、連字號）。"""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_\-]+", "_", path.stem)).strip("_") or "doc"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_posix(path: Path) -> str:
    """manifest.source_path：相對於程式根目錄；不在程式根目錄時改相對於 LIPMATCH_DATA_ROOT（程式與資料分開放時讀資料根目錄的
    data/raw）。兩者都不是才保留絕對路徑——那份輸出綁這台電腦，ingest 換根目錄就讀不到（2026-10-23 修復 A／D20）。"""
    path = path.resolve()
    bases = [ROOT]
    if os.environ.get("LIPMATCH_DATA_ROOT"):
        bases.append(Path(os.environ["LIPMATCH_DATA_ROOT"]).resolve())
    for base in bases:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def ensure_outside_raw(path: Path) -> None:
    """防呆：任何輸出都不得寫進 data/raw。"""
    p = path.resolve()
    raw = RAW_DIR.resolve()
    if p == raw or raw in p.parents:
        raise ValueError(f"拒絕寫入原始資料夾：{p}")


def parse_page_spec(spec: Optional[str], total: int) -> list[int]:
    """'1-12' / '1,3,5-7' → [1..12]；None → 全部頁。頁碼為 1-based。"""
    if not spec:
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            pages.update(range(a, b + 1))
        else:
            pages.add(int(part))
    bad = [p for p in pages if p < 1 or p > total]
    if bad:
        raise ValueError(f"頁碼超出範圍 1..{total}: {sorted(bad)}")
    return sorted(pages)


def heading_level_for(text: str) -> Optional[int]:
    """依編號推斷標題層級：Chapter N / Abstract → 1，2.1 → 2，2.1.1 → 3 … 最多 6。"""
    if CHAPTER_RE.match(text) or FRONT_MATTER_RE.match(text):
        return 1
    m = NUMBERED_HEADING_RE.match(text)
    # 編號後面要有文字（英文或中文），排除「0.977 0.043 -0.028」這種數字列
    if m and re.search(r"[A-Za-z一-鿿]", text[m.end(1):]):
        return min(len(m.group(1).split(".")), 6)
    return None


class SectionTracker:
    """維護章節堆疊，產生 'Chapter 2 > 2.1 Colour Science' 這類路徑。"""

    def __init__(self) -> None:
        self.stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        title = "".join(ch for ch in title if ch not in "\u00ad\u200b\u200c\u200d\u2060\ufeff")
        self.stack.append((level, " ".join(title.split())))

    @property
    def path(self) -> Optional[str]:
        return " > ".join(t for _, t in self.stack) or None


def norm_cell(c: Optional[str]) -> Optional[str]:
    """儲存格比對用：壓空白；空字串視為 None。只用於結構判斷，不覆寫 table_data。"""
    if c is None:
        return None
    s = " ".join(str(c).split())
    return s or None


def is_numeric_cell(c: Optional[str]) -> bool:
    c = norm_cell(c)
    return bool(c) and bool(NUMERIC_CELL_RE.match(c))


def _is_units_row(row: list[Optional[str]]) -> bool:
    vals = [norm_cell(c) for c in row if norm_cell(c)]
    return bool(vals) and all(UNIT_ONLY_RE.match(v) for v in vals)


# --- 標題框（教材的「Application of Foundation」這類有色底框：一列標題 + 下方內容）--------------
STEP_NUMBER_RE = re.compile(r"^(\d{1,2})\.$")
BOX_TITLE_MAX_CHARS = 80
BOX_STEP_MIN = 3


def is_box_title_row(row: list[Optional[str]]) -> bool:
    """標題框的標題列：只有第一格有字，其餘是被合併掉的格（find_tables 回傳 None，不是空字串），
    標題短且不以句讀結尾（不是一句正文）。"""
    title = norm_cell(row[0]) if row else None
    return (bool(title) and len(row) >= 2 and all(c is None for c in row[1:])
            and len(title) <= BOX_TITLE_MAX_CHARS and not re.search(r"[.!?:;。]$", title))


def _is_blank_row(row: list[Optional[str]]) -> bool:
    return all(not norm_cell(c) for c in row)


def split_stacked_boxes(rows: list[list[Optional[str]]], boxes: Optional[list[list[Optional[tuple]]]],
                        ) -> list[tuple[list[list[Optional[str]]], Optional[list[list[Optional[tuple]]]]]]:
    """框線偵測把上下相鄰的兩個標題框認成同一張表時，切回各自的框。

    參數：
        rows：find_tables 的儲存格文字。
        boxes：與 rows 同形狀的儲存格座標（可為 None）。

    回傳：
        [(rows, boxes), ...]；不需要切時回傳原樣一份。

    切點：第一列是標題列（見 `is_box_title_row`），且中間有「全空白列，下一列又是標題列」。
    每一段去掉頭尾的全空白列，至少要剩 2 列，否則整張不切（避免切出無意義的碎片）。
    TX001 p.26：「Application of Foundation」11 步與「Application of Concealer」是兩個框，
    原本合成一張 16 列的表，遮瑕的內容被當成粉底表的最後幾列。
    """
    if not rows or not is_box_title_row(rows[0]):
        return [(rows, boxes)]
    cuts = [i for i in range(1, len(rows) - 1) if _is_blank_row(rows[i]) and is_box_title_row(rows[i + 1])]
    if not cuts:
        return [(rows, boxes)]
    pieces = []
    start = 0
    for end in cuts + [len(rows)]:
        idx = list(range(start, end))
        while idx and _is_blank_row(rows[idx[-1]]):
            idx.pop()
        while idx and _is_blank_row(rows[idx[0]]):
            idx.pop(0)
        if len(idx) < 2:
            return [(rows, boxes)]
        pieces.append(([rows[i] for i in idx], [boxes[i] for i in idx] if boxes else None))
        start = end + 1
    return pieces


def titled_step_rows(rows: list[list[Optional[str]]]) -> Optional[list[int]]:
    """「標題列 + 編號步驟」的標題框（TX001 p.26 Application of Foundation）。

    參數：
        rows：表格儲存格。

    回傳：
        步驟編號清單（1..n）；不是這種框時回傳 None。

    條件（全部成立才算）：恰 2 欄；第一列是標題列；其後 >= BOX_STEP_MIN 列，每列第一格是「n.」、
    第二格有字；編號從 1 開始連續不跳號（跳號或重複代表漏列或錯位，交人工確認）。
    """
    if len(rows) < 1 + BOX_STEP_MIN or any(len(r) != 2 for r in rows) or not is_box_title_row(rows[0]):
        return None
    nums = []
    for r in rows[1:]:
        m = STEP_NUMBER_RE.match(norm_cell(r[0]) or "")
        if not m or not norm_cell(r[1]):
            return None
        nums.append(int(m.group(1)))
    return nums if nums == list(range(1, len(nums) + 1)) else None


def table_quality(rows: list[list[Optional[str]]]) -> tuple[bool, dict[str, str]]:
    """判斷候選表格是否可信：至少 2×2，且非空儲存格比例 >= TABLE_MIN_FILL。

    向量圖表常被 find_tables 誤判成表格，特徵是大量空儲存格。
    """
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    cells = [c for r in rows for c in r]
    filled = sum(1 for c in cells if norm_cell(c))
    fill = filled / len(cells) if cells else 0.0
    stats = {"rows": str(n_rows), "cols": str(n_cols), "fill_ratio": f"{fill:.2f}"}
    ok = n_rows >= 2 and n_cols >= 2 and fill >= TABLE_MIN_FILL
    return ok, stats


def sparse_table_ok(rows: list[list[Optional[str]]], rect: pymupdf.Rect, words: list[tuple], coverage: float) -> bool:
    """非空格比例 < TABLE_MIN_FILL 的候選，若符合以下條件仍視為真表格：
    非空格 >= TABLE_SPARSE_MIN_FILL、至少 3×3、範圍內確實有字，且這些字 100% 落在儲存格中。
    被誤判成表格的向量圖表：非空格通常 <= 0.16，或軸標籤數字落在格子外（覆蓋率低）。"""
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    cells = [c for r in rows for c in r]
    filled = sum(1 for c in cells if norm_cell(c))
    has_words = any(rect.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for w in words)
    return (n_rows >= 3 and n_cols >= 3 and has_words and coverage >= TABLE_MIN_COVERAGE
            and bool(cells) and filled / len(cells) >= TABLE_SPARSE_MIN_FILL)


def build_table_structure(
    rows: list[list[Optional[str]]],
    method: str,
    cell_bboxes: Optional[list[list[Optional[tuple]]]] = None,
    header_rows_hint: Optional[int] = None,
) -> tuple[TableStructure, list[str]]:
    """從儲存格推出欄名、列名、單位；回傳 (TableStructure, 需人工確認的原因)。

    - 表頭：第一列以文字為主、下方有數值 → 1 列表頭（HTML 可用 <th>/<thead> 提示）。
    - 單位：表頭下一列若全是單位（nm、K、%、°…）→ 單位列；或欄名結尾的 (nm)、[K]。
    - 列名：資料列第一欄以文字為主 → row_labels。
    - 空白儲存格保持空值並計數，不推測。
    """
    reasons: list[str] = []
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    steps = titled_step_rows(rows) if header_rows_hint is None else None
    if steps is not None:
        # 標題框：標題是跨兩欄的合併表頭（攤平成每欄同名），步驟編號是列名；
        # 標題列第二格是合併格不是缺值，資料區每格都有字，沒有需要人工確認的結構疑點
        title = norm_cell(rows[0][0])
        return TableStructure(
            detection_method=method, n_rows=n_rows, n_cols=n_cols, header_rows=1,
            column_names=[title] * n_cols, row_labels=[f"{n}." for n in steps], units={},
            cell_bboxes=[[tuple(round(v, 1) for v in c) if c else None for c in r] for r in cell_bboxes]
            if cell_bboxes else None,
            empty_cells=0,
        ), []
    if any(len(r) != n_cols for r in rows):
        reasons.append("各列欄數不一致，欄位可能錯位")

    def text_ratio(row: list[Optional[str]]) -> float:
        vals = [norm_cell(c) for c in row if norm_cell(c)]
        return sum(not is_numeric_cell(v) for v in vals) / len(vals) if vals else 0.0

    def has_numeric(rs: list[list[Optional[str]]]) -> bool:
        return any(is_numeric_cell(c) for r in rs for c in r)

    if header_rows_hint is not None:
        name_rows = header_rows_hint
    else:
        name_rows = 0
        if n_rows >= 2 and text_ratio(rows[0]) >= 0.5 and has_numeric(rows[1:]):
            name_rows = 1
            if (n_rows >= 3 and not _is_units_row(rows[1]) and text_ratio(rows[1]) >= 0.5
                    and not has_numeric([rows[1]]) and has_numeric(rows[2:])):
                name_rows = 2

    units: dict[str, str] = {}
    header_rows = name_rows
    if name_rows and n_rows > name_rows and _is_units_row(rows[name_rows]):
        for j, c in enumerate(rows[name_rows]):
            if norm_cell(c):
                units[str(j)] = norm_cell(c).strip("()[] ")
        header_rows += 1

    body = rows[header_rows:]
    first_col = [norm_cell(r[0]) if r else None for r in body]
    vals = [v for v in first_col if v]
    row_labels = first_col if vals and sum(not is_numeric_cell(v) for v in vals) / len(vals) >= 0.5 else None

    column_names: Optional[list[Optional[str]]] = None
    if name_rows:
        column_names = []
        for j in range(n_cols):
            parts = [norm_cell(rows[r][j]) for r in range(name_rows) if j < len(rows[r]) and norm_cell(rows[r][j])]
            column_names.append(" / ".join(parts) if parts else None)
        for j, name in enumerate(column_names):
            if name and str(j) not in units:
                m = UNIT_IN_HEADER_RE.search(name)
                if m:
                    units[str(j)] = m.group(1)
        missing = [j for j, n in enumerate(column_names) if n is None and not (j == 0 and row_labels)]
        if missing:
            reasons.append(f"第 {missing} 欄沒有欄名（可能是合併儲存格），請確認欄位對應")
    else:
        reasons.append("未辨識出表頭（欄名），請人工確認各欄意義")

    empty = sum(1 for r in body for c in r if not norm_cell(c))
    if empty:
        reasons.append(f"資料區有 {empty} 個空白儲存格，已保留為空值、未補值，請對照原表確認")

    ts = TableStructure(
        detection_method=method, n_rows=n_rows, n_cols=n_cols, header_rows=header_rows,
        column_names=column_names, row_labels=row_labels, units=units,
        cell_bboxes=[[tuple(round(v, 1) for v in c) if c else None for c in r] for r in cell_bboxes]
        if cell_bboxes else None,
        empty_cells=empty,
    )
    return ts, reasons


def serialize_table(rows: list[list[Optional[str]]]) -> str:
    """表格轉成可讀文字（每列一行，欄位以 ' | ' 分隔），作為 raw_text。"""
    return "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)


def char_counter(text: str) -> Counter:
    return Counter(ch for ch in text if not ch.isspace())


# ---------------------------------------------------------------------------
# PDF：讀取行、文字、區域
# ---------------------------------------------------------------------------

@dataclass
class Line:
    """PDF 上的一條「視覺行」（同一高度的 span 已合併）。"""

    text: str
    bbox: pymupdf.Rect
    size: float
    bold_all: bool
    first_bold: bool
    fonts: set[str] = field(default_factory=set)
    math_chars: int = 0
    total_chars: int = 0
    garbled_math: bool = False
    symbol_chars: int = 0      # Symbol 字型（希臘字母、數學符號）的字數
    col: Optional[int] = None  # 雙欄版面：0 左欄、1 右欄、-1 跨欄；單欄為 None
    rotated: bool = False      # 非水平書寫（直排）；不和水平行合併
    max_size: float = 0.0      # 行內最大字級（首字放大、小型大寫的章名「GLOSSARY」看得出來；size 是加權眾數）
    run_in_label: bool = False  # 行首是粗體引言、以冒號結束、後面接非粗體正文（見 is_run_in_label）
    running_label: bool = False  # 多頁重複出現在同一位置的頁面標籤（見 find_running_labels）；不是章節
    spans: list = field(default_factory=list, repr=False, compare=False)  # 組成這一行的 span（拆行用）

    @property
    def has_math_font(self) -> bool:
        return self.math_chars > 0

    @property
    def math_ratio(self) -> float:
        return self.math_chars / self.total_chars if self.total_chars else 0.0

    @property
    def symbol_formula(self) -> bool:
        return self.total_chars > 0 and self.symbol_chars >= SYMBOL_FORMULA_SHARE * self.total_chars


@dataclass
class TableCandidate:
    rect: pymupdf.Rect
    rows: list[list[Optional[str]]]
    method: str
    cell_bboxes: Optional[list[list[Optional[tuple]]]] = None
    coverage: float = 1.0
    reasons: list[str] = field(default_factory=list)  # 對齊過程發現的疑點


@dataclass
class ColumnLayout:
    """單頁的欄位版面。

    split_x：左右欄的分界（欄間空白帶中線）；None = 單欄。
    spanning_y：跨欄元素（標題、跨欄圖片…）的 y0，用來把頁面切成段落區間。
    閱讀順序：跨欄元素（標題、跨欄圖表）是分界；每個區間先讀左欄、再讀右欄。
    """

    split_x: Optional[float] = None
    spanning_y: list[float] = field(default_factory=list)

    def col_of(self, r: pymupdf.Rect) -> Optional[int]:
        if self.split_x is None:
            return None
        if r.x0 < self.split_x - 3 and r.x1 > self.split_x + 3:
            return -1
        return 0 if (r.x0 + r.x1) / 2 < self.split_x else 1

    def key(self, r: pymupdf.Rect) -> tuple:
        """排序鍵：單欄 = 由上而下；雙欄 = (區間, 欄, y, x)。"""
        if self.split_x is None:
            return (0, 0, 0, r.y0, r.x0)
        c = self.col_of(r)
        if c == -1:
            # 跨欄元素本身就是一條分界：歸到它下方那一段的最前面（容許 10pt 誤差，
            # 例如表格文字比表格底色低幾點）
            return (bisect.bisect_left(self.spanning_y, r.y0 + 10), 0, 0, r.y0, r.x0)
        seg = bisect.bisect_left(self.spanning_y, r.y0 - 0.5)
        return (seg, 1, c, r.y0, r.x0)


def detect_columns(rects: list[pymupdf.Rect], page_rect: pymupdf.Rect) -> ColumnLayout:
    """偵測雙欄：在頁寬 30%–70% 之間找一條垂直分界，使「像正文的行」（寬度 >= 15% 頁寬，含窄側欄）
    左右兩側各至少 5 行，且跨過分界的行 <= 40%（頁首常有跨欄的摘要框）。單欄論文的正文行都跨過中線，所以不會被判成雙欄；
    圖表的短標籤寬度不足，不參與判斷。"""
    W = page_rect.width
    wide = [r for r in rects if r.width >= 0.15 * W and r.height < 40]
    if len(wide) < 10:
        return ColumnLayout()
    best: list[tuple[int, int]] = []
    for x in range(int(W * 0.3), int(W * 0.7) + 1):
        left = sum(1 for r in wide if r.x1 <= x)
        right = sum(1 for r in wide if r.x0 >= x)
        cross = len(wide) - left - right
        if left >= 5 and right >= 5 and cross <= 0.4 * len(wide):
            best.append((cross, x))
    if not best:
        return ColumnLayout()
    fewest = min(c for c, _ in best)
    xs = [x for c, x in best if c == fewest]
    return ColumnLayout(split_x=(xs[0] + xs[-1]) / 2)


def _is_bold(span: dict) -> bool:
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()


def _has_caption_body(text: str) -> bool:
    """圖說開頭的文字在標籤（Figure 1.2: / FIGURE 1）之後還有說明文字（至少兩個字母）。"""
    m = CAPTION_RE.match(text)
    return bool(m) and len(re.findall(r"[A-Za-z一-鿿]", text[m.end():])) >= 2


def _caption_under_side_image(cap: pymupdf.Rect, other: pymupdf.Rect, images: list[pymupdf.Rect]) -> bool:
    """圖說在左、緊貼在一張圖正下方且寬度落在圖內，旁邊的文字整段在這張圖的右邊（TX001 p.17 的版面）。

    只有這種版面才把同高的圖說與正文拆開。其他同高情況（圖說在右、正文在左，或沒有圖）先不動：
    2026-10-18 試過不分版面一律拆開，TX001 p.38、p.59、p.88 會產生新的錯接或把條列變成標題。
    2026-10-22 再試只加「正文在左、圖與圖說在右」的鏡像版面，TX001 p.38、p.50、p.62、p.74、p.88
    仍產生新錯接（圖說吃進正文半句、條列只剩半句），所以不在這裡改；表格旁的圖說改在
    `rescue_captions_from_table_lines` 處理（只動被表格吃掉的那一行）。
    """
    if cap.x1 > other.x0:
        return False
    return any(img.x0 - 5 <= cap.x0 and cap.x1 <= img.x1 + 5 and img.x1 <= other.x0 + 2
               and -5 <= cap.y0 - img.y1 <= 20 for img in images)


def _is_math_font(font: str) -> bool:
    return "math" in font.lower()


def _join_spans(spans: list[dict]) -> str:
    """依 x 座標串接 span；兩個 span 之間有明顯水平空隙時補一個空白。"""
    out = ""
    prev = None
    for s in spans:
        t = s["text"]
        if prev is not None and out and not out[-1].isspace() and not t[:1].isspace():
            gap = s["bbox"][0] - prev["bbox"][2]
            if gap > 0.2 * s["size"]:
                out += " "
        out += t
        prev = s
    return out


SIDE_BY_SIDE_MIN_PAIRS = 2   # 頁面至少要有這麼多組「左右欄文字同高並排」才沿用文件的欄位分界
DOC_COLUMN_MIN_SHARE = 0.5   # 至少這麼多比例的頁面偵測為雙欄，才認定整份文件是雙欄


def side_by_side_pairs(rects: list[pymupdf.Rect], split_x: float) -> int:
    """數一數有幾組「左欄一段、右欄一段」的文字同高並排，且距離近到會被合併成同一行（< 40pt）。

    參數：
        rects：頁面上每個水平文字單位（PyMuPDF 的 line）的範圍。
        split_x：候選的欄位分界 x。

    回傳：
        並排組數。單欄頁面的正文行都跨過分界，不會被計入；每個左欄單位最多算一組。
    """
    left = [r for r in rects if r.x1 <= split_x + 3 and r.height < 40]
    right = [r for r in rects if r.x0 >= split_x - 3 and r.height < 40]
    pairs = 0
    for a in left:
        for b in right:
            overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
            if overlap >= 0.5 * min(a.height, b.height) and 0 <= b.x0 - a.x1 < 40:
                pairs += 1
                break
    return pairs


def document_column_split(pdf: pymupdf.Document, pages: list[int]) -> Optional[float]:
    """整份文件的欄位分界：過半頁面偵測為雙欄時，取各頁分界的中位數；否則 None（單欄文件）。

    用途：圖表佔大半的頁面正文行太少，detect_columns 判不出雙欄，左右欄同高的行會被合併成一行
    （AR012 p.3 頁頂、p.7／p.8 頁尾）。read_pdf_lines 會在這種頁面驗證並沿用這個分界。
    """
    splits = []
    for pno in pages:
        _, _, layout = read_pdf_lines(pdf[pno - 1])
        if layout.split_x is not None:
            splits.append(layout.split_x)
    if not pages or len(splits) < DOC_COLUMN_MIN_SHARE * len(pages):
        return None
    splits.sort()
    return splits[len(splits) // 2]


def is_run_in_label(visible: list[dict]) -> bool:
    """行首是否為「粗體引言＋冒號＋非粗體正文」的版式（run-in heading）。

    參數：
        visible：同一視覺行、由左到右排好的非空白 span（get_text("dict") 格式）。

    回傳：
        True 表示：行首連續的粗體 span 合起來以冒號結尾、不超過 RUN_IN_LABEL_MAX_LEN 字、含英文字母，
        且同一行後面還有含英文字母的非粗體文字。
        例：TX001 p.35「**1. Vein test:** Observe for …」、p.99 詞彙表「**Blusher:** Used to …」。

    注意：
        這種行是段落的開頭，不是標題：編號引言不可因「1.」編號被判成標題（見 _line_kind），
        詞彙表裡每一個引言是新詞條的開頭（見 group_lines）。
    """
    lead = ""
    for s in visible:
        if not _is_bold(s):
            break
        lead += s["text"]
    lead = lead.strip()
    rest = "".join(s["text"] for s in visible if not _is_bold(s))
    return (lead.endswith(":") and len(lead) <= RUN_IN_LABEL_MAX_LEN and bool(re.search(r"[A-Za-z]", lead))
            and bool(re.search(r"[A-Za-z]", rest)))


def _label_candidates(page: pymupdf.Page) -> list[tuple[str, pymupdf.Rect]]:
    """一頁中可能是頁面標籤的短行：PyMuPDF 的單一水平行、<= RUNNING_LABEL_MAX_CHARS 字、含英文或中文、
    在頁首頁尾區（上下 MARGIN_RATIO）以外。頁首頁尾區的重複文字已由 zone() 處理，這裡不重複判斷。"""
    h = page.rect.height
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            if abs(ln["dir"][1]) >= 0.1 or ln["dir"][0] <= 0:
                continue
            text = " ".join("".join(s["text"] for s in ln["spans"]).split())
            r = pymupdf.Rect(ln["bbox"])
            if (text and len(text) <= RUNNING_LABEL_MAX_CHARS and re.search(r"[A-Za-z一-鿿]", text)
                    and r.y0 > h * MARGIN_RATIO and r.y1 < h * (1 - MARGIN_RATIO)):
                out.append((text, r))
    return out


def find_running_labels(pdf: pymupdf.Document, pages: list[int]) -> list[tuple[str, pymupdf.Rect]]:
    """找出整份文件的頁面標籤（例如 TX001 頁邊欄每頁都有的「Notes」）。

    同一文字在同一位置（四邊誤差 < RUNNING_LABEL_TOLERANCE）出現 >= 3 頁算一個位置；
    該文字所有位置合計 >= RUNNING_LABEL_MIN_SHARE 的頁面（且 >= 3 頁）才算頁面標籤。
    回傳 [(文字, 代表位置)]，依文字、位置排序（讓輸出可逐字重現）。
    """
    seen: dict[str, list[tuple[pymupdf.Rect, set[int]]]] = {}
    for pno in pages:
        for text, r in _label_candidates(pdf[pno - 1]):
            spots = seen.setdefault(text, [])
            for rect, on_pages in spots:
                if same_box(rect, r, RUNNING_LABEL_TOLERANCE):
                    on_pages.add(pno)
                    break
            else:
                spots.append((r, {pno}))
    need = max(3, RUNNING_LABEL_MIN_SHARE * len(pages))
    out = []
    for text, spots in seen.items():
        kept = [(rect, on_pages) for rect, on_pages in spots if len(on_pages) >= 3]
        pages_with_label: set[int] = set()
        for _, on_pages in kept:
            pages_with_label |= on_pages
        if kept and len(pages_with_label) >= need:
            out += [(text, rect) for rect, _ in kept]
    return sorted(out, key=lambda tr: (tr[0], tr[1].y0, tr[1].x0))


def is_running_label(text: str, r: pymupdf.Rect, labels: Optional[list[tuple[str, pymupdf.Rect]]]) -> bool:
    """這段文字與位置是否是已知的頁面標籤。"""
    t = " ".join(text.split())
    return bool(labels) and any(t == lt and same_box(r, lr, RUNNING_LABEL_TOLERANCE) for lt, lr in labels)


def _build_line(text: str, r: pymupdf.Rect, spans: list[dict], col: Optional[int], rotated: bool,
                running_label: bool) -> Line:
    """由合併好的 span 建一條 Line（字級眾數、粗體、數學字型等統計）；spans 一併保留供之後拆行。"""
    visible = [s for s in spans if s["text"].strip()]
    weights: Counter = Counter()
    for s in visible:
        weights[round(s["size"], 1)] += len(s["text"].strip())
    math_spans = [s for s in visible if _is_math_font(s["font"])]
    return Line(
        text=text, bbox=r, size=weights.most_common(1)[0][0],
        bold_all=all(_is_bold(s) for s in visible), first_bold=_is_bold(visible[0]),
        fonts={s["font"] for s in visible},
        math_chars=sum(len(s["text"].strip()) for s in math_spans),
        total_chars=sum(len(s["text"].strip()) for s in visible),
        # 串起整行的數學字型文字再檢查：上標常被存成獨立 span（"(𝑎∗)" + "*"），逐段看不到前文
        garbled_math=bool(GARBLED_MATH_RE.search("".join(s["text"] for s in math_spans))),
        symbol_chars=sum(len(s["text"].strip()) for s in visible if "symbol" in s["font"].lower()),
        col=col,
        rotated=rotated,
        max_size=max(s["size"] for s in visible),
        run_in_label=is_run_in_label(visible),
        running_label=running_label,
        spans=list(spans),
    )


def rescue_captions_from_table_lines(lines: list[Line], table_rects: list[pymupdf.Rect]) -> list[Line]:
    """表格列與表格外的圖說同高、被併成同一行時，把表格外的圖說拆回獨立一行。

    參數：
        lines：read_pdf_lines 的結果。
        table_rects：本頁已接受的表格範圍。

    回傳：
        新的行清單：被拆的行換成「表格內部分（之後照常因在表格內而略過）」與「表格外的圖說行」兩行；其他行原樣。

    只處理：表格外的 span 全部在表格範圍之外（不相交），且串起來以圖表標題開頭（CAPTION_RE）。
    TX001 p.26：「Figure 1.14: Application of」在粉底表第 2 步右邊同高，合併後的行有 6 成以上落在表格內，
    整行被當成表格文字丟掉，只剩第二行「cream foundation」落在表格旁，害粉底表被判 partial、圖說只剩半句。
    不在 read_pdf_lines 的合併規則改（那裡還不知道表格位置；2026-10-22 試過在那裡加鏡像規則，見
    `_caption_under_side_image`，會在沒有表格的頁面產生新錯接）。
    """
    if not table_rects:
        return lines
    out: list[Line] = []
    for ln in lines:
        if ln.rotated or not ln.spans or not any(r.intersects(ln.bbox) for r in table_rects):
            out.append(ln)
            continue
        inside = [s for s in ln.spans if any(r.intersects(pymupdf.Rect(s["bbox"])) for r in table_rects)]
        outside = [s for s in ln.spans if s not in inside]
        cap_text = _join_spans(outside).strip() if inside and outside else ""
        if not CAPTION_RE.match(cap_text):
            out.append(ln)
            continue
        for part in (inside, outside):
            r = pymupdf.Rect(part[0]["bbox"])
            for s in part[1:]:
                r |= pymupdf.Rect(s["bbox"])
            out.append(_build_line(_join_spans(part), r, part, ln.col, False, False))
    return out


def read_pdf_lines(page: pymupdf.Page, column_hint: Optional[float] = None,
                   running_labels: Optional[list[tuple[str, pymupdf.Rect]]] = None,
                   ) -> tuple[list[Line], list[pymupdf.Rect], ColumnLayout]:
    """讀出頁面所有視覺行（依閱讀順序）、圖片區域與欄位版面。

    PyMuPDF 對 macOS Quartz 產生的 PDF 常把每一行、甚至同一行的不同字型拆成獨立 block，
    因此這裡依垂直重疊把 span 重新組成行。水平距離 >= 40pt 的不合併（表格欄）；
    雙欄版面中，不同欄的文字即使同高也不合併（欄間距常只有 10–20pt）。
    直排文字（例如 Wiley 頁面右緣的下載浮水印）外框幾乎和整頁等高，若參與合併會被併進
    右欄每一行，因此各自成行、不參與欄位判斷，交給 zone() 決定是否為邊緣文字。

    column_hint：整份文件的欄位分界（document_column_split）。本頁偵測不出雙欄、但確實有
    >= SIDE_BY_SIDE_MIN_PAIRS 組左右欄文字同高並排時，改用這個分界，避免左右欄被合併成一行。

    running_labels：整份文件的頁面標籤（find_running_labels）。標籤各自成行、不和同高的文字合併
    （TX001 p.25「Notes」與標題「General Guidelines for a Beauty Therapist」原本被併成同一行），
    並標上 Line.running_label。
    """
    d = page.get_text("dict")
    images: list[pymupdf.Rect] = []
    units: list[list[dict]] = []
    rotated_units: list[list[dict]] = []
    for b in d["blocks"]:
        if b["type"] == 1:
            images.append(pymupdf.Rect(b["bbox"]))
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"]]
            for s in spans:
                s["text"] = map_symbol_font(s["text"], s["font"])
            if spans:
                horizontal = abs(ln["dir"][1]) < 0.1 and ln["dir"][0] > 0
                (units if horizontal else rotated_units).append(spans)

    def ubox(u: list[dict]) -> pymupdf.Rect:
        r = pymupdf.Rect(u[0]["bbox"])
        for s in u[1:]:
            r |= pymupdf.Rect(s["bbox"])
        return r

    units.sort(key=lambda u: (ubox(u).y0, ubox(u).x0))
    layout = detect_columns([ubox(u) for u in units], page.rect)
    if (layout.split_x is None and column_hint is not None
            and side_by_side_pairs([ubox(u) for u in units], column_hint) >= SIDE_BY_SIDE_MIN_PAIRS):
        layout = ColumnLayout(split_x=column_hint)
    groups: list[tuple[pymupdf.Rect, list[dict]]] = []
    label_ids: set[int] = set()   # 頁面標籤的 group：不和其他文字合併
    for u in units:
        r = ubox(u)
        if is_running_label("".join(s["text"] for s in u), r, running_labels):
            groups.append((r, list(u)))
            label_ids.add(len(groups) - 1)
            continue
        placed = False
        for i, (gr, gspans) in enumerate(groups):
            if i in label_ids:
                continue
            overlap = min(gr.y1, r.y1) - max(gr.y0, r.y0)
            hgap = max(r.x0 - gr.x1, gr.x0 - r.x1, 0)
            same_col = layout.split_x is None or layout.col_of(gr) == layout.col_of(r)
            # 圖下方的圖說與旁邊正文同高（TX001 p.17「Figure 1.2: Cream foundation」與右欄「brand of foundation …」）：
            # 一邊是「圖說標籤＋說明文字」的完整圖說、另一邊不是圖說，且中間空隙超過一個字寬 → 是兩段不同的文字。
            # 只有標籤（AR001「FIGURE 1」與右邊的說明文字分成兩個單位）時照舊合併。
            u_text, g_text = "".join(s["text"] for s in u), "".join(s["text"] for s in gspans)
            caption_split = (hgap > max(s["size"] for s in u)
                             and bool(CAPTION_RE.match(u_text)) != bool(CAPTION_RE.match(g_text))
                             and any(_has_caption_body(t) for t in (u_text, g_text))
                             and _caption_under_side_image(*((r, gr) if CAPTION_RE.match(u_text) else (gr, r)), images))
            if overlap >= 0.5 * min(gr.height, r.height) and hgap < 40 and same_col and not caption_split:
                groups[i] = (gr | r, gspans + u)
                placed = True
                break
        if not placed:
            groups.append((r, list(u)))
    rotated_ids = set()
    for u in rotated_units:
        groups.append((ubox(u), list(u)))
        rotated_ids.add(len(groups) - 1)

    lines: list[Line] = []
    for gi, (r, spans) in enumerate(groups):
        is_rotated = gi in rotated_ids
        if not is_rotated:
            spans.sort(key=lambda s: s["bbox"][0])
        text = _join_spans(spans)
        if any(s["font"].split("+")[-1] == "AdvPi1" for s in spans):
            text = PARTIAL_ETA_SQUARED_RE.sub("ηp²", text)   # 只在這行真的有 Advent 希臘字型時處理
        if not text.strip():
            continue
        lines.append(_build_line(text, r, spans, layout.col_of(r), is_rotated, gi in label_ids))
    if layout.split_x is not None:
        spanning = [ln.bbox.y0 for ln in lines if ln.col == -1]
        spanning += [r.y0 for r in images if layout.col_of(r) == -1]
        try:
            clusters = page.cluster_drawings()
        except Exception:
            clusters = []
        spanning += [r.y0 for r in clusters
                     if r.width >= FIGURE_MIN_SIZE[0] and r.height >= 20 and layout.col_of(r) == -1]
        layout.spanning_y = sorted(spanning)
    lines.sort(key=lambda ln: layout.key(ln.bbox))
    return lines, images, layout


def same_box(a: pymupdf.Rect, b: pymupdf.Rect, tol: float = WATERMARK_BOX_TOLERANCE) -> bool:
    """兩個矩形四邊誤差總和 < tol（pt）→ 視為同一個位置。"""
    return abs(a.x0 - b.x0) + abs(a.y0 - b.y0) + abs(a.x1 - b.x1) + abs(a.y1 - b.y1) < tol


def matches_watermark(r: pymupdf.Rect, watermark_rects: list[pymupdf.Rect], page_rect: pymupdf.Rect) -> bool:
    """r 是否與某張浮水印圖片同位置。

    整頁底圖常超出頁面（TX001：get_image_info 給未裁切的 [-8,-39,604,826]），
    但 get_text("dict") 的圖片框、get_drawings 的繪圖框已裁到頁面內 [0,0,596,786]，
    兩者差約 95pt。因此原始框、裁到頁內的框任一對上都算。"""
    return any(same_box(r, w) or same_box(r, w & page_rect) for w in watermark_rects)


def _lines_inside(r: pymupdf.Rect, lines: list["Line"]) -> list["Line"]:
    """面積 >= 60% 落在 r 裡的水平行（直排行、零面積行不算）。"""
    return [ln for ln in lines if not ln.rotated and ln.bbox.get_area() > 0
            and (r & ln.bbox).get_area() >= 0.6 * ln.bbox.get_area()]


def is_prose_line(ln: "Line", body_size: float) -> bool:
    """是否為正文文句行：>= PROSE_MIN_CHARS 字、字母占非空白字元 >= PROSE_LETTER_SHARE、
    字級 >= 正文 PROSE_MIN_SIZE_RATIO。數字表格列（「2,500 K 0.19 0.51 …」）、圖表軸標籤、
    小字引用框都不算。"""
    text = ln.text.strip()
    visible = [c for c in text if not c.isspace()]
    return (len(text) >= PROSE_MIN_CHARS and bool(visible)
            and sum(c.isalpha() for c in visible) >= PROSE_LETTER_SHARE * len(visible)
            and ln.size >= PROSE_MIN_SIZE_RATIO * body_size)


def is_rule_drawing(d: dict) -> bool:
    """只由水平／垂直直線組成、沒有填色的繪圖（分隔線、框線）。"""
    return d.get("fill") is None and bool(d["items"]) and all(
        it[0] == "l" and (abs(it[1].x - it[2].x) < 0.5 or abs(it[1].y - it[2].y) < 0.5) for it in d["items"])


def is_rect_fill_drawing(d: dict) -> bool:
    """只由矩形組成的填色繪圖（底色、色塊標題列）。長條圖的長條也是這種，需配合框內文字判斷。"""
    return d.get("fill") is not None and bool(d["items"]) and all(it[0] == "re" for it in d["items"])


def layout_decoration(d: dict, page_rect: pymupdf.Rect, watermark_rects: list[pymupdf.Rect],
                      lines: list["Line"]) -> Optional[str]:
    """判斷一個繪圖是否為版面裝飾（不是圖表的一部分）；是則回傳原因代碼，否則 None。

    A：面積 >= 頁面 DECOR_PAGE_AREA_SHARE，或超出頁面 DECOR_BLEED 以上（外框、出血底色）。
    B：與浮水印圖片同位置（浮水印底下常墊一塊同尺寸的填色矩形）。
    C：純矩形填色，框內有 >= DECOR_FILL_MIN_LINES 行、>= DECOR_FILL_MIN_CHARS 字（文字底色框）。
    這些裝飾若參與叢集，會把整頁的框線、面板底色併成一個大區域而被誤判成圖表（TX001）。
    """
    r = pymupdf.Rect(d["rect"])
    if (r.get_area() >= DECOR_PAGE_AREA_SHARE * page_rect.get_area()
            or r.x0 < page_rect.x0 - DECOR_BLEED or r.y0 < page_rect.y0 - DECOR_BLEED
            or r.x1 > page_rect.x1 + DECOR_BLEED or r.y1 > page_rect.y1 + DECOR_BLEED):
        return "A"
    if matches_watermark(r, watermark_rects, page_rect):
        return "B"
    if is_rect_fill_drawing(d):
        inside = _lines_inside(r, lines)
        if len(inside) >= DECOR_FILL_MIN_LINES and sum(len(ln.text.strip()) for ln in inside) >= DECOR_FILL_MIN_CHARS:
            return "C"
    return None


def content_drawings(page: pymupdf.Page, watermark_rects: list[pymupdf.Rect],
                     lines: list["Line"]) -> list[dict]:
    """頁面上排除版面裝飾（見 layout_decoration）後的繪圖，作為圖表叢集與正文框判斷的輸入。"""
    return [d for d in page.get_drawings()
            if layout_decoration(d, page.rect, watermark_rects, lines) is None]


def chart_regions(page: pymupdf.Page, exclude: list[pymupdf.Rect],
                  drawings: Optional[list[dict]] = None) -> list[pymupdf.Rect]:
    """「夠大」的向量繪圖叢集 = 圖表。與圖片或已接受表格重疊者排除。

    drawings：只用這些繪圖叢集（通常是 content_drawings 排除版面裝飾後的結果）；
    None 表示用頁面全部繪圖（舊行為）。空清單表示沒有可叢集的繪圖。
    """
    if drawings is not None and not drawings:
        return []  # 全是版面裝飾：不必叢集；也不依賴各版 PyMuPDF 對空清單的處理方式
    try:
        clusters = page.cluster_drawings() if drawings is None else page.cluster_drawings(drawings=drawings)
    except Exception:  # 舊版 PyMuPDF 沒有 cluster_drawings
        clusters = []
    out = []
    for r in clusters:
        if r.width < FIGURE_MIN_SIZE[0] or r.height < FIGURE_MIN_SIZE[1]:
            continue
        if any((r & e).get_area() >= 0.5 * min(r.get_area(), e.get_area()) for e in exclude):
            continue
        out.append(r)
    return out


def _has_side_by_side_rows(inside: list["Line"], min_rows: int = 2) -> bool:
    """框內是否有 >= min_rows 列「同高、左右並排」的文字（表格狀排列，例如 TX001 p86 的兩欄對照表）。"""
    rows: set[int] = set()
    for a in inside:
        for b in inside:
            if a is not b and min(a.bbox.y1, b.bbox.y1) - max(a.bbox.y0, b.bbox.y0) >= 0.5 * min(a.bbox.height, b.bbox.height):
                rows.add(round(min(a.bbox.y0, b.bbox.y0)))
    return len(rows) >= min_rows


def is_ruled_prose_panel(r: pymupdf.Rect, lines: list["Line"], drawings: list[dict], body_size: float) -> bool:
    """由分隔線、框線、色塊標題列圍成的說明面板，框裡是正文（TX001 p28 唇膏步驟、p95 職場儀容）。

    條件全部成立才算：
    1. 區域內的繪圖全是分隔線（is_rule_drawing）或純矩形色塊（is_rect_fill_drawing），沒有曲線、斜線；
    2. 沒有 >= 2 列左右並排的文字（那是表格，交表格／人工確認）；
    3. >= TEXT_BOX_MIN_LONG_LINES 行是正文文句行，且占框內字數 >= TEXT_BOX_LONG_SHARE。
    長條圖、線框表格雖然也只有矩形與直線，但框內是短標籤或數字，過不了第 3 點。
    """
    shapes = [d for d in drawings if (r + (-1, -1, 1, 1)).contains(pymupdf.Rect(d["rect"]))]
    if not shapes or not all(is_rule_drawing(d) or is_rect_fill_drawing(d) for d in shapes):
        return False
    inside = _lines_inside(r, lines)
    if _has_side_by_side_rows(inside):
        return False
    prose = [ln for ln in inside if is_prose_line(ln, body_size)]
    total = sum(len(ln.text.strip()) for ln in inside)
    return (len(prose) >= TEXT_BOX_MIN_LONG_LINES
            and sum(len(ln.text.strip()) for ln in prose) >= TEXT_BOX_LONG_SHARE * total)


def is_text_box(r: pymupdf.Rect, lines: list["Line"], body_size: Optional[float] = None,
                drawings: Optional[list[dict]] = None) -> bool:
    """底色框（例如 Wiley 期刊摘要的灰底）也是「夠大」的向量繪圖，但框裡是正文：
    字數大多落在長行裡（圖表的軸標籤、數值都是短行）。誤當圖表會把段落最後一行短句切成 figure_text。

    長行：> FIGURE_TEXT_MAX_LEN 字；有 body_size 時，寫滿框寬的文句行（行寬 >= 區域寬
    PROSE_WIDE_SHARE 且 is_prose_line）也算，窄面板每行字數本來就少。
    有 drawings 時，再檢查是否為分隔線圍成的正文面板（is_ruled_prose_panel）。
    """
    inside = _lines_inside(r, lines)

    def is_long(ln: "Line") -> bool:
        if len(ln.text.strip()) > FIGURE_TEXT_MAX_LEN:
            return True
        return (body_size is not None and ln.bbox.width >= PROSE_WIDE_SHARE * r.width
                and is_prose_line(ln, body_size))

    long_lines = [ln for ln in inside if is_long(ln)]
    total = sum(len(ln.text.strip()) for ln in inside)
    if (len(long_lines) >= TEXT_BOX_MIN_LONG_LINES
            and sum(len(ln.text.strip()) for ln in long_lines) >= TEXT_BOX_LONG_SHARE * total):
        return True
    return drawings is not None and body_size is not None and is_ruled_prose_panel(r, lines, drawings, body_size)


def margin_body_lines(lines: list["Line"], page_height: float) -> set[int]:
    """落在頁首／頁尾區，但其實是正文的行（回傳 id(line)）。

    期刊沒有書眉的頁面，正文第一行就在上方 7% 內（AR012 p.2「characteristics, and Vergnaud …」）；
    當成頁首會讓換欄、換頁的段落少一行。判斷：與區外的正文行同欄、同字級、水平重疊、行距正常（緊貼），
    上緣由下往上、下緣由上往下逐行傳遞。書眉、頁碼與正文之間都有明顯空白，不受影響。
    """
    top = page_height * MARGIN_RATIO
    bottom = page_height * (1 - MARGIN_RATIO)

    def in_top(ln: "Line") -> bool:
        return ln.bbox.y1 <= top

    def in_bottom(ln: "Line") -> bool:
        return ln.bbox.y0 >= bottom

    def adjacent(upper: "Line", lower: "Line") -> bool:
        gap = lower.bbox.y0 - upper.bbox.y1
        overlap = min(upper.bbox.x1, lower.bbox.x1) - max(upper.bbox.x0, lower.bbox.x0)
        return (upper.col == lower.col and abs(upper.size - lower.size) <= 0.5
                and -MARGIN_BODY_GAP * upper.size <= gap <= MARGIN_BODY_GAP * upper.size
                and overlap >= 0.5 * min(upper.bbox.width, lower.bbox.width))

    flat = [ln for ln in lines if not ln.rotated]
    body = {id(ln) for ln in flat if not in_top(ln) and not in_bottom(ln)}
    found: set[int] = set()
    for ln in sorted((x for x in flat if in_top(x)), key=lambda x: -x.bbox.y1):
        if any(id(o) in body and adjacent(ln, o) for o in flat):
            body.add(id(ln))
            found.add(id(ln))
    for ln in sorted((x for x in flat if in_bottom(x)), key=lambda x: x.bbox.y0):
        if any(id(o) in body and adjacent(o, ln) for o in flat):
            body.add(id(ln))
            found.add(id(ln))
    return found


CAPTION_TAIL_GAP = 0.5        # 標題續行與上一行的距離上限（× 字級）
CAPTION_TAIL_X_TOLERANCE = 5  # 標題續行與標題的左緣差距上限（pt）


def caption_continuation_lines(lines: list["Line"], page_height: float) -> set[int]:
    """落在頁首區、但其實是圖表標題第二行的行（回傳 id(line)）。

    Wiley 期刊整頁表格的標題排在頁面最上緣，兩行標題的第二行也落在上方 7% 內
    （AR012 p.7「TABLE 4 | … The Munsell notation and the visual」⏎「assessment results differed, …」），
    原本被當成頁首，標題只剩半句，表格偵測也因此從錯的位置開始找表身。
    判斷：上一行是圖表標題（或已認定的續行），兩行緊貼（間距 <= 0.5 × 字級）、左緣對齊（<= 5pt）、字級相同。
    書眉與標題之間有明顯空白或左緣不對齊，不受影響。
    """
    top = page_height * MARGIN_RATIO
    flat = sorted((ln for ln in lines if not ln.rotated), key=lambda ln: ln.bbox.y0)
    anchors = [ln for ln in flat if CAPTION_RE.match(ln.text)]
    found: set[int] = set()
    for ln in flat:
        if ln.bbox.y1 > top or CAPTION_RE.match(ln.text):
            continue
        for up in anchors:
            gap = ln.bbox.y0 - up.bbox.y1
            if (-2 <= gap <= CAPTION_TAIL_GAP * up.size and abs(ln.bbox.x0 - up.bbox.x0) <= CAPTION_TAIL_X_TOLERANCE
                    and abs(ln.size - up.size) <= 0.5):
                found.add(id(ln))
                anchors.append(ln)       # 三行標題：第三行接在第二行之後
                break
    return found


def is_table_like_text(raw: str) -> bool:
    """沒被辨識成表格的表格本體常變成一大段「數字為主」的段落（AR012 附錄 A1、表 3）。
    當正文匯出時數值脫離欄名，也會讓下一欄的續段被誤接上去，所以交人工確認、不參與段落接續。"""
    rows = [ln.split() for ln in raw.split("\n") if ln.strip()]
    if len(rows) < TABLE_TEXT_MIN_LINES:
        return False
    tokens = [t for r in rows for t in r]
    numeric = sum(bool(NUMERIC_TOKEN_RE.match(t)) for t in tokens)
    numeric_end = sum(bool(NUMERIC_TOKEN_RE.match(r[-1])) for r in rows)
    numeric_rows = sum(all(NUMERIC_TOKEN_RE.match(t) for t in r) for r in rows)
    return (numeric >= TABLE_TEXT_NUMERIC_SHARE * len(tokens)
            or numeric_end >= TABLE_TEXT_NUMERIC_END_SHARE * len(rows)
            or numeric_rows >= TABLE_TEXT_NUMERIC_ROW_SHARE * len(rows))


def stroke_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """線條類繪圖（分數線、根號、表格線）。純填色底框不算。"""
    out = []
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"])
        if any(it[0] in ("l", "c", "qu") for it in d["items"]) or min(r.width, r.height) <= 4:
            out.append(r)
    return out


def word_rows(words: list[tuple]) -> list[tuple[pymupdf.Rect, list[tuple]]]:
    """把 get_text('words') 的字依垂直中心分成列；每列的字依 x 排序。"""
    rows: list[tuple[pymupdf.Rect, list[tuple]]] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy = (w[1] + w[3]) / 2
        if rows:
            r, ws = rows[-1]
            if abs(cy - (r.y0 + r.y1) / 2) <= 0.5 * max(r.height, w[3] - w[1]):
                rows[-1] = (r | pymupdf.Rect(w[:4]), ws + [w])
                continue
        rows.append((pymupdf.Rect(w[:4]), [w]))
    return [(r, sorted(ws, key=lambda w: w[0])) for r, ws in rows]


def has_column_gap(row_words: list[tuple], size: float) -> bool:
    return any(b[0] - a[2] > COLUMN_GAP_FACTOR * size for a, b in zip(row_words, row_words[1:]))


WRAP_ROW_MAX_GAP_FACTOR = 0.5   # 儲存格換行的續行與上一列的垂直距離上限（× 字級；緊貼，常為負值）
HEADER_ROW_MAX = 2              # 標題正下方最多暫存幾列沒有欄距的表頭
HEADER_ROW_MAX_GAP = 1.0        # 表頭列與標題／上一列表頭的距離上限（× 字級）
MARGIN_ROW_MAX_GAP = 1.0        # 落在頁首頁尾區的列，與上一列／標題的距離上限（× 字級）；頁碼、書眉通常相距 > 10pt
TABLE_END_GAP = 2.0             # 已收到 2 列資料後，列距超過這個值（× 字級）就視為表格結束；
                                # 雙欄正文的左右欄之間也有大空隙，會被誤當成欄距（AR012 Table 5 下方 26pt 的正文）


def wrapped_cell_row(row_words: list[tuple], anchor_words: list[tuple], gap: float, size: float) -> bool:
    """判斷沒有欄距的一列是否為上一列某個儲存格的換行續行（AR012 Table 1「Red, orange (including brown),」
    →「purple, pink, coral, nude」）。

    參數：
        row_words：這一列的字（只有一段，沒有欄距）。
        anchor_words：上一列「第一行」的字（用來切出欄位區段；續行不再當錨點，避免一路往下吞正文）。
        gap：與上一列的垂直距離（pt）。
        size：字級。

    回傳：
        True 表示是續行：緊貼上一列（gap <= 0.5 × 字級），而且水平範圍只落在上一列「第一欄以外」的
        某一欄裡（不碰到該欄左邊那一欄）。置中排版的欄位，續行左緣不一定對齊，所以只看範圍不看對齊。
        落在第一欄的單段文字（新的列名）、跨好幾欄的正文都不算。
    """
    if gap > WRAP_ROW_MAX_GAP_FACTOR * size or len(split_row_segments(row_words, size)) != 1:
        return False
    x0, x1 = min(w[0] for w in row_words), max(w[2] for w in row_words)
    segs = [r for r, _ in split_row_segments(anchor_words, size)]
    if len(segs) < 2:
        return False
    overlaps = [min(x1, s.x1) - max(x0, s.x0) for s in segs]
    j = max(range(len(segs)), key=lambda k: overlaps[k])
    return j >= 1 and overlaps[j] > 0 and x0 > segs[j - 1].x1


def find_text_table_rows(caption: pymupdf.Rect, words: list[tuple], body_size: float,
                         page_rect: pymupdf.Rect, layout: Optional[ColumnLayout] = None,
                         ) -> list[tuple[pymupdf.Rect, list[tuple]]]:
    """無格線表格：從表格標題往下（找不到再往上）收集「有欄距」的連續文字列。

    正文行字距小（< 1.5 × 字級），表格的欄與欄之間有大空隙；遇到正文、或列距 > 3 × 字級就停。
    往下找時，儲存格內的換行（wrapped_cell_row）併回上一列的字，不中斷表格。
    回傳由上而下的 [(列範圍, 該列的字)]；少於 2 列回傳空清單。
    """
    h = page_rect.height

    def in_margin(r: pymupdf.Rect) -> bool:
        return r.y0 <= h * MARGIN_RATIO or r.y1 >= h * (1 - MARGIN_RATIO)

    # 頁首頁尾區的字先保留：表格最後一行常壓到底部 7%（AR012 Table 1 末行），附錄續表的表頭也常在頂部 7% 內；
    # 下面逐列收集時，落在邊緣區的列必須緊貼上一列或標題（<= 1 × 字級）才收，書眉、頁碼與表格之間有空白，會在那裡停下
    usable = [w for w in words if not pymupdf.Rect(w[:4]).intersects(caption)]
    if layout is not None and layout.col_of(caption) in (0, 1):
        # 雙欄：表格在標題那一欄；另一欄的正文同高也不能被當成同一列
        side = layout.col_of(caption)
        usable = [w for w in usable if layout.col_of(pymupdf.Rect(w[:4])) == side]
    rows = word_rows(usable)
    below = sorted([r for r in rows if r[0].y0 >= caption.y1 - 1], key=lambda r: r[0].y0)
    above = sorted([r for r in rows if r[0].y1 <= caption.y0 + 1], key=lambda r: -r[0].y1)
    for seq, down in ((below, True), (above, False)):
        prev = caption
        got: list[tuple[pymupdf.Rect, list[tuple]]] = []
        header: list[tuple[pymupdf.Rect, list[tuple]]] = []   # 標題正下方、沒有欄距的表頭列（暫存）
        anchor: list[tuple] = []          # 上一列第一行的字（判斷續行用）
        for rect, ws in seq:
            gap = rect.y0 - prev.y1 if down else prev.y0 - rect.y1
            limit = TABLE_END_GAP if len(got) >= 2 else 3   # 已收到 2 列資料：列距變大就是表格結束（下方接正文）
            if gap > limit * body_size or (in_margin(rect) and gap > MARGIN_ROW_MAX_GAP * body_size):
                break
            if not has_column_gap(ws, body_size):
                if down and got and wrapped_cell_row(ws, anchor, gap, body_size):
                    last_rect, last_ws = got[-1]
                    got[-1] = (last_rect | rect, last_ws + ws)   # 字保持「先原列、後續行」的順序
                    prev = rect
                    continue
                if down and not got and len(header) < HEADER_ROW_MAX and gap <= HEADER_ROW_MAX_GAP * body_size:
                    header.append((rect, ws))   # 跨欄或堆疊的表頭（「Mean value (SD)」「Undertone」），等資料列出現才算
                    prev = rect
                    continue
                break
            got.append((rect, ws))
            anchor = ws
            prev = rect
        if len(got) >= 2:
            x0, x1 = min(r.x0 for r, _ in got), max(r.x1 for r, _ in got)
            header = [(r, ws) for r, ws in header if r.x0 >= x0 - 5 and r.x1 <= x1 + 5]   # 表頭要落在表格寬度內
            return sorted(header + got, key=lambda r: r[0].y0)
    return []


def split_row_segments(row_words: list[tuple], size: float) -> list[tuple[pymupdf.Rect, list[tuple]]]:
    """把一列的字在大空隙（> COLUMN_GAP_FACTOR × 字級）處切開，每段 = 一個候選儲存格。回傳 (範圍, 該段的字)。"""
    segs: list[list[tuple]] = []
    for w in row_words:
        if segs and w[0] - segs[-1][-1][2] <= COLUMN_GAP_FACTOR * size:
            segs[-1].append(w)
        else:
            segs.append([w])
    out = []
    for seg in segs:
        r = pymupdf.Rect(seg[0][:4])
        for w in seg[1:]:
            r |= pymupdf.Rect(w[:4])
        out.append((r, seg))
    return out


def _merge_intervals(spans: list[tuple[float, float]]) -> list[list[float]]:
    columns: list[list[float]] = []
    for x0, x1 in sorted(spans):
        if columns and x0 <= columns[-1][1]:
            columns[-1][1] = max(columns[-1][1], x1)
        else:
            columns.append([x0, x1])
    return columns


def align_word_table(rows: list[tuple[pymupdf.Rect, list[tuple]]], size: float
                     ) -> tuple[list[list[Optional[str]]], list[list[Optional[tuple]]], list[str]]:
    """以單字座標對齊欄位（無格線表格）。

    1. 欄位範圍：以「資料列」（第 2 列起）的文字區段水平範圍分群，範圍重疊的屬於同一欄。
       不用表頭決定欄位：表頭常缺左上角那格，或欄名太長、彼此靠太近而黏成一段。
       表頭中落在所有欄位之外的區段，另成新欄。資料列不足 2 列或只分出 1 欄時，改用全部列。
    2. 每段文字依水平重疊量放進對應的欄。若一段明顯橫跨多欄，就拆成單字、依單字中心分配，並記錄原因。
    3. 兩段落在同一欄 → 依序並列並記錄原因。不猜、不補值；缺的欄位保持 None。
    回傳 (儲存格文字, 儲存格 bbox, 需人工確認原因)。
    """
    seg_rows = [split_row_segments(ws, size) for _, ws in rows]
    data_spans = [(r.x0, r.x1) for segs in seg_rows[1:] for r, _ in segs]
    columns = _merge_intervals(data_spans) if len(seg_rows) >= 3 else []
    if len(columns) >= 2:
        for r, _ in seg_rows[0]:
            if not any(min(r.x1, c[1]) - max(r.x0, c[0]) > 0 for c in columns):
                columns = _merge_intervals([(c[0], c[1]) for c in columns] + [(r.x0, r.x1)])
    else:
        columns = _merge_intervals([(r.x0, r.x1) for segs in seg_rows for r, _ in segs])
    n_cols = len(columns)
    bounds = [float("-inf")] + [(columns[j][1] + columns[j + 1][0]) / 2 for j in range(n_cols - 1)] + [float("inf")]

    def col_for(x0: float, x1: float) -> tuple[int, list[int]]:
        overlaps = [max(0.0, min(x1, bounds[j + 1]) - max(x0, bounds[j])) for j in range(n_cols)]
        width = max(x1 - x0, 1e-6)
        return max(range(n_cols), key=lambda k: overlaps[k]), [k for k, o in enumerate(overlaps) if o > 0.3 * width]

    reasons: list[str] = []
    cells: list[list[Optional[str]]] = []
    boxes: list[list[Optional[tuple]]] = []
    for i, segs in enumerate(seg_rows):
        row: list[Optional[str]] = [None] * n_cols
        rb: list[Optional[tuple]] = [None] * n_cols
        pieces: list[tuple[int, pymupdf.Rect, str]] = []
        for rect, ws in segs:
            j, spans = col_for(rect.x0, rect.x1)
            text = " ".join(w[4] for w in ws)
            if len(spans) > 1 and len(ws) > 1:
                # 橫跨多欄的區段：拆成單字，依單字左緣落在哪一欄的起點之後分配
                reasons.append(f"第 {i + 1} 列「{text}」橫跨第 {spans} 欄，已依單字位置拆開，請確認")
                for w in ws:
                    j_w = max([k for k in range(n_cols) if columns[k][0] <= w[0] + 1] or [0])
                    pieces.append((j_w, pymupdf.Rect(w[:4]), w[4]))
            else:
                if len(spans) > 1:
                    reasons.append(f"第 {i + 1} 列「{text}」橫跨第 {spans} 欄，可能是合併儲存格")
                pieces.append((j, rect, text))
        for j, rect, text in pieces:
            if row[j] is not None:
                joined_words = any(r.startswith(f"第 {i + 1} 列「") and "依單字位置拆開" in r for r in reasons)
                if not joined_words:
                    reasons.append(f"第 {i + 1} 列有兩段文字落在第 {j} 欄（{row[j]!r}、{text!r}），已依序並列，請確認")
                row[j] = row[j] + " " + text
                rb[j] = tuple(pymupdf.Rect(rb[j]) | rect)
            else:
                row[j] = text
                rb[j] = tuple(rect)
        cells.append(row)
        boxes.append(rb)
    return cells, boxes, reasons


def table_text_coverage(rect: pymupdf.Rect, words: list[tuple], rows: list[list[Optional[str]]]) -> float:
    """表格範圍內的字（以字元多重集合計）有多少比例出現在儲存格中。"""
    inside = "".join(w[4] for w in words if rect.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)))
    want = char_counter(inside)
    if not want:
        return 1.0
    got = char_counter("".join(c or "" for r in rows for c in r))
    missing = want - got
    return 1 - sum(missing.values()) / sum(want.values())


def detect_tables(page: pymupdf.Page, words: list[tuple], table_caption_rects: list[pymupdf.Rect],
                  body_size: float, pno: int, layout: Optional[ColumnLayout] = None,
                  ) -> tuple[list[TableCandidate], list[ExtractionIssue]]:
    """兩條路徑找表格：

    1. find_tables()（lines 策略）：有格線的表格。
    2. 對尚未配到表格的「Table N」標題，用 find_text_table_rows 圈出有欄距的連續文字列，
       再用 align_word_table 依單字座標對齊欄位（無格線表格）。
    候選都要通過 table_quality；沒通過的記錄 table_low_confidence，不輸出成表格。
    """
    accepted: list[TableCandidate] = []
    issues: list[ExtractionIssue] = []

    def reject(rect: pymupdf.Rect, stats: dict, method: str, why: str) -> None:
        issues.append(ExtractionIssue(
            issue_type="table_low_confidence", severity="warning", stage="extracted", page_number=pno,
            message=f"偵測到疑似表格但{why}，未當作表格輸出，需人工確認。",
            details={**stats, "method": method, "bbox": ",".join(f"{v:.1f}" for v in rect)},
        ))

    try:
        for t in page.find_tables().tables:
            all_rows = [[None if c is None else str(c) for c in r] for r in t.extract()]
            all_boxes = [[tuple(c) if c else None for c in row.cells] for row in t.rows]
            pieces = split_stacked_boxes(all_rows, all_boxes)
            for rows, boxes in pieces:
                if len(pieces) == 1:
                    rect = pymupdf.Rect(t.bbox)
                else:
                    # 切開的框：範圍取這一段儲存格的聯集（覆蓋率只算這一段裡的字）
                    rect = pymupdf.Rect()
                    for c in (c for r in boxes for c in r if c):
                        rect |= pymupdf.Rect(c)
                ok, stats = table_quality(rows)
                coverage = table_text_coverage(rect, words, rows)
                if not ok and sparse_table_ok(rows, rect, words, coverage):
                    ok = True  # 稀疏但真實的表格（例如事後檢定表、成分負荷量表）
                if not ok:
                    reject(rect, stats, "pymupdf_lines", "儲存格多為空白或列欄不足（常見於向量圖表）")
                    continue
                accepted.append(TableCandidate(
                    rect=rect, rows=rows, method="pymupdf_lines", cell_bboxes=boxes, coverage=coverage,
                ))
    except Exception as e:  # pragma: no cover - PyMuPDF 內部錯誤
        issues.append(ExtractionIssue(issue_type="table_detection_failed", severity="error", stage="extracted",
                                      page_number=pno, message=f"find_tables 失敗：{e!r}"))

    def nearest_caption(t: pymupdf.Rect) -> Optional[pymupdf.Rect]:
        """離表格最近（且水平重疊）的標題；表格只屬於一個標題。"""
        near = [c for c in table_caption_rects if _vdist(c, t) <= CAPTION_LINK_DIST and _hoverlap(c, t)]
        return min(near, key=lambda c: _vdist(c, t)) if near else None

    for cap in table_caption_rects:
        # 標題已有自己的表格才略過；離另一個標題更近的表格不算（AR012 p.13：Table 7 的表身就在 Table 8 標題上方 22pt）
        if any(nearest_caption(c.rect) is cap for c in accepted):
            continue
        # 先看標題旁邊有沒有表格底色／框線（並排版面的強訊號），沒有才往標題上下找
        rows_w = side_table_rows(page, cap, words, body_size)
        if not rows_w:
            rows_w = find_text_table_rows(cap, words, body_size, page.rect, layout)
        if not rows_w:
            continue
        region = rows_w[0][0]
        for r, _ in rows_w[1:]:
            region = region | r
        cells, boxes, reasons = align_word_table(rows_w, body_size)
        ok, stats = table_quality(cells)
        if not ok:
            reject(region, stats, "pymupdf_words", "欄位對齊後空白過多或列欄不足")
            continue
        accepted.append(TableCandidate(
            rect=region, rows=cells, method="pymupdf_words", cell_bboxes=boxes,
            coverage=table_text_coverage(region, words, cells), reasons=reasons,
        ))
    # 同一區域若最後被文字對齊路徑接受，先前 lines 策略的「低信心」紀錄就不再成立
    issues = [i for i in issues if i.issue_type != "table_low_confidence" or not any(
        (pymupdf.Rect([float(v) for v in i.details["bbox"].split(",")]) & c.rect).get_area()
        >= 0.5 * pymupdf.Rect([float(v) for v in i.details["bbox"].split(",")]).get_area() for c in accepted)]
    return accepted, issues


def side_table_rows(page: pymupdf.Page, caption: pymupdf.Rect, words: list[tuple], body_size: float,
                    ) -> list[tuple[pymupdf.Rect, list[tuple]]]:
    """並排版面（Wiley 等期刊）：表格標題在左、表格在右。
    找與標題垂直重疊、水平不重疊、距離 <= CAPTION_LINK_DIST 的繪圖叢集，取其中的字當表格列。"""
    try:
        clusters = page.cluster_drawings()
    except Exception:
        return []
    for r in clusters:
        if r.width < FIGURE_MIN_SIZE[0] or r.height < 2 * body_size:
            continue
        side_gap = max(r.x0 - caption.x1, caption.x0 - r.x1)
        vertical = min(r.y1, caption.y1) - max(r.y0, caption.y0) > 0
        if vertical and 0 <= side_gap <= CAPTION_LINK_DIST:
            inside = [w for w in words if r.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
            rows = word_rows(inside)
            if len(rows) >= 2 and sum(has_column_gap(ws, body_size) for _, ws in rows) >= 2:
                return rows
    return []


TABLE_INCOMPLETE_REASON = "表格同一高度範圍外還有 {n} 段疑似儲存格文字，表格可能被欄位分界或對齊截斷而不完整，請對照 PDF"
SIDE_FRAGMENT_TYPES = ("paragraph", "label", "list_item")
CONTINUED_MARKER_RE = re.compile(r"^\s*(\((Continued|Continues)\)\s*)+$", re.I)   # 可能兩欄各一個，同一行


def is_numeric_dominant(text: str) -> bool:
    """一半以上的詞是數字（「1.815 21.948 c 2.513」「11.546 b」）。"""
    tokens = text.split()
    return bool(tokens) and sum(bool(NUMERIC_TOKEN_RE.match(t)) for t in tokens) >= 0.5 * len(tokens)


def table_side_fragments(table: TextBlock, page_blocks: list[TextBlock]) -> list[TextBlock]:
    """找出與已解析表格同高、但不在表格內的疑似儲存格段落。

    參數：
        table：已解析的表格區塊。
        page_blocks：同頁所有區塊。

    回傳：
        段落／標籤／列表項中，垂直範圍有一半以上落在表格高度內、與表格本身不重疊，且
        「短於 ORPHAN_TABLE_FRAGMENT_CHARS」或「以數字為主」的區塊；圖表標題格式的文字除外。
        雙欄頁另一欄的正文同高但又長又是文字，不會被選到。
    """
    tr = pymupdf.Rect(table.bbox)
    out = []
    for b in page_blocks:
        if b is table or b.block_type not in SIDE_FRAGMENT_TYPES or not b.bbox:
            continue
        r = pymupdf.Rect(b.bbox)
        if (r & tr).get_area() > 0.5 * r.get_area():
            continue                                  # 在表格裡面（理論上不會發生，表格內的行已排除）
        overlap = min(r.y1, tr.y1) - max(r.y0, tr.y0)
        text = b.raw_text.strip()
        if CAPTION_RE.match(text):
            continue                                  # 並排的圖表標題（AR002「FIGURE 7」）不是儲存格
        if overlap >= 0.5 * r.height and (len(text) < ORPHAN_TABLE_FRAGMENT_CHARS or is_numeric_dominant(text)):
            out.append(b)
    return out


def _vdist(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    return max(0.0, b.y0 - a.y1, a.y0 - b.y1)


def _hoverlap(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    return min(a.x1, b.x1) - max(a.x0, b.x0) > 0


def _is_upper_heading(text: str) -> bool:
    """全大寫的英文標題（INTRODUCTION、MATERIALS AND METHODS）。沒有英文字母時回傳 False。"""
    letters = [c for c in text if c.isalpha() and c.isascii()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _is_plain_subheading(glines: list[Line], body_size: float) -> bool:
    """期刊的小標（例如 Wiley 的 "Data acquisition device"）：非粗體，但
    單獨成段（只有一行）、字級比內文大 1.5–5pt、沒有句尾標點、不含作者列的 "|"、結尾不是上標數字。
    封面的特大字（> 內文 + 5pt）與多行的大字段落都不算。"""
    if len(glines) != 1:
        return False
    ln = glines[0]
    text = ln.text.strip()
    return (body_size + 1.5 <= ln.size <= body_size + 5 and 3 <= len(text) <= 80
            and bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))
            and not re.search(r"[.。:;,，：；\d]$", text) and "|" not in text
            and not ends_unfinished(text))   # TX001 p.33「Toned colours generally appeal pleasing to」是半句正文


def ends_unfinished(text: str) -> bool:
    """文字是否停在句子中間：最後一個英文字是虛詞（UNFINISHED_TAIL_WORDS，例如 to、with、if）。
    標題不會停在這些字上；只看英文，中文不受影響。"""
    m = re.search(r"([A-Za-z]+)\W*$", text.strip())
    return bool(m) and m.group(1).lower() in UNFINISHED_TAIL_WORDS


UNFINISHED_LOOKAHEAD = 4   # 編號條列最多往下看幾行續行找句號


def _continuation_ends_sentence(lines: list[Line], i: int) -> bool:
    """lines[i] 之後的續行是否在 UNFINISHED_LOOKAHEAD 行內以句號（. ! ?）結束。

    續行：緊接在上一行下方（行距 <= PARA_GAP_FACTOR × 字級；容許重疊不到半行高，TX001 p.26 行距很緊，
    上下行重疊 2.2pt）、同欄、左右重疊、字級相差 <= 1pt、本身不是另一個編號項目。
    例：TX001 p.26「15.⇥ Minimise … by using products ⏎ economically, and following correct storage ⏎
    procedures as per the manufacturer's instructions.」第二行續行才寫完句子。
    """
    ln = lines[i]
    prev = ln
    beside_caption = False
    seen = 0
    for nxt in lines[i + 1:]:
        if not _hoverlap(ln.bbox, nxt.bbox) and (beside_caption or CAPTION_RE.match(nxt.text)):
            # 旁邊圖下方的圖說與它的續行（TX001 p.59「Figure 2.5: Different types of ⏎ electric brushes」
            # 在條列左邊）不是條列的續行，也不切斷續行，也不佔往下看的行數
            beside_caption = True
            continue
        seen += 1
        if seen > UNFINISHED_LOOKAHEAD:
            return False
        gap = nxt.bbox.y0 - prev.bbox.y1
        if not (-0.5 * min(prev.bbox.height, nxt.bbox.height) <= gap <= PARA_GAP_FACTOR * max(prev.size, nxt.size)
                and nxt.col == ln.col and _hoverlap(ln.bbox, nxt.bbox) and abs(nxt.size - ln.size) <= 1.0
                and not NUMBERED_HEADING_RE.match(nxt.text.strip())):
            return False
        if re.search(r"[.!?]$", nxt.text.strip()):
            return True
        prev = nxt
    return False


def demote_unfinished_numbered_headings(lines: list[Line], kinds: list[str]) -> list[str]:
    """把「沒寫完的編號條列」從標題改回條列，回傳新的種類清單（不修改輸入）。

    _line_kind 的「編號＋字級比內文大 1.5pt → 標題」規則，會把整頁用較大字級排版的編號條列
    （TX001 p.26「15.⇥ Minimise the wastage of products by using products ⏎ economically, …」）當成標題，
    之後的頁面章節路徑全掛在這些「標題」下面（p.28 變成「the client's satisfaction with」）。
    只處理同時符合下列條件的行：種類是 heading、有阿拉伯數字編號（NUMBERED_HEADING_RE）、
    行首不是粗體（靠字級才成為標題）；且這一項是句子——本行以標點（. , ; :）結尾，
    或往下的續行（最多 UNFINISHED_LOOKAHEAD 行，見 _continuation_ends_sentence）寫到句號為止。
    兩行的真標題（AR002 p.7「3.5. The evaluation of color stripes on the ⏎ forearm」）續行沒有句號，
    不受影響，所以不單看本行是否以虛詞結尾。粗體編號標題、Chapter／Abstract 這類章名、中文標題都不受影響。
    """
    out = list(kinds)
    for i, (ln, kind) in enumerate(zip(lines, kinds)):
        text = ln.text.strip()
        if kind != "heading" or ln.first_bold or not NUMBERED_HEADING_RE.match(text):
            continue
        if re.search(r"[.,;:]$", text) or _continuation_ends_sentence(lines, i):
            out[i] = "list_item" if LIST_ITEM_RE.match(text) else "text"
    return out


BACK_MATTER_HEADING_RE = re.compile(
    r"^\s*(References?|Acknowledg(e)?ments?|Declaration of (Conflicting|Competing) Interests?"
    r"|Conflicts? of Interests?|Funding|Author Biograph(y|ies))\s*$", re.I)


def promote_back_matter_headings(lines: list[Line], kinds: list[str]) -> list[str]:
    """書末章名（References、Funding、Acknowledgments…）單獨一行、用和下一行正文不同的字型 → 標題。

    SAGE 期刊（AR003 p.8）的書末章名用標題字型，但字級和內文一樣（10pt）、也不是粗體旗標，
    _line_kind 的「字級大 1.5pt／粗體／置中」都不成立，章名和下面的正文被併成一段，
    參考書目與基金聲明就留在「Discussion」章節底下被匯出。只處理完全等於書末章名的短行，
    且下一行存在、在它下方、字型集合和它完全不重疊；回傳新的種類清單（不修改輸入）。
    """
    out = list(kinds)
    for i, (ln, kind) in enumerate(zip(lines, kinds)):
        if kind not in ("text", "label") or i + 1 >= len(lines) or not BACK_MATTER_HEADING_RE.match(ln.text):
            continue
        nxt = lines[i + 1]
        if nxt.bbox.y0 >= ln.bbox.y1 - 1 and ln.fonts and nxt.fonts and not (ln.fonts & nxt.fonts):
            out[i] = "heading"
    return out


def _line_kind(ln: Line, body_size: float, in_figure: bool, page_center: Optional[float] = None) -> str:
    text = ln.text.strip()
    if TOC_LINE_RE.search(text):
        return "toc_entry"
    # 置中 = 行中心接近頁面中心，且寬度不到頁寬的 60%（滿版的正文行中心也在中間，要排除）
    centered = (page_center is not None and abs((ln.bbox.x0 + ln.bbox.x1) / 2 - page_center) <= CENTER_TOLERANCE
                and ln.bbox.width <= 0.6 * 2 * page_center)
    if CAPTION_RE.match(text):
        # 英文標題要求粗體；中文論文的標題常是非粗體的標楷體，改以「置中且不長」判斷，
        # 避免把「表 2-1 顯示……」這類正文句首誤判成標題
        if ln.first_bold or (CJK_CAPTION_RE.match(text) and centered and len(text) <= 80):
            return "table_caption" if TABLE_CAPTION_RE.match(text) else "figure_caption"
    if EQUATION_LABEL_RE.match(text):
        return "equation_label"
    if ln.math_ratio >= 0.6:
        return "formula"
    level = heading_level_for(text)
    if ln.bold_all and len(text) <= 150:
        if level is not None or ln.size >= body_size + 1.5:
            return "heading"
        if len(text) <= 60:
            return "label"
    # 中文論文：「1.1 研究背景」只有編號是粗體、「第一章 緒論」「摘要」不是粗體但字級較大
    # 「**1. Vein test:** Observe …」是編號條列的粗體引言，後面接正文，不是標題（交給下面的 list_item）
    if (level is not None and len(text) <= 60 and (ln.first_bold or ln.size >= body_size + 1.5)
            and not ln.run_in_label):
        return "heading"
    # 「參考文獻」「第五章 結論」這類章名：內文字級、非粗體，但單獨一行置中；
    # 沒置中但首字放大（TX001 p.99「Glossary」：G 16pt、其餘小型大寫 11.2pt）也算
    if ((FRONT_MATTER_RE.match(text) or CHAPTER_RE.match(text)) and len(text) <= 40
            and (centered or ln.max_size >= body_size + DISPLAY_HEADING_DELTA)):
        return "heading"
    if in_figure and len(text) <= FIGURE_TEXT_MAX_LEN:
        return "figure_text"
    if LIST_ITEM_RE.match(text):
        return "list_item"
    return "text"


ORDERED_MARKER_RE = re.compile(r"^\s*(\d{1,2}[.)]|[a-z][.)])\s+\S")
LINE_END_PUNCT_RE = re.compile(r"[.:;!?。：；！？]\s*$")
WRAPPED_LINE_MIN_SHARE = 0.85   # 上一行寬度 >= 區塊最寬行的 85% → 是寫滿換行的正文行


def is_wrapped_list_marker(prev_lines: list[Line], ln: Line) -> bool:
    """段落換行後，下一行剛好以「40.」「f.」開頭（AR012 p.6「below⏎40. Similar …」、p.12「from a to⏎f. For …」），
    不是新的列表項，而是同一句話的續行。

    參數：
        prev_lines：目前這個正文區塊已收進的行（由上而下）。
        ln：下一行（_line_kind 判成 list_item）。

    回傳：
        True 表示應留在同一段：上一行沒有句尾標點、而且寫滿整行（寬度 >= 區塊最寬行的 85%，表示是自動換行），
        這一行是數字或單一英文字母的編號（項目符號「•」一律是新項目，不在此列）。
    """
    if not ORDERED_MARKER_RE.match(ln.text):
        return False
    prev = prev_lines[-1]
    widest = max(g.bbox.width for g in prev_lines)
    return (not LINE_END_PUNCT_RE.search(prev.text) and widest > 0
            and prev.bbox.width >= WRAPPED_LINE_MIN_SHARE * widest and len(prev_lines) >= 2)


def continues_beside_label(blocks: list[tuple[str, list[Line]]], ln: Line, kind: str) -> bool:
    """這一行是否接續「倒數第二個區塊」的正文，而最後一個區塊只是排在旁邊的側欄標籤。

    參數：
        blocks：group_lines 目前已組好的區塊（種類, 行）。
        ln、kind：下一行與它的種類。

    回傳：
        True 表示：最後一個區塊是單行 heading／label，和前一個正文區塊（text／list_item）的最後一行
        同高並排（垂直重疊 >= 較矮者的 50%）、左右不重疊，也和這一行左右不重疊；
        這一行是 text，和前一個正文區塊最後一行的行距、欄位、字級都符合同一段的條件。
        例：TX001 p.35 左欄「Notes」與右欄「1. Vein test: … under the」同高，
        閱讀順序排在中間，原本把「wrist. If the vein appears blue…」切成孤立段落。
    """
    if kind != "text" or len(blocks) < 2:
        return False
    side_kind, side_lines = blocks[-1]
    body_kind, body_lines = blocks[-2]
    if side_kind not in ("heading", "label") or len(side_lines) != 1 or body_kind not in ("text", "list_item"):
        return False
    side, prev = side_lines[0], body_lines[-1]
    beside = (not _hoverlap(side.bbox, prev.bbox) and not _hoverlap(side.bbox, ln.bbox)
              and min(side.bbox.y1, prev.bbox.y1) - max(side.bbox.y0, prev.bbox.y0)
              >= 0.5 * min(side.bbox.height, prev.bbox.height))
    gap = ln.bbox.y0 - prev.bbox.y1
    close = (gap <= PARA_GAP_FACTOR * max(ln.size, prev.size) and ln.col == prev.col
             and ln.bbox.y1 > prev.bbox.y0 and abs(ln.size - prev.size) <= 1.0)
    return beside and close


LIST_OUTDENT_TOLERANCE = 5.0   # 條列續行最多可比編號靠左幾 pt（對齊誤差）；再靠左就是新段落


def outdented_after_list_item(bkind: str, blines: list[Line], ln: Line) -> bool:
    """這一行是否是條列項目之後的新段落：目前區塊是 list_item，而這一行的左緣比條列編號
    （區塊第一行左緣）還靠左超過 LIST_OUTDENT_TOLERANCE。

    條列的續行會對齊編號或縮在編號右邊，不會比編號更靠左。TX001 p.35「2. The old jewellery trick」
    編號在 x=226.6、續行在 244.8，後面原文另起的「The colour wheel helps identify…」從 210.8 開始，
    原本被當成續行，三段併成一筆。
    """
    return bkind == "list_item" and ln.bbox.x0 < blines[0].bbox.x0 - LIST_OUTDENT_TOLERANCE


FIRST_LINE_OFFSET_MIN = 8.0     # 段落第一行與續行左緣至少差這麼多 pt，才算有首行縮排／凸排的版式
LAST_LINE_SHORT_SHARE = 0.8     # 上一行寬度 < 區塊最寬行的 80% → 是段落的最後一行


def starts_new_offset_paragraph(blines: list[Line], ln: Line) -> bool:
    """首行縮排或凸排版式中，這一行是否是下一個段落的第一行。

    參數：
        blines：目前正文區塊已收進的行（至少兩行才判斷，第二行是續行的基準左緣）。
        ln：下一行。

    回傳：
        True 表示另起新段落：區塊第一行與續行的左緣相差 >= FIRST_LINE_OFFSET_MIN（這份版式有首行偏移），
        上一行以句號結尾而且較短（段落最後一行），這一行的左緣又回到「第一行」的位置。
        例：TX001 p.35「The colour wheel helps … and lipstick.」⏎「The basic rule is to stick to cool colours …」，
        兩段的第一行都從 210.8 起（行首有 tab）、續行在 244.8，原本被併成一段。
        沒有首行偏移的文件（第一行與續行左緣相同），這條規則不會成立。
    """
    if len(blines) < 2:
        return False
    first_x0, cont_x0 = blines[0].bbox.x0, blines[1].bbox.x0
    prev = blines[-1]
    widest = max(g.bbox.width for g in blines)
    return (abs(first_x0 - cont_x0) >= FIRST_LINE_OFFSET_MIN and abs(ln.bbox.x0 - first_x0) <= 3
            and bool(re.search(r"[.!?][\"”’)]?$", prev.text.strip()))
            and prev.bbox.width < LAST_LINE_SHORT_SHARE * widest)


def group_lines(lines: list[Line], kinds: list[str], body_size: float,
                in_glossary: bool = False) -> list[tuple[str, list[Line]]]:
    """把行依「種類 + 垂直間距 + 縮排」組成區塊。

    in_glossary：本頁開頭是否已在詞彙表區（上一頁的章節是 Glossary）。頁內遇到 Glossary 標題進入、
    遇到其他標題離開。詞彙表區裡，行首是粗體引言（Line.run_in_label）的行是新詞條，不接到上一段；
    詞彙表區以外不拆（AR001 結構式摘要「Methods: …」、AR012 作者貢獻維持原樣）。
    旁邊並排的側欄標籤（見 continues_beside_label）不切斷正文：正文接回前一段，標籤順延到該段之後。
    """
    blocks: list[tuple[str, list[Line]]] = []
    deferred: Optional[tuple[str, list[Line]]] = None   # 被跳過的側欄標籤，等正文段落結束再放回
    for ln, kind in zip(lines, kinds):
        if kind == "heading":
            in_glossary = bool(GLOSSARY_MARKER_RE.match(ln.text.strip()))
        new_entry = in_glossary and ln.run_in_label
        if deferred is None and not new_entry and continues_beside_label(blocks, ln, kind):
            deferred = blocks.pop()
            blocks[-1][1].append(ln)
            continue
        if blocks:
            bkind, blines = blocks[-1]
            prev = blines[-1]
            gap = ln.bbox.y0 - prev.bbox.y1
            # 換欄（左欄底 → 右欄頂）或往上跳時，gap 為負，不可當成「很接近」
            close = (gap <= PARA_GAP_FACTOR * max(ln.size, prev.size) and ln.col == prev.col
                     and ln.bbox.y1 > prev.bbox.y0)
            same_size = abs(ln.size - prev.size) <= 1.0
            cont = False
            if ln.running_label or prev.running_label:
                pass   # 頁面標籤（Notes）自成一塊，不和標題或正文併在一起
            elif (bkind in ("figure_caption", "table_caption") and kind == "text" and blines[0].col == -1
                    and ln.col in (0, 1) and -2 <= gap <= PARA_GAP_FACTOR * max(ln.size, prev.size)
                    and abs(ln.bbox.x0 - blines[0].bbox.x0) <= 5 and same_size):
                # 雙欄頁的跨欄圖說：最後一行較短，只落在左欄，原本會因「換欄」被切斷成孤立段落
                cont = True
            elif close and same_size:
                if bkind in ("text", "list_item") and kind == "text":
                    # 詞彙表的下一個詞條另起一段；條列項目後面比編號更靠左的行是新段落，不是條列續行
                    cont = (not new_entry and not outdented_after_list_item(bkind, blines, ln)
                            and not (bkind == "text" and starts_new_offset_paragraph(blines, ln)))
                elif bkind == "text" and kind == "list_item" and is_wrapped_list_marker(blines, ln):
                    cont = True  # 換行後恰好以「40.」「f.」開頭的續行，不是新列表項
                elif (bkind == "heading" and kind == "heading" and heading_level_for(ln.text) is None
                      and _is_upper_heading(prev.text) == _is_upper_heading(ln.text)):
                    cont = True  # 多行標題（第二行沒有編號；全大寫大標不與下一行小標合併）
                elif bkind in ("figure_caption", "table_caption") and kind == "text":
                    # 圖說續行：有縮排，或上一行還沒結束句子；且這行要從圖說的寬度內開始
                    # （圖在左、正文在右時，圖說右邊的正文不是圖說續行，TX001 p.17）
                    within = ln.bbox.x0 < max(g.bbox.x1 for g in blines)
                    cont = within and (ln.bbox.x0 >= blines[0].bbox.x0 + 10 or not prev.text.rstrip().endswith("."))
            if bkind == "figure_text" and kind == "figure_text" and gap <= 3 * body_size and ln.col == prev.col:
                cont = True
            if bkind == "formula" and kind == "formula" and gap <= FORMULA_GAP_FACTOR * body_size and ln.col == prev.col:
                cont = True
            if bkind == "table_text" and kind == "table_text" and gap <= 3 * body_size and ln.col == prev.col:
                cont = True
            if cont:
                blines.append(ln)
                continue
        if deferred is not None:
            blocks.append(deferred)
            deferred = None
        blocks.append((kind, [ln]))
    if deferred is not None:
        blocks.append(deferred)
    return blocks


def body_font_size(doc: pymupdf.Document, pages: list[int]) -> float:
    """以字元數加權找出最常見字級（= 內文字級）。"""
    c: Counter = Counter()
    for pno in pages:
        for b in doc[pno - 1].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    c[round(s["size"])] += len(s["text"].strip())
    return float(c.most_common(1)[0][0]) if c else 12.0


def _rect_tuple(r: pymupdf.Rect) -> tuple[float, float, float, float]:
    return tuple(round(v, 1) for v in r)  # type: ignore[return-value]


def _nearest_caption(rect: pymupdf.Rect, captions: list[TextBlock]) -> Optional[TextBlock]:
    """先找正上方／正下方最近的標題；找不到再找同高度、在旁邊的標題（並排版面）。"""
    best, best_d = None, None
    for c in captions:
        cr = pymupdf.Rect(c.bbox)
        if not _hoverlap(rect, cr):
            continue
        d = _vdist(rect, cr)
        if d <= CAPTION_LINK_DIST and (best_d is None or d < best_d):
            best, best_d = c, d
    if best is not None:
        return best
    for c in captions:
        cr = pymupdf.Rect(c.bbox)
        if min(rect.y1, cr.y1) - max(rect.y0, cr.y0) <= 0:
            continue
        d = max(rect.x0 - cr.x1, cr.x0 - rect.x1)
        if 0 <= d <= CAPTION_LINK_DIST and (best_d is None or d < best_d):
            best, best_d = c, d
    return best


def _link(a: TextBlock, b: TextBlock) -> None:
    """雙向連結（此時 block_id 已確定）。"""
    if b.block_id not in a.linked_block_ids:
        a.linked_block_ids.append(b.block_id)
    if a.block_id not in b.linked_block_ids:
        b.linked_block_ids.append(a.block_id)


def _mark(b: TextBlock, reason: str, status: Optional[str] = None) -> None:
    """標記待人工確認；status='partial'/'failed' 時一併設定 parse_status 與 failure_reason。"""
    b.needs_review = True
    if reason not in b.review_reasons:
        b.review_reasons.append(reason)
    if status and (b.parse_status == "ok" or status == "failed"):
        b.parse_status = status  # type: ignore[assignment]
        b.failure_reason = reason if not b.failure_reason else b.failure_reason + "；" + reason


def find_watermarks(pdf: pymupdf.Document, pages: list[int]) -> dict[bytes, int]:
    """找出重複出現的圖片（浮水印、頁面 logo）：同一張圖（以內容雜湊比對）出現在
    >= WATERMARK_MIN_SHARE 的頁面且至少 3 頁。回傳 {digest: 出現頁數}。"""
    seen: Counter = Counter()
    for pno in pages:
        digests = {i.get("digest") for i in pdf[pno - 1].get_image_info(hashes=True) if i.get("digest")}
        seen.update(digests)
    need = max(3, WATERMARK_MIN_SHARE * len(pages))
    # 依出現頁數（多→少）、雜湊值排序：集合的走訪順序每次執行都不同，不排序會讓 manifest 無法逐字重現
    return {d: n for d, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])) if n >= need}


def extract_pdf_page(page: pymupdf.Page, pno: int, doc_id: str, body_size: float,
                     tracker: SectionTracker, watermarks: Optional[dict[bytes, int]] = None,
                     column_hint: Optional[float] = None,
                     running_labels: Optional[list[tuple[str, pymupdf.Rect]]] = None,
                     ) -> tuple[list[TextBlock], list[ExtractionIssue], PageReport]:
    """單頁擷取：文字區塊、表格、圖片／圖表／公式區域、連結、issue、完整性指標。

    column_hint：整份文件的欄位分界（見 document_column_split），交給 read_pdf_lines 驗證後使用。
    running_labels：整份文件的頁面標籤（見 find_running_labels）；標籤自成 page_header 區塊，不更新章節路徑。
    """
    h = page.rect.height
    lines, images, layout = read_pdf_lines(page, column_hint, running_labels)
    images = [r for r in images if r.width >= IMAGE_MIN_SIZE and r.height >= IMAGE_MIN_SIZE]
    wm: list[pymupdf.Rect] = []
    if watermarks:
        # 浮水印不是內容：不建立圖片區域，也不讓底下的正文被當成「圖表文字」（manifest 另有紀錄）
        # 整頁底圖的原始框可能超出頁面，matches_watermark 會一併比對裁到頁內的框
        wm = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info(hashes=True) if i.get("digest") in watermarks]
        images = [r for r in images if not matches_watermark(r, wm, page.rect)]
    words = page.get_text("words")
    issues: list[ExtractionIssue] = []
    section_at_start = tracker.path
    edge_body = margin_body_lines(lines, h)
    caption_tail = caption_continuation_lines(lines, h)

    def zone(ln: Line) -> Optional[str]:
        # 貼在左右邊緣的直排文字（出版商下載浮水印、側邊頁籤）歸為頁面邊緣文字；
        # 清洗時若在多頁重複出現會被移除，只出現一次則保留供人工確認
        if ln.rotated and (ln.bbox.x0 >= page.rect.width * (1 - SIDE_MARGIN_RATIO)
                           or ln.bbox.x1 <= page.rect.width * SIDE_MARGIN_RATIO):
            return "page_footer"
        # 圖表標題可能排在頁面最上緣（例如 Wiley 期刊整頁表格的「TABLE 5 | …」），不是頁首；
        # 表格偵測要靠標題定位，誤當頁首會讓整張表被拆成零散段落；緊貼正文的邊緣行也是正文（edge_body）
        if CAPTION_RE.match(ln.text) or id(ln) in edge_body or id(ln) in caption_tail:
            return None
        if ln.bbox.y1 <= h * MARGIN_RATIO:
            return "page_header"
        if ln.bbox.y0 >= h * (1 - MARGIN_RATIO):
            return "page_footer"
        # 頁碼可能高於底部 7%（例如臺科大論文在 92% 處）：底部 15% 內、置中、只有數字或羅馬數字
        if (ln.bbox.y0 >= h * (1 - PAGE_NUMBER_ZONE) and PAGE_NUMBER_ONLY_RE.match(ln.text)
                and abs((ln.bbox.x0 + ln.bbox.x1) / 2 - page.rect.width / 2) <= CENTER_TOLERANCE):
            return "page_footer"
        return None

    # 同一高度被切成多段的行（段與段距離 >= 40pt）= 欄位排列的文字，不能當成標題續行或一般段落
    # 只有「連續 >= 2 列」都是多段才算（單一列可能只是左右對齊拉開字距的正文）
    def find_multi_segment(pool: list[Line]) -> set[int]:
        """同一欄、同一高度被切成多段，且連續 >= 2 列 → 欄位排列的文字。
        公式編號 "(2-5)" 和公式同一列不算；單一列可能只是左右對齊拉開字距的正文。"""
        cand_lines = [ln for ln in pool if not EQUATION_LABEL_RE.match(ln.text.strip())]
        segmented = [
            a for a in cand_lines
            if any(a is not b and a.col == b.col
                   and min(a.bbox.y1, b.bbox.y1) - max(a.bbox.y0, b.bbox.y0) >= 0.5 * min(a.bbox.height, b.bbox.height)
                   for b in cand_lines)
        ]
        seg_rows: list[list[Line]] = []
        for a in sorted(segmented, key=lambda ln: ln.bbox.y0):
            if seg_rows and a.bbox.y0 < seg_rows[-1][0].bbox.y1:
                seg_rows[-1].append(a)
            else:
                seg_rows.append([a])
        found: set[int] = set()
        for i, row in enumerate(seg_rows):
            neighbours = [seg_rows[j] for j in (i - 1, i + 1) if 0 <= j < len(seg_rows)]
            if any(_vdist(row[0].bbox, n[0].bbox) <= 3 * body_size for n in neighbours):
                found.update(id(a) for a in row)
        return found

    multi_segment = find_multi_segment(lines)

    def kind_of(ln: Line, in_figure: bool) -> str:
        # 版面中間的直排文字多為圖表軸標題或旋轉表頭：不和正文組成段落，交人工確認
        if ln.rotated:
            return "figure_text"
        k = _line_kind(ln, body_size, in_figure, page.rect.width / 2)
        if k in ("text", "label", "list_item") and id(ln) in multi_segment and not in_figure:
            return "table_text"
        return k

    # 邊緣直排文字（下載浮水印）拆出的單字不可參與表格偵測：會湊出假表格，或讓真表格的欄位對不齊
    margin_rotated = [ln.bbox for ln in lines if ln.rotated and zone(ln)]
    if margin_rotated:
        words = [w for w in words
                 if not any(r.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for r in margin_rotated)]

    # 1) 先粗分一次找出「Table N」標題的位置（無格線表格要靠它定位）
    body_lines = [ln for ln in lines if zone(ln) is None]
    pre = group_lines(body_lines, [kind_of(ln, False) for ln in body_lines], body_size)
    table_caption_rects = []
    for kind, gl in pre:
        if kind == "table_caption":
            r = gl[0].bbox
            for g in gl[1:]:
                r = r | g.bbox
            table_caption_rects.append(r)

    # 2) 表格
    tables, t_issues = detect_tables(page, words, table_caption_rects, body_size, pno, layout)
    issues += t_issues
    table_rects = [t.rect for t in tables]
    lines = rescue_captions_from_table_lines(lines, table_rects)
    # 圖表叢集前先排除版面裝飾（整頁外框、浮水印底塊、文字底色框），否則會併成一個大「圖表」
    drawings = content_drawings(page, wm, lines)
    charts = [r for r in chart_regions(page, images + table_rects, drawings)
              if not is_text_box(r, lines, body_size, drawings)]
    figure_areas = images + charts

    # 3) 文字行 → 區塊（表格內的行不重複輸出）
    text_lines: list[Line] = []
    kinds: list[str] = []
    def in_table(ln: Line) -> bool:
        return any((r & ln.bbox).get_area() > 0.6 * ln.bbox.get_area() for r in table_rects if r.intersects(ln.bbox))

    # 表格的字已收進表格，排除後重算：與表格同列的正文（例如並排標題的最後一行）不再被當成欄位文字
    multi_segment = find_multi_segment([ln for ln in lines if not in_table(ln)])
    for ln in lines:
        if in_table(ln):
            continue
        z = zone(ln)
        kinds.append(z or kind_of(ln, any(r.intersects(ln.bbox) for r in figure_areas)))
        text_lines.append(ln)
    # 句子沒寫完的編號行不是標題（否則後面的章節路徑都掛在半句條列下）
    kinds = demote_unfinished_numbered_headings(text_lines, kinds)
    kinds = promote_back_matter_headings(text_lines, kinds)

    strokes = stroke_rects(page)
    page_blocks: list[TextBlock] = []
    # 詞彙表跨頁時，下一頁開頭仍在詞彙表區（章節路徑最上層是 Glossary）
    glossary_carry = bool(GLOSSARY_MARKER_RE.match((tracker.path or "").split(" > ")[0]))
    for kind, glines in group_lines(text_lines, kinds, body_size, glossary_carry):
        raw = "\n".join(g.text for g in glines)
        bbox = glines[0].bbox
        for g in glines[1:]:
            bbox = bbox | g.bbox
        block_type = "paragraph" if kind in ("text", "table_text") else kind
        if kind == "text" and _is_plain_subheading(glines, body_size):
            block_type = "heading"
        if kind == "table_text":
            # 視覺行順序：同一列的各段依 x 排，列與列換行，保留原始排列
            glines = sorted(glines, key=lambda g: (round(g.bbox.y0), g.bbox.x0))
            raw = "\n".join(g.text for g in glines)
        if len(glines) == 1 and glines[0].running_label:
            # 頁面標籤（TX001 頁邊欄「Notes」）：不是章節，不更新章節路徑；清洗時依多頁重複移除
            block_type = "page_header"
        level = None
        if block_type == "heading":
            level = heading_level_for(raw) or (
                1 if _is_upper_heading(raw) or max(g.size for g in glines) >= body_size + 3
                else 2 if any(g.first_bold for g in glines) else 3)
            tracker.push(level, raw)
        b = TextBlock(
            doc_id=doc_id, block_id="", block_type=block_type, order=0, section=tracker.path,
            heading_level=level, page_number=pno, bbox=_rect_tuple(bbox), source_locator=f"page:{pno}",
            raw_text=raw, region_kind="formula" if block_type == "formula" else None,
            has_formula=block_type == "formula" or any(g.has_math_font for g in glines) or bool(MATH_CHAR_RE.search(raw)),
        )
        garbled = any(g.garbled_math for g in glines) or any(
            SUSPECT_GLYPH_RE.search(PUA_BULLET_RE.sub("", g.text)) for g in glines)
        if kind == "table_text":
            _mark(b, "文字呈欄位排列但未能解析成表格，欄列對應未知，請對照 PDF 人工確認", "partial")
        elif block_type == "figure_text":
            _mark(b, "figure_text：圖表內的軸標籤／數值殘留，請確認是否需轉錄成表格")
        elif block_type == "formula":
            n_strokes = sum(1 for s in strokes if s.intersects(bbox + (-2, -2, 2, 2)))
            if garbled:
                _mark(b, "公式字形編碼遺失（數學字型中出現 !\"#$%* 等替代字元），原式需對照 PDF 人工轉錄", "partial")
            if n_strokes:
                _mark(b, f"公式含 {n_strokes} 個向量線條（分數線／根號），純文字無法保留結構，請對照 PDF", "partial")
        elif garbled:
            _mark(b, "數學字型或特殊字元可能對應錯誤（例如 𝐿\"），請對照原 PDF", "partial")
        elif block_type in ("paragraph", "list_item") and any(g.symbol_formula for g in glines):
            _mark(b, "Symbol 字型排版的公式（積分、上下限、分數），純文字無法保留版面，請對照原 PDF", "partial")
        if block_type == "paragraph" and kind == "text" and is_table_like_text(raw):
            _mark(b, TABLE_TEXT_REASON)
        page_blocks.append(b)

    # 4) 表格區塊
    table_blocks: list[tuple[TextBlock, TableCandidate]] = []
    for t in tables:
        ts, reasons = build_table_structure(t.rows, t.method, t.cell_bboxes)
        ts.text_coverage = round(t.coverage, 4)
        b = TextBlock(
            doc_id=doc_id, block_id="", block_type="table", order=0, page_number=pno,
            bbox=_rect_tuple(t.rect), source_locator=f"page:{pno}", raw_text=serialize_table(t.rows),
            table_data=t.rows, table=ts, region_kind="table",
            has_formula=bool(MATH_CHAR_RE.search(serialize_table(t.rows))),
        )
        for r in t.reasons + reasons:
            _mark(b, r)
        if t.coverage < TABLE_MIN_COVERAGE:
            _mark(b, f"表格範圍內僅 {t.coverage:.0%} 的文字落入儲存格，可能漏欄或錯位", "partial")
        page_blocks.append(b)
        table_blocks.append((b, t))

    # 5) 圖片與圖表區域（無法擷取內容 → failed）
    region_blocks: list[TextBlock] = []
    for kind, rects in (("image", images), ("chart", charts)):
        for r in rects:
            b = TextBlock(
                doc_id=doc_id, block_id="", block_type="image_region" if kind == "image" else "chart_region",
                order=0, page_number=pno, bbox=_rect_tuple(r), source_locator=f"page:{pno}", raw_text="",
                region_kind="image" if kind == "image" else "chart",
                parse_status="failed", needs_review=True,
                failure_reason="點陣圖片沒有文字層，內容未解析（尚未啟用 OCR）" if kind == "image"
                else "向量圖表：數據點無法結構化擷取，軸標籤／數值文字另存於 figure_text 區塊",
            )
            b.review_reasons.append(b.failure_reason)
            page_blocks.append(b)
            region_blocks.append(b)

    # 6) 依版面排序、編號、補 section
    page_blocks.sort(key=lambda b: layout.key(pymupdf.Rect(b.bbox)))  # 雙欄：先左欄再右欄
    last_section = section_at_start
    for n, b in enumerate(page_blocks, start=1):
        if b.block_type in ("table", "image_region", "chart_region"):
            b.section = last_section
        last_section = b.section if b.block_type not in ("page_header", "page_footer") else last_section
        b.block_id = f"{doc_id}_p{pno:04d}_b{n:03d}"

    # 7) 連結：標題 ↔ 表格／圖片／圖表；公式 ↔ 編號；圖表 ↔ 軸標籤
    t_caps = [b for b in page_blocks if b.block_type == "table_caption"]
    f_caps = [b for b in page_blocks if b.block_type == "figure_caption"]
    for b, _t in table_blocks:
        cap = _nearest_caption(pymupdf.Rect(b.bbox), t_caps)
        if cap:
            _link(b, cap)
            b.table.caption = " ".join(cap.raw_text.split())
            b.table.caption_block_id = cap.block_id
    caps_with_table = {b.table.caption_block_id for b, _ in table_blocks if b.table.caption_block_id}
    free_t_caps = [c for c in t_caps if c.block_id not in caps_with_table]

    def sub_caption_below(r: pymupdf.Rect) -> Optional[TextBlock]:
        """子圖標題：緊接在圖片正下方的 "(a) …" 短段落。"""
        for x in page_blocks:
            if (x.block_type in ("paragraph", "figure_caption") and SUBCAPTION_RE.match(x.raw_text)
                    and len(x.raw_text.strip()) <= 80):
                xr = pymupdf.Rect(x.bbox)
                if _hoverlap(r, xr) and 0 <= xr.y0 - r.y1 <= 25:
                    return x
        return None

    def main_caption_below(y: float) -> Optional[TextBlock]:
        """從 y 往下找第一個圖說；中間遇到長段落（> 80 字）就停。"""
        for x in sorted(page_blocks, key=lambda x: x.bbox[1]):
            if x.bbox[1] < y:
                continue
            if x.block_type == "figure_caption" and not SUBCAPTION_RE.match(x.raw_text):
                return x
            if x.block_type in ("paragraph", "heading") and len(x.raw_text.strip()) > 80:
                return None
        return None

    for b in region_blocks:
        r = pymupdf.Rect(b.bbox)
        if b.block_type == "image_region":
            host = next((tb for tb, _ in table_blocks
                         if (r & pymupdf.Rect(tb.bbox)).get_area() >= 0.8 * r.get_area()), None)
            if host:
                _link(b, host)
                b.region_kind = "figure_image"
                msg = f"表格儲存格內的圖片，圖片內容未解析；表格文字已解析於 {host.block_id}"
                b.failure_reason, b.review_reasons = msg, [msg]
                continue
            sub = sub_caption_below(r)
            if sub:
                sub.block_type = "figure_caption"
                _link(b, sub)
                b.region_kind = "figure_image"
                main = main_caption_below(sub.bbox[3])
                if main:
                    _link(b, main)
                continue
        cap = _nearest_caption(r, free_t_caps)
        if cap and b.block_type == "image_region":
            _link(b, cap)
            b.region_kind = "table_image"
            msg = "表格以點陣圖嵌入，沒有文字層，欄名與數值都無法擷取，需人工轉錄（或日後 OCR）"
            b.failure_reason, b.review_reasons = msg, [msg]
            continue
        cap = _nearest_caption(r, f_caps) or cap
        if cap:
            _link(b, cap)
            if b.block_type == "image_region":
                b.region_kind = "figure_image"
        elif b.block_type == "image_region":
            nearby = " ".join(x.raw_text for x in page_blocks
                              if x.bbox and x.block_type == "paragraph" and _vdist(r, pymupdf.Rect(x.bbox)) <= CAPTION_LINK_DIST)
            if re.search(r"\btable\b", nearby, re.I):
                msg = "無標題圖片，鄰近文字提到 table，可能是圖片表格，需人工轉錄"
                b.failure_reason, b.review_reasons = msg, [msg]
        if b.block_type == "chart_region":
            for x in page_blocks:
                if x.block_type == "figure_text" and r.intersects(pymupdf.Rect(x.bbox)):
                    _link(b, x)
    # 公式可能是數學字型（formula）或一般字型寫的段落（has_formula，例如 "∆E*ab=[(∆L*)2+…]1/2"）
    formulas = [b for b in page_blocks if b.block_type == "formula"
                or (b.block_type == "paragraph" and b.has_formula and len(b.raw_text.strip()) <= 120)]
    for lab in (b for b in page_blocks if b.block_type == "equation_label"):
        lr = pymupdf.Rect(lab.bbox)
        cand = [f for f in formulas if _vdist(lr, pymupdf.Rect(f.bbox)) <= 2 * body_size]
        if cand:
            f = min(cand, key=lambda f: _vdist(lr, pymupdf.Rect(f.bbox)))
            _link(lab, f)
        else:
            _mark(lab, "公式編號附近找不到公式文字（公式本體可能是圖片），請人工轉錄", "partial")
    orphan_table_caption = False
    for cap in t_caps:
        if not any(x.block_type in ("table", "image_region", "chart_region") for x in page_blocks
                   if x.block_id in cap.linked_block_ids):
            _mark(cap, "有表格標題但附近找不到表格或圖片區域，表格內容可能遺失，請對照 PDF")
            orphan_table_caption = True
    # 表格本體沒解析出來時，儲存格常被切成一堆短段落（例如「108 37.86 35.86」「3.3 Red lip」）；
    # 同頁短段落很多時一律交人工確認，避免數值脫離欄名後被當成證據
    if orphan_table_caption:
        fragments = [b for b in page_blocks if b.block_type in ("paragraph", "label", "list_item")
                     and len(b.raw_text.strip()) < ORPHAN_TABLE_FRAGMENT_CHARS]
        if len(fragments) >= ORPHAN_TABLE_MIN_FRAGMENTS:
            for b in fragments:
                _mark(b, TABLE_FRAGMENT_REASON)
    # 續表標記「(Continued)」「(Continues)」本身不是內容；以前只在同頁有未解析表格時被順帶標記，
    # 表格解析成功後就會被當成正文匯出（AR012 p.7、p.23），所以一律標記
    for b in page_blocks:
        if b.block_type in SIDE_FRAGMENT_TYPES and CONTINUED_MARKER_RE.match(b.raw_text):
            _mark(b, TABLE_FRAGMENT_REASON)
    # 已解析的表格可能不完整（AR001 Table 1 橫跨整頁，但標題只在左欄，表身被欄位分界截斷）：
    # 與表格同高、在表格外的短段落或數字段落，一律當表格殘片交人工確認，表格本身也標記可能不完整
    for tb, _t in table_blocks:
        leftovers = table_side_fragments(tb, page_blocks)
        for b in leftovers:
            _mark(b, TABLE_FRAGMENT_REASON)
        if leftovers:
            _mark(tb, TABLE_INCOMPLETE_REASON.format(n=len(leftovers)), "partial")
    # 同頁已有數字為主的表格本體：表頭（「Vividness intensity Lightness …」）、續表標記（「(Continues)」）
    # 等沒有句尾標點的短段落與粗體欄名（label）也是表格的一部分；標記後段落接續會略過它們，不再被欄名切斷
    if any(TABLE_TEXT_REASON in b.review_reasons for b in page_blocks):
        for b in page_blocks:
            text = b.raw_text.strip()
            if (b.block_type in ("paragraph", "label") and not b.needs_review and len(text) < TABLE_TEXT_FRAGMENT_CHARS
                    and not re.search(r"[.!?。！？]$", text)):
                _mark(b, TABLE_TEXT_FRAGMENT_REASON)

    # 8) issues（每個待確認區塊一筆，帶 bbox 與原因）
    for b in page_blocks:
        if not b.needs_review:
            continue
        issue_type, severity = {
            ("image_region", "table_image"): ("table_not_machine_readable", "error"),
            ("image_region", "figure_image"): ("figure_image_unparsed", "info"),
            ("image_region", "image"): ("image_unparsed", "warning"),
            ("chart_region", "chart"): ("chart_unparsed", "info"),
            ("formula", "formula"): ("formula_needs_review", "warning"),
            ("table", "table"): ("table_needs_review", "warning"),
            ("figure_text", None): ("figure_text_detected", "info"),
            ("equation_label", None): ("formula_not_found", "error"),
            ("table_caption", None): ("table_not_found", "error"),
        }.get((b.block_type, b.region_kind), ("needs_manual_check", "warning"))
        details = {"bbox": ",".join(f"{v:.1f}" for v in b.bbox), "parse_status": b.parse_status}
        linked_caps = [x for x in page_blocks if x.block_id in b.linked_block_ids
                       and x.block_type in ("table_caption", "figure_caption", "equation_label")]
        if linked_caps:
            details["caption"] = " ".join(linked_caps[0].raw_text.split())[:200]
            details["caption_block_id"] = linked_caps[0].block_id
        issues.append(ExtractionIssue(
            issue_type=issue_type, severity=severity, stage="extracted", page_number=pno,
            block_id=b.block_id, message="；".join(b.review_reasons), details=details,
        ))

    # 9) 完整性指標：頁面文字層的字元是否都被收進某個 block
    want = char_counter(page.get_text("text"))
    got = Counter()
    for b in page_blocks:
        got += char_counter("".join(c or "" for r in b.table_data for c in r) if b.table_data is not None else b.raw_text)
    missing = want - got
    total = sum(want.values())
    coverage = 1 - sum(missing.values()) / total if total else 1.0
    if not lines and images:
        issues.append(ExtractionIssue(
            issue_type="page_without_text_layer", severity="error", stage="extracted", page_number=pno,
            message="此頁只有影像沒有文字層，可能是掃描頁，需要 OCR 或人工轉錄。",
        ))
    if total and coverage < PAGE_MIN_COVERAGE:
        issues.append(ExtractionIssue(
            issue_type="page_text_coverage_low", severity="error", stage="extracted", page_number=pno,
            message=f"頁面文字只有 {coverage:.1%} 被收錄到區塊，可能有內容遺失。",
            details={"missing_sample": "".join(sorted(missing.elements()))[:80]},
        ))

    report = build_page_report(pno, page_blocks, [i for i in issues if i.page_number == pno], total,
                               total - sum(missing.values()), coverage,
                               tables_rejected=sum(1 for i in issues if i.issue_type == "table_low_confidence"))
    return page_blocks, issues, report


def build_page_report(pno: int, blocks: list[TextBlock], issues: list[ExtractionIssue], text_chars: int,
                      captured_chars: int, coverage: float, tables_rejected: int = 0) -> PageReport:
    """彙整單頁狀態：failed（無文字層）> needs_review（任何待確認區塊或 warning/error issue）> ok。"""
    reasons = sorted({i.issue_type for i in issues if i.severity in ("warning", "error")}
                     | {i.issue_type for i in issues if i.block_id and any(b.block_id == i.block_id and b.needs_review for b in blocks)})
    if any(i.issue_type == "page_without_text_layer" for i in issues):
        status = "failed"
    elif reasons or any(b.needs_review for b in blocks):
        status = "needs_review"
    else:
        status = "ok"
    return PageReport(
        page_number=pno, status=status, text_chars=text_chars, captured_chars=captured_chars,
        text_coverage=round(coverage, 4), blocks=len(blocks),
        tables_parsed=sum(b.block_type == "table" for b in blocks),
        tables_failed=sum(b.region_kind == "table_image" for b in blocks)
        + sum(b.block_type == "table_caption" and b.needs_review for b in blocks),
        image_regions=sum(b.block_type == "image_region" for b in blocks),
        chart_regions=sum(b.block_type == "chart_region" for b in blocks),
        formulas=sum(b.block_type == "formula" for b in blocks),
        formulas_need_review=sum(b.block_type == "formula" and b.needs_review for b in blocks),
        reasons=reasons + (["table_low_confidence"] if tables_rejected and "table_low_confidence" not in reasons else []),
    )


def extract_pdf(path: Path, doc_id: str, pages_spec: Optional[str] = None) -> Document:
    """擷取 PDF → Document(stage='extracted')，含每頁 PageReport。"""
    blocks: list[TextBlock] = []
    issues: list[ExtractionIssue] = []
    reports: list[PageReport] = []
    tracker = SectionTracker()

    with pymupdf.open(path) as pdf:  # 唯讀開啟，不呼叫任何 save
        pages = parse_page_spec(pages_spec, pdf.page_count)
        body_size = body_font_size(pdf, pages)
        meta = {k: v for k, v in (pdf.metadata or {}).items() if v}
        watermarks = find_watermarks(pdf, pages)
        column_hint = document_column_split(pdf, pages)
        if column_hint is not None:
            meta["document_column_split_x"] = f"{column_hint:.1f}"
        if watermarks:
            meta["watermark_images_excluded"] = "; ".join(
                f"digest={d.hex()[:12]} on {n}/{len(pages)} pages" for d, n in watermarks.items())
        running_labels = find_running_labels(pdf, pages)
        if running_labels:  # 只在有標籤時記錄，沒有的文件 manifest 不變
            meta["running_labels_excluded"] = "; ".join(
                f"{t!r}@({r.x0:.0f},{r.y0:.0f})" for t, r in running_labels)
        for pno in pages:
            pb, pi, pr = extract_pdf_page(pdf[pno - 1], pno, doc_id, body_size, tracker, watermarks, column_hint,
                                          running_labels)
            for b in pb:
                b.order = len(blocks)
                blocks.append(b)
            issues += pi
            reports.append(pr)

        manifest = SourceManifest(
            doc_id=doc_id, source_path=rel_posix(path), source_type="pdf",
            sha256=sha256_of(path), file_size_bytes=path.stat().st_size,
            title=meta.get("title") or None, total_pages=pdf.page_count,
            pages_processed=pages, extractor=f"pymupdf {pymupdf.VersionBind}",
            metadata={**{k: str(v) for k, v in meta.items()}, "body_font_size": str(body_size)},
        )
    return Document(stage="extracted", manifest=manifest, blocks=blocks, issues=issues, pages=reports)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

DROP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
             "input", "select", "textarea", "button", "head", "link", "meta"}
HEADING_TAGS = {f"h{i}" for i in range(1, 7)}
LEAF_BLOCK_TAGS = HEADING_TAGS | {"p", "li", "figcaption", "caption", "blockquote", "pre",
                                  "dt", "dd", "address"}
CONTAINER_TAGS = {"html", "body", "div", "section", "article", "main", "header", "footer",
                  "nav", "aside", "ul", "ol", "dl", "figure", "form", "details", "summary"}
BLOCK_LEVEL = LEAF_BLOCK_TAGS | CONTAINER_TAGS | {"table", "hr"}


def describe_tag(tag: Tag) -> str:
    """'div#main.content.post' 形式的節點描述，供 02_clean 判斷雜訊。"""
    s = tag.name
    if tag.get("id"):
        s += f"#{tag['id']}"
    classes = tag.get("class") or []
    if classes:
        s += "." + ".".join(classes)
    return s


def dom_path(tag: Tag) -> str:
    """'body>div[1]>article[1]>p[3]'：用 nth-of-type 標示位置。"""
    parts = []
    node: Optional[Tag] = tag
    while node is not None and isinstance(node, Tag) and node.name not in ("[document]", "html"):
        parent = node.parent
        if parent is not None:
            same = [c for c in parent.find_all(node.name, recursive=False)]
            idx = same.index(node) + 1 if node in same else 1
            parts.append(f"{node.name}[{idx}]")
        else:
            parts.append(node.name)
        node = parent
    return ">".join(reversed(parts))


def inline_text(node) -> str:
    """取得節點文字：保留原始空白、<br> → 換行、<sub>/<sup> 直接串接（ΔE<sub>00</sub> → ΔE00）。"""
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in DROP_TAGS:
        return ""
    if node.name == "br":
        return "\n"
    inner = "".join(inline_text(c) for c in node.children)
    if node.name in BLOCK_LEVEL:
        return "\n" + inner + "\n"
    return inner


def _has_block_descendant(tag: Tag) -> bool:
    return any(isinstance(d, Tag) and d.name in BLOCK_LEVEL for d in tag.descendants)


def is_custom_container(tag: Tag) -> bool:
    """判斷節點是不是「包住區塊內容的自訂元素」，要當成容器往下拆，而不是行內元素。

    參數：
        tag：BeautifulSoup 的元素節點。

    回傳：
        True 表示同時符合：
          1. 標籤名稱含 '-'：HTML 規範要求自訂元素（Vue／Web Components）名稱必須有連字號，
             例如媚比琳的 <article-page-wrapper>、L'Oréal 的 <oap-accordion>。
          2. 底下還有區塊層級元素（p、h2、li、div…）。
        其他情況回傳 False，維持原本當成行內元素處理。

    為什麼需要：
        walk() 只認得 BLOCK_LEVEL 內的標準標籤；不認得的標籤一律當成行內元素（像 <span>），
        把整棵子樹壓成一個 paragraph。媚比琳整篇文章包在 <article-page-wrapper> 裡，
        因此標題、步驟與說明全部黏成一段。只有底下確實有區塊元素時才拆開，
        純文字的自訂元素（例如 <oap-favorite>愛心</oap-favorite>）仍視為行內。
    """
    return "-" in tag.name and _has_block_descendant(tag)


def extract_html(path: Path, doc_id: str) -> Document:
    """擷取 HTML → Document(stage='extracted')。"""
    data = path.read_bytes()
    soup = BeautifulSoup(data, "html.parser")
    blocks: list[TextBlock] = []
    issues: list[ExtractionIssue] = []
    tracker = SectionTracker()
    dropped: Counter = Counter()

    meta: dict[str, str] = {}
    title = soup.title.get_text(strip=True) if soup.title else None
    for key, attrs in {
        "og_title": {"property": "og:title"}, "author": {"name": "author"},
        "published": {"property": "article:published_time"}, "description": {"name": "description"},
    }.items():
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            meta[key] = m["content"]
    canonical = soup.find("link", rel="canonical")
    source_url = canonical.get("href") if canonical else None
    if soup.original_encoding:
        meta["detected_encoding"] = soup.original_encoding

    root = soup.body or soup

    def ctx(tag: Tag) -> list[str]:
        chain = [describe_tag(p) for p in reversed(list(tag.parents)) if isinstance(p, Tag) and p.name not in ("[document]", "html")]
        return chain + [describe_tag(tag)]

    def emit(block_type: str, tag: Tag, raw: str, *, locator: Optional[str] = None,
             level: Optional[int] = None, table: Optional[list] = None, reasons: Optional[list[str]] = None,
             **extra) -> TextBlock:
        reasons = reasons or []
        b = TextBlock(
            doc_id=doc_id, block_id=f"{doc_id}_h{len(blocks) + 1:04d}", block_type=block_type,
            order=len(blocks), section=tracker.path, heading_level=level,
            source_locator=locator or dom_path(tag), html_context=ctx(tag), raw_text=raw,
            table_data=table, has_formula=bool(MATH_CHAR_RE.search(raw)) or tag.find("math") is not None,
            needs_review=bool(reasons), review_reasons=reasons, **extra,
        )
        blocks.append(b)
        return b

    def emit_table(tag: Tag) -> None:
        cap = tag.find("caption")
        cap_block = None
        if cap and cap.get_text(strip=True):
            cap_block = emit("table_caption", cap, inline_text(cap).strip("\n"))
        rows: list[list[Optional[str]]] = []
        reasons: list[str] = []
        header_hint = 0
        counting_header = True
        if tag.find("table"):
            reasons.append("巢狀表格，欄位對應可能錯誤")
        for tr in tag.find_all("tr"):
            if tr.find_parent("table") is not tag:
                continue
            row: list[Optional[str]] = []
            cells = tr.find_all(["td", "th"], recursive=False)
            for cell in cells:
                row.append(inline_text(cell).strip("\n"))
                span = int(cell.get("colspan", 1) or 1)
                if span > 1:
                    row.extend([None] * (span - 1))
                    reasons.append("含合併儲存格 (colspan)，已以 None 補位，請確認欄位對應")
                if int(cell.get("rowspan", 1) or 1) > 1:
                    reasons.append("含合併儲存格 (rowspan)，下方列未補位，請確認欄位對應")
            is_header = bool(cells) and (tr.find_parent("thead") is not None or all(c.name == "th" for c in cells))
            if counting_header and is_header:
                header_hint += 1
            else:
                counting_header = False
            rows.append(row)
        ok, stats = table_quality(rows) if rows else (False, {"rows": "0", "cols": "0", "fill_ratio": "0"})
        if not ok:
            reasons.append(f"表格內容不足或結構異常 ({stats})")
        ts, s_reasons = build_table_structure(rows, "html", header_rows_hint=header_hint or None) if rows else (
            TableStructure(detection_method="html", n_rows=0, n_cols=0), [])
        b = TextBlock(
            doc_id=doc_id, block_id=f"{doc_id}_h{len(blocks) + 1:04d}", block_type="table", order=len(blocks),
            section=tracker.path, source_locator=dom_path(tag), html_context=ctx(tag),
            raw_text=serialize_table(rows), table_data=rows, table=ts, region_kind="table",
            has_formula=bool(MATH_CHAR_RE.search(serialize_table(rows))),
            needs_review=bool(reasons + s_reasons), review_reasons=sorted(set(reasons + s_reasons)),
        )
        if not rows:
            b.parse_status, b.failure_reason = "failed", "表格沒有任何列"
        blocks.append(b)
        if cap_block is not None:
            _link(b, cap_block)
            ts.caption = " ".join(cap_block.raw_text.split())
            ts.caption_block_id = cap_block.block_id
        if b.needs_review:
            issues.append(ExtractionIssue(
                issue_type="table_needs_review", severity="warning", stage="extracted",
                block_id=b.block_id, message="；".join(b.review_reasons), details=stats,
            ))

    def emit_figure_image(fig: Tag) -> None:
        """<figure><img> 沒有 <table>：建立 image_region（failed），再處理 figcaption 並互相連結。"""
        img = fig.find("img")
        cap = fig.find("figcaption")
        cap_text = " ".join(inline_text(cap).split()) if cap else ""
        is_table = bool(TABLE_CAPTION_RE.match(cap_text))
        src, alt = str(img.get("src", "")), str(img.get("alt", ""))
        msg = (f"表格以圖片呈現（img src='{src}'），欄名與數值無法擷取，需人工轉錄" if is_table
               else f"圖片內容未解析（img src='{src}' alt='{alt}'），請確認是否含數據")
        region = emit("image_region", img, "", reasons=[msg], parse_status="failed", failure_reason=msg,
                      region_kind="table_image" if is_table else ("figure_image" if cap_text else "image"))
        start = len(blocks)
        walk(fig)
        for b in blocks[start:]:
            if b.block_type in ("table_caption", "figure_caption"):
                _link(region, b)
        issues.append(ExtractionIssue(
            issue_type="table_not_machine_readable" if is_table else "figure_image_unparsed",
            severity="error" if is_table else "info", stage="extracted", block_id=region.block_id, message=msg,
            details={"img_src": src, "img_alt": alt, "caption": cap_text[:200]},
        ))

    def walk(container: Tag) -> None:
        run: list = []

        def flush() -> None:
            if not run:
                return
            raw = "".join(inline_text(n) for n in run)
            if raw.strip():
                emit("paragraph", container, raw.strip("\n"), locator=dom_path(container) + ">#text")
            run.clear()

        for child in list(container.children):
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                run.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in DROP_TAGS:
                dropped[name] += 1
                continue
            if is_custom_container(child):
                flush()
                walk(child)  # 自訂元素包著區塊內容：當成容器往下拆
                continue
            if name not in BLOCK_LEVEL:
                run.append(child)  # inline 元素（a, span, strong, sub, sup, math…）
                continue
            flush()
            if name == "table":
                emit_table(child)
            elif name == "hr":
                continue
            elif name == "figure" and child.find("img") and not child.find("table"):
                emit_figure_image(child)
            elif name in LEAF_BLOCK_TAGS and not _has_block_descendant(child):
                raw = inline_text(child).strip("\n")
                if not raw.strip():
                    continue
                if name in HEADING_TAGS:
                    level = int(name[1])
                    tracker.push(level, raw)
                    emit("heading", child, raw, level=level)
                elif name in ("figcaption", "caption"):
                    emit("table_caption" if TABLE_CAPTION_RE.match(raw) else "figure_caption", child, raw)
                elif name == "li":
                    emit("list_item", child, raw)
                else:
                    emit("paragraph", child, raw)
            else:
                walk(child)
        flush()

    walk(root)

    # 覆蓋率檢查：確認可見文字都有被擷取到某個 block
    def nonspace_len(s: str) -> int:
        return len(re.sub(r"\s+", "", s))

    visible = nonspace_len(inline_text(root))
    captured = sum(
        nonspace_len("".join(c or "" for row in b.table_data for c in row)) if b.table_data is not None
        else nonspace_len(b.raw_text)
        for b in blocks
    )
    coverage = captured / visible if visible else 1.0
    if coverage < 0.95:
        issues.append(ExtractionIssue(
            issue_type="html_low_coverage", severity="warning", stage="extracted",
            message=f"擷取文字僅佔可見文字 {coverage:.0%}，可能有內容未被擷取。",
        ))
    meta["text_coverage"] = f"{coverage:.3f}"
    if dropped:
        meta["dropped_non_content_tags"] = ", ".join(f"{k}={v}" for k, v in sorted(dropped.items()))

    manifest = SourceManifest(
        doc_id=doc_id, source_path=rel_posix(path), source_type="html",
        sha256=sha256_of(path), file_size_bytes=path.stat().st_size,
        title=title or meta.get("og_title"), source_url=source_url,
        extractor=f"beautifulsoup4 {bs4.__version__} (html.parser)", metadata=meta,
    )
    return Document(stage="extracted", manifest=manifest, blocks=blocks, issues=issues)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SUPPORTED = {".pdf": "pdf", ".html": "html", ".htm": "html"}


def iter_inputs(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in SUPPORTED)
        elif p.suffix.lower() in SUPPORTED:
            out.append(p)
        else:
            raise ValueError(f"不支援的檔案類型：{p}")
    return out


def extract_file(path: Path, pages: Optional[str] = None, doc_id: Optional[str] = None) -> Document:
    """依副檔名分派到 extract_pdf / extract_html。"""
    doc_id = doc_id or make_doc_id(path)
    kind = SUPPORTED[path.suffix.lower()]
    return extract_pdf(path, doc_id, pages) if kind == "pdf" else extract_html(path, doc_id)


def run(inputs: list[Path], out_dir: Path, review_dir: Path, pages: Optional[str],
        doc_id: Optional[str] = None) -> list[Path]:
    ensure_outside_raw(out_dir)
    ensure_outside_raw(review_dir)
    written = []
    files = iter_inputs(inputs)
    if doc_id and len(files) != 1:
        raise ValueError("--doc-id 只能用在單一檔案")
    for path in files:
        before = sha256_of(path)
        doc = extract_file(path, pages, doc_id)
        if sha256_of(path) != before:  # 理論上不可能，但仍檢查原始檔未被改動
            raise RuntimeError(f"原始檔在擷取過程中被修改：{path}")
        out = doc.save(out_dir / f"{doc.manifest.doc_id}.json")
        review = review_dir / f"{doc.manifest.doc_id}.extract_review.json"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(json.dumps(doc.review_queue(), ensure_ascii=False, indent=2), encoding="utf-8")
        counts = Counter(b.block_type for b in doc.blocks)
        print(f"[extract] {path.name} → {rel_posix(out)}")
        print(f"          blocks={len(doc.blocks)} {dict(sorted(counts.items()))}")
        if doc.pages:
            st = Counter(p.status for p in doc.pages)
            print(f"          pages ok={st['ok']} needs_review={st['needs_review']} failed={st['failed']}")
        print(f"          issues={len(doc.issues)} review_blocks={sum(b.needs_review for b in doc.blocks)} → {rel_posix(review)}")
        written.append(out)
    return written


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LipMatch AI 文獻擷取 (PDF/HTML → JSON)")
    ap.add_argument("inputs", nargs="+", type=Path, help="PDF/HTML 檔案或資料夾")
    ap.add_argument("--pages", help="PDF 頁碼範圍，如 1-12 或 1,3,5-7（1-based）")
    ap.add_argument("--doc-id", help="指定 doc_id（預設由檔名產生；中文檔名建議指定，如 TH014）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    args = ap.parse_args(argv)
    run(args.inputs, args.out, args.review_dir, args.pages, args.doc_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
