"""來源支持單位（support unit，contract-v0 support_unit）：由 05 匯出的 rag_ready 片段衍生，給 B（檢索／選段）與 D（匯入）用。

一個單位＝一個既有 chunk（unit_id 沿用 block_id）；本版不切子範圍（#r<n> 留給確實混接的片段）。
每個單位保留完整原文與 SHA、主要／第二定位、原章節、前後／接續／佐證連接；另加：

- search_text：05 的搜尋文字＋「本單位原文自己出現的」用途／部位／操作／條件／色名之中英通用術語。
  術語表固定一份（LEXICON，版本與 SHA 記在 manifest），全庫每個片段一致套用，不看題目、不看評測錨點，
  不含題號或標準答案。只翻譯原文出現的詞，不從原文推論出原文沒寫的東西：
  tan／deep 等膚色詞不翻成小麥色等使用者描述（2026-10-22 M01 tan＝小麥色錯配），pout／slip 不翻。
- conditions：原文明示的適用條件，逐字截取原句片段（沒有就是 []），不改寫、不推論。
- product_mentions：原文的品牌／商品指涉（™®、品牌名、「by 品牌」），只供回答端以完整主張處理，不用來刪證據。
- content_role、support_scope：規則判讀（rule 版本記在 manifest），可為 null；是 AI 規則結果，不是人工核准。
- eligibility：一律 pending——ETL 可匯出、清洗通過都不等於人工核准（規範 4）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

SCHEMA = "support-unit-v0"
RULES_VERSION = "support-rules-v2"
SPLIT_VERSION = "multi_scenario_split-v3"  # v3：通則範圍、承接詞才併入、or 替代分支標記、縮寫不斷句（E-030）


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 術語表：(facet, 比對原文的 regex, 加入搜尋文字的中英通用術語)
# facet：部位／操作／用途／條件／色名。比對不分大小寫；只有原文命中才加。
# ---------------------------------------------------------------------------
LEXICON: list[tuple[str, str, tuple[str, ...]]] = [
    # 部位
    ("部位", r"\blips?\b|唇(?!膏|蜜|彩|線筆)", ("lips", "嘴唇")),
    ("部位", r"\blip ?line\b|\blip edges?\b|\bouter edges? of (?:your|the) lips?\b|唇線|唇緣", ("lip line", "唇線")),
    ("部位", r"\bcupid'?’?s bow\b|唇峰", ("cupid's bow", "唇峰")),
    ("部位", r"\bcent(?:er|re) of (?:your |the )?(?:lips?|mouth)\b|唇中央|唇中間", ("center of lips", "唇中央")),
    ("部位", r"\beyes?\b|\beyelids?\b|眼(?!鏡)", ("eyes", "眼部")),
    ("部位", r"\bcheeks?\b|\bblush\b|腮紅|臉頰", ("cheeks", "臉頰", "腮紅")),
    # 操作
    ("操作", r"\bexfoliat\w*|\bscrub\b|去角質", ("exfoliate", "去角質")),
    ("操作", r"\bprim(?:e|er|ing)\b|打底", ("primer", "打底")),
    ("操作", r"\bconceal\w*|\bcover[- ]?stick\b|遮瑕", ("concealer", "遮瑕")),
    ("操作", r"\bfoundation\b|粉底", ("foundation", "粉底")),
    ("操作", r"\b(?:lip ?)?liner\b|\bline (?:your|the) lips\b|唇線筆|描唇線", ("lip liner", "唇線筆")),
    ("操作", r"\bblot\w*|\bpress\w*|\bdab\w*|\bpat\b|按壓|輕拍", ("blot", "按壓")),
    ("操作", r"\blayer\w*|疊擦|疊色", ("layering", "疊擦")),
    ("操作", r"\bblend\w*|暈染|推勻", ("blend", "暈染")),
    ("操作", r"\bswatch\w*|\btest\w* (?:it |them |the \w+ )?on (?:your |the )?(?:arm|wrist|hand|lips?)\b|試色",
     ("swatch", "試色")),
    ("操作", r"\bthin (?:layer|coat)s?\b|\blet (?:it )?dry\b|\bbuild (?:it )?up\b|薄擦|待乾|再補",
     ("thin layers", "薄擦後再疊")),
    ("操作", r"\bmoisturi[sz]\w*|\bhydrat\w*|\bbalm\b|保濕|護唇", ("moisturize", "保濕")),
    # 用途
    ("用途", r"\bcover(?:age|s|ed|ing)?\b|\bneutrali[sz]\w*|\bcancel(?:s|ing)? out\b|遮蓋|蓋住|中和", ("cover", "遮蓋")),
    ("用途", r"\blong[- ]?(?:lasting|wear\w*)\b|\bstay(?:s|ing)? (?:put|power)\b|持久|持妝", ("long-lasting", "持久")),
    ("用途", r"\bdefin\w*|\boutlin\w*|勾勒|輪廓", ("define", "勾勒")),
    # 條件
    ("條件", r"\bcool(?:-toned)? (?:skin|undertones?|complexions?)\b|\bcool undertone|冷(?:色)?(?:調|底)", ("cool undertone", "冷底")),
    ("條件", r"\bwarm(?:-toned)? (?:skin|undertones?|complexions?)\b|\bwarm undertone|暖(?:色)?(?:調|底)", ("warm undertone", "暖底")),
    ("條件", r"\bneutral (?:skin|undertones?)\b|中性(?:色)?(?:調|底)", ("neutral undertone", "中性底")),
    ("條件", r"\b(?:dark|pigmented|deep[- ]colou?red|darker) lips?\b|\blip pigment\w*|唇色(?:較|偏)?深", ("pigmented lips", "唇色深")),
    ("條件", r"\bdry lips?\b|\bchapped\b|\bflak\w*|乾唇|脫皮|乾裂", ("dry lips", "乾唇")),
    ("條件", r"\b(?:heavy|bold|dramatic|smok(?:y|ey)) (?:eye|eyes|eye ?makeup|eye ?shadow|eyeliner)\b|眼妝(?:很|較)?重|煙燻",
     ("heavy eye makeup", "眼妝重")),
    ("條件", r"\boffice\b|\b(?:at|for|to) work\b|\bworkplace\b|\bworkday\b|\bwork (?:looks?|makeup|meetings?)\b|"
     r"上班|辦公|職場", ("office", "上班")),
    ("條件", r"\bnight\b|\bevening\b|夜晚|晚上", ("night", "夜晚")),
    ("條件", r"\bdaytime\b|\bday ?time\b|\beveryday\b|白天|日常", ("daytime", "白天日常")),
    # 色名（只把原文出現的色名翻成通用中英名，不推論冷暖歸屬）
    ("色名", r"\bdusty rose\b|乾燥玫瑰", ("dusty rose", "乾燥玫瑰")),
    ("色名", r"\bmlbb\b|my lips? but better", ("MLBB",)),
    ("色名", r"\bnudes?\b|裸色", ("nude", "裸色")),
    ("色名", r"\boranges?\b|橘", ("orange", "橘色")),
    ("色名", r"\bcorals?\b|珊瑚", ("coral", "珊瑚色")),
    ("色名", r"\bmauves?\b|藕紫|藕色", ("mauve", "藕紫")),
    ("色名", r"\bberr(?:y|ies)\b|莓果", ("berry", "莓果色")),
    ("色名", r"\bbrick\b|磚紅", ("brick red", "磚紅")),
    ("色名", r"\bpeach\w*|蜜桃", ("peach", "蜜桃色")),
    ("色名", r"\bplum\b|\bpurpl\w*|\bviolet\b|紫", ("purple", "紫色")),
    ("色名", r"\bbrown\w*|棕|咖啡色", ("brown", "棕色")),
    ("色名", r"\bred\b|紅", ("red", "紅色")),
    ("色名", r"\bpink\w*|粉(?:色|紅)", ("pink", "粉色")),
]
_LEX = [(facet, re.compile(rx, re.I), terms) for facet, rx, terms in LEXICON]
LEXICON_SHA256 = sha256_text(json.dumps(LEXICON, ensure_ascii=False))
SEARCH_TERMS_MARK = "〔原文術語〕"

# search_text 的原文部分＝normalize_search(text)（contract-v1.1 search_text_rule）。與 02_clean.build_search_text 同一組
# 規則（NFKC、∆→Δ／∗→*／−→-、空白整併），但不含文件層級的黏字拆分（那需要整份文件字彙；目前匯出未觸發）。
SEARCH_NORMALIZE_VERSION = "search-normalize-v1"
_SEARCH_CHAR_MAP = {"∆": "Δ", "∗": "*", "−": "-"}


def normalize_search(text: str) -> str:
    out = unicodedata.normalize("NFKC", text)
    for k, v in _SEARCH_CHAR_MAP.items():
        out = out.replace(k, v)
    return re.sub(r"\s+", " ", out).strip()


def search_base(search_text: str) -> str:
    """search_text 去掉〔原文術語〕段後的原文部分。"""
    return search_text.split("\n" + SEARCH_TERMS_MARK + " ", 1)[0]

# 原文明示條件（conditions-v2，C-REQ-5）：只逐字截取「條件子句」本身——
# if／when 子句到第一個逗號、破折號、括號或句末為止，而且必須在句首或逗號／破折號之後（「mistakes when choosing」
# 這類句中 when 不算）；for＋情境（daytime、nighttime、the office…）、for／with＋膚色底調；眼妝／服裝的搭配情境。
# 不收：學術來源（研究發現、方法敘述不是使用條件）、比較句裡 than 後面的膚色、商品名、引號沒閉合的截斷句、
# even if／as if 與 if needed 這類非適用條件。
_SKIN_ADJ = r"(?:fair|light|lighter|medium|olive|tan|tanned|deep|deeper|dark|darker|cool|warm|neutral)(?:[- ]toned)?"
_COND_PATTERNS = [
    ("clause", re.compile(r"(?:(?<=^)|(?<=[.!?。！？]\s)|(?<=[.!?][”\"’)]\s)|(?<=[,—–:]\s)|(?<=[,—–])|(?<=\n))"
                          r"\b(?:if|when|whenever|unless)\b(?! it comes to)"
                          r"[^,.;:!?—–()“”\"\n]{3,150}", re.I)),
    ("scene", re.compile(r"\bfor (?:a |an |the |your )?(?:[\w-]+,? ){0,3}?(?:daytime|day|nighttime|night(?: out)?|"
                         r"evening(?: events?)?|office|work|everyday|special occasions?|part(?:y|ies)|date night|"
                         r"weekend)\b(?: (?:look|wear|events?|makeup))?", re.I)),
    ("skin", re.compile(r"\b(?:for|on|with|if you have) (?:those with |anyone with |people with |someone with )?"
                        r"(?:" + _SKIN_ADJ + r"(?:,? (?:and|or) |, | ))+(?:skin(?: tones?)?|complexions?|undertones?)\b",
                        re.I)),
    ("pairing", re.compile(r"\b(?:pair|pairing|match|matching|mirroring|with) (?:the magic of )?(?:your |a |an )?"
                           r"(?:(?!every\b)[\w-]+,? ){0,3}?(?:graphic eyeliner|eyeliner|smok(?:e)?y eyes?|bold eyes?|"
                           r"(?:heavy|bold|dramatic|smok(?:e)?y) eye makeup|dress|outfit|suit)(?: look)?\b", re.I)),
    ("zh", re.compile(r"(?:如果|假如|倘若|若是?)[^，。；,;]{1,40}")),
    ("zh", re.compile(r"(?<![相適應])當[^，。；,;時然中]{1,20}(?:時|的時候)")),
]
_LIST_TAIL = re.compile(r",\s*(?:[\w’'-]+\s?){1,3}(?:,\s*(?:[\w’'-]+\s?){1,3})*,?\s+(?:or|and)\s+(?:[\w’'-]+\s?){1,3}"
                        r"(?=[,.;:!?)]|$)")
_COND_REJECT = re.compile(r"^(?:if|when)\s+(?:needed|required|necessary|desired|"
                          r"possible|any|so|not|ever|in doubt|I)\b", re.I)  # if I could…：作者假設語，不是適用條件

# 品牌／商品指涉
_TM = re.compile(r"(?:[A-Z0-9][\w’'&.+-]*\s){0,6}[A-Z0-9][\w’'&.+-]*\s?[™®]")
_BY_BRAND = re.compile(r"\bby ((?:[A-Z][\w’'&.-]+\s?){1,4})")
_PRODUCT_NOUN = (r"(?:Lipsticks?|Lip ?(?:Colou?rs?|Gloss|Liner|Balm|Oil|Stain|Tint|Kit)|Gloss|Liner|Balm|"
                 r"Foundation|Concealer|Primer|Setting Spray|Matte Ink|Lacquer|Crayon)")
_NAMED_PRODUCT = re.compile(r"\b((?:[A-Z0-9][\w’'&.-]*\s){2,8}" + _PRODUCT_NOUN + r")\b")
# 品牌口吻的系列／色號名：「my Lip Cheat Contour Duo」「Icon Baby lipstick」（兩個以上大寫詞＋小寫商品名詞）
_MY_PRODUCT = re.compile(r"\b(?:my|our) ((?:[A-Z][\w’'.&-]*\s?){1,6})")
_NAMED_LOWER = re.compile(r"\b((?:[A-Z][\w’'.&-]*\s){2,5})(?:lipsticks?|lip ?liners?|liners?|gloss|balm|lip tint|"
                          r"lip blur|blush)\b")
# 色號名（C-REQ-2）：商品名後的「in 色號」、品牌口吻「like 色號」、中文「」色號
_SHADE_AFTER = re.compile(r"^\s+in\s+((?:[A-Z][\w’'.&-]*)(?:\s(?:[A-Z][\w’'.&-]*|of|the|de|du|la|le|à)){0,4})")
_LIKE_NAME = re.compile(r"\blike (?:the )?(?:[a-z]+(?:-[a-z]+)* )?((?:[A-Z][\w’'.&-]*)(?:\s[A-Z][\w’'.&-]*){0,4})")
_ZH_PRODUCT_NOUN = r"(?:唇膏|唇彩|唇釉|唇蜜|唇線筆|睫毛膏|眼影盤|眼影|粉底液|遮瑕膏|腮紅)"
_ZH_PRODUCT = re.compile(r"([一-鿿0-9 ]{2,20}" + _ZH_PRODUCT_NOUN + r")\s*(?=[™®]|中的|系列)")
_ZH_PRODUCT_LEAD = re.compile(r"(?:我們的|使用|試試|介紹|譬如|例如)\s*([一-鿿0-9 ]{2,20}?" + _ZH_PRODUCT_NOUN + r")")
_ZH_SPLIT = re.compile(r".*[和與及或、，的]")
_ZH_VERB_LEAD = re.compile(r"^(?:不妨試試|向您介紹|例如我們的|我們的|使用|試試|塗抹|介紹|譬如|例如|用)\s*")
_GENERIC_WORDS = {"Tan", "Deep", "Dark", "Fair", "Light", "Medium", "Red", "Pink", "Nude", "Black", "Brown", "Rose",
                  "Coral", "Berry", "Plum", "Orange", "White", "Gold", "Silver"}
_ZH_SHADE = re.compile(r"「([^」]{1,15})」")
_ZH_SHADE_CONTEXT = re.compile(r"色號|" + _ZH_PRODUCT_NOUN)

_METHOD_LEAD = re.compile(
    r"(?:^|[.!?•\n]\s*|\d\.\s*\|?\s*)(?:Apply|Use|Start|Begin|Blot|Line|Exfoliate|Swipe|Dab|Layer|Prep|Fill|Outline|"
    r"Pat|Press|Cover|Match|Take|Decant|Squeeze|Blend|Finish|Trace|Smooth|Remove|Wipe|Try|Opt|Pick|Choose|Go|"
    r"Avoid|Skip|Keep|Add|Top|Follow|Don['’]t)\b"
    r"|[,—]\s*(?:apply|use|blot|dab|line|swipe|layer|pat|press|blend|reapply|touch up)\b"
    r"|(?:先|再|接著|然後|最後)[^，。]{0,6}(?:塗|擦|按壓|描|打底|上)", re.M)
_PICK_COLOR = re.compile(r"\b(?:opt for|go for|look for|choose|pick|suits?|flatter\w*|complement\w*|works? best|"
                         r"try)\b|適合|選擇|推薦", re.I)
_RESEARCH = re.compile(r"\b(?:participants?|subjects?|results?|study|studies|experiment\w*|significant\w*|"
                       r"p\s?[<=]|ΔE|CIELAB|L\*|samples?|observers?|we found|was found|were found)\b|受試|實驗|顯著",
                       re.I)
_CLAIM = re.compile(r"\b(?:\d+[- ]hours?|all[- ]day|formula\w*|shop\b|available|our\b|new\b)|小時|配方", re.I)


@dataclass
class DocInputs:
    doc_id: str
    records: list[dict]
    cleaned: dict
    bibliography: dict
    export_sha256: str


def matched_terms(text: str) -> dict[str, list[str]]:
    """原文命中的術語，依 facet 分組（保持 LEXICON 順序、不重複）。"""
    out: dict[str, list[str]] = {}
    for facet, rx, terms in _LEX:
        if rx.search(text):
            bucket = out.setdefault(facet, [])
            bucket += [t for t in terms if t not in bucket]
    return out


def explicit_conditions(text: str, *, academic: bool = False, products: Iterable[str] = ()) -> list[str]:
    """原文明示的適用條件（conditions-v2），每個都是原文的連續子字串；依出現順序、去重、被較長片段包含者不重列。"""
    if academic:
        return []
    products = [p for p in products if p]
    spans: list[tuple[int, str]] = []
    for kind, rx in _COND_PATTERNS:
        for m in rx.finditer(text):
            s = m.group(0).strip(" ,，")
            start = m.start() + m.group(0).find(s)
            if not s or len(s) > 160:
                continue
            if kind == "clause":
                before = text[max(0, start - 8):start].lower()
                if _COND_REJECT.match(s) or before.endswith(("even ", "as ")):
                    continue
                tail = _LIST_TAIL.match(text[start + len(s):])
                if tail:  # 「whether your skin is warm, cool, or neutral」：逗號是列舉，不是子句結束
                    s = text[start:start + len(s) + tail.end()]
            if kind == "skin" and "than " in text[max(0, start - 30):start].lower():
                continue  # 比較句（richer than fair and medium skin）裡的膚色不是適用條件
            if s.count("“") != s.count("”") or s.count("(") != s.count(")"):
                continue
            if any(p in s for p in products) or re.search(r"[™®]", s):
                continue
            spans.append((start, s))
    found: list[str] = []
    for _, s in sorted(spans):
        if s not in found:
            found.append(s)
    return [s for s in found if not any(s != o and s in o for o in found)]


# ---------------------------------------------------------------------------
# 多情境拆分（#r<n>）：一段裡有兩個以上「不相容」情境的條件、且分在不同句子時，依句子切成連續子範圍。
# 情境類別只看條件字串；白天與上班相容（同一組妝），其餘不同類別都視為不同情境。
# ---------------------------------------------------------------------------
_SCENE_CLASSES = [
    ("day", re.compile(r"\bday(?:time)?\b|\beveryday\b|白天|日間|日常", re.I)),
    ("office", re.compile(r"\boffice\b|\bwork\b|上班|辦公", re.I)),
    ("night", re.compile(r"\bnight(?:time)?\b|\bevening\b|\bpart(?:y|ies)\b|夜|派對", re.I)),
    ("lip_focus", re.compile(r"emphasi[sz]e your lips|going bold with your|statement lip", re.I)),
    ("eye_focus", re.compile(r"eyeliner|smok(?:e)?y eye|bold eyes?|graphic|煙燻", re.I)),
    ("heavy_eye", re.compile(r"dialled up your blush or eye|heavy eye|dramatic eye makeup|眼妝(?:很|較)?重", re.I)),
    ("outfit", re.compile(r"\bdress\b|\boutfit\b|\bsuit\b|服裝|洋裝", re.I)),
    ("cool", re.compile(r"\bcool\b|冷", re.I)),
    ("warm", re.compile(r"\bwarm\b|暖", re.I)),
    ("neutral", re.compile(r"\bneutral\b|中性", re.I)),
    ("depth_light", re.compile(r"\b(?:fair|light|lighter)\b", re.I)),
    ("depth_medium", re.compile(r"\bmedium\b", re.I)),
    ("depth_deep", re.compile(r"\b(?:tan|tanned|deep|deeper|dark|darker)\b", re.I)),
]
_COMPATIBLE = [{"day", "office"}]
_SENT = re.compile(r"[^.!?。！？]+(?:[.!?。！？]+[”\"’)]*)?\s*")


def scene_classes(condition: str) -> frozenset[str]:
    return frozenset(name for name, rx in _SCENE_CLASSES if rx.search(condition))


def _conflict(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b or a & b:
        return False
    return not any(a | b <= g for g in _COMPATIBLE)


# 縮寫、姓名縮寫與括號內的句點不是句界（「(see M.U.I. Issue 3)」）：避免子範圍邊界切在句子中間
_ABBREV_END = re.compile(r"(?:\b[A-Z]|\b(?:e\.g|i\.e|vs|etc|Dr|Mr|Mrs|Ms|St|No|approx|Fig|p))\.[”\"’)]*$")
# 一般句接在情境句後面時，只有這些承接詞開頭才算延續同一情境；否則另成通則範圍（條件不錯綁）
_CONTINUE = re.compile(r"^(?:This|That|These|Those|It|It[’']s|They|Their|However|But|Yet|So|Likewise|Also|And|Then|"
                       r"Just|Plus)\b")


def sentences(text: str) -> list[tuple[int, int]]:
    """句子的 (起, 迄) 位置；迄不含句末空白。拼起來覆蓋全文；縮寫與未閉合括號內的句點不斷句。"""
    out: list[list[int]] = []
    for m in _SENT.finditer(text):
        s, e = m.start(), m.end()
        while e > s and text[e - 1].isspace():
            e -= 1
        if e <= s:
            continue
        if out:
            prev = text[out[-1][0]:out[-1][1]]
            if _ABBREV_END.search(prev) or prev.count("(") > prev.count(")"):
                out[-1][1] = e
                continue
        out.append([s, e])
    return [(s, e) for s, e in out]


def _alternatives(text: str, s: int, e: int, conds: list[str]) -> list[dict]:
    """同一句用 or 接兩個不相容情境時（「matching a smokey eye with a nude lipstick or mirroring a red dress with a
    classic red lip, take cues from your look」），回傳各分支在母段中的位置與條件；不切句（切了會留下沒有主要子句的
    殘段），由 sub_range.alternatives 標明它們是替代分支、不是同時成立的條件。沒有這種情況回傳 []。"""
    pos = sorted((text.find(c, s, e), c) for c in conds)
    cuts = []
    for (p1, c1), (p2, c2) in zip(pos, pos[1:]):
        if _conflict(scene_classes(c1), scene_classes(c2)):
            m = re.search(r",?\s+(or)\s+", text[p1 + len(c1):p2])
            if m:
                cuts.append((p1 + len(c1) + m.start(), p1 + len(c1) + m.start(1)))
    if not cuts:
        return []
    bounds = [(s, cuts[0][0])] + [(cuts[i][1], cuts[i + 1][0]) for i in range(len(cuts) - 1)] + [(cuts[-1][1], e)]
    return [{"start": bs, "end": be, "conditions": [c for p, c in pos if bs <= p < be]} for bs, be in bounds]


def split_ranges(text: str, conditions: list[str]) -> list[tuple[int, int, list[str], str, list[dict]]]:
    """回傳子範圍 [(起, 迄, 條件, kind, 替代分支)]，kind＝scenario（情境）或 general（通則）；不需拆時回傳 []。

    是否拆：同段不同句有不相容情境才拆（與 v2 相同）。怎麼切（v3，E-030）：
    - 情境句開一個 scenario 範圍；後面的一般句只有以承接詞（This、However…）開頭才併入，否則另成 general 範圍
    - 第一個情境句之前的導言／通則句成為 general 範圍（replace 模式母段不進檢索，通則不能因此遺失）
    - 同一句以 or 接兩個不相容情境時整句一段，另回傳各替代分支的位置與條件（不切出沒有主要子句的殘段）
    - 斷句不切在縮寫或括號中間"""
    tagged = []
    for s, e in sentences(text):
        conds = [c for c in conditions if c in text[s:e]]
        cls = frozenset().union(*(scene_classes(c) for c in conds)) if conds else frozenset()
        tagged.append((s, e, conds, cls))
    keys = [t[3] for t in tagged if t[3]]
    if not any(_conflict(a, b) for i, a in enumerate(keys) for b in keys[i + 1:]):
        return []
    ranges: list[list] = []  # [起, 迄, 條件, 類別, kind, 替代分支]
    for s, e, conds, cls in tagged:
        last = ranges[-1] if ranges else None
        alts = _alternatives(text, s, e, conds) if cls else []
        if cls:
            if last and last[4] == "scenario" and not last[5] and not alts and not _conflict(last[3], cls):
                last[1], last[3] = e, last[3] | cls
                last[2] += [c for c in conds if c not in last[2]]
            else:
                ranges.append([s, e, list(conds), cls, "scenario", alts])
        elif last and (last[4] == "general" or _CONTINUE.match(text[s:e])):
            last[1] = e
            last[2] += [c for c in conds if c not in last[2]]
        else:
            ranges.append([s, e, list(conds), frozenset(), "general", []])
    return [(s, e, c, k, a) for s, e, c, _, k, a in ranges]


# ---------------------------------------------------------------------------
# 切片整修 F 版（2026-10-25，data/review/chunk-repair-20261025/REPORT.md）：預設關閉，build_units 參數打開。
# - 長段切（long_split-v1）：超過 512 token 的段（表格除外）依句界切成 ≤384 token、相鄰重疊 ≤64 token 的 #s 子範圍
# - 參考文獻（reference_list-v1）：作者-年份／DOI 密度高的段標 retrieval.exclude（留作引用，不進檢索）
# - 碎片併入（fragment_merge-v1）：圖說／表格標題／列表項目／不到 50 字且沒有句尾標點的整段單位，併進同一節
#   緊鄰的整段單位（先往前、節首才往後），合併後 ≤512 token；子範圍與排除檢索的單位不參與
# ---------------------------------------------------------------------------
LONG_SPLIT_VERSION = "long_split-v1"
LONG_SPLIT_OVER, LONG_PIECE_MAX, LONG_PIECE_OVERLAP = 512, 384, 64
REFERENCE_RULE = "reference_list-v1"
MERGE_VERSION = "fragment_merge-v1"
MERGE_MAX = 512
FRAGMENT_SHORT_CHARS = 50
FRAGMENT_TYPES = {"figure_caption", "table_caption", "list_item"}
_SENT_END = re.compile(r"[.!?。！？」”\")]\s*$")
_REF_ITEM = re.compile(r"[A-Z][A-Za-z'\-]+,\s(?:[A-Z]\.\s?){1,3}.{0,200}?\((?:19|20)\d\d[a-z]?\)")
_DOI = re.compile(r"doi[:.]|https?://doi", re.I)


def is_reference_list(text: str) -> bool:
    """參考文獻列表：作者-年份條目與 DOI 合計 ≥3，而且平均每 250 字至少半個（AR002 p.13–14 被誤標成 Publisher's note）。"""
    n = len(_REF_ITEM.findall(text)) + len(_DOI.findall(text))
    return n >= 3 and n * 250 > len(text) * 0.5


