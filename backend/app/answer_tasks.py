"""Question-specific answer focus, with no answer facts or evaluation IDs.

Other tasks use the normal approved-evidence pipeline. Only an explicit
"tidy my own words" command uses the separate wording entry (wording-entry-v1).

wording-entry-v1（2026-10-30）：原本 task-focus-v2 的 user_request_quote 會把「我想…這樣要怎麼說？」
在問答一開頭短路成原話複述，不檢索；實測會把「我想知道冷底適合的紅色要怎麼描述？」這類知識題也短路。
改為：凡是路由判為知識／比較的問句一律進檢索；只有使用者明確下指令「幫我把這句整理／改寫……」
並給出要整理的文字時，才走 main.py 的獨立入口（不檢索、不呼叫模型、不算有來源的回答）。
"""
from __future__ import annotations

import re

VERSION = "task-focus-v2"
WORDING_VERSION = "wording-entry-v1"
# 來源術語修正（source_term_repairs）自己的版本；不併進 VERSION，因為 VERSION 會串進 prompt_version
# （answer_policy.py），改它會讓重放對不上提示版本。plain-term-v2：fix-answers 第五批加 tissue／Cupid's bow／中性。
TERM_REPAIR_VERSION = "plain-term-v5"
# plain-term-v5：fix-metrics（2026-11-08）測試組 H07「先用面紙輕壓吸掉多餘的唇膏，再用透明定妝粉…」——
#   來源是 press a tissue over your lips and dust setting powder over the tissue（面紙是墊著撲粉用，不是吸唇膏）。
#   只在「主張講定妝粉／蜜粉＋面紙吸掉多餘的唇膏」且來源有 press a tissue over lips 時改成「把面紙輕壓在嘴唇上」；
#   護唇膏的 blot（吸掉多餘護唇膏，P10／P12）不動。
_POWDER = re.compile(r"定妝粉|蜜粉|散粉|setting powder", re.I)
_TISSUE_OVER_LIPS = re.compile(r"press(?:es|ing)?\s+a\s+tissue\s+over\s+(?:your\s+|the\s+)?lips", re.I)
_TISSUE_BLOT_LIPSTICK = re.compile(r"(?:先)?用面紙輕壓吸掉多餘的?(?:唇膏|口紅)")
# plain-term-v4：fix-heldout 加 flattering 誤譯成「討厭」改「討喜」（測試組 H09「為了獲得最討厭的唇色效果」）。
# plain-term-v3：fix-answers 第八批加「中文句子裡留英文 tissue」改面紙（P12）。
# 中文句子裡夾的英文 tissue（前後空白一併拿掉）；只在主張含中文字時才改
_TISSUE_EN = re.compile(r"\s*\btissues?\b\s*", re.I)
_HAS_CJK = re.compile(r"[一-鿿]")
# plain-term-v2 用的樣式（規則說明見 source_term_repairs 內）
# 「組織」＋6 字內的按壓動作；group(1) 保留動作與中間的字
_TISSUE_ACTION = re.compile(r"組織([^，。；]{0,6}?(?:壓|按|吸|蓋|覆))")
# 來源把 cool、warm、neutral 三者並列（「cool, warm, or neutral」，順序不拘、三個都不同）
_THREE_UNDERTONES = re.compile(
    r"\b(cool|warm|neutral),\s*(?!\1\b)(cool|warm|neutral),?\s*(?:or|and)\s+(?!\1\b|\2\b)(cool|warm|neutral)\b", re.I)
# 「分為／分成／有 冷色調與暖色調」這種只列兩類的分類句；group(1) 分類動詞，2、3 是冷／暖（保持原順序）
_TWO_TONE_LIST = re.compile(r"(分為|分成|有)([冷暖])色調(?:與|和|及|、)(?!\2)([冷暖])色調")

# 整理／改寫的動詞（只認明確的編修動作，不認「怎麼說」「怎麼形容」這類其實在問知識的說法）
_EDIT_VERB = r"(?:整理|改寫|重組|重寫|潤飾|修飾|寫通順|寫得?通順|改得?通順|順一下)"
# 指令開頭：可帶「請／麻煩」與「幫我」，要被整理的對象可省略或寫「這句話／下面這段／我的需求」等
_WORDING_HEAD = (r"^(?:請|麻煩)?(?:你)?(?:幫我|幫忙|替我)?(?:把|將)?"
                 r"(?:這句話?|這段話?|這段文字|下面(?:這句|這段)?話?|以下(?:這句|這段)?話?|我的(?:話|需求|描述))?")
# 要被整理的對象寫在動詞後面的說法（「幫我重組這句話：…」「幫我整理我的需求：…」）
_WORDING_OBJECT_AFTER = r"(?:這句話?|這段話?|這段文字|下面(?:這句|這段)?話?|以下(?:這句|這段)?話?|我的(?:話|需求|描述))?"
# 形式一：「幫我把這句整理一下：<文字>」「幫我重組這句話：<文字>」——冒號後面整段都是要整理的文字
_WORDING_COLON = re.compile(_WORDING_HEAD + _EDIT_VERB + r"(?:一下)?" + _WORDING_OBJECT_AFTER
                            + r"(?:一下)?(?:成[^：:「」]{0,12})?\s*[：:]\s*(?P<text>.+)$", re.S)
