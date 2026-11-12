"""02_clean.py — 清洗 01_extract 的輸出：PDF 異常斷行、重複頁首頁尾、HTML 雜訊。

用法：
    python scripts/02_clean.py data/interim/extracted/TH013.json
    python scripts/02_clean.py data/interim/extracted/      # 整個資料夾

輸出：
    data/processed/cleaned/<doc_id>.json           Document(stage="cleaned")
    data/processed/reports/<doc_id>.clean_report.md  清洗前後對照
    data/review/<doc_id>.clean_review.json         待人工確認清單

原則：
- 永遠從 raw_text 重新計算 cleaned_text（不疊加清洗），所以重跑結果一致。
- 只做「格式」層級的修正（斷行、空白、Unicode、雜訊區塊），不改寫、不摘要內容。
- 被移除的區塊只是標記 is_removed，raw_text 仍完整保留。
- 每個區塊清洗後都會比對數字、CIELAB 符號 (L* a* b* ΔE…) 與公式符號；
  若有任何遺失 → 退回原文並標記 needs_review。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Optional, Union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lipmatch.citations import find_citations, find_numeric_citations, parse_reference_list  # noqa: E402
from lipmatch.html_scope import REVIEW_PREFIX, ScopeChecker  # noqa: E402
from lipmatch.eligibility import (EXERCISE_MARKER_RE, GLOSSARY_MARKER_RE,  # noqa: E402
                                  OBJECTIVES_MARKER_RE, classify_blocks, is_boilerplate, marker_region_ids)
from schemas import Document, ExtractionIssue, TextBlock  # noqa: E402

RAW_DIR = ROOT / "data" / "raw"
DEFAULT_OUT_DIR = ROOT / "data" / "processed" / "cleaned"
DEFAULT_REPORT_DIR = ROOT / "data" / "processed" / "reports"
DEFAULT_REVIEW_DIR = ROOT / "data" / "review"

# ---------------------------------------------------------------------------
# 文字層級：Unicode、斷行、空白
# ---------------------------------------------------------------------------

# 只展開排版用連字；刻意「不」用 NFKC，因為 NFKC 會把 ² → 2、𝐿 → L、½ → 1⁄2，破壞公式。
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st"}
SPACE_CHARS = {" ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
               "　": "　"}  # 全形空白保留給中文排版
INVISIBLE_CHARS = {"­", "​", "‌", "‍", "⁠", "﻿"}

CJK = r"㐀-䶿一-鿿豈-﫿　-〿＀-￯"
CJK_END_RE = re.compile(f"[{CJK}]$")
CJK_START_RE = re.compile(f"^[{CJK}]")
URL_TAIL_RE = re.compile(r"(https?://|www\.)[^\s]*[^\s.,;)]$")


PUA_BULLET_LINE_RE = re.compile(r"(?m)^([ \t]*)[\uf000-\uf0ff][ \t]*")


def normalize_pua_bullets(text: str) -> tuple[str, list[str]]:
    """Word 的 Symbol／Wingdings 項目符號（私有區 U+F000–F0FF，例如 U+F06C）只出現在行首時換成 "• "。
    行中的私有區字元（例如 Symbol 字型的 Δ = U+F044）不動，留給人工確認。"""
    out = PUA_BULLET_LINE_RE.sub(lambda m: m.group(1) + "• ", text)
    return out, (["normalized_pua_bullet"] if out != text else [])


def normalize_unicode(text: str) -> tuple[str, list[str]]:
    """NFC 正規化 + 展開連字 + 不換行空白轉一般空白 + 移除軟連字號/零寬字元。

    輸入：任意字串。輸出：(新字串, 套用的操作名稱列表)。
    """
    ops: list[str] = []
    out = unicodedata.normalize("NFC", text)
    if out != text:
        ops.append("unicode_nfc")
    before = out
    for k, v in LIGATURES.items():
        out = out.replace(k, v)
    if out != before:
        ops.append("expanded_ligatures")
    before = out
    for k, v in SPACE_CHARS.items():
        out = out.replace(k, v)
    if out != before:
        ops.append("normalized_special_spaces")
    before = out
    out = "".join(ch for ch in out if ch not in INVISIBLE_CHARS)
    if out != before:
        ops.append("removed_invisible_chars")
    return out, ops


TYPESET_HYPHEN_RATE = 0.02   # 行尾連字號佔全部行數 >= 2% → 視為排版軟體自動斷字（期刊 InDesign 等）
WORD_TOKEN_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")


class HyphenationPolicy:
    """整份文件的行尾斷字判斷（"X-⏎y" 要不要去掉連字號）。

    依據文件本身的用字，不靠外部字典：
    1. 完整的字 "Xy" 在文件其他地方出現過 → 去掉連字號（colour、techniques）。
    2. 帶連字號的 "X-y" 在文件其他地方（行內）出現過 → 保留（self-esteem）。
    3. 都沒出現過：若整份文件行尾斷字很頻繁（排版軟體自動斷字）→ 去掉並標記為推測；
       否則（Word 論文，斷字多半是真的複合詞）→ 保留。
    """

    def __init__(self, whole: set[str], hyphenated: set[str], typeset: bool) -> None:
        self.whole, self.hyphenated, self.typeset = whole, hyphenated, typeset

    def decide(self, left: str, right: str) -> tuple[bool, str]:
        """回傳 (是否去掉連字號, 記錄到 cleaning_ops 的操作名稱)。"""
        if (left + right).lower() in self.whole:
            return True, "dehyphenated"
        if f"{left}-{right}".lower() in self.hyphenated:
            return False, "joined_hyphenated_word_kept_hyphen"
        if self.typeset:
            return True, "dehyphenated_typeset_guess"
        return False, "joined_hyphenated_word_kept_hyphen"


def build_hyphenation_policy(blocks: list[TextBlock]) -> HyphenationPolicy:
    """從所有區塊的 raw_text 收集文件用字，並估計是否為排版軟體自動斷字。"""
    whole: set[str] = set()
    hyphenated: set[str] = set()
    n_lines = n_line_end = 0
    for b in blocks:
        text = "".join(ch for ch in unicodedata.normalize("NFC", b.raw_text) if ch not in INVISIBLE_CHARS)
        for ln in text.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            n_lines += 1
            if re.search(r"[A-Za-z]-$", ln):
                n_line_end += 1
            for tok in WORD_TOKEN_RE.findall(ln):
                (hyphenated if "-" in tok else whole).add(tok.lower())
    typeset = n_lines > 0 and n_line_end / n_lines >= TYPESET_HYPHEN_RATE
    return HyphenationPolicy(whole, hyphenated, typeset)


def fix_pdf_line_breaks(text: str, dehyphenate: Union[bool, Callable[[str, str], tuple[bool, str]]] = False,
                        ) -> tuple[str, list[str]]:
    """把段落內的硬換行接回一行。

    規則：
    - 一般換行 → 一個空白。
    - 行尾是「字母-」且下一行以小寫開頭（如 self-\\nesteem、col-\\nour）：
      dehyphenate=False 保留連字號；True 一律刪除；傳入 HyphenationPolicy.decide 則依文件用字逐一判斷。
    - 行尾是「 -」（前有空白，如 a* -\\nb*）：視為運算/區間符號，以空白相接。
    - 行尾是尚未結束的網址（https://…）：直接相接，不插空白。
    - 中文（CJK）與中文相接時不插入空白。
    - 空白行直接略過（段落內不應出現）。
    """
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) <= 1:
        out = lines[0] if lines else ""
        return out, (["stripped_line_whitespace"] if out != text else [])
    ops: set[str] = {"joined_line_breaks"}
    out = lines[0]
    for nxt in lines[1:]:
        if re.search(r"[A-Za-z]-$", out) and re.match(r"[a-z]", nxt):
            if callable(dehyphenate):
                left = re.search(r"([A-Za-z]+)-$", out).group(1)
                right = re.match(r"[a-z]+", nxt).group(0)
                remove, op = dehyphenate(left, right)
            else:
                remove, op = bool(dehyphenate), ("dehyphenated" if dehyphenate else "joined_hyphenated_word_kept_hyphen")
            out = (out[:-1] if remove else out) + nxt
            ops.add(op)
        elif URL_TAIL_RE.search(out) and not re.match(r"[A-Z(]", nxt):
            out = out + nxt  # 被換行切斷的網址
            ops.add("joined_broken_url")
        elif CJK_END_RE.search(out) and CJK_START_RE.match(nxt):
            out = out + nxt
            ops.add("joined_cjk_without_space")
        else:
            out = out + " " + nxt
    return out, sorted(ops)


def collapse_whitespace(text: str) -> tuple[str, list[str]]:
    """連續空白（不含換行）壓成一個並去除頭尾空白。"""
    out = re.sub(r"[ \t\f\v]+", " ", text).strip()
    out = re.sub(r" *\n *", "\n", out)
    return out, (["collapsed_whitespace"] if out != text else [])


def collapse_toc_leaders(text: str) -> tuple[str, list[str]]:
    """目錄列的 '.......' 引導點縮成 ' … '，頁碼保留。"""
    out = re.sub(r"\s*\.{4,}\s*", " … ", text)
    return out, (["collapsed_toc_leaders"] if out != text else [])


# ---------------------------------------------------------------------------
# 內容保全檢查
# ---------------------------------------------------------------------------

PROTECTED_RE = re.compile(
    r"\d+(?:[.,]\d+)*"                            # 數字（含小數、千分位）
    r"|[A-Za-zΔ∆]{1,3}\*[A-Za-z0-9]{0,3}"         # L* a* b* C*ab ΔE*ab h*
    r"|[Δ∆][A-Za-z]+\d*"                           # ΔE ΔE00 ΔL Δa
    r"|[\U0001D400-\U0001D7FF]"                   # 數學斜體字母 𝐿 𝑎 𝑏
    r"|[±°√∑∏∫≤≥≈≠×÷=<>%‰²³¹⁰⁴-⁹₀-₉µμ−+]"       # 公式／單位符號
)


def protected_tokens(text: str) -> Counter:
    """抽出必須在清洗後原樣保留的 token（數值、CIELAB 符號、公式符號），回傳計數。"""
    return Counter(PROTECTED_RE.findall(unicodedata.normalize("NFC", text)))


def verify_content_preserved(raw: str, cleaned: str) -> list[str]:
    """比對 raw 與 cleaned 的受保護 token；回傳遺失（或數量變少）的 token 清單，空清單表示通過。"""
    missing = protected_tokens(raw) - protected_tokens(cleaned)
    return sorted(missing.elements())


# ---------------------------------------------------------------------------
# 搜尋用文字（search_text）
# ---------------------------------------------------------------------------

# cleaned_text 保留原文（不做 NFKC、不修正原文錯字）；search_text 只給搜尋／embedding 用，
# 讓 𝐿 能被 "L" 搜到、原文黏字（如 p.14 "CIE Labcolour"）能被 "Lab colour" 搜到。
SEARCH_CHAR_MAP = {"∆": "Δ", "∗": "*", "−": "-"}  # NFKC 不處理的同義字元
GLUED_TERM_RE = re.compile(r"\b(CIELAB|CIELab|Lab|sRGB|RGB|XYZ)([a-z]{4,})\b")


def word_vocabulary(texts: Iterable[str]) -> Counter:
    """全文小寫單字頻率，用來判斷黏字拆開後的後半段是不是文件裡真的出現過的字。"""
    return Counter(w.lower() for t in texts for w in re.findall(r"[A-Za-z]+", t))


def build_search_text(text: str, vocab: Counter) -> tuple[str, list[str]]:
    """由 cleaned_text 產生 search_text：NFKC、統一數學符號、拆開領域術語黏字、換行改空白。

    黏字只在「後半段本身在文件中單獨出現過」時才拆，避免把 Laboratory 拆成 Lab oratory。
    """
    ops: list[str] = []
    out = unicodedata.normalize("NFKC", text)
    for k, v in SEARCH_CHAR_MAP.items():
        out = out.replace(k, v)
    if out != text:
        ops.append("search:nfkc")

    def split(m: re.Match) -> str:
        return f"{m.group(1)} {m.group(2)}" if vocab[m.group(2).lower()] > 0 else m.group(0)

    before = out
    out = GLUED_TERM_RE.sub(split, out)
    if out != before:
        ops.append("search:split_glued_term")
    out = re.sub(r"\s+", " ", out).strip()
    return out, ops


# ---------------------------------------------------------------------------
# 區塊層級：頁首頁尾、HTML 雜訊
# ---------------------------------------------------------------------------

PAGE_NUMBER_RE = re.compile(r"^\s*(page\s*)?(\d{1,4}|[ivxlcdm]{1,7})(\s*(of|/)\s*\d{1,4})?\s*$", re.I)


def margin_signature(text: str) -> str:
    """頁首頁尾的比對簽名：小寫、壓空白、數字換成 #（讓 'Page 3' 與 'Page 4' 相同）。"""
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def detect_repeated_margin_text(blocks: list[TextBlock], min_pages: int = 3) -> dict[str, str]:
    """找出應移除的頁首頁尾區塊。

    - 純頁碼（'12'、'Page 3 of 90'、'xii'）→ 移除（頁碼已存在 page_number 欄位）。
    - 同一簽名出現在 >= min(min_pages, 處理頁數) 個不同頁面 → 視為重複頁首頁尾而移除。
    - 只出現一次的頁尾文字（例如註腳）保留。
    回傳 {block_id: removal_reason}。
    """
    margin = [b for b in blocks if b.block_type in ("page_header", "page_footer")]
    n_pages = len({b.page_number for b in blocks if b.page_number is not None})
    threshold = max(2, min(min_pages, n_pages))
    pages_by_sig: dict[str, set] = defaultdict(set)
    for b in margin:
        pages_by_sig[(b.block_type, margin_signature(b.raw_text))].add(b.page_number)

    removed: dict[str, str] = {}
    for b in margin:
        if PAGE_NUMBER_RE.match(b.raw_text):
            removed[b.block_id] = f"{b.block_type}:page_number"
        elif len(pages_by_sig[(b.block_type, margin_signature(b.raw_text))]) >= threshold:
            removed[b.block_id] = f"{b.block_type}:repeated_on_{len(pages_by_sig[(b.block_type, margin_signature(b.raw_text))])}_pages"
    removed.update(detect_running_heads_outside_margin(blocks, threshold))
    return removed


