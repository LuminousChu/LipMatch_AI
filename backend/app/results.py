"""S8 完整使用流程的後端支援：請求結果檢視、引用片段查看、回饋驗證。

用途
- 推薦頁要能「回頭看」某一次請求的結果（主要／替代推薦、限制、補問），並查看每個引用的來源原文與定位。
- 回饋必須掛在 request_id 上，且只有該請求的擁有者、對「已完成」的結果才能回饋；
  指定推薦方向時（rank），該方向必須真的出現在這次請求的推薦結果裡。

權限原則（規範 1、2）
- 角色只取自後端認證（CurrentUser.role），不讀請求欄位。
- 日誌（requests.detail.recommendation）保留完整取捨紀錄 trace，但 trace 會列出被排除候選與
  「失效／無權限」證據的 chunk_key、doc_key；這些對一般使用者是受限資訊，所以對外檢視一律移除 trace。
- 引用每次查看都以 usable_chunks（核准、已發布、啟用、可進 RAG、有效期間）＋角色可見範圍重新判斷；
  不可用或看不到時只回「目前無法查看」，不回文件名稱、頁碼或原文，也不區分「不存在」與「無權限」。
- 不回傳任何原始檔案路徑；定位只用 doc_key、版本、頁碼、html_locator（網頁段落定位）。
"""
from __future__ import annotations

import copy
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import history_access, source_label

# 角色 → 可見的文件範圍（與 main._visible、recommend.ROLE_VISIBILITY 一致）
ROLE_VISIBILITY: dict[str, tuple[str, ...]] = {"user": ("all",), "admin": ("all", "admin")}

# 回饋原因代碼白名單：前端只送代碼，避免自由文字被當成分類；自由說明放 comment。
FEEDBACK_REASONS: dict[str, str] = {
    "fits_preference": "符合我的條件",
    "clear_sources": "來源清楚",
    "useful_alternative": "替代方向有幫助",
    "not_fit": "不符合我的條件",
    "source_unclear": "來源看不懂或對不上",
    "too_generic": "太籠統",
    "missed_exclusion": "沒有排除我不要的",
    "other": "其他",
}

# 推薦結果對外公開的欄位（白名單）；其餘欄位（trace、內部 id 等）只留在日誌。
_PUBLIC_DECISION_KEYS = (
    "status", "rules_version", "reason", "recommendations", "hard_conditions", "soft_preferences",
    "assumptions", "limitations", "question", "candidate_count", "synthetic", "product_enabled",
    "brief",   # brief-v1：使用者看的精簡呈現（只含方向名稱、條件與已核對主張原文）
)
# 引用對外公開的欄位（只含定位，不含片段內部 id、全文或檔案路徑）
_PUBLIC_CITATION_KEYS = ("chunk_key", "doc_key", "version_label", "title", "page_start", "page_end",
                         "html_locator", "venue", "year")