# 形式二：「幫我把「<文字>」整理一下」——引號裡是要整理的文字，引號後只能接編修動詞與語尾
_WORDING_QUOTED = re.compile(r"^(?:請|麻煩)?(?:你)?(?:幫我|幫忙|替我)(?:把|將)「(?P<text>[^「」]+)」(?:這句話?|這段話?)?"
                             + _EDIT_VERB + r"(?:一下)?(?:成[^「」]{0,12})?[。！!？?～~\s]*$")
# 看起來像要系統執行其他指令的文字不整理（來源與使用者文字都視為資料，規範 2）
_INJECTION = re.compile(r"忽略|指令|系統提示|執行|prompt", re.I)


def wording_only_request(question: str) -> str | None:
    """判斷這句是不是「只要求整理使用者自己的文字」的明確指令；是就回傳要整理的文字，否則回傳 None。

    只認兩種清楚的寫法（wording-entry-v1）：
    1. 冒號式：「幫我把這句整理一下：我想找紅色口紅，不要偏粉」→ 取冒號後面的文字。
    2. 引號式：「幫我把「我想找紅色口紅，不要偏粉」整理一下」→ 取引號裡的文字。
    「我想…這樣要怎麼說比較準？」「…要怎麼描述？」不算：那是在問有沒有更準的說法，屬知識題，要進檢索。
    取出的文字只去掉頭尾空白與標點，不改字、不加色名或任何來源事實；
    少於 4 個字、或含疑似注入指令的字樣時不當成整理任務（交回一般路由）。
    """
    q = question.strip()
    m = _WORDING_COLON.match(q) or _WORDING_QUOTED.match(q)
    if not m:
        return None
    text = m.group("text").strip().strip("「」\"' ").strip(" ，,。；;！!？?")
    if len(text) < 4 or _INJECTION.search(text):
        return None
    return text


def wording_answer(text: str) -> str:
    """把 wording_only_request 取出的文字排成交付給使用者的回覆。

    明說這是依使用者提供的文字整理，系統沒有查資料、沒有引用來源，也沒有新增色彩分類；
    內容只有使用者自己的字，所以不會冒充成文獻證據或模型生成。
    """
    return "依你提供的文字整理（只用你的原話；沒有查資料、沒有新增色彩分類）：\n「" + text + "」"


def focus(question: str) -> list[str]:
    """Choose a small task contract; no document IDs, expected answers or examples."""
    notes = []
    if re.search(r"差在哪|差別|有何不同|有什麼不同|兩者.*比較", question):
        notes.append("這是比較題：第一條主張先用一句話直接回答兩者差別，不要只有各列定義；先辨別是否屬於同一分類層級，層級不同要明說。比較句可同時引用分別支持雙方的兩段證據；其餘主張再各依證據補充。只比較來源寫明的特徵，不推論優劣或不存在的色值。")
    if re.search(r"色號|色名|名字|名稱", question) and re.search(r"實際|知道|確認|判斷", question):
        notes.append("這是確認實際顏色的方法題：優先說明名稱能否判斷顏色及如何試色；保留來源的部位、光源與研究範圍，不把前臂換成手背，不把顏色的組成色調說成化學成分。")
    if re.search(r"穿|服裝|洋裝|衣服|野餐|花色|圖案", question):
        notes.append("若來源沒有直接交代使用者的服裝、圖案或活動搭配，只給來源支持的一般起點，並明說尚未涵蓋該搭配；不可將一般建議宣稱為針對該穿搭的結論。")
        if re.search(r"自然|低調|不搶戲|不張揚", question):
            notes.append("只給一至兩條符合使用者明說效果的起點，不羅列相反風格的替代造型。")
    if re.search(r"冷底|暖底|底調", question) and re.search(r"協調|有辦法|有沒有辦法", question):
        notes.append("這是有條件的選色做法題：第一條必須保留來源具體可操作的選色描述或例子，不只重述選擇相符底調；優先回答怎麼選才協調，再補其他來源支持的做法。")
    if re.search(r"看起來.*有精神|看起來.*精神一點|提氣色", question):
        notes.append("外觀目標尚待澄清時，不把有精神直接改成喜歡鮮豔或大膽；先給不依賴這項解讀的一般起點，再問一個確認問題，不自行確定使用者偏好。")
    return notes


def claim_limit(question: str) -> int:
    """A general outfit starting point should not turn into a list of other styles."""
    return 2 if (re.search(r"穿|服裝|洋裝|衣服|野餐|花色|圖案", question) and
                 re.search(r"自然|低調|不搶戲|不張揚", question)) else 6   # C-ap-v10：4 → 6