RUNNING_HEAD_MAX_CHARS = 120   # 書眉、頁碼都是短的單行
RUNNING_HEAD_EDGE_TOL = 2.0    # pt：與該頁最上／最下文字的距離
RUNNING_HEAD_Y_TOL = 3.0       # pt：各頁同一書眉的高度差
RUNNING_HEAD_MAX_HEIGHT = 20.0 # pt：書眉、頁碼都是單行；比這高的區塊是多行正文，不可能是書眉


def is_running_head_shape(text: str, height: float) -> bool:
    """區塊外形是否可能是書眉／頁尾。

    單行：高度 <= RUNNING_HEAD_MAX_HEIGHT 且 <= RUNNING_HEAD_MAX_CHARS 字。
    兩行：其中一行是純頁碼、另一行 <= RUNNING_HEAD_MAX_CHARS 字，高度 <= 2 × RUNNING_HEAD_MAX_HEIGHT
    （TX001 教材頁尾「Salon Reception Duties ⏎ 57」，章名與頁碼上下排、被組成同一區塊）。
    兩行的正文段落不會有一行是純頁碼，所以不會因此放寬到一般段落。
    """
    if not text or len(text) > 2 * RUNNING_HEAD_MAX_CHARS:
        return False
    rows = [s.strip() for s in text.split("\n")]
    if len(rows) == 1:
        return len(text) <= RUNNING_HEAD_MAX_CHARS and height <= RUNNING_HEAD_MAX_HEIGHT
    return (len(rows) == 2 and all(rows) and height <= 2 * RUNNING_HEAD_MAX_HEIGHT
            and sum(bool(PAGE_NUMBER_RE.match(s)) for s in rows) == 1
            and all(len(s) <= RUNNING_HEAD_MAX_CHARS for s in rows))


def detect_running_heads_outside_margin(blocks: list[TextBlock], threshold: int) -> dict[str, str]:
    """頁首頁尾區（上下 7%）以外的書眉與頁碼：01 判成 paragraph／label，需要另外找出來移除。

    參數：
        blocks：PDF 擷取區塊。
        threshold：同一文字至少出現在幾個不同頁面才算重複書眉（與 detect_repeated_margin_text 相同）。

    回傳：
        {block_id: removal_reason}。

    規則：區塊是該頁最上方或最下方的文字（容許 2pt）、外形像書眉（is_running_head_shape：單行，
    或「一行文字＋一行純頁碼」），而且同一簽名（數字換成 #）
    在 >= threshold 個頁面出現在相同高度（容許 3pt）。純頁碼也照這個規則（頁碼左右交替，但高度相同）。
    例：AR004 頁高 728pt，書眉「The Japanese Journal of Psychonomic Science Vol. 36, No. 1」與頁碼在 53–64pt，
    超出上緣 7%（51pt），原本被當成正文，第 11 頁的正文還被接在書眉後面匯出。
    正文段落不會在多頁的同一高度重複出現相同文字，所以不會誤刪。
    """
    text_blocks = [b for b in blocks if b.bbox and b.page_number is not None and not b.is_removed
                   and b.block_type not in ("image_region", "chart_region", "page_header", "page_footer")]
    top: dict[int, float] = {}
    bottom: dict[int, float] = {}
    for b in text_blocks:
        top[b.page_number] = min(top.get(b.page_number, b.bbox[1]), b.bbox[1])
        bottom[b.page_number] = max(bottom.get(b.page_number, b.bbox[3]), b.bbox[3])
    cands = []
    for b in text_blocks:
        text = (b.raw_text or "").strip()
        if b.block_type not in ("paragraph", "label") or not is_running_head_shape(text, b.bbox[3] - b.bbox[1]):
            continue
        edge = ("top" if abs(b.bbox[1] - top[b.page_number]) <= RUNNING_HEAD_EDGE_TOL
                else "bottom" if abs(b.bbox[3] - bottom[b.page_number]) <= RUNNING_HEAD_EDGE_TOL else None)
        if edge:
            sig = "<page_number>" if PAGE_NUMBER_RE.match(text) else margin_signature(text)
            cands.append((b, edge, sig))
    removed: dict[str, str] = {}
    for b, edge, sig in cands:
        same = {o.page_number for o, e, s in cands
                if e == edge and s == sig and abs(o.bbox[1] - b.bbox[1]) <= RUNNING_HEAD_Y_TOL}
        if len(same) >= threshold:
            kind = "page_header" if edge == "top" else "page_footer"
            removed[b.block_id] = (f"{kind}:page_number_outside_margin" if sig == "<page_number>"
                                   else f"{kind}:repeated_on_{len(same)}_pages_outside_margin")
    return removed


NOISE_TAGS = {"nav", "aside", "form"}
NOISE_ATTR_RE = re.compile(
    r"(^|[-_])(nav|navbar|menu|breadcrumbs?|cookie|cookies|consent|gdpr|banner|ad|ads|adslot|advert\w*"
    r"|sponsor\w*|promo\w*|newsletter|subscribe|subscription|signup|share|sharing|social|related"
    r"|recommend\w*|comments?|sidebar|popup|modal|widget|toolbar|pagination|shop"
    r"|bestsellers?|cta|slider|carousel)($|[-_])",
    re.I,
)
# 小寫（或數字）後面緊接大寫的位置，用來把駝峰命名拆成單字
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# 購物／導流用的短連結行（例如「Shop Cherry Talk」「Discover more red lipstick looks to try」）
CALL_TO_ACTION_RE = re.compile(r"^(shop|discover|explore|browse|buy)\b", re.I)
CALL_TO_ACTION_MAX_LEN = 100
# 「widget」是許多網站通用的版面容器名稱，有些網站連文章正文也包在 widget 裡
# （例如 Charlotte Tilbury：div.widget.widget--blog_content_block）。
# 因此 widget 底下要再看 BEM 修飾 class（widget--xxx）判斷容器用途。
WIDGET_CLASS_RE = re.compile(r"^widget($|--)", re.I)
# 修飾 class 去掉 'widget--' 前綴後，含這些詞（以 - 或 _ 分隔）就視為正文容器
CONTENT_WIDGET_RE = re.compile(
    r"(^|[-_])(content|blog|article|post|text|body|how_?to|steps?)($|[-_])",
    re.I,
)
BOILERPLATE_RE = re.compile(
    r"^(advertisement|sponsored( content)?|share( this)?( (article|post|story))?( on \w+)?|read more\W*"
    r"|related (articles|posts|stories)|you may also like|skip to (main )?content|back to top|print( this)?"
    r"|tweet|pin it|follow us.*|subscribe( now)?\W*|sign up for (our|the) newsletter.*"
    r"|(we|this (site|website)) uses? cookies.*|accept( all)?( cookies)?|reject( all)?|cookie (settings|preferences)"
    r"|all rights reserved.*|©.*all rights reserved.*)$",
    re.I,
)
# 這些訊號出現時，就算區塊位在雜訊區也不自動刪除，而是交給人工確認
DATA_SIGNAL_RE = re.compile(
    r"([LabCh]\s?\*|Δ\s?E|∆\s?E|\bCIE|\bD65\b|\bD50\b|\d+(\.\d+)?\s?(nm|K\b|°)|\bn\s?=\s?\d+|\bp\s?[<=]\s?0?\.\d+)"
)


def _parse_descriptor(desc: str) -> tuple[str, list[str]]:
    """'div#cookie-bar.cc.cc-banner' → ('div', ['cookie-bar', 'cc', 'cc-banner'])。"""
    tag = re.match(r"[A-Za-z0-9\[\]-]+", desc)
    tag_name = tag.group(0).lower() if tag else ""
    attrs = re.findall(r"[#.]([^#.]+)", desc[len(tag_name):])
    return tag_name, attrs


def split_camel_case(attr: str) -> str:
    """把駝峰命名的 class 拆成以 '-' 分隔的單字，讓 NOISE_ATTR_RE 能逐字比對。

    參數：
        attr：一個 id 或 class 名稱，例如 'RelatedContentCarousel_container__JGAc3'。

    回傳：
        在「小寫或數字 → 大寫」的交界插入 '-' 後的字串，例如
        'Related-Content-Carousel_container__JGAc3'。原本就用 - 或 _ 分隔的名稱不受影響。

    為什麼需要：
        NOISE_ATTR_RE 要求雜訊詞前後是字串邊界或 - ／ _，才不會把 'shadow' 誤認成 'ad'。
        駝峰命名（React／CSS Modules 常見）沒有分隔字元，'RelatedContentCarousel'
        因此認不出 related；拆開後就能用同一套規則判斷。
    """
    return CAMEL_BOUNDARY_RE.sub("-", attr)


def is_call_to_action(block: TextBlock) -> bool:
    """判斷區塊是不是購物／導流用的短連結行（不是知識內容）。

    參數：
        block：清洗中的文字區塊，用到 block_type 與 raw_text。

    回傳：
        True 表示符合以下全部條件，呼叫端可當成雜訊移除：
          1. 是 paragraph 或 list_item（標題不算：'Discover the Best Lipstick Colours…'
             是文章章節標題，要保留）。
          2. 壓空白後長度 ≤ CALL_TO_ACTION_MAX_LEN。
          3. 以 Shop／Discover／Explore／Browse／Buy 開頭。
          4. 結尾不是句號、驚嘆號、問號（完整句子通常是正文，交給人看，不自動刪）。

    注意：
        含色彩或研究數據的區塊，呼叫端（clean_block）仍會用 DATA_SIGNAL_RE 攔下改成人工確認。
    """
    if block.block_type not in ("paragraph", "list_item"):
        return False
    text = " ".join(block.raw_text.split())
    if not text or len(text) > CALL_TO_ACTION_MAX_LEN:
        return False
    if not CALL_TO_ACTION_RE.match(text):
        return False
    return text[-1] not in ".!?。！？"


# --- 網頁文章的署名／日期／閱讀時間列（metadata，不是知識內容） -------------------------
# 例：PR024「By Maybelline New York • 六月 03, 2026」、PR021「8 minute read」、
#     PR012「By」「Gillian Fuller」「March 21, 2025」（署名被拆成三個區塊）
_MONTH_EN = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
             r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_MONTH_ZH = r"(?:十[一二]?|[一二三四五六七八九])月"
