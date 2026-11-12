"""回答後處理候選（修復 C，版本 C-pp-v1）：以完整主張為單位處理商品、驗引用與來源條件。

和既有流程的關係：
  - 不改 qa.py、不改 D4 v11 凍結檔；qa.py 的接線由 D 負責（contract.json answer_policy）。
  - 輸入是 answer_policy 組出的提示所得的模型原始輸出；也接受舊格式（citations: ["E1"]）以重放保存回答。

處理順序（postprocess）：
  1. 引用：主張的 unit_id 必須在本次選用集合（kept_unit_ids）內；不在的 ID 拿掉，沒有剩下合法 ID 的主張不交付。
  2. 商品：主張含具體商品／色號名時，只有它是「附帶舉例」（括號內，或以「例如／像是／比如…」開頭的子句）才刪那一段；
     商品是主張本身的成分（可以嘗試 X、使用 X 可以…、X 能…）→ **整條不交付**，不改寫、不猜補主詞或效果。
     刪附帶舉例後，緊接著用代詞（它、其、這款…）指回商品的子句一併刪到句尾；
     刪完若剩下的子句全是從屬子句（為了…、如果…、當…時）而沒有主要子句，也整條不交付（H03 殘句）。
     沒有商品的句子完全不動，所以正常的中文省略主詞句不會被誤刪；判斷依據是「刪掉了什麼」，不是「有沒有主詞」。
  3. 使用者條件：使用者自述（例如「小麥色」）出現在主張裡、但被引用的來源原文沒有這個詞 → 這是把使用者條件套到來源條件上，
     整條不交付，並標「需要澄清」；底調未知時，主張不得以肯定句說使用者是暖底／冷底。
  4. 來源條件：主張顯示的適用條件一律取自被引用單位的 conditions（資料），不取模型文字；含商品名或超過 80 字的條件句不顯示；
     模型自己列的條件不在來源裡、或漏列來源條件，都記旗標給人核對（程式不判斷語意）。
  5. 內容角色：A 以整個單位標角色，同一單位可同時有做法句與商品句，所以只記旗標（只引用商品宣稱單位、引用研究觀察
     而模型沒寫支持範圍），不據以刪主張；商品句由第 2 步處理。
  6. 選段標籤：引用「直接／條件式」單位 → role＝answer；只引用「背景」單位 → background，不算回答本題的方法，
     全部都是背景時狀態為 no_direct_answer；被引用單位沒有選段標籤（selector＝none 的向量名次裝箱、或標籤缺）→ unjudged，
     不當成直接、也不當成背景。
  7. 近似重複的主張只留第一條（dup-v2：只刪沒帶來新內容的後一條）。

avoid-v1（2026-10-26）：主張推薦使用者說不要的效果（不要太誇張／不要太鮮豔 → 大膽、濃豔、鮮明…；不要亮面 → 光澤）
  → 不列出（rule user_avoidance），系統註記「有內容和你說的「…」相反，沒有列出」；「不要太亮」意思未確認時，
  鮮明類不列出、光澤類保留並註記。詞前有否定／減弱（中和誇張效果）不算。

C-pp-v2（2026-10-26，Gemini 3.5 P05、N03）：
  - 商品步驟保留：一般色彩描述加品類（「櫻桃紅色唇膏」）不是具體商品，只記旗標 generic_color_category_mention、不刪；
    具體商品是步驟的工具（「用 X 填滿唇中央」）時，商品名換成品類（唇膏），同一句的商品宣稱子句（16 小時不褪色…）刪掉，
    保留步驟；商品不是步驟工具（可以嘗試 X、使用 X 可以打造…）仍整句不交付。商品效果不會說成一般效果。
  - 條件夾原文：主張文字裡原樣出現英文條件、或寫成「對於如果…」→ 旗標 condition_copied_into_text（不改寫文字）。

C-pp-v3（2026-10-27，T4 判讀 W04、W01）：模型標 insufficient 但附了主張時，不再整批丟掉——主張照一般規則檢查
  （引用、商品、使用者自述、條件），通過的照樣交付，狀態 delivered_partial，系統註記「現有來源只能回答一部分」；
  全部沒通過才是 source_insufficient。W04 兩個參考片段都送入，模型仍標不足，有依據的 MLBB 說明因此整題消失。
  沒有附主張的 insufficient 照舊是 source_insufficient。

C-pp-v4（2026-10-27，T4 新程式重跑 M01）：主張用使用者自述（小麥色）當適用對象、來源原文沒有這個詞時，
  若被引用的來源原文有對應的英文膚色分級（小麥 → tan skin，對照表沿用 need_relevance._SKIN_TERMS），視為同義詞
  （2026-10-27 決定 tan skin 與小麥色肌膚當同義），主張照原文交付，整份回答第一次出現自述詞的地方
  加「（來源寫 tan skin）」供追溯；不加註記、不補問。規範 5 禁止的是把小麥色推定成暖底調，不是膚色詞的翻譯。
  對不到來源分級（來源沒寫任何對應的膚色詞）照舊移除。need-rel-v3 讓 tan 片段送進模型後，M01 三句全被移除、整題不答。

程式能證明的只有「追溯與格式」（trace_check）；主張是否被來源語意支持（content_support）一律 not_verified，由人讀全文判定。
C-pp-v5（2026-10-28，配合 C-ap-v8 轉述）：模型輸出可帶 restatement（重組使用者原話的一句描述，不引用來源）。
reconcile 核對它只用使用者原話的字：問題原文沒有的漢字超過 3 成就拿掉（rule＝restatement_not_from_user），
避免模型借轉述加入事實；交付時第一行標「依你的描述整理（不是來源內容）」。
"""
from __future__ import annotations

import json
import re
from . import answer_tasks
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .need_relevance import _SKIN_TERMS

POSTPROCESS_VERSION = "C-pp-v5"
PARTIAL_NOTE = "模型表示現有來源只能回答這題的一部分；以下只列有來源支持的部分。"

# 選段標籤（contract.json evidence_selection）：直接、條件式算方法；背景不占方法位置
METHOD_LABELS = frozenset({"直接", "條件式"})
BACKGROUND_LABEL = "背景"
ROLE_PRODUCT_CLAIM = "商品宣稱"
ROLE_RESEARCH = "研究觀察"

MAX_CONDITION_CHARS = 80      # 顯示用條件句長度上限；A 第一批有 94 個單位的條件句超過 80 字（多為整句）
# limit-v2：改白話，不寫「主張／交付」等內部用語
PRODUCT_NOTE = "有幾句在講具體商品，商品功能還沒開放，所以先拿掉；以下只保留色系方向。"
# product-note-v2（2026-11-08，第十四輪 LM35-I02）：只刪掉句子裡附帶舉例的商品名、句子本身都還在時，
# 不能說「有幾句…先拿掉」（使用者會以為少了內容）；改成只說拿掉了商品名。
PRODUCT_NAME_ONLY_NOTE = "回答裡舉例的商品名先拿掉了（商品功能還沒開放），內容照常保留。"
PRODUCT_NAME_ONLY_REASON = "只刪除附帶舉例的商品／色號名"
# product-ref-v1（2026-11-01 商品界線）：品牌色當顏色參考可以提，推薦購買特定商品要攔
# product-ref-v2（2026-11-07）：整則回答最多保留 3 個商品品項，色號／色碼不算
PRODUCT_REF_VERSION = "product-ref-v4"   # v3：「建議選擇／推薦」某支商品也算推薦；v4：商品名前面要寫得出顏色
PRODUCT_REF_MAX_ITEMS = 3
_COLOR_CODE = re.compile(r"^#?[0-9A-Fa-f]{6}$|^\d{1,4}[A-Za-z]?$|^[A-Za-z]{1,3}\s?\d{1,4}$")


def product_items(names: Sequence[str]) -> list[str]:
    """product-ref-v2：要算進「品項」的名稱——去掉色碼（#ed2600、212、MA 012 這類編號），同名只算一次。"""
    return list(dict.fromkeys(n for n in names if not _COLOR_CODE.match(n.strip())))
PRODUCT_REF_NOTE = "提到的商品或色號是句末出處（品牌）舉的例子，名稱前面寫的是它的顏色；只供顏色參考，不是購買推薦。"
# 同一則回答既拿掉了叫人買的商品句、又保留了當參考的商品名時，兩句說明合成一句（2026-11-08，M07 兩句說明互相矛盾）
PRODUCT_MIXED_NOTE = ("有幾句在叫人買具體商品，商品功能還沒開放，所以先拿掉；留下的商品或色號是句末出處（品牌）舉的例子，"
                      "名稱前面寫的是它的顏色，只供顏色參考，不是購買推薦。")

# product-ref-v4（2026-11-08，prodref 的 W05、M07）：
# 當參考保留的商品名，前面一定要有顏色描述，使用者才知道它是什麼顏色。
# - 中文顏色字已經在名稱前面（「淺粉色的 Icon Baby」）→ 照留。
# - 來源原文的英文顏色形容詞（light-pink Icon Baby）→ 依下表翻成中文顏色（淺粉色的 Icon Baby）；有一個字查不到就不翻。
# - 前面沒有顏色（W05「極端暖色調（如來源舉的例子：Icon Baby）」，原文只寫 truly warm like Icon Baby）→ 不當參考保留，
#   照商品原本的處理拿掉商品名。品牌由句末出處交代（出處就是品牌官網文章）。
_EN_COLOR_WORDS = {
    "light": "淺", "pale": "淺", "soft": "柔和", "deep": "深", "dark": "深", "bright": "亮", "rich": "濃",
    "peachy": "蜜桃", "peach": "蜜桃", "ruby": "寶石", "candy": "糖果", "cherry": "櫻桃", "berry": "莓",
    "nude": "裸", "pink": "粉", "red": "紅", "coral": "珊瑚", "orange": "橘", "brown": "棕", "plum": "梅子",
    "mauve": "藕", "rose": "玫瑰", "rosy": "玫瑰", "beige": "米", "purple": "紫", "berry-red": "莓紅",
}
_EN_COLOR_BEFORE = re.compile(r"([A-Za-z]+(?:-[A-Za-z]+)*)\s*$")
# 看得出色相的字：去掉「色號、色調、色澤…」這種不是在講顏色的詞以後，句子裡還要有這些字
_NOT_HUE = re.compile(r"色號|色調|色澤|色彩|顏色|上色|試色|色系|膚色|氣色|唇色|口紅|腮紅|紅潤|紅腫")   # 「口紅」的紅不是顏色（W05 T4 實跑）
_HUE = re.compile(r"[紅粉橘棕裸紫莓杏桃梅]|珊瑚|豆沙|玫瑰|奶茶|焦糖|酒紅|磚紅|米色|藕")


def product_ref_with_color(text: str, names: Sequence[str]) -> str | None:
    """product-ref-v4：當參考保留的商品句要看得出顏色，回傳（英文顏色翻成中文後的）主張文字；看不出就回 None
    （呼叫端改走原本的商品處理）。
    1. 名稱前面是來源原文的英文顏色形容詞（light-pink Icon Baby）→ 照 _EN_COLOR_WORDS 翻成「淺粉色的 Icon Baby」。
    2. 整句去掉商品名後，要有色相字（粉、紅、裸、豆沙…）；「色調、色澤、色號」不算。
       N02「…的 Worth It，這是一款粉色裸色」算；W05「極端暖色調…冷色調」、M09「帶有色澤的唇油」不算。"""
    out = text
    for name in names:
        i = out.casefold().find(name.casefold())
        if i < 0:
            continue
        before = out[:i]
        m = _EN_COLOR_BEFORE.search(before)
        if m:
            parts = m.group(1).casefold().split("-")
            if all(p in _EN_COLOR_WORDS for p in parts):
                zh = "".join(_EN_COLOR_WORDS[p] for p in parts)
                out = f"{before[:m.start()]}{zh}色的 {out[i:]}"
    rest = out
    for name in names:
        rest = re.sub(re.escape(name), "", rest, flags=re.I)
    if not _HUE.search(_NOT_HUE.sub("", rest)):
        return None
    return out


# 叫人買／找替代／問價格的說法：就算開了參考模式也照舊拿掉
# product-ref-v3（2026-11-07 prodref 40 題 N02「建議選擇 L’Oréal…的 Le Nude Admirable」）：建議／推薦某支商品也是推薦，不是參考
_BUY_CUE = re.compile(r"購買|買|入手|下單|價格|多少錢|平價|替代|代替品|推薦|建議(?:選擇|選用|使用|挑選|擦|試試|用)|必備|必入")


# ---------------------------------------------------------------------------
# 資料型別
# ---------------------------------------------------------------------------
@dataclass
class Claim:
    """一條模型主張：文字、引用的 unit_id、模型寫的支持範圍與適用條件（後兩者只作核對，不直接顯示）。"""

    text: str
    unit_ids: list[str]
    support_scope: str | None = None
    conditions: list[str] = field(default_factory=list)


@dataclass
class ParsedAnswer:
    """解析後的模型輸出；errors 非空代表整份不採用。"""

    insufficient: bool
    claims: list[Claim]
    limitations: list[str]
    clarifying_question: str | None
    raw: str
    errors: list[str] = field(default_factory=list)
    restatement: str | None = None      # C-pp-v5：重組使用者原話的描述（不是來源內容）