def long_pieces(text: str, count_tokens: Callable[[str], int], max_tokens: int = LONG_PIECE_MAX,
                overlap: int = LONG_PIECE_OVERLAP) -> list[tuple[int, int]]:
    """依句界把長段切成 [(起, 迄)]；每片 ≤max_tokens，相鄰片重疊最後幾句（≤overlap token，至少前進一句）。
    單句超過上限時才在空白處切。"""
    spans: list[tuple[int, int]] = []
    for s, e in sentences(text):
        if count_tokens(text[s:e]) <= max_tokens:
            spans.append((s, e))
            continue
        cur, last = None, None
        for m in re.finditer(r"\S+", text[s:e]):
            ws, we = s + m.start(), s + m.end()
            if cur is None:
                cur = ws
            elif count_tokens(text[cur:we]) > max_tokens:
                spans.append((cur, last))
                cur = ws
            last = we
        spans.append((cur, last))
    pieces, i = [], 0
    while i < len(spans):
        j = i
        while j + 1 < len(spans) and count_tokens(text[spans[i][0]:spans[j + 1][1]]) <= max_tokens:
            j += 1
        pieces.append((spans[i][0], spans[j][1]))
        if j + 1 >= len(spans):
            break
        k = j + 1
        while k - 1 > i and count_tokens(text[spans[k - 1][0]:spans[j][1]]) <= overlap:
            k -= 1
        i = k
    return pieces