_ORDINAL_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
DATE_LINE_RE = re.compile(
    r"^(?:(?:Updated|Published|Posted|Last updated|Modified)(?: on)?\s*:?\s*|(?:發布|發佈|更新)(?:日期|時間)?\s*[:：]?\s*)?"
    r"(?:" + _MONTH_EN + r"\s+" + _ORDINAL_DAY + r",?\s+\d{4}"          # March 21, 2025
    r"|" + _ORDINAL_DAY + r"\s+" + _MONTH_EN + r",?\s+\d{4}"            # 31st March 2026
    r"|" + _MONTH_ZH + r"\s*\d{1,2},?\s*\d{4}"                         # 六月 03, 2026
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"                        # 2026年6月3日
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})$",                               # 2026-06-03
    re.I,
)
READ_TIME_LINE_RE = re.compile(
    r"^(?:\d{1,3}\s*(?:min|mins|minute|minutes)\s+read|閱讀時間\s*[:：]?\s*(?:約\s*)?\d{1,3}\s*分鐘"
    r"|(?:約\s*)?\d{1,3}\s*分鐘(?:即可)?閱讀)$",
    re.I,
)
BYLINE_PREFIX_RE = re.compile(r"^(?:By|Written by|作者\s*[:：]?|文\s*[:：/／])\s*(.*)$", re.I)
BYLINE_SEPARATOR_RE = re.compile(r"\s*[•|·・]\s*")
BYLINE_MAX_LEN = 80
# 人名／品牌名：1–6 個字，每個字大寫開頭（允許 de、van、and、& 等連接詞）或全為中日文字
_NAME_CONNECTORS = {"de", "da", "van", "von", "der", "la", "le", "and", "&", "of"}
_NAME_WORD_RE = re.compile(r"^(?:[A-Z][\w’'.-]*|[぀-ヿ㐀-鿿]+)$")


def is_person_name_line(text: str) -> bool:
    """判斷一行文字是不是單純的人名或品牌名（署名用），而不是句子。

    參數：
        text：已壓過空白的一行文字，例如 'Gillian Fuller'、'Maybelline New York'。

    回傳：
        True 表示同時符合：1–6 個字、長度 ≤ 60、不含數字與句讀（, ! ? : ;，結尾不是句點）、
        每個字都是大寫開頭（連接詞 de／van／and／& 等例外）或全為中日文字。

    注意：
        只看字形，不查人名字典；因此只用在「緊接在 'By' 之後」或「'By' 開頭」的情境，
        不能單獨拿來判斷任意區塊。'By applying lip liner first, …' 因含逗號與小寫字而不算。
    """
    text = text.strip()
    # 縮寫姓名中的句點（如 'J. Smith'）允許；結尾句點表示是句子
    if not text or len(text) > 60 or re.search(r"[\d,!?:;。，！？：；]", text) or text.endswith("."):
        return False
    words = text.split()
    if not 1 <= len(words) <= 6:
        return False
    return all(w.lower() in _NAME_CONNECTORS or _NAME_WORD_RE.match(w) for w in words)


def classify_html_metadata(text: str) -> Optional[str]:
    """判斷一個網頁區塊的文字是不是文章的署名／日期／閱讀時間列。

    參數：
        text：區塊文字（raw 或 cleaned 皆可，函式內會壓空白）。

    回傳：
        - 'html_meta:byline'      'By Charlotte Tilbury'、'By Maybelline New York • 六月 03, 2026'、
                                  單獨一個 'By'（署名被拆成多個區塊時的第一塊）
        - 'html_meta:date'        'March 21, 2025'、'31st March 2026'、'2026-06-03'、'Updated: May 1, 2025'
        - 'html_meta:read_time'   '8 minute read'、'閱讀時間 5 分鐘'
        - None                    不是上述 metadata（包含任何完整句子）

    注意：
        - 只比對「整個區塊就是這一列」的情況，長度上限 BYLINE_MAX_LEN；
          正文中提到日期或 'by' 的句子不會被判為 metadata。
        - 'By' 開頭時，用 '•'、'|'、'·' 切開後：第一段必須是人名（見 `is_person_name_line`），
          其餘每段都必須是日期或閱讀時間，否則回傳 None。
        - 署名被拆開時，'By' 後面那個人名區塊由 `mark_html_metadata` 依前後文判斷。
    """
    text = " ".join((text or "").split())
    if not text or len(text) > BYLINE_MAX_LEN:
        return None
    if READ_TIME_LINE_RE.match(text):
        return "html_meta:read_time"
    if DATE_LINE_RE.match(text):
        return "html_meta:date"
    m = BYLINE_PREFIX_RE.match(text)
    if m:
        rest = m.group(1).strip()
        if not rest:
            return "html_meta:byline"
        parts = BYLINE_SEPARATOR_RE.split(rest)
        if is_person_name_line(parts[0]) and all(DATE_LINE_RE.match(p) or READ_TIME_LINE_RE.match(p)
                                                 for p in parts[1:]):
            return "html_meta:byline"
    return None


# --- 網頁的頁面框架文字（跳至內容、上一篇／下一篇、關閉鈕、圖片來源、作者欄標籤…） ----------
# 例：PR012「Go to content」「Photo courtesy of L’Oréal Paris」「Next Up: How To Apply Makeup Like a Pro」
#     「Your Privacy Choices Notice at Collection」、PR020「Jump to content」「Author Details」、
#     PR003「previous post」「next post」「x」（語言提示框的關閉鈕）
SKIP_LINK_RE = re.compile(r"^(?:go|jump|skip) to (?:the )?(?:main )?content$", re.I)
POST_NAV_RE = re.compile(r"^(?:previous|prev|next|older|newer) (?:post|article|story)$", re.I)
CLOSE_BUTTON_TEXTS = {"x", "×", "✕", "✖", "close"}
AUTHOR_LABEL_RE = re.compile(r"^(?:author details|about the author)$", re.I)
PRIVACY_LINK_LABELS = ("your privacy choices", "notice at collection",
                       "do not sell or share my personal information", "do not sell my personal information")
PHOTO_CREDIT_RE = re.compile(r"^(?:photo|image|images|photos|photography)\s+(?:courtesy of|credit\s*:|by)\s+(.+)$",
                             re.I)
NEXT_UP_RE = re.compile(r"^(?:next up|up next|read next)\s*:\s*(.+)$", re.I)
PAGE_CHROME_MAX_LEN = 120


def is_privacy_link_row(text: str) -> bool:
    """判斷一行文字是不是只由隱私權連結標籤組成（例：'Your Privacy Choices Notice at Collection'）。

    參數：
        text：已壓過空白的一行文字。

    回傳：
        True 表示整行可以被 PRIVACY_LINK_LABELS 中的標籤依序完整拼出（中間只允許空白），
        至少一個標籤；含任何其他字（例如 'Your privacy choices matter to us.'）就回傳 False。

    做法：
        從開頭反覆剝掉一個符合的標籤與後面的空白，剝完剩空字串才算。
    """
    rest = text.strip().lower()
    if not rest:
        return False
    while rest:
        for label in PRIVACY_LINK_LABELS:
            if rest.startswith(label) and (len(rest) == len(label) or rest[len(label)] == " "):
                rest = rest[len(label):].lstrip()
                break
        else:
            return False
    return True


def classify_html_page_chrome(text: str) -> Optional[str]:
    """判斷一個網頁區塊的文字是不是頁面框架（導覽、關閉鈕、圖片來源、作者欄標籤），而非知識內容。

    參數：
        text：區塊文字（raw 或 cleaned 皆可，函式內會壓空白）。

    回傳：
        - 'html_meta:skip_link'      'Go to content'、'Jump to content'、'Skip to main content'
        - 'html_meta:post_nav'       'previous post'、'next post'
        - 'html_meta:close_button'   單獨的 'x'、'×'、'Close'（彈出框的關閉鈕）
        - 'html_meta:author_label'   'Author Details'、'About the author'（作者欄的標籤；作者姓名與頭銜是 heading，不動）
        - 'html_meta:privacy_links'  'Your Privacy Choices Notice at Collection'（見 `is_privacy_link_row`）
        - 'html_meta:photo_credit'   'Photo courtesy of L’Oréal Paris'（來源必須是人名／品牌名，見 `is_person_name_line`）
        - 'html_meta:next_up'        'Next Up: How To Apply Makeup Like a Pro'（連到下一篇文章的標題）
        - None                       其他文字

    注意：
        - 一律「整段完全符合」才算；長度上限 PAGE_CHROME_MAX_LEN。
          'Photo courtesy of the author, who tested 12 shades.' 這類句子不會被判定。
        - 'Next Up:' 後面的標題若以句號結尾（像完整句子）不算，交給人看。
        - 含色彩數據的區塊由呼叫端 `mark_html_metadata` 以 DATA_SIGNAL_RE 再擋一次。
    """
    text = " ".join((text or "").split())
    if not text or len(text) > PAGE_CHROME_MAX_LEN:
        return None
    if SKIP_LINK_RE.match(text):
        return "html_meta:skip_link"
    if POST_NAV_RE.match(text):
        return "html_meta:post_nav"
    if text.lower() in CLOSE_BUTTON_TEXTS:
        return "html_meta:close_button"
    if AUTHOR_LABEL_RE.match(text):
        return "html_meta:author_label"
    if is_privacy_link_row(text):
        return "html_meta:privacy_links"
    m = PHOTO_CREDIT_RE.match(text)
    if m and is_person_name_line(m.group(1)):
        return "html_meta:photo_credit"
    m = NEXT_UP_RE.match(text)
    if m and not m.group(1).rstrip().endswith((".", "。")):
        return "html_meta:next_up"
    return None


METADATA_BLOCK_TYPES = {"paragraph", "list_item"}


def mark_html_metadata(blocks: list[TextBlock]) -> int:
    """網頁：把文章的署名／日期／閱讀時間列標為移除（removal_reason 以 'html_meta:' 開頭）。

    參數：
        blocks：clean_block 處理過的區塊（會直接修改 is_removed、removal_reason、cleaning_ops）。

    回傳：
        這次新標為移除的區塊數。

    規則：
        1. 只看 paragraph／list_item、且尚未被移除的區塊；標題不動。
        2. 區塊文字整段符合 `classify_html_metadata`（署名／日期／閱讀時間）
           或 `classify_html_page_chrome`（跳至內容、上一篇／下一篇、關閉鈕、圖片來源…）→ 移除。
        3. 單獨的 'By' 區塊之後，下一個未移除的 paragraph／list_item 若是人名
           （`is_person_name_line`，例如 'Gillian Fuller'）→ 以 'html_meta:byline_author' 移除。
        4. 含色彩或研究數據（DATA_SIGNAL_RE）的區塊一律不動，與 HTML 雜訊同一道保險。

    注意：
        raw_text 原樣保留，只是不進 RAG；清洗報告的「移除原因」會列出 html_meta:*。
    """
    n = 0
    after_bare_by = False
    for b in sorted(blocks, key=lambda x: x.order):
        if b.is_removed or b.block_type not in METADATA_BLOCK_TYPES:
            continue
        text = " ".join((b.cleaned_text or "").split())
        reason = classify_html_metadata(text) or classify_html_page_chrome(text)
        if reason is None and after_bare_by and is_person_name_line(text):
            reason = "html_meta:byline_author"
        after_bare_by = reason == "html_meta:byline" and BYLINE_PREFIX_RE.match(text).group(1).strip() == ""
        if reason is None or DATA_SIGNAL_RE.search(b.raw_text):
            continue
        b.is_removed, b.removal_reason = True, reason
        b.cleaning_ops.append("removed_block")
        n += 1
    return n


def is_content_widget(attrs: list[str]) -> bool:
    """判斷一個 DOM 元素是不是「裝正文的 widget 容器」。

    參數：
        attrs：同一個元素的 id／class 清單（`_parse_descriptor` 的第二個回傳值），
               例如 ['widget', 'widget--blog_content_block', 'ctContainer']。

    回傳：
        True  → 這個 widget 的修飾 class 指出它是正文（blog、content、how_to_steps…），
                呼叫端應略過 'widget' 這個名稱本身，不因它判為雜訊。
        False → 不是 widget、沒有修飾 class，或修飾 class 本身就是雜訊／用途不明，
                維持原本「widget 視為雜訊」的保守判斷。

    判斷順序（雜訊優先）：
        1. 取出所有 'widget--xxx' 修飾 class，去掉前綴得到 'xxx'。
        2. 任一個 'xxx' 符合 NOISE_ATTR_RE（例如 'related_posts' 含 related）→ False。
           這一步避免「related_posts」因為含 post 被誤當成正文。
        3. 任一個 'xxx' 符合 CONTENT_WIDGET_RE → True。
        4. 其他情況（例如 'product_listing'、'is-themed'）→ False。
    """
    modifiers = [a[len("widget--"):] for a in attrs if a.lower().startswith("widget--")]
    if any(NOISE_ATTR_RE.search(m) for m in modifiers):
        return False
    return any(CONTENT_WIDGET_RE.search(m) for m in modifiers)