@dataclass(frozen=True)
class Policy:
    """後處理政策：本次選用的 unit_id、選段標籤、使用者明說的條件與品牌詞。

    kept_unit_ids 為 None 時以傳入的 units 為準（D 只傳本次送進回答模型的單位）。
    explicit_conditions 用 contract-v1 格式 [{field, value, values, label, raw, source}]；空的話使用者條件檢查不作用，
    所以應用端應以 answer_policy.policy_from(selection, explicit_conditions) 建立，DEFAULT_POLICY 只是後備。
    """

    kept_unit_ids: frozenset[str] | None = None
    selection_labels: Mapping[str, str] = field(default_factory=dict)   # unit_id → 直接｜條件式｜背景｜不確定
    selector: str | None = None        # "none"＝沒有選段判讀（E-028：依向量名次裝箱），標籤一律視為未判讀
    explicit_conditions: Sequence[Mapping[str, Any]] | Mapping[str, Any] = ()
    brand_terms: tuple[str, ...] = ()
    max_claims: int = 8
    display_source_conditions: bool = False   # A 條件欄位經 E 認可前，交付文字不顯示來源條件（資料仍保留在 claims）
    # product-ref-v1：True＝商品或色號名可以當顏色參考留在主張裡（不推薦購買）；由應用依開關與過敏狀態決定
    product_reference: bool = False
    version: str = POSTPROCESS_VERSION


DEFAULT_POLICY = Policy()      # 後備：引用以傳入 units 為準、沒有使用者條件與選段標籤


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_ALLOWED_TOP = {"insufficient", "restatement", "claims", "limitations", "clarifying_question"}
# field-echo-v1（2026-11-07 exp4 P08）：單行 JSON 文法下模型偶爾把欄位名稱當成一條限制寫出
# （"limitations": [..., "clarifying_question"]），會原樣顯示給使用者；整條就是欄位名稱的限制拿掉。
OUTPUT_FIELD_NAMES = frozenset(_ALLOWED_TOP | {"text", "unit_ids", "citations", "conditions", "support_scope"})


def _str_list(v: Any) -> list[str] | None:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        return None
    return [x.strip() for x in v if x.strip()]


class ClaimList(list):
    """parse_claims 的回傳：本身是 Claim 清單（contract），另帶 .answer（完整 ParsedAnswer），
    讓只傳主張清單的呼叫端（D 的 qa.py）在 postprocess 時不會丟掉限制、澄清問題與原始輸出。"""

    answer: ParsedAnswer


# 解析模型輸出成 ParsedAnswer。label_map 把舊格式的 E1、E2… 對回 unit_id（重放保存回答用）。
def parse_answer(raw_text: str, *, label_map: Mapping[str, str] | None = None) -> ParsedAnswer:
    m = _FENCE_RE.match(raw_text or "")
    body = m.group(1) if m else (raw_text or "")
    empty = ParsedAnswer(False, [], [], None, raw_text or "")
    try:
        obj = json.loads(body)
    except ValueError:
        empty.errors.append("not_json")
        return empty
    if not isinstance(obj, dict):
        empty.errors.append("not_object")
        return empty
    errors = [f"unexpected_field:{k}" for k in sorted(set(obj) - _ALLOWED_TOP)]
    insufficient = obj.get("insufficient", False)
    if not isinstance(insufficient, bool):
        errors.append("insufficient_not_bool")
        insufficient = False
    claims: list[Claim] = []
    raw_claims = obj.get("claims", [])
    if not isinstance(raw_claims, list):
        errors.append("claims_not_list")
        raw_claims = []
    for i, c in enumerate(raw_claims, 1):
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
            errors.append(f"claim_shape:{i}")
            continue
        ids = _str_list(c.get("unit_ids"))
        if c.get("unit_ids") is None and "citations" in c:          # 舊格式：citations 是本次標籤
            labels = _str_list(c.get("citations"))
            ids = None if labels is None else [(label_map or {}).get(x, x) for x in labels]
        conds = _str_list(c.get("conditions"))
        scope = c.get("support_scope")
        if ids is None or conds is None or (scope is not None and not isinstance(scope, str)):
            errors.append(f"claim_field_types:{i}")
            continue
        claims.append(Claim(text=c["text"].strip(), unit_ids=list(dict.fromkeys(ids)),
                            support_scope=(scope or "").strip() or None, conditions=conds))
    lims = _str_list(obj.get("limitations"))
    if lims is None:
        errors.append("limitations_types")
        lims = []
    lims = [x for x in lims if x not in OUTPUT_FIELD_NAMES]   # field-echo-v1
    q = obj.get("clarifying_question")
    if q is not None and not isinstance(q, str):
        errors.append("clarifying_question_type")
        q = None
    rs = obj.get("restatement")
    if rs is not None and not isinstance(rs, str):
        errors.append("restatement_type")
        rs = None
    return ParsedAnswer(insufficient, claims, lims, (q or "").strip() or None, raw_text, errors,
                        restatement=(rs or "").strip() or None)


# contract.json 的 parse_claims：回主張清單（ClaimList，帶完整解析結果）。
def parse_claims(raw_text: str, *, label_map: Mapping[str, str] | None = None) -> ClaimList:
    answer = parse_answer(raw_text, label_map=label_map)
    out = ClaimList(answer.claims)
    out.answer = answer
    return out


# ---------------------------------------------------------------------------
# 單位與使用者條件的兩種形狀
# ---------------------------------------------------------------------------
def _db_locator(u: Mapping[str, Any]) -> str | None:
    ps, pe = u.get("page_start"), u.get("page_end")
    if ps is not None:
        return f"p.{ps}" if pe in (None, ps) else f"p.{ps}–{pe}"
    return u.get("html_locator")


def unit_view(u: Mapping[str, Any]) -> dict:
    """單位統一成同一形狀：A 的 support_unit（欄位在頂層、text），或 D 的資料庫列（quote_text＋support_unit 子物件）。"""
    su = u.get("support_unit") if isinstance(u.get("support_unit"), Mapping) else {}

    def get(k: str) -> Any:
        return u[k] if u.get(k) is not None else su.get(k)

    return {"unit_id": u.get("unit_id") or u.get("chunk_key") or su.get("unit_id"),
            "text": u.get("text") or u.get("quote_text") or su.get("text") or "",
            "conditions": get("conditions") or [], "product_mentions": get("product_mentions"),
            "content_role": get("content_role"), "support_scope": get("support_scope"),
            "title": u.get("title") or get("source_id"), "locator_primary": get("locator_primary") or _db_locator(u),
            "section_original": get("section_original") or u.get("section")}


SKIN_FIELDS = ("skin_tone", "natural_lip")


def normalize_conditions(conds: Any) -> list[dict]:
    """使用者明說條件統一成 contract-v1 清單 [{field, value, values, label, raw, source}]。

    舊版 C 的 dict 格式（self_descriptions、undertone）轉成同格式（source＝legacy_c），只為重放舊資料。
    """
    if not conds:
        return []
    if isinstance(conds, Mapping):
        out = [{"field": "skin_tone", "value": t, "values": None, "label": None, "raw": t, "source": "legacy_c"}
               for t in conds.get("self_descriptions") or []]
        if conds.get("undertone") not in (None, "", "unknown"):
            out.append({"field": "undertone", "value": conds["undertone"], "values": None, "label": None,
                        "raw": None, "source": "legacy_c"})
        return out
    return [dict(c) for c in conds if isinstance(c, Mapping) and c.get("field")]


def condition_value(conds: Any, field_name: str) -> Any:
    return next((c.get("value") for c in normalize_conditions(conds) if c["field"] == field_name), None)


# ---------------------------------------------------------------------------
# 商品指涉
# ---------------------------------------------------------------------------
_MARKS_RE = re.compile(r"[™®©]")
# 兩個以上首字大寫的拉丁字詞連在一起（中間可夾 in／of／de／&），例如 L’Oréal Paris … Lipstick in Le Nude Admirable
_LATIN_NAME_RE = re.compile(
    r"[A-Z][\w'’.&-]*(?:\s+(?:(?:in|of|de|&)\s+)?(?:[A-Z][\w'’.&-]*|\d+[A-Za-z]+))+")
_ZH_MARKED_RE = re.compile(r"([一-鿿]{2,24})\s*[™®]")       # 中文品名後接 ™／®
_ZH_SHADE_RE = re.compile(r"「([^」]{1,20})」\s*(?:色號|色)")         # 「激裸色」色號


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'")
    s = _MARKS_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


# 從來源原文找商品／色號指涉（A 尚未提供 product_mentions 時的後備；結果標 detector=fallback）。
def source_product_mentions(text: str) -> list[str]:
    out: list[str] = []
    for m in _LATIN_NAME_RE.finditer(text or ""):
        out.append(m.group(0).strip(" .,"))
    out += [m.group(1) for m in _ZH_MARKED_RE.finditer(text or "")]
    out += [m.group(1) for m in _ZH_SHADE_RE.finditer(text or "")]
    return list(dict.fromkeys(x for x in out if x))


def unit_product_mentions(unit: Mapping[str, Any]) -> tuple[list[str], str]:
    """單位的商品指涉＝A 契約欄位 product_mentions ∪ 原文後備偵測。

    A 第一批（export_9d966b8）中文品名有漏（例如 PR025_h0055「超持久霧感液態唇膏™」的欄位是 []），
    所以 [] 不當作「確認沒有」；偵測來源記在 detector（contract／fallback／contract+fallback）。
    """
    pm = [x for x in (unit.get("product_mentions") or []) if isinstance(x, str) and x.strip()]
    fb = [x for x in source_product_mentions(unit.get("text", "")) if _norm(x) not in {_norm(y) for y in pm}]
    det = "+".join(k for k, v in (("contract", unit.get("product_mentions") is not None), ("fallback", bool(fb))) if v)
    return pm + fb, det or "none"


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    name: str
    level: str      # definite｜suspect


# 主張中的商品片段：與來源指涉（或品牌詞）相符者為 definite；主張裡另有拉丁專名但對不上來源者為 suspect。
def product_spans(text: str, mentions: Iterable[str], brand_terms: Iterable[str] = ()) -> list[_Span]:
    spans: list[_Span] = []
    low = text.casefold().replace("’", "'")
    names = sorted({m for m in list(mentions) + list(brand_terms) if m and len(_norm(m)) >= 2}, key=len, reverse=True)
    taken: list[tuple[int, int]] = []
    for name in names:
        key = _MARKS_RE.sub("", name).casefold().replace("’", "'").strip()
        for m in re.finditer(re.escape(key), low):
            a, b = m.start(), m.end()
            if any(a < y and x < b for x, y in taken):
                continue
            # 主張可能寫商品全名後再接 ™／® 或色號 in X，向後吃掉緊接的同名延伸
            ext = _LATIN_NAME_RE.match(text, a)
            if ext and ext.end() > b:
                b = ext.end()
            b += len(re.match(r"[™®]*", text[b:]).group(0))
            taken.append((a, b))
            spans.append(_Span(a, b, text[a:b], "definite"))
    norm_mentions = [_norm(x) for x in mentions]
    for m in _LATIN_NAME_RE.finditer(text):
        a, b = m.start(), m.end()
        if any(a < y and x < b for x, y in taken):
            continue
        nm = _norm(m.group(0))
        level = "definite" if any(nm in x or x in nm for x in norm_mentions if len(x) >= 4) else "suspect"
        taken.append((a, b))
        spans.append(_Span(a, b, m.group(0), level))
    return sorted(spans, key=lambda s: s.start)


# ---------------------------------------------------------------------------
# 子句結構（以完整主張為單位的刪改判斷）
# ---------------------------------------------------------------------------
_CLAUSE_END = "，,；;。：:！!？?"
_SENT_END = "。！!？?"
EXAMPLE_START = re.compile(r"^\s*(?:例如|像是|比如|譬如|好比|包括|如(?![果何此同今])|e\.g\.)")
PRONOUN_START = re.compile(r"^\s*(?:它們?|這款|該款|此款|這支|此支|該支|這個產品|此產品|該產品|該商品|其(?![他實中次餘]))")
SUBORDINATE_START = re.compile(r"^\s*(?:為了|如果|若|假如|要是|假設|當|只要|一旦|以便|以免|由於|因為|雖然)")
SUBORDINATE_END = re.compile(r"(?:時|的話|之後|以前|之前)\s*$")


def _paren_ranges(text: str) -> list[tuple[int, int]]:
    out, stack = [], []
    for i, ch in enumerate(text):
        if ch in "（(":
            stack.append(i)
        elif ch in "）)" and stack:
            a = stack.pop()
            if not stack:
                out.append((a, i + 1))
    return out


def _clauses(text: str) -> list[tuple[int, int]]:
    """切成子句（括號內的標點不切），回傳 [start, end)；end 含該子句結尾標點。"""
    parens = _paren_ranges(text)
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _CLAUSE_END and not any(a < i < b for a, b in parens):
            out.append((start, i + 1))
            start = i + 1
    if start < len(text):
        out.append((start, len(text)))
    return [(a, b) for a, b in out if text[a:b].strip(" " + _CLAUSE_END)]


def _sentence_end(text: str, clauses: list[tuple[int, int]], k: int) -> int:
    """第 k 個子句所在句子的最後一個子句索引。"""
    j = k
    while j < len(clauses) - 1 and text[clauses[j][1] - 1] not in _SENT_END:
        j += 1
    return j


def _is_subordinate(clause: str) -> bool:
    c = clause.strip(" " + _CLAUSE_END)
    return bool(SUBORDINATE_START.match(c) or SUBORDINATE_END.search(c))