# long_split-v1-ov：重疊改成「前一片的 10～20%」（老師的切片原則 2；2026-10-25 使用者指定先測這一項，門檻仍 512 token）
LONG_SPLIT_OVERLAP_VERSION = "long_split-v1-ov10-20"
LONG_OVERLAP_MIN, LONG_OVERLAP_MAX, LONG_OVERLAP_FALLBACK = 0.10, 0.20, 0.15
_CJK = "㐀-鿿豈-﫿　-〿＀-￯"
_OVERLAP_WORD = re.compile(f"[{_CJK}]|[^\\s{_CJK}]+")


def long_pieces_overlap(text: str, count_tokens: Callable[[str], int], max_tokens: int = LONG_PIECE_MAX,
                        lo: float = LONG_OVERLAP_MIN, hi: float = LONG_OVERLAP_MAX,
                        fallback: float = LONG_OVERLAP_FALLBACK) -> list[tuple[int, int]]:
    """依句界把長段切成 [(起, 迄)]，每片 ≤max_tokens；下一片的開頭重疊前一片的 lo～hi（以前一片的 token 數計）。
    先找落在範圍內、重疊最多的句首；找不到（句子太長）才在詞的邊界取前一片的 fallback 比例。"""
    sents = sentences(text)
    ends, starts = [e for _, e in sents], [s for s, _ in sents]
    # 詞的邊界：英文等以空白分詞；中文沒有空白，每個字都是邊界（否則整句被當成一個詞，重疊會 0% 或一次跳一整句）
    words = [(m.start(), m.end()) for m in re.finditer(_OVERLAP_WORD, text)]
    pieces, pos = [], words[0][0] if words else 0
    while True:
        fit = [e for e in ends if e > pos and count_tokens(text[pos:e]) <= max_tokens]
        if fit:
            end = fit[-1]
        else:                                   # 開頭這句就超過上限：在詞的邊界切
            end = pos
            for _, we in (w for w in words if w[0] >= pos):
                if count_tokens(text[pos:we]) > max_tokens:
                    break
                end = we
            if end == pos:                      # 單一個詞就超過上限：整個詞自成一片
                end = next(we for ws, we in words if ws >= pos)
        pieces.append((pos, end))
        if not any(ws >= end for ws, _ in words):
            break
        n = count_tokens(text[pos:end])
        lo_n, hi_n = lo * n, hi * n
        cands = [s for s in starts if pos < s < end and lo_n <= count_tokens(text[s:end]) <= hi_n]
        if cands:
            nxt = cands[0]                      # 範圍內重疊最多的句首
        else:
            target, nxt = fallback * n, end
            for ws, _ in reversed([w for w in words if pos < w[0] < end]):
                nxt = ws
                if count_tokens(text[ws:end]) >= target:
                    break
        pos = nxt
    return pieces