def classify_html_noise(block: TextBlock) -> Optional[str]:
    """依 DOM 祖先（tag、id、class）與文字樣板判斷 HTML 雜訊；回傳原因或 None。

    - nav / aside / form 底下的內容 → 雜訊。
    - header / footer：只有「不在 article/main 裡」才算雜訊（文章自己的 header 常放標題與作者）。
    - id / class 含 cookie、ad、share、newsletter、related、comment、shop… → 雜訊。
      駝峰命名先拆字再比對（見 `split_camel_case`），例如 'RelatedContentCarousel'。
    - 自訂元素（標籤名含 '-'）的名稱含上述雜訊詞 → 雜訊，例如 <article-share-item>。
    - class 含 widget：修飾 class 指出是正文（見 `is_content_widget`）時不算雜訊，
      同一元素的其他 class 仍照常檢查；其餘 widget（如商品輪播）仍算雜訊。
    - 文字本身是樣板（'Advertisement'、'Read more'、'Accept cookies'…）→ 雜訊。
    - 購物／導流短連結行（'Shop Cherry Talk'，見 `is_call_to_action`）→ 雜訊。
    """
    inside_content = False
    for desc in block.html_context or []:
        tag, attrs = _parse_descriptor(desc)
        if tag in ("article", "main"):
            inside_content = True
        if tag in NOISE_TAGS:
            return f"html_noise:tag={tag}"
        if tag in ("header", "footer") and not inside_content:
            return f"html_noise:site_{tag}"
        if "-" in tag and NOISE_ATTR_RE.search(tag):
            # 自訂元素（Vue／Web Components）的標籤名稱本身就說明用途，
            # 例如 article-share-item、card-entry-video-popup，比照 class 判斷
            return f"html_noise:custom_tag={tag}"
        content_widget = is_content_widget(attrs)
        for a in attrs:
            if content_widget and WIDGET_CLASS_RE.match(a):
                continue  # 正文型 widget：略過 widget 名稱本身，不因它判為雜訊
            if NOISE_ATTR_RE.search(split_camel_case(a)):
                return f"html_noise:attr={a}"
    text = " ".join(block.raw_text.split())
    if len(text) <= 120 and BOILERPLATE_RE.match(text):
        return "html_noise:boilerplate_text"
    if is_call_to_action(block):
        return "html_noise:call_to_action"
    return None


# ---------------------------------------------------------------------------
# 表格、跨頁接續
# ---------------------------------------------------------------------------

def clean_cell(cell: Optional[str]) -> Optional[str]:
    """單一儲存格：Unicode 正規化 + 接回換行 + 壓空白；None 保持 None。"""
    if cell is None:
        return None
    t, _ = normalize_unicode(cell)
    t, _ = fix_pdf_line_breaks(t)
    t, _ = collapse_whitespace(t)
    return t


def clean_table(rows: list[list[Optional[str]]]) -> list[list[Optional[str]]]:
    """逐格清洗，不合併、不刪除任何列或欄，保持表格形狀。"""
    return [[clean_cell(c) for c in row] for row in rows]


def serialize_table(rows: list[list[Optional[str]]]) -> str:
    return "\n".join(" | ".join("" if c is None else c for c in row) for row in rows)


TERMINAL_RE = re.compile(r"[.!?:;。！？」』”\"')\]]\s*$")
CONTINUABLE = {"paragraph", "list_item"}


# 這些區塊常插在一段話中間（圖、表、圖說、頁首頁尾），判斷段落接續時略過，不中斷
CONTINUATION_SKIP = {"page_header", "page_footer", "figure_caption", "table_caption", "figure_text",
                     "image_region", "chart_region", "table", "formula", "equation_label"}
# 01_extract 標記的未解析表格殘片（「108 37.86 35.86」「0.6」）與數字為主的表格本體也常插在段落中間，同樣略過；
# 否則表格下方的續段會接到表格上：05 匯出時鏈頭被排除，續段只剩半句（AR012 p.11），
# 或表格數值被當成正文接進段落（AR012 p.4）。字串必須與 01_extract 一致，見 tests/test_continuation.py
TABLE_NOTE_REASON = "clean: 表格註記（Note. …），屬於表格的一部分、不是正文段落，請對照 PDF"
TABLE_FRAGMENT_REASONS = (
    "同頁有找不到本體的表格標題，此短段落疑似未解析表格的殘片，請對照 PDF",
    "多行且以數字為主，疑似未解析的表格內容（欄名與數值的對應已遺失），請對照 PDF",
    "同頁有疑似未解析的表格內容，此短段落疑似表頭或表格殘片，請對照 PDF",
    TABLE_NOTE_REASON,
    # 01_extract 的 table_text：文字呈欄位排列但沒解析成表格（AR004 p.7「vs L* vs a* 0.794 …」）
    "文字呈欄位排列但未能解析成表格，欄列對應未知，請對照 PDF 人工確認",
)
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
CONTINUATION_START = ",;:)）]，、；："

# 英文句子不會停在這些字上（介系詞、冠詞、連接詞、be 動詞）：前一段停在這裡，下一段即使大寫或數字開頭也是同一句
CONNECTOR_WORDS = {
    "a", "an", "the", "and", "or", "nor", "but", "of", "to", "for", "with", "in", "on", "at", "by", "from",
    "into", "than", "as", "between", "below", "above", "under", "over", "about", "within", "without",
    "is", "are", "was", "were", "be", "been", "that", "which", "whose", "whereas", "while", "=", "＝",
}
LAST_WORD_RE = re.compile(r"(?:^|\s|\))([A-Za-z]+|[=＝])\W*$")   # 獨立的英文字或等號（「λa」的 a 不算；「480)＝」的＝算）
# 圖表標題的寫法（「TABLE 3 Pearson’s …」「Figure 2: …」）；正文句首引用「Table 4.1, the …」後面是逗號，不在此列
CAPTION_LIKE_START_RE = re.compile(
    r"^(TABLE|FIGURE)\b|^(Table|Fig\.?|Figure)\s*[A-Z]?\d+(?:\.\d+)*(?:\s*[|:  ]|\.\s)")
STRONG_MIN_WORDS = 3            # 續段至少要有這麼多字才像句子（排除被當成段落的頁碼「13」）
# 數字開頭的續段（「4.03 for Hispanic …」）；「1. 」「2) 」這類列表編號不算
NUMBER_START_RE = re.compile(r"^[−\-+]?\d+(?:[.,]\d+)*(?![.)]\s)")
CAPITALIZED_WORD_RE = re.compile(r"^[A-Z][a-z]+\b")
PROPER_NOUN_SPLIT_MIN_LEN = 80   # 「…African」+「American …」：前一段至少這麼長才算句中斷開（排除短標籤）


def _is_strong_continuation(prev_text: str, cur_text: str) -> bool:
    """數字或大寫開頭的續段：只在前一段的結尾本身證明句子沒講完時才接。

    參數：
        prev_text：前一段（閱讀順序上的前一個可接續區塊）的清洗後文字。
        cur_text：這一段的清洗後文字。

    回傳：
        True 表示這一段是前一段同一句話的後半。

    規則（前一段都不能以句尾標點結束；列表編號「1. 」「2) 」不算數字開頭，見 NUMBER_START_RE）：
        1. 前一段以逗號結尾，或最後一個字是介系詞／冠詞／連接詞／be 動詞（CONNECTOR_WORDS）
           → 下一段以數字或大寫字開頭都算。例：AR015「… = 4.99 for African American women,」→「4.03 for …」。
        2. 專有名詞被換欄拆開：前一段夠長、最後一個字首字大寫，下一段也以首字大寫的英文字開頭。
           例：AR015「… French, Hispanic American, African」→「American and Asian Chinese.」

    注意：
        呼叫端還要確認兩段之間沒有略過 figure_text（句子真正的後半可能在那裡），見 link_cross_page_continuations。
    """
    prev_text, cur_text = (prev_text or "").rstrip(), (cur_text or "").lstrip()
    if (not prev_text or not cur_text or TERMINAL_RE.search(prev_text)
            or CAPTION_LIKE_START_RE.match(cur_text) or len(cur_text.split()) < STRONG_MIN_WORDS):
        return False
    starts_number = bool(NUMBER_START_RE.match(cur_text))
    starts_upper = cur_text[0].isupper()
    if not (starts_number or starts_upper):
        return False
    m = LAST_WORD_RE.search(prev_text)
    last_word = m.group(1) if m else ""
    if prev_text.endswith(",") or last_word.lower() in CONNECTOR_WORDS:
        return True
    return (starts_upper and len(prev_text) >= PROPER_NOUN_SPLIT_MIN_LEN and prev_text[-1].isalpha()
            and bool(CAPITALIZED_WORD_RE.match(last_word)) and bool(CAPITALIZED_WORD_RE.match(cur_text)))


def _is_continuation(prev_text: str, cur_text: str) -> bool:
    """上一段沒有句尾標點，且這一段像是句子的後半：小寫或逗號等開頭；中文則前後都是漢字。

    原有的規則：小寫開頭本身就是強烈的續句訊號。數字或大寫開頭的續段另由 _is_strong_continuation 判斷。
    """
    prev_text, cur_text = (prev_text or "").rstrip(), (cur_text or "").lstrip()
    if not prev_text or not cur_text or TERMINAL_RE.search(prev_text):
        return False
    first, last = cur_text[0], prev_text[-1]
    # 中文沒有大小寫：前後都是漢字、且前一段夠長（>= 30 字，封面那種短行不算）才視為句子被切斷
    cjk_cut = bool(HAN_RE.match(last) and HAN_RE.match(first)) and len(prev_text) >= 30
    return first.islower() or prev_text.endswith("-") or first in CONTINUATION_START or cjk_cut


COLUMN_END_TOLERANCE = 6.0      # pt：區塊下緣離該欄最低正文下緣這麼近，就算是該欄最後一段
BODY_TYPES_FOR_COLUMN_END = {"paragraph", "list_item", "heading", "label"}


def _column_bottoms(blocks: list[TextBlock]) -> dict[int, list[tuple[float, float, float]]]:
    """每頁正文區塊的 (x0, x1, y1)，供判斷「這一段是不是所在欄的最後一段」。"""
    out: dict[int, list[tuple[float, float, float]]] = {}
    for b in blocks:
        if b.is_removed or b.block_type not in BODY_TYPES_FOR_COLUMN_END or not b.bbox or b.page_number is None:
            continue
        out.setdefault(b.page_number, []).append((b.bbox[0], b.bbox[2], b.bbox[3]))
    return out


def _ends_its_column(b: TextBlock, bottoms: dict[int, list[tuple[float, float, float]]]) -> bool:
    """區塊是否為所在欄（與它水平重疊過半的正文區塊）在該頁的最後一段。

    真正的換欄／換頁斷句，前半段一定落在欄底；中間插入圖表的情況，前半段也是插圖前該欄的最後一段，
    但那種情形下一段的開頭通常是小寫，已由原規則處理。沒有座標（HTML、合成資料）時回傳 False。
    """
    if not b.bbox or b.page_number is None:
        return False
    x0, x1, y1 = b.bbox[0], b.bbox[2], b.bbox[3]
    same_col = [oy1 for ox0, ox1, oy1 in bottoms.get(b.page_number, [])
                if min(x1, ox1) - max(x0, ox0) > 0.5 * min(x1 - x0, ox1 - ox0)]
    return bool(same_col) and y1 >= max(same_col) - COLUMN_END_TOLERANCE


def _caption_tops(blocks: list[TextBlock]) -> dict[int, list[float]]:
    """每頁圖表標題的上緣 y0；並排式圖說（「FIGURE 6」標籤＋右側說明文字）的說明文字與標籤同高。"""
    out: dict[int, list[float]] = {}
    for b in blocks:
        if b.block_type in ("figure_caption", "table_caption") and b.bbox and b.page_number is not None:
            out.setdefault(b.page_number, []).append(b.bbox[1])
    return out


