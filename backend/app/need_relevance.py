"""需求相關性排序（need-rel-v1，2026-10-26）：裝箱前依「問題的需求」與「片段的適用條件」重排候選池，不呼叫模型。

為什麼：selector=none 原本照向量名次裝前 7 段。全開實驗 W05 問「抽象色號名稱要怎麼知道實際顏色」，前 7 名幾乎都是
研究觀察段落，真正的確認方法（片段屬性「選色、操作：試色」）排在第 15 名送不進模型；N01 也把紅唇商品宣稱段落裝了進去。

做法（通用規則，不看題號、答案或片段 ID）：
  1. 需求線索：問題裡的線索詞對應到片段屬性標籤（support_scope 的「條件／操作／用途／部位」值），
     例如「乾」→ 乾唇、保濕；「煙燻／眼妝」→ 眼妝重、眼部；「怎麼知道／實際顏色」→ 試色。
     命中「條件／操作／用途」權重 2，命中「部位」權重 1。
  2. 問做法（怎麼／如何／辦法…）的題目：命中「條件／操作／用途」的片段之間，研究觀察、背景、商品宣稱排在做法類之後
     （只命中「部位」不算：幾乎每段都有「嘴唇」，否則會把只是部位相符的無關段落提前）。
  3. 條件判定：使用者明說了底調，而片段的條件列了底調卻不含使用者的 → 排到最後（不刪，保留給預算有餘時）。
     底調相符的片段加權 2。
  4. need-rel-v2（2026-10-26）排序鍵：(條件不符, 是否命中強標籤, −強標籤分數, 角色排後, 原向量名次)。
     只有命中「條件／操作／用途」（或底調相符）才往前提；只命中「部位」這類幾乎每段都有的弱標籤不再改變順序，
     照原向量名次。v1 用總分排序時，屬性標得不完整的片段（例如只標「眼部、臉頰」的眼妝重原則句）會被往後推出上限。
  5. need-rel-v3（2026-10-27，T4 兩輪 GAUS 判讀：M01、P04 的參考片段入池卻沒送進模型）：
     a. 命中「部位」但不是「嘴唇」（臉頰、腮紅、眼部…）也算往前提，每個 1 分（比條件／操作／用途的 2 分低）：
        P04 問「哪個好搭粉色腮紅」，同色系唇頰片段只標「部位：臉頰、腮紅」，被一排只命中冷底的片段擠出上限。
        「嘴唇」幾乎每段都有，仍不算。
     b. 使用者自述膚色（小麥色、白皙、深膚色…）時，片段「原文條件」寫到對應的英文膚色詞（tan、fair、deep…）加 2 分：
        M01「我是小麥色皮膚」，for tan skin 的片段排在池第 11、16，送不進模型。這只決定裝箱順序，
        不把小麥色說成 tan（規範 5）；回答時仍由模型依原文條件呈現。膚色不符不降權（不推定）。
     排序鍵：(條件不符, 是否往前提, −(強標籤分數＋部位分數), 角色排後, 原向量名次)。
片段原文、定位、去重都不動；這裡只決定裝箱順序，預算與上限仍由 retrieval_policy.pack_budget 套用。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .preferences import SKIN_DEGREE

NEED_REL_VERSION = "need-rel-v4"   # v4：膚色口語描述與英文比較級（S1）

_METHOD_Q = re.compile(r"怎麼|如何|怎樣|辦法|方法|步驟|擦法|畫法|塗法|技巧|要怎")
# 問題線索 → 片段屬性標籤值（support_scope 裡「鍵：值、值」的值）
_CUES: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"乾(?!燥玫瑰)|脫皮|龜裂"), ("乾唇", "保濕")),   # 「乾燥玫瑰」是色名，不是唇部乾燥
    (re.compile(r"煙燻|眼妝"), ("眼妝重", "眼部")),
    (re.compile(r"蓋不住|遮不住|蓋不過|遮不過|被吃掉|吃色|遮蓋"), ("遮蓋", "遮瑕")),
    (re.compile(r"試色|實際.{0,6}顏色|怎麼知道|如何知道|怎麼確認|如何確認"), ("試色",)),
    (re.compile(r"上班|辦公|通勤"), ("上班",)),
    (re.compile(r"晚上|夜晚|夜間|派對|晚宴"), ("夜晚",)),
    (re.compile(r"白天|日常"), ("白天日常",)),
    (re.compile(r"唇線"), ("唇線筆",)),
    (re.compile(r"持久|掉色|脫妝"), ("持久",)),
    (re.compile(r"腮紅|臉頰"), ("腮紅", "臉頰")),
    (re.compile(r"唇色|唇彩|口紅|唇膏|唇釉|嘴唇|紅唇|唇妝"), ("嘴唇",)),
]
_STRONG_KEYS = ("條件", "操作", "用途")
_PART_KEY = "部位"
_LATE_ROLES = frozenset({"研究觀察", "背景", "商品宣稱"})
_UNDERTONE_LABEL = {"cool": "冷底", "warm": "暖底", "neutral": "中性底"}
_COMMON_PART = "嘴唇"           # 幾乎每段都有，不當成往前提的訊號
_ORIGINAL_COND_KEY = "原文條件"
# need-rel-v3：使用者自述膚色 → 來源原文條件裡的英文膚色詞（只用來排序，不是等同）
# need-rel-v4（S1）：中文口語與詞表值共用 preferences 的程度詞（「膚色偏深」「皮膚很白」「膚色比較深」都對得到）；
# 英文加比較級（來源常寫 deeper／fairer skin tone）。「膚色偏黃」不對應任何英文詞（不推定 olive），只記錄。
_SKIN_DESC = r"(?:皮膚|膚色)" + SKIN_DEGREE
_SKIN_TERMS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"小麥"), ("tan",)),
    (re.compile(r"黝黑|深膚|深色|黑皮|很黑|" + _SKIN_DESC + r"[深黑]"), ("deep", "deeper", "dark", "darker")),
    (re.compile(r"白皙|白皮|淺膚|很白|" + _SKIN_DESC + r"[白淺]"), ("fair", "fairer", "light", "lighter")),
    (re.compile(r"中等膚色|膚色中等|中間膚色"), ("medium",)),
]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def scope_tags(support_scope: str | None) -> dict[str, set[str]]:
    """「部位：嘴唇、眼部；條件：眼妝重」→ {部位: {嘴唇, 眼部}, 條件: {眼妝重}}。"""
    out: dict[str, set[str]] = {}
    for part in (support_scope or "").split("；"):
        if "：" not in part:
            continue
        key, vals = part.split("：", 1)
        out.setdefault(key.strip(), set()).update(v.strip() for v in re.split(r"[、→]", vals) if v.strip())
    return out


def need_tags(question: str) -> list[str]:
    tags: list[str] = []
    for pat, vals in _CUES:
        if pat.search(question):
            tags += [v for v in vals if v not in tags]
    return tags


def _user_undertone(explicit_conditions: Any) -> str | None:
    for c in explicit_conditions or []:
        if isinstance(c, Mapping) and c.get("field") == "undertone" and c.get("value") in _UNDERTONE_LABEL:
            return _UNDERTONE_LABEL[c["value"]]
    return None


def _user_skin_terms(explicit_conditions: Any) -> list[str]:
    """need-rel-v3：使用者自述膚色（skin_tone）對應的來源英文膚色詞；沒有自述或對不到就回 []。"""
    terms: list[str] = []
    for c in explicit_conditions or []:
        if isinstance(c, Mapping) and c.get("field") == "skin_tone":
            said = f"{c.get('value') or ''} {c.get('raw') or ''}"
            for pat, words in _SKIN_TERMS:
                if pat.search(said):
                    terms += [w for w in words if w not in terms]
    return terms


def order_for_pack(question: str, explicit_conditions: Any, candidates: Sequence[Any]) -> tuple[list[Any], dict]:
    """回傳 (重排後的候選, 日誌)。候選物件本身不改（pool_rank 保留）。"""
    tags = need_tags(question)
    method = bool(_METHOD_Q.search(question))
    user_ut = _user_undertone(explicit_conditions)
    skin = _user_skin_terms(explicit_conditions)
    skin_re = re.compile(r"\b(?:" + "|".join(skin) + r")\b", re.I) if skin else None
    rows = []
    for c in candidates:
        meta = _get(c, "meta") or {}
        st = scope_tags(meta.get("support_scope"))
        strong = set().union(*(st.get(k, set()) for k in _STRONG_KEYS))
        strong_score = sum(2 for t in tags if t in strong)
        conflict = False
        uts = {v for v in st.get("條件", set()) if v in _UNDERTONE_LABEL.values()}
        if user_ut and uts:
            if user_ut in uts:
                strong_score += 2
            else:
                conflict = True
        skin_hit = bool(skin_re and any(skin_re.search(v) for v in st.get(_ORIGINAL_COND_KEY, set())))
        if skin_hit:
            strong_score += 2
        parts = st.get(_PART_KEY, set())
        part_score = sum(1 for t in tags if t in parts and t != _COMMON_PART)
        score = strong_score + sum(1 for t in tags if t in parts)
        late = 1 if (method and strong_score > 0 and meta.get("content_role") in _LATE_ROLES) else 0
        rows.append({"unit_id": _get(c, "unit_id"), "pool_rank": _get(c, "pool_rank"), "score": score,
                     "strong_score": strong_score, "part_score": part_score, "skin_match": skin_hit,
                     "late_role": late, "condition_conflict": conflict, "_c": c})
    rows.sort(key=lambda r: (r["condition_conflict"], 0 if r["strong_score"] + r["part_score"] > 0 else 1,
                             -(r["strong_score"] + r["part_score"]), r["late_role"], r["pool_rank"]))
    log = {"version": NEED_REL_VERSION, "need_tags": tags, "method_question": method, "user_undertone": user_ut,
           "user_skin_terms": skin, "order": [{k: v for k, v in r.items() if k != "_c"} for r in rows]}
    return [r["_c"] for r in rows], log