def _is_fragment(u: dict, block_type: Optional[str]) -> bool:
    if u["unit_id"] != u["chunk_id"] or u.get("retrieval", {}).get("exclude"):
        return False
    if block_type in FRAGMENT_TYPES:
        return True
    return len(u["text"].strip()) < FRAGMENT_SHORT_CHARS and not _SENT_END.search(u["text"])


def brand_names(doc_id: str, cleaned: dict, bibliography: dict) -> list[str]:
    """網頁來源的發行品牌：<title> 最後一段（例如「| L’Oréal Paris」）與書目 institution 中的品牌部分。"""
    names: list[str] = []
    title = (cleaned.get("manifest") or {}).get("title") or ""
    if "|" in title:
        names.append(title.rsplit("|", 1)[1].strip())
    inst = bibliography.get("institution") or ""
    if "品牌" in (bibliography.get("doc_type") or "") and inst:
        names.append(re.sub(r"\s*(?:Beauty )?Magazine$", "", inst).strip())
    out: list[str] = []
    for n in names:
        for v in (n, n.replace("’", "'"), n.replace("'", "’")):  # 固定順序：set 的順序隨 PYTHONHASHSEED 變，輸出 SHA 不可重現
            if v and v not in out:
                out.append(v)
    return out


def product_mentions(text: str, brands: Iterable[str], *, academic: bool = False) -> list[str]:
    """學術來源只收 ™／® 標記（研究用商品或器材）；「by 某某」與「大寫詞＋商品名詞」在論文裡多半是作者或族群，不收。"""
    found: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in found:
            found.append(s)

    for m in _TM.finditer(text):
        add(m.group(0))
    for b in brands:
        if b and b in text:
            add(b)
    for m in re.finditer(r"[一-鿿0-9 ]{2,20}[™®]", text):  # 中文品名＋™®
        add(_ZH_VERB_LEAD.sub("", _ZH_SPLIT.sub("", m.group(0))))
    if not academic:
        for m in _BY_BRAND.finditer(text):
            add(m.group(1))
        for rx in (_NAMED_PRODUCT, _MY_PRODUCT, _NAMED_LOWER):
            for m in rx.finditer(text):
                add(m.group(1))
                shade = _SHADE_AFTER.match(text[m.end(1):])
                name = shade.group(1).split(". ")[0].rstrip(".") if shade else ""
                if name and name not in _GENERIC_WORDS:  # 「in Tan or Deep」這類單字不當色號，免得誤配膚色詞
                    add(name)
        for m in _LIKE_NAME.finditer(text):
            if m.group(1) not in brands:
                add(m.group(1))
        for rx in (_ZH_PRODUCT, _ZH_PRODUCT_LEAD):
            for m in rx.finditer(text):
                add(_ZH_VERB_LEAD.sub("", _ZH_SPLIT.sub("", m.group(1))).strip())
        if _ZH_SHADE_CONTEXT.search(text):
            for m in _ZH_SHADE.finditer(text):
                add(m.group(1))
    return [s for s in found if len(s) > 1 and not any(s != o and s in o for o in found)]