def _strong_link_allowed(prev: TextBlock, cur: TextBlock, bottoms: dict, caption_tops: dict,
                         skipped_figure_text: bool) -> bool:
    """數字／大寫開頭續段的版面與狀態前提（文字條件見 _is_strong_continuation）。

    1. 前一段是所在欄的最後一段（確實發生換欄、換頁或插圖）。
    2. 不在第 1 頁：期刊首頁常把作者單位、編輯、授權框排在摘要旁，結尾常是國名或「EDITED BY」，會被誤接。
    3. 前後兩段都不是待確認區塊：接上待確認的段落，匯出時會變成懸空的半句。
    4. 中間沒有略過 figure_text：句子真正的後半可能在圖表文字裡。
    5. 這一段的第一行沒有和圖表標題並排（AR001「FIGURE 6」右邊的說明文字被擷取成段落，不是正文續句）。
    """
    beside_caption = bool(cur.bbox) and any(abs(y - cur.bbox[1]) <= 3 for y in caption_tops.get(cur.page_number, []))
    return (not skipped_figure_text and not beside_caption
            and prev.page_number not in (None, 1) and cur.page_number not in (None, 1)
            and not prev.needs_review and not cur.needs_review and _ends_its_column(prev, bottoms))


def link_cross_page_continuations(blocks: list[TextBlock]) -> int:
    """PDF：被分頁、換欄或中間插入的圖表切斷的段落 → 設定 continues_from_block_id。

    依閱讀順序走過區塊：遇到標題重新開始；頁首頁尾、圖、表、圖說、未解析表格殘片略過（它們常插在一段話中間）。
    出版行政資訊（授權聲明、通訊作者、基金註腳，見 eligibility.is_boilerplate）也略過：它們常排在首頁欄底，
    把跨頁的正文句子切開（AR012 p.1→2「… due to its material」→「characteristics, …」）。
    小寫／標點開頭用 _is_continuation（原規則）；數字或大寫開頭用 _is_strong_continuation，
    另須通過 _strong_link_allowed 的版面與狀態前提（欄底、非首頁、非待確認、未略過 figure_text）。
    只設定連結，不合併文字（每一段的 page_number 保持正確）；05 匯出時再依連結合併成一筆。
    回傳連結數。
    """
    bottoms, caption_tops = _column_bottoms(blocks), _caption_tops(blocks)
    linked = 0
    prev: Optional[TextBlock] = None
    skipped_figure_text = False      # prev 之後是否略過了 figure_text
    for b in sorted(blocks, key=lambda x: x.order):
        if (b.is_removed or b.block_type in CONTINUATION_SKIP
                or any(r in b.review_reasons for r in TABLE_FRAGMENT_REASONS)
                or (b.block_type in CONTINUABLE and is_boilerplate(b))):
            if not b.is_removed and b.block_type == "figure_text":
                skipped_figure_text = True
            continue
        if b.block_type not in CONTINUABLE:
            prev = None          # 標題、目錄等：段落不會跨過它們
            continue
        if prev is not None and b.block_type == "paragraph" and b.continues_from_block_id is None:
            weak = _is_continuation(prev.cleaned_text, b.cleaned_text)
            strong = (not weak and _strong_link_allowed(prev, b, bottoms, caption_tops, skipped_figure_text)
                      and _is_strong_continuation(prev.cleaned_text, b.cleaned_text))
            if weak or strong:
                b.continues_from_block_id = prev.block_id
                b.cleaning_ops.append("linked_cross_page_continuation" if b.page_number != prev.page_number
                                      else "linked_column_or_figure_break")
                if strong:
                    b.cleaning_ops.append("linked_by_sentence_end_evidence")
                linked += 1
        prev = b
        skipped_figure_text = False
    return linked


# ---------------------------------------------------------------------------
# PDF：併進正文的圖說、只剩半句的殘片、詞彙表多條併段（TX001 NCERT 職訓教材）
# 只標成待人工確認（needs_review，原因以 'clean:' 開頭），不刪除、不改寫正文
# ---------------------------------------------------------------------------

# 圖說行：'Figure 1.23: Interpretation of hue'、'Fig. 2: …'（冒號是圖說與內文引用 '(Figure 1.23)' 的差別）
FIGURE_CAPTION_START_RE = re.compile(r"^Fig(?:ure)?\.?\s*\d+(?:\.\d+)*\s*:\s*\S")
EMBEDDED_FIGURE_CAPTION_RE = re.compile(r"(?<=\S)\s+(Fig(?:ure)?\.?\s*\d+(?:\.\d+)*\s*:\s*\S.{0,50})")


def mark_figure_captions(blocks: list[TextBlock]) -> list[ExtractionIssue]:
    """處理被擷取成正文的圖說：開頭就是圖說 → 改成 figure_caption；夾在正文中 → 待人工確認。

    參數：
        blocks：clean_block 處理過的 PDF 區塊（會直接修改 block_type、needs_review、review_reasons）。

    回傳：
        新產生的 ExtractionIssue（只有「夾在正文中」才產生；改型別只記在 cleaning_ops）。

    規則：
        1. paragraph／list_item 開頭就是 'Figure 1.2: …' → block_type 改為 figure_caption，
           走既有的圖說流程（05 匯出時標 data_status，提醒圖片內容沒有擷取）。
        2. paragraph／list_item 中間出現 'Figure 1.35: Party wear bindi'（例：TX001 p.38
           '4. Flower style bindi: This is a simple bindi design Figure 1.35: Party wear bindi'）
           → 版面把旁邊的圖說併進段落，無法自動判斷切點，標為待人工確認。

    注意：
        內文引用 '(Figure 1.35)' 沒有冒號，不會被當成圖說。必須在 link_cross_page_continuations 之前執行，
        改成 figure_caption 的區塊才會在段落接續時被略過。
    """
    issues: list[ExtractionIssue] = []
    for b in blocks:
        if b.is_removed or b.block_type not in CONTINUABLE:
            continue
        text = (b.cleaned_text or "").strip()
        if FIGURE_CAPTION_START_RE.match(text):
            b.block_type = "figure_caption"
            b.cleaning_ops.append("retyped_as_figure_caption")
            continue
        m = EMBEDDED_FIGURE_CAPTION_RE.search(text)
        if m:
            msg = f"clean: 正文中夾有圖說「{m.group(1).strip()}」，疑似版面把圖說併入段落，請拆分後再核准"
            b.needs_review = True
            b.review_reasons.append(msg)
            b.cleaning_ops.append("flagged_embedded_figure_caption")
            issues.append(ExtractionIssue(issue_type="embedded_figure_caption", severity="warning", stage="cleaned",
                                          page_number=b.page_number, block_id=b.block_id, message=msg))
    return issues


# 句號式標題：「Figure 3.　Perceptual …」「Table 3. Relationships …」（AR003、AR004）；句號後接空白（含全形空白）
PERIOD_CAPTION_RE = re.compile(r"^(Fig(?:ure)?|Table)\.?\s*[A-Z]?\d+(?:\.\d+)*\.[\s　]+\S", re.I)
# 只有標籤的並排式標題：「FIGURE 2」「TABLE 1」單獨一塊，說明文字在右側同一高度（AR001、AR002）
CAPTION_LABEL_RE = re.compile(r"^(Fig(?:ure)?|Table)\.?\s*[A-Z]?\d+(?:\.\d+)*$", re.I)
TABLE_NOTE_RE = re.compile(r"^Notes?\s*[.:]\s+\S")
CAPTION_MAX_CHARS = 600          # 超過這麼長的段落不當標題（避免整段正文被改型別）
CAPTION_TAIL_MAX_CHARS = 200     # 標題／註記的續行（小寫開頭的短行）長度上限
CAPTION_TAIL_MAX_GAP = 4.0       # pt：續行與標題的垂直距離上限
CAPTION_TAIL_OVERLAP = 3.0       # pt：續行與上一行的外框可重疊（行距緊時，AR004 表格註記第二行）
SIDE_CAPTION_Y_TOL = 3.0         # pt：並排說明文字與標籤的上緣高度差


def _caption_kind(text: str) -> str:
    return "table_caption" if text.lower().startswith("table") else "figure_caption"


def _is_tail_of(prev: TextBlock, b: TextBlock) -> bool:
    """b 是否為 prev（標題或註記）的續行：同頁、緊貼在下方、小寫開頭、夠短。"""
    text = (b.cleaned_text or "").strip()
    return (b.block_type == "paragraph" and prev.page_number == b.page_number and bool(prev.bbox) and bool(b.bbox)
            and -CAPTION_TAIL_OVERLAP <= b.bbox[1] - prev.bbox[3] <= CAPTION_TAIL_MAX_GAP and len(text) <= CAPTION_TAIL_MAX_CHARS
            and bool(re.match(r"^[a-z(]", text)))


def mark_caption_variants(blocks: list[TextBlock]) -> list[ExtractionIssue]:
    """被擷取成正文的圖表標題變體與表格註記：改型別或標記，讓段落接續略過它們。

    參數：
        blocks：clean_block 處理過的 PDF 區塊（會直接修改 block_type、needs_review、review_reasons、cleaning_ops）。

    回傳：
        ExtractionIssue 清單（只有表格註記會產生，改型別只記在 cleaning_ops）。

    規則（只看 paragraph）：
        1. 句號式標題「Figure 3.　…」「Table 3. …」（PERIOD_CAPTION_RE，<= 600 字）→ 改成 figure_caption／table_caption。
        2. 只有標籤「FIGURE 2」的段落 → 改成標題；說明文字也改成標題：與它上緣同高（<= 3pt）、在它右側
           （AR001 並排式），或在它正下方緊貼、左緣對齊（AR002「FIGURE 2」⏎「Display of the lipstick colors …」）。
        3. 標題正下方緊貼、小寫開頭、<= 200 字的短行（「and F-values in parenthesis.」）→ 同型別的標題續行。
        4. 「Note. …」表格註記與其續行 → 標為 TABLE_NOTE_REASON（待確認；段落接續會略過）。

    注意：
        這些區塊原本插在跨頁、跨欄的句子中間（AR004 p.6→7），使正文接到圖說或表格註記上、或接不起來。
        只改型別或加標記，不改寫文字；圖說之後照既有流程匯出（data_status 說明圖片內容未擷取）。
        必須在 link_cross_page_continuations 之前執行。
    """
    issues: list[ExtractionIssue] = []
    ordered = [b for b in sorted(blocks, key=lambda x: x.order) if not b.is_removed]
    labels = []
    for b in ordered:
        text = (b.cleaned_text or "").strip()
        if b.block_type != "paragraph" or not text:
            continue
        if CAPTION_LABEL_RE.match(text) or (PERIOD_CAPTION_RE.match(text) and len(text) <= CAPTION_MAX_CHARS):
            b.block_type = _caption_kind(text)
            b.cleaning_ops.append("retyped_as_caption")
            if CAPTION_LABEL_RE.match(text):
                labels.append(b)
    for lab in labels:                                         # 標籤的說明文字：右側同高，或正下方緊貼且左緣對齊
        for b in ordered:
            if b.block_type != "paragraph" or b.page_number != lab.page_number or not b.bbox or not lab.bbox:
                continue
            beside = abs(b.bbox[1] - lab.bbox[1]) <= SIDE_CAPTION_Y_TOL and b.bbox[0] >= lab.bbox[2] - 1
            below = (abs(b.bbox[0] - lab.bbox[0]) <= SIDE_CAPTION_Y_TOL           # AR002「FIGURE 2」⏎「Display of …」
                     and -CAPTION_TAIL_OVERLAP <= b.bbox[1] - lab.bbox[3] <= CAPTION_TAIL_MAX_GAP)
            if beside or below:
                b.block_type = lab.block_type
                b.cleaning_ops.append("retyped_as_side_caption_text" if beside else "retyped_as_caption_text_below_label")
    prev: Optional[TextBlock] = None
    for b in ordered:                                          # 續行與表格註記
        text = (b.cleaned_text or "").strip()
        if prev is not None and prev.block_type in ("figure_caption", "table_caption") and _is_tail_of(prev, b):
            b.block_type = prev.block_type
            b.cleaning_ops.append("retyped_as_caption_continuation")
            # 接回標題本身：05 會把同型別的標題續行併成同一筆（否則標題停在半句，AR004 Table 3「… correlation」）
            b.continues_from_block_id = prev.block_id
            if prev.needs_review:
                # 標題本身待確認（例如找不到表身）時，續行單獨匯出會變成懸空的半句，一併待確認
                b.needs_review = True
                b.review_reasons.append(f"clean: 接續的標題 {prev.block_id} 待人工確認，單獨匯出會只剩半句，請一併確認")
        elif b.block_type == "paragraph" and (TABLE_NOTE_RE.match(text) or (
                prev is not None and TABLE_NOTE_REASON in prev.review_reasons and _is_tail_of(prev, b))):
            b.needs_review = True
            b.review_reasons.append(TABLE_NOTE_REASON)
            b.cleaning_ops.append("flagged_table_note")
            issues.append(ExtractionIssue(issue_type="table_note", severity="info", stage="cleaned",
                                          page_number=b.page_number, block_id=b.block_id, message=TABLE_NOTE_REASON))
        if b.block_type not in ("page_header", "page_footer"):
            prev = b
    issues += _flag_numeric_near_unparsed_tables(ordered)
    return issues


