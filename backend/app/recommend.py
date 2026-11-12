"""S7 有依據的個人化推薦：候選載入（含證據再驗證）、硬排除、透明排序、主推與替代、取捨紀錄。

整體流程（詳見 docs/mvp/s7-recommendation.md 的規則表）：

    資料庫 active 候選 ──► load_candidates()  逐筆證據檢查：核准、啟用版本、文件啟用、有效期間、角色可見
                              │                  （失效的證據不能證明任何屬性）
    S6 偏好狀態 ─────────────►decide()          純函式：
                              │                   1. 是否需要推薦（一般知識問題不強制推薦）
                              │                   2. 有待決衝突／阻斷模糊詞 → 先補問
                              │                   3. 候選沒有有效證據 → 排除（來源失效同步阻擋）
                              │                   4. 硬排除（明確排斥；屬性未知不能證明符合）
                              │                   5. 字典序排序（不展示分數）
                              │                   6. 主推 1 個＋最多 2 個有差異的替代，不湊數
                              │                   7. 組裝主張、引用、預設揭露、限制
                              ▼
                           run()               回傳前再驗證一次引用的證據；有變動就重算（最多 2 次）

原則（CLAUDE.md 規範 2、4、5、6、9）：
- 不憑模型記憶產生搭配規則：候選屬性只有「有效證據明確支持」才算已核對，其餘一律視為未知。
- 未知屬性不能證明符合硬限制（例如未記載妝效的候選，不能判成「不是霧面」）。
- 只有使用者明說的場合才算偏好；預設「日常」要揭露，且不能當成有依據的適配理由，除非候選有日常的證據。
- 沒有膚色證據就不能說「因為你的膚色而推薦」；不從膚色推定底調。
- 商品功能依 S4 決策為 No-go：不輸出任何商品；成分核對未啟用，不宣稱不過敏或安全。
- 合成候選（SYNTHETIC）只用來驗證演算法，結果會標示 synthetic，不能當正式驗收。

rec-v2（2026-10-25）新增「底調」：
- 候選屬性 undertones＝「來源支持的適用使用者底調」（cool／warm／neutral），每個值仍要有證據 supports（G03）。
- rec-v2/H06：使用者底調已確定，候選已核對的 undertones 非空且不含它 → 排除，記「來源適用條件不符」；
  這不表示這個顏色不能用。全部候選都因 H06 被排除時回 no_match（reason＝undertone_condition_not_met），
  不問「要不要放寬」（底調不是可放寬的偏好），如實說明沒有適用於該底調的來源方向（含 neutral）。
- 候選沒有 undertones（未知）：不排除，但不寫「依你的底調推薦」，限制中揭露。
- 使用者底調未知或 tentative，且通過的候選中有底調限定的：狀態 conditional——依來源條件分開列出
  （「如果你是冷底…」），不任意挑一支當主推，並只追問底調一題。
"""
from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import preferences as pref
from . import source_label
from .retrieval import ROLE_VISIBILITY

RULES_VERSION = "rec-v2"
CANDIDATE_SCHEMA_VERSION = "cand-v2"
SUPPORTED_CANDIDATE_SCHEMAS = ("cand-v1", "cand-v2")   # cand-v1 照原規則接受（沒有 undertones＝未知）
PRODUCT_ENABLED = False          # S4 商品 go/no-go：No-go（docs/mvp/s4-retrieval.md）
ALLERGY_MATCH_ENABLED = False    # 成分核對未啟用（推薦決策規格 §6）
MAX_ALTERNATIVES = 2
MAX_RECHECKS = 2                 # 回傳前發現證據失效時，最多重算幾次

# 候選屬性：color_family 是單值，其餘是清單。證據的 supports 用「屬性:值」標記它支持哪一項。
LIST_ATTRS = ("finishes", "effects", "occasions", "skin_tones", "undertones")
ATTR_LABELS = {"color_family": "色系", "finishes": "妝感／質地", "effects": "效果",
               "occasions": "場合", "skin_tones": "膚色搭配", "undertones": "來源適用底調"}
OCCASION_LABELS = {"daily": "日常", "office": "職場／上班", "party": "聚會／鮮明場合"}
# 證據支持類型（與 recommendation_option_evidence.support_type 相同；personal_preference 由對話產生，不是候選證據）
EVIDENCE_TYPES = ("research_observation", "styling_advice", "product_rule")
CLAIM_TYPE_LABELS = {"research_observation": "研究觀察", "styling_advice": "造型建議", "product_rule": "商品屬性",
                     "personal_preference": "你的偏好", "transparent_default": "預設條件"}

# 需要推薦流程的意圖。product_request 在商品未啟用時只提供被清楚標示的「方向」。
_RECOMMEND_INTENTS = ("recommend", "product_request")


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    """候選的一筆證據（一個片段＋一句主張）。valid=False 時 invalid_reason 說明原因。"""
    chunk_id: int
    chunk_key: str
    doc_key: str
    version_label: str
    title: str
    page_start: int | None
    page_end: int | None
    html_locator: str | None
    support_type: str
    claim: str
    supports: list[str]
    valid: bool
    invalid_reason: str | None = None
    venue: str | None = None            # 刊物／學校／網站（documents.bibliography.institution）
    year: int | str | None = None       # 出版年份（documents.bibliography.year）；沒有就 None

    # 轉成可回傳給前端、寫入日誌的引用格式（只含定位與書目，不含片段全文）。
    def citation(self) -> dict:
        return {"chunk_id": self.chunk_id, "chunk_key": self.chunk_key, "doc_key": self.doc_key,
                "version_label": self.version_label, "title": self.title, "page_start": self.page_start,
                "page_end": self.page_end, "html_locator": self.html_locator,
                "venue": self.venue, "year": self.year}