def content_role(text: str, *, academic: bool, products: list[str]) -> Optional[str]:
    """規則判讀的主要內容角色；判不出來回傳 None（不硬給）。"""
    method = bool(_METHOD_LEAD.search(text))
    if products and _CLAIM.search(text) and not method:
        return "商品宣稱"
    if method and not academic:
        return "方法"
    if academic:
        return "研究觀察" if _RESEARCH.search(text) else "背景"
    if _PICK_COLOR.search(text) and matched_terms(text).get("色名"):
        return "選色"
    if method:
        return "方法"
    return None


def support_scope(terms: dict[str, list[str]], conditions: list[str]) -> Optional[str]:
    """只描述原文本身出現的部位／操作／用途／條件／色名；不寫效果保證。"""
    parts = [f"{facet}：{'、'.join(t for t in terms[facet] if not t.isascii())}"
             for facet in ("部位", "操作", "用途", "條件", "色名") if terms.get(facet)]
    if conditions:
        parts.append("原文條件：" + "；".join(conditions))
    return "；".join(parts) or None


def _html_locators(cleaned: dict) -> dict[str, dict]:
    """與 backend ingest._html_block_locators 同規則：DOM 路徑＋標題階層（被移除的標題不算）。"""
    out: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []
    for b in cleaned["blocks"]:
        level, text = b.get("heading_level"), (b.get("cleaned_text") or "").strip()
        if b.get("block_type") == "heading" and isinstance(level, int) and text and not b.get("is_removed"):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
        out[b["block_id"]] = {"locator": b.get("source_locator"), "heading_path": [t for _, t in stack]}
    return out