def _flag_numeric_near_unparsed_tables(ordered: list[TextBlock]) -> list[ExtractionIssue]:
    """清洗時才被認出的表格標題（句號式「Table 4. …」），同頁若沒有已解析的表格，把表身殘留標為表格殘片：

    1. 標題下方到表格註記（Note. …）之間的段落：表頭文字（「vs Perceptual redness …」）也在這裡，
       只靠數字比例抓不到，正文會被接到表頭上（AR004 p.6→7）。
    2. 同頁以數字為主的短段落（「－0.662 (14.07 **) －0.803 (32.62 **) …」）。
    01 擷取時這頁還沒有表格標題，所以沒觸發殘片標記，這些內容會脫離欄名被當成正文、還會被接進段落。
    """
    pages_with_tables = {b.page_number for b in ordered if b.block_type == "table"}
    captions = [b for b in ordered if b.block_type == "table_caption" and b.bbox
                and {"retyped_as_caption", "retyped_as_caption_continuation"} & set(b.cleaning_ops)
                and b.page_number not in pages_with_tables]
    pages = {b.page_number for b in captions}
    regions = []
    for cap in captions:
        note = next((n for n in ordered if n.page_number == cap.page_number and n.bbox and n.bbox[1] >= cap.bbox[3] - 1
                     and TABLE_NOTE_REASON in n.review_reasons), None)
        if note is not None:
            regions.append((cap.page_number, cap.bbox[3] - 1, note.bbox[1] + 1))
    issues: list[ExtractionIssue] = []
    for b in ordered:
        text = (b.cleaned_text or "").strip()
        tokens = text.split()
        in_region = bool(b.bbox) and any(p == b.page_number and b.bbox[1] >= y0 and b.bbox[3] <= y1
                                         for p, y0, y1 in regions)
        numeric = bool(tokens) and len(text) <= 200 and sum(
            bool(re.match(r"^[(（]?[<>≤≥~±+\-−－]?\d+(?:[.,]\d+)*%?[)）*]*$", t)) for t in tokens) >= 0.5 * len(tokens)
        if b.page_number in pages and b.block_type == "paragraph" and not b.needs_review and (in_region or numeric):
            b.needs_review = True
            b.review_reasons.append(TABLE_FRAGMENT_REASONS[0])
            b.cleaning_ops.append("flagged_numeric_table_fragment")
            issues.append(ExtractionIssue(issue_type="table_fragment", severity="warning", stage="cleaned",
                                          page_number=b.page_number, block_id=b.block_id, message=TABLE_FRAGMENT_REASONS[0]))
    return issues


# 找鄰居時略過的區塊：它們不會是同一句話的另一半
FRAGMENT_NEIGHBOR_SKIP = {"page_header", "page_footer", "image_region", "chart_region", "table", "formula",
                          "equation_label", "figure_caption", "table_caption"}


# 句子沒講完：結尾是英文字母、逗號、連字號或左括號（結尾是數字的多半是表格列，例 AR001 p.6 'b 2.856'，不算）
FRAGMENT_OPEN_END_RE = re.compile(r"[A-Za-z,(\-–]$")
# 後半句：以小寫英文字開頭（'outfits.'、'a pin.'）
FRAGMENT_LOWER_START_RE = re.compile(r"^\s*[a-z][a-z’'-]*\b")


def _cannot_join(b: TextBlock) -> bool:
    """判斷區塊是否「無法匯出也無法接成段落」。

    參數：
        b：清洗中的區塊。

    回傳：
        True 表示待人工確認、是 figure_text，或解析不完整（parse_status 不是 ok）。

    注意：
        figure_text 在 link_cross_page_continuations 中會被略過，所以句子的後半若落在 figure_text，
        前半永遠接不上，只能交給人工確認。
    """
    return b.needs_review or b.block_type == "figure_text" or b.parse_status != "ok"


def flag_truncated_fragments(blocks: list[TextBlock]) -> list[ExtractionIssue]:
    """PDF：句子被切成兩半、另一半落在待確認區塊裡 → 這一半也標為待人工確認，不讓半句話進 RAG。

    參數：
        blocks：已完成 link_cross_page_continuations 的 PDF 區塊（會直接修改 needs_review、review_reasons）。

    回傳：
        新產生的 ExtractionIssue 清單。

    規則（只看 paragraph／list_item，依閱讀順序找前後一個有文字的區塊，略過圖、表、圖說、頁首頁尾）：
        1. 只剩前半：這段結尾是字母／逗號等（FRAGMENT_OPEN_END_RE，句子沒講完），下一段以小寫英文字開頭
           （FRAGMENT_LOWER_START_RE）、不是接續這段，且下一段無法匯出
           （待確認或 figure_text）。例：TX001 p.40 '• Evaluate the best bindi styles for different
           face shapes and' 後半 'outfits.' 在 figure_text 裡。
        2. 只剩後半：這段以小寫英文字開頭、沒有接續任何段落，前一段句子沒講完且無法匯出。
           例：TX001 p.38 'oils. Use a cotton pad to wipe the forehead gently if needed.'
        3. 連鎖：接續（continues_from_block_id）在被標記區塊後面的段落也一併標記，
           否則 05 匯出時它會變成「前一段沒有匯出」的半句。

    注意：
        不刪除、不改寫文字；人工確認後可直接核准或合併。英文以小寫開頭判斷，中文沒有大小寫，不在此規則內。
        規則 1 的例外：段落已經有續段接在後面，而且下一個區塊是表格殘片／表格文字／表格註記（段落接續本來就會略過），
        後半句其實已經接上（AR004 p.6「… face look」→ 表格文字 → p.7「significantly lighter …」）。
        下一個區塊是 figure_text 時不例外：後半句可能就在圖表文字裡，跳過它接到的續段可能是錯的。
    """
    seq = [b for b in sorted(blocks, key=lambda x: x.order)
           if not b.is_removed and b.block_type not in FRAGMENT_NEIGHBOR_SKIP and (b.cleaned_text or "").strip()]
    has_continuation = {b.continues_from_block_id for b in blocks if b.continues_from_block_id and not b.is_removed}
    flagged: dict[str, str] = {}
    for i, b in enumerate(seq):
        if b.block_type not in CONTINUABLE or _cannot_join(b):
            continue
        text = b.cleaned_text.strip()
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        prv = seq[i - 1] if i > 0 else None
        skipped_table_piece = (nxt is not None and b.block_id in has_continuation
                               and any(r in nxt.review_reasons for r in TABLE_FRAGMENT_REASONS))
        if (nxt is not None and not skipped_table_piece and FRAGMENT_OPEN_END_RE.search(text) and _cannot_join(nxt)
                and nxt.continues_from_block_id != b.block_id and FRAGMENT_LOWER_START_RE.match(nxt.cleaned_text)):
            flagged[b.block_id] = (f"clean: 只剩前半句，後半句在無法匯出的區塊 {nxt.block_id}（{nxt.block_type}），"
                                   "未自動刪除，請人工確認")
        elif (prv is not None and b.continues_from_block_id is None and FRAGMENT_LOWER_START_RE.match(text)
              and _cannot_join(prv) and FRAGMENT_OPEN_END_RE.search(prv.cleaned_text.strip())):
            flagged[b.block_id] = (f"clean: 只剩後半句，前半句在無法匯出的區塊 {prv.block_id}（{prv.block_type}），"
                                   "未自動刪除，請人工確認")
    # 規則 4（2026-10-18）：小寫開頭、沒接續任何段落，且閱讀順序上緊鄰的前一個區塊是同頁「未完整解析」的表格。
    # 前半句可能被吃進表格範圍（TX001 p.18 整頁正文與側欄表格被抽成同一個 partial 表格，
    # 「that catch light, …」的前半「(x) Shimmer foundation: … particles」在表格裡）；接不回來，只隔離。
    ordered = [x for x in sorted(blocks, key=lambda x: x.order)
               if not x.is_removed and x.block_type not in ("page_header", "page_footer")]
    for j, b in enumerate(ordered[1:], start=1):
        t = ordered[j - 1]
        if (b.block_type in CONTINUABLE and b.block_id not in flagged and b.continues_from_block_id is None
                and FRAGMENT_LOWER_START_RE.match((b.cleaned_text or "").strip())
                and t.block_type == "table" and t.page_number == b.page_number
                and (t.needs_review or t.parse_status != "ok")):
            flagged[b.block_id] = (f"clean: 只剩後半句，前半句可能在未完整解析的表格 {t.block_id} 範圍內，"
                                   "未自動接回，請人工確認")
    changed = True
    while changed:                       # 連鎖：接在被標記區塊後面的續段
        changed = False
        for b in blocks:
            src = b.continues_from_block_id
            if src in flagged and b.block_id not in flagged and not b.is_removed:
                flagged[b.block_id] = f"clean: 接續的前一段 {src} 待人工確認，單獨匯出會只剩半句，請一併確認"
                changed = True
    issues: list[ExtractionIssue] = []
    for b in blocks:
        msg = flagged.get(b.block_id)
        if msg is None:
            continue
        b.needs_review = True
        b.review_reasons.append(msg)
        b.cleaning_ops.append("flagged_truncated_fragment")
        issues.append(ExtractionIssue(issue_type="truncated_fragment", severity="warning", stage="cleaned",
                                      page_number=b.page_number, block_id=b.block_id, message=msg))
    return issues


# 詞彙表區：'Glossary' 標記行之後、下一個章節標題（或練習題、學習目標標記）之前
_GLOSSARY_STOP_RE = re.compile(f"(?:{EXERCISE_MARKER_RE.pattern})|(?:{OBJECTIVES_MARKER_RE.pattern})", re.I | re.M)
# 詞條開頭：句首（或上一句結束後）的 1–5 個字 + 冒號，例 'Analogous colours: The colours …'
GLOSSARY_TERM_RE = re.compile(r"(?:^|(?<=[.;!?])\s+)[A-Z][\w’'()/-]*(?:\s+[\w’'()/-]+){0,4}\s*:\s+\S")


# 段落鏈停在句子中間：結尾是英文字母、逗號、連字號、左括號、等號，或中文字／中文逗號頓號分號（沒有「。」），
# 或斷字連字號後面黏著頁碼（AR004「the ef-15」：effect 被換頁切開，頁碼 15 併進正文）
CHAIN_OPEN_END_RE = re.compile(r"([A-Za-z,(\-–=＝]|[，、；]|[一-鿿]|[A-Za-z]-\d{1,3})$")
CHAIN_COLON_END_RE = re.compile(r"[:：]$")
CHAIN_MIN_CHARS = 40        # 短於此字數的段落（標籤、短條列）不判斷結尾
UNFINISHED_END_MSG = "clean: 段落停在句子中間，後面沒有接續的段落（後半句可能未擷取或未接上），未自動刪除，請人工確認"
UNFINISHED_START_MSG = "clean: 段落以小寫英文開頭，但沒有接到前一段（前半句可能未擷取或未接上），未自動刪除，請人工確認"
UNFINISHED_LEAD_MSG = "clean: 引導句以冒號結尾，但後面的公式／清單／表格沒有匯出，單獨使用語意不完整，請人工確認"


