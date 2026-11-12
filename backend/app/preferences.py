"""S6 需求與偏好：意圖分類、偏好解析、對話內偏好狀態合併、補問與需求摘要。

整體設計（詳見 docs/mvp/s6-preferences.md）：

    使用者原話 ──► RuleParser.parse() ──► ParseResult（意圖、變更、句內衝突、模糊詞）
                                              │
    對話既有狀態 ─────────────────────────────►apply_parse()  ──► 新狀態＋已套用變更＋跨訊息衝突
                                              │
                                              ├─► plan_clarification()  最多 2 題，一般 1 題
                                              └─► summarize()／limitations()  給前端顯示的需求摘要

原則（CLAUDE.md 規範 5、6、8）：
- 解析器只「提出」變更；真正寫入狀態的是 apply_parse()，模型或規則都不能直接改狀態。
- 每個欄位保留使用者原話、來源、request_id、時間；沒說過的欄位維持「未知」，不補值。
- 不從膚色推定底調（小麥色 ≠ 暖底調）；底調只有使用者明說才記。
- 更新一個欄位時其他欄位原封不動；明確取消時移除對應限制，不反向新增喜好。
- 同一件事同時出現「要」與「不要」、或與既有條件相反卻沒有修改語意 → 記為衝突、不套用、補問。
- 過敏四狀態：unanswered／none_known／has_known／unsure；沒提到過敏就維持原狀，
  未回答永遠不會被當成「無已知過敏」。
- 狀態只存在該對話（conversations.preferences），不建立跨對話長期記憶。

目前生成模型尚未決定（DEC-11），解析器是可重現的規則版（rule-v1）；之後換成模型解析時，
模型輸出必須轉成同一種 ParseResult，再走同一個 apply_parse()。
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

PREF_SCHEMA_VERSION = "pref-v1"
PARSER_NAME = "rule"
PARSER_VERSION = "rule-v15"   # rule-v15：「分別」等詞不當否定、「搭配」不算推薦、只被提到的詞不當想要、膚色口語（S1）。
# rule-v14：沒認出的回答要求不因有條件變推薦／修改（K1）；索取非公開資料另判（I03）。
# rule-v13：緊鄰效果詞的「不太／不那麼」保留否定；延續 rule-v12 搭配問句。
# rule-v10 rule-v10（2026-10-26）：「不要太亮」模糊詞的 raw 只放該段，補問句不再引用整句。
# rule-v9（2026-10-26）：「不要太誇張／濃豔／大膽」對到鮮明亮眼（詞表 draft-4）；「有什麼擦法」算知識（問做法）。
# rule-v8（2026-10-26）：「不要偏粉或偏橘的紅」只排斥偏粉、偏橘，被修飾的「紅」不算排斥
# rule-v7（2026-10-25）：程度副詞「比較深」、求做法與遮蓋問題算知識、描述原生唇色的色詞不算喜好
# rule-v3（2026-10-18，開發題 IJ10、FB01、FB02、FB07）：底調只記自述（規則宣稱與問句不記）、
# 「比較＋形容詞」是程度副詞不是比較、「忽略…」取消舊條件與「改回」、「什麼唇色」算推薦提問
# rule-v4（2026-10-18，同四題的根因補完；rule-v3 的評估結果保留不覆寫）：只提到拍照情境不算照片功能
# （「拍照時什麼唇色比較上相」；FB01 需求「沒有上傳照片就是一般推薦需求」）；「比較」改用句型判斷是不是在比兩個選項
# rule-v5（2026-10-18，K1～K5 決策）：場合未提供就保持未知（不填日常、不強制補問）；撤回場合清除舊場合；
# 「A 改回 B」是替換、兩個場合都要是多情境（不判衝突）；「覺得／比較像冷底」是暫定底調；一般問句裡的膚色／底調不寫入
# rule-v6（2026-10-19，研究對齊 rev1 的第一輪需求處理）：依語意類型修三種第一輪行為——
# (1) 只提到品牌／色號／專櫃、卻沒有要系統挑商品（例如問「怎麼知道色號實際是什麼顏色」）不算商品請求；
#     「它是什麼顏色」是問身分不是問推薦。
# (2) 「看起來有精神」這類模糊的外觀目標不再直接記成「有氣色」偏好，改成本輪補問確認
#     （暫定理解：自然、不張揚的好氣色），暫定理解不寫進偏好。
# (3) 想要「不一樣的擦法」卻沒說平常怎麼擦 → 本輪先問平常的擦法；知識問答也要問，不被缺證據擋掉。
# followup-v1（2026-10-19，rev1 第二輪接續）：上面 (2)(3) 的本輪問題問出去後，記成「待回答」（pending_turn，
# 不是偏好）；下一則訊息先判斷是不是在回答它——肯定就把暫定理解轉成使用者確認的需求、否定就丟掉暫定理解並
# 依回答更新、回答平常擦法就記下來並接回原本的問題；答非所問就放棄這個待回答，照一般訊息處理。解析器輸出不變。
# followup-v2（2026-10-19）：明確肯定／明確否定／含糊分開（「好像是」不再因「好」被當肯定）；回答要符合題型
# （「是」不能存成平常擦法，也不再只因句子短就收）；插問時暫存待回答（一次一題）、插問後只說「對」先確認；
# 明確取消就清掉；待回答最多等 LIPMATCH_PENDING_MAX_TURNS 則（預設 3）就過期。
FOLLOWUP_VERSION = "followup-v2"

_VOCAB_PATH = Path(__file__).with_name("preference_vocab.json")

# 單值欄位（後說的取代先說的）與清單欄位（可多個）
SCALAR_FIELDS = ("skin_tone", "undertone", "natural_lip", "occasion", "budget", "notes")
LIST_FIELDS = ("effects", "avoided_effects", "liked_colors", "disliked_colors",
               "liked_finishes", "disliked_finishes")
# 想要／不要互為相反的清單；用來判斷跨訊息衝突與「改成」時要從哪邊移除
OPPOSITE = {
    "liked_colors": "disliked_colors", "disliked_colors": "liked_colors",
    "liked_finishes": "disliked_finishes", "disliked_finishes": "liked_finishes",
    "effects": "avoided_effects", "avoided_effects": "effects",
}
# 硬條件：明確排斥（推薦時必須排除）；其餘清單是軟偏好
HARD_FIELDS = ("disliked_colors", "disliked_finishes")
ALLERGY_STATES = ("unanswered", "none_known", "has_known", "unsure")
OCCASIONS = ("daily", "office", "party")

FIELD_LABELS = {
    "occasion": "場合", "skin_tone": "自述膚色", "undertone": "底調", "natural_lip": "原生唇色",
    "effects": "希望效果", "avoided_effects": "不想要的效果", "liked_colors": "喜歡的色系",
    "disliked_colors": "排斥的色系", "liked_finishes": "喜歡的妝感／質地",
    "disliked_finishes": "排斥的妝感／質地", "budget": "預算", "allergy": "過敏狀態",
    "allergy_avoid": "需避開項目", "notes": "其他說明",
    "technique": "平常擦法（想換的方向）",
}
ALLERGY_LABELS = {"unanswered": "尚未回答", "none_known": "沒有已知過敏",
                  "has_known": "有已知過敏", "unsure": "不確定"}
UNDERTONE_LABELS = {"warm": "暖底調", "cool": "冷底調", "neutral": "中性底調"}


# ---------------------------------------------------------------------------
# 詞彙表
# ---------------------------------------------------------------------------
# 讀取詞彙草稿（B 線維護的 JSON），整理成「詞 → (種類, 正規化鍵, 顯示名稱)」對照表。
def _load_vocab(path: Path = _VOCAB_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    terms: dict[str, tuple[str, str, str]] = {}
    labels: dict[tuple[str, str], str] = {}
    ambiguity: dict[tuple[str, str], str] = {}
    for kind, group in (("color", "colors"), ("finish", "finishes"),
                        ("effect", "effects"), ("occasion", "occasions")):
        for key, spec in data[group].items():
            labels[(kind, key)] = spec["label"]
            if spec.get("ambiguity"):
                ambiguity[(kind, key)] = spec["ambiguity"]
            for t in spec["terms"]:
                terms[t] = (kind, key, spec["label"])
    undertones = {t: key for key, words in data["undertones"].items() for t in words}
    by_label = {label: (kind, key, label) for (kind, key), label in labels.items()}
    return {"version": data["vocab_version"], "status": data["status"], "terms": terms, "labels": labels,
            "by_label": by_label,
            "ambiguity": ambiguity, "skin_tones": data["skin_tones"], "undertones": undertones,
            "stopwords": data["stopwords"], "lip_descriptors": data["natural_lip_descriptors"]}
# 核心邏輯：同一個詞只會對到一個欄位；顯示名稱與模糊詞類型一併建表，之後掃描時直接查。


VOCAB = _load_vocab()

# 句中標記詞：決定後面詞語是「想要／不要／取消／改成」。長詞放前面，正規表示式才會優先吃長詞。
_LIKE = ["只想要", "只要", "一定要", "想要", "喜歡", "偏好", "偏愛", "希望", "想找", "想擦", "愛用", "要"]
_DISLIKE = ["不太喜歡", "不喜歡", "不想要", "不要", "不想", "不愛", "討厭", "排斥", "避開", "避免", "拒絕", "別"]
# rule-v15（第 3 類假衝突，N07）：含「別」但不是否定的詞。掃描用的詞索引把它們當停用詞（長詞優先，先被吃掉），
# 否定說法 _RE_NEG_PHRASE 也從同一份清單產生前後字，兩套規則不再分歧。「別再」是否定，不放進來。
_BIE_NOT_NEGATION = ("分別", "個別", "特別", "區別", "類別", "性別", "識別", "差別", "告別", "級別",
                     "別人", "別的", "別家", "別處", "別種", "別說")
_BIE_BEFORE = "".join(w[0] for w in _BIE_NOT_NEGATION if w.endswith("別"))
_BIE_AFTER = "".join(w[1] for w in _BIE_NOT_NEGATION if w.startswith("別"))
# rule-v3：「忽略我剛剛說的聚會」是撤回自己先前的條件（FB02），與「取消」同義；
# 只影響本模組的條件掃描，不會讓任何指令生效（來源文字與模型提示的注入防護在 ingest／qa，不受影響）
_CANCEL_STRONG = ["取消", "拿掉", "移除", "刪掉", "忽略"]
_CANCEL_SOFT = ["不再排斥", "不排斥", "可以接受", "不介意", "接受"]
_REPLACE = ["改成", "改為", "換成", "改要", "改用", "改回", "變成", "改"]
_DEGREE = ["太過", "過於", "太"]
# 詞語後面接這些字 → 視為取消排斥（例如「霧面也可以」）
_POSTFIX_CANCEL = re.compile(r"^(也可以|也行|也ok|也OK|可以了|沒關係|沒問題)")

_CLAUSE_SPLIT = re.compile(r"[，。！？；,.!?;\n]+")
_SELF_MARK = re.compile(r"我|本人|自己|膚色是|皮膚")
# rule-v3：判斷底調是不是「自述」用。主詞是自己才算（「皮膚」不算，「小麥色皮膚就是暖底」是規則宣稱）
_SELF_SUBJECT = re.compile(r"我|本人|自己")
# 「X 就是／等於／代表 暖底」這類把某條件連到底調的說法：使用者宣稱的規則，不是自述（IJ10，規範 5）
_RE_RULE_LINK = re.compile(r"就是|等於|等同|代表|意味|一定是|都是|屬於|[=＝]")
# 問句裡提到底調名稱（「什麼是暖底調」「我是暖底嗎」）不是自述（S12 已知問題，與 IJ10 同一規則）
# 「嗎」只認緊接在底調詞後面（「我是暖底嗎」），避免「我是冷底可以推薦嗎」這種自述＋提問被丟掉
_RE_UNDERTONE_QUESTION = re.compile(r"什麼是|是什麼|什麼叫|(?:底調?|皮)嗎|怎麼看|怎麼判斷|如何判斷|怎麼知道|怎麼分|差在哪|差別")
# rule-v5（K4）：「覺得／比較像／好像…冷底」是暫定自述：保留使用者的說法與不確定性，不當成確定底調
_RE_UNDERTONE_HEDGE = re.compile(r"覺得|好像|似乎|應該|可能|大概|也許|感覺|比較像|有點像|偏向|猜")
# rule-v5（K5）：子句是一般問句（沒有「我／本人／自己」當主詞）時，裡面的膚色、底調詞是話題，不是自述
_RE_QUESTION_WORD = re.compile(r"什麼|哪|嗎|呢|是不是|是否|為什麼|為何|怎麼|如何|會不會|能不能")
# 提到照片類的東西（不論是不是範圍外請求）：回覆要說明系統沒有看到、也沒有分析任何圖片
_RE_PHOTO_MENTION = re.compile(r"照片|相片|自拍|圖片|截圖|附圖")


# 把標記詞與詞彙合成一個正規表示式，掃描時一次找出所有詞與標記的位置。
def _build_token_index() -> tuple[re.Pattern, dict[str, tuple[str, Any]]]:
    index: dict[str, tuple[str, Any]] = {}
    for words, kind in ((_LIKE, "like"), (_DISLIKE, "dislike"), (_CANCEL_STRONG, "cancel_strong"),
                        (_CANCEL_SOFT, "cancel_soft"), (_REPLACE, "replace"), (_DEGREE, "degree"),
                        (VOCAB["stopwords"], "stop"), (_BIE_NOT_NEGATION, "stop")):
        for w in words:
            index.setdefault(w, (kind, None))
    for t, info in VOCAB["terms"].items():
        index.setdefault(t, ("term", info))
    pattern = re.compile("|".join(re.escape(w) for w in sorted(index, key=len, reverse=True)))
    return pattern, index
# 核心邏輯：依長度由長到短排列，「不要」會比「要」先被吃掉、「橘紅」比「紅」先被吃掉、「口紅」被當停用詞吃掉。


_TOKEN_RE, _TOKEN_INDEX = _build_token_index()


# ---------------------------------------------------------------------------
# 偏好狀態（存在 conversations.preferences；過敏狀態與場合同步寫回原有欄位）
# ---------------------------------------------------------------------------
# 建立一個「什麼都還不知道」的欄位值。
def _unknown() -> dict:
    return {"value": None, "status": "unknown", "raw": None, "source": None, "request_id": None, "updated_at": None}


# 建立全新對話的偏好狀態：所有欄位一律未知（含場合）、過敏未回答。
def empty_state() -> dict:
    state: dict[str, Any] = {"schema_version": PREF_SCHEMA_VERSION}
    for f in SCALAR_FIELDS:
        state[f] = _unknown()
    for f in LIST_FIELDS:
        state[f] = []
    state["allergy"] = {"status": "unanswered", "avoid": [], "raw": None, "source": None,
                        "request_id": None, "updated_at": None}
    state["conflicts"] = []      # 尚未釐清的衝突（不套用，等使用者決定）
    state["ambiguities"] = []    # 尚未釐清的模糊詞（不轉成排除條件）
    # rule-v11：對象不在詞表的否定說法（原話）；只交給問答，不顯示在需求摘要、不補問
    state["unclassified_avoid"] = []
    state["asked"] = {}          # 非阻斷補問已問過的次數，避免同一欄位無限重問
    # followup-v1：使用者說過的平常擦法與想換的方向（P05 類型）；回答「平常怎麼擦」後才有值
    state["technique"] = {**_unknown(), "goal": None, "keep": None}
    # followup-v1：上一輪問出去、等使用者回答的本輪確認問題（只有一題）；不是偏好，不出現在需求摘要
    state["pending_turn"] = None
    return state
# 核心邏輯（rule-v5，K1）：場合未提供就是未知，不自動填「日常」（舊版填 daily／default 並揭露，已停用）。


# 從資料庫的對話列還原偏好狀態。場合與過敏以資料表欄位為準（有 CHECK 約束），其餘來自 JSON。
def load_state(conv: dict) -> dict:
    raw = conv.get("preferences") or {}
    state = empty_state()
    if raw.get("schema_version") == PREF_SCHEMA_VERSION:
        for k, v in raw.items():
            if k in state:
                state[k] = copy.deepcopy(v)
    elif raw:
        _upgrade_legacy(state, raw)
    occ = state["occasion"]
    if conv.get("scenario_source") == "user" and conv.get("scenario"):
        state["occasion"] = {**occ, "value": conv["scenario"], "status": "user"}
        state["occasion"].pop("values", None)
    elif not (occ.get("status") == "user" and occ.get("values")):
        state["occasion"] = _unknown()  # 未指定（含舊資料的「預設日常」）一律視為未知
    allergy = state["allergy"]
    allergy["status"] = conv.get("allergy_status") or "unanswered"
    column_avoid = list(conv.get("allergy_avoid") or [])
    known = {a["value"]: a for a in allergy["avoid"]}
    allergy["avoid"] = [known.get(v) or _item(v, v, None, "legacy", None, None) for v in column_avoid]
    return state
# 核心邏輯：
# 1. 新格式（pref-v1）直接載入；UI 第一版留下的舊格式 {liked, disliked, finish, notes} 轉成新欄位。
# 2. 場合、過敏狀態、需避開項目以資料表欄位為準，JSON 只補原話與時間等中繼資料，兩邊不會各說各話。
# 3. rule-v5：資料表的場合欄位不可為空（CHECK daily／office／party），因此 scenario_source = default 改為
#    「未指定單一場合」的意思，不再代表預設日常；多情境（K3）只存在 JSON 的 occasion.values。


# UI 第一版的舊格式 → pref-v1：詞語依詞彙表分到色系或妝感，舊資料沒有原話與時間，來源標 legacy。
def _upgrade_legacy(state: dict, raw: dict) -> None:
    for term in raw.get("liked") or []:
        kind, key, label = classify_term(term)
        _list_add(state["liked_finishes" if kind == "finish" else "liked_colors"],
                  _item(key, label, term, "legacy", None, None))
    for term in raw.get("disliked") or []:
        kind, key, label = classify_term(term)
        _list_add(state["disliked_finishes" if kind == "finish" else "disliked_colors"],
                  _item(key, label, term, "legacy", None, None))
    if raw.get("finish") in ("matte", "satin", "gloss"):
        key = raw["finish"]
        _list_add(state["liked_finishes"], _item(key, VOCAB["labels"][("finish", key)], key, "legacy", None, None))
    if raw.get("notes"):
        state["notes"] = {"value": raw["notes"], "status": "user", "raw": raw["notes"],
                          "source": "legacy", "request_id": None, "updated_at": None}


# 把狀態拆成要寫回資料庫的欄位：scenario、scenario_source、preferences（JSON）、allergy_status、allergy_avoid。
def dump_state(state: dict) -> dict:
    occ = state["occasion"]
    single = occ["status"] == "user" and occ["value"] in OCCASIONS
    return {
        # rule-v5：欄位不可為空；未指定或多情境時寫佔位值 daily／default（讀回時視為未指定，見 load_state）
        "scenario": occ["value"] if single else "daily",
        "scenario_source": "user" if single else "default",
        "preferences": copy.deepcopy(state),
        "allergy_status": state["allergy"]["status"],
        "allergy_avoid": [a["value"] for a in state["allergy"]["avoid"]],
    }


# 建立清單欄位中的一個項目：正規化鍵、顯示名稱、使用者原話、來源、request_id、時間。
def _item(key: str, label: str, raw: str | None, source: str | None,
          request_id: str | None, now: str | None) -> dict:
    return {"value": key, "label": label, "raw": raw, "source": source, "request_id": request_id, "updated_at": now}


# 在清單中加入項目；同一個鍵已存在時只更新原話與時間，不重複加入（同義詞不重複計分）。
def _list_add(items: list[dict], item: dict) -> bool:
    for existing in items:
        if existing["value"] == item["value"]:
            existing.update({k: item[k] for k in ("raw", "source", "request_id", "updated_at")})
            return False
    items.append(item)
    return True


# 從清單移除指定鍵；回傳是否真的有移除。
def _list_remove(items: list[dict], key: str) -> bool:
    before = len(items)
    items[:] = [i for i in items if i["value"] != key]
    return len(items) != before


# 把一個自由輸入的詞對到詞彙表：回傳 (種類, 正規化鍵, 顯示名稱)；查不到就以原詞當鍵、種類 custom。
def classify_term(term: str) -> tuple[str, str, str]:
    t = term.strip()
    if t in VOCAB["terms"]:
        kind, key, label = VOCAB["terms"][t]
        return kind, key, label
    if t in VOCAB["by_label"]:  # 表單回填的是顯示名稱（例如「裸色／奶茶色」）
        return VOCAB["by_label"][t]
    m = _TOKEN_RE.search(t)
    if m and _TOKEN_INDEX[m.group(0)][0] == "term" and len(m.group(0)) >= len(t) - 1:
        kind, key, label = _TOKEN_INDEX[m.group(0)][1]
        return kind, key, label
    return "custom", t, t
# 核心邏輯：完全相符（詞或顯示名稱）最優先；其次允許多一個字（例如「霧面感」→ 霧面）；其他一律保留原詞，不硬套分類。


# ---------------------------------------------------------------------------
# 解析：使用者原話 → ParseResult（只是提案，不會直接改狀態）
# ---------------------------------------------------------------------------
@dataclass
class ParseResult:
    intent: str                                    # knowledge／recommend／compare／update／product_request／outside_scope／restricted_request
    secondary_intents: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)       # 句內衝突（同一句同時要與不要）
    ambiguities: list[dict] = field(default_factory=list)
    allergy: dict | None = None
    mentions_photo: bool = False                   # rule-v5：訊息提到照片類的東西（回覆要說明沒有看過圖片）
    outside_text: str | None = None                # limit-v2：命中範圍外的字樣（限制句只寫題目真的碰到的那一類）
    # rule-v6：只屬於這一則訊息的確認問題（不寫進對話狀態、不變成偏好）；知識問答也會問
    turn_questions: list[dict] = field(default_factory=list)
    # rule-v11：否定說法的對象不在詞表（[{raw, clause}]）；不分類、不補問，原話交給問答
    unclassified_avoid: list[dict] = field(default_factory=list)
    parser: str = PARSER_NAME
    parser_version: str = PARSER_VERSION
    vocab_version: str = VOCAB["version"]

    # 轉成可存進日誌（requests.detail）的字典。
    def to_dict(self) -> dict:
        return asdict(self)


# rule-v4：照片只有在「要系統看／分析照片」時才算範圍外（規範 9 不做照片）；只提到拍照情境
# （「拍團體照時」「拍照時什麼唇色比較上相」）是一般推薦需求（FB01）。
# 照片詞前面有處理動詞（看、傳、判斷…），或後面接處理動詞（幫我、判斷、選色…）才算。
_PHOTO_WORDS = r"照片|相片|自拍照?|圖片|截圖"
_PHOTO_REQUEST = (r"上傳|看我的臉"
                  rf"|(?:傳|附上?|給你|幫我看|看|判斷|分析|辨識|根據|依照|用)[^，。！？；,.!?;]{{0,6}}?(?:{_PHOTO_WORDS})"
                  rf"|(?:{_PHOTO_WORDS})[^，。！？；,.!?;]{{0,6}}?(?:幫我|判斷|分析|辨識|看看|看一下|選色|測)")
_RE_OUTSIDE = re.compile(_PHOTO_REQUEST + r"|診斷|皮膚病|處方|醫生|過敏測試|過敏原檢測"
                         # S10（rule-v2）：交易與即時庫存屬範圍外（規範 9），不再落到知識問答
                         r"|下單|訂購|結帳|付款|刷卡|加入購物車|有貨|有沒有貨|庫存|現貨|到貨|缺貨")
# rule-v15（第 4 類，O01）：問的對象不是唇彩——在問衣服、頭髮、指甲該選什麼顏色，整則訊息又沒有提到唇彩。
# 只認「顏色／穿／染＋非唇彩物件」同一子句；「穿黑色洋裝搭什麼唇色」有唇彩詞，仍是唇彩問題。
_NON_LIP_ITEMS = r"裙子?|衣服|洋裝|上衣|襯衫|褲子?|外套|鞋子?|包包?|頭髮|髮色|指甲油?|美甲|領帶|圍巾"
_RE_NON_LIP_COLOR = re.compile(rf"(?:什麼|哪種|哪個|哪一種)(?:顏色|色)的?(?:{_NON_LIP_ITEMS})"
                               rf"|(?:穿|染|擦|塗|買)(?:什麼|哪種|哪個)(?:顏色|色)?的?(?:{_NON_LIP_ITEMS})"
                               rf"|(?:{_NON_LIP_ITEMS})(?:要|該|適合)?(?:選|挑|穿|染|擦|塗)?(?:什麼|哪種|哪個)(?:顏色|色)")
_RE_LIP_TOPIC = re.compile(r"唇|口紅|唇膏|唇釉|唇蜜")
# rule-v6：商品詞分兩類。「指定商品」詞本身就是在要商品；「商品脈絡」詞（品牌、色號、專櫃…）只是提到，
# 要同一子句裡有挑選／推薦的動作才算商品請求（「推薦品牌色號」是；「色號名字很抽象，怎麼知道實際顏色」不是）。
# rule-v15（2026-11-01 商品界線）：只攔真的在推薦購買特定商品——要系統挑出一支、找替代品、問價格或購買管道。
# 只有「哪支／哪款」、沒有品牌或購買字眼（「哪支口紅比較持久？」）不算商品要求，當質地問題走問答（Q6）。
# 單獨的「想買／要買」不算（N09「第一支口紅想買最不容易出錯的顏色」是在問顏色方向，不是要特定商品）。
_RE_PRODUCT = re.compile(r"多少錢|價錢|價格|購買|哪裡買|在哪買|買哪|買得到|給我買|連結|官網|型號"
                         r"|替代品|代替品|平替|dupe", re.IGNORECASE)
# 要系統挑出「一支／哪一支」特定商品（「直接推薦一支適合我的口紅」「推薦哪一支」）
_RE_PRODUCT_PICK = re.compile(r"(?:推薦|挑|選|給我|找)(?:一支|一款|一條|哪一支|哪支|哪款|哪一款)")
# 品牌色當顏色參考（「有沒有品牌色號可以參考這種顏色」「像哪個色號的顏色」）不是商品推薦，不攔
_RE_COLOR_REFERENCE = re.compile(r"參考|長怎樣|長什麼樣|什麼樣的顏色|像")
_RE_PRODUCT_CONTEXT = re.compile(r"品牌|牌子|色號|專櫃")
_RE_PRODUCT_SEEK = re.compile(r"推薦|建議|適合|想找|幫我找|幫我挑|挑|選|有沒有|給我|告訴我|只要|哪個|哪些|哪幾")
# rule-v3：「比較上相／比較好看／比較適合」的「比較」是程度副詞（＝較），不是要求比較兩個選項（FB01）。
# 後面接這些形容詞／動詞時不算強比較；真的在比兩個選項時，「還是／哪個＋兩個色系或妝感」仍會判成比較。
# 刻意不放色系、妝感、深淺字（紅、霧、亮、深…）：「比較霧面與亮面」「比較深色和淺色」是在比兩個選項。
# rule-v7：「唇色比較深、偏紫」的「比較深」是程度副詞；「比較深色和淺色」（後面接「色」）仍是比較
_COMPARE_ADVERB_NEXT = (r"深(?!色)|淺(?!色)|暗(?!色)|淡(?!色)|黑(?!色)|乾|薄|厚|"
                        r"上相|好看|適合|自然|顯|好|容易|不|低調|明顯|搭|襯|柔|溫|美|有|會|能|推薦|偏|常|"
                        r"安全|保守|大方|正式|持久|日常|氣色|喜歡|想|在意|怕|穩|成熟|年輕|可愛|甜|酷|專業|討喜|百搭|耐看")
# rule-v4：只靠上面的副詞清單會漏（「我比較像冷底」被當成比較）。改成看句型，「比較」是動詞才算強比較：
# 後面接「一下／看看」或在子句結尾（「霧面和亮面比較一下」「A 跟 B 比較」），
# 或不是副詞用法、且 12 字內接兩個選項的連接詞（「比較霧面與亮面」「比較深色和淺色」）。
_RE_COMPARE_STRONG = re.compile(r"比較(?:一下|看看|(?=[，。！？；,.!?;\s]|$)"
                                rf"|(?!{_COMPARE_ADVERB_NEXT})[^，。！？；,.!?;]{{0,12}}?(?:和|跟|與|及|、|還是))"
                                r"|差別|差異|差在哪|對比|vs|VS")
_RE_COMPARE_WEAK = re.compile(r"還是|哪個|哪一個")
# rule-v6：「怎麼知道／怎麼確認／怎麼分辨」是在問方法，屬知識問答
# rule-v7：問做法（怎麼辦、有什麼辦法…）與描述遮蓋問題（蓋不住、被吃掉…）是在求方法，也屬知識問答，不是要推薦色系
_RE_KNOWLEDGE = re.compile(r"什麼是|是什麼|為什麼|為何|原因|研究|定義|意思|怎麼分|如何判斷|怎麼判斷|原理|有什麼影響"
                           r"|怎麼知道|如何知道|怎麼確認|如何確認|怎麼分辨|如何分辨"
                           r"|怎麼辦|有什麼辦法|有沒有辦法|怎麼處理|如何處理|怎麼改善|如何改善|怎麼做才|怎樣才"
                           # rule-v9：問擦法／畫法是在問做法（P05 加了「不要太誇張」這個條件後，不能因此被判成修改）
                           r"|有什麼(?:擦法|畫法|塗法)|(?:怎麼|如何)(?:擦|畫|塗)(?:比較|才|會)"
                           r"|蓋不住|遮不住|蓋不過|遮不過|被吃掉|吃色"
                           # rule-v8：問怎麼描述／形容（「這樣要怎麼說比較準」）是在問說法，屬知識問答
                           r"|怎麼說|如何說|怎麼形容|如何形容|怎麼描述|如何描述"
                           r"|(?:怎麼|如何|怎樣)(?:搭|配)")
# rule-v3：「什麼唇色／哪種口紅」也是在問推薦（FB01、FB07；原本只認「什麼顏色／什麼色」）
# rule-v6：「（它）是什麼顏色」是問身分，不是要推薦，所以「什麼顏色／什麼色」前面是「是」時不算
# rule-v15：拿掉「搭配」——「怎麼搭配」是問做法（知識詞已收），「搭配什麼唇色」由「什麼唇色」認出，不再單憑「搭配」判推薦
_RE_RECOMMEND = re.compile(r"推薦|建議|適合|想找|幫我找|幫我挑|選什麼|哪種顏色|哪個顏色|(?<!是)什麼顏色|(?<!是)什麼色|擦什麼|用什麼"
                           r"|(?:什麼|哪種|哪個|哪一種|哪些)(?:唇色|唇彩|口紅|唇膏|唇釉)")
_RE_UPDATE = re.compile(r"改成|改為|換成|改要|改用|變成|改|取消|拿掉|移除|刪掉|不排斥|不再排斥|可以接受|不介意|"
                        r"也可以|也行|可以了|沒關係|不用避開|不必避開")
_RE_BUDGET = re.compile(r"(?:預算|價位|價格|價錢)[^0-9０-９]{0,6}([0-9０-９]{2,6})\s*(元|塊|台幣|NT|nt)?"
                        r"|([0-9０-９]{2,6})\s*(元|塊|台幣)\s*(?:以內|以下|內|左右)?")
_RE_RESOLVE = re.compile(r"以(.{1,10}?)為(?:準|主)")
# S10（rule-v2）：「不要太亮／別太艷」這類說法可能指顏色鮮豔，也可能指亮面光澤；不能自行選一邊，
# 也不能直接丟掉（排斥條件會遺失）。排除已有明確詞的「亮面、亮澤、亮眼、閃亮」。
_RE_BRIGHTNESS = re.compile(r"(?:不要|不想|別|避免|不喜歡)(?:太|過於|那麼|很)?(?:亮|艷|豔|閃)(?![面澤眼片粉])")
_RE_LIP_ITEM = re.compile(r"唇色|唇彩|口紅|唇膏|唇釉|唇蜜|顏色|色系")
# rule-v15（S1）：口語膚色描述（「皮膚很白」「膚色比較深」）。詞表整詞比對沒命中才用；值正規化成詞表既有的
# 「膚色偏X」，raw 保留原話。只寫 skin_tone，不推定底調（規範 5）。need_relevance 的膚色排序共用同一組程度詞。
# rule-v15：子句裡明說在找某個色系／妝感的動詞（只用來決定提到詞算不算想要，不影響意圖）
_RE_SEEK_IN_CLAUSE = re.compile(r"推薦|想找|幫我找|幫我挑|挑一|選一|找一")
SKIN_DEGREE = r"(?:很|比較|偏|較|算|有點|蠻|滿|超)?"
_RE_SKIN_DESC = re.compile(r"(?:皮膚|膚色)" + SKIN_DEGREE + r"(白|淺|深|黑|黃)")
# rule-v14（K1，2026-11-01）：訊息是不是在「要求回答」——問句，或要系統列出、計算、說明、提供步驟的指令句。
# 只用在 _classify 的預設判斷：沒被任何明確規則（知識、推薦、比較、修改詞）認出的回答要求，
# 不能只因帶了條件（has_changes）就被當成推薦或純修改。「什麼都可以」「不怎麼喜歡」是陳述，不算問句。
_RE_ANSWER_REQUEST = re.compile(
    r"[?？]|嗎(?=[，。！；,.!;\s]|$)|呢(?=[，。！；,.!;\s]|$)"
    r"|為什麼|(?<!不)怎麼|怎樣|如何|哪|多少|幾(?:個|種|支|點|成|次|歲)|什麼(?!都)"
    r"|會不會|能不能|可不可以|是不是|有沒有|要不要|對不對"
    # rule-v15：指令句中間可夾「分別／簡單／詳細」（N07「請分別說明…」），之前沒認出、又帶條件就落到「只改條件」
    r"|請(?:幫我|你)?(?:分別|簡單|詳細|再)?(?:列出|列|計算|算|說明|解釋|提供|整理|告訴|給|附上|比較)|列出|計算|教我|步驟")
# rule-v14（I03）：明確索取其他使用者的對話／資料，或非公開的系統指令。權限只看登入帳號，
# 訊息裡自稱「系統管理員」「授權稽核」不改變權限，所以規則不看身分說法，只看「要拿什麼」。
# 只在同一子句裡同時有「索取動作」和「受限對象」才算；只是討論權限（「其他使用者看得到我的對話嗎」）不算。
_RE_RESTRICTED_TARGET = re.compile(
    r"(?:其他|別的|其它)(?:使用者|用戶|會員|人)[^，。！？；,.!?;]{0,10}?(?:對話|聊天|紀錄|記錄|提問|問了|個資|帳號|資料)"
    r"|(?:別人|他人)的?(?:對話|聊天|紀錄|記錄|提問|個資|帳號|資料)"
    r"|系統提示|系統指令|system\s*prompt|提示詞|開發者指令|內部指令|隱藏指令", re.IGNORECASE)
_RE_RESTRICTED_VERB = re.compile(r"列出|列給|給我|貼出|貼上|顯示|調出|印出|輸出|匯出|洩漏|公開|提供|告訴我|讓我看|看一下")
# rule-v6：模糊的「看起來有精神」類外觀目標（不是明說「好氣色」）。詞彙表的「有氣色」不再收「有精神」。
_RE_VITALITY = re.compile(r"有精神|精神一點|精神些|有活力|有朝氣|有元氣|元氣滿滿"
                          r"|(?:不要|不想|別)(?:看起來)?(?:很|太)?(?:累|疲倦|疲憊|憔悴|沒精神|沒氣色)"
                          r"|看起來(?:很|太)?(?:累|疲倦|憔悴)")
# 使用者已明說想要的效果方向（自然／鮮明／好氣色），有的話就不用再確認「有精神」的意思
_RE_EFFECT_STATED = re.compile(r"好氣色|有氣色|紅潤|自然|低調|不張揚|鮮明|鮮豔|顯色|亮眼|搶眼|明亮")
# rule-v6：想換一種擦法／畫法，需要先知道平常怎麼擦才談得上「不一樣」
_RE_TECHNIQUE = re.compile(r"擦法|畫法|塗法|上妝方式|擦的方式|畫的方式|怎麼擦|怎麼畫|怎麼塗|如何擦|如何畫|如何塗")
_RE_TECHNIQUE_CHANGE = re.compile(r"不一樣|不同|換個|換一種|換種|新的|新鮮|變化|別的|其他")
_RE_TECHNIQUE_KNOWN = re.compile(r"平常|平時|通常|習慣|我都|一向|每次都")
_RE_NOT_DRAMATIC = re.compile(r"(?:(?:不要|不想|別|避免)(?:太|過於|那麼|很)?|不(?:太|過於|那麼|很))(?:誇張|浮誇|做作)")
# rule-v11：否定說法＋詞表外的對象（「不會太搶戲」「不要看起來乾乾的」）。對象到「的」或標點為止。
# 「要不要」「想不想」的「不要／不想」不是否定；「特別、分別、個別」的「別」不是否定；「不會／不能」要接「太」才算
_RE_NEG_PHRASE = re.compile(
    r"(?:(?<![要想])(?:不太喜歡|不喜歡|不想要|不要|不想)|不愛|討厭|排斥|避開|避免|(?<![" + _BIE_BEFORE + r"])別(?![" + _BIE_AFTER + r"再])"
    r"|不會太|不能太|不可以太)(?:太|過於|那麼|很)?(?P<obj>[^，。！？；,.!?;\n、的]{1,8})")
# 對象尾巴的品類詞與語氣詞不算對象本身（「不會太搶戲唇色」→「搶戲」）
_RE_NEG_TAIL = re.compile(r"(?:唇色|唇彩|口紅|唇膏|顏色|色系|啦|喔|哦|耶|呢|吧|了)+$")
_RE_TECH_ITEM = re.compile(r"紅唇|唇妝|口紅|唇膏|唇釉|唇彩")
# rule-v8：「不要偏粉或偏橘的紅」的「紅」是被修飾的主體，排斥的是「偏粉、偏橘」，不是紅色本身
_RE_QUALIFIED_HEAD = re.compile(r"偏[^，。！？；,.!?;的]{1,4}的$")

# 過敏相關句型
_RE_ALLERGY_UNSURE = re.compile(r"(不確定|不知道|不清楚|不太確定)[^，。,.!?！？]{0,8}過敏")
_RE_ALLERGY_NONE = re.compile(r"(確定)?(?<!有)(沒有|沒|無)(任何|已知的?|什麼)?過敏|(?<!會不)不會過敏")
_RE_ALLERGY_ITEM = re.compile(r"對([^，。,.!?！？]{1,20}?)(?:會)?過敏")
_RE_ALLERGY_AVOID = re.compile(r"(?:不能用|不能碰|不可以用|要避開|需要避開)([^，。,.!?！？]{1,20}?)(?:這類|這種|類)?成分")
_RE_ALLERGY_REMOVE = re.compile(r"(?:不用|不必|不需要)再?避開([^，。,.!?！？]{1,20})")
_RE_ALLERGY_HAS = re.compile(r"有過敏|會過敏|過敏體質")
_RE_ITEM_SPLIT = re.compile(r"和|跟|及|與|、|或|/")


class RuleParser:
    """規則版解析器（rule-v2）：可重現、不花 token。生成模型決定後可換成模型版，輸出格式相同。"""

    name = PARSER_NAME
    version = PARSER_VERSION

    # 解析一則使用者訊息，回傳 ParseResult。
    def parse(self, text: str) -> ParseResult:
        normalized = _RE_RESOLVE.sub(_resolve_phrase, text)
        changes, occasion_conflicts, ambiguities = self._scan_terms(normalized)
        ambiguities += self._scan_brightness(normalized, changes)
        changes += self._scan_profile(normalized)
        budget = self._scan_budget(normalized)
        if budget:
            changes.append(budget)
        allergy = self._scan_allergy(normalized)
        # 只被提到的詞（mention）不算條件；模糊詞與場合衝突算（使用者確實在描述需求）
        # rule-v6：「看起來有精神」這類外觀目標也是在描述需求（雖然不寫進偏好），和模糊詞一樣算
        has_changes = (any(c["op"] != "mention" for c in changes) or bool(allergy)
                       or bool(ambiguities) or bool(occasion_conflicts) or bool(_RE_VITALITY.search(normalized)))
        intent, secondary = self._classify(normalized, has_changes)
        wants = intent in ("recommend", "update") or "recommend" in secondary
        kept: list[dict] = []
        # rule-v7：描述自己原生唇色的子句裡的色詞（「唇色偏紫」）是在說唇色，不是喜歡那個色系
        for c in changes:
            if c["op"] == "mention":
                # rule-v15：只被提到的色系／妝感（沒有想要／不要的語氣）只是話題，不記成偏好；之前在推薦或修改語境下
                # 整則訊息的提到詞都會變成想要（W06 的「霧面」、N07 第三子句的「紅色」）。
                # 只有同一子句明說在找它（「推薦霧面的唇膏」「幫我挑豆沙色」）才算想要。
                if wants and _RE_SEEK_IN_CLAUSE.search(c.get("raw") or ""):
                    kept.append({**c, "op": "add"})
            elif c["field"] == "skin_tone" and not wants and not _SELF_MARK.search(text):
                continue  # 一般知識問題提到膚色（例如「小麥色是什麼意思」）不是自述，不寫進偏好
            else:
                kept.append(c)
        kept, conflicts = _split_in_message_conflicts(kept)
        turn_questions = self._turn_questions(normalized, intent, secondary)
        # rule-v11：在判斷意圖之後才掃，不影響意圖（不會因多一個說法就從知識變成修改）
        unclassified = self._scan_unclassified_avoid(normalized)
        return ParseResult(intent=intent, secondary_intents=secondary, changes=kept,
                           conflicts=conflicts + occasion_conflicts, ambiguities=ambiguities, allergy=allergy,
                           mentions_photo=bool(_RE_PHOTO_MENTION.search(text)), turn_questions=turn_questions,
                           unclassified_avoid=unclassified, outside_text=outside_hit(normalized))
    # 核心邏輯：
    # 1. 「以 X 為準」先改寫成「要 X」，讓補問的回答可以被當成明確選擇。
    # 2. 依序掃描色系／妝感／效果／場合、膚色等個人描述、預算、過敏，最後判斷意圖。
    # 3. 「只是被提到」的詞不算偏好（rule-v15）；再檢查句內衝突（例如「霧面，不要霧面」）。
    # 4. 只回傳提案；是否真的套用、會不會和既有條件衝突，交給 apply_parse() 決定。
    # 5. rule-v6：最後找出只屬於這一則訊息的確認問題（模糊外觀目標、不知道平常擦法）。

    # rule-v6：找出這一則訊息需要先確認、但不該寫進偏好的地方，回傳阻斷性補問。
    @staticmethod
    def _turn_questions(text: str, intent: str, secondary: list[str]) -> list[dict]:
        questions: list[dict] = []
        wants = intent in ("recommend", "update") or "recommend" in secondary
        if wants and _RE_VITALITY.search(text) and not _RE_EFFECT_STATED.search(text):
            questions.append({"field": "turn:vitality", "key": None, "blocking": True, "kind": "vitality",
                              "question": "想「看起來有精神」，是想要自然、不張揚的好氣色嗎？"
                                          "（這是暫時的理解，還沒記成你的偏好；想要其他感覺也可以直接說）",
                              # followup-v1：使用者回答「是」時要轉成確認需求的暫定理解（自然低調＋有氣色）
                              "context": {"tentative_effects": ["natural", "healthy_look"],
                                          "origin_intent": intent, "origin_text": text}})
        if (intent not in ("outside_scope", "product_request", "restricted_request") and _RE_TECHNIQUE.search(text)
                and _RE_TECHNIQUE_CHANGE.search(text) and not _RE_TECHNIQUE_KNOWN.search(text)):
            item = _RE_TECH_ITEM.search(text) or _RE_LIP_ITEM.search(text)
            keep = _RE_NOT_DRAMATIC.search(text)
            tail = (f"知道之後，才能找和平常不一樣、又符合「{keep.group(0)}」的擦法。" if keep
                    else "知道之後，才能找和平常不一樣的擦法。")
            questions.append({"field": "turn:technique_baseline", "key": None, "blocking": True,
                              "kind": "technique_baseline",
                              "question": f"你平常{item.group(0) if item else '唇妝'}是怎麼擦的？{tail}",
                              # followup-v1：回答平常擦法後，要接回原本的問題並保留「不要太誇張」
                              "context": {"item": item.group(0) if item else "唇妝",
                                          "keep": keep.group(0) if keep else None,
                                          "origin_intent": intent, "origin_text": text}})
        return questions
    # 核心邏輯：
    # 1. 「有精神」只在使用者要找唇色（推薦／修改）、而且沒有明說效果方向時才問；暫定理解只出現在問句裡，不寫進偏好。
    # 2. 「不一樣的擦法」要有擦法詞＋改變詞、且沒說平常怎麼擦才問；不論意圖（知識問答也問），
    #    只排除範圍外與商品請求。問句只問平常怎麼擦，不舉例、不預設均勻塗滿，也不先提任何擦法。
    # 3. 使用者講的「不要太誇張」照原話放進問句，讓這個偏好不在補問時遺失。

    # 逐句掃描色系、妝感、效果、場合，依前面的標記詞決定是想要、不要、取消或改成。
    def _scan_terms(self, text: str) -> tuple[list[dict], list[dict], list[dict]]:
        changes: list[dict] = []
        ambiguities: list[dict] = []
        occasions: list[tuple[str, str, str, bool]] = []   # (鍵, 顯示名稱, 原話, 是否在「改成／改回」之後)
        cancelled: list[tuple[str, str]] = []              # 被撤回的場合 (鍵, 原話)
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
            if not clause:
                continue
            polarity, replace_flag, strong, last_end, degree_end = None, False, False, -10, -10
            for m in _TOKEN_RE.finditer(clause):
                kind, info = _TOKEN_INDEX[m.group(0)]
                adjacent = m.start() - last_end <= 2
                if kind == "stop":
                    continue
                if kind == "degree":
                    degree_end = m.end()
                    continue
                if kind in ("like", "dislike", "cancel_strong", "cancel_soft", "replace"):
                    if kind == "replace":
                        polarity, replace_flag, strong = "like", True, False
                    elif kind.startswith("cancel"):
                        polarity, replace_flag, strong = "cancel", False, kind == "cancel_strong"
                    elif polarity == "cancel" and adjacent:
                        pass  # 「取消不要霧面」：取消的對象是「不要霧面」，仍是取消
                    else:
                        replace_flag = replace_flag and adjacent  # 「改成不要霧面」保留改成語意
                        polarity = kind
                    last_end = m.end()
                    continue
                term_kind, key, label = info
                term_polarity = polarity
                # 只限定緊鄰效果詞的否定，不讓「不太懂」污染同句其他偏好。
                if (term_kind == "effect" and term_polarity != "cancel" and
                        re.search(r"不(?:太|那麼|過於|很)\s*$", clause[:m.start()])):
                    term_polarity = "dislike"
                if term_kind == "color" and term_polarity == "dislike" and _RE_QUALIFIED_HEAD.search(clause[:m.start()]):
                    term_polarity = None   # rule-v8：被「偏X的」修飾的色詞只是被提到
                if _POSTFIX_CANCEL.match(clause[m.end():]):
                    term_polarity, strong = "cancel", False
                degree = degree_end == m.start() and term_polarity == "dislike"
                if term_kind == "occasion":
                    if term_polarity == "cancel":
                        cancelled.append((key, clause))
                    elif term_polarity != "dislike":
                        occasions.append((key, label, clause, replace_flag))
                    continue
                change = self._term_change(term_kind, key, label, clause, term_polarity, replace_flag, strong, degree)
                if change is None:
                    continue
                if change["op"] == "ambiguity":
                    ambiguities.append(change["ambiguity"])
                else:
                    changes.append(change)
                    amb_kind = VOCAB["ambiguity"].get((term_kind, key))
                    if amb_kind and change["op"] == "add":
                        ambiguities.append({"kind": amb_kind, "field": change["field"], "key": key,
                                            "label": label, "raw": clause, "blocking": False,
                                            "resolves_with": ["liked_finishes:gloss"]})
        conflicts: list[dict] = []
        if any(rep for *_, rep in occasions):
            occasions = [o for o in occasions if o[3]]   # 「聚會改回上班用」：改回之後的才是新場合（K3 替換）
        distinct = {k: (k, lbl, raw) for k, lbl, raw, _ in occasions}
        if len(distinct) == 1:
            key, label, raw = next(iter(distinct.values()))
            changes.append({"op": "set", "field": "occasion", "value": key, "label": label, "raw": raw})
        elif len(distinct) > 1:
            keys = sorted(distinct)
            changes.append({"op": "set_multi", "field": "occasion", "values": keys,
                            "label": "、".join(distinct[k][1] for k in keys),
                            "raw": "；".join(dict.fromkeys(distinct[k][2] for k in keys))})
        elif cancelled:
            keys = sorted({k for k, _ in cancelled})
            changes.append({"op": "clear", "field": "occasion", "keys": keys, "label": "場合",
                            "raw": "；".join(dict.fromkeys(raw for _, raw in cancelled))})
        return changes, conflicts, ambiguities
    # 核心邏輯：
    # 1. 每個子句重新開始判斷，標記詞的效力延續到同一子句後面的詞（「不要霧面跟橘色」兩個都不要）。
    # 2. 「取消不要…」「改成不要…」這類緊鄰的標記組合，保留前一個標記的語意。
    # 3. 「不要太紅」這種程度詞＋色系 → 只記模糊詞，不轉成排除紅色（規格 R07）。
    # 4. rule-v5（K3）：「A 改回 B」只取改回之後的 B（替換）；兩個以上場合都要 → 多情境（set_multi），不判衝突。
    # 5. rule-v5（K2）：只撤回場合、沒有新場合 → clear，套用時清除舊場合，其他偏好不動。

    # S10（rule-v2）：找出「不要太亮」這類沒有明確對象的亮度排斥，記成阻斷性模糊詞（先問清楚再推薦）。
    # 同一則訊息已經明說「不要鮮豔」或「不要亮面」時，意思已清楚，不再記。
    @staticmethod
    def _scan_brightness(text: str, changes: list[dict]) -> list[dict]:
        explicit = any((c.get("field"), c.get("key")) in (("avoided_effects", "vivid"), ("disliked_finishes", "gloss"))
                       for c in changes)
        if explicit:
            return []
        found: list[dict] = []
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
            m = _RE_BRIGHTNESS.search(clause)
            if not m:
                continue
            # rule-v10：raw 只放「不要太亮」這段（補問句、註記會引用它）；整個子句另存 clause 供追溯。
            # 舊版 raw 放整個子句，P03 第二輪補問變成「「我想要上班用、不要太亮的唇色」是指…」。
            found.append({"kind": "brightness", "field": "avoided_effects", "key": "brightness",
                          "label": m.group(0), "raw": m.group(0), "clause": clause, "blocking": True,
                          "resolves_with": ["avoided_effects:vivid", "disliked_finishes:gloss", "effects:vivid",
                                            "liked_finishes:gloss", "effects:natural"]})
        return found
    # 核心邏輯：
    # 1. 只看否定語氣＋「亮／艷／閃」，排除「亮面、亮澤、亮眼、亮片、亮粉」等已有明確意思的詞。
    # 2. 記成 blocking 模糊詞：推薦前一定先補問；使用者回答「不要鮮豔」或「不要亮面」等明確條件後自動解除。
    # 3. 不自行轉成排除鮮明或排除亮面（不替使用者選邊），但也不丟掉，需求摘要與日誌都看得到。

    # rule-v11：找出「不要／不會太…」但對象不在詞表的說法，回傳 [{raw, clause}]。
    # 對象裡有詞表的詞（霧面、橘…），或已由「不要太亮」「不要太誇張」「不要看起來累」規則處理的，都略過（避免重複）。
    @staticmethod
    def _scan_unclassified_avoid(text: str) -> list[dict]:
        found: list[dict] = []
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
            for m in _RE_NEG_PHRASE.finditer(clause):
                obj = _RE_NEG_TAIL.sub("", m.group("obj"))
                if not re.search(r"[一-鿿]", obj) or obj.startswith(("含", "對")):
                    continue   # 沒有中文對象；「不要含羊毛脂」「對…過敏」交給過敏解析
                raw = clause[m.start():m.start("obj")] + obj
                if _RE_BRIGHTNESS.search(raw) or _RE_NOT_DRAMATIC.search(raw) or _RE_VITALITY.search(raw):
                    continue
                if any(_TOKEN_INDEX[t.group(0)][0] == "term" for t in _TOKEN_RE.finditer(obj)):
                    continue
                if raw not in [f["raw"] for f in found]:
                    found.append({"raw": raw, "clause": clause})
        return found
    # 核心邏輯：
    # 1. 詞表永遠補不齊；認得的照常分類，不認得的也不丟，至少讓模型看到使用者的原話。
    # 2. 不推定意思、不補問（避免一次問兩題），也不在回答裡提醒使用者（2026-10-27）。

    # 把一個詞＋它的語氣轉成單一變更（或模糊詞）。沒有語氣的色系／妝感只是被提到，不記成偏好。
    @staticmethod
    def _term_change(kind: str, key: str, label: str, raw: str, polarity: str | None,
                     replace_flag: bool, strong: bool, degree: bool) -> dict | None:
        if kind == "effect":
            if polarity == "dislike":
                return {"op": "add", "field": "avoided_effects", "key": key, "label": label, "raw": raw,
                        "replace": replace_flag}
            if polarity == "cancel":
                return {"op": "cancel", "field": "avoided_effects", "key": key, "label": label, "raw": raw,
                        "strong": strong}
            return {"op": "add", "field": "effects", "key": key, "label": label, "raw": raw, "replace": replace_flag}
        liked, disliked = (("liked_colors", "disliked_colors") if kind == "color"
                           else ("liked_finishes", "disliked_finishes"))
        if polarity is None:
            return {"op": "mention", "field": liked, "key": key, "label": label, "raw": raw, "replace": False}
        if polarity == "cancel":
            return {"op": "cancel", "field": disliked, "key": key, "label": label, "raw": raw, "strong": strong}
        if polarity == "dislike" and degree and kind == "color":
            return {"op": "ambiguity", "ambiguity": {
                "kind": "degree", "field": disliked, "key": key, "label": label, "raw": raw, "blocking": True,
                "resolves_with": [f"disliked_colors:{key}", f"liked_colors:{key}", "avoided_effects:vivid",
                                  "effects:vivid", "effects:natural"]}}
        target = liked if polarity == "like" else disliked
        return {"op": "add", "field": target, "key": key, "label": label, "raw": raw, "replace": replace_flag}
    # 核心邏輯：效果沒有語氣時也算「想要」（「上班用的低調唇色」→ 自然低調）；
    # 色系與妝感沒有語氣時標成 mention，parse() 不把它算成偏好（rule-v15）。

    # 自述膚色、明說的底調、原生唇色。只記原話，不推論。
    @staticmethod
    def _scan_profile(text: str) -> list[dict]:
        changes: list[dict] = []
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
            undertone = RuleParser._self_undertone(clause)
            if undertone == "rule_claim":
                continue  # 「小麥色就是暖底」：整句是使用者宣稱的規則，膚色與底調都不當成自述
            if _RE_QUESTION_WORD.search(clause) and not _SELF_SUBJECT.search(clause):
                continue  # K5：「小麥色是不是暖底」「暖底適合什麼唇色」是一般問句，膚色與底調都不寫入
            for tone in sorted(VOCAB["skin_tones"], key=len, reverse=True):
                if tone in clause:
                    changes.append({"op": "set", "field": "skin_tone", "value": tone, "label": tone, "raw": clause})
                    break
            else:
                desc = _RE_SKIN_DESC.search(clause)        # rule-v15（S1）：詞表沒命中才看口語描述
                if desc:
                    tone = "膚色偏" + desc.group(1)
                    changes.append({"op": "set", "field": "skin_tone", "value": tone, "label": tone, "raw": clause})
            if undertone and _RE_UNDERTONE_HEDGE.search(clause):
                # K4：暫定自述（「我覺得我比較像冷底」）：底調值維持未知，只保留說法與傾向
                changes.append({"op": "set", "field": "undertone", "value": None, "tentative": undertone,
                                "label": f"不確定（你說可能是{UNDERTONE_LABELS[undertone]}）", "raw": clause})
            elif undertone:
                changes.append({"op": "set", "field": "undertone", "value": undertone,
                                "label": UNDERTONE_LABELS[undertone], "raw": clause})
            lip = re.search(r"(原生唇色|原本的?唇色|天生唇色|唇色本身|我的?唇色|我的?嘴唇|自己的?唇色|^唇色|^嘴唇)"
                            r"[^，。]{0,6}?(" + "|".join(map(re.escape, VOCAB["lip_descriptors"])) + ")", clause)
            if lip:
                changes.append({"op": "set", "field": "natural_lip", "value": lip.group(2),
                                "label": lip.group(2), "raw": clause})
        return changes
    # 核心邏輯：膚色詞只記原詞（例如「小麥色」），底調欄位只有使用者自己說出暖／冷／中性底才會填。
    # rule-v5：一般問句（沒有我／本人／自己）整句不寫入（K5）；混合句依子句分開，「我是小麥色，是不是暖底」只記膚色；
    # 帶「覺得／比較像…」的底調是暫定自述，值仍是未知（K4）。

    # rule-v3：判斷一個子句裡的底調詞是不是使用者「自述」自己的底調。
    # 回傳 "warm"／"cool"／"neutral"（自述）、"rule_claim"（使用者宣稱的規則，整句不當自述）或 None（不記）。
    @staticmethod
    def _self_undertone(clause: str) -> str | None:
        for word, key in sorted(VOCAB["undertones"].items(), key=lambda kv: len(kv[0]), reverse=True):
            pos = clause.find(word)
            if pos < 0:
                continue
            if re.search(r"(不知道|不確定|不清楚|是不是|是否)", clause) or _RE_UNDERTONE_QUESTION.search(clause):
                return None  # 不確定或在發問（「什麼是暖底調」「我是暖底嗎」）
            link = _RE_RULE_LINK.search(clause, 0, pos)
            if link and not _SELF_SUBJECT.search(clause[:link.start()]):
                return "rule_claim"  # 「小麥色就是暖底」「黃皮＝暖皮」：連結詞前面不是在說自己
            return key
        return None
    # 核心邏輯：
    # 1. 子句沒有底調詞 → None；有底調詞但帶不確定或問句 → None（維持未知，規範 5）。
    # 2. 底調詞前面有「就是／等於／代表…」，且連結詞前的主詞不是「我／本人／自己」→ 規則宣稱，不記。
    # 3. 其餘（「我是冷底」「冷底，想要日常」）→ 照原本行為記成使用者自述。

    # 預算：只記數字與幣別原話；商品功能未啟用時不會被用來推薦商品。
    @staticmethod
    def _scan_budget(text: str) -> dict | None:
        m = _RE_BUDGET.search(text)
        if not m:
            return None
        amount_text = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        amount = int(amount_text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        currency = "TWD" if unit in ("元", "塊", "台幣", "NT", "nt") else None
        return {"op": "set", "field": "budget", "value": {"max": amount, "currency": currency},
                "label": f"{amount} {'元' if currency else '（幣別未說明）'}", "raw": m.group(0)}

    # 過敏：四種狀態與需避開項目。沒提到過敏就回傳 None（維持原狀，不會變成無已知）。
    @staticmethod
    def _scan_allergy(text: str) -> dict | None:
        add: list[str] = []
        remove: list[str] = []
        for m in _RE_ALLERGY_REMOVE.finditer(text):
            remove += _split_items(m.group(1))
        for rx in (_RE_ALLERGY_ITEM, _RE_ALLERGY_AVOID):
            for m in rx.finditer(text):
                add += [i for i in _split_items(m.group(1)) if not re.search(r"沒|不|無|什麼|哪些|哪種", i)]
        unsure = _RE_ALLERGY_UNSURE.search(text)
        none = None if unsure else _RE_ALLERGY_NONE.search(text)
        if unsure:
            status = "unsure"
        elif none and not add:
            status = "none_known"
        elif add or _RE_ALLERGY_HAS.search(text):
            status = "has_known"
        elif remove:
            status = None
        else:
            return None
        raw = (unsure or none or _RE_ALLERGY_ITEM.search(text) or _RE_ALLERGY_AVOID.search(text)
               or _RE_ALLERGY_HAS.search(text) or _RE_ALLERGY_REMOVE.search(text)).group(0)
        return {"status": status, "add": add, "remove": remove, "raw": raw,
                "confirmed": bool(none and none.group(1))}
    # 核心邏輯：
    # 1. 「不確定有沒有過敏」優先判成 unsure，避免被「沒有…過敏」誤判。
    # 2. 「對羊毛脂過敏」→ has_known＋需避開羊毛脂；「不用再避開 X」→ 移除 X。
    # 3. 「確定沒有過敏」帶 confirmed，用來解除「已有需避開項目卻說沒有過敏」的衝突。

    # 判斷主要意圖與次要意圖（混合問題拆開處理）。
    @staticmethod
    def _classify(text: str, has_changes: bool) -> tuple[str, list[str]]:
        found: list[str] = []
        if _requests_restricted(text):
            found.append("restricted_request")
        if outside_hit(text):
            found.append("outside_scope")
        if product_hit(text):
            found.append("product_request")
        n_terms = len({_TOKEN_INDEX[m.group(0)][1][:2] for m in _TOKEN_RE.finditer(text)
                       if _TOKEN_INDEX[m.group(0)][0] == "term" and _TOKEN_INDEX[m.group(0)][1][0] in ("color", "finish")})
        if _RE_COMPARE_STRONG.search(text) or (_RE_COMPARE_WEAK.search(text) and n_terms >= 2):
            found.append("compare")
        update_words = bool(_RE_UPDATE.search(text))
        asks = bool(_RE_RECOMMEND.search(text) or _RE_KNOWLEDGE.search(text))
        # rule-v14（K1）：沒被提問詞認出、但本身是問句或指令句 → 不走下面兩條「有條件就推薦／修改」的預設
        unrecognized_answer_request = not asks and not update_words and bool(_RE_ANSWER_REQUEST.search(text))
        if has_changes and not update_words and not asks and not unrecognized_answer_request \
                and _RE_LIP_ITEM.search(text):
            found.append("recommend")  # 「上班用的低調唇色」：描述想找的唇彩，視為推薦需求
        elif has_changes and (update_words or (not asks and not unrecognized_answer_request)):
            found.append("update")
        if _RE_KNOWLEDGE.search(text):
            found.append("knowledge")
        if _RE_RECOMMEND.search(text):
            found.append("recommend")
        if not found:
            # 什麼都沒命中：回答要求一律走知識（問答）；只有不是回答要求、又帶了條件的陳述才視為推薦需求
            found.append("recommend" if has_changes and not unrecognized_answer_request else "knowledge")
        found = list(dict.fromkeys(found))
        return found[0], found[1:]
    # 核心邏輯：優先序 索取非公開資料 → 範圍外 → 商品 → 比較 → 修改 → 知識 → 推薦。
    # 只有條件、沒有提問（例如「我不要霧面」）算修改；描述想找的唇彩算推薦；
    # 什麼線索都沒有時當成知識問答，不硬進推薦。
    # rule-v14（K1）：問句或指令句沒被提問詞認出時，不因帶了條件就變成推薦或修改，一律當知識問答；
    # 明確的修改詞（改成、取消…）、推薦詞、比較、商品與範圍外規則都不受影響。推薦詞過泛（K3）不在這次處理。


# rule-v6：商品脈絡詞（品牌、色號、專櫃…）和挑選／推薦動作出現在同一子句，才算在要商品。
def _mentions_product_with_seek(text: str) -> bool:
    return any(_RE_PRODUCT_CONTEXT.search(c) and _RE_PRODUCT_SEEK.search(c) and not _RE_COLOR_REFERENCE.search(c)
               for c in (c.strip() for c in _CLAUSE_SPLIT.split(text)))


# rule-v15：範圍外的判斷集中在這裡（_classify 與 intent_trace 共用）；回傳命中的字樣，沒有就回 None。
def outside_hit(text: str) -> str | None:
    m = _RE_OUTSIDE.search(text)
    if m:
        return m.group(0)
    m = _RE_NON_LIP_COLOR.search(text)
    return m.group(0) if m and not _RE_LIP_TOPIC.search(text) else None


# rule-v15：商品要求的判斷集中在這裡（_classify 與 intent_trace 共用）；回傳命中的字樣，沒有就回 None。
def product_hit(text: str) -> str | None:
    for pat in (_RE_PRODUCT, _RE_PRODUCT_PICK):
        m = pat.search(text)
        if m:
            return m.group(0)
    return "品牌／色號＋挑選" if _mentions_product_with_seek(text) else None
# 核心邏輯：以子句為單位，避免「色號名字很抽象」與後面別的子句的字湊在一起被誤判。


# rule-v14（I03）：同一子句裡同時有索取動作與受限對象（其他使用者的對話／資料、系統提示）才算。
def _requests_restricted(text: str) -> bool:
    return any(_RE_RESTRICTED_TARGET.search(c) and _RE_RESTRICTED_VERB.search(c)
               for c in (c.strip() for c in _CLAUSE_SPLIT.split(text)))
# 核心邏輯：不看「我是管理員」「授權稽核」這類身分說法（權限只由後端認證決定）；
# 只問「其他使用者看得到我的對話嗎」「系統提示是什麼意思」這種討論，沒有索取動作，不攔。


# 「以 X 為準」→「要 X」；X 本身已有語氣（例如「以不要霧面為準」）就保留原語氣。
def _resolve_phrase(m: re.Match) -> str:
    inner = m.group(1)
    return inner if re.match(r"不|別|取消|改", inner) else "要" + inner


# 把「霧面和羊毛脂」這種列舉拆成單項並去除空白。
def _split_items(text: str) -> list[str]:
    return [p.strip(" 「」『』\"'") for p in _RE_ITEM_SPLIT.split(text) if p.strip(" 「」『』\"'")]


# 同一則訊息裡同一個鍵同時被要與不要（且沒有改成／取消語意）→ 抽出成衝突，兩邊都不套用。
def _split_in_message_conflicts(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    sides: dict[tuple[str, str], set[str]] = {}
    for c in changes:
        if c["op"] == "add" and not c.get("replace") and c["field"] in OPPOSITE:
            base = c["field"].replace("disliked_", "").replace("liked_", "").replace("avoided_", "")
            sides.setdefault((base, c["key"]), set()).add(c["field"])
    clashing = {k for k, fields in sides.items() if len(fields) > 1}
    kept, conflicts = [], []
    for c in changes:
        base = c["field"].replace("disliked_", "").replace("liked_", "").replace("avoided_", "")
        if c["op"] == "add" and (base, c.get("key")) in clashing:
            same = [x for x in conflicts if x["key"] == c["key"] and x["field"] == base]
            if same:
                if c["raw"] not in same[0]["raw"].split("／"):
                    same[0]["raw"] += "／" + c["raw"]   # 兩邊的原話都保留，例如「霧面／不要霧面」
            else:
                conflicts.append({"field": base, "key": c["key"], "label": c["label"], "raw": c["raw"],
                                  "kind": "in_message"})
            continue
        kept.append(c)
    return kept, conflicts
# 核心邏輯：以「色系／妝感／效果＋鍵」為單位比對，例如「霧面，不要霧面」→ finishes:matte 衝突（規格 R04）。


# ---------------------------------------------------------------------------
# 合併：把 ParseResult 套用到既有狀態（純函式，不改傳入的 state）
# ---------------------------------------------------------------------------
@dataclass
class MergeOutcome:
    state: dict
    applied: list[dict]          # 實際生效的變更（含前後值的說明）
    new_conflicts: list[dict]    # 這次新增、等待使用者決定的衝突
    resolved: list[dict]         # 這次被解決的衝突／模糊詞


# 把解析結果套用到偏好狀態，回傳新狀態與變更說明。
def apply_parse(state: dict, parsed: ParseResult, *, request_id: str | None, now: str,
                source: str = "chat") -> MergeOutcome:
    new = copy.deepcopy(state)
    applied: list[dict] = []
    new_conflicts: list[dict] = []
    resolved: list[dict] = []
    for c in parsed.changes:
        result = _apply_change(new, c, request_id=request_id, now=now, source=source)
        if result.get("conflict"):
            new_conflicts.append(result["conflict"])
        elif result.get("applied"):
            applied.append(result["applied"])
            resolved += result.get("resolved", [])
    for conf in parsed.conflicts:
        new_conflicts.append({**conf, "request_id": request_id, "created_at": now})
    if parsed.allergy:
        result = _apply_allergy(new, parsed.allergy, request_id=request_id, now=now, source=source)
        if result.get("conflict"):
            new_conflicts.append(result["conflict"])
        if result.get("applied"):
            applied.append(result["applied"])
            resolved += result.get("resolved", [])
    for conf in new_conflicts:
        if not any(_same_conflict(conf, x) for x in new["conflicts"]):
            new["conflicts"].append(conf)
    touched = {f"{a['field']}:{a.get('key')}" for a in applied}
    keep = []
    for amb in new["ambiguities"]:
        if touched & set(amb.get("resolves_with", [])):
            resolved.append({"type": "ambiguity", **amb})
        else:
            keep.append(amb)
    new["ambiguities"] = keep
    _label_fields(applied)
    for amb in parsed.ambiguities:
        if not any(a["kind"] == amb["kind"] and a["key"] == amb["key"] for a in new["ambiguities"]):
            new["ambiguities"].append({**amb, "request_id": request_id, "created_at": now})
    # rule-v11：未分類的否定說法照原話累積（舊對話狀態沒有這個欄位就補上）；同一句原話不重複記
    avoid = new.setdefault("unclassified_avoid", [])
    for u in getattr(parsed, "unclassified_avoid", None) or []:
        if u["raw"] not in [x.get("raw") for x in avoid]:
            avoid.append({"raw": u["raw"], "clause": u.get("clause"), "source": source,
                          "request_id": request_id, "created_at": now})
    return MergeOutcome(new, applied, new_conflicts, resolved)
# 核心邏輯：
# 1. 逐一套用變更；和既有條件相反又沒有修改語意的，轉成衝突，不套用。
# 2. 句內衝突直接加入待決衝突；同一衝突不重複加入。
# 3. 若這次的變更動到某個模糊詞對應的欄位（例如回答了「不要太紅」是指不要鮮豔），模糊詞視為已釐清。
# 4. 沒被提到的欄位完全不動——這就是「改場合保留不要霧面」的保證。


# 在每筆已套用變更加上欄位中文名稱（例如 disliked_finishes → 排斥的妝感／質地），前端直接顯示。
def _label_fields(applied: list[dict]) -> None:
    for a in applied:
        a["field_label"] = FIELD_LABELS.get(a["field"], a["field"])


# 判斷兩個衝突是不是同一件事（同欄位、同鍵）。
def _same_conflict(a: dict, b: dict) -> bool:
    return a["field"] == b["field"] and a.get("key") == b.get("key")


# 取出某欄位基底（liked_finishes → finishes）對應的待決衝突，並從狀態中移除；回傳被移除的衝突。
def _pop_conflict(state: dict, base: str, key: str | None) -> list[dict]:
    hit = [c for c in state["conflicts"] if c["field"] == base and (key is None or c.get("key") == key)]
    state["conflicts"] = [c for c in state["conflicts"] if c not in hit]
    return [{"type": "conflict", **c} for c in hit]


# 套用單一變更。回傳 {"applied": 說明} 或 {"conflict": 衝突}；什麼都沒改時回傳空字典。
def _apply_change(state: dict, c: dict, *, request_id: str | None, now: str, source: str) -> dict:
    fld = c["field"]
    if c["op"] == "clear":
        return _clear_occasion(state, c, request_id=request_id, now=now, source=source)
    if c["op"] == "set_multi":
        before = state[fld].get("values") or state[fld].get("value")
        state[fld] = {"value": None, "values": list(c["values"]), "status": "user", "raw": c["raw"],
                      "source": source, "request_id": request_id, "updated_at": now}
        return {"applied": {"field": fld, "op": "set", "key": None, "before": before, "after": list(c["values"]),
                            "label": c["label"], "raw": c["raw"]}, "resolved": _pop_conflict(state, fld, None)}
    if c["op"] == "set":
        before = state[fld].get("value")
        state[fld] = {"value": c["value"], "status": "tentative" if c.get("tentative") else "user",
                      "raw": c["raw"], "source": source, "request_id": request_id, "updated_at": now}
        if c.get("tentative"):
            state[fld]["tentative"] = c["tentative"]   # K4：只記傾向，值仍是未知
        resolved = _pop_conflict(state, fld, None)
        return {"applied": {"field": fld, "op": "set", "key": c["value"] if isinstance(c["value"], str) else None,
                            "before": before, "after": c["value"], "label": c["label"], "raw": c["raw"]},
                "resolved": resolved}
    base = fld.replace("disliked_", "").replace("liked_", "").replace("avoided_", "")
    opposite = OPPOSITE[fld]
    if c["op"] == "cancel":
        removed = _list_remove(state[fld], c["key"])
        if not removed and c.get("strong"):
            removed = _list_remove(state[opposite], c["key"])
            fld = opposite if removed else fld
        resolved = _pop_conflict(state, base, c["key"])
        if not removed and not resolved:
            return {}
        return {"applied": {"field": fld, "op": "remove", "key": c["key"], "label": c["label"], "raw": c["raw"]},
                "resolved": resolved}
    # op == add
    pending = [x for x in state["conflicts"] if x["field"] == base and x.get("key") == c["key"]]
    in_opposite = any(i["value"] == c["key"] for i in state[opposite])
    if in_opposite and not c.get("replace") and not pending:
        return {"conflict": {"field": base, "key": c["key"], "label": c["label"], "raw": c["raw"],
                             "kind": "against_existing", "existing_field": opposite, "new_field": fld,
                             "request_id": request_id, "created_at": now}}
    if in_opposite:
        _list_remove(state[opposite], c["key"])
    if c.get("replace") and fld == "liked_finishes":
        state[fld] = []  # 「改成霧面」取代原本的妝感設定
    _list_add(state[fld], _item(c["key"], c["label"], c["raw"], source, request_id, now))
    resolved = _pop_conflict(state, base, c["key"])
    return {"applied": {"field": fld, "op": "add", "key": c["key"], "label": c["label"], "raw": c["raw"],
                        "removed_from": opposite if in_opposite else None},
            "resolved": resolved}
# 核心邏輯：
# 1. 單值欄位（場合、膚色…）直接取代，並解除該欄位的待決衝突（例如兩個場合的衝突被單一回答解決）。
#    rule-v5：多情境（set_multi）存在 occasion.values；暫定底調 status = tentative、value 仍是 None。
# 2. 取消：從排斥清單移除；用「取消／拿掉」等強語氣且排斥清單沒有時，才從喜好清單移除。不反向新增喜好。
# 3. 新增：與相反清單衝突時，只有「改成」語意或正在回答待決衝突才會改邊，否則記成衝突（不自行選邊）。


# rule-v5（K2）：撤回場合。目前場合是被撤回的那個 → 回到未知；多情境只拿掉被撤回的那個；其他偏好都不動。
def _clear_occasion(state: dict, c: dict, *, request_id: str | None, now: str, source: str) -> dict:
    occ = state["occasion"]
    current = list(occ.get("values") or ([occ["value"]] if occ.get("value") else []))
    remaining = [v for v in current if v not in c["keys"]]
    if remaining == current:
        return {}  # 撤回的不是目前的場合：什麼都不改
    if not remaining:
        state["occasion"] = {**_unknown(), "raw": c["raw"], "source": source, "request_id": request_id,
                             "updated_at": now}
    else:
        state["occasion"] = {"value": remaining[0] if len(remaining) == 1 else None, "status": "user",
                             "raw": c["raw"], "source": source, "request_id": request_id, "updated_at": now}
        if len(remaining) > 1:
            state["occasion"]["values"] = remaining
    return {"applied": {"field": "occasion", "op": "remove", "key": None, "before": current,
                        "after": remaining, "label": "場合", "raw": c["raw"]},
            "resolved": _pop_conflict(state, "occasion", None)}


# 套用過敏變更。
def _apply_allergy(state: dict, a: dict, *, request_id: str | None, now: str, source: str) -> dict:
    allergy = state["allergy"]
    before = {"status": allergy["status"], "avoid": [i["value"] for i in allergy["avoid"]]}
    if a["status"] == "none_known" and allergy["avoid"] and not a.get("confirmed"):
        return {"conflict": {"field": "allergy", "key": None, "label": "、".join(before["avoid"]),
                             "raw": a["raw"], "kind": "against_existing",
                             "request_id": request_id, "created_at": now}}
    for item in a.get("remove", []):
        _list_remove(allergy["avoid"], item)
    if a["status"] == "none_known":
        allergy["avoid"] = []
    for item in a.get("add", []):
        _list_add(allergy["avoid"], _item(item, item, a["raw"], source, request_id, now))
    if a["status"]:
        allergy["status"] = a["status"]
    allergy.update({"raw": a["raw"], "source": source, "request_id": request_id, "updated_at": now})
    after = {"status": allergy["status"], "avoid": [i["value"] for i in allergy["avoid"]]}
    resolved = _pop_conflict(state, "allergy", None) if a["status"] == "none_known" else []
    if before == after and not resolved:
        return {}
    return {"applied": {"field": "allergy", "op": "set", "key": None, "before": before, "after": after,
                        "label": ALLERGY_LABELS[allergy["status"]], "raw": a["raw"]}, "resolved": resolved}
# 核心邏輯：已經記錄需避開項目、這次卻說「沒有過敏」→ 不清掉安全相關紀錄，先補問；
# 使用者說「確定沒有過敏」才清除。狀態只在使用者明確提到時改變。


# ---------------------------------------------------------------------------
# 表單（PATCH）→ 同一組變更規則
# ---------------------------------------------------------------------------
class FormInvalid(ValueError):
    """表單內容不合規則（例如未回答過敏卻填需避開項目）。訊息會直接顯示給使用者。"""


class FormConflict(FormInvalid):
    """表單同一個詞同時放在想要與不要。"""


# 把需求設定表單的欄位套用到偏好狀態。表單是使用者逐欄明確編輯，因此以「取代該欄」處理。
def apply_form(state: dict, form: dict, *, now: str) -> MergeOutcome:
    new = copy.deepcopy(state)
    applied: list[dict] = []
    resolved: list[dict] = []
    edited: set[str] = set()   # 這次表單親手改過的欄位基底（finishes、colors、occasion…）

    # 記錄一筆表單變更（前後值相同就不記）。
    def note(fld: str, before: Any, after: Any) -> None:
        if before != after:
            applied.append({"field": fld, "op": "replace" if isinstance(after, list) else "set", "key": None,
                            "before": before, "after": after, "label": FIELD_LABELS[fld], "raw": None})

    # 1. 想要／不要／質地：詞語依詞彙表分到色系或妝感，各欄整欄取代
    liked = _route_terms(form["liked"]) if "liked" in form else None
    disliked = _route_terms(form["disliked"]) if "disliked" in form else None
    plan: dict[str, list[tuple[str, str, str]]] = {}
    if liked is not None:
        plan["liked_colors"], plan["liked_finishes"] = liked["color"], liked["finish"]
    if "finish" in form:
        finishes = list(plan.get("liked_finishes", []))
        if form["finish"] in ("matte", "satin", "gloss"):
            key = form["finish"]
            finishes = [f for f in finishes if f[0] != key] + [(key, VOCAB["labels"][("finish", key)], key)]
        plan["liked_finishes"] = finishes
    if disliked is not None:
        plan["disliked_colors"], plan["disliked_finishes"] = disliked["color"], disliked["finish"]
    if "effects" in form:
        plan["effects"] = [(key, label, t) for t in form["effects"] if t.strip()
                           for _, key, label in [classify_term(t)]]
    for fld in ("liked_colors", "liked_finishes"):
        opp = OPPOSITE[fld]
        if fld in plan and opp in plan:
            labels = {k: lbl for k, lbl, _ in plan[fld]}
            clash = sorted(labels[k] for k in labels.keys() & {k for k, _, _ in plan[opp]})
            if clash:
                raise FormConflict(f"同一個詞不能同時放在「想要」和「不要」：{'、'.join(clash)}")
    for fld, entries in plan.items():
        before = [i["value"] for i in new[fld]]
        existing = {i["value"]: i for i in new[fld]}
        # 沒變的項目保留原本的原話、來源與 request_id；只有新加入的項目標成表單
        new[fld] = [existing.get(k) or _item(k, lbl, raw, "form", None, now) for k, lbl, raw in entries]
        if OPPOSITE[fld] not in plan:
            for key in {k for k, _, _ in entries}:
                _list_remove(new[OPPOSITE[fld]], key)  # 表單明確放到這一邊 → 從另一邊移除
        if before != [k for k, _, _ in entries]:  # 只有真的改動才算回答了該欄的衝突
            edited.add(fld.replace("disliked_", "").replace("liked_", "").replace("avoided_", ""))
        note(fld, before, [k for k, _, _ in entries])

    # 2. 單值文字欄位：空字串代表清除（回到未知）
    for fld in ("skin_tone", "natural_lip", "notes"):
        if fld in form:
            value = (form[fld] or "").strip() or None
            before = new[fld]["value"]
            if value == before:
                continue  # 值沒變：保留原話（例如「我是小麥色」）不被表單覆蓋
            new[fld] = _unknown() if value is None else {"value": value, "status": "user", "raw": value,
                                                          "source": "form", "request_id": None, "updated_at": now}
            note(fld, before, value)
    if "undertone" in form:
        value = None if form["undertone"] in (None, "unknown") else form["undertone"]
        before = new["undertone"]["value"]
        if value != before:
            new["undertone"] = _unknown() if value is None else {
                "value": value, "status": "user", "raw": UNDERTONE_LABELS[value], "source": "form",
                "request_id": None, "updated_at": now}
            note("undertone", before, value)

    # 3. 情境
    if form.get("scenario") and (form["scenario"] != new["occasion"]["value"] or new["occasion"]["status"] != "user"):
        before = new["occasion"]["value"]
        new["occasion"] = {"value": form["scenario"], "status": "user", "raw": None, "source": "form",
                           "request_id": None, "updated_at": now}
        edited.add("occasion")
        note("occasion", before, form["scenario"])

    # 4. 過敏：未回答／無已知時不能有需避開項目
    if "allergy_status" in form or "allergy_avoid" in form:
        allergy = new["allergy"]
        status = form.get("allergy_status") or allergy["status"]
        avoid = form.get("allergy_avoid")
        items = None if avoid is None else [a.strip() for a in avoid if a.strip()]
        if status in ("unanswered", "none_known"):
            if items:
                raise FormInvalid("過敏狀態為「未回答」或「無已知過敏」時不能填需避開項目")
            items = []
        before = {"status": allergy["status"], "avoid": [i["value"] for i in allergy["avoid"]]}
        after = {"status": status, "avoid": before["avoid"] if items is None else list(dict.fromkeys(items))}
        if after != before:  # 沒變就保留原本的原話（例如「對羊毛脂過敏」）
            existing = {i["value"]: i for i in allergy["avoid"]}
            allergy["status"] = status
            allergy["avoid"] = [existing.get(v) or _item(v, v, v, "form", None, now) for v in after["avoid"]]
            allergy.update({"raw": None, "source": "form", "request_id": None, "updated_at": now})
            edited.add("allergy")
            note("allergy", before, after)

    # 5. 使用者親手改過的欄位，視同回答了該欄的待決衝突與模糊詞
    for base in edited:
        resolved += _pop_conflict(new, base, None)
    keep = []
    for amb in new["ambiguities"]:
        base = amb["field"].replace("disliked_", "").replace("liked_", "").replace("avoided_", "")
        if base in edited:
            resolved.append({"type": "ambiguity", **amb})
        else:
            keep.append(amb)
    new["ambiguities"] = keep
    _label_fields(applied)
    return MergeOutcome(new, applied, [], resolved)
# 核心邏輯：
# 1. 只處理表單有帶的欄位（沒帶的完全不動）——改情境不會清掉排斥條件。
# 2. 同一個詞同時在想要與不要 → FormConflict（API 回 422），不自行選邊。
# 3. 過敏四狀態的限制與 UI 第一版相同；需避開項目只在 has_known／unsure 時保存。
# 4. 使用者在表單上親手編輯某欄，視同回答了該欄的衝突／模糊詞，因此一併解除。


# 把表單輸入的詞分成色系與妝感兩組：[(正規化鍵, 顯示名稱, 原詞)]。
def _route_terms(terms: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {"color": [], "finish": []}
    seen: set[tuple[str, str]] = set()
    for t in (x.strip() for x in terms):
        if not t:
            continue
        kind, key, label = classify_term(t)
        bucket = "finish" if kind == "finish" else "color"
        if (bucket, key) not in seen:
            seen.add((bucket, key))
            out[bucket].append((key, label, t))
    return out


# ---------------------------------------------------------------------------
# 補問與摘要
# ---------------------------------------------------------------------------
# 決定這一輪要問什麼：阻斷性問題（衝突、會左右排除的模糊詞）最多 2 題；沒有阻斷時最多 1 題非阻斷問題。
def plan_clarification(state: dict, intent: str, secondary: list[str]) -> tuple[list[dict], dict]:
    new = copy.deepcopy(state)
    blocking = [_conflict_question(c) for c in new["conflicts"]]
    blocking += [_ambiguity_question(a) for a in new["ambiguities"] if a.get("blocking")]
    if blocking:
        return blocking[:2], new
    # 純修改條件（例如只說「我對羊毛脂過敏」）不追問其他欄位；有推薦需求才問會影響推薦的非阻斷問題
    wants_recommendation = intent == "recommend" or "recommend" in secondary
    candidates: list[dict] = []
    if wants_recommendation and not new["effects"] and new["asked"].get("effects", 0) == 0:
        candidates.append({"field": "effects", "blocking": False,
                           "question": "希望效果比較自然低調，還是鮮明有精神？"})
    for amb in new["ambiguities"]:
        if not amb.get("blocking") and new["asked"].get(f"ambiguity:{amb['key']}", 0) == 0:
            candidates.append({"field": f"ambiguity:{amb['key']}", "blocking": False,
                               "question": _ambiguity_question(amb)["question"]})
    questions = candidates[:1]
    for q in questions:
        new["asked"][q["field"]] = new["asked"].get(q["field"], 0) + 1
    return questions, new
# 核心邏輯（推薦決策規格 §4）：
# 1. 優先序：硬條件衝突 → 會左右排除的模糊詞 → 希望效果 → 其他模糊詞。
# 2. 只有兩個必要且獨立的阻斷問題才一次問兩題；一般只問一題。
# 3. 非阻斷問題問過一次就記在 asked，使用者略過就不再追問，有可用起點就先給起點。


# 衝突 → 補問句。
def _conflict_question(c: dict) -> dict:
    if c["field"] == "occasion":
        q = f"這次提到{'和'.join('「' + x + '」' for x in c['labels'])}，要以哪個場合為主？"
    elif c["field"] == "allergy":
        q = (f"之前記錄需避開「{c['label']}」，這次又說沒有過敏。要清除這些需避開項目嗎？"
             "（回覆「確定沒有過敏」才會清除；系統不做過敏判斷）")
    elif c.get("kind") == "against_existing":
        verb_old = "不要" if c["existing_field"].startswith(("disliked", "avoided")) else "想要"
        verb_new = "想要" if verb_old == "不要" else "不要"
        q = (f"之前設定{verb_old}「{c['label']}」，這次又提到{verb_new}「{c['label']}」。"
             f"要改成{verb_new}，還是維持{verb_old}？")
    else:
        q = f"同時提到想要和不要「{c['label']}」，要以哪一個為準？"
    return {"field": c["field"], "key": c.get("key"), "blocking": True, "question": q}


# 模糊詞 → 補問句。
def _ambiguity_question(a: dict) -> dict:
    if a["kind"] == "degree":
        q = f"「{a['raw']}」是指不要太鮮豔（彩度高）、完全不要{a['label']}，還是其他意思？"
    elif a["kind"] == "brightness":
        q = f"「{a['raw']}」是指顏色不要太鮮豔，還是不要亮面光澤的質地？（也可以兩者都不要）"
    elif a["kind"] == "appearance_vs_performance":
        q = "「水潤」是指看起來有光澤，還是希望有保濕感？（系統只能描述外觀，不判斷保濕性能）"
    else:
        q = f"「{a['raw']}」是什麼意思？"
    return {"field": a["field"], "key": a.get("key"), "blocking": bool(a.get("blocking")), "question": q}


# 產生前端顯示用的需求摘要：每列有欄位、顯示值、狀態（使用者／表單／預設／未知）、原話、是否為硬條件。
def summarize(state: dict) -> dict:
    rows: list[dict] = []

    # 加入一列摘要。
    def row(fld: str, value: str, status: str, raw: Any = None, hard: bool = False, note: str | None = None):
        rows.append({"field": fld, "label": FIELD_LABELS[fld], "value": value, "status": status,
                     "raw": raw, "hard": hard, "note": note})

    occ = state["occasion"]
    occ_keys = occ.get("values") or ([occ["value"]] if occ.get("value") else [])
    occ_label = "、".join(VOCAB["labels"].get(("occasion", k), k) for k in occ_keys) or "未指定"
    row("occasion", occ_label, occ["status"] if occ_keys else "unknown", occ.get("raw"),
        note="多個情境都要；推薦時任一情境有依據就算符合" if len(occ_keys) > 1 else None)
    for fld in ("skin_tone", "natural_lip"):
        v = state[fld]
        row(fld, v["value"] or "未提供", v["status"], v.get("raw"))
    ut = state["undertone"]
    if ut.get("tentative"):
        row("undertone", f"不確定（你說可能是{UNDERTONE_LABELS[ut['tentative']]}）", "tentative", ut.get("raw"),
            note="暫定說法，不當成確定底調")
    else:
        row("undertone", UNDERTONE_LABELS.get(ut["value"], "未知"), ut["status"], ut.get("raw"),
            note=None if ut["value"] else "不從膚色推定底調")
    for fld in LIST_FIELDS:
        items = state[fld]
        row(fld, "、".join(i["label"] for i in items) or "未提供", "user" if items else "unknown",
            [i["raw"] for i in items] or None, hard=fld in HARD_FIELDS and bool(items))
    b = state["budget"]
    row("budget", "未提供" if b["value"] is None else
        f"{b['value']['max']} {'元' if b['value']['currency'] else '（幣別未說明）'}",
        b["status"], b.get("raw"), note=None if b["value"] is None else "商品功能未啟用，只記錄")
    al = state["allergy"]
    row("allergy", ALLERGY_LABELS[al["status"]], al["status"], al.get("raw"),
        note="尚未回答不等於沒有已知過敏" if al["status"] == "unanswered" else None)
    row("allergy_avoid", "、".join(i["value"] for i in al["avoid"]) or "無", "user" if al["avoid"] else "unknown",
        [i["raw"] for i in al["avoid"]] or None, hard=bool(al["avoid"]))
    n = state["notes"]
    row("notes", n["value"] or "未提供", n["status"], n.get("raw"))
    tq = state["technique"]
    if tq["value"]:
        want = "想換和平常不一樣的擦法" + (f"、{tq['keep']}" if tq.get("keep") else "")
        row("technique", f"平常：{tq['value']}（{want}）", tq["status"], tq.get("raw"))
    return {"rows": rows, "conflicts": state["conflicts"], "ambiguities": state["ambiguities"]}
# 核心邏輯：摘要由後端產生，前端只負責顯示，避免兩邊各自解讀狀態；未知欄位明確寫「未提供／未知」。


# limit-v2（2026-11-03）：範圍外的限制句只寫題目真的碰到的那一類，不再一律附
# 「不提供照片判讀、膚況或過敏診斷」。問的對象不是唇彩（裙子、頭髮…）時說明句已交代，不另加限制。
_RE_OUTSIDE_MEDICAL = re.compile(r"診斷|皮膚病|處方|醫生|過敏測試|過敏原檢測")
_RE_OUTSIDE_TRADE = re.compile(r"下單|訂購|結帳|付款|刷卡|加入購物車|有貨|有沒有貨|庫存|現貨|到貨|缺貨")


def outside_limit_note(outside_text: str | None) -> str | None:
    """依命中的範圍外字樣，回傳對應的一句限制；看不出類別（或沒有字樣）就回 None。"""
    if not outside_text:
        return None
    if _RE_OUTSIDE_MEDICAL.search(outside_text):
        return "不提供膚況、用藥或過敏診斷。"
    if _RE_OUTSIDE_TRADE.search(outside_text):
        return "不提供下單、付款或庫存查詢。"
    if re.search(_PHOTO_REQUEST, outside_text):
        return "不提供照片判讀。"
    return None


# 這次回應必須揭露的限制（範圍、商品、過敏）。
def limitations(state: dict, intent: str, secondary: list[str], *, mentions_photo: bool = False,
                outside_text: str | None = None) -> list[str]:
    notes: list[str] = []
    intents = {intent, *secondary}
    if "outside_scope" in intents:
        note = outside_limit_note(outside_text)
        if note:
            notes.append(note)
    if mentions_photo:
        # 照片請求可能被規則漏判；不論有沒有判成範圍外，都不能讓回覆看起來像看過或分析過圖片
        notes.append("系統沒有照片功能：沒有接收、看過或分析任何圖片，以下只依你的文字描述。")
    if "product_request" in intents or state["budget"]["value"] is not None:
        notes.append("商品功能未啟用：只能提供色系／妝感方向，不提供品牌、色號或價格。")
    if state["allergy"]["status"] in ("has_known", "unsure"):
        notes.append("已記錄需避開項目；成分核對功能未啟用，不提供具體商品，也不保證任何產品安全。")
    return notes
# rule-v5（K1）：場合未指定時不再揭露「暫以日常為預設」，因為已不套用任何預設場合。


# ---------------------------------------------------------------------------
# 一輪對話的完整處理（API 與成對案例執行器共用，確保兩邊規則一致）
# ---------------------------------------------------------------------------
@dataclass
class TurnResult:
    parsed: ParseResult
    merge: MergeOutcome
    clarification: list[dict]
    state: dict
    limitations: list[str]
    # followup-v1：這則訊息若是在回答上一輪的本輪問題，記下怎麼接續（寫進 requests.detail，可追溯）
    followup: dict | None = None
    # followup-v1：知識問答要拿去檢索的問題；回答平常擦法時是「原問題＋平常擦法」，其餘為 None（用原訊息）
    qa_question: str | None = None
    # followup-v2：True＝這一輪不推薦也不檢索（例如只說「算了」取消先前的任務）
    hold_answer: bool = False


# ---------------------------------------------------------------------------
# followup-v2：第二輪接續（回答上一輪的本輪確認問題；含糊回答、插問、取消、過期）
# ---------------------------------------------------------------------------
# 待回答最多等幾則訊息（含插問）就過期。預設 3：一次插問＋一次「你在回答哪一題」確認＋真正的回答，
# 足以涵蓋 rev1 開發題的對話；再多就很可能是舊問題，繼續等反而會把之後的訊息誤接到舊問題上。
# 可用環境變數 LIPMATCH_PENDING_MAX_TURNS 調整（1～10，超出範圍或不是整數就用預設）。
_DEFAULT_PENDING_MAX_TURNS = 3


def _pending_max_turns() -> int:
    """讀取待回答的輪數上限：環境變數 LIPMATCH_PENDING_MAX_TURNS（1～10），不合法時用預設 3。"""
    raw = os.environ.get("LIPMATCH_PENDING_MAX_TURNS", "")
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= 10 else _DEFAULT_PENDING_MAX_TURNS


# 含糊語氣：有這些詞就不算明確肯定或否定（「好像是」「應該不是」「可能吧」「不確定」）
_RE_HEDGE = re.compile(r"好像|應該|可能|大概|也許|或許|似乎|不確定|不知道|不清楚|說不定|看看吧|再想想|吧[。！!～~]*$")
# 明確肯定／否定：整則回答（去掉頭尾標點）只有肯定詞，或肯定詞後面接逗號再補充。
# 「好」單獨出現才算肯定；「好像」「好看」「好冷」的「好」不是在回答。
_AFFIRM_WORDS = r"(?:是(?:的|啊|呀)?|對(?:啊|呀|的)?|嗯+|恩+|沒錯|好(?:的|啊)?|可以|就是這樣|就是這個意思|正是|沒問題|當然|yes|ok|okay)"
_RE_AFFIRM = re.compile(rf"^{_AFFIRM_WORDS}(?:[，,。.!！～~\s]|$)", re.IGNORECASE)
_DENY_WORDS = r"(?:不是|不對|不要|不用|不太對|不想|不需要|沒有|並不是|也不是|其實不是|不|no)"
_RE_DENY = re.compile(rf"^{_DENY_WORDS}(?:[，,。.!！～~\s]|$)", re.IGNORECASE)
# 回答「平常怎麼擦」時描述擦法的說法（一定要有，才算回答了這一題；不再只看長度）
_RE_BASELINE_TECH = re.compile(r"塗滿|擦滿|全唇|整個|均勻|一層|薄擦|薄塗|厚塗|直接擦|直接塗|暈染|漸層|咬唇|點擦|點塗"
                               r"|拍開|拍勻|按壓|唇線|唇刷|手指|疊擦|疊塗|描邊|只擦|只塗|唇中|中間|內側|唇峰")
_RE_HABIT_WORD = re.compile(r"平常|平時|通常")
# 明確放棄原本的需求
_CANCEL_WORDS = r"(?:算了|不用了|不用找了|先不用|不需要了|不找了|不問了|取消|先這樣|沒事了?)"
_RE_CANCEL = re.compile(rf"^{_CANCEL_WORDS}(?:[，,。.!！～~\s]|$)")
_RE_CANCEL_ANY = re.compile(_CANCEL_WORDS)
_RE_PUNCT_EDGE = re.compile(r"^[\s，,。.!！~～…]+|[\s，,。.!！~～…]+$")
# 明確返回先前的問題（「剛剛的問題，是」「回到上一題：都塗滿」）：講明了在回答哪一題，就不用再確認
_RE_RETURN = re.compile(r"^(?:回到|關於|至於)?(?:剛剛|剛才|先前|前面|上一題)(?:的|那個|那)?(?:問題|題)?[，,：:\s]*")
# 插問：問句或新的請求（問知識、比較、推薦、商品、範圍外），不是在回答上一題
_RE_QUESTION_CUE = re.compile(r"[?？]|什麼|為什麼|怎麼|如何|哪|嗎$|呢$")


def _new_pending(question: dict, *, request_id: str | None, now: str) -> dict:
    """把一題本輪確認問題轉成待回答紀錄：題型、問句、接續上下文、問的時間、已等幾則、是否被插問打斷。"""
    return {"kind": question["kind"], "question": question["question"],
            "context": copy.deepcopy(question.get("context") or {}),
            "request_id": request_id, "asked_at": now, "turns_waited": 0, "interrupted": False}


def _classify_reply(pending: dict, text: str, parsed: ParseResult) -> str:
    """判斷這則訊息和待回答問題的關係，回傳類別：

    cancel（明確不找了）、vague（含糊）、affirm／deny（明確肯定／否定）、
    answer（內容直接回答了這一題：講了效果方向，或描述了平常擦法）、
    supplement（同一個找唇色需求的補充條件，例如場合）、interjection（插問／新的請求）、unclear（看不出來）。
    """
    answer = _RE_PUNCT_EDGE.sub("", text)
    if _RE_CANCEL.search(answer):
        return "cancel"
    stated_effects = any(c["field"] in ("effects", "avoided_effects") for c in parsed.changes)
    if pending["kind"] == "vitality" and stated_effects:
        return "answer"                              # 「不是，我想要亮眼一點」「好，但不要自然妝」
    if pending["kind"] == "technique_baseline" and _RE_BASELINE_TECH.search(answer):
        return "answer"                              # 「都塗滿」「我平常都塗滿」
    if _RE_HEDGE.search(answer):
        return "vague"
    if _RE_AFFIRM.search(answer):
        return "affirm"
    if _RE_DENY.search(answer):
        return "deny"
    real_changes = [c for c in parsed.changes if c["op"] != "mention"]
    if (pending["kind"] == "vitality" and real_changes and parsed.intent in ("recommend", "update")
            and not _RE_QUESTION_CUE.search(answer)):
        return "supplement"                          # 「要上班用的」：同一個需求多一個條件
    if (_RE_QUESTION_CUE.search(answer)
            or parsed.intent in ("compare", "product_request", "outside_scope", "restricted_request")
            or (parsed.intent == "recommend" and real_changes)):
        return "interjection"
    return "unclear"


def _reask(pending: dict, *, why: str) -> dict:
    """待回答還沒得到可用的回答時，再問一次同一題（阻斷性，這一輪不推薦也不檢索）。"""
    prefix = {"vague": "想再確認一下：",
              "mismatch": "這一題想知道的是：",
              "unclear": "還想先確認先前的問題：",
              "target": "想確認你是在回答先前的問題嗎？先前問的是："}[why]
    return {"field": f"turn:{pending['kind']}", "key": None, "blocking": True, "kind": pending["kind"],
            "question": prefix + pending["question"]}


def resolve_pending(state: dict, text: str, parsed: ParseResult, *, request_id: str | None,
                    now: str) -> dict:
    """如果上一輪有待回答的本輪問題，判斷這則訊息怎麼接續，回傳處理結果（字典）：

    state（新狀態）、parsed（可能調整過意圖與變更）、record（接續紀錄，寫進日誌）、
    keep（要繼續保留的待回答或 None）、reask（這一輪要再問的題目或 None）、
    reminder（插問時提醒先前問題還在的非阻斷提示或 None）、qa_question（檢索用問題或 None）、
    hold（True＝這一輪不推薦也不檢索，例如只說「算了」）。
    """
    out = {"state": state, "parsed": parsed, "record": None, "keep": None, "reask": None,
           "reminder": None, "qa_question": None, "hold": False}
    pending = state.get("pending_turn")
    if not pending:
        return out
    new = copy.deepcopy(state)
    new["pending_turn"] = None
    pending = {**copy.deepcopy(pending), "turns_waited": pending.get("turns_waited", 0) + 1}
    out["state"] = new
    base = {"version": FOLLOWUP_VERSION, "kind": pending["kind"], "question": pending["question"],
            "asked_request_id": pending.get("request_id"), "turns_waited": pending["turns_waited"]}
    limit = _pending_max_turns()
    if pending["turns_waited"] > limit:
        out["record"] = {**base, "result": "expired", "max_turns": limit}
        return out                                   # 過期：照一般訊息處理，不再接到舊問題
    ctx = pending["context"]
    answer = _RE_PUNCT_EDGE.sub("", text)
    if _RE_RETURN.search(answer) and _RE_RETURN.sub("", answer):
        answer = _RE_RETURN.sub("", answer)          # 明確返回：去掉「剛剛的問題，」再判斷，也不算被插問打斷
        pending["interrupted"] = False
    kind = _classify_reply(pending, answer, parsed)
    origin_intent = ctx.get("origin_intent") or ("recommend" if pending["kind"] == "vitality" else "knowledge")

    def keep(why: str, result: str) -> dict:
        """保留待回答、再問一次，意圖接回原本的需求（這一輪不推薦、不檢索）。"""
        kept = {**pending, "interrupted": False} if why == "target" else pending
        out.update(keep=kept, reask=_reask(pending, why=why),
                   parsed=replace(out["parsed"], intent=origin_intent, secondary_intents=[]),
                   record={**base, "result": result})
        return out

    if kind == "cancel":
        rest = _RE_PUNCT_EDGE.sub("", re.sub(r"[，,。.!！～~\s]+", " ", _RE_CANCEL_ANY.sub("", answer))).strip()
        out["record"] = {**base, "result": "cancelled"}
        if not rest:                                 # 只說「算了」：清掉舊任務，不另外推薦或檢索
            out["hold"] = True
            out["parsed"] = replace(parsed, intent="update", secondary_intents=[])
        return out
    if kind in ("affirm", "deny") and pending.get("interrupted"):
        return keep("target", "target_unclear")      # 插問後只說「對」：不猜它在回答哪一題
    if pending["kind"] == "vitality":
        if kind == "affirm":
            keys = ctx.get("tentative_effects", [])
            confirmed = [{"op": "add", "field": "effects", "key": k, "label": VOCAB["labels"][("effect", k)],
                          "raw": f"{text}（確認：{pending['question']}）", "replace": False} for k in keys]
            out["parsed"] = replace(parsed, changes=parsed.changes + confirmed,
                                    intent=origin_intent if parsed.intent == "knowledge" else parsed.intent)
            out["record"] = {**base, "result": "confirmed", "confirmed_effects": keys}
            return out
        if kind in ("deny", "answer"):
            denied = kind == "deny" or bool(_RE_DENY.search(answer))   # 「不是，我想要亮眼一點」是否定＋新方向
            out["record"] = {**base, "result": "denied" if denied else "corrected",
                             "dropped_tentative": ctx.get("tentative_effects", [])}
            if parsed.intent == "knowledge" and not parsed.secondary_intents:
                out["parsed"] = replace(parsed, intent=origin_intent)
            return out
        if kind == "supplement":                     # 條件照記，暫定理解仍待確認
            out.update(keep=pending, reask=_reask(pending, why="unclear"),
                       record={**base, "result": "supplemented"})
            return out
    if pending["kind"] == "technique_baseline":
        if kind == "answer":
            new["technique"] = {"value": answer, "status": "user", "raw": text, "source": "chat",
                                "request_id": request_id, "updated_at": now,
                                "goal": "想換和平常不一樣的擦法", "keep": ctx.get("keep")}
            changes = [c for c in parsed.changes
                       if not (c["field"] == "occasion" and c.get("value") == "daily" and _RE_HABIT_WORD.search(text))]
            other = any(c["op"] != "mention" for c in changes)
            out["parsed"] = replace(parsed, changes=changes,
                                    # 回答擦法時附帶條件仍是接續原問題，不能只更新偏好而不回答。
                                    intent=parsed.intent if other and parsed.intent not in ("knowledge", "update") else origin_intent)
            out["qa_question"] = f"{ctx.get('origin_text') or ''}（我平常{ctx.get('item') or '唇妝'}的擦法：{answer}）"
            out["record"] = {**base, "result": "baseline_recorded", "baseline": answer, "keep": ctx.get("keep"),
                             "dropped_habit_occasion": len(parsed.changes) - len(changes)}
            return out
        if kind in ("affirm", "deny"):
            return keep("mismatch", "type_mismatch")   # 「是／沒錯」不是平常擦法
    if kind == "vague":
        return keep("vague", "vague")                # 含糊：不升級成確認，也不丟掉原需求
    if kind == "interjection":
        out.update(keep={**pending, "interrupted": True}, record={**base, "result": "parked"},
                   reminder={"field": f"turn:{pending['kind']}:parked", "key": None, "blocking": False,
                             "kind": "parked", "question": f"（先前的問題還在，方便時再回答：{pending['question']}）"})
        return out                                   # 插問：照一般訊息處理，先前問題暫存
    return keep("unclear", "unclear")                # 「今天好冷」：看不出在回答，再問一次


# 把一次解析結果套用到對話狀態，並決定補問與要揭露的限制。
def process_parsed(state: dict, parsed: ParseResult, *, request_id: str | None, now: str,
                   text: str | None = None) -> TurnResult:
    res = (resolve_pending(state, text, parsed, request_id=request_id, now=now) if text is not None
           else {"state": state, "parsed": parsed, "record": None, "keep": None, "reask": None,
                 "reminder": None, "qa_question": None, "hold": False})
    state, parsed = res["state"], res["parsed"]
    merged = apply_parse(state, parsed, request_id=request_id, now=now)
    wants = parsed.intent in ("recommend", "update", "compare") or "recommend" in parsed.secondary_intents
    if wants and not res["hold"]:
        clarification, new_state = plan_clarification(merged.state, parsed.intent, parsed.secondary_intents)
    else:
        clarification, new_state = [], merged.state
    if parsed.turn_questions or res["reask"]:
        # rule-v6：本輪確認問題優先；只和既有的阻斷問題合併（最多 2 題），非阻斷問題這輪不問、也不記「問過」
        # followup-v2：再問待回答的那一題也一樣優先
        firsts = parsed.turn_questions or [res["reask"]]
        blocking = [q for q in clarification if q["blocking"]]
        clarification, new_state = (firsts + blocking)[:2], merged.state
    asked_turn = next((q for q in parsed.turn_questions if q.get("kind") in ("vitality", "technique_baseline")), None)
    new_state = copy.deepcopy(new_state)
    if asked_turn is not None and asked_turn in clarification:
        new_state["pending_turn"] = _new_pending(asked_turn, request_id=request_id, now=now)  # 新問題取代舊任務
    elif res["keep"] is not None:
        new_state["pending_turn"] = res["keep"]
    if (res["reminder"] is not None and new_state["pending_turn"] is res["keep"]
            and len(clarification) < 2):                 # 被新問題取代時就不提醒舊問題
        clarification = clarification + [res["reminder"]]
    return TurnResult(parsed, merged, clarification, new_state,
                      limitations(new_state, parsed.intent, parsed.secondary_intents,
                                  mentions_photo=parsed.mentions_photo, outside_text=parsed.outside_text),
                      followup=res["record"], qa_question=res["qa_question"], hold_answer=res["hold"])
# 核心邏輯：推薦／修改／比較才需要補問；純知識問答或範圍外問題不被偏好衝突卡住，但衝突仍保留在狀態裡。
# rule-v6：例外是本輪確認問題（例如「不一樣的擦法」先問平常怎麼擦），知識問答也要問；
# 非阻斷問題被丟掉時改用 merged.state，避免記成「問過」而之後不再問。
# followup-v2：
# 1. 有傳 text（API 會傳）時，先看待回答問題：明確肯定／否定／直接回答就接續；含糊、題型不符、看不出來就保留並再問；
#    插問就暫存（只暫存這一題）、照一般訊息處理並附提醒；明確取消就清掉；等太多則就過期。
# 2. 這一輪若問了新的本輪確認問題，取代舊的待回答（一次只有一個暫存任務）。
# 3. 偏好欄位只由 apply_parse 寫入，排除條件與之後的修改照常保留；暫定理解只在待回答的上下文。
# 4. 沒傳 text（舊的成對案例執行器）時不接續，只記待回答。
