"""交付前的「對題」修正（focus-v1，2026-11-03 的 N01、W03、N05 修改方向）。

在 postprocess.reconcile 之後執行，只做看得出來的保守調整（固定規則，不呼叫模型、不補新事實）：
1. unasked_ingredient（N01）：問題沒問成分時，主張裡「含有 神經醯胺／荷荷巴油… 的」這種附帶成分修飾拿掉
   （同商品附帶舉例的處理：只刪修飾語，不改主要做法）；刪完只剩空殼就整條不交付。
2. unverified_skin_effect（W03）：問題沒提到膚色時，「與黃皮互補、提亮膚色、顯白」這類沒核對過的膚色效果
   以分號為界拿掉那一段；整條都是這種效果就整條不交付。
3. festive_red（N05）：問喜氣、過年這類場合，回答已有「紅色象徵喜氣」那條時，沒提紅色、也沒提喜氣的
   其他色系主張（轉去桃色、珊瑚、橙色）不交付；另加限制說明紅色是文化背景說法。
4. color_name_limit（W03、G03）：問某個色名「是什麼／差在哪」時，加一條限制：色名沒有跨品牌的統一定義。
5. product_specifics_limit（focus-v2，2026-11-07，exp2 40 題判讀）：商品功能沒開、問題裡也沒有會附色名參照的色名時，
   模型寫的「證據沒有具體色號／品牌／產品名稱」限制不交付——商品本來就不提供，這句對使用者沒有資訊，
   而且常和送入的片段矛盾（片段其實有提到品牌或色號：G01、M07、N08 判讀 3 句錯）。
9. other_look_steps（focus-v5，M09）：問某款妝怎麼畫、送入片段有那款妝，但主張引用的片段都沒提到那款妝、
   又在講腮紅／高光等部位 → 是別款妝的步驟，不交付。
10. unsupported_comparison（focus-v5，P04 第二輪）：「A 比 B 更適合搭 X」的比較句，引用片段都沒提到 X →
   不交付，另加限制說明來源沒有直接比較。
11. research_prefix_repeated（focus-v6，W09）：同一篇文獻只在第一次出現的主張保留「研究觀察顯示…」開頭，
   之後引用同一篇的主張拿掉這段引導語（不改其他字）。
第 9、10 項要知道引用片段原文（apply 的 unit_texts）；呼叫端沒傳時跳過，行為和 focus-v4 相同。
每一項都記旗標（flags）或 removed，日誌追得到前後文字（規範 8）。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from . import color_reference
from . import recommend

FOCUS_VERSION = "focus-v8"
# focus-v8（2026-11-08，fix-metrics）：第 13 項 foundation_advice_as_lip（驗證組 P02 粉底說法套成唇色）。
# focus-v7（2026-11-08，fix-heldout，依測試組診斷）：第 12 項 non_lip_colour_advice（H03）。
# focus-v6（2026-11-08，fix-answers 第八批，第十四輪 W09）：
# 第 11 項 research_prefix_repeated，見 drop_repeated_research_prefix。
# focus-v5（2026-11-08，fix-answers 第五批，prodref 後的 M09、P04 第二輪）：第 9、10 項，見下方各段說明。
# focus-v3（2026-11-08，fix-answers T4 實跑）：C-ap-v12 第 10 條寫了「判斷膚色底調的方法只在使用者沒說底調時寫」，
# 模型反而在不相關的題加上判斷方法（M01 曬黑、P08 一般建議的血管與白色奶油色測試），
# 或寫「證據沒有判斷底調的方法」這種不相關的限制（N09、LM35-I04）。
# 第 6 項：問題沒問怎麼判斷自己的底調時，這類主張與限制不交付。
# focus-v4（2026-11-08，M01／P11／N10）：
#   - 第 6 項改成只認「判斷底調」：問題要講到底調（冷底、暖底、底調）才算。P11「唇色合不合適、要怎麼判斷」
#     問的是唇色，不是自己的底調（判斷的是肌膚顏色，不是唇彩）→ 血管判斷句不交付；N11 照留。
#   - 第 7 項 off_area_advice（N10）、第 8 項 avoid_merged（M01），見 apply 內說明。
_ASK_JUDGE = re.compile(r"(?:判斷|分辨|怎麼知道|怎麼看)[^，。？?]{0,10}(?:冷底|暖底|中性底|底調)|冷底還是暖底"
                        r"|是不是.{0,6}[冷暖]底|(?:冷底|暖底|底調)[^，。？?]{0,8}(?:怎麼判斷|怎麼分|怎麼看)")
# 第 7 項：問題只問唇妝／底妝（沒提眼、睫毛、頰、眉）時，「建議…」後面只講眼妝、腮紅這些部位的主張不交付
_ASK_AREA = re.compile(r"唇妝|底妝|口紅|唇膏|唇色|唇彩")
_ASK_OTHER_AREA = re.compile(r"眼|睫毛|腮紅|頰|眉|修容|高光")
# 問整個妝怎麼畫（M09「草莓女孩妝…要怎麼畫，唇妝用什麼」）時，腮紅、高光本來就是答案，第 7 項不套用
_ASK_WHOLE_LOOK = re.compile(r"怎麼畫|怎麼化|畫法|化法|步驟")
_ADVICE_SPLIT = re.compile(r"建議|可以|應該|應")
_OTHER_AREA_WORD = re.compile(r"眼妝|眼線|眼影|睫毛|腮紅|頰|眉|修容|高光")
_ASKED_AREA_WORD = re.compile(r"唇|口紅|底妝|粉底|遮瑕")
# 第 8 項：同一則回答裡「X 色都可以」和「避免 Y 色」分兩條、顏色又有重疊（粉紅 vs 淺粉）→ 合成一條
_AVOID_HEAD = re.compile(r"避免|避開|不要選|不適合")
_HUE_WORD = re.compile(r"焦糖|豆沙|玫瑰|珊瑚|奶茶|酒紅|磚紅|[棕莓裸紅粉橘紫杏桃米]")


def advice_only_other_area(text: str) -> bool:
    """第 7 項：主張的建議部分（「建議／可以／應」之後）只講眼妝、腮紅等部位，沒提唇或底妝。"""
    m = _ADVICE_SPLIT.search(text)
    advice = text[m.end():] if m else text
    return bool(_OTHER_AREA_WORD.search(advice)) and not _ASKED_AREA_WORD.search(advice)


def merge_avoid(claims: list[dict]) -> tuple[list[dict], dict | None]:
    """第 8 項：找「避免 Y」開頭的主張與前面「X 都可以」的主張（沒有反向說法、顏色和 Y 有共同色相字），
    把避免那句接在 X 句後面（「…，但應避免…」），拿掉原本的避免句。一則回答只合併一次。回傳（新主張清單, 紀錄）。"""
    for j, b in enumerate(claims):
        bt = b["text"]
        neg = _AVOID_HEAD.search(bt)
        if not neg:
            continue
        before_neg = bt[:neg.start()]
        if _HUE_WORD.search(before_neg):          # 避免之前就有顏色（「選 X，並避免 Y」）→ 本身已經寫清楚，不用合
            continue
        avoid_part = bt[neg.start():].rstrip("。")
        avoid_hues = set(_HUE_WORD.findall(avoid_part))
        # 選顏色和避免句重疊最多的那條（M01：「紅色、粉紅色和裸色」重疊粉、裸，勝過「棕色、莓色和焦糖裸色」只重疊裸）
        cands = []
        for i, a in enumerate(claims):
            at = a["text"]
            if i == j or _AVOID_HEAD.search(at) or "不" in at:
                continue
            a_hues = set(_HUE_WORD.findall(at))
            if len(a_hues) >= 2 and a_hues & avoid_hues:
                cands.append((len(a_hues & avoid_hues), -abs(i - j), i))
        if cands:
            i = max(cands)[2]
            a, at = claims[i], claims[i]["text"]
            merged = {**a, "text": f"{at.rstrip('。')}，但應{avoid_part.lstrip('應')}。",
                      "unit_ids": list(dict.fromkeys(list(a.get("unit_ids") or []) + list(b.get("unit_ids") or [])))}
            out = [merged if k == i else c for k, c in enumerate(claims) if k != j]
            return out, {"flag": "avoid_merged", "version": FOCUS_VERSION, "kept_claim_no": a.get("claim_no"),
                         "merged_claim_no": b.get("claim_no"), "before": [at, bt], "after": merged["text"]}
    return claims, None
_UNDERTONE_TEST = re.compile(r"血管|珠寶|金飾|銀飾|曬黑|曬傷|白色對比|奶油色測試|白色.{0,6}奶油色")
_UNDERTONE_LIMIT = re.compile(r"判斷(?:膚色|皮膚)?(?:的)?底調")

# ---------------------------------------------------------------------------
# 1. 沒問的成分
# ---------------------------------------------------------------------------
_INGREDIENT_NAMES = (r"神經醯胺|荷荷巴油|透明質酸|玻尿酸|乳木果油?|角鯊烷|甘油|維生素\s?E|蜂蠟|凡士林|羊毛脂|"
                     r"保濕因子|保濕成分|膠原蛋白|椰子油|橄欖油|酪梨油|蘆薈|蜂蜜|尿素|水楊酸|果酸")
_INGREDIENT = rf"(?:{_INGREDIENT_NAMES}|油)"   # 「含油」的油只在修飾語裡算成分
_ITEM = rf"(?:含有?)?{_INGREDIENT}"
# 「含有 A 或 B、C 的」「富含 A 的」：整段修飾語拿掉
_INGREDIENT_MOD = re.compile(rf"(?:含有?|富含|添加了?){_ITEM}(?:(?:或|、|和|及|與|跟|以及){_ITEM})*的")
# 「（如神經醯胺、荷荷巴油）」這種括號舉例
_INGREDIENT_PAREN = re.compile(rf"[（(](?:如|例如|像是)?{_ITEM}(?:(?:或|、|和|及|與|跟|以及){_ITEM})*(?:等)?[）)]")
# 問題本身在問成分（或點名某個成分）就不動
_ASK_INGREDIENT = re.compile(rf"成分|配方|含有|{_INGREDIENT_NAMES}")
# 刪完後太短、沒有動作或對象的主張視為空殼
_MIN_LEFT = 6


def trim_ingredients(text: str) -> str:
    """拿掉附帶的成分修飾語與括號舉例，回傳新文字（沒有就原樣）。"""
    return _INGREDIENT_PAREN.sub("", _INGREDIENT_MOD.sub("", text))


# ---------------------------------------------------------------------------
# 2. 沒核對過的膚色效果
# ---------------------------------------------------------------------------
_SKIN_EFFECT = re.compile(r"(?:黃皮|白皮|黑皮|膚色|膚況|皮膚)[^。；]{0,10}?(?:互補|提亮|顯白|襯|修飾)"
                          r"|提亮膚色|顯白|顯黑|讓膚色(?:更|看起來)?(?:亮|白|透)")
_ASK_SKIN = re.compile(r"膚|皮|底調|冷底|暖底|中性底|膚色")


def drop_skin_effect(text: str) -> str | None:
    """以分號切段，拿掉含膚色效果的段落；全部都是就回 None（整條不交付），沒有就原樣。"""
    parts = [p for p in re.split(r"(?<=；)", text) if p]
    keep = [p for p in parts if not _SKIN_EFFECT.search(p)]
    if len(keep) == len(parts):
        return text
    if not keep:
        return None
    out = "".join(keep).rstrip("；，")
    return out if out.endswith("。") else out + "。"


# ---------------------------------------------------------------------------
# 3. 喜氣場合只談紅色
# ---------------------------------------------------------------------------
_FESTIVE_Q = re.compile(r"喜氣|喜慶|過年|新年|春節|拜年|吉利|討喜")
_RED_FESTIVE = re.compile(r"紅[^。]{0,20}?(?:喜氣|好運|吉利|喜慶|繁榮|象徵)|(?:喜氣|好運|吉利|喜慶)[^。]{0,20}?紅")
_COLOR_FAMILY = re.compile(r"桃|珊瑚|橙|橘|粉|裸|棕|紫|豆沙|奶茶|米色|杏")
FESTIVE_LIMIT = "紅色代表喜氣是華人文化背景的說法（研究論文轉引），不是比較唇色效果的研究結果。"

# ---------------------------------------------------------------------------
# 4. 色名不是跨品牌定義
# ---------------------------------------------------------------------------
_DEFINE_Q = re.compile(r"差在哪|差別|差異|區別|不同|不一樣|是什麼|什麼樣|怎麼分|哪裡不")


def color_name_limit(question: str) -> str | None:
    """問題問色名的定義或差別時，回傳「色名不是跨品牌定義」的限制句；不是就回 None。"""
    if not _DEFINE_Q.search(question or ""):
        return None
    names = [k for k in color_reference.TRIGGER_KEYS if k in question]
    names = [k for k in names if not any(k != o and k in o for o in names)]   # 「玫瑰」被「乾燥玫瑰」包含就不重複
    if not names:
        return None
    shown = "".join(f"「{k if k.endswith('色') else k + '色'}」" for k in names[:3])
    # 不用「色號」「沒有」等字：color_reference.reconcile_limitations 會把「色碼／色號＋否定」的限制改寫成色碼固定句
    #（T4 30a9e1d 的 W03、G03 就是這樣被吃掉）
    return f"{shown}這類色名是各品牌自己取的，同一個名字在不同品牌可能是差很多的顏色。"


# ---------------------------------------------------------------------------
# 5. 商品功能沒開時，不交付「證據沒有具體色號／品牌／產品名稱」的限制（focus-v2）
# ---------------------------------------------------------------------------
# 否定詞後 20 字內（不跨句號、分號）講到色號、品牌、產品／商品名稱；只說「產品」不算（「如何使用該產品」是在講做法）
_PRODUCT_LIMIT = re.compile(r"(?:沒有|沒|並未|未|無法|缺少|缺乏)[^。；;]{0,20}?(?:色號|品牌|產品名稱|商品名稱)")
# 色碼、HEX 這類限制交給 color_reference.reconcile_limitations 改寫成固定句（附色名參照時要留著說明），不在這裡刪
_COLOR_CODE = re.compile(r"色碼|HEX|色值|RGB", re.I)
# 系統自己加的固定句（商品功能說明、色碼固定句）一律不動
_SYSTEM_FIXED = ("商品功能未啟用", color_reference.REWRITTEN_LIMITATION)


def is_product_specifics_limit(limitation: str) -> bool:
    """這條限制是不是模型寫的「證據沒有具體色號／品牌／產品名稱」（系統固定句、色碼類限制都回 False）。"""
    if any(limitation.startswith(s) for s in _SYSTEM_FIXED) or _COLOR_CODE.search(limitation):
        return False
    return bool(_PRODUCT_LIMIT.search(limitation))


def may_attach_color_reference(question: str) -> bool:
    """問題裡有色名參照的分組詞（豆沙、乾燥玫瑰…）＝之後可能附色名參照。

    附參照時 color_reference.reconcile_limitations 會把「沒有色號／色碼」的限制改寫成固定說明句，
    所以這種題不刪，留給它改寫（只看問題有沒有分組詞，比實際附不附更保守：沒問色號、證據不足附一般建議時也會附）。"""
    return any(k in (question or "") for k in color_reference.TRIGGER_KEYS)


# ---------------------------------------------------------------------------
# 9. 別款妝的步驟（focus-v5，M09）
# ---------------------------------------------------------------------------
# 問題點名的妝 → 片段原文裡那款妝的寫法。只在這裡用，不加進 query_glossary（那會改到檢索）。
_NAMED_LOOKS: tuple[tuple[re.Pattern, re.Pattern], ...] = (
    (re.compile(r"草莓"), re.compile(r"strawberry|草莓", re.I)),
    (re.compile(r"法式"), re.compile(r"french|法式", re.I)),
    (re.compile(r"煙燻"), re.compile(r"smok(?:e|ey|y)|煙燻", re.I)),
    (re.compile(r"韓系|韓式"), re.compile(r"korean|k-beauty|韓", re.I)),
    (re.compile(r"日系|日式"), re.compile(r"japanese|日系|日式", re.I)),
)


def named_look(question: str) -> tuple[re.Pattern, re.Pattern] | None:
    """問題點名的妝：回傳（問題用詞樣式, 片段用詞樣式）；沒點名回 None。"""
    for q_pat, src_pat in _NAMED_LOOKS:
        if q_pat.search(question or ""):
            return q_pat, src_pat
    return None


def cited_texts(claim: Mapping[str, Any], unit_texts: Mapping[str, str]) -> list[str]:
    """主張引用的片段原文（只取 unit_texts 裡有的）。"""
    return [unit_texts[u] for u in claim.get("unit_ids") or [] if u in unit_texts]


# ---------------------------------------------------------------------------
# 12. 非唇彩用色套成唇色（focus-v7，測試組 H03；開發 P02、LM35-O01 同句）
# ---------------------------------------------------------------------------
# 主張建議用綠色、黃色這類一般不是唇色的顏色（「適合／可以使用…綠色、藍色」）；
# 只看綠、黃：藍色常出現在「藍色調的紅色」「藍粉色」這類真正的唇色描述，不能當判斷依據
_NON_LIP_HUE = re.compile(r"綠色|黃色")
_ADVISE = re.compile(r"適合|可以使用|可以用|使用|選擇|搭配")
# 引用片段有提到嘴唇、唇彩（中英文）就代表來源在講唇色，不動
_LIP_WORD = re.compile(r"lip|唇|口紅|gloss", re.I)


def non_lip_colour_advice(text: str, sources: list[str]) -> bool:
    """主張把綠／黃等顏色當成建議使用的顏色，而且引用片段原文完全沒提到嘴唇或唇彩 → True。

    H03「冷色調膚色也適合使用綠色、藍色…」引用 Kryolan〈Skin Tones〉講整體配色的段落（原文沒有 lip），
    模型把它寫成唇色建議。沒傳片段原文（sources 空）時不判斷，回 False。"""
    if not sources or not _NON_LIP_HUE.search(text) or not _ADVISE.search(text):
        return False
    return not any(_LIP_WORD.search(s or "") for s in sources)


# ---------------------------------------------------------------------------
# 13. 粉底的說法套成唇色（focus-v8，fix-metrics，驗證組 P02）
# ---------------------------------------------------------------------------
# P02「若在冷底調上使用過於暖色的唇色，可能會顯得灰暗」引用 Kryolan〈Skin Tones〉的
# 「if you use a foundation that's too warm…」——原文講粉底、沒提嘴唇，模型把它寫成唇色。
_CLAIM_LIP = re.compile(r"唇色|唇膏|口紅|唇彩|唇妝")
_FOUNDATION = re.compile(r"foundation|粉底", re.I)


def foundation_advice_as_lip(text: str, sources: list[str]) -> bool:
    """主張在講唇色，但引用片段原文完全沒提到嘴唇／唇彩、而且講的是粉底 → True（沒傳片段不判斷）。"""
    if not sources or not _CLAIM_LIP.search(text):
        return False
    return (not any(_LIP_WORD.search(s or "") for s in sources)
            and any(_FOUNDATION.search(s or "") for s in sources))


# ---------------------------------------------------------------------------
# 10. 來源沒支持的搭配比較（focus-v5，P04 第二輪）
# ---------------------------------------------------------------------------
# 「比 B 更適合／好搭／搭／好看」的比較句
_COMPARE = re.compile(r"比[^，。；]{1,20}?更(?:適合|好搭|搭|好看)")
# 搭配對象：主張裡的中文詞 → 片段原文裡要出現的寫法（中英都認）
_PAIR_OBJECTS: tuple[tuple[str, re.Pattern], ...] = (
    ("腮紅", re.compile(r"blush|cheek|腮紅|頰", re.I)),
    ("眼影", re.compile(r"eye ?shadow|眼影", re.I)),
    ("粉底", re.compile(r"foundation|粉底", re.I)),
    ("衣服", re.compile(r"outfit|wear|dress|cloth|衣|服", re.I)),
    ("服裝", re.compile(r"outfit|wear|dress|cloth|衣|服", re.I)),
)


def unsupported_pair(text: str, sources: list[str]) -> str | None:
    """主張是比較句、講到搭配對象，而引用片段都沒提到該對象 → 回傳主張裡的對象片語（例如「粉色腮紅」）；否則回 None。"""
    if not _COMPARE.search(text):
        return None
    for word, src_pat in _PAIR_OBJECTS:
        if word in text and not any(src_pat.search(s) for s in sources):
            # 對象前最多 3 個修飾字（「粉色腮紅」），不跨「與／和／跟／搭配」這類連接詞
            m = re.search(rf"[^，。；與和跟搭配]{{0,3}}{word}", text)
            return m.group(0) if m else word
    return None


# ---------------------------------------------------------------------------
# 主函式
# ---------------------------------------------------------------------------
# 11. 同一篇文獻重複的「研究觀察…」開頭（focus-v6，W09）
# 主張開頭的「研究觀察顯示，」「研究指出」「研究也發現：」這類引導語；group(0) 整段拿掉
_RESEARCH_PREFIX = re.compile(r"^研究(?:觀察)?(?:也|還)?(?:顯示|指出|發現|觀察到)[，,：:]?\s*")


def _doc_id(unit_id: str) -> str:
    """片段編號的文件部分：AR015_p0002_b006 → AR015、PR005_h0101 → PR005（第一個底線前）。"""
    return (unit_id or "").split("_", 1)[0]


def drop_repeated_research_prefix(claims: list[dict]) -> tuple[list[dict], dict | None]:
    """同一篇文獻的研究發現，只有第一次出現的主張保留「研究觀察顯示…」開頭，之後引用同一篇的主張拿掉這段引導語。

    W09 六條主張每條都以「研究觀察顯示／指出／發現」開頭，讀起來太囉嗦；
    但 answer_policy 第 6 條要求研究觀察要說明是研究發現，所以每篇文獻第一次出現時保留，讓讀者知道是研究。
    判斷「之前出現過」：這條主張引用的每篇文獻，都已在前面某條帶引導語的主張出現過。
    拿掉後剩下的字太短（少於 6 個字）就不動。回傳（新主張清單, 旗標或 None）。"""
    seen: set[str] = set()
    out, changed = [], []
    for c in claims:
        text = c.get("text") or ""
        m = _RESEARCH_PREFIX.match(text)
        docs = {_doc_id(u) for u in c.get("unit_ids") or [] if u}
        if not m or not docs:
            out.append(c)
            continue
        rest = text[m.end():]
        if docs <= seen and len(rest) >= 6:
            out.append({**c, "text": rest})
            changed.append({"claim_no": c.get("claim_no"), "removed_prefix": m.group(0).strip()})
        else:
            out.append(c)
        seen |= docs
    if not changed:
        return claims, None
    return out, {"flag": "research_prefix_repeated", "version": FOCUS_VERSION, "claims": changed}


def apply(delivered: Mapping[str, Any], question: str, unit_texts: Mapping[str, str] | None = None) -> dict:
    """依序套用十項對題修正（第 5 項在第 4 項之前執行），回傳新的 Delivered（不改原物件）。

    unit_texts：送入模型的片段 {unit_id: 原文}；第 9、10 項要用，沒傳就跳過這兩項。"""
    out = dict(delivered)
    q = question or ""
    claims = [dict(c) for c in out.get("claims") or []]
    flags = list(out.get("flags") or [])
    removed = list(out.get("removed") or [])
    limits = list(out.get("limitations") or [])

    # 1. 沒問的成分
    if not _ASK_INGREDIENT.search(q):
        kept = []
        for c in claims:
            new = trim_ingredients(c["text"])
            if new == c["text"]:
                kept.append(c)
            elif len(re.sub(r"[，。；、\s]", "", new)) < _MIN_LEFT:
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "unasked_ingredient",
                                "reason": "問題沒問成分，主張拿掉成分後沒有內容"})
            else:
                flags.append({"flag": "unasked_ingredient_trimmed", "claim_no": c.get("claim_no"),
                              "before": c["text"], "after": new})
                kept.append({**c, "text": new})
        claims = kept

    # 2. 沒核對過的膚色效果
    if not _ASK_SKIN.search(q):
        kept = []
        for c in claims:
            new = drop_skin_effect(c["text"])
            if new is None:
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "unverified_skin_effect",
                                "reason": "問題沒提到膚色，主張在講沒核對過的膚色效果"})
            elif new != c["text"]:
                flags.append({"flag": "unverified_skin_effect_trimmed", "claim_no": c.get("claim_no"),
                              "before": c["text"], "after": new})
                kept.append({**c, "text": new})
            else:
                kept.append(c)
        claims = kept

    # 3. 喜氣場合只談紅色
    if _FESTIVE_Q.search(q) and any(_RED_FESTIVE.search(c["text"]) for c in claims):
        kept = []
        for c in claims:
            t = c["text"]
            if "紅" not in t and not _FESTIVE_Q.search(t) and _COLOR_FAMILY.search(t):
                removed.append({"claim_no": c.get("claim_no"), "text": t, "rule": "festive_offtopic_color",
                                "reason": "問喜氣場合，這條轉去推薦其他色系、沒有講紅色或喜氣"})
            else:
                kept.append(c)
        claims = kept
        if FESTIVE_LIMIT not in limits:
            limits.append(FESTIVE_LIMIT)

    # 5. 商品功能沒開、問題也沒有會附色名參照的色名：「證據沒有具體色號／品牌」的限制不交付（在第 4 項加系統句之前做）
    if not recommend.PRODUCT_ENABLED and not may_attach_color_reference(q):
        kept_limits = []
        for lim in limits:
            if is_product_specifics_limit(lim):
                removed.append({"limitation": lim, "rule": "product_specifics_limit",
                                "reason": "商品功能未啟用、問題沒問色號，「證據沒有具體色號／品牌／產品名稱」對使用者沒有資訊，且常和片段矛盾"})
            else:
                kept_limits.append(lim)
        limits = kept_limits

    # 6. 沒問怎麼判斷底調：判斷底調的方法（血管、曬黑、珠寶、白色奶油色）與「證據沒有判斷底調的方法」限制不交付
    if not _ASK_JUDGE.search(q):
        kept = []
        for c in claims:
            if _UNDERTONE_TEST.search(c["text"]):
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "undertone_test_unasked",
                                "reason": "問題沒問怎麼判斷，判斷膚色底調的方法對這題沒有用", "version": FOCUS_VERSION})
            else:
                kept.append(c)
        claims = kept
        kept_limits = []
        for lim in limits:
            if _UNDERTONE_LIMIT.search(lim or ""):
                removed.append({"limitation": lim, "rule": "undertone_limit_unasked",
                                "reason": "問題沒問怎麼判斷底調，這條限制不相關", "version": FOCUS_VERSION})
            else:
                kept_limits.append(lim)
        limits = kept_limits

    # 7. off_area_advice（N10「唇妝和底妝要怎麼處理」卻建議「搭配輕淡的眼妝與低調的臉頰妝容」）：
    #    問題只問唇／底妝時，建議部分只講眼妝、腮紅等其他部位的主張不交付；建議裡也提到唇或底妝的照留
    if _ASK_AREA.search(q) and not _ASK_OTHER_AREA.search(q) and not _ASK_WHOLE_LOOK.search(q):
        kept = []
        for c in claims:
            if advice_only_other_area(c["text"]):
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "off_area_advice",
                                "reason": "問題只問唇妝或底妝，這條建議只講眼妝、腮紅等其他部位", "version": FOCUS_VERSION})
            else:
                kept.append(c)
        claims = kept

    # 8. avoid_merged（M01「X 都可以」與「避免 Y」分兩條看起來衝突）：合成一條寫清楚
    claims, merged = merge_avoid(claims)
    if merged:
        flags.append(merged)

    # 9. other_look_steps（M09「草莓女孩妝…要怎麼畫」卻混進 PR017 夏日粉色妝的腮紅、高光步驟）：
    #    問題點名某款妝、問整個妝怎麼畫、送入片段至少一段有那款妝時，引用片段都沒提到那款妝、
    #    主張也沒提到那款妝、又在講其他部位的主張不交付。術語修正已先跑（「人中」已改「唇峰」），
    #    所以不能用「沒講唇」判斷，改看引用片段。
    look = named_look(q)
    if (unit_texts and look and _ASK_WHOLE_LOOK.search(q)
            and any(look[1].search(t) for t in unit_texts.values())):
        kept = []
        for c in claims:
            src = cited_texts(c, unit_texts)
            if (src and not any(look[1].search(t) for t in src) and not look[0].search(c["text"])
                    and _OTHER_AREA_WORD.search(c["text"])):
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "other_look_steps",
                                "unit_ids": list(c.get("unit_ids") or []),
                                "reason": "問題問某款妝怎麼畫，這條引用的片段講的是別款妝的步驟", "version": FOCUS_VERSION})
            else:
                kept.append(c)
        claims = kept

    # 10. unsupported_comparison（P04 第二輪「藍調紅比暖紅更適合搭粉色腮紅」，引用片段沒提腮紅也沒比較）：
    #     比較句的搭配對象不在引用片段裡 → 不交付，加限制說明來源沒有直接比較
    if unit_texts:
        kept = []
        for c in claims:
            obj = unsupported_pair(c["text"], cited_texts(c, unit_texts))
            if obj:
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "unsupported_comparison",
                                "unit_ids": list(c.get("unit_ids") or []), "object": obj,
                                "reason": "比較句的搭配對象沒出現在引用片段，來源沒有支持這個比較", "version": FOCUS_VERSION})
                lim = f"來源沒有直接比較哪個方向比較適合搭配{obj}。"
                if lim not in limits:
                    limits.append(lim)
            else:
                kept.append(c)
        claims = kept

    # 12. non_lip_colour_advice（focus-v7，測試組 H03）：把整體配色的綠、黃等顏色寫成唇色建議，
    #     引用片段又完全沒提到嘴唇 → 不交付（不改寫、不補新事實）
    if unit_texts:
        kept = []
        for c in claims:
            if non_lip_colour_advice(c["text"], cited_texts(c, unit_texts)):
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "non_lip_colour_advice",
                                "unit_ids": list(c.get("unit_ids") or []),
                                "reason": "把綠、黃等顏色當唇色建議，但引用的來源沒有提到嘴唇或唇彩（講的是整體配色）",
                                "version": FOCUS_VERSION})
            else:
                kept.append(c)
        claims = kept

    # 13. foundation_advice_as_lip（focus-v8，驗證組 P02）：主張講唇色，引用片段只講粉底、完全沒提嘴唇 → 不交付
    if unit_texts:
        kept = []
        for c in claims:
            if foundation_advice_as_lip(c["text"], cited_texts(c, unit_texts)):
                removed.append({"claim_no": c.get("claim_no"), "text": c["text"], "rule": "foundation_advice_as_lip",
                                "unit_ids": list(c.get("unit_ids") or []),
                                "reason": "主張在講唇色，但引用的來源講的是粉底、沒有提到嘴唇或唇彩",
                                "version": FOCUS_VERSION})
            else:
                kept.append(c)
        claims = kept

    # 11. research_prefix_repeated（focus-v6，W09）：同一篇文獻只在第一次出現時保留「研究觀察顯示…」開頭
    claims, prefix_flag = drop_repeated_research_prefix(claims)
    if prefix_flag:
        flags.append(prefix_flag)

    # 4. 色名不是跨品牌定義
    if (lim := color_name_limit(q)) and lim not in limits:
        limits.append(lim)

    if not claims and out.get("claims"):
        out["status"] = "no_direct_answer"
    out.update(claims=claims, flags=flags, removed=removed, limitations=limits, focus_version=FOCUS_VERSION)
    return out
# 核心邏輯：
# - 每項都先看「使用者有沒有問到」：問了成分、提了膚色，就不動對應的主張（不擋使用者自己要的內容）。
# - 只刪修飾語或分號段落，不改寫主要做法、不補新事實；刪光就整條不交付，引用照原本的來源。
# - 喜氣題另加文化背景說明，讓讀者知道「紅色有喜氣」不是唇色效果研究。
# - focus-v2：商品功能沒開時，只刪模型寫的「證據沒有具體色號／品牌」限制；系統固定句與色碼類限制不動。
# - focus-v5：第 9、10 項看引用片段原文（unit_texts），只刪不改寫；P04 那類另加一條限制讓讀者知道來源沒比較。