def _page_text(pages: list[int]) -> str:
    if not pages:
        return ""
    return f"p.{pages[0]}" if pages[0] == pages[-1] else f"p.{pages[0]}–{pages[-1]}"


def build_units(doc: DocInputs, count_tokens: Optional[Callable[[str], int]] = None, *, split: bool = True,
                keep_split_parent: bool = True, long_split: bool = False, exclude_references: bool = False,
                merge_fragments: bool = False, long_split_overlap: bool = False) -> list[dict]:
    """整段單位依 rag_ready 順序；確實混接多情境的段，緊接在母段後面加 <chunk_id>#r<n> 子範圍單位。
    count_tokens：cl100k 計數函式（build_support_units.py 注入）；None 時 tokens_cl100k 記 None，不估。
    long_split／exclude_references／merge_fragments：F 版規則（見上方說明）；long_split、merge_fragments 需要 count_tokens。"""
    if (long_split or merge_fragments) and count_tokens is None:
        raise ValueError("long_split／merge_fragments 需要 count_tokens（不估算 token）")
    manifest = doc.cleaned["manifest"]
    is_html = manifest.get("source_type") == "html"
    html = _html_locators(doc.cleaned) if is_html else {}
    academic = not is_html and "textbook" not in (doc.bibliography.get("doc_type") or "").lower()
    brands = brand_names(doc.doc_id, doc.cleaned, doc.bibliography)
    owner = {b: r["block_id"] for r in doc.records for b in (r.get("block_ids") or [r["block_id"]])}
    ids = [r["block_id"] for r in doc.records]
    units = []
    for i, rec in enumerate(doc.records):
        uid = rec["block_id"]
        text = rec["display_text"]
        products = product_mentions(text, brands, academic=academic)
        conds = explicit_conditions(text, academic=academic, products=products)
        block_ids = rec.get("block_ids") or [uid]
        if is_html:
            locs = [html.get(b) or {} for b in block_ids]
            primary = {"type": "html_dom", "url": manifest.get("source_url"),
                       "dom_paths": [loc.get("locator") for loc in locs],
                       "heading_path": (locs[0].get("heading_path") if locs else []),
                       "block_ids": block_ids}
        else:
            primary = {"type": "pdf_page", "pages": rec.get("pages") or [], "text": _page_text(rec.get("pages") or []),
                       "block_ids": block_ids}
        corr = []
        for n, c in enumerate(rec.get("corroborating_units") or [], start=1):
            corr.append({"unit_id": f"{uid}#c{n}", "kind": c.get("kind"), "relation": c.get("relation"),
                         "title": c.get("title"), "type": "pdf_page", "pages": c.get("pages") or [],
                         "text": _page_text(c.get("pages") or []), "block_ids": c.get("block_ids") or [],
                         "step_block_ids": c.get("step_block_ids") or [], "steps_matched": c.get("steps_matched"),
                         "display_text": c.get("display_text") or "",
                         "display_text_sha256": sha256_text(c.get("display_text") or "")})
        cont = rec.get("continues_from_block_id")
        parent = _unit(doc, manifest, uid, uid, text, rec.get("text") or text, primary, corr or None, rec,
                       academic, products, conds, count_tokens,
                       {"prev": ids[i - 1] if i > 0 else None, "next": ids[i + 1] if i + 1 < len(ids) else None,
                        "parent": owner.get(cont) if cont else None,
                        "corroborating_units": [c["unit_id"] for c in corr]})
        units.append(parent)
        if exclude_references and is_reference_list(text):
            parent["retrieval"] = {"exclude": True, "reason": "reference_list", "rule": REFERENCE_RULE}
            continue
        ranges = split_ranges(text, conds) if split else []
        if (long_split and not ranges and rec.get("block_type") != "table"
                and count_tokens(text) > LONG_SPLIT_OVER):
            cut = (long_pieces_overlap(text, count_tokens, LONG_PIECE_MAX, LONG_OVERLAP_MIN, LONG_OVERLAP_MAX,
                                       LONG_OVERLAP_FALLBACK) if long_split_overlap
                   else long_pieces(text, count_tokens, LONG_PIECE_MAX, LONG_PIECE_OVERLAP))
            ranges = [(s, e, [c for c in conds if c in text[s:e]], "long_piece", []) for s, e in cut]
        if ranges and not keep_split_parent:  # replace 模式：拆分段只輸出子範圍（母段由匯入端留作引用）
            units.pop()
        mark = "s" if ranges and ranges[0][3] == "long_piece" else "r"
        sub_ids = [f"{uid}#{mark}{n}" for n in range(1, len(ranges) + 1)]
        for n, (start, end, rconds, kind, alts) in enumerate(ranges):
            sub = text[start:end]
            sub_products = [p for p in products if p in sub]
            u = _unit(doc, manifest, sub_ids[n], uid, sub, normalize_search(sub), {**primary, "char_range": [start, end]},
                      None, rec,
                      academic, sub_products, rconds, count_tokens,
                      {"prev": sub_ids[n - 1] if n > 0 else None,
                       "next": sub_ids[n + 1] if n + 1 < len(sub_ids) else None,
                       "parent": uid, "corroborating_units": []})
            u["sub_range"] = {"of": uid, "start": start, "end": end,
                              "rule": (SPLIT_VERSION if kind != "long_piece" else
                                       LONG_SPLIT_OVERLAP_VERSION if long_split_overlap else LONG_SPLIT_VERSION),
                              "kind": kind,
                              "alternatives": alts,
                              "scene_classes": sorted(set().union(*(scene_classes(c) for c in rconds)))}
            units.append(u)
    if merge_fragments:
        units = _merge_fragments(doc, units, count_tokens, academic)
    return units


