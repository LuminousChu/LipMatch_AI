"""HTML 區塊是否應繼承目前的文章章節（section）。

問題：02_clean.rebuild_html_sections 依閱讀順序，用「上一個保留下來的標題」替每個區塊建章節路徑。
頁面上的網站通知（在 <main> 外）或文章中嵌入的互動模組（例如 AI 助理推廣），因此會被標成
它前面那個彩妝小節，搜尋時就帶著錯的主題。

一般規則（不看題號、片段 ID 或網站名稱）：
  1. outside_main_content：頁面有 <main>（或 role="main"），目前章節的標題在 main 裡，
     區塊卻在 main 外 → 不繼承。
  2. ui_module：取區塊與目前標題的最近共同祖先 L；L 底下包住區塊的那個子節點 C 同時符合
     (a) C 是容器（不是段落、清單項等文字元素本身）、(b) 含互動元件（button／input／select／textarea／form）、
     (c) 自己沒有任何 h1–h6、(d) 裡面沒有任何正文段落元素（p、li、dd、dt、blockquote、pre、td）
     → C 是嵌入模組，不繼承。
     只符合 (b)(c)、但 (a) 或 (d) 不成立（例如教學段旁或段內的「複製步驟」按鈕）→ 證據不足，
     保留章節並回報 review:control_near_prose，交人工核對，不自動清空。
只改「這個區塊要不要繼承」；標題本身、原文、區塊 ID 與其他區塊的章節都不變。

定位方式：01_extract 的 source_locator（dom_path，例如 body[1]>div[1]>main[1]>p[3]，段落串流為 …>#text）
在同一份原 HTML、同一個 html.parser 解析結果上逐層找回節點。找不到或文字對不上 → 回報 unresolved，不改章節。
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

CONTROL_TAGS = ("button", "input", "select", "textarea", "form")
PROSE_TAGS = ("p", "li", "dd", "dt", "blockquote", "pre", "td")
REVIEW_PREFIX = "review:"
HEADING_RE = re.compile(r"^h[1-6]$")
_PART_RE = re.compile(r"^(?P<name>[^\[\]]+)\[(?P<idx>\d+)\]$")
_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub("", s or "")


class ScopeChecker:
    """用原 HTML 判斷區塊是否在目前標題的章節範圍內。"""

    def __init__(self, html: bytes):
        self.soup = BeautifulSoup(html, "html.parser")
        self.main = self.soup.find(lambda t: isinstance(t, Tag) and (t.name == "main" or t.get("role") == "main"))
        self.unresolved: list[str] = []
        self._cache: dict[str, Optional[Tag]] = {}

    def element(self, locator: Optional[str]) -> Optional[Tag]:
        """依 dom_path 找回節點；'#text' 結尾表示串流段落，取其容器。"""
        if not locator:
            return None
        if locator in self._cache:
            return self._cache[locator]
        node: Optional[Tag] = self.soup.html if self.soup.html is not None else self.soup
        for part in locator.split(">"):
            if part == "#text":
                break
            m = _PART_RE.match(part)
            if not m or node is None:
                node = None
                break
            kids = node.find_all(m["name"], recursive=False)
            idx = int(m["idx"])
            node = kids[idx - 1] if len(kids) >= idx else None
        self._cache[locator] = node
        return node

    def _located(self, block) -> Optional[Tag]:
        el = self.element(block.source_locator)
        if el is None:
            self.unresolved.append(block.block_id)
            return None
        raw = _norm(block.raw_text)
        if raw and raw[:40] not in _norm(el.get_text()):
            self.unresolved.append(block.block_id)
            return None
        return el

    @staticmethod
    def _ancestors(el: Tag) -> list[Tag]:
        return [el] + [p for p in el.parents if isinstance(p, Tag)]

    def reason(self, block, heading) -> Optional[str]:
        """回傳不繼承的原因（'outside_main_content'／'ui_module'）；證據不足回傳 'review:control_near_prose'
        （呼叫端保留章節、只做標記）；應繼承則回傳 None。"""
        if heading is None or block.block_type == "heading":
            return None
        el, h = self._located(block), self._located(heading)
        if el is None or h is None:
            return None
        a_el, a_h = self._ancestors(el), self._ancestors(h)
        if self.main is not None and self.main in a_h and self.main not in a_el:
            return "outside_main_content"
        ids_h = {id(x) for x in a_h}
        k = next((i for i, x in enumerate(a_el) if id(x) in ids_h), None)
        if k is None or k == 0:
            return None
        branch = a_el[k - 1]  # L 底下包住區塊的子節點（可能就是區塊本身）
        has_control = branch.name in CONTROL_TAGS or branch.find(CONTROL_TAGS) is not None
        has_heading = bool(HEADING_RE.match(branch.name)) or branch.find(HEADING_RE) is not None
        if not has_control or has_heading:
            return None
        is_container = branch is not el and branch.name not in PROSE_TAGS
        has_prose = branch.find(PROSE_TAGS) is not None
        if is_container and not has_prose:
            return "ui_module"
        return REVIEW_PREFIX + "control_near_prose"