def _tidy(s: str) -> str:
    s = re.sub(r"\s+([，。；、])", r"\1", s)
    s = re.sub(r"[，,；;]\s*(?=[。！？]|$)", "", s)
    s = re.sub(r"^[，,；;。\s]+", "", s)
    s = re.sub(r"([，；])[，；]+", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


@dataclass
class ProductDecision:
    action: str                       # keep｜edit｜remove
    text: str
    removed_parts: list[str] = field(default_factory=list)
    reason: str | None = None
    names: list[str] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)
    residue: str | None = None        # 若照舊做法刪改會留下的文字（只供核對，不交付）
    generic: list[str] = field(default_factory=list)   # C-pp-v2：一般色彩描述＋品類（不是具體商品，不刪）


# C-pp-v2：品類名詞、一般色彩描述、步驟動詞與商品宣稱用語
_CATEGORY = r"(?:護唇膏|唇線筆|唇膏|口紅|唇釉|唇彩|唇蜜|唇油)"
_CATEGORY_END = re.compile(_CATEGORY + r"$")
_GENERIC_COLOR_RE = re.compile(r"(?:經典的?)?[一-鿿]{0,4}[紅粉橘棕裸紫莓玫珊瑚杏桃酒梅磚茶褐][一-鿿]{0,2}?色?的?"
                               r"(?:液態|霧面|霧感|滋潤|亮面|緞面)?" + _CATEGORY)
_STEP_VERB = re.compile(r"填|塗|擦|勾勒|描|畫|抹|疊|融合|按壓|輕拍|點在|暈開|推開")
_INSTRUMENT_BEFORE = re.compile(r"(?:用|使用|以|拿|改用)\s*$")
_PRODUCT_CLAIM_CUE = re.compile(r"\d+\s*小時|長效|持久|不褪色|不掉色|不脫色|無需補妝|免補妝|持妝|防水|全晚|全天")


def generic_color_mention(name: str) -> bool:
    """「櫻桃紅色唇膏」「深棕紅色液態唇膏」這種「顏色＋品類」、或只有品類名（護唇膏）的一般描述（沒有拉丁字、商標符號）→ True，不當具體商品。"""
    n = (name or "").strip()
    if not n or re.search(r"[A-Za-z™®]", n):
        return False
    # 只有品類名（N01：A 契約把「護唇膏」列成商品指涉）也是一般描述
    return bool(_GENERIC_COLOR_RE.fullmatch(n) or re.fullmatch(_CATEGORY, n))


def _instrument_category(text: str, clause: tuple[int, int], span: "_Span") -> str | None:
    """商品是步驟的工具（「用 X 填滿…」：前面緊接用／使用／以，同一子句後面有步驟動詞、沒有商品宣稱）→ 回傳要換成的品類名；否則 None。"""
    a, b = clause
    if not _INSTRUMENT_BEFORE.search(text[a:span.start]):
        return None
    after = text[span.end:b]
    # 商品名本身可能含「持久」等字（超持久霧感液態唇膏），商品宣稱只看商品名以外的文字
    if not _STEP_VERB.search(after) or _PRODUCT_CLAIM_CUE.search(text[a:span.start] + after):
        return None
    m = _CATEGORY_END.search(span.name)
    return m.group(0) if m else "唇彩"


# 一個句子對商品指涉的處理：只刪附帶舉例；商品是步驟工具 → 換成品類、刪商品宣稱子句；其他成分或刪後沒有主要子句 → 整句移除。
def _decide_sentence(text: str, mentions: Iterable[str], brand_terms: Iterable[str] = ()) -> ProductDecision:
    spans = product_spans(text, mentions, brand_terms)
    generic = [s.name for s in spans if s.level == "definite" and generic_color_mention(s.name)]
    definite = [s for s in spans if s.level == "definite" and not generic_color_mention(s.name)]
    suspects = [s.name for s in spans if s.level == "suspect"]
    if not definite:
        return ProductDecision("keep", text, suspects=suspects, generic=generic)
    names = [s.name for s in definite]
    parens = _paren_ranges(text)
    clauses = _clauses(text)
    cut: list[tuple[int, int]] = []
    replace: list[tuple[int, int, str]] = []      # 步驟工具：(start, end, 品類名)
    for s in definite:
        par = next(((a, b) for a, b in parens if a <= s.start and s.end <= b), None)
        if par:                                           # 括號內的附帶舉例：只刪括號
            cut.append(par)
            nxt = text[par[1]:].lstrip("，,；; ")
            if PRONOUN_START.match(nxt):
                k = next((i for i, (a, b) in enumerate(clauses) if a <= par[1] < b or a >= par[1]), None)
                if k is not None:
                    cut.append((par[1], clauses[_sentence_end(text, clauses, k)][1]))
            continue
        k = next((i for i, (a, b) in enumerate(clauses) if a <= s.start < b), None)
        category = None if k is None else _instrument_category(text, clauses[k], s)
        if category is not None:                          # 步驟的工具：只換掉商品名，步驟保留
            replace.append((s.start, s.end, category))
            continue
        if k is None or not EXAMPLE_START.match(text[clauses[k][0]:clauses[k][1]]):
            return ProductDecision("remove", text, reason="商品是句子本身的成分（不是附帶舉例），整句不交付、不改寫",
                                   names=names, suspects=suspects, generic=generic)
        a, b = clauses[k]
        if k + 1 < len(clauses) and text[b - 1] not in _SENT_END \
                and PRONOUN_START.match(text[clauses[k + 1][0]:clauses[k + 1][1]]):
            b = clauses[_sentence_end(text, clauses, k + 1)][1]     # 代詞指回商品：刪到句尾
        cut.append((a, b))
    if replace:
        # 換掉商品名的句子裡，講商品效果（持妝 16 小時、不褪色…）的子句一併刪掉，不能變成一般效果
        cut += [(a, b) for a, b in clauses if _PRODUCT_CLAIM_CUE.search(text[a:b])
                and not any(a <= x < b for x, _, _ in replace)]
    pieces, i = [], 0
    while i < len(text):
        rep = next((r for r in replace if r[0] == i), None)
        if rep is not None:
            pieces.append(rep[2])
            i = rep[1]
        elif any(a <= i < b for a, b in cut):
            i += 1
        else:
            pieces.append(text[i])
            i += 1
    removed = [text[a:b] for a, b in sorted(set(cut))] + [text[a:b] for a, b, _ in replace]
    after = _tidy("".join(pieces))
    if text.rstrip().endswith("。") and after and not after.endswith(("。", "！", "？")):
        after += "。"
    rest = [after[a:b] for a, b in _clauses(after)]
    if not rest or len(re.sub(r"[^一-鿿A-Za-z0-9]", "", after)) < 6:
        return ProductDecision("remove", text, removed, "刪除商品舉例後剩下的內容不足以成為完整句子，整句不交付", names, suspects, after,
                               generic)
    if all(_is_subordinate(c) for c in rest):
        return ProductDecision("remove", text, removed, "刪除商品後只剩從屬子句、沒有主要子句（殘句），整句不交付", names, suspects, after,
                               generic)
    if any(PRONOUN_START.match(c.strip(" " + _CLAUSE_END)) for c in rest):
        return ProductDecision("remove", text, removed, "刪除商品後仍有代詞開頭的子句，指涉不明，整句不交付", names, suspects, after,
                               generic)
    why = ("商品是步驟的工具：商品名換成品類、刪除商品宣稱子句，只保留步驟" if replace
           else PRODUCT_NAME_ONLY_REASON)
    return ProductDecision("edit", after, removed, why, names, suspects, generic=generic)


# 接在被整句移除的句子後面、依賴它的句子（代詞指回、接續步驟、承接結果）也一併移除
DEPENDENT_SENTENCE_START = re.compile(
    PRONOUN_START.pattern + r"|^\s*(?:這樣|如此|這麼做|這個方法|此方法|然後|接著|再|之後|最後|接下來|因此|所以|同樣)")


def _sentences(text: str) -> list[str]:
    parens = _paren_ranges(text)
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _SENT_END and not any(a < i < b for a, b in parens):
            out.append(text[start:i + 1])
            start = i + 1
    if text[start:].strip():
        out.append(text[start:])
    return [x for x in out if x.strip()]


# 決定一條主張對商品指涉的處理。移除單位是「完整句子」或整條主張，不留半句：
# 每句各自判斷（只刪附帶舉例；商品是句子成分 → 整句移除）；被移除句後面依賴它的句子一併移除；全部移除 → 整條不交付。
def decide_product(text: str, mentions: Iterable[str], brand_terms: Iterable[str] = ()) -> ProductDecision:
    mentions = list(mentions)
    parts, removed, names, suspects, reasons, residues, generic = [], [], [], [], [], [], []
    dropping = rewritten = False
    for sent in _sentences(text):
        if dropping and DEPENDENT_SENTENCE_START.match(sent):
            removed.append(sent)
            continue
        if rewritten and PRONOUN_START.match(sent):     # 商品名換成品類後，「它…」指的仍是那個商品，一併刪
            removed.append(sent)
            continue
        d = _decide_sentence(sent, mentions, brand_terms)
        names += d.names
        suspects += d.suspects
        generic += [g for g in d.generic if g not in generic]
        dropping = d.action == "remove"
        rewritten = d.action == "edit" and (d.reason or "").startswith("商品是步驟的工具")
        if d.action == "remove":
            removed.append(sent)
            reasons.append(d.reason)
            if d.residue:
                residues.append(d.residue)
        else:
            parts.append(d.text)
            removed += d.removed_parts
            if d.action == "edit":
                reasons.append(d.reason)
    if not names:
        return ProductDecision("keep", text, suspects=suspects, generic=generic)
    if not parts:
        return ProductDecision("remove", text, removed, "；".join(dict.fromkeys(reasons)) + "（整條不交付）", names, suspects,
                               "".join(residues) or None, generic)
    return ProductDecision("edit", "".join(parts).strip(), removed, "；".join(dict.fromkeys(reasons)), names, suspects,
                           generic=generic)


# ---------------------------------------------------------------------------
# 使用者條件與來源條件
# ---------------------------------------------------------------------------
_COND_PREFIX = re.compile(r"(?:如果|若|假如|要是|假設|對於|針對)")
_UNDERTONE_ASSERT = re.compile(r"(?:你|您)(?:的)?(?:膚色|皮膚|底調)?(?:是|屬於|屬|偏|為)(?:偏)?(?:暖|冷|中性)(?:色)?(?:調|底|皮)")


def user_terms(explicit_conditions: Any) -> list[str]:
    """使用者自述的膚色／唇色用語，例如「小麥色」（skin_tone、natural_lip 的 value／values）。"""
    out: list[str] = []
    for c in normalize_conditions(explicit_conditions):
        if c["field"] in SKIN_FIELDS:
            for v in [c.get("value")] + list(c.get("values") or []):
                if isinstance(v, str) and len(v.strip()) >= 2 and v not in out:
                    out.append(v)
    return out


def _unknown(v: Any) -> bool:
    return v is None or v == "" or v == "unknown"


def user_condition_violation(text: str, unit_texts: Sequence[str], explicit_conditions: Any) -> str | None:
    """主張把使用者自述套到來源條件、或在底調未知時斷言使用者底調 → 回傳原因。"""
    src = " ".join(unit_texts)
    for t in user_terms(explicit_conditions):
        if t in text and t not in src:
            return f"主張用使用者自述「{t}」當適用對象，但被引用的來源原文沒有這個詞；來源條件與使用者條件的對應未經來源支持"
    if _unknown(condition_value(explicit_conditions, "undertone")):
        for a, b in _clauses(text):
            clause = text[a:b]
            m = _UNDERTONE_ASSERT.search(clause)
            if m and not _COND_PREFIX.search(clause[:m.start()]):
                return "使用者沒有說明底調，主張卻以肯定句說明使用者的底調"
    return None


_SKIN_NOUN = r"(?:skin(?:\s+tones?)?|complexions?)"


def source_skin_term(term: str, unit_texts: Sequence[str]) -> str | None:
    """C-pp-v4：使用者自述（小麥色）在被引用來源原文裡對應的英文膚色分級（tan skin）；對不到回 None。"""
    words = [w for pat, ws in _SKIN_TERMS if pat.search(term) for w in ws]
    src = " ".join(unit_texts)
    for w in words:
        if re.search(rf"\b{w}\b[\s,]*(?:(?:and|or|,)\s*\w+[\s,]*)*{_SKIN_NOUN}\b", src, re.I):
            return f"{w} skin"
    return None


def annotate_source_term(text: str, term: str, label: str) -> str:
    """第一次出現的自述詞（含後面的皮膚／膚色／肌膚）後面加「（來源寫 tan skin）」，其餘文字不動。"""
    return re.sub(rf"{re.escape(term)}(?:皮膚|膚色|肌膚)?", lambda m: f"{m.group(0)}（來源寫 {label}）", text, count=1)


_LATIN_WORDS = re.compile(r"[A-Za-z][A-Za-z'’]*(?:\s+[A-Za-z][A-Za-z'’]*)+")
_COND_STACKED = re.compile(r"對於\s*(?:如果|若|假如|要是)")


def condition_copied_into_text(text: str, conditions: Sequence[str]) -> list[str]:
    """C-pp-v2：主張文字原樣貼了英文條件（至少兩個英文字）、或寫成「對於如果…」→ 回傳有問題的條件（或該片段）；沒有回 []。"""
    low = _cond_norm(text)
    out = [x for x in conditions if _LATIN_WORDS.search(x) and _cond_norm(x) in low]
    m = _COND_STACKED.search(text)
    if m and not out:
        out.append(text[m.start():m.end() + 8])
    return out