def _export_chains(blocks: list[TextBlock], reasons: dict) -> list[list[TextBlock]]:
    """模擬 05_export_rag_ready.build_chains：可匯出的區塊依閱讀順序串鏈；中間有一段不能匯出，鏈就斷開。
    回傳的每條鏈 = 05 實際匯出的一筆紀錄（必須與 05 一致，否則偵測的對象和實際匯出的不同）。"""
    chains: list[list[TextBlock]] = []
    tail: dict[str, list[TextBlock]] = {}
    for b in sorted(blocks, key=lambda x: x.order):
        if reasons.get(b.block_id) is not None:
            continue
        chain = tail.pop(b.continues_from_block_id, None) if b.block_type in CONTINUABLE else None
        if chain is None:
            chain = []
            chains.append(chain)
        chain.append(b)
        tail[b.block_id] = chain
    return chains


def flag_unfinished_chains(blocks: list[TextBlock]) -> list[ExtractionIssue]:
    """PDF：可匯出的段落鏈若只剩半句，整條鏈標為待人工確認，避免半句話進 RAG。

    參數：
        blocks：已完成段落接續與其他標記的 PDF 區塊（會直接修改 needs_review、review_reasons）。

    回傳：
        新產生的 ExtractionIssue（issue_type='unfinished_paragraph'）。

    規則（以 05 實際匯出的一筆紀錄為單位，見 _export_chains；資格判斷沿用 lipmatch.eligibility，
    參考文獻、前言等本來就不匯出的不重複標記；只看以段落／列表項開頭的紀錄）：
        1. 鏈的結尾停在句子中間（CHAIN_OPEN_END_RE），且全文 >= 40 字 → UNFINISHED_END_MSG。
           例：AR002「… (Greenhouse and Geisser, 1959) if the Sphericity」後半句因圖說插入沒接上。
        2. 鏈頭以小寫英文開頭 → UNFINISHED_START_MSG。例：AR002「assumption was violated via …」。
        3. 鏈以冒號結尾，且閱讀順序上的下一個內容區塊不能匯出（公式、待確認表格等）→ UNFINISHED_LEAD_MSG。
           例：TH013「… calculated using the following formulae:」後面的公式字形遺失。

    注意：
        只標記、不刪除、不改寫；網頁不套用（原文條列常不加句號，例如使用者決定保留的「Wear it with」）。
        規則 1 只在「確實發生中斷」的位置判斷：段落是所在欄的最後一段（換欄、換頁），或下一個區塊不是正文
        （圖、表、圖說插入）。下一段就在正下方時，沒有句號是原文寫法（TX001 教材的條列句），不是被切斷。
        這是偵測與保護，不是修好接續：其餘根本原因（圖表內文字、無法辨識的版型）在報告中另列。
    """
    # 資格判斷只讀 blocks；此時 search_text 尚未產生，不能建立完整的 cleaned Document（schema 驗證會擋）
    reasons = classify_blocks(SimpleNamespace(blocks=blocks))
    ordered = [b for b in sorted(blocks, key=lambda x: x.order) if not b.is_removed
               and b.block_type not in ("page_header", "page_footer")]
    position = {b.block_id: i for i, b in enumerate(ordered)}
    bottoms = _column_bottoms(blocks)

    def at_break(last: TextBlock, text: str) -> bool:
        """段落後面確實有中斷，句子才可能被切斷：

        - 所在欄的最後一段（換欄、換頁）：任何未完結尾都算。
        - 下一個區塊不是正文段落／標題（圖、表、圖說插入）：只在結尾是逗號、連字號、等號或功能詞
          （the、of、and…）時算；名詞結尾多半是原文不加句號的寫法（TX001「… accentuate the eye and lashes」接圖片）。
        """
        if _ends_its_column(last, bottoms):
            return True
        nxt = next(iter(ordered[position[last.block_id] + 1:]), None)
        if nxt is not None and nxt.block_type in BODY_TYPES_FOR_COLUMN_END:
            return False
        m = LAST_WORD_RE.search(text)
        return bool(re.search(r"[,，、\-–=＝]$", text)) or bool(m and m.group(1).lower() in CONNECTOR_WORDS)

    issues: list[ExtractionIssue] = []
    for chain in _export_chains(blocks, reasons):
        if chain[0].block_type not in CONTINUABLE:
            continue                                  # 圖說、表格等不在此判斷
        text = " ".join((b.cleaned_text or "").strip() for b in chain).strip()
        msg = None
        if len(text) >= CHAIN_MIN_CHARS and CHAIN_OPEN_END_RE.search(text) and at_break(chain[-1], text):
            msg = UNFINISHED_END_MSG
        elif re.match(r"^[a-z]", text):
            msg = UNFINISHED_START_MSG
        elif CHAIN_COLON_END_RE.search(text):
            nxt = next((b for b in ordered[position[chain[-1].block_id] + 1:]
                        if b.block_type not in ("heading", "label")), None)
            if nxt is not None and reasons.get(nxt.block_id) is not None:
                msg = UNFINISHED_LEAD_MSG
        if msg is None:
            continue
        for b in chain:
            b.needs_review = True
            b.review_reasons.append(msg)
            b.cleaning_ops.append("flagged_unfinished_paragraph")
            issues.append(ExtractionIssue(issue_type="unfinished_paragraph", severity="warning", stage="cleaned",
                                          page_number=b.page_number, block_id=b.block_id, message=msg))
    return issues


def flag_merged_glossary_entries(blocks: list[TextBlock]) -> list[ExtractionIssue]:
    """詞彙表裡多個詞條被併成同一段 → 標為待人工確認（拆開後才能各自引用）。

    參數：
        blocks：同一份 PDF 的全部區塊（會直接修改 needs_review、review_reasons）。

    回傳：
        新產生的 ExtractionIssue 清單。

    規則：
        詞彙表區（GLOSSARY_MARKER_RE 之後、下一個章節標題或練習題／學習目標標記之前，
        見 lipmatch.eligibility.marker_region_ids）
        的 paragraph／list_item，含 >= 2 個「詞條: 定義」開頭 → 標記。
        例：TX001 p.99 'Allergic and sensitive skin: Skin sensitive to cold, heat, and wind.
        Analogous colours: The colours found right next to each other'。

    注意：
        只在詞彙表區判斷，正文裡 'Note: … Result: …' 這類句子不受影響；不自動拆分。
    """
    region = marker_region_ids(blocks, GLOSSARY_MARKER_RE, stop_re=_GLOSSARY_STOP_RE)
    issues: list[ExtractionIssue] = []
    for b in blocks:
        if b.block_id not in region or b.is_removed or b.block_type not in CONTINUABLE or b.needs_review:
            continue
        n_terms = len(GLOSSARY_TERM_RE.findall((b.cleaned_text or "").strip()))
        if n_terms < 2:
            continue
        msg = f"clean: 詞彙表有 {n_terms} 個詞條併成一段，請拆分後再核准"
        b.needs_review = True
        b.review_reasons.append(msg)
        b.cleaning_ops.append("flagged_merged_glossary_entries")
        issues.append(ExtractionIssue(issue_type="merged_glossary_entries", severity="warning", stage="cleaned",
                                      page_number=b.page_number, block_id=b.block_id, message=msg))
    return issues


REFERENCE_SECTION_RE = re.compile(r"^\s*(References?|Reference List|Bibliography|參考文獻|參考資料)\b", re.I)


def attach_numeric_citations(blocks: list[TextBlock]) -> int:
    """編號引用（"[31]"、"[6]"）對應到文件自己的參考文獻清單，附加到 cited_works。回傳有引用的區塊數。"""
    def in_refs(b: TextBlock) -> bool:
        return bool(REFERENCE_SECTION_RE.match((b.section or "").split(" > ")[0]))

    refs = parse_reference_list(b.cleaned_text for b in sorted(blocks, key=lambda x: x.order)
                                if in_refs(b) and not b.is_removed)
    n = 0
    for b in blocks:
        if in_refs(b) or b.is_removed:
            continue
        found = find_numeric_citations(b.cleaned_text, refs)
        if found:
            b.cited_works += [c for c in found if c not in b.cited_works]
            n += 1
    return n


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def rebuild_html_sections(blocks: list[TextBlock], scope=None) -> None:
    """HTML：移除雜訊後，只用「保留下來的」標題重建 section 路徑。

    擷取時側欄、留言區的 <h3> 也會推進章節堆疊；清洗後應該排除它們，
    否則正文會被標成「延伸閱讀」之類的章節。

    scope：lipmatch.html_scope.ScopeChecker（可省略）。給了就再檢查每個非標題區塊是否真的在
    目前標題的範圍內；<main> 外的網站通知、文章中嵌入的互動模組不繼承章節（section＝None），
    並在 cleaning_ops 記 section_not_inherited:<原因>。標題與其他區塊的章節不變。
    """
    stack: list[tuple[int, str, TextBlock]] = []
    for b in sorted(blocks, key=lambda x: x.order):
        if b.block_type == "heading" and not b.is_removed and b.heading_level:
            while stack and stack[-1][0] >= b.heading_level:
                stack.pop()
            stack.append((b.heading_level, b.cleaned_text or "", b))
        new = " > ".join(t for _, t, _ in stack) or None
        why = scope.reason(b, stack[-1][2]) if (scope is not None and stack and new) else None
        if why and why.startswith(REVIEW_PREFIX):
            if not b.is_removed:
                b.cleaning_ops.append(f"section_scope_review:{why[len(REVIEW_PREFIX):]}")  # 證據不足：保留章節，待人工
        elif why:
            new = None
            if not b.is_removed:
                b.cleaning_ops.append(f"section_not_inherited:{why}")
        if new != b.section:
            b.section = new
            if not b.is_removed:
                b.cleaning_ops.append("rebuilt_section_path")


def html_scope_checker(manifest) -> tuple[Optional[ScopeChecker], Optional[ExtractionIssue]]:
    """讀 manifest 指的原 HTML 並核對 SHA-256；讀不到或雜湊不符就不檢查範圍。
    相對路徑先以專案根目錄為準，找不到再以 LIPMATCH_DATA_ROOT 為準（程式與資料分開放時；與 01 rel_posix 對應）。"""
    p = Path(manifest.source_path)
    if not p.is_absolute():
        bases = [ROOT] + ([Path(os.environ["LIPMATCH_DATA_ROOT"])] if os.environ.get("LIPMATCH_DATA_ROOT") else [])
        p = next((b / p for b in bases if (b / p).exists()), ROOT / p)
    if not p.exists():
        return None, ExtractionIssue(issue_type="section_scope_unchecked", severity="info", stage="cleaned",
                                     message=f"找不到原 HTML（{manifest.source_path}），章節範圍未檢查")
    data = p.read_bytes()
    if manifest.sha256 and hashlib.sha256(data).hexdigest() != manifest.sha256:
        return None, ExtractionIssue(issue_type="section_scope_unchecked", severity="warning", stage="cleaned",
                                     message="原 HTML 的 SHA-256 與擷取時不同，章節範圍未檢查")
    return ScopeChecker(data), None


PRESERVE_LAYOUT_TYPES = {"formula", "figure_text", "equation_label"}


def clean_preserving_lines(text: str) -> tuple[str, list[str]]:
    """保留換行與行內排列，只做 Unicode 安全正規化並去掉每行頭尾空白。

    用於公式、圖表文字、以及 partial/failed 區塊：這些內容的「排列」本身就是資訊，
    接成一行可能改變意義（例如分子／分母、軸標籤與數值）。
    """
    t, ops = normalize_unicode(text)
    t, o = normalize_pua_bullets(t)
    ops += o
    lines = [ln.strip() for ln in t.split("\n")]
    out = "\n".join(ln for ln in lines if ln)
    if out != t:
        ops.append("stripped_line_whitespace_kept_line_breaks")
    return out, ops


def clean_text(text: str, block_type: str, source_type: str, preserve_layout: bool = False,
               hyphenation: Optional[HyphenationPolicy] = None) -> tuple[str, list[str]]:
    """單一區塊文字的清洗管線（不含移除判斷）。preserve_layout=True 時不重排。"""
    if preserve_layout or block_type in PRESERVE_LAYOUT_TYPES:
        return clean_preserving_lines(text)
    ops: list[str] = []
    t, o = normalize_unicode(text)
    ops += o
    t, o = normalize_pua_bullets(t)
    ops += o
    t, o = (fix_pdf_line_breaks(t, hyphenation.decide if hyphenation else False)
            if block_type != "toc_entry" else (t, []))
    ops += o
    if block_type == "toc_entry":
        t, o = collapse_toc_leaders(t)
        ops += o
    t, o = collapse_whitespace(t)
    ops += o
    return t, ops


