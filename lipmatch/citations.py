"""從段落文字找出「引用他人研究」的引用標記：作者–年份與編號引用。

用途：RAG 回答時區分「本研究發現」與「本研究引用」。例如 Leeds p.75 的
"The orange hue is deemed particularly attractive to yellowish skin ... (Zhang, 2019)"
是轉述 Zhang (2019)，不能寫成 Chen (2024) 的結論。

支援兩種寫法：
- 括號引用：(Russell, 2003; Lu et al., 2022)、(Elliot & Niesta, 2008)、(Frost, 1988, 2005)、(CIE, 2019)
- 敘述引用：Zhang (2019)、Kiritani et al. (2017)、Ohta and Roberston (2005)、Berns et al.(1993)

只抓有作者名＋年份的引用；單獨的 "(2012)" 若前面沒有作者名就略過。輸出統一成 "作者, 年份"。

編號引用（期刊 "[31]"、"[10, 36–39]"；中文碩論 "[6]"）另由 find_numeric_citations 處理：
必須對得上文件本身的參考文獻清單（parse_reference_list），否則不算引用——
例如 "[0, 255]" 這種數值範圍不會被當成引用。輸出 "[31] 條目開頭…"，方便判斷引用的是哪篇研究。
"""

from __future__ import annotations

import re

YEAR = r"(?:19|20)\d{2}[a-z]?"
YEARS = rf"{YEAR}(?:\s*,\s*{YEAR})*"
NAME = r"[A-Z][\w'’\-]+"
ET_AL = r"\s*,?\s*et\s*,?\s*al\.?,?"

NARRATIVE_RE = re.compile(
    rf"\b(?P<auth>{NAME}(?:{ET_AL}|(?:\s*,\s*{NAME})*\s*,?\s*(?:and|&)\s*{NAME})?)\s*\((?P<years>{YEARS})\)"
)
PAREN_RE = re.compile(r"\(([^()]*\b(?:19|20)\d{2}[^()]*)\)")
ITEM_RE = re.compile(rf"^(?P<auth>[A-Z][^;()\d]*?)\s*,?\s*(?P<years>{YEARS})(?:\s*,\s*p+\.\s*[\d\-–]+)?$")
PREFIX_RE = re.compile(r"^(?:e\.g\.,?|i\.e\.,?|see(?: also)?|cf\.)\s*", re.I)


# 句首連接詞會被敘述引用的「Name, Name and Name」吃進去，例如 "Similarly, Jones and Hill (1993)"
LEADING_WORDS_RE = re.compile(
    r"^(?:Similarly|However|Moreover|Furthermore|Additionally|Also|Thus|Therefore|Hence|Recently|Notably|"
    r"Likewise|Conversely|Meanwhile|Overall|Finally|Firstly|Secondly|Indeed|According to|In addition)\s*,?\s+"
)


def _normalize_author(auth: str) -> str:
    auth = re.sub(ET_AL + r"$", " et al.", auth.strip())
    auth = LEADING_WORDS_RE.sub("", auth)
    auth = re.sub(r"\s*&\s*", " and ", auth)                # Elliot & Niesta → Elliot and Niesta
    return re.sub(r"\s+", " ", auth).strip(" ,")


def _emit(auth: str, years: str, out: list[str]) -> None:
    a = _normalize_author(auth)
    for y in re.findall(YEAR, years):
        ref = f"{a}, {y}"
        if ref not in out:
            out.append(ref)


def find_citations(text: str) -> list[str]:
    """回傳文字中引用的研究，依出現順序去重，例如 ['Russell, 2003', 'Lu et al., 2022']。"""
    out: list[str] = []
    for m in PAREN_RE.finditer(text):
        for item in m.group(1).split(";"):
            item = PREFIX_RE.sub("", item.strip())
            im = ITEM_RE.match(item)
            if im and len(im.group("auth")) <= 80:
                _emit(im.group("auth"), im.group("years"), out)
    for m in NARRATIVE_RE.finditer(text):
        _emit(m.group("auth"), m.group("years"), out)
    # 敘述引用只抓到多字作者的最後一個字（"Chanel Beauty (2023)" → "Beauty, 2023"）時，保留完整的那個
    return [r for r in out if not any(o != r and o.endswith(" " + r) for o in out)]


# ---------------------------------------------------------------------------
# 編號引用
# ---------------------------------------------------------------------------

NUM = r"\d{1,3}"
NUM_ITEM = rf"{NUM}(?:\s*[-–—]\s*{NUM})?"
NUMERIC_CITE_RE = re.compile(rf"\[\s*({NUM_ITEM}(?:\s*[,，;]\s*{NUM_ITEM})*)\s*\]")
REF_BRACKET_RE = re.compile(r"\[(\d{1,3})\]\s*")            # "[1] …[2] …"（中文碩論常多筆擠在同一段）
REF_NUMBERED_RE = re.compile(r"^\s*(\d{1,3})\.\s+(\S.*)$", re.S)  # "1. Schaffer SE. …"（期刊一筆一段）
MAX_RANGE = 50


def parse_reference_list(texts) -> dict[int, str]:
    """參考文獻清單 → {編號: 條目文字}。同一編號只取第一次出現。"""
    refs: dict[int, str] = {}
    for text in texts:
        t = " ".join((text or "").split())
        marks = list(REF_BRACKET_RE.finditer(t))
        if marks and marks[0].start() == 0:
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
                refs.setdefault(int(m.group(1)), t[m.end():end].strip())
            continue
        m = REF_NUMBERED_RE.match(t)
        if m:
            refs.setdefault(int(m.group(1)), m.group(2).strip())
    return refs


def _expand(spec: str) -> list[int]:
    """"10, 36–39" → [10, 36, 37, 38, 39]。範圍過大（> 50）視為不是引用。"""
    nums: list[int] = []
    for part in re.split(r"[,，;]", spec):
        bounds = [int(x) for x in re.findall(r"\d+", part)]
        if len(bounds) == 2:
            a, b = bounds
            if not 0 < b - a <= MAX_RANGE:
                return []
            nums += list(range(a, b + 1))
        elif len(bounds) == 1:
            nums.append(bounds[0])
    return nums


def find_numeric_citations(text: str, refs: dict[int, str], snippet: int = 100) -> list[str]:
    """回傳文字中的編號引用，例如 ['[31] Fairchild MD. Color appearance models…']。
    refs 為空（文件沒有編號式參考文獻）或任何一個編號不在清單中 → 那組括號不算引用。"""
    if not refs:
        return []
    out: list[str] = []
    for m in NUMERIC_CITE_RE.finditer(text or ""):
        nums = _expand(m.group(1))
        if not nums or any(n not in refs for n in nums):
            continue
        for n in nums:
            entry = refs[n]
            item = f"[{n}] {entry[:snippet]}{'…' if len(entry) > snippet else ''}".strip()
            if item not in out:
                out.append(item)
    return out