# ---------------------------------------------------------------------------
# avoid-v1（2026-10-26，P05「不要太誇張」卻給「大膽鮮豔」、P03「不要太亮」卻給濃豔紅唇）
# ---------------------------------------------------------------------------
AVOID_VERSION = "avoid-v1"
_VIVID_CLAIM = re.compile(r"大膽|鮮豔|濃豔|美豔|鮮明|鮮紅|搶眼|亮眼|誇張|浮誇|烈焰|豔紅|高飽和")
_GLOSS_CLAIM = re.compile(r"唇蜜|唇油|光澤|亮面|水光|透亮|亮澤")
# 詞前面 6 字內有否定或減弱（不、避免、中和、降低…）就不算在推薦這個效果（「紅棕色調能中和誇張效果」）
_WEAKEN_BEFORE = re.compile(r"(?:不|別|避免|少|降低|減少|減弱|中和|柔化|淡化|沖淡|收斂)[^，。；]{0,4}$")


def _recommends(pattern: re.Pattern, text: str) -> str | None:
    """主張文字裡有沒有「在推薦」這個效果的詞（前面沒有否定／減弱）；有就回傳那個詞。"""
    for m in pattern.finditer(text):
        if not _WEAKEN_BEFORE.search(text[max(0, m.start() - 6):m.start()]):
            return m.group(0)
    return None


def _said(c: Mapping[str, Any]) -> str:
    """使用者原話裡的那一段（去掉開頭的「但／可是」），沒有原話就用標籤。"""
    raw = (c.get("raw") or "").strip()
    return re.sub(r"^(?:但是?|可是|不過|，|,|\s)+", "", raw) or (c.get("label") or "")


def user_avoidance(text: str, explicit_conditions: Any) -> tuple[str, str, str] | None:
    """主張推薦了使用者說不要的效果 → 回傳 (處理, 使用者原話, 主張裡的詞)；處理是 remove（不列出）或 note（保留、加註記）。
    - 不想要的效果是鮮明亮眼（不要太誇張／不要太鮮豔）→ 推薦鮮明效果的主張 remove；
    - 不喜歡的妝感是亮面（不要亮面）→ 推薦光澤的主張 remove；
    - 「不要太亮」意思還沒確認（pending_ambiguity＝brightness）→ 推薦鮮明的 remove（兩種意思都不會想要濃豔），
      推薦光澤的 note（如果是指不要亮面才不適用，不能替使用者決定）。"""
    for c in normalize_conditions(explicit_conditions):
        f, v = c["field"], c.get("value")
        if f == "avoided_effects" and v == "vivid":
            hit = _recommends(_VIVID_CLAIM, text)
            if hit:
                return "remove", _said(c), hit
        if f == "disliked_finishes" and v == "gloss":
            hit = _recommends(_GLOSS_CLAIM, text)
            if hit:
                return "remove", _said(c), hit
        if f == "pending_ambiguity" and v == "brightness":
            hit = _recommends(_VIVID_CLAIM, text)
            if hit:
                return "remove", c.get("label") or "不要太亮", hit
            hit = _recommends(_GLOSS_CLAIM, text)
            if hit:
                return "note", c.get("label") or "不要太亮", hit
    return None


def _cond_norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip().casefold()


