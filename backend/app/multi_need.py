"""multi-need-v1（2026-11-07）：一句話有多個需求時，每個需求各查一次，保證每個需求都有證據進模型。

為什麼：「上班用什麼唇色」加上「唇色偏深怎麼辦」這種一句兩問，整句只查一次，前 25 名幾乎都是
「唇色深怎麼處理」，講上班顏色的段落沒進池（只有一段第 22 名、沒送進模型），回答只答了一半，還說「證據沒提到上班顏色」。

做法（保守：整句的檢索結果不動，只「補」各需求最相關的幾段到裝箱最前面）：
1. split_needs：依標點與連接詞（而且、還有、另外…）切句；有在「問」的子句是需求，只描述狀況的子句接到後面的需求；
   只有語氣、沒有內容的需求（「真的一定要嗎」「我該怎麼辦」）併回前一個。少於 2 個需求就不拆（回空清單）。
   接續題（含「；」，上一則；本句）不拆。
2. guarantee_front：每個需求各自檢索的前 per_need 段，依需求輪流排到裝箱順序最前面，其餘照原本順序。
   裝箱只取前 N 段，所以每個需求至少有 per_need 段送進模型。
不呼叫模型；拆句是固定規則（可重現、可測試）。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Sequence

MULTI_NEED_VERSION = "multi-need-v1"
SUB_K = 8          # 每個需求自己檢索的名次數
PER_NEED = 2       # 每個需求保證送進模型的段數

_NEED = re.compile(r"怎麼|怎樣|如何|什麼|哪[一個種些款]?|嗎|？|\?|推薦|建議|要擦|該擦|要用|該用|挑|選|好不好|差在|差別|比較好|適合")
_SPLIT = re.compile(r"[，,；;。！!？?]|而且|還有|另外|然後|同時|以及")
# 拿掉這些之後還剩不到 2 個字的需求，算「只有語氣、沒有內容」
_FILLER = re.compile(r"我|你|該|要|會|能|可以|真的|一定|是不是|請問|這樣|那|就|嗎|呢|啊|吧|怎麼辦|怎麼|如何|什麼|[，,？?。！!\s]")
_MIN_CHARS = 4


def split_needs(question: str) -> list[str]:
    """一句話有 2 個以上需求時回傳各需求的查詢句（條件子句接在需求前）；否則回 []。"""
    if not question or "；" in question:
        return []
    parts = [p.strip() for p in _SPLIT.split(question) if p and p.strip()]
    groups: list[str] = []
    pending: list[str] = []
    for p in parts:
        if _NEED.search(p):
            groups.append("，".join(pending + [p]))
            pending = []
        else:
            pending.append(p)
    if pending and groups:
        groups[-1] = groups[-1] + "，" + "，".join(pending)
    merged: list[str] = []
    for g in groups:
        if merged and len(_FILLER.sub("", g)) < 2:
            merged[-1] = merged[-1] + "，" + g
        else:
            merged.append(g)
    merged = [g for g in merged if len(re.sub(r"[^一-鿿A-Za-z0-9]", "", g)) >= _MIN_CHARS]
    if len(merged) < 2:
        return []
    # 後面的需求太短、沒有主詞（M09「唇妝用什麼」）：借第一個需求問句前的主詞（「草莓女孩妝」）
    m = _NEED.search(merged[0])
    subject = _SUBJECT_LEAD.sub("", merged[0][:m.start()]).strip("，, ") if m else ""
    if 2 <= len(subject) <= 10:
        merged = [merged[0]] + [f"{subject} {g}" if _GENERIC_HEAD.match(g) and len(_content(g)) <= _SHORT
                                and subject not in g else g for g in merged[1:]]
    return merged


_SUBJECT_LEAD = re.compile(r"^(?:我想要|我想|我|很多人說|請問|如果|想問|想要)")
# 只有泛稱開頭（「唇妝用什麼」）才借主詞；「我嘴唇偏乾」這種自己有主題的不借
_GENERIC_HEAD = re.compile(r"^(?:唇妝|底妝|眼妝|腮紅|妝容|顏色|色號|質地|唇色)")
_SHORT = 4


def _content(text: str) -> str:
    return re.sub(r"[^一-鿿A-Za-z0-9]", "", _FILLER.sub("", text))


# 子查詢開頭的口語自述（「我在挑我…」「我想找…」「我的…」）會把向量拉偏：
# 實測：「我在挑我…」開頭的上班唇色子查詢，講辦公室顏色的 PR031_h0027 排第 13；拿掉開頭後排第 2（11-07 離線檢索）
_LEAD_SELF = re.compile(r"^(?:我們的|我的|(?:我們|我)(?:現在|正在|想要|打算|在|想|要)?(?:挑|找|選|買|看)?(?:一下)?(?:我們的|我的|我)?)")
_INNER_SELF = re.compile(r"[，,](?:我|我們)(?:該|要|應該)?")


def clean_query(sub_query: str) -> str:
    """子查詢檢索前拿掉口語自述開頭與子句開頭的「我（該）」；拿完太短（少於 4 個字）就用原句。只用在子查詢，整句查詢不動。"""
    q = _INNER_SELF.sub("，", _LEAD_SELF.sub("", sub_query.strip())).strip("，, ")
    return q if len(re.sub(r"[^一-鿿A-Za-z0-9]", "", q)) >= _MIN_CHARS else sub_query


def renumber_extra_hits(first: Sequence[Any], all_hits: Sequence[Any]) -> list[Any]:
    """子查詢新找到的命中接在原本命中後面重新編名次（retrieval_hits 的主鍵含名次，不能重複）；
    原本的命中與名次不動。命中物件要有 chunk_id、rank（dataclass，用 replace 換名次）。"""
    from dataclasses import replace
    seen = {h.chunk_id for h in first}
    start = max((h.rank for h in first), default=0)
    extra = [h for h in all_hits if h.chunk_id not in seen]
    return list(first) + [replace(h, rank=start + i) for i, h in enumerate(extra, 1)]


def guarantee_front(pack_input: Sequence[Any], sub_lists: Sequence[Sequence[Any]], unit_id: Callable[[Any], str],
                    per_need: int = PER_NEED) -> tuple[list[Any], list[list[str]]]:
    """各需求檢索的前 per_need 段，依需求輪流放最前面（同一段只放一次）；回傳（新順序, 每個需求被保證的段）。
    同一段在原本的候選裡已有物件時用原本的物件（保留原本的分數與欄位）。"""
    existing = {unit_id(c): c for c in pack_input}
    picks = [[c for c in lst][:per_need] for lst in sub_lists]
    front, seen = [], set()
    guaranteed: list[list[str]] = [[] for _ in sub_lists]
    for i in range(per_need):
        for n, lst in enumerate(picks):
            if i < len(lst):
                u = unit_id(lst[i])
                guaranteed[n].append(u)
                if u not in seen:
                    seen.add(u)
                    front.append(existing.get(u, lst[i]))
    rest = [c for c in pack_input if unit_id(c) not in seen]
    return front + rest, guaranteed