def clean_block(block: TextBlock, source_type: str, removal_reason: Optional[str],
                hyphenation: Optional[HyphenationPolicy] = None) -> tuple[TextBlock, list[ExtractionIssue]]:
    """清洗單一區塊，回傳新的 TextBlock（不修改輸入）與新產生的 issues。"""
    b = block.model_copy(deep=True)
    # 重設清洗欄位，確保從 raw_text 重新計算（可重複執行）
    b.cleaning_ops, b.is_removed, b.removal_reason, b.continues_from_block_id = [], False, None, None
    b.review_reasons = [r for r in b.review_reasons if not r.startswith("clean:")]
    b.needs_review = bool(b.review_reasons)
    issues: list[ExtractionIssue] = []

    if b.table_data is not None:
        b.cleaned_table_data = clean_table(b.table_data)
        cleaned = serialize_table(b.cleaned_table_data)
        ops = ["cleaned_table_cells"]
    elif not b.raw_text.strip():
        cleaned, ops = "", ["unparsed_region_kept"]  # 解析失敗的區域：保留紀錄，不當成空白刪除
    else:
        cleaned, ops = clean_text(b.raw_text, b.block_type, source_type, preserve_layout=b.parse_status != "ok",
                                  hyphenation=hyphenation)
    b.cleaning_ops = ops

    missing = verify_content_preserved(b.raw_text, cleaned)
    if missing:
        cleaned, _ = collapse_whitespace(b.raw_text)
        b.cleaning_ops = ["content_check_failed_fallback_to_raw"]
        msg = f"clean: 清洗後遺失受保護內容 {missing[:10]}，已退回原文，請人工確認"
        b.needs_review = True
        b.review_reasons.append(msg)
        issues.append(ExtractionIssue(issue_type="content_check_failed", severity="error", stage="cleaned",
                                      page_number=b.page_number, block_id=b.block_id, message=msg))

    if removal_reason is None and source_type == "html":
        removal_reason = classify_html_noise(b)
        if removal_reason and DATA_SIGNAL_RE.search(b.raw_text):
            msg = f"clean: 位於疑似雜訊區 ({removal_reason}) 但含色彩/研究數據，未自動移除，請人工確認"
            b.needs_review = True
            b.review_reasons.append(msg)
            issues.append(ExtractionIssue(issue_type="noise_candidate_contains_data", severity="warning",
                                          stage="cleaned", block_id=b.block_id, message=msg))
            removal_reason = None
    if removal_reason is None and not cleaned.strip() and b.parse_status == "ok" and b.table_data is None:
        removal_reason = "empty_after_cleaning"

    b.cleaned_text = cleaned
    if removal_reason:
        b.is_removed = True
        b.removal_reason = removal_reason
        b.cleaning_ops.append("removed_block")
    return b, issues


CONTENT_HOLDS_PATH = ROOT / "config" / "content_holds.json"
CONTENT_HOLD_PREFIX = "clean: 人工決定排除："


def load_content_holds(path: Path = CONTENT_HOLDS_PATH) -> list[dict]:
    """讀人工決定排除的內容清單（config/content_holds.json）；檔案不存在時回傳空清單。"""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("holds", [])


def apply_content_holds(blocks: list[TextBlock], manifest, holds: list[dict]) -> list[ExtractionIssue]:
    """把人工決定排除的區塊標為待確認並寫明原因（不刪除、不改寫），05 依資格判斷不匯出。

    參數：
        blocks：清洗中的區塊（會直接修改 needs_review、review_reasons、cleaning_ops）。
        manifest：文件的 SourceManifest（doc_id、原始檔 sha256）。
        holds：load_content_holds() 的清單。

    回傳：
        ExtractionIssue（issue_type='content_hold'）清單。

    錯誤：
        設定綁定的原始檔 SHA-256 與這次輸入不同，或找不到文字指紋相符的區塊 → ValueError。
        來源或擷取結果變了，舊的排除決定可能不再對應同一段文字，必須重新核對後更新設定；
        不可只改指紋讓它通過（修補受來源指紋約束）。
    """
    issues: list[ExtractionIssue] = []
    for h in holds:
        if h["doc_id"] != manifest.doc_id:
            continue
        if manifest.sha256 != h["source_sha256"]:
            raise ValueError(f"{manifest.doc_id}：原始檔指紋與 content_holds 不符，請重新核對排除設定（{h['text_start']}）")
        hits = [b for b in blocks if not b.is_removed and b.page_number == h.get("page")
                and hashlib.sha256(" ".join((b.cleaned_text or "").split()).encode("utf-8")).hexdigest() == h["text_sha256"]]
        if not hits:
            raise ValueError(f"{manifest.doc_id}：找不到與 content_holds 文字指紋相符的區塊（p.{h.get('page')} "
                             f"「{h['text_start']}」），擷取結果可能已變動，請重新核對")
        for b in hits:
            msg = CONTENT_HOLD_PREFIX + h["reason"]
            b.needs_review = True
            b.review_reasons.append(msg)
            b.cleaning_ops.append("content_hold")
            issues.append(ExtractionIssue(issue_type="content_hold", severity="info", stage="cleaned",
                                          page_number=b.page_number, block_id=b.block_id, message=msg))
    return issues


def clean_document(doc: Document, holds: Optional[list[dict]] = None) -> Document:
    """清洗整份文件，回傳 stage='cleaned' 的新 Document。輸入物件不會被修改。

    holds：人工決定排除的內容（預設讀 config/content_holds.json，見 apply_content_holds）。
    """
    holds = load_content_holds() if holds is None else holds
    source_type = doc.manifest.source_type
    margin_removals = detect_repeated_margin_text(doc.blocks) if source_type == "pdf" else {}
    hyphenation = build_hyphenation_policy(doc.blocks) if source_type == "pdf" else None
    issues = [i for i in doc.issues if i.stage == "extracted"]  # 重跑時丟棄舊的 cleaned issues
    blocks: list[TextBlock] = []
    for blk in doc.blocks:
        nb, new_issues = clean_block(blk, source_type, margin_removals.get(blk.block_id), hyphenation)
        blocks.append(nb)
        issues += new_issues
    if source_type == "pdf":
        issues += mark_figure_captions(blocks)
        issues += mark_caption_variants(blocks)       # 句號式／並排式標題、標題續行、表格註記
        link_cross_page_continuations(blocks)
        issues += apply_content_holds(blocks, doc.manifest, holds)   # 先排除，前後段的半句保護才會一併處理
        issues += flag_merged_glossary_entries(blocks)   # 先標詞彙表併段，原因比「半句」更具體
        issues += flag_truncated_fragments(blocks)
        issues += flag_unfinished_chains(blocks)   # 最後一道：其餘只剩半句的段落鏈
    else:
        mark_html_metadata(blocks)
        scope, scope_issue = html_scope_checker(doc.manifest)
        if scope_issue is not None:
            issues.append(scope_issue)
        rebuild_html_sections(blocks, scope)
        if scope is not None and scope.unresolved:
            issues.append(ExtractionIssue(
                issue_type="section_scope_unresolved", severity="info", stage="cleaned",
                message=f"{len(set(scope.unresolved))} 個區塊無法在原 HTML 定位，章節照舊繼承、未檢查範圍",
                details={"block_ids": ",".join(sorted(set(scope.unresolved))[:50])}))
        review = [b.block_id for b in blocks if any(o.startswith("section_scope_review:") for o in b.cleaning_ops)]
        if review:
            issues.append(ExtractionIssue(
                issue_type="section_scope_review", severity="info", stage="cleaned",
                message=f"{len(review)} 個區塊旁有互動元件但屬正文結構，章節已保留，請人工確認是否為網站模組",
                details={"block_ids": ",".join(review[:50])}))
        issues += apply_content_holds(blocks, doc.manifest, holds)
    vocab = word_vocabulary(b.cleaned_text for b in blocks if not b.is_removed)
    for b in blocks:
        b.search_text, ops = build_search_text(b.cleaned_text, vocab)
        b.cleaning_ops += ops
        b.cited_works = find_citations(b.cleaned_text)
    attach_numeric_citations(blocks)
    return Document(stage="cleaned", manifest=doc.manifest.model_copy(deep=True), blocks=blocks, issues=issues,
                    pages=[p.model_copy(deep=True) for p in doc.pages])


def build_comparison_report(doc: Document, max_blocks: Optional[int] = None) -> str:
    """產生 Markdown 清洗前後對照報告（摘要統計 + 每個有變動區塊的 Before/After）。"""
    m = doc.manifest
    changed = [b for b in doc.blocks if b.cleaned_text != b.raw_text]
    removed = [b for b in doc.blocks if b.is_removed]
    reasons = Counter(b.removal_reason for b in removed)
    ops = Counter(op for b in doc.blocks for op in b.cleaning_ops)
    raw_chars = sum(len(b.raw_text) for b in doc.blocks)
    kept_chars = sum(len(b.cleaned_text or "") for b in doc.blocks if not b.is_removed)
    lines = [
        f"# 清洗報告：{m.doc_id}",
        "",
        f"- 來源：`{m.source_path}`（{m.source_type}，sha256 `{m.sha256[:12]}…`）",
        f"- 處理頁：{m.pages_processed if m.pages_processed else '—'}",
        f"- 區塊：{len(doc.blocks)}；有變動：{len(changed)}；移除：{len(removed)}；"
        f"待人工確認：{sum(b.needs_review for b in doc.blocks)}",
        f"- 字元數：raw {raw_chars} → 保留區塊 cleaned {kept_chars}",
        "",
        "## 移除原因", "",
        *([f"- `{r}` × {n}" for r, n in reasons.most_common()] or ["- （無）"]),
        "", "## 清洗操作次數", "",
        *([f"- `{o}` × {n}" for o, n in ops.most_common()] or ["- （無）"]),
        "", "## 待人工確認", "",
        *([f"- `{i.issue_type}` p.{i.page_number or '-'} {i.block_id or ''}：{i.message}" for i in doc.issues if not i.resolved]
          or ["- （無）"]),
        "", "## 區塊對照（Before → After）", "",
    ]
    for b in changed[: max_blocks or len(changed)]:
        lines += [
            f"### {b.block_id} · {b.block_type} · p.{b.page_number or '-'}"
            + (" · **REMOVED** (" + (b.removal_reason or "") + ")" if b.is_removed else ""),
            f"section: {b.section or '—'}  ",
            f"ops: {', '.join(b.cleaning_ops) or '—'}",
            "", "Before:", "```text", b.raw_text, "```", "After:", "```text", b.cleaned_text or "", "```", "",
        ]
    return "\n".join(lines)


def ensure_outside_raw(path: Path) -> None:
    p, raw = path.resolve(), RAW_DIR.resolve()
    if p == raw or raw in p.parents:
        raise ValueError(f"拒絕寫入原始資料夾：{p}")


def iter_inputs(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.glob("*.json")) if p.is_dir() else [p]
    return out


def run(inputs: list[Path], out_dir: Path, report_dir: Path, review_dir: Path) -> list[Path]:
    for d in (out_dir, report_dir, review_dir):
        ensure_outside_raw(d)
    written = []
    for path in iter_inputs(inputs):
        doc = Document.load(path)
        cleaned = clean_document(doc)
        doc_id = cleaned.manifest.doc_id
        out = cleaned.save(out_dir / f"{doc_id}.json")
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{doc_id}.clean_report.md").write_text(build_comparison_report(cleaned), encoding="utf-8")
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{doc_id}.clean_review.json").write_text(
            json.dumps(cleaned.review_queue(), ensure_ascii=False, indent=2), encoding="utf-8")
        n_removed = sum(b.is_removed for b in cleaned.blocks)
        n_changed = sum(b.cleaned_text != b.raw_text for b in cleaned.blocks)
        print(f"[clean] {path.name} → {out.relative_to(ROOT).as_posix() if ROOT in out.resolve().parents else out}")
        print(f"        blocks={len(cleaned.blocks)} changed={n_changed} removed={n_removed} "
              f"needs_review={sum(b.needs_review for b in cleaned.blocks)} issues={len(cleaned.issues)}")
        written.append(out)
    return written


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LipMatch AI 文獻清洗 (extracted JSON → cleaned JSON)")
    ap.add_argument("inputs", nargs="+", type=Path, help="01_extract 輸出的 JSON 檔或資料夾")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    ap.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    args = ap.parse_args(argv)
    run(args.inputs, args.out, args.report_dir, args.review_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