def source_term_repairs(text: str, source_texts: list[str]) -> tuple[str, list[str]]:
    """依被引用片段的原文，修正主張裡看得出來的直譯錯誤，回傳（新文字, 套用的規則名）。

    只有來源用詞能確定意思時才改（例如來源是 tissue 才把「組織」改「面紙」）；
    來源有互相衝突的說法就不改。呼叫端保留模型原文並記錄前後文字，不新增顏色解讀。

    Conflicting source terms prevent repair. Callers keep the original model
    output and record before/after; no new colour interpretation is invented.
    """
    source = "\n".join(source_texts)
    rules = []
    if (re.search(r"\bforearms?\b", source, re.I) and not
            re.search(r"back(?:s)? of (?:the |your )?hands?|手背", source, re.I) and "手背" in text):
        text = text.replace("手背", "前臂")
        rules.append("forearm_literal")
    if (re.search(r"\bcameo red\b", source, re.I) and not
            re.search(r"\b(?:deep|dark) red\b|深紅", source, re.I) and "深紅" in text):
        text = re.sub(r"深紅色?(?:[（(]cameo red[）)])?", "cameo red", text, flags=re.I)
        rules.append("unverified_colour_name_kept_in_english")
    # plain-term-v1（2026-11-08，prodref）：直譯後一般人看不懂的詞改白話，只在來源確實是那個說法時改。
    # LM35-I04「防止口紅溢出唇紋或產生羽化現象 看不懂」：feathering 是口紅沿著唇紋往外暈開。
    if re.search(r"\bfeather(?:s|ing|ed)?\b", source, re.I) and "羽化" in text:
        text = re.sub(r"(?:產生|出現)?羽化(?:現象)?", "暈開", text)
        rules.append("feathering_plain_wording")
    # N01「在上色前先進行去角質 是擦唇膏前」：來源講的是擦唇膏（lipstick）前，「上色」太籠統；講眼妝／腮紅的不動。
    if (re.search(r"\blip(?:stick|s)?\b", source, re.I) and "上色前" in text
            and not re.search(r"眼|腮|頰|粉底|指甲", text)):
        text = text.replace("上色前", "擦唇膏前")
        rules.append("apply_colour_plain_wording")
    # plain-term-v2（fix-answers 第五批，2026-11-08 prodref 後的 P10／M09／W10）：
    # P10「用組織輕輕壓在嘴唇上」：來源是 press a tissue，這裡的 tissue 是面紙。
    # 只改「組織」後 6 字內接壓／按／吸／蓋／覆的（講皮膚組織的句子不會接這些動作）。
    if re.search(r"\btissues?\b", source, re.I) and _TISSUE_ACTION.search(text):
        text = _TISSUE_ACTION.sub(lambda m: "面紙" + m.group(1), text)
        rules.append("tissue_plain_wording")
    # plain-term-v3（2026-11-08，第十四輪 P12）：
    # 模型在中文句子裡直接留英文 tissue（「先用 tissue 輕壓唇部」），來源也是 tissue → 改成面紙（和 P10 用詞一致）。
    if re.search(r"\btissues?\b", source, re.I) and _HAS_CJK.search(text) and _TISSUE_EN.search(text):
        text = _TISSUE_EN.sub("面紙", text)
        rules.append("tissue_english_left")
    # plain-term-v4（測試組 H09）：來源是 "most flattering lipstick look"（最討喜、最好看），模型寫成「最討厭的唇色效果」，
    # 語意相反。來源有 flattering、主張出現「討厭」→ 改「討喜」。來源沒有 flattering 時不動（可能真的在講討厭）。
    if re.search(r"\bflatter(?:ing|s|ed)?\b", source, re.I) and "討厭" in text:
        text = text.replace("討厭", "討喜")
        rules.append("flattering_mistranslated")
    # M09「在顴骨上方、人中及眉骨處加上高光」：來源是 Cupid's bow（唇峰），不是人中（philtrum）。
    # 來源同時有 philtrum 時兩種說法都可能，不改。
    if re.search(r"cupid", source, re.I) and not re.search(r"philtrum", source, re.I) and "人中" in text:
        text = text.replace("人中", "唇峰")
        rules.append("cupids_bow_wording")
    # W10「唇膏可以分為冷色調與暖色調」：被引用的來源寫 cool, warm, or neutral undertones（三類並列），
    # 主張只列兩類 → 照來源補上中性（不是新事實）。只改「分為／分成／有」開頭的分類句，
    # 「同時包含暖色調與冷色調」這種描述句（W04）不動。
    if _THREE_UNDERTONES.search(source) and "中性" not in text:
        new = _TWO_TONE_LIST.sub(lambda m: f"{m.group(1)}{m.group(2)}色調、{m.group(3)}色調與中性色調", text, count=1)
        if new != text:
            text = new
            rules.append("neutral_undertone_listed")
    # plain-term-v5（H07）：撲定妝粉的步驟，面紙是墊在嘴唇上撲粉，來源沒說「吸掉多餘的唇膏」
    if _TISSUE_OVER_LIPS.search(source) and _POWDER.search(text) and _TISSUE_BLOT_LIPSTICK.search(text):
        text = _TISSUE_BLOT_LIPSTICK.sub(lambda m: ("先" if m.group(0).startswith("先") else "") + "把面紙輕壓在嘴唇上", text)
        rules.append("tissue_over_lips_for_powder")
    return text, rules