@dataclass
class Candidate:
    """一個推薦方向（色系／妝感組合）。attributes 是 B 線宣告的屬性；是否已核對要看 verified()。"""
    option_id: int
    option_key: str
    name: str
    description: str
    attributes: dict
    limitations: list[str]
    evidence: list[Evidence]
    synthetic: bool = False
    status: str = "active"

    # 只用「有效證據」支持的屬性值組成已核對屬性；宣告了但沒有有效證據的值視為未知。
    def verified(self) -> dict[str, Any]:
        tokens = {t for e in self.evidence if e.valid for t in e.supports}
        out: dict[str, Any] = {"color_family": None}
        declared_color = self.attributes.get("color_family")
        if declared_color and f"color_family:{declared_color}" in tokens:
            out["color_family"] = declared_color
        for attr in LIST_ATTRS:
            out[attr] = sorted(v for v in self.attributes.get(attr) or [] if f"{attr}:{v}" in tokens)
        return out
    # 核心邏輯：屬性是否成立只取決於「此刻仍有效、且角色看得到」的證據；
    # 來源停用後，同一個候選的屬性會自動變回未知，後面的硬排除與排序就會跟著改變。

    # 找出支持某個「屬性:值」的有效證據。
    def evidence_for(self, token: str) -> list[Evidence]:
        return [e for e in self.evidence if e.valid and token in e.supports]


@dataclass
class Decision:
    """一次推薦判斷的完整結果；to_dict() 直接寫進 requests.detail.recommendation。"""
    status: str                     # not_applicable／needs_clarification／insufficient_evidence／no_match／recommended／conditional
    rules_version: str = RULES_VERSION
    reason: str | None = None
    recommendations: list[dict] = field(default_factory=list)   # rank 1 = 主推，2–3 = 替代
    hard_conditions: list[dict] = field(default_factory=list)
    soft_preferences: list[dict] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    question: str | None = None
    trace: list[dict] = field(default_factory=list)             # 每個候選的取捨紀錄
    candidate_count: int = 0
    synthetic: bool = False
    product_enabled: bool = PRODUCT_ENABLED
    rechecks: int = 0
    effect_choice: bool = False     # rec-v2：通過的候選在已核對效果上有差異（追問「希望效果」才可能改變結果）
    brief: dict | None = None       # brief-v1：給使用者看的精簡呈現（一句總結＋每個方向一句原因）；細節在 claims／limitations

    # 轉成可寫進 JSONB 的字典。
    def to_dict(self) -> dict:
        return asdict(self)

    # 這次結果引用到的所有片段 id（回傳前再驗證用）。
    def cited_chunk_ids(self) -> list[int]:
        return sorted({c["chunk_id"] for r in self.recommendations for cl in r["claims"]
                       for c in cl.get("citations", [])})


class RecommendationError(RuntimeError):
    """推薦流程無法安全完成（例如證據在回傳前一再變動）。訊息可以顯示給使用者。"""


# ---------------------------------------------------------------------------
# 候選載入（資料庫）：每次請求都重新檢查證據資格，不快取
# ---------------------------------------------------------------------------
_LOAD_SQL = """
SELECT o.id AS option_id, o.option_key, o.name, o.description, o.attributes, o.status AS option_status,
       e.chunk_id, e.support_type, e.claim,
       c.chunk_key, c.page_start, c.page_end, c.html_locator, c.review_status, c.is_active AS chunk_active,
       dv.version_label, dv.status AS version_status,
       d.doc_key, d.title, d.bibliography, d.visibility, d.is_active AS doc_active, d.rag_eligible,
       (d.valid_from IS NOT NULL AND d.valid_from > now()) AS not_yet_valid,
       (d.valid_until IS NOT NULL AND d.valid_until <= now()) AS expired,
       (u.chunk_id IS NOT NULL) AS usable
  FROM recommendation_options o
  LEFT JOIN recommendation_option_evidence e ON e.option_id = o.id
  LEFT JOIN chunks c ON c.id = e.chunk_id
  LEFT JOIN document_versions dv ON dv.id = c.document_version_id
  LEFT JOIN documents d ON d.id = dv.document_id
  LEFT JOIN usable_chunks u ON u.chunk_id = e.chunk_id
 WHERE o.status = ANY(%s)
 ORDER BY o.option_key, e.id
"""


# 角色可看的 visibility；未知角色直接拒絕（角色只來自後端認證，不讀請求欄位）。
def _visibility(role: str) -> tuple[str, ...]:
    if role not in ROLE_VISIBILITY:
        raise RecommendationError(f"未知角色：{role}")
    return ROLE_VISIBILITY[role]


# 判斷一筆證據為什麼不能用；可用時回傳 None。usable_chunks 檢視是唯一的資格判斷，其餘欄位只用來寫原因。
def _invalid_reason(row: dict, visibility: tuple[str, ...]) -> str | None:
    if not row["usable"]:
        if row["review_status"] != "approved":
            return "chunk_not_approved"
        if not row["chunk_active"]:
            return "chunk_inactive"
        if row["version_status"] != "published":
            return "version_not_published"
        if not row["doc_active"]:
            return "document_inactive"
        if not row["rag_eligible"]:
            return "document_not_rag_eligible"
        if row["expired"] or row["not_yet_valid"]:
            return "outside_valid_period"
        return "not_usable"
    if row["visibility"] not in visibility:
        return "role_not_permitted"
    return None