# ---------------------------------------------------------------------------
# 已核准範圍單位（approved_range-v1，2026-10-25）：來源只核准部分範圍時，只取已核准的連續原文建立 <母段>#a<n> 子範圍。
# 匯入時母段照子範圍規則改成只供引用，未核准的文字不會進檢索；核准範圍不擴大。
# 範圍之間只隔空白（例：清單項目之間的換行）視為連續；合併後蓋滿整段就是整段核准，不另建 #a。
# ---------------------------------------------------------------------------
APPROVED_RANGE_VERSION = "approved_range-v1"


def approved_spans(text: str, ranges: Iterable[Iterable[int]]) -> list[tuple[int, int]]:
    """核准範圍 → 連續段：只隔空白的相鄰範圍合併；回傳 (起, 迄)。"""
    spans: list[list[int]] = []
    for s, e in sorted((int(a), int(b)) for a, b in ranges):
        if spans and s <= spans[-1][1] or (spans and text[spans[-1][1]:s].strip() == ""):
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    return [(s, e) for s, e in spans]


def approved_range_units(parent: dict, ranges: Iterable[Iterable[int]], decision_id: str,
                         count_tokens: Optional[Callable[[str], int]] = None) -> list[dict]:
    """依核准範圍建立 #a 子範圍單位；連續段蓋滿整段時回傳 []（整段已核准，不需 #a）。
    文字是母段的連續子字串；搜尋文字、條件、商品指涉依子範圍原文重算，連結指回母段。"""
    text = parent["text"]
    spans = approved_spans(text, ranges)
    if len(spans) == 1 and text[:spans[0][0]].strip() == "" and text[spans[0][1]:].strip() == "":
        return []
    out = []
    ids = [f"{parent['unit_id']}#a{n}" for n in range(1, len(spans) + 1)]
    for n, (s, e) in enumerate(spans):
        sub = text[s:e]
        terms = matched_terms(sub)
        added = [t for facet in terms.values() for t in facet]
        base = normalize_search(sub)
        search = base + ("\n" + SEARCH_TERMS_MARK + " " + " ".join(added) if added else "")
        conds = [c for c in parent.get("conditions") or [] if c in sub]
        u = {**{k: v for k, v in parent.items() if k not in ("sub_range", "merge_range", "retrieval")},
             "unit_id": ids[n], "text": sub, "text_sha256": sha256_text(sub),
             "tokens_cl100k": count_tokens(sub) if count_tokens else None,
             "search_text": search, "search_text_sha256": sha256_text(search), "search_text_base_sha256": sha256_text(base),
             "locator_primary": {**(parent.get("locator_primary") or {}), "char_range": [s, e]},
             "conditions": conds, "product_mentions": [p for p in parent.get("product_mentions") or [] if p in sub],
             "support_scope": support_scope(terms, conds),
             "links": {"prev": ids[n - 1] if n else None, "next": ids[n + 1] if n + 1 < len(ids) else None,
                       "parent": parent["unit_id"], "corroborating_units": []},
             "rules": {**(parent.get("rules") or {}), "search_terms_added": added}}
        u["sub_range"] = {"of": parent["unit_id"], "start": s, "end": e, "rule": APPROVED_RANGE_VERSION,
                          "kind": "approved_range", "alternatives": [], "scene_classes": [],
                          "approval_decision_id": decision_id, "approved_ranges": [list(r) for r in ranges]}
        out.append(u)
    return out