class ResultError(Exception):
    """結果檢視或回饋驗證失敗；status／code／message 直接對應 API 錯誤格式。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# 取得角色可見範圍；未知角色直接拒絕（不預設成一般使用者）。
def visibility_for(role: str) -> tuple[str, ...]:
    if role not in ROLE_VISIBILITY:
        raise ResultError(403, "forbidden", "未知角色")
    return ROLE_VISIBILITY[role]


# ---------------------------------------------------------------------------
# 推薦結果公開檢視
# ---------------------------------------------------------------------------
def public_recommendation(decision: dict | None) -> dict | None:
    """把推薦結果（Decision.to_dict() 或日誌中的同格式字典）轉成可回給使用者的版本。

    - 只保留白名單欄位：移除 trace（含被排除候選與受限／失效證據的鍵值）、rechecks 等內部資訊。
    - 每個推薦方向移除 option_id（資料庫內部 id）；每個引用只留定位欄位，並移除 chunk_id。
    - 不修改傳入的字典（日誌內容保持完整）。
    """
    if decision is None:
        return None
    out = {k: copy.deepcopy(decision.get(k)) for k in _PUBLIC_DECISION_KEYS if k in decision}
    for r in out.get("recommendations") or []:
        r.pop("option_id", None)
        for claim in r.get("claims") or []:
            claim["citations"] = [{k: c.get(k) for k in _PUBLIC_CITATION_KEYS}
                                  for c in claim.get("citations") or []]
    return out
# 核心邏輯：採白名單而不是黑名單——recommend.Decision 之後若新增欄位，預設不會外流，
# 需要公開時再明確加進 _PUBLIC_DECISION_KEYS。


def _citation_refs(public: dict) -> list[dict]:
    """列出公開推薦結果中所有引用物件（同一物件，可直接就地標記）。"""
    return [c for r in public.get("recommendations") or [] for cl in r.get("claims") or []
            for c in cl.get("citations") or []]


def mark_citation_availability(conn: psycopg.Connection, public: dict | None, role: str) -> dict | None:
    """回頭查看舊結果時，逐一重新判斷每個引用「此刻」是否仍可用、角色是否看得到。

    可用 → available=True，保留定位；不可用 → 清掉文件名稱、頁碼等，只留 available=False 與說明，
    避免來源被下架或改成管理者限定後，一般使用者仍從歷史結果看到受限資訊。
    """
    if public is None:
        return None
    _mark_refs(conn, _citation_refs(public), role)
    return public


def _mark_refs(conn: psycopg.Connection, refs: list[dict], role: str | None, keep: tuple[str, ...] = (),
               with_text: bool = False) -> None:
    """就地標記一組引用物件此刻的狀態（S9：改由 history_access.resolve 判斷）。

    available  → 以資料庫目前的出處（標題、頁碼、網頁定位）補齊。
    superseded → 當時的版本已被取代、但目前版本有完全相同內容：引用改指向目前版本，
                 並以 cited_version_label 保留原本引用的版本（版本追溯）。
    unavailable→ 清掉文件名稱、頁碼等，只留 available=False 與說明。
    keep：不可用時仍保留的欄位（例如問答的來源編號，讓回答中的〔來源 n〕還對得上）。
    with_text：可用時另補原文與章節（問答的來源清單直接顯示原文；推薦的原文改由引用端點查看）。
    """
    if not refs:
        return
    res = history_access.resolve(
        conn, [(c.get("doc_key"), c.get("version_label"), c.get("chunk_key")) for c in refs], role)
    for c in refs:
        key = (c.get("doc_key"), c.get("version_label"), c.get("chunk_key"))
        r = res.get(key)
        if r is not None and r.state in (history_access.AVAILABLE, history_access.SUPERSEDED):
            row = r.current
            fields = ("title", "page_start", "page_end", "html_locator") + (("section", "quote_text") if with_text else ())
            c.update({k: row[k] for k in fields}, available=True)
            # citation-label-v1：以資料庫目前的書目補刊物、年份與出處文字（給人看的引用不寫文件編號）
            bib = row.get("bibliography") or {}
            c.update(venue=source_label.venue_of(bib), year=source_label.year_of(bib))
            if r.state == history_access.SUPERSEDED:
                c.update(cited_version_label=c.get("version_label"), cited_chunk_key=c.get("chunk_key"),
                         version_label=row["version_label"],
                         chunk_key=row["chunk_key"], superseded=True,
                         note=f"此引用原本來自 {key[1]}；目前版本 {row['version_label']} 有相同內容。")
                if "locator" in c:
                    c["locator"] = _locator(row)
            c["source_label"] = source_label.label_of(c)
        else:
            kept = {k: c[k] for k in keep if k in c}
            c.clear()
            c.update(kept, available=False, note=history_access.UNAVAILABLE_NOTE)
# 核心邏輯：推薦與問答的引用共用同一套判斷；看不到時不區分「不存在」與「無權限」。


# 頁碼或網頁定位轉成顯示文字（與 qa.locator_text 相同格式）。
def _locator(row: dict) -> str:
    ps, pe = row.get("page_start"), row.get("page_end")
    if ps is not None:
        return f"p.{ps}" if pe in (None, ps) else f"p.{ps}–{pe}"
    return row.get("html_locator") or "（無定位）"


# 問答引用對外公開的欄位（S5 日誌 detail.qa.citations 的子集；不含 chunk_id、label、分數）
_PUBLIC_QA_CITATION_KEYS = ("number", "chunk_key", "doc_key", "version_label", "locator", "content_support")


def public_qa(conn: psycopg.Connection, qa_log: dict | None, role: str) -> dict | None:
    """把 S5 問答日誌（detail.qa）轉成可回給使用者的版本，並依角色重新判斷每個引用此刻可否查看。

    回答文字本身在 requests.answer；這裡只回結果類型與引用。出處與原文在每次讀取時由 usable_chunks 補齊，
    所以來源下架或改成限管理者後，原文就不會再出現在一般使用者的回應裡。
    """
    if not qa_log:
        return None
    cites = [{k: c.get(k) for k in _PUBLIC_QA_CITATION_KEYS} for c in qa_log.get("citations") or []]
    _mark_refs(conn, cites, role, keep=("number",), with_text=True)
    return {"outcome": qa_log.get("outcome"), "citations": cites,
            "content_support": "not_verified" if cites else None}
# 核心邏輯：送出當下與回頭查看都用同一個函式，所以兩邊看到的引用一致；
# content_support=not_verified 表示「引用編號在許可來源內」已檢查，但句子是否被來源支持尚未人工核對。


# ---------------------------------------------------------------------------
# 單一請求的結果檢視
# ---------------------------------------------------------------------------
def _own_request(conn: psycopg.Connection, request_id: uuid.UUID, user_id: int) -> dict:
    """讀取「自己的」請求；不是自己的或不存在一律 404（不透露是否存在）。"""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """SELECT id, conversation_id, question, status, answer, error_code, detail,
                      started_at, completed_at, latency_ms
                 FROM requests WHERE id = %s AND user_id = %s""",
            (request_id, user_id),
        ).fetchone()
    if row is None:
        raise ResultError(404, "not_found", "找不到請求")
    return row


def _own_feedback(conn: psycopg.Connection, request_id: uuid.UUID, user_id: int) -> dict | None:
    """讀取使用者對這個請求的回饋（含指定的推薦方向名次）；沒有回饋回傳 None。"""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """SELECT f.rating, f.reason_codes, f.comment, f.created_at, f.updated_at, rr.rank
                 FROM feedback f
                 LEFT JOIN request_recommendations rr ON rr.request_id = f.request_id AND rr.option_id = f.option_id
                WHERE f.request_id = %s AND f.user_id = %s""",
            (request_id, user_id),
        ).fetchone()


def public_result(conn: psycopg.Connection, detail: dict | None, answer: str | None, role: str | None) -> dict:
    """一則回答對外可顯示的部分（送出當下與回頭查看共用）：回答文字、推薦、問答引用、限制。

    S9：先以 history_access.assess 依「此刻」角色與資料狀態判斷；有任何引用或上下文片段已不可用 →
    整則遮蔽（masked=True），只回固定說明，不回回答文字、推薦主張、限制與引用定位。
    """
    detail = detail or {}
    rec_log = detail.get("recommendation")
    qa_log = detail.get("qa")
    verdict = history_access.assess(conn, detail, role)
    if verdict.masked:
        return {"answer": None, "recommendation": None, "limitations": [], "masked": True,
                "masked_note": history_access.MASK_NOTE,
                "qa": ({"outcome": qa_log.get("outcome"), "citations": [], "content_support": None}
                       if qa_log else None)}
    # 日誌中的推薦紀錄有兩種：完整 Decision（有 recommendations 欄位），或延後／錯誤的簡短紀錄
    public = public_recommendation(rec_log) if rec_log and "recommendations" in rec_log else None
    public = mark_citation_availability(conn, public, role)
    if public is not None and public.get("status") == "not_applicable":
        public = None
    if (detail.get("route") or {}).get("use_qa"):
        public = None   # route-v1：這輪改用問答回答，推薦卡判斷只留在紀錄，不對外顯示
    return {"answer": answer, "recommendation": public, "limitations": detail.get("limitations") or [],
            "masked": False, "masked_note": None, "qa": public_qa(conn, qa_log, role)}
# 核心邏輯：遮蔽是「整則」的——只要有一個依據此刻不可用，回答裡由它推導出的句子就可能洩漏受限內容，
# 所以不嘗試逐句挑掉；日誌原文不改，管理者可用 /api/admin/requests/{id}/trace 追溯。


def request_view(conn: psycopg.Connection, request_id: uuid.UUID, user_id: int, role: str | None) -> dict:
    """組出一次請求給擁有者看的結果：問題、結果狀態、回覆、推薦（已清理）、限制、補問、自己的回饋。"""
    row = _own_request(conn, request_id, user_id)
    detail = row["detail"] or {}
    shown = public_result(conn, detail, row["answer"], role)
    return {
        "request_id": str(row["id"]), "conversation_id": str(row["conversation_id"]),
        "question": row["question"], "request_status": row["status"],
        "status": detail.get("outcome"), "message": detail.get("message"), "intent": detail.get("intent"),
        "secondary_intents": detail.get("secondary_intents") or [],
        "error_code": row["error_code"],
        "applied_changes": detail.get("applied_changes") or [],
        "clarification": detail.get("clarification") or [],
        **shown,
        "started_at": row["started_at"], "completed_at": row["completed_at"], "latency_ms": row["latency_ms"],
        "feedback": _own_feedback(conn, request_id, user_id),
        "feedback_reasons": FEEDBACK_REASONS,
    }
# 核心邏輯：
# 1. 只有擁有者讀得到（user_id 條件寫在 SQL）；管理者也不能經由這個端點讀別人的請求。
# 2. detail 裡的 preferences_before、parse_calls、trace 等日誌欄位不回傳；只回畫面需要的欄位。
# 3. 回答、推薦、引用在每次讀取時依此刻角色重新判斷（public_result）；role 由呼叫端以 current_role 取得。


# ---------------------------------------------------------------------------
# 引用片段查看
# ---------------------------------------------------------------------------
def citation_text(conn: psycopg.Connection, doc_key: str, version_label: str, chunk_key: str, role: str) -> dict:
    """依引用定位取回片段原文與出處；只回此刻可用且角色看得到的片段，否則 404。

    回傳欄位：原文（quote_text）、章節、頁碼、html_locator、文件標題與版本、書目資訊；
    不回傳原始檔案路徑、內部 id 或審核資訊。
    """
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """SELECT chunk_key, doc_key, title, version_label, page_start, page_end, html_locator, section,
                      quote_text, attributes -> 'citation' AS citation, bibliography ->> 'year' AS year,
                      bibliography ->> 'url' AS url, bibliography ->> 'institution' AS venue
                 FROM usable_chunks
                WHERE doc_key = %s AND version_label = %s AND chunk_key = %s AND visibility = ANY(%s)""",
            (doc_key, version_label, chunk_key, list(visibility_for(role))),
        ).fetchone()
    if row is None:
        raise ResultError(404, "not_found", "找不到可查看的來源片段")
    return row
# 核心邏輯：資格判斷只看 usable_chunks＋角色可見範圍；不存在、未核准、下架、過期、無權限都回同一個 404，
# 一般使用者無法藉此探測管理者限定文件是否存在。來源文字只當資料回傳，前端以純文字顯示。


# ---------------------------------------------------------------------------
# 回饋驗證
# ---------------------------------------------------------------------------
def validate_feedback(conn: psycopg.Connection, request_id: uuid.UUID, user_id: int,
                      rank: int | None, reason_codes: list[str]) -> int | None:
    """檢查使用者能不能對這個請求（與指定的推薦方向）回饋；通過時回傳要寫入的 option_id（或 None）。

    規則：
    1. 請求必須是自己的（否則 404，不透露存在與否）。
    2. 請求必須已完成（started 代表還沒有結果，不能回饋；409）。
    3. 原因代碼必須在白名單內（422）。
    4. 指定 rank 時，該名次必須是這次請求實際的推薦方向（422）；回饋因此能追到哪一個方向。
    """
    row = _own_request(conn, request_id, user_id)
    if row["status"] == "started":
        raise ResultError(409, "request_not_finished", "這個請求還沒有完成，不能回饋")
    unknown = sorted(set(reason_codes) - FEEDBACK_REASONS.keys())
    if unknown:
        raise ResultError(422, "validation_error", f"不支援的回饋原因：{'、'.join(unknown)}")
    if rank is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        hit = cur.execute("SELECT option_id FROM request_recommendations WHERE request_id = %s AND rank = %s",
                          (request_id, rank)).fetchone()
    if hit is None:
        raise ResultError(422, "validation_error", "這次結果沒有這個推薦方向")
    return hit["option_id"]
# 核心邏輯：資料庫外鍵已保證回饋只能掛在自己的請求（feedback → requests(id, user_id)）；
# 這裡再補上「有結果才回饋」與「方向屬於這次結果」兩項，讓回饋與日誌、推薦紀錄能一一對上。