# 核心邏輯：資格以資料庫的 usable_chunks 為準（核准、啟用、已發布版本、文件啟用、可進 RAG、有效期間），
# 再加上角色可見範圍；原因依序判斷，只是為了讓取捨紀錄看得懂，不會放寬資格。


# 讀出指定狀態（預設只有 active）的候選與其證據，逐筆標記證據是否有效。
def load_candidates(conn: psycopg.Connection, role: str,
                    statuses: tuple[str, ...] = ("active",)) -> list[Candidate]:
    visibility = _visibility(role)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_LOAD_SQL, (list(statuses),)).fetchall()
    by_key: dict[str, Candidate] = {}
    for r in rows:
        attrs = r["attributes"] or {}
        cand = by_key.get(r["option_key"])
        if cand is None:
            cand = Candidate(option_id=r["option_id"], option_key=r["option_key"], name=r["name"],
                             description=r["description"], attributes=attrs,
                             limitations=list(attrs.get("limitations") or []), evidence=[],
                             synthetic=bool(attrs.get("synthetic")), status=r["option_status"])
            by_key[r["option_key"]] = cand
        if r["chunk_id"] is None:
            continue  # 沒有任何證據的 active 候選（不應發生；啟用時已檢查）→ 證據清單為空，之後會被排除
        supports = _supports_for(attrs, r["chunk_id"], r["claim"])
        reason = _invalid_reason(r, visibility)
        cand.evidence.append(Evidence(
            chunk_id=r["chunk_id"], chunk_key=r["chunk_key"], doc_key=r["doc_key"],
            version_label=r["version_label"], title=r["title"], page_start=r["page_start"],
            page_end=r["page_end"], html_locator=r["html_locator"], support_type=r["support_type"],
            claim=r["claim"], supports=supports, valid=reason is None, invalid_reason=reason,
            venue=source_label.venue_of(r["bibliography"]), year=source_label.year_of(r["bibliography"])))
    return list(by_key.values())
# 核心邏輯：
# 1. 只讀 status = active 的候選；draft、retired 不會進推薦。
# 2. 每一筆證據都在這次請求重新判斷資格，所以片段被退回、版本被取代、文件下架、過期或改成管理者限定，
#    下一次請求立即失效（「來源失效同步阻擋」），不需要另外清除快取。


# 從候選屬性的 evidence_map 找出某筆證據支持哪些「屬性:值」；對不到時回傳空清單（不支持任何屬性）。
def _supports_for(attrs: dict, chunk_id: int, claim: str) -> list[str]:
    for m in attrs.get("evidence_map") or []:
        if m.get("chunk_id") == chunk_id and m.get("claim") == claim:
            return list(m.get("supports") or [])
    return []


# ---------------------------------------------------------------------------
# 判斷（純函式）
# ---------------------------------------------------------------------------
# 這次訊息是否需要推薦：推薦／商品需求，或修改條件且這個對話之前已經在推薦流程中。
def wants_recommendation(intent: str, secondary: list[str], had_prior: bool) -> bool:
    intents = {intent, *secondary}
    if intents & set(_RECOMMEND_INTENTS):
        return True
    return intent == "update" and had_prior
# 核心邏輯：一般知識問題、比較、範圍外問題不強制推薦；只說「改成聚會」時，若之前請求過推薦就重新推薦
# （偏好變更後結果要跟著變），從沒請求過推薦就只更新條件。


# 從偏好狀態整理出硬條件（明確排斥）與軟偏好；回傳 (hard, soft, assumptions)。
def _conditions(state: dict) -> tuple[list[dict], list[dict], list[dict]]:
    hard: list[dict] = []
    for fld, attr in (("disliked_colors", "color_family"), ("disliked_finishes", "finishes"),
                      ("avoided_effects", "effects")):
        for item in state.get(fld) or []:
            hard.append({"field": fld, "attr": attr, "value": item["value"], "label": item["label"],
                         "raw": item.get("raw")})
    soft: list[dict] = []
    for fld, attr in (("effects", "effects"), ("liked_finishes", "finishes"), ("liked_colors", "color_family")):
        items = state.get(fld) or []
        if items:
            soft.append({"field": fld, "attr": attr, "values": [i["value"] for i in items],
                         "labels": [i["label"] for i in items], "raw": [i.get("raw") for i in items]})
    occ = state["occasion"]
    assumptions: list[dict] = []   # rule-v5（K1）起不再產生預設場合假設；保留欄位讓舊紀錄格式相容
    occ_values = occ.get("values") or ([occ["value"]] if occ.get("value") else [])
    if occ["status"] == "user" and occ_values:
        soft.append({"field": "occasion", "attr": "occasions", "values": occ_values,
                     "labels": [OCCASION_LABELS.get(v, v) for v in occ_values], "raw": [occ.get("raw")]})
    return hard, soft, assumptions
# 核心邏輯（規則 H01–H03、S01）：
# 1. 硬條件只來自使用者明確「不要」的色系、妝感、效果（S6 的 disliked_*／avoided_effects）。
# 2. 軟偏好依「希望效果 → 妝感 → 色系 → 場合」的固定順序排列，排序時照這個順序比較。
# 3. 場合只有使用者明說才是偏好；未指定就不當條件、也不假設日常（K1）。
#    多情境（K3，「聚會和上班都要」）：任一情境有已核對依據就算符合。