def usable_conditions(unit: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """單位的條件分成可用（給模型看、可顯示）與不用（含商品名或超過 80 字的整句；全文已在 text 裡）；大小寫重複只留一個。"""
    mentions, _ = unit_product_mentions(unit)
    shown: list[str] = []
    hidden: list[str] = []
    for x in unit.get("conditions") or []:
        if not isinstance(x, str) or not x.strip() or _cond_norm(x) in {_cond_norm(y) for y in shown + hidden}:
            continue
        if len(x) > MAX_CONDITION_CHARS or any(sp.level == "definite" for sp in product_spans(x, mentions)):
            hidden.append(x)
        else:
            shown.append(x)
    return shown, hidden


# ---------------------------------------------------------------------------
# 近似重複
# ---------------------------------------------------------------------------
ECHO_PAREN_VERSION = "echo-paren-v1"
_PAREN = re.compile(r"[（(]\s*([^（）()]{1,30}?)\s*[）)]")


def drop_echo_paren(text: str) -> tuple[str, list[str]]:
    """括號內容和緊接在括號前面的字完全相同（例：深紫紅色（深紫紅色））→ 拿掉整個括號；回傳（新文字, 拿掉的括號）。
    只比對完全相同的字，翻譯、補充說明（vibrant crimsons、即 MLBB）都保留。"""
    removed: list[str] = []

    def keep(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner and text[:m.start()].rstrip().endswith(inner):
            removed.append(m.group(0))
            return ""
        return m.group(0)

    out = _PAREN.sub(keep, text)
    return (out, removed) if removed else (text, [])


def _bigrams(s: str) -> set[str]:
    t = re.sub(r"[^一-鿿A-Za-z0-9]", "", s.casefold())
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def near_duplicate(a: str, b: str, threshold: float = 0.75, containment: float = 0.8) -> bool:
    """近似重複：字元二元組 Jaccard ≥ 0.75，或較短一條（至少 12 組）有 ≥ 80% 包含在另一條裡。

    門檻依 14 題＋R4 保存回答：重複組（M03）包含率 0.85～0.98，其餘所有主張配對最高 0.50。
    """
    x, y = _bigrams(a), _bigrams(b)
    inter = len(x & y)
    if inter / max(1, len(x | y)) >= threshold:
        return True
    small = min(len(x), len(y))
    return small >= 12 and inter / small >= containment


DUPLICATE_VERSION = "dup-v3"   # v2：只刪沒新內容的後一條；v3：前一條是縮短版就留長的


def repeats_delivered(new: str, kept: str, containment: float = 0.8) -> bool:
    """dup-v2：後面這條主張（new）是否只是重複已交付的那條（kept）——new 的字元二元組有 ≥ 80% 已出現在 kept 裡才算。

    為什麼改（2026-11-07）：
    舊規則 near_duplicate 是對稱的「較短一條 80% 包含在另一條」，會把資訊比較多的後一條刪掉：
      - M08 exp5：留「可以搭配深色唇線來使用」，刪掉「…搭配深色唇線，並將唇膏輕點在唇部中心…」（後一條新增 18 組）。
      - N11 exp4／exp5：留「綠色則可能為暖底」，刪掉「紫色或藍色則可能為冷底」（意思相反，只是句型相同）。
    新規則只看「後一條帶來多少新內容」：M03 那種後一條幾乎全被前一條包含的才刪（測試 test_m03_near_duplicates_removed）。
    """
    x, y = _bigrams(kept), _bigrams(new)
    return len(y) >= 6 and len(x & y) / len(y) >= containment


_FILLER = set("來使用即可的了地也就")      # 前一條和後一條比，只差這些字＝沒有自己的內容


def superseded_by(earlier: str, later: str) -> bool:
    """dup-v3：前一條（earlier）是否只是後一條（later）的縮短版——應該留長的那條。

    做法：去掉標點後逐字對齊（difflib），前一條沒對上的字只剩「來／使用／即可…」這類虛字，
    且後一條比較長，才算被取代。
    例：M08「可以搭配深色唇線來使用」被「可以搭配深色唇線，並將唇膏輕點在唇部中心…」取代；
    N11「綠色則可能為暖底」對「紫色或藍色則可能為冷底」沒對上的是「綠、暖」，是內容，不取代。
    """
    import difflib
    a = re.sub(r"[^一-鿿A-Za-z0-9]", "", earlier)
    b = re.sub(r"[^一-鿿A-Za-z0-9]", "", later)
    if len(a) < 12 or len(b) <= len(a):
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    left = [a[i] for i in range(len(a)) if not any(bl.a <= i < bl.a + bl.size for bl in sm.get_matching_blocks())]
    return matched >= 0.8 * len(a) and all(ch in _FILLER for ch in left)


# dup-v4（2026-11-08，prodref N09）：
#   「如果是小麥色或深膚色，選擇裸色時應選擇焦糖色或棕色調的裸色」／「如果是小麥色或深膚色，建議選擇棕色、莓色或焦糖色裸色」
#   字面不像（dup-v2／v3 都抓不到），但開頭條件相同、前一條的顏色全部包含在後一條裡 → 留顏色比較完整的那條。
#   很窄：兩條開頭條件（第一個逗號前）逐字相同且是條件句、各自至少 2 個顏色詞（只算「避免／不要」之前的正向部分；
#   條件後面一開頭就是反向說法的不比）。
COLOR_DUP_VERSION = "dup-v4"
_COLOR_WORD = re.compile(r"焦糖|豆沙|玫瑰|珊瑚|奶茶|酒紅|磚紅|[棕莓裸紅粉橘紫杏桃梅]")
_NEGATIVE = re.compile(r"避免|避開|不要|不適合|別|少用|不建議")
_LEAD_CONDITION = re.compile(r"^(?:如果|若|假如|對於|針對)[^，,]{2,20}[，,]")


def color_set_lead(text: str) -> tuple[str, frozenset[str]] | None:
    """回傳（開頭條件、條件後面提到的顏色詞集合）；不是條件句、有反向說法或顏色詞少於 2 個時回 None。"""
    m = _LEAD_CONDITION.match(text)
    if not m:
        return None
    # 只看「避免…」之前的正向部分（N09 T4：「建議選擇棕色、莓色或焦糖色裸色，並避免淺粉色…」）；
    # 開頭就是反向說法（如果是…，建議避免…）的不比
    body = text[m.end():]
    neg = _NEGATIVE.search(body)
    if neg and neg.start() < 4:
        return None
    colors = frozenset(_COLOR_WORD.findall(body[:neg.start()] if neg else body))
    return (m.group(0), colors) if len(colors) >= 2 else None


def color_subset(a: str, b: str) -> bool:
    """dup-v4：a 和 b 開頭條件相同，且 a 的顏色詞全部出現在 b 裡（b 比較完整或一樣）。"""
    x, y = color_set_lead(a), color_set_lead(b)
    return bool(x and y and x[0] == y[0] and x[1] <= y[1])


# undertone-known-v1（2026-11-08，prodref M03）：使用者已經說了自己的底調（我是冷底），
# 回答還在教怎麼用血管、珠寶、曬黑判斷底調 → 對這個問題沒有用，拿掉。使用者沒說底調時（P11、N11）不動。
UNDERTONE_KNOWN_VERSION = "undertone-known-v1"
_UNDERTONE_TEST = re.compile(r"血管|珠寶|金飾|銀飾|金色首飾|銀色首飾|曬黑|曬傷|白色對比|奶油色測試|白色.{0,6}奶油色")


def undertone_test_for_known(text: str, explicit_conditions: Any) -> bool:
    """使用者明說了冷／暖／中性底，而這條主張是在教怎麼判斷底調。"""
    return condition_value(explicit_conditions, "undertone") in ("cool", "warm", "neutral") and bool(
        _UNDERTONE_TEST.search(text))


VAGUE_VERSION = "vague-v1"
# 「搭配與其匹配的唇線筆」：「其」指原文裡某個商品（PR020「Pair it with the matching lip liner」），商品不交付後這句沒有內容
_VAGUE_PAIRING = re.compile(r"(與其|與之|和它)(相)?(匹配|相配|對應|配套|成套)的")
# 來源明寫例外（apart from／except…），主張卻寫「所有／全部」＝把例外吃掉（W09：除了淺橘、淺粉以外都有顯著差異）
_SOURCE_EXCEPTION = re.compile(r"\b(apart from|except|other than|with the exception of)\b", re.I)


def vague_or_overgeneral(text: str, unit_texts: Sequence[str]) -> str | None:
    """vague-v1：回傳要拿掉的理由；沒問題回 None。只抓兩種很窄的情況：
    1. 指向看不到的商品的空泛搭配（「搭配與其匹配的唇線筆」）。
    2. 來源寫明例外，主張卻說「所有」或「全部」。"""
    if _VAGUE_PAIRING.search(text):
        return "vague_product_pairing"
    if re.search(r"所有|全部", text) and any(_SOURCE_EXCEPTION.search(t or "") for t in unit_texts):
        return "drops_source_exception"
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def postprocess(answer: ParsedAnswer | Sequence[Claim], units: Mapping[str, Mapping[str, Any]], policy: Policy | None = None) -> dict:
    """處理解析後的回答，回傳 Delivered（contract.json）：claims、removed、flags、raw、mode，另含 status 與核對說明。"""
    policy = policy or DEFAULT_POLICY
    if not isinstance(answer, ParsedAnswer):
        answer = getattr(answer, "answer", None) or ParsedAnswer(False, list(answer), [], None, "")
    units = {k: unit_view(v) for k, v in units.items()}
    kept = set(units) if policy.kept_unit_ids is None else set(policy.kept_unit_ids)
    removed: list[dict] = []
    flags: list[dict] = []
    delivered: list[dict] = []
    if answer.errors:
        return _result("validation_failed", [], removed, [{"flag": "parse_errors", "detail": answer.errors}], answer, policy)
    if answer.insufficient and not answer.claims:
        return _result("source_insufficient", [], removed, flags, answer, policy)
    if answer.insufficient:      # C-pp-v3：附帶的主張照一般規則檢查，通過的交付為部分回答
        flags.append({"flag": "model_marked_insufficient", "claims": len(answer.claims)})
    for n, c in enumerate(answer.claims[:policy.max_claims], 1):
        rec = {"claim": asdict(c), "claim_no": n}
        ids = [u for u in c.unit_ids if u in kept and u in units]
        bad = [u for u in c.unit_ids if u not in ids]
        if bad:
            flags.append({"claim_no": n, "flag": "unit_id_not_in_kept_set", "unit_ids": bad})
        if not ids:
            removed.append({**rec, "reason": "沒有任何引用在本次選用的單位內", "rule": "trace"})
            continue
        cited = [units[u] for u in ids]
        text = c.text
        text, term_rules = answer_tasks.source_term_repairs(text, [u["text"] for u in cited])
        if term_rules:
            flags.append({"claim_no": n, "flag": "source_term_corrected", "version": answer_tasks.VERSION,
                          "term_version": answer_tasks.TERM_REPAIR_VERSION, "rules": term_rules,
                          "before": c.text, "after": text, "unit_ids": ids})
        # 2. 商品
        mentions: list[str] = []
        detectors = set()
        for u in cited:
            pm, det = unit_product_mentions(u)
            mentions += pm
            detectors.add(det)
        pd = decide_product(text, mentions, policy.brand_terms)
        product_ref = None
        if (policy.product_reference and pd.action in ("remove", "edit")
                and not _BUY_CUE.search(text) and not _PRODUCT_CLAIM_CUE.search(text)):
            # product-ref-v1：當顏色參考提到的商品／色號保留原句；叫人買、商品宣稱（持妝幾小時…）照舊處理
            # product-ref-v2：整則回答的品項（色碼不算）超過 3 個時，這條改回原本的處理（拿掉商品名或整句）
            used = product_items([x for d in delivered for x in d.get("product_reference") or []])
            total = product_items(used + list(pd.names))
            colored = product_ref_with_color(text, pd.names)
            if colored is None:
                # product-ref-v4：名稱前面看不出顏色 → 不當參考保留，照原本的商品處理
                flags.append({"claim_no": n, "flag": "product_reference_no_color", "version": PRODUCT_REF_VERSION,
                              "names": list(pd.names)})
            elif len(total) <= PRODUCT_REF_MAX_ITEMS:
                if colored != text:
                    flags.append({"claim_no": n, "flag": "product_reference_color_translated",
                                  "version": PRODUCT_REF_VERSION, "before": text, "after": colored})
                    text = colored
                product_ref = list(pd.names)
                flags.append({"claim_no": n, "flag": "product_reference_kept", "version": PRODUCT_REF_VERSION,
                              "names": product_ref})
                pd = ProductDecision("keep", text, suspects=pd.suspects, generic=pd.generic)
            else:
                flags.append({"claim_no": n, "flag": "product_reference_over_limit", "version": PRODUCT_REF_VERSION,
                              "names": list(pd.names), "already": used, "limit": PRODUCT_REF_MAX_ITEMS})
        if pd.suspects:
            flags.append({"claim_no": n, "flag": "suspect_product_name", "names": pd.suspects})
        if pd.generic:
            flags.append({"claim_no": n, "flag": "generic_color_category_mention", "names": pd.generic})
        if pd.action == "remove":
            removed.append({**rec, "reason": pd.reason, "rule": "product_whole_claim", "product_names": pd.names,
                            "residue_not_delivered": pd.residue,
                            "product_detector": sorted(detectors)})
            continue
        edit_log = None
        if pd.action == "edit":
            edit_log = {"before": text, "removed_parts": pd.removed_parts, "product_names": pd.names,
                        "reason": pd.reason, "product_detector": sorted(detectors)}
            text = pd.text
        # 2b. avoid-v1：使用者說不要的效果
        avoid = user_avoidance(text, policy.explicit_conditions)
        if avoid and avoid[0] == "remove":
            removed.append({**rec, "reason": f"主張推薦「{avoid[2]}」，和使用者說的「{avoid[1]}」相反，不列出",
                            "rule": "user_avoidance", "user_said": avoid[1], "term": avoid[2]})
            continue
        if avoid:
            flags.append({"claim_no": n, "flag": "may_conflict_pending_ambiguity", "user_said": avoid[1], "term": avoid[2]})
        # 3. 使用者條件
        unit_texts = [u.get("text", "") for u in cited]
        why = user_condition_violation(text, unit_texts, policy.explicit_conditions)
        rewrite_log = None
        if why:      # C-pp-v4：來源有對應的膚色分級 → 當同義詞，照原文交付（第一次出現加來源原詞），不整條移除
            for t in user_terms(policy.explicit_conditions):
                label = source_skin_term(t, unit_texts) if t in text and t not in " ".join(unit_texts) else None
                if label:
                    # 自述詞換成來源原詞後仍要通過檢查（例如底調未知時斷言使用者是暖底，照舊移除）
                    if not user_condition_violation(text.replace(t, label), unit_texts, policy.explicit_conditions):
                        new = text if any(f.get("flag") == "user_term_matched_source_term" for f in flags) \
                            else annotate_source_term(text, t, label)
                        rewrite_log = {"before": text, "user_term": t, "source_term": label}
                        text, why = new, None
                    break
        if rewrite_log:
            flags.append({"claim_no": n, "flag": "user_term_matched_source_term",
                          "user_term": rewrite_log["user_term"], "source_term": rewrite_log["source_term"]})
        if why:
            removed.append({**rec, "reason": why, "rule": "user_condition_inference"})
            flags.append({"claim_no": n, "flag": "needs_clarification", "user_terms": user_terms(policy.explicit_conditions)})
            continue
        # 5. 內容角色
        # A 以整個單位標內容角色；同一單位可同時有做法句與商品句（PR015_h0139），所以角色只記旗標、不據以刪主張，
        # 商品句本身由第 2 步的商品指涉處理
        roles = [u.get("content_role") for u in cited]
        if roles and all(r == ROLE_PRODUCT_CLAIM for r in roles):
            flags.append({"claim_no": n, "flag": "cites_only_product_claim_units"})
        elif ROLE_PRODUCT_CLAIM in roles:
            flags.append({"claim_no": n, "flag": "mixed_with_product_claim_unit"})
        # 7. 近似重複
        dup = next((d for d in delivered if repeats_delivered(text, d["text"])), None)   # dup-v2：只刪沒帶來新內容的後一條
        if dup is not None:
            removed.append({**rec, "reason": "與已交付主張近似重複", "rule": "duplicate", "kept_claim_no": dup["claim_no"],
                            "version": DUPLICATE_VERSION})
            continue
        # dup-v3：前一條只是這一條的縮短版 → 拿掉前一條、留這條
        short = next((d for d in delivered if superseded_by(d["text"], text)), None)
        if short is not None:
            delivered.remove(short)
            removed.append({"claim_no": short["claim_no"], "claim": {"text": short["text"], "unit_ids": short["unit_ids"]},
                            "reason": "被後面較完整的同一句取代", "rule": "duplicate_superseded",
                            "kept_claim_no": n, "version": DUPLICATE_VERSION})
        # dup-v4：開頭條件相同、顏色詞是包含關係 → 留顏色比較完整的那條
        if any(color_subset(text, d["text"]) for d in delivered):
            kept_no = next(d["claim_no"] for d in delivered if color_subset(text, d["text"]))
            removed.append({**rec, "reason": "和前面同條件的主張重複，顏色已包含在前一條", "rule": "duplicate_color_subset",
                            "kept_claim_no": kept_no, "version": COLOR_DUP_VERSION})
            continue
        shorter = next((d for d in delivered if color_subset(d["text"], text)), None)
        if shorter is not None:
            delivered.remove(shorter)
            removed.append({"claim_no": shorter["claim_no"], "claim": {"text": shorter["text"], "unit_ids": shorter["unit_ids"]},
                            "reason": "被後面同條件、顏色比較完整的主張取代", "rule": "duplicate_color_subset",
                            "kept_claim_no": n, "version": COLOR_DUP_VERSION})
        # undertone-known-v1：使用者已說底調，判斷底調的方法不交付
        if undertone_test_for_known(text, policy.explicit_conditions):
            removed.append({**rec, "reason": "使用者已經說了自己的底調，判斷底調的方法對這題沒有用",
                            "rule": "undertone_already_stated", "version": UNDERTONE_KNOWN_VERSION})
            continue
        # vague-v1：空泛商品搭配、吃掉來源例外的「所有」
        vague = vague_or_overgeneral(text, [u.get("text", "") for u in cited])
        if vague:
            removed.append({**rec, "reason": {"vague_product_pairing": "指向看不到的商品的空泛搭配，沒有可用內容",
                                              "drops_source_exception": "來源寫明例外，主張卻說所有／全部"}[vague],
                            "rule": vague, "version": VAGUE_VERSION})
            continue
        # 4. 來源條件（一律取資料；含商品名或過長的條件句不列入，只記旗標）
        src_conds: list[str] = []
        hidden: list[str] = []
        for u in cited:
            shown, hid = usable_conditions(u)
            src_conds += [x for x in shown if _cond_norm(x) not in {_cond_norm(y) for y in src_conds}]
            hidden += [x for x in hid if _cond_norm(x) not in {_cond_norm(y) for y in hidden}]
        if hidden:
            flags.append({"claim_no": n, "flag": "source_condition_not_displayed", "conditions": hidden})
        src_norm = {_cond_norm(x) for x in src_conds + hidden}
        extra = [x for x in c.conditions if _cond_norm(x) not in src_norm]
        missing = [x for x in src_conds + hidden if _cond_norm(x) not in {_cond_norm(y) for y in c.conditions}]
        if extra:
            flags.append({"claim_no": n, "flag": "model_condition_not_in_source", "conditions": extra})
        copied = condition_copied_into_text(text, c.conditions)
        if copied:
            flags.append({"claim_no": n, "flag": "condition_copied_into_text", "conditions": copied})
        if missing:
            flags.append({"claim_no": n, "flag": "source_condition_not_restated_by_model", "conditions": missing})
        multi = [i for i, u in zip(ids, cited) if len(u.get("conditions") or []) > 1]
        if multi:
            flags.append({"claim_no": n, "flag": "unit_has_multiple_conditions", "unit_ids": multi})
        if len({tuple(sorted(_cond_norm(x) for x in (u.get("conditions") or []))) for u in cited}) > 1:
            flags.append({"claim_no": n, "flag": "cites_units_with_different_conditions", "unit_ids": ids})
        scopes = [u.get("support_scope") for u in cited if u.get("support_scope")]
        if ROLE_RESEARCH in roles and not c.support_scope:
            flags.append({"claim_no": n, "flag": "research_scope_attached_from_unit"})
        # 6. 選段標籤
        labels = [None if policy.selector == "none" else policy.selection_labels.get(u) for u in ids]
        if any(x in METHOD_LABELS for x in labels):
            role = "answer"
        elif labels and all(x == BACKGROUND_LABEL for x in labels):
            role = "background"
        else:
            role = "unjudged"
        # echo-paren-v1（2026-11-06，重審 G01）：「深紫紅色（深紫紅色）」括號照抄前面的字 → 拿掉括號
        text, echoed = drop_echo_paren(text)
        if echoed:
            flags.append({"claim_no": n, "flag": "echo_paren_removed", "version": ECHO_PAREN_VERSION, "removed": echoed})
        out = {"claim_no": n, "text": text, "unit_ids": ids, "role": role,
               "source_conditions": src_conds, "source_support_scopes": scopes,
               "model_support_scope": c.support_scope, "model_conditions": c.conditions,
               "conditions": src_conds, "support_scope": c.support_scope,
               "trace_check": "unit_ids_in_kept_set", "content_support": "not_verified"}
        if edit_log:
            out["edit"] = edit_log
        if product_ref:
            out["product_reference"] = product_ref
        if rewrite_log:
            out["condition_rewrite"] = rewrite_log
        delivered.append(out)
    if len(answer.claims) > policy.max_claims:
        for c in answer.claims[policy.max_claims:]:
            removed.append({"claim": asdict(c), "reason": "超過主張數上限", "rule": "max_claims"})
    if not delivered:
        status = "emptied_after_postprocess" if answer.claims else "no_claims"
    elif all(d["role"] == "background" for d in delivered):
        status = "no_direct_answer"
    elif removed or any("edit" in d for d in delivered):
        status = "delivered_with_removals"
    else:
        status = "delivered"
    if answer.insufficient:      # C-pp-v3：模型標不足——有交付就是部分回答，沒有就維持來源不足
        status = "source_insufficient" if not delivered else ("delivered_partial" if status.startswith("delivered") else status)
    return _result(status, delivered, removed, flags, answer, policy)


def _result(status: str, claims: list[dict], removed: list[dict], flags: list[dict], answer: ParsedAnswer, policy: Policy) -> dict:
    notes = []
    if claims and any(f.get("flag") == "model_marked_insufficient" for f in flags):
        notes.append(PARTIAL_NOTE)
    product_cut = any(r.get("rule") == "product_whole_claim" for r in removed) or any("edit" in c for c in claims)
    product_kept = any(c.get("product_reference") for c in claims)
    if product_cut and product_kept:
        notes.append(PRODUCT_MIXED_NOTE)      # product-ref-v4：兩種情況都有時合成一句，不再出現「先拿掉」又「保留」
    elif product_cut:
        # product-note-v2：沒有整條被拿掉、每個改寫都只是刪附帶舉例的商品名 → 用「只拿掉商品名」的說法
        name_only = (not any(r.get("rule") == "product_whole_claim" for r in removed)
                     and all(c["edit"].get("reason") == PRODUCT_NAME_ONLY_REASON for c in claims if "edit" in c))
        notes.append(PRODUCT_NAME_ONLY_NOTE if name_only else PRODUCT_NOTE)
    elif product_kept:
        notes.append(PRODUCT_REF_NOTE)
    said = list(dict.fromkeys(r["user_said"] for r in removed if r.get("rule") == "user_avoidance"))
    if said:
        notes.append("有內容和你說的" + "、".join(f"「{x}」" for x in said) + "相反，沒有列出。")
    pend = list(dict.fromkeys(f["user_said"] for f in flags if f.get("flag") == "may_conflict_pending_ambiguity"))
    if pend:
        notes.append("有提到光澤／亮面的內容；如果你說的" + "、".join(f"「{x}」" for x in pend) + "是指不要亮面質地，那幾條不適用。")
    question = answer.clarifying_question
    if question is None and any(f["flag"] == "needs_clarification" for f in flags):
        question = clarification_question(policy.explicit_conditions)
    return {"version": policy.version, "status": status, "mode": "generated", "claims": claims,
            "display_source_conditions": policy.display_source_conditions,
            "selection_mode": "none" if policy.selector == "none" or not policy.selection_labels else "labels",
            "restatement": answer.restatement,
            "limitations": list(answer.limitations), "system_notes": notes, "clarifying_question": question,
            "clarifying_question_source": None if question is None else ("model" if answer.clarifying_question else "postprocess_template"),
            "removed": removed, "flags": flags, "raw": answer.raw,
            "duplicate_version": DUPLICATE_VERSION, "vague_version": VAGUE_VERSION,
            "color_duplicate_version": COLOR_DUP_VERSION, "undertone_known_version": UNDERTONE_KNOWN_VERSION,
            "product_reference": PRODUCT_REF_VERSION if policy.product_reference else "off",
            "check_scope": "程式只核對引用 ID、商品指涉、使用者自述詞與來源條件欄位；主張語意是否被來源支持未驗證"}


RESTATEMENT_HEAD = "依你的描述整理（不是來源內容）："
_HAN = re.compile(r"[一-鿿]")
RESTATEMENT_MAX_NEW = 0.3      # 轉述裡問題原文沒有的漢字比例上限


def restatement_new_ratio(restatement: str, question: str) -> float:
    """C-pp-v5：轉述裡有多少漢字不在問題原文裡（0～1）；沒有漢字回 0。"""
    han = _HAN.findall(restatement or "")
    if not han:
        return 0.0
    q = set(question or "")
    return sum(1 for ch in han if ch not in q) / len(han)


# 使用者自述與來源分級對不上時的固定澄清問題（只問、不推定）。
def clarification_question(explicit_conditions: Any) -> str:
    terms = user_terms(explicit_conditions)
    t = "、".join(f"「{x}」" for x in terms) or "你的膚色描述"
    return f"來源的建議是依膚色深淺分開寫的；你說的{t}比較接近偏淺、中等，還是較深？"


# 交付文字（給人看）：每條主張後附來源單位；政策允許時附資料中的適用條件；背景主張另列。
def render_delivered(delivered: Mapping[str, Any]) -> str:
    lines = []
    if delivered.get("restatement"):
        lines.append(RESTATEMENT_HEAD + delivered["restatement"])
    for roles, head in ((("answer", "unjudged"), None), (("background",), "背景（不是本題的直接做法）：")):
        group = [c for c in delivered["claims"] if c["role"] in roles]
        if group and head:
            lines.append(head)
        for c in group:
            show = delivered.get("display_source_conditions") and c["source_conditions"]
            cond = f"（來源適用條件：{'；'.join(c['source_conditions'])}）" if show else ""
            lines.append(f"{c['text']}{cond}〔來源 {'、'.join(c['unit_ids'])}〕")
    if delivered.get("limitations"):
        lines.append("限制：" + "；".join(delivered["limitations"]))
    for note in delivered.get("system_notes") or []:
        lines.append("補充說明：" + note)  # limit-v2：「系統註記」偏技術，改成一般讀者看得懂的標題
    if delivered.get("clarifying_question"):
        lines.append("想再確認：" + delivered["clarifying_question"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# reconcile-v1（2026-10-26）：交付前的一致性檢查——與自己主張矛盾的限制、做法順序
# ---------------------------------------------------------------------------
RECONCILE_VERSION = "reconcile-v8"
# reconcile-v8（2026-11-09，fix-metrics 第七批，驗證 P09）：限制「沒有如何卸除口紅的具體步驟或技巧，僅提到油性與水性清潔產品的順序」
# 和主張（先油性、再水性清潔）矛盾卻沒攔下：「卸除」對不到主張的「卸妝／清潔」，「技巧」「順序」被當主題內容。
# 改：主題概念加「卸除」（主張有卸、清潔即算）；「技巧」「順序」和「步驟」一樣當概括詞。問步驟的其他條件（要有做法階段）不變。
# reconcile-v7（2026-11-09，fix-metrics 第四批，開發 N01）：限制講「底調」，但問題完全沒提底調或冷暖 → 拿掉
# （rule＝limitation_unasked_undertone）。提示第 8 條已規定限制只寫這次問題提到、證據沒涵蓋的部分。
# 例：N01 只問嘴唇乾想擦霧面，模型寫「證據中沒有提到如何選擇適合特定膚色底調的霧面唇膏」。
# 只限「問擦法」的題：問題有做法用語、而且完全沒有顏色或膚色用語（選色題的「無法依底調給建議」是合理限制，N02、M01 照留）。
_UNDERTONE_LIMIT = re.compile(r"底調")
_UNDERTONE_ASKED = re.compile(r"底調|底色|冷調|暖調|中性調|冷底|暖底|冷色|暖色|冷暖|冷皮|暖皮|黃皮|粉皮")
_COLOR_OR_SKIN_Q = re.compile(r"色|膚|皮膚|肌")


def limitation_unasked_undertone(limitation: str, question: str) -> bool:
    """reconcile-v7：問擦法的題（有做法用語、沒有任何顏色／膚色用語）卻寫底調限制 → True（這條限制不該寫）。

    問題為空、問題提到底調／冷暖、或問題涉及顏色／膚色（選色題）時都回 False，保守保留。"""
    q = question or ""
    if not q or not _UNDERTONE_LIMIT.search(limitation or ""):
        return False
    if _UNDERTONE_ASKED.search(q) or _COLOR_OR_SKIN_Q.search(q):
        return False
    return bool(_METHOD_Q.search(q))

# reconcile-v6（2026-11-08，fix-answers 第五批，M09）：問題也問整個妝怎麼畫（怎麼畫／怎麼化／畫法／化法／步驟）時，
# 腮紅、底妝這些步驟本身就是答案，off_target 不降背景。例：M09「草莓女孩妝…要怎麼畫，唇妝用什麼」的潤色保濕乳、腮紅。
# M04（濃煙燻眼妝配什麼唇色）沒問怎麼畫，照舊降背景。
_ASK_WHOLE_LOOK = re.compile(r"怎麼畫|怎麼化|畫法|化法|步驟")
# reconcile-v5（2026-10-29，第五十一批判讀：C-ap-v6 第 8 條例子被照抄）：限制寫「使用者提到／要求的（特定）X」，
# 但 X 的每個詞都不在問題原文裡 → 使用者沒說過，拿掉（rule＝limitation_user_term_not_said）。
# 例：W03「證據未提及使用者提到的特定服裝或唇色」（問題只問豆沙與奶茶差在哪）。X 裡任一詞有兩個連續字出現在問題就保留。
_USER_SAID_LIMIT = re.compile(r"使用者(?:所)?(?:提到|提及|要求|說|指定|描述|自述)(?:的|之)(?:特定|具體)?([一-鿿、或和與及/／]+?)(?:的|。|，|；|$)")
_TERM_SPLIT = re.compile(r"[、或和與及/／]")


def user_terms_not_said(limitation: str, question: str) -> list[str] | None:
    """reconcile-v5：限制裡「使用者提到／要求的 X」的 X 詞；全部不在問題原文時回傳這些詞，否則回 None。"""
    m = _USER_SAID_LIMIT.search(limitation or "")
    if not m:
        return None
    terms = [t for t in _TERM_SPLIT.split(m.group(1)) if len(t) >= 2]
    q = question or ""
    # 任一詞有兩個連續字出現在問題裡就算使用者說過（「豆沙色比較」對到「豆沙」）；保守，寧可留
    if not terms or any(t[i:i + 2] in q for t in terms for i in range(len(t) - 1)):
        return None
    return terms
# 「證據未說明／沒有提供…」這類說來源缺什麼的限制；group(1) 是缺的內容
_NEG_LIMIT = re.compile(r"(?:未|沒有|沒|並未|並沒有)(?:直接|具體)?(?:說明|提供|提到|提及|涵蓋|包含|給出|列出|寫到|寫出)(?:關於|有關)?(.+)")
_TOPIC_STOP = re.compile(r"如何|怎麼|怎樣|是否|具體|相關|直接|適合|特定|關於|有關|的|之|等|方法|建議|步驟|資訊|內容|做法")
# 缺的內容裡的動作詞：主張必須也有同一個動作，才算已涵蓋（「使用妝前乳」不等於「怎麼選妝前乳」）
_ACTION = re.compile(r"選擇|挑選|判斷|分辨|確認|搭配|使用|定妝|補擦|疊擦|去角質")
_METHOD_Q = re.compile(r"怎麼|如何|怎樣|辦法|方法|步驟|擦法|畫法|塗法|技巧|要怎")
# 做法的階段：事前整理 → 打底 → 唇線 → 上色 → 定妝／吸掉／補擦。選產品類主張不算步驟
_STAGES: tuple[tuple[int, re.Pattern], ...] = (
    (0, re.compile(r"去角質|磨砂|清潔")),
    (1, re.compile(r"護唇膏|潤唇|妝前乳|打底|粉底|遮瑕|蜜粉|散粉")),
    (2, re.compile(r"唇線")),
    (3, re.compile(r"(?:塗|擦|上|填)[^，。；]{0,8}(?:唇膏|口紅|唇彩|唇釉|顏色)|上色|填滿|填充")),
    (4, re.compile(r"定妝|定型|吸掉|吸走|補一層|補擦|第二層|再疊")),
)
_CHOICE = re.compile(r"^(?:選擇|選用|挑選|挑)|選擇含|選用含")
# reconcile-v2：問題在問唇（唇色、口紅…）時，主張完全沒提到唇 → 不是直接回答，降為背景（不刪，交付時另列）
_LIP_Q = re.compile(r"唇色|唇彩|口紅|唇膏|唇釉|唇蜜|紅唇|唇妝|嘴唇")
_LIP_CLAIM = re.compile(r"唇|口紅")
# reconcile-v3（W01）：沒提到唇的主張，只有在講別的部位（眼、眉、頰、底妝…）時才降為背景；
# 只講顏色本身（「正紅（true red）這個色名」）是在回答唇色問題，不降
_OTHER_AREA = re.compile(r"眼|眉|睫毛|腮紅|臉頰|頰|粉底|底妝|遮瑕|蜜粉|修容|指甲|頭髮|髮色")
# reconcile-v3（W04）：限制說「證據未提及 X」，X 取英文詞或「」裡的詞，只看第一個子句
_LIMIT_TERM = re.compile(r"[A-Za-z][A-Za-z\-']+|「([^」]{2,12})」")
# 「在去角質後塗抹護唇膏」「在塗抹唇膏前…」的時間子句只是在講前後關係，不是這條主張自己的步驟
_TIME_CLAUSE = re.compile(r"在[^，。；]{1,12}?(?:之)?[前後]")
# 第五十一批（T4 詞表開 M02）：限制問的是品牌、產品、色號時，「」裡的詞（深唇色）只是範圍；
# 片段出現這個詞不代表有品牌或產品對比，v3 的逐字比對不適用。「數據」不列入：W04「dusty rose…對比數據」第四十五批判為正確移除
_ITEM_ASK = re.compile(r"品牌|產品|商品|色號")


def limitation_covered_by(limitation: str, claim_texts: Sequence[str]) -> str | None:
    """限制說「來源沒有 X」，而本次交付的某條主張已經在講 X → 回傳那條主張（矛盾）；否則 None。
    只比對交付主張（中文對中文），不比對英文原文；判準保守：主題雙字詞至少 3 個且過半出現在同一條主張，
    且主題裡的動作詞（選擇、使用…）主張也有。"""
    m = _NEG_LIMIT.search(limitation)
    if not m:
        return None
    topic = _TOPIC_STOP.sub("", m.group(1))
    grams = _bigrams(topic)
    if len(grams) < 3:
        return None
    actions = set(_ACTION.findall(topic))
    for t in claim_texts:
        hit = len(grams & _bigrams(t))
        if hit >= 3 and hit / len(grams) >= 0.5 and all(a in t for a in actions):
            return t
    return None


def limitation_terms_in_evidence(limitation: str, evidence_texts: Sequence[str]) -> list[str]:
    """限制說「證據未提及／沒有提供 X」，而本次送入模型的片段原文確實有 X → 回傳這些詞（矛盾）；否則 []。
    只比對英文詞（至少 2 個字母）與「」裡的詞這種能逐字比對的詞，不比對一般中文詞（中文限制對英文原文無法可靠比對）。"""
    m = _NEG_LIMIT.search(limitation)
    if not m or not evidence_texts:
        return []
    topic = re.split(r"[，,。；;]", m.group(1))[0]
    if _ITEM_ASK.search(topic):   # 第五十一批：問品牌／產品／色號時，詞只是範圍，片段有這個詞不代表有這些項目
        return []
    src = " ".join(evidence_texts).casefold()
    out = []
    for t in _LIMIT_TERM.finditer(topic):
        term = t.group(1) or t.group(0)
        pat = (r"(?<![a-z])" + re.escape(term.casefold()) + r"(?![a-z])") if t.group(1) is None else re.escape(term.casefold())
        if re.search(pat, src) and term not in out:
            out.append(term)
    return out


# reconcile-v4（gaus_v1 共通問題）：限制說「證據沒有 X」，但 X 是本次交付主張合起來已經在講的事，或就在送入的片段裡。
# v1 只比對單一主張、門檻 3 個雙字詞且過半；v3 只比對英文詞與「」裡的詞。「沒有具體建議」「色調機制」「暖底和上班場合」
# 這類概括或換了說法的限制都漏掉（gaus_v1：11／20 題）。
# 「無法提供…」也算（N02）；「無法根據使用者…提供」是在講使用者資訊不足，不比對。
# 「沒有關於／針對 X」省略動詞也算（T4 P03 第二輪「沒有關於聚會場合…的直接描述」）
_NEG_LIMIT_V4 = re.compile(r"(?:未|沒有|沒|並未|並沒有|無法)(?:直接|具體)?"
                           r"(?:(?:說明|提供|提到|提及|涵蓋|包含|給出|列出|寫到|寫出)(?:關於|有關|針對)?|關於|有關|針對)(.+)")


def _p(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


_LIP_DARK = r"唇色[」』]?(?:偏|較|比較|有點|很)?(?:紫|暗|暗沉|發紫|發黑)|(?:偏紫|發紫|偏暗|暗沉)[」』]?的?(?:唇|嘴唇)|紫唇|暗唇"
# 「膚色（如偏紫）」：模型把唇色寫成膚色，括號裡的例子才是實際在講的東西（E2B M02）。
# 括號例子是偏紫、偏暗這類唇色深的說法時，連同前面的名詞換成「深唇」；其他括號不動
_EXAMPLE_LIP_DARK = re.compile(r"(?:膚色|皮膚|唇色|顏色|色調)?[（(](?:如|例如|像是|像)[「『]?(?:偏紫|發紫|偏暗|暗沉|偏深)[」』]?[）)]")

# 保濕成分名稱和「保濕」說法在同一句（30 字內、不跨句號或換行）
_MOIST_NAMES = (r"(?:神經醯胺|荷荷巴油|透明質酸|玻尿酸|乳木果|角鯊烷|甘油|維生素\s?E|ceramides?|jojoba|hyaluronic|shea|squalane|"
                r"glycerin)")
_MOIST_WORD = r"(?:保濕|滋潤|hydrat\w*|moistur\w*)"
_MOIST_NAMED = f"{_MOIST_WORD}[^。\\n]{{0,30}}{_MOIST_NAMES}|{_MOIST_NAMES}[^。\\n]{{0,30}}{_MOIST_WORD}"

# 常換說法的主題：(名稱, 限制裡的說法, 主張或片段裡算「有講到」的說法, 細分)。
# 細分＝限制點名了某一種（白天、暖底…）時，主張／片段要講到同一種；只說「特定場合」才接受任一種（T4 M04：「白天」不能算到「夜間」）。
_CONCEPTS: tuple[tuple[str, re.Pattern, re.Pattern, tuple[tuple[re.Pattern, re.Pattern], ...]], ...] = (
    ("場合", _p(r"場合|上班|辦公|通勤|聚會|派對|約會|晚間|夜間|晚上|白天|日間|假日"),
     _p(r"場合|上班|辦公|通勤|聚會|派對|約會|晚間|夜間|晚上|白天|日間|假日|活動|office|work|evening|night|party|event|occasion|daytime"),
     ((_p(r"上班|辦公|通勤"), _p(r"上班|辦公|通勤|office|work")),
      (_p(r"白天|日間"), _p(r"白天|日間|daytime")),
      (_p(r"晚間|夜間|晚上"), _p(r"晚間|夜間|晚上|evening|night")),
      # 聚會不接受「活動」「event」：「晚間活動」（evening events）不是聚會（第四十批，T4 新程式 P03 第二輪誤刪正確限制）
      (_p(r"聚會|派對"), _p(r"聚會|派對|party")),
      (_p(r"約會"), _p(r"約會|date")),
      (_p(r"假日"), _p(r"假日|holiday")))),
    # 深唇（M02，2026-10-27）：補「唇色偏紫／偏暗／暗沉／發紫」「偏紫的唇」等說法；一定要連著「唇」，
    # 單說「偏紫」「偏暗」可能是在講唇彩顏色（偏紫的口紅），不算。
    # 細分：限制直接點名偏紫（或偏暗）時，主張也要講到紫（或暗）——T4 詞表開 M02「沒有針對「偏紫」唇色的特定中和顏色建議」，
    # 主張只講打底中和原本唇色、沒講紫，該保留；括號舉例「特定膚色（如偏紫）」已先換成「深唇」，接受一般深唇說法（同「特定場合」）
    ("深唇", _p(r"深唇|唇色深|唇色較深|原有唇色|原生唇色|天生唇色|原本的唇色|" + _LIP_DARK),
     _p(r"深唇|唇色深|唇色較深|原有唇色|原生唇色|天生唇色|原本的唇色|naturally pigmented|pigmented lips|" + _LIP_DARK),
     ((_p(r"紫"), _p(r"紫|purple")), (_p(r"暗"), _p(r"暗|dark")))),
    ("底調", _p(r"底調|暖底|冷底|暖調|冷調|中性底"), _p(r"底調|暖底|冷底|暖調|冷調|偏暖|偏冷|中性底|undertone"),
     ((_p(r"暖底|暖調"), _p(r"暖底|暖調|偏暖|warm")), (_p(r"冷底|冷調"), _p(r"冷底|冷調|偏冷|cool")),
      (_p(r"中性底"), _p(r"中性|neutral")))),
    ("膚色", _p(r"膚色|皮膚|膚"), _p(r"膚色|皮膚|黃皮|白皙|skin"), ()),
    ("打底", _p(r"打底"), _p(r"打底|粉底|遮瑕|foundation|concealer"), ()),
    # 第五十一批（T4 詞表開 N01）：「保濕因子種類／名稱」要在同一句裡把成分名當保濕成分講才算講過：
    # 「含有保濕因子或含油、透明質酸的配方」算；只說「含保濕因子」不算；「含神經醯胺的護唇膏」沒說是保濕因子也不算
    # （舊 T4 N01 標註保留：來源的神經醯胺是護唇膏成分，霧面配方的保濕因子沒有寫名稱）
    ("保濕成分", _p(r"保濕(?:因子|成分)(?:的)?(?:種類|名稱|成分)?"), _p(_MOIST_NAMED), ()),
    ("精神", _p(r"精神|氣色"), _p(r"精神|氣色|提亮"), ()),
    ("誇張", _p(r"誇張|搶戲|張揚"), _p(r"誇張|搶戲|張揚"), ()),
    # reconcile-v8（驗證 P09）：限制寫「卸除」，主張寫「卸妝」「清潔」→ 同一件事
    ("卸除", _p(r"卸除|卸妝|卸掉|卸下"), _p(r"卸|清潔|cleans|remov"), ()),
)
# 概括詞、抽象詞與虛詞：換成斷點後剩下的才是主題；唇色、唇膏等品類詞每條主張都有，不算主題
_TOPIC_STOP_V4 = re.compile(r"如何|怎麼|怎樣|是否|具體|相關|直接|適合|適用性|適用|特定|關於|有關|針對|進行|根據|不同|之間|方面|任何|"
                            r"一些|這些|方法|建議|步驟|技巧|順序|資訊|內容|做法|說明|參考|推薦|選擇|操作|處理|達到|效果|詳細|對應|"
                            r"機制|差異|差別|區別|比較|對比|原理|細節|視覺|描述|"
                            r"唇色|唇膏|口紅|唇彩|顏色|色調|證據|來源|研究|的|之|等|與|和|及|或|在|上|中|時|來|下|應|以|已|有")
# 含單字停用字（和、中…）的實詞：先整個保留，不被切開（T4 M02「特定中和顏色」原本只剩「|」，主題整個消失）
_TOPIC_KEEP_V4 = re.compile(r"中和|調和|柔和|溫和|和諧")


def _strip_topic_stop(text: str) -> str:
    """把概括詞、虛詞換成斷點「|」；_TOPIC_KEEP_V4 的實詞原樣保留。"""
    return re.sub(f"(?P<keep>{_TOPIC_KEEP_V4.pattern})|{_TOPIC_STOP_V4.pattern}",
                  lambda m: m.group("keep") or "|", text, flags=_TOPIC_STOP_V4.flags)
# 具體項目（商品、色號、名稱、數值、深淺分級…）：限制講到時，主張要有同一個詞才算講過（T4 P02「產品推薦」、
# T4 M04「煙燻深淺程度」、T4 N01「保濕因子名稱」）；片段只是提到這些詞不算（T4 W03 片段有「色號」但沒有色號對比），不用片段判斷
_SPECIFIC = re.compile(r"產品|商品|品牌|色號|色值|HEX|數據|數值|量化|對照表|名稱|深淺|濃淡|程度|分級", re.I)
_METHOD_TOPIC = re.compile(r"步驟|操作|擦法|技巧|順序")
# 「X 和 Y 之間的差異」才算比較型；「定義與色調比較」只是列出缺的內容，不算
_COMPARE = re.compile(r"之間[^，。；]{0,6}(?:差異|差別|區別|比較|不同)")
# 限制在講使用者沒給的條件（膚色、底調、場合…未知），不是在講來源缺什麼，不比對
_USER_INFO = re.compile(r"使用者|您的|你的|個人偏好")


@dataclass(frozen=True)
class LimitTopic:
    """限制「沒有 X」拆出來的 X：raw 原文；need 每個主題要對到的說法；segs 其餘內容（每段的雙字詞）；specific 具體項目詞。"""
    raw: str
    need: tuple[re.Pattern, ...]
    segs: tuple[frozenset[str], ...]
    specific: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.need and not self.segs and not self.specific

    @property
    def grams(self) -> set[str]:
        return set().union(*self.segs) if self.segs else set()


def _limit_topic(limitation: str) -> LimitTopic | None:
    """拆限制「沒有 X」的 X；不是這類限制或在講使用者資訊回 None。"""
    m = _NEG_LIMIT_V4.search(limitation)
    if not m or _USER_INFO.search(limitation):
        return None
    raw = m.group(1)
    rest, need = _EXAMPLE_LIP_DARK.sub("深唇", raw), []
    for _, topic_pat, answer_pat, subs in _CONCEPTS:
        if not topic_pat.search(rest):
            continue
        named = [ans for tp, ans in subs if tp.search(rest)]
        need += named or [answer_pat]
        rest = topic_pat.sub("|", rest)
    specific = tuple(dict.fromkeys(x.group(0) for x in _SPECIFIC.finditer(rest)))
    rest = _SPECIFIC.sub("|", rest)
    segs = []
    for seg in re.split(r"[|，,。；;、\s]+", _strip_topic_stop(rest)):
        seg = re.sub(r"[^一-鿿A-Za-z0-9]", "", seg)
        if len(seg) >= 2:  # 單字段（例如「色」「不」）沒有主題意義
            segs.append(frozenset(_bigrams(seg)))
    return LimitTopic(raw, tuple(need), tuple(segs), specific)


def _topic_in(text: str, tp: LimitTopic, min_ratio: float) -> bool:
    """這段文字是否講到限制的主題：每個主題都有對應說法、具體項目詞都在、其餘每段內容都碰到，且雙字詞至少 min_ratio。"""
    if not all(p.search(text) for p in tp.need) or not all(w.casefold() in text.casefold() for w in tp.specific):
        return False
    if not tp.segs:
        return True
    have = _bigrams(text)
    if not all(seg & have for seg in tp.segs):   # 每一段都要碰到（T4 P04：講了兩種紅，沒講粉腮紅）
        return False
    return len(tp.grams & have) / len(tp.grams) >= min_ratio


def _touches(text: str, tp: LimitTopic) -> bool:
    """這條主張是否碰到限制主題：有任一主題的對應說法，或任一主題雙字詞。"""
    return any(p.search(text) for p in tp.need) or bool(tp.grams & _bigrams(text))


# 第四十批：「X 的色彩特徵描述」裡的「色彩特徵」只是在說 X 長什麼樣，不是另一個主題（T4 新程式 W04 詞表關：
# 「沒有乾燥玫瑰色的色彩特徵描述」，PR017_h0120#r3 已寫 dusty rose 是藍粉色）。「色彩學」「定義」「差異」不算，照舊要另外對到
_FEATURE_WORDS = re.compile(r"(?:色彩|顏色)?特徵")


def _glossary_english(topic: str) -> list[tuple[str, ...]]:
    """術語表把限制主題裡的中文詞轉成英文別名（乾燥玫瑰 → dusty rose），比對英文片段用。
    只在主題「全部」是術語表詞時回傳（每個詞一組英文別名）；還有其他內容（例如「搭配」）就回空，
    因為片段有這些詞不代表有講限制在問的事。術語表不在也回空。"""
    try:
        from . import query_glossary as qg  # type: ignore[import-not-found]
    except ImportError:
        try:
            import query_glossary as qg  # type: ignore[no-redef]  # 離線重放時以檔案路徑載入
        except ImportError:
            return []
    hits = [mt for mt in qg.analyze(topic).matches if mt.category != "literal" and mt.english]
    if not hits:
        return []
    rest = topic
    for mt in sorted(hits, key=lambda x: -x.start):
        rest = rest[:mt.start] + "|" + rest[mt.end:]
    rest = _FEATURE_WORDS.sub("|", rest)
    leftover = [re.sub(r"[^一-鿿A-Za-z0-9]", "", seg) for seg in re.split(r"[|，,。；;、\s]+", _strip_topic_stop(rest))]
    if any(len(seg) >= 2 for seg in leftover):
        return []
    return [tuple(mt.english) for mt in hits]


def limitation_topic_covered(limitation: str, claim_texts: Sequence[str]) -> str | None:
    """reconcile-v4：限制說「證據沒有／未說明／無法提供 X」，而本次交付主張「合起來」已經在講 X → 回傳理由；否則 None。
    主題拿掉概括詞後是空的（「沒有提供具體建議」）→ 有交付主張就算矛盾；主題裡的動作詞要和主題出現在同一條主張；
    問步驟時還要至少一條主張看得出做法階段。"""
    tp = _limit_topic(limitation)
    if tp is None or not claim_texts:
        return None
    if tp.empty:
        return "限制只說沒有具體建議／做法，但本次已交付主張"
    if not _topic_in("\n".join(claim_texts), tp, 0.5):
        return None
    # 同 v1：動作詞（選擇、使用…）要和主題在同一條主張；「用了妝前乳」＋另一條「選擇唇膏」不等於「怎麼選妝前乳」。
    # 第四十批：有動作詞時，整個主題要在那「一條」主張裡（不是只碰到一點）——「藍調紅與粉色腮紅搭配」不能用
    # 「冷底選藍調紅」＋「唇膏與腮紅搭配一致」兩條湊出來（T4 新程式 P04 詞表開誤刪兩條正確限制）
    actions = set(_ACTION.findall(tp.raw))
    if actions and not any(all(a in t for a in actions) and _topic_in(t, tp, 0.5) for t in claim_texts):
        return None
    if _METHOD_TOPIC.search(tp.raw) and not any(procedure_stage(t) is not None for t in claim_texts):
        return None
    if _METHOD_TOPIC.search(tp.raw) and not _sub_step_explained(tp.raw, claim_texts):
        return None
    return "限制說來源沒有的主題，本次交付主張合起來已經在講"


# 第五十一批（T4 詞表開 N01）：限制問「去角質的具體步驟」這種某一個階段自己的做法時，主張只說「先去角質」
# 是把它當一個步驟，不是說明它怎麼做。上色（階段 3）是整體做法本身，不在此列
_SUB_STAGES = tuple(pat for stage, pat in _STAGES if stage != 3)
# 做法手段：用什麼、怎麼動手。「可以」的「以」不算
_MEANS = re.compile(r"用|(?<!可)以|透過|搭配|輕輕|輕拍|輕壓|畫圈|按壓|來回|濕敷|磨砂|毛巾|牙刷|棉花棒|指腹|刷")


def _sub_step_explained(raw: str, claim_texts: Sequence[str]) -> bool:
    """限制問的步驟若是某一階段（去角質、打底、唇線、定妝）自己的做法：要有一條主張講到該階段（時間子句不算）且帶做法手段；
    限制沒有點名階段時回 True（交給原本的判斷）。"""
    named = [pat for pat in _SUB_STAGES if pat.search(raw)]
    if not named:
        return True
    for t in claim_texts:
        core = _TIME_CLAUSE.sub("", t)
        if any(pat.search(core) for pat in named) and _MEANS.search(core):
            return True
    return False


# 比較、區分型限制裡的「或」連的是比較對象（「藍調紅或鮮明紅的對比」「區分非粉紅或非橙紅」），不是兩件缺的事，不切
_SPLIT_BLOCK = re.compile(r"對比|比較|差異|差別|區分|區別|分辨|之間|相比")


def limitation_trim_covered(limitation: str, claim_texts: Sequence[str]) -> tuple[str, list[str]] | None:
    """第五十一批（T4 詞表開 N01）：限制「沒有 A 或 B」，A 本次交付主張已經在講、B 沒講 → 回傳（只留 B 的限制, [A]）；
    全部或都沒講到、沒有「或」、分出來的某段只剩概括詞時回 None（整條照原本規則處理）。"""
    m = _NEG_LIMIT_V4.search(limitation)
    if not m or _USER_INFO.search(limitation) or not claim_texts or _SPLIT_BLOCK.search(m.group(1)):
        return None
    if re.search(r"[，,；;]", m.group(1).rstrip("。．. ")):   # 第五十三批（T4 v4 P03）：後面還接別的子句（「，僅提供裸色或柔和色調」），「或」不一定屬於缺的那件事，不切
        return None
    parts = [p.strip() for p in re.split(r"或是|或", m.group(1).rstrip("。．. "))]
    if len(parts) < 2 or not all(parts):
        return None
    prefix = limitation[:m.start(1)]
    covered = []
    for p in parts:
        tp = _limit_topic(prefix + p)
        if tp is None or tp.empty:   # 「具體建議」這種概括段不拿來判斷，整條不動
            return None
        if limitation_topic_covered(prefix + p, claim_texts):
            covered.append(p)
    if not covered or len(covered) == len(parts):
        return None
    return prefix + "或".join(p for p in parts if p not in covered) + "。", covered


def limitation_topic_in_evidence(limitation: str, evidence_texts: Sequence[str]) -> str | None:
    """reconcile-v4：限制說「證據沒有 X」，而某一段送入的片段原文就在講 X → 回傳理由；否則 None。
    比主張嚴格：同一段片段要有全部主題、其餘內容雙字詞至少八成；英文片段用術語表的英文別名逐字比對。
    概括限制（主題是空的）、問步驟的限制（片段提到不等於有步驟，T4 M02）、講具體項目的限制都不用片段判斷。"""
    tp = _limit_topic(limitation)
    if tp is None or not evidence_texts or tp.empty or tp.specific or _METHOD_TOPIC.search(tp.raw):
        return None
    for t in evidence_texts:
        if _topic_in(t, tp, 0.8):
            return "限制說來源沒有的主題，送入的片段原文有講"
    src = " ".join(evidence_texts).casefold()
    groups = _glossary_english(tp.raw)
    found = [next((p for p in g if re.search(r"(?<![a-z])" + re.escape(p.casefold()) + r"(?![a-z])", src)), None) for g in groups]
    if groups and all(found):
        return "限制說來源沒有的主題，送入的片段原文有（" + "、".join(found) + "）"
    return None


def procedure_stage(text: str) -> int | None:
    """主張屬於做法的哪個階段（取最早的階段）；選產品類或看不出階段回 None。"""
    if _CHOICE.search(text):
        return None
    core = _TIME_CLAUSE.sub("", text)
    found = [stage for stage, pat in _STAGES if pat.search(core)]
    return min(found) if found else None


def reconcile(delivered: Mapping[str, Any], question: str, evidence_texts: Sequence[str] | None = None) -> dict:
    """回傳調整後的 Delivered（不改原物件）：
    0. reconcile-v2／v3：問題在問唇、主張沒提到唇而且在講別的部位（例如濃煙燻題裡只講眼影顏色）→ role 改成 background，
       交付時列在「背景（不是本題的直接做法）」下；記旗標 off_target_claim。全部都變背景時 status＝no_direct_answer。
       v3：只講顏色、沒講別的部位的主張（W01「正紅這個色名」）不降。
       v6：問題也問整個妝怎麼畫（M09）時整項不套用，其他部位的步驟就是答案。
    1. 移除和本次交付主張矛盾的限制（記 removed，rule＝limitation_contradicts_claim）。
    1b. reconcile-v3：限制說「證據未提及 X」但送入的片段原文有 X（evidence_texts，W04 的 MLBB）→ 移除該限制
       （rule＝limitation_contradicts_evidence）；X 沒被任何交付主張用到時記旗標 evidence_term_unused，並加系統註記讓讀者知道。
    1c. 第五十一批：「沒有 A 或 B」只有一部分被主張講到 → 限制只留沒講的部分（rule＝limitation_part_contradicts_claims）。
    2. 問做法、而且至少 2 條主張看得出步驟時，依階段穩定排序（看不出階段的主張排在後面、保持原順序），
       順序有變才記旗標 reordered_by_procedure（含前後順序）。主張文字、引用都不改。"""
    out = dict(delivered)
    claims = list(out.get("claims") or [])
    flags = list(out.get("flags") or [])
    rs = out.get("restatement")
    if rs and restatement_new_ratio(rs, question) > RESTATEMENT_MAX_NEW:
        # C-pp-v5：轉述只能用使用者原話的詞；問題沒有的字太多 → 可能加了事實，不交付
        out["restatement"] = None
        out["removed"] = list(out.get("removed") or []) + [{
            "restatement": rs, "rule": "restatement_not_from_user",
            "reason": f"轉述裡問題原文沒有的漢字比例 {restatement_new_ratio(rs, question):.2f} 超過 {RESTATEMENT_MAX_NEW}"}]
    if _LIP_Q.search(question or "") and not _ASK_WHOLE_LOOK.search(question or ""):
        off = [c["claim_no"] for c in claims if c.get("role") != "background" and not _LIP_CLAIM.search(c["text"])
               and _OTHER_AREA.search(c["text"])]
        if off:
            claims = [{**c, "role": "background"} if c["claim_no"] in off else c for c in claims]
            flags.append({"flag": "off_target_claim", "claim_nos": off, "reason": "問題在問唇，主張沒有提到唇、在講別的部位"})
            if all(c["role"] == "background" for c in claims):
                out["status"] = "no_direct_answer"
    texts = [c["text"] for c in claims]
    kept_lims, removed = [], list(out.get("removed") or [])
    notes = list(out.get("system_notes") or [])
    for lim in out.get("limitations") or []:
        unsaid = user_terms_not_said(lim, question)
        if unsaid:
            removed.append({"limitation": lim, "reason": "限制說使用者提到／要求，但問題原文沒有這些詞",
                            "rule": "limitation_user_term_not_said", "terms": unsaid})
            continue
        if limitation_unasked_undertone(lim, question):
            removed.append({"limitation": lim, "reason": "限制講底調，但問題沒有提到底調或冷暖",
                            "rule": "limitation_unasked_undertone"})
            continue
        hit = limitation_covered_by(lim, texts)
        if hit is not None and not all(w.casefold() in hit.casefold() for w in _SPECIFIC.findall(lim)):
            hit = None   # v4：限制問的是具體項目（名稱、色號…），那條主張沒有同一個詞就不算講過（T4 N01「保濕因子名稱」）
        tp1 = _limit_topic(lim) if hit is not None else None
        if tp1 is not None and not all(p.search(hit) for p in tp1.need):
            hit = None   # 第四十批：限制點名的主題（底調、場合…）那條主張沒講到就不算講過（M01「小麥色皮膚特定底調」，主張只有小麥色）
        v4 = limitation_topic_covered(lim, texts) if hit is None else None
        neg = _NEG_LIMIT_V4.search(lim)
        # v4：「X 和 Y 之間的差異」這種比較型限制，片段有 X、Y 兩個詞不代表有比較（W04 曾被誤刪），不用片段判斷
        compare = bool(neg and _COMPARE.search(neg.group(1)))
        v4e = limitation_topic_in_evidence(lim, evidence_texts or []) if hit is None and not v4 and not compare else None
        terms = [] if hit is not None or v4 or v4e or compare else limitation_terms_in_evidence(lim, evidence_texts or [])
        if hit is not None:
            removed.append({"limitation": lim, "reason": "限制說來源沒有，但本次交付的主張已經在講", "rule": "limitation_contradicts_claim",
                            "covered_by": hit})
        elif v4:
            removed.append({"limitation": lim, "reason": v4, "rule": "limitation_contradicts_claims_union"})
        elif v4e:
            removed.append({"limitation": lim, "reason": v4e, "rule": "limitation_topic_in_evidence"})
            flags.append({"flag": "evidence_topic_unused", "limitation": lim})
            notes.append("送入的來源有講到「" + lim.rstrip("。") + "」這件事，但這次回答沒有用到；已拿掉這條限制，這部分請再問一次或看來源。")
        elif terms:
            removed.append({"limitation": lim, "reason": "限制說來源沒有提到，但送入的來源片段原文有這個詞",
                            "rule": "limitation_contradicts_evidence", "terms": terms})
            unused = [t for t in terms if not any(t.casefold() in x.casefold() for x in texts)]
            if unused:
                flags.append({"flag": "evidence_term_unused", "terms": unused})
                notes.append("送入的來源有提到" + "、".join(f"「{t}」" for t in unused)
                             + "，但這次回答沒有用到；已拿掉「來源沒有提到」的限制，這部分請再問一次或看來源。")
        elif (trim := limitation_trim_covered(lim, texts)) is not None:
            kept_lims.append(trim[0])
            removed.append({"limitation": lim, "reason": "限制的一部分本次交付主張已經在講，只留沒講的部分",
                            "rule": "limitation_part_contradicts_claims", "parts": trim[1], "kept_as": trim[0]})
        else:
            kept_lims.append(lim)
    stages = [procedure_stage(t) for t in texts]
    if _METHOD_Q.search(question or "") and sum(s is not None for s in stages) >= 2:
        order = sorted(range(len(claims)), key=lambda i: (9 if stages[i] is None else stages[i], i))
        if order != list(range(len(claims))):
            flags.append({"flag": "reordered_by_procedure", "before": [c["claim_no"] for c in claims],
                          "after": [claims[i]["claim_no"] for i in order], "stages": stages})
            claims = [claims[i] for i in order]
    out.update(claims=claims, limitations=kept_lims, removed=removed, flags=flags, system_notes=notes,
               reconcile_version=RECONCILE_VERSION)
    return out
# 核心邏輯：只做「看得出來就調整、看不出來就不動」的保守檢查；不改寫主張文字、不補限制、不呼叫模型。
# 降為背景的主張仍交付（另列），不刪除；比對矛盾限制時仍把背景主張算進「本次交付主張」。
# 限制和英文原文矛盾（例如來源有寫、但主張沒用到）不在這裡處理：中文限制對英文原文無法可靠比對，記為未解。