def _merge_fragments(doc: DocInputs, units: list[dict], count_tokens: Callable[[str], int],
                     academic: bool) -> list[dict]:
    """碎片併進同一節緊鄰的整段單位（fragment_merge-v1）；併入的單位由 <主段>#m 取代，其他單位的連結改指向它。"""
    recs = {r["block_id"]: r for r in doc.records}
    frag = {u["unit_id"]: _is_fragment(u, recs[u["chunk_id"]].get("block_type")) for u in units}
    groups: list[list[dict]] = [[u] for u in units]

    def mergeable(g: list[dict]) -> bool:      # 可以接收碎片的一組：整段單位、不排除檢索
        return all(x["unit_id"] == x["chunk_id"] and not x.get("retrieval", {}).get("exclude") for x in g)

    def fits(g1: list[dict], g2: list[dict]) -> bool:
        return (recs[g1[-1]["chunk_id"]].get("section") == recs[g2[0]["chunk_id"]].get("section")
                and count_tokens("\n\n".join(x["text"] for x in g1 + g2)) <= MERGE_MAX)

    i = 1
    while i < len(groups):                      # 往前併
        if all(frag[x["unit_id"]] for x in groups[i]) and mergeable(groups[i - 1]) and fits(groups[i - 1], groups[i]):
            groups[i - 1] += groups.pop(i)
        else:
            i += 1
    i = 0
    while i < len(groups) - 1:                  # 節首併不了的，往後併
        if all(frag[x["unit_id"]] for x in groups[i]) and mergeable(groups[i + 1]) and fits(groups[i], groups[i + 1]):
            head = groups.pop(i)
            groups[i] = head + groups[i]
        else:
            i += 1

    manifest = doc.cleaned["manifest"]
    out, renamed = [], {}
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            continue
        main = next((x for x in g if not frag[x["unit_id"]]), g[0])
        uid = f"{main['unit_id']}#m"
        text = "\n\n".join(x["text"] for x in g)
        locs = [x["locator_primary"] for x in g]
        primary = {**main["locator_primary"], "block_ids": [b for loc in locs for b in loc["block_ids"]]}
        if primary["type"] == "pdf_page":
            primary["pages"] = sorted({p for loc in locs for p in loc["pages"]})
            primary["text"] = _page_text(primary["pages"])
        else:
            primary["dom_paths"] = [p for loc in locs for p in loc["dom_paths"]]
        secondary = [s for x in g for s in (x["locator_secondary"] or [])] or None
        products = list(dict.fromkeys(p for x in g for p in x["product_mentions"]))
        conds = list(dict.fromkeys(c for x in g for c in x["conditions"]))
        links = {"prev": g[0]["links"]["prev"], "next": g[-1]["links"]["next"], "parent": main["links"]["parent"],
                 "corroborating_units": [c for x in g for c in x["links"]["corroborating_units"]]}
        u = _unit(doc, manifest, uid, main["chunk_id"], text, normalize_search(text), primary, secondary,
                  recs[main["chunk_id"]], academic, products, conds, count_tokens, links)
        u["merge_range"] = {"rule": MERGE_VERSION, "main": main["unit_id"], "members": [x["unit_id"] for x in g],
                            "separator": "\n\n"}
        renamed.update({x["unit_id"]: uid for x in g})
        out.append(u)
    for u in out:                               # 連結改指向合併後的單位（自己指自己的改成 None）
        for k in ("prev", "next", "parent"):
            t = renamed.get(u["links"][k], u["links"][k])
            u["links"][k] = None if t == u["unit_id"] else t
    return out


def _unit(doc: DocInputs, manifest: dict, uid: str, chunk_id: str, text: str, base_search: str, primary: dict,
          secondary, rec: dict, academic: bool, products: list[str], conds: list[str],
          count_tokens: Optional[Callable[[str], int]], links: dict) -> dict:
    terms = matched_terms(text)
    added = [t for facet in terms.values() for t in facet]
    search = base_search + ("\n" + SEARCH_TERMS_MARK + " " + " ".join(added) if added else "")
    return {
        "schema": SCHEMA,
        "unit_id": uid,
        "source_id": doc.doc_id,
        "source_version": manifest["sha256"],
        "export_sha256": doc.export_sha256,
        "chunk_id": chunk_id,
        "text": text,
        "text_sha256": sha256_text(text),
        "tokens_cl100k": count_tokens(text) if count_tokens else None,
        "search_text": search,
        "search_text_sha256": sha256_text(search),
        "search_text_base_sha256": sha256_text(base_search),
        "locator_primary": primary,
        "locator_secondary": secondary,
        "section_original": rec.get("section"),
        "section_search": None,
        "content_role": content_role(text, academic=academic, products=products),
        "support_scope": support_scope(terms, conds),
        "conditions": conds,
        "links": links,
        "product_mentions": products,
        "eligibility": "pending",
        "citation": rec.get("citation"),
        "cited_works": rec.get("cited_works") or [],
        "attribution": rec.get("attribution"),
        "rules": {"version": RULES_VERSION, "lexicon_sha256": LEXICON_SHA256, "search_terms_added": added,
                  "derived_by": "規則（AI 撰寫），非人工核准"},
    }


def load_doc(root: Path, doc_id: str, sources: dict) -> DocInputs:
    jsonl = root / "data" / "processed" / "rag_ready" / f"{doc_id}.jsonl"
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    cleaned = json.loads((root / "data" / "processed" / "cleaned" / f"{doc_id}.json").read_text(encoding="utf-8"))
    return DocInputs(doc_id, records, cleaned, sources.get(doc_id) or {}, sha256_file(jsonl))


# ---------------------------------------------------------------------------
# 可攜 manifest：所有路徑相對於 manifest 所在資料夾；換位置解包後 verify_manifest 仍可驗
# ---------------------------------------------------------------------------
def build_manifest(base: Path, files: Iterable[Path], extra: dict) -> dict:
    entries = []
    for p in sorted(set(files)):
        rel = p.resolve().relative_to(base.resolve()).as_posix()
        lines = None
        if p.suffix == ".jsonl":
            with open(p, "rb") as f:
                lines = sum(1 for line in f if line.strip())
        entries.append({"path": rel, "sha256": sha256_file(p), "bytes": p.stat().st_size, "lines": lines})
    return {**extra, "files": entries}


def verify_manifest(manifest_path: Path) -> list[str]:
    """回傳不符項目（空 list＝全部相符）。只用相對路徑，不讀任何絕對路徑。"""
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    base = Path(manifest_path).resolve().parent
    problems = []
    for e in m["files"]:
        if Path(e["path"]).is_absolute() or ".." in Path(e["path"]).parts:
            problems.append(f"{e['path']}：不是相對路徑")
            continue
        p = base / e["path"]
        if not p.is_file():
            problems.append(f"{e['path']}：缺檔")
        elif sha256_file(p) != e["sha256"]:
            problems.append(f"{e['path']}：SHA-256 不符")
    return problems