# 對單一候選做硬排除；回傳排除原因（空清單代表通過）。
def _hard_reasons(verified: dict, hard: list[dict]) -> list[dict]:
    reasons: list[dict] = []
    for h in hard:
        if h["attr"] == "color_family":
            if verified["color_family"] == h["value"]:
                reasons.append({"rule": "H01", "code": "disliked_color", "value": h["value"], "label": h["label"]})
            continue
        values = verified[h["attr"]]
        if not values:
            code = "finish_unknown" if h["attr"] == "finishes" else "effect_unknown"
            reasons.append({"rule": "H04", "code": code, "value": h["value"], "label": h["label"]})
        elif h["value"] in values:
            rule, code = ("H02", "disliked_finish") if h["attr"] == "finishes" else ("H03", "avoided_effect")
            reasons.append({"rule": rule, "code": code, "value": h["value"], "label": h["label"]})
    return _dedupe(reasons)
# 核心邏輯：色系一定已核對（沒核對的候選更早就被排除）；妝感或效果沒有任何已核對值時，
# 無法證明它不是使用者排斥的那一種 → 排除（規則 H04，推薦決策規格 §5「未知屬性不能證明符合硬限制」）。


# 同一個原因只留一筆（例如同時排斥霧面與緞光、但妝感未知，只記一次 finish_unknown）。
def _dedupe(reasons: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in reasons:
        k = (r["code"], r["value"] if r["code"] not in ("finish_unknown", "effect_unknown") else None)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# 計算候選對每一項軟偏好是否「已證實相符」，並產生排序鍵。
def _soft_match(verified: dict, soft: list[dict], assumptions: list[dict]) -> dict:
    matched: dict[str, list[str]] = {}
    unmet: list[dict] = []
    for s in soft:
        if s["attr"] == "color_family":
            hits = [v for v in s["values"] if v == verified["color_family"]]
        else:
            hits = [v for v in s["values"] if v in verified[s["attr"]]]
        if hits:
            matched[s["field"]] = hits
        else:
            unmet.append({"field": s["field"], "labels": s["labels"]})
    order = ("effects", "liked_finishes", "liked_colors", "occasion")
    vector = [1 if f in matched else 0 for f in order]
    default_supported = None
    for a in assumptions:
        if a["field"] == "occasion":
            default_supported = a["value"] in verified["occasions"]
    key = (-len(matched), *(-v for v in vector), 0 if default_supported else 1)
    return {"matched": matched, "unmet": unmet, "vector": vector, "default_supported": default_supported,
            "sort_key": list(key)}
# 核心邏輯（規則 S01–S04，v0.1 工程預設，不是科學適合度）：
# 1. 已證實相符的軟偏好「欄位數」多者優先；每欄只算一次，同義詞不重複加分，未知不加分。
# 2. 同分時依「效果 → 妝感 → 色系 → 場合」逐項比較。
# 3. 再同分時，場合是預設的話，有日常證據的候選優先（較少依賴未經證實的預設）。
# 4. 最後由呼叫端以 option_key 穩定排序；完全同分會在結果中註明「同樣符合」。


# 兩個候選的已核對色系／妝感是否完全相同（相同就不能當彼此的替代）。
def _same_look(a: dict, b: dict) -> bool:
    return (a["color_family"] == b["color_family"] and set(a["finishes"]) == set(b["finishes"])
            and set(a.get("undertones") or []) == set(b.get("undertones") or []))   # rec-v2：適用底調不同就不是同一方向


# rec-v2：使用者「已確定」的底調（status=user 且有值）；未知或 tentative 都回 None，另回 tentative 傾向供揭露。
def _user_undertone(state: dict) -> tuple[str | None, str | None]:
    ut = state.get("undertone") or {}
    if ut.get("tentative"):
        return None, ut["tentative"]
    return (ut.get("value") if ut.get("status") == "user" and ut.get("value") else None), None


# rec-v2：底調的中文名稱（取自偏好詞彙表）。
def _ut_label(value: str) -> str:
    return pref.UNDERTONE_LABELS.get(value, value)


# 主判斷：依偏好狀態與候選產生 Decision（不碰資料庫，方便測試與產生取捨紀錄）。
def decide(state: dict, candidates: list[Candidate], *, intent: str, secondary: list[str],
           had_prior: bool = False) -> Decision:
    hard, soft, assumptions = _conditions(state)
    d = Decision(status="recommended", hard_conditions=hard, soft_preferences=soft, assumptions=assumptions,
                 candidate_count=len(candidates), synthetic=any(c.synthetic for c in candidates))
    if not wants_recommendation(intent, secondary, had_prior):
        d.status, d.reason = "not_applicable", "intent_does_not_request_recommendation"
        return d
    if state.get("conflicts") or any(a.get("blocking") for a in state.get("ambiguities") or []):
        d.status, d.reason = "needs_clarification", "pending_conflict_or_ambiguity"
        return d

    user_ut, tentative_ut = _user_undertone(state)
    passed: list[tuple[Candidate, dict, dict]] = []
    for c in candidates:
        v = c.verified()
        invalid = [{"chunk_key": e.chunk_key, "doc_key": e.doc_key, "reason": e.invalid_reason,
                    "supports": e.supports} for e in c.evidence if not e.valid]
        entry = {"option_key": c.option_key, "name": c.name, "verified": v, "invalid_evidence": invalid,
                 "synthetic": c.synthetic}
        if not any(e.valid for e in c.evidence):
            d.trace.append({**entry, "decision": "excluded",
                            "reasons": [{"rule": "G01", "code": "no_valid_evidence"}]})
            continue
        if v["color_family"] is None:
            d.trace.append({**entry, "decision": "excluded",
                            "reasons": [{"rule": "G02", "code": "color_family_unverified"}]})
            continue
        reasons = _hard_reasons(v, hard)
        if user_ut and v["undertones"] and user_ut not in v["undertones"]:
            # rec-v2/H06：來源的適用條件（底調）與使用者已確定的底調不符；不表示這個顏色不能用
            reasons.append({"rule": "rec-v2/H06", "code": "undertone_condition_not_met", "value": user_ut,
                            "label": _ut_label(user_ut), "source_undertones": list(v["undertones"])})
        if reasons:
            d.trace.append({**entry, "decision": "excluded", "reasons": reasons})
            continue
        m = _soft_match(v, soft, assumptions)
        m["sort_key"].append(0 if user_ut and user_ut in v["undertones"] else 1)   # rec-v2：底調已證實相符者優先
        passed.append((c, v, m))
        d.trace.append({**entry, "decision": "eligible", "reasons": [], "matched": m["matched"],
                        "unmet": m["unmet"], "sort_key": m["sort_key"], "default_supported": m["default_supported"]})

    eligible_by_evidence = [t for t in d.trace if not any(r["rule"] in ("G01", "G02") for r in t["reasons"])]
    if not eligible_by_evidence:
        d.status = "insufficient_evidence"
        d.reason = "no_active_candidates" if not candidates else "no_candidate_with_valid_evidence"
        d.limitations = _limitations(state, d)
        return d
    if not passed:
        h06_only = all(r["rule"] == "rec-v2/H06" for t in eligible_by_evidence for r in t["reasons"])
        if h06_only:   # 只因來源的底調條件不符：如實說明，不問放寬（底調不是可放寬的偏好）
            d.status, d.reason = "no_match", "undertone_condition_not_met"
        else:
            d.status, d.reason = "no_match", "all_candidates_excluded_by_hard_conditions"
            d.question = _relax_question(hard)
        d.limitations = _limitations(state, d)
        return d
    # 效果追問會不會改變結果：通過的候選在「已核對效果」上有差異才算有實際分支（沒有就延後不問）
    d.effect_choice = len({tuple(sorted(v["effects"])) for _, v, _ in passed}) > 1
    passed.sort(key=lambda t: (t[2]["sort_key"], t[0].option_key))
    selected = _select(passed, soft, d.trace)
    if user_ut is None and any(v["undertones"] for _, v, _, _ in selected):
        # rec-v2 conditional：照常選出的方向裡有底調限定的，而使用者底調未知或只是 tentative——
        # 依來源條件分開列（不限底調的照列「不限底調」），不當成依你的底調推薦，追問底調一題。
        # 選出的方向都不限底調時，底調不影響結果，照常 recommended、不追問。
        d.status = "conditional"
        d.reason = "user_undertone_unknown" if tentative_ut is None else "user_undertone_tentative"
        for t in d.trace:
            if t["decision"] in ("selected_primary", "selected_alternative"):
                t["decision"] = "selected_conditional"
        d.recommendations = [_build_recommendation(rank, c, v, m, state, hard, assumptions, tied, role="conditional")
                             for rank, (c, v, m, tied) in enumerate(selected, start=1)]
        d.question = ("你的底調是冷底、暖底還是中性底？上面的方向要看這個條件。"
                      + (f"（你說可能是{_ut_label(tentative_ut)}，這不當成確定底調。）" if tentative_ut else ""))
        d.limitations = _limitations(state, d)
        d.brief = _brief(d, None)
        return d
    d.recommendations = [_build_recommendation(rank, c, v, m, state, hard, assumptions, tied)
                         for rank, (c, v, m, tied) in enumerate(selected, start=1)]
    d.limitations = _limitations(state, d)
    d.brief = _brief(d, user_ut)
    return d
# 核心邏輯：
# 1. 不需要推薦（知識問題等）→ not_applicable；有待決衝突或會左右排除的模糊詞 → needs_clarification。
# 2. 每個候選依序檢查：有沒有任何有效證據（G01）→ 色系是否已核對（G02）→ 硬排除（H01–H04），
#    每一步的結果都寫進 trace，形成「推薦取捨紀錄」。
# 3. 沒有任何有證據的候選 → insufficient_evidence（不讓模型補知識）；有候選但全被硬條件排除 → no_match，
#    只詢問是否放寬非安全條件，不自行放寬。
# 4. 通過者以字典序排序後挑主推與替代，組裝主張與限制。


# brief-v1（使用者 2026-10-25 真人判定：卡片只要「顏色＋為什麼」，限制與來源收進參考依據）。
# 只取已核對的主張原文（去掉「來源說」開頭），不另外生成或補說明。
BRIEF_VERSION = "brief-v1"


def _why(text: str) -> str:
    return re.sub(r"^來源說", "", text).strip()


def _supports(c: dict, attr: str) -> bool:
    return attr in [c.get("supports"), *(c.get("also_supports") or [])]


def _brief(d: Decision, user_ut: str | None) -> dict:
    """每個方向一句原因：優先用支持使用者底調的那條主張，否則用第一條有引用的主張。
    中性底：來源有「中性底幾乎都能駕馭」且方向的已核對底調含中性時，合成一句總結（headline），
    這些方向不再逐一列原因（headline 已經說明為什麼）。"""
    headline, fits = None, []
    if user_ut == "neutral":
        neutral = next((c for r in d.recommendations for c in r["claims"]
                        if c.get("citations") and _supports(c, "undertones:neutral")), None)
        fits = [r["name"] for r in d.recommendations if "neutral" in (r["verified_attributes"].get("undertones") or [])]
        if neutral and fits:
            headline = f"{_why(neutral['text']).rstrip('。')}；{'、'.join(fits)}都適合。"
        else:
            fits = []
    items = []
    for r in d.recommendations:
        if r["name"] in fits:
            continue
        cited = [c for c in r["claims"] if c.get("citations")]
        pick = next((c for c in cited if user_ut and _supports(c, f"undertones:{user_ut}")), None) or             next(iter(cited), None)
        items.append({"rank": r["rank"], "role": r["role"], "name": r["name"],
                      "condition": (r.get("condition") or {}).get("text"), "why": _why(pick["text"]) if pick else None})
    return {"version": BRIEF_VERSION, "headline": headline, "covered": fits, "items": items}


# 挑主推與替代：替代必須與已選的每一個在已核對色系或妝感上不同；使用者有軟偏好時，替代至少要符合一項。
def _select(passed: list[tuple[Candidate, dict, dict]], soft: list[dict],
            trace: list[dict]) -> list[tuple[Candidate, dict, dict, list[str]]]:
    by_key = {t["option_key"]: t for t in trace}
    primary = passed[0]
    chosen = [primary]
    by_key[primary[0].option_key]["decision"] = "selected_primary"
    for c, v, m in passed[1:]:
        t = by_key[c.option_key]
        if len(chosen) >= 1 + MAX_ALTERNATIVES:
            t["decision"], t["not_selected_reason"] = "not_selected", "alternatives_full"
            continue
        same = next((x for x in chosen if _same_look(x[1], v)), None)
        if same is not None:
            t["decision"], t["not_selected_reason"] = "not_selected", f"same_look_as:{same[0].option_key}"
            continue
        if soft and not m["matched"]:
            t["decision"], t["not_selected_reason"] = "not_selected", "matches_no_stated_preference"
            continue
        chosen.append((c, v, m))
        t["decision"] = "selected_alternative"
    out = []
    for c, v, m in chosen:
        tied = [o.option_key for o, _, om in passed if o.option_key != c.option_key and om["sort_key"] == m["sort_key"]]
        out.append((c, v, m, tied))
    return out
# 核心邏輯（規則 S05、S06）：
# 1. 主推 = 排序第一；替代最多 2 個，必須跟已選者在色系或妝感上有差異，否則跳過（不湊數）。
# 2. 使用者說過任何軟偏好時，一項都沒符合的候選不當替代（避免為了湊滿三個而推不相干的方向）。
# 3. 與其他候選排序鍵完全相同時記下 tied_with，回覆時承認「同樣符合」，不捏造優劣。


# 組裝一個推薦方向：理由（主張＋引用）、未符合的偏好、預設揭露、限制。
def _build_recommendation(rank: int, c: Candidate, v: dict, m: dict, state: dict, hard: list[dict],
                          assumptions: list[dict], tied: list[str], *, role: str | None = None) -> dict:
    claims: list[dict] = []
    # 1. 色系本身的依據（每個推薦都必須有）
    claims += _evidence_claims(c, f"color_family:{v['color_family']}", "色系")
    # 2. 已證實符合的軟偏好：引用支持該屬性的證據
    for fld, values in m["matched"].items():
        attr = {"effects": "effects", "liked_finishes": "finishes", "liked_colors": "color_family",
                "occasion": "occasions"}[fld]
        if attr == "color_family":
            claims.append(_preference_claim(state, fld, values))
            continue
        for val in values:
            claims += _evidence_claims(c, f"{attr}:{val}", pref.FIELD_LABELS.get(fld, fld))
        claims.append(_preference_claim(state, fld, values))
    # 3. 通過的硬條件（使用者偏好主張，由對話支持）
    for h in hard:
        claims.append({"claim_type": "personal_preference", "text": f"已排除你不要的「{h['label']}」",
                       "raw": h.get("raw"), "citations": []})
    # 4. 預設場合：揭露，且只有候選真的有日常證據才引用
    for a in assumptions:
        supported = a["value"] in v["occasions"]
        claims.append({"claim_type": "transparent_default",
                       "text": a["text"] + ("此方向有該情境的來源依據。" if supported
                                            else "此方向沒有該情境的直接來源依據，只是可用的起點。"),
                       "citations": []})
        if supported:
            claims += _evidence_claims(c, f"occasions:{a['value']}", "場合")
    limitations = list(c.limitations)
    skin = state["skin_tone"]["value"]
    if skin:
        if skin in v["skin_tones"]:
            claims += _evidence_claims(c, f"skin_tones:{skin}", "膚色搭配")
        else:
            limitations.append(f"目前沒有「{skin}」膚色的搭配證據；這個方向不是依你的膚色推薦。")
    if v["finishes"] == []:
        limitations.append("此方向的妝感／質地未記載。")
    # rec-v2：底調——來源條件的依據、與使用者底調的關係、條件式呈現
    user_ut, _ = _user_undertone(state)
    condition = None
    if v["undertones"]:
        for ut in v["undertones"]:
            claims += _evidence_claims(c, f"undertones:{ut}", "來源適用底調")
        labels = [_ut_label(u) for u in v["undertones"]]
        if role == "conditional":
            condition = {"undertones": list(v["undertones"]), "labels": labels, "text": "如果你是" + "或".join(labels)}
        elif user_ut in v["undertones"]:
            ut_state = state.get("undertone") or {}
            claims.append({"claim_type": "personal_preference", "text": f"符合你說的底調：{_ut_label(user_ut)}",
                           "raw": [ut_state.get("raw")], "citations": []})
    elif user_ut:
        limitations.append(f"此方向沒有底調的來源條件；不是依你的底調（{_ut_label(user_ut)}）推薦。")
    elif role == "conditional":
        condition = {"undertones": [], "labels": [], "text": "不限底調（來源沒有底調條件）"}
    return {
        "rank": rank, "role": role or ("primary" if rank == 1 else "alternative"), "condition": condition,
        "option_id": c.option_id, "option_key": c.option_key, "name": c.name, "description": c.description,
        "verified_attributes": v, "matched": m["matched"], "unmet": m["unmet"], "tied_with": tied,
        "claims": _merge_evidence_claims(claims), "limitations": limitations, "synthetic": c.synthetic,
        "reason": _short_reason(m, tied, has_soft=bool(m["matched"] or m["unmet"])),
    }


# rec-v2：同一筆證據同時支持多個屬性（例如色系＋效果、色系＋底調）時，卡片只列一次主張，
# 其餘屬性記在 also_supports；主張文字、引用與順序都不改。
def _merge_evidence_claims(claims: list[dict]) -> list[dict]:
    out: list[dict] = []
    for cl in claims:
        same = next((x for x in out if "supports" in cl and "supports" in x and x["text"] == cl["text"]
                     and x["citations"] == cl["citations"]), None)
        if same is None:
            out.append(cl)
        elif cl["supports"] != same["supports"] and cl["supports"] not in same.get("also_supports", []):
            same.setdefault("also_supports", []).append(cl["supports"])
    return out
# 核心邏輯：
# 1. 每條理由都標 claim_type：研究觀察、造型建議、商品屬性（來自證據）、你的偏好、預設條件（不需引用）。
# 2. 研究觀察不改寫成造型保證；主張文字直接用 B 線核對過的 claim，不由程式或模型另外生成。
# 3. 有自述膚色但沒有該膚色的證據 → 明確寫進限制，不讓「推薦」看起來像膚色適配。


# 取出支持某個屬性值的證據，轉成主張＋引用。
def _evidence_claims(c: Candidate, token: str, label: str) -> list[dict]:
    return [{"claim_type": e.support_type, "text": e.claim, "supports": token, "about": label,
             "citations": [e.citation()]} for e in c.evidence_for(token)]


# 使用者偏好主張：「符合你說的『上班用』」——依據是對話，不需要文獻。
def _preference_claim(state: dict, fld: str, values: list[str]) -> dict:
    if fld == "occasion":
        labels = [OCCASION_LABELS.get(v, v) for v in values]
        raw = [state["occasion"].get("raw")]
    else:
        items = [i for i in state.get(fld) or [] if i["value"] in values]
        labels, raw = [i["label"] for i in items], [i.get("raw") for i in items]
    return {"claim_type": "personal_preference",
            "text": f"符合你提出的{pref.FIELD_LABELS.get(fld, fld)}：{'、'.join(labels)}", "raw": raw, "citations": []}


# 寫進 request_recommendations.reason 的一句話（純事實：符合哪些、是否同分）。
def _short_reason(m: dict, tied: list[str], *, has_soft: bool) -> str:
    parts = [pref.FIELD_LABELS.get(f, f) for f in m["matched"]]
    if parts:
        text = "符合：" + "、".join(parts)
    elif has_soft:
        text = "通過所有排除條件；未符合任何已說明的偏好"
    else:
        text = "通過所有排除條件；尚未說明可比較的偏好"
    if tied:
        text += "（與其他方向同樣符合，順序依固定編號）"
    return text


# 沒有候選符合全部硬條件時的補問：只問是否放寬非安全偏好，不自行放寬。
def _relax_question(hard: list[dict]) -> str:
    labels = "、".join(f"不要{h['label']}" for h in hard) or "目前的條件"
    return f"沒有同時符合「{labels}」的已核對方向。願意放寬其中哪一項嗎？（不放寬也可以，我們就不推薦）"


# 這次推薦結果必須揭露的限制。
def _limitations(state: dict, d: Decision) -> list[str]:
    notes: list[str] = []
    if d.status == "insufficient_evidence":
        notes.append("目前沒有經人工核對且有有效來源的推薦方向，所以不提供推薦。"
                     if d.reason == "no_active_candidates"
                     else "推薦方向的來源已失效（停用、過期、未核准或無權限查看），所以不提供推薦。")
    if not PRODUCT_ENABLED:
        notes.append("商品功能未啟用：只能提供色系／妝感方向，不提供品牌、色號或價格。")
    if state["allergy"]["status"] in ("has_known", "unsure"):
        notes.append("已記錄需避開項目；成分核對功能未啟用，不提供具體商品，也不保證任何產品安全。")
    if d.status == "conditional":
        notes.append("你的底調還沒確定，以下方向依來源的底調條件分開列出，不是依你的底調推薦；確定後再依你的底調篩選。")
    if d.status == "no_match" and d.reason == "undertone_condition_not_met":
        ut, _ = _user_undertone(state)
        notes.append(f"目前已核對的方向都有底調條件，但沒有適用於「{_ut_label(ut)}」的來源方向；"
                     "這只表示來源沒有涵蓋，不表示這些顏色不能用。")
    if any(r["synthetic"] for r in d.recommendations) or (d.synthetic and d.status != "recommended"):
        notes.append("【SYNTHETIC】此結果使用合成測試候選，只驗證規則，不是正式推薦。")
    return notes


# ---------------------------------------------------------------------------
# 對外入口：載入 → 判斷 → 回傳前再驗證
# ---------------------------------------------------------------------------
# 回傳引用片段中此刻已不可用（或角色看不到）的 id。
def stale_citations(conn: psycopg.Connection, chunk_ids: list[int], role: str) -> list[int]:
    if not chunk_ids:
        return []
    visibility = list(_visibility(role))
    with conn.cursor(row_factory=dict_row) as cur:
        ok = {r["chunk_id"] for r in cur.execute(
            "SELECT chunk_id FROM usable_chunks WHERE chunk_id = ANY(%s) AND visibility = ANY(%s)",
            (chunk_ids, visibility)).fetchall()}
    return [i for i in chunk_ids if i not in ok]


# 完整推薦：載入候選、判斷，回傳前確認引用仍有效；有變動就重算，最多 MAX_RECHECKS 次。
def run(conn: psycopg.Connection, state: dict, *, role: str, intent: str, secondary: list[str],
        had_prior: bool = False) -> Decision:
    for attempt in range(MAX_RECHECKS + 1):
        decision = decide(state, load_candidates(conn, role), intent=intent, secondary=secondary,
                          had_prior=had_prior)
        decision.rechecks = attempt
        if not stale_citations(conn, decision.cited_chunk_ids(), role):
            return decision
    raise RecommendationError("推薦依據在處理期間持續變動，這次不回傳推薦，請稍後再試。")
# 核心邏輯（推薦決策規格 §3 第 10 步）：判斷與回傳之間若有片段被停用或改權限，引用就會對不上 usable_chunks；
# 這時重新載入並重算，而不是把已失效的依據回給使用者。


# 把結果寫進 request_recommendations（rank 1–3）。引用細節在 requests.detail.recommendation。
def record(conn: psycopg.Connection, request_id, decision: Decision) -> None:
    for r in decision.recommendations:
        conn.execute("INSERT INTO request_recommendations (request_id, rank, option_id, reason) VALUES (%s, %s, %s, %s)",
                     (request_id, r["rank"], r["option_id"], r["reason"]))
# 注意：request_citations 的觸發器要求引用必須是同一請求的 retrieval_hits；候選證據不是檢索命中，
# 目前不寫入該表（避免偽造檢索紀錄），引用完整保存在 detail。需要的話由協調者分配遷移號擴充 retriever 種類。


# 由結果產生保守模板回覆（只用已核對屬性、主張與限制；不經模型）。
def render_text(d: Decision) -> str:
    if d.status == "insufficient_evidence":
        return "目前沒有可以引用來源的推薦方向，這次不提供推薦。" + "".join(d.limitations[:1])
    if d.status == "no_match":
        if d.reason == "undertone_condition_not_met":
            return next((x for x in d.limitations if "底調" in x), "沒有適用於你底調的來源方向。")
        return d.question or "沒有符合全部條件的方向。"
    if d.status not in ("recommended", "conditional"):
        return ""
    lines: list[str] = []
    if d.brief:
        return "\n".join(_render_brief(d))
    for a in d.assumptions:
        lines.append(a["text"])
    if d.hard_conditions:
        lines.append("已排除你不要的：" + "、".join(h["label"] for h in d.hard_conditions))
    if d.status == "conditional":
        lines.append("你的底調還沒確定，以下依來源的底調條件分開列出：")
    for r in d.recommendations:
        if r["role"] == "conditional":
            head = (r.get("condition") or {}).get("text") or "條件式方向"
        else:
            head = "主要方向" if r["role"] == "primary" else f"替代方向 {r['rank'] - 1}"
        lines.append(f"{head}：{r['name']}（{r['reason']}）")
        for cl in r["claims"]:
            if cl["citations"]:
                # citation-label-v1：寫〈標題〉，刊物，年份，p.頁碼，不寫文件編號（使用者 2026-11-01）
                src = source_label.MULTI_SEP.join(dict.fromkeys(source_label.label_of(c) for c in cl["citations"]))
                lines.append(f"・{CLAIM_TYPE_LABELS.get(cl['claim_type'], cl['claim_type'])}：{cl['text']}〔{src}〕")
        for u in r["unmet"]:
            lines.append(f"・未符合：{pref.FIELD_LABELS.get(u['field'], u['field'])}（{'、'.join(u['labels'])}）")
        for lim in r["limitations"]:
            lines.append(f"・限制：{lim}")
    if d.status == "conditional" and d.question:
        lines.append(f"想再確認：{d.question}")
    return "\n".join(lines)
def _render_brief(d: Decision) -> list[str]:
    b = d.brief or {}
    lines = [a["text"] for a in d.assumptions]
    if d.hard_conditions:
        lines.append("已排除你不要的：" + "、".join(h["label"] for h in d.hard_conditions))
    if d.status == "conditional":
        lines.append("你的底調還沒確定，以下依來源的底調條件分開列出：")
    if b.get("headline"):
        lines.append(b["headline"])
    for it in b.get("items") or []:
        if d.status == "conditional":
            head = it.get("condition") or "條件式方向"
        else:
            head = "推薦" if it["role"] == "primary" and not b.get("headline") else "也可以考慮"
        lines.append(f"{head}：{it['name']}" + (f"——{it['why']}" if it.get("why") else ""))
    if d.status == "conditional" and d.question:
        lines.append(f"想再確認：{d.question}")
    return lines


# 核心邏輯：模板只是把結構化結果照順序寫出來——預設揭露 → 每個方向的理由與來源 → 未符合偏好 → 限制；
# 生成模型接上後（S5／DEC-11），模型也只能改寫這些已驗證的主張，不能新增。


# 深複製結果（給 API 回傳用，避免之後修改影響日誌）。
def public_view(d: Decision) -> dict:
    out = copy.deepcopy(d.to_dict())
    for r in out["recommendations"]:
        r.pop("option_id", None)
    return out
