"""S10 管理者限定的最小監控：統計摘要、請求清單、單一請求詳情（含錯誤）。

用途
- 讓管理者在一個頁面看到：某段時間內的請求數、成功／失敗／補問／資料不足分類、
  耗時中位數與 P95、Token 用量（含未知筆數）、負面回饋，並可點進單一 request 看處理過程與錯誤。

資料來源
- 全部讀自既有日誌（requests、retrieval_hits、request_recommendations、feedback），
  不新增資料表、不新增遷移，也不改變寫入日誌的方式。

權限與隱私原則（規範 1、8；資訊安全規格「日誌外洩」）
- 端點只掛在 require_admin 之後；角色只取自後端認證。
- 延續 S2 的 /api/admin/requests 決定：管理日誌只給營運所需的中繼資料；
  使用者的問題、回答、模型原始輸出、送入模型的上下文、偏好原文、回饋自由說明都不在監控端點。
  （詳情只回「有沒有」這些內容與長度，方便判斷問題，不回原文。）
- 所有回傳的日誌欄位都再經過 redact()：鍵名像秘密（密碼、金鑰、Authorization、cookie、CSRF…）
  的值一律遮蔽；網址中的帳密與查詢字串也遮蔽。即使未來有人把秘密誤寫進日誌，也不會從監控頁外流。

統計口徑（寫在回應的 definitions 欄位，前端照樣顯示）
- 時間範圍以 requests.started_at 判斷，左閉右開 [since, until)，時區 UTC。
- 分類（互斥，每筆請求只屬一類）：
    in_progress        status = started（尚未完成；程序中斷的請求也會停在這裡）
    failed             status = failed
    clarification      outcome = needs_clarification（先補問、不推薦）
    insufficient_data  outcome ∈ {insufficient_evidence, answer_insufficient_evidence}
    success            其餘 status = succeeded（含 no_match：已正確套用排除條件但沒有符合者）
- 耗時：只計已完成且有 latency_ms 的請求；P95 用 PostgreSQL percentile_cont（線性內插）。
- Token：NULL 代表未知（規範 8），加總只加「已知」的值，另外列出未知筆數；全部未知時加總回 null，不回 0。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query
from psycopg import Connection

# 預設統計範圍（天）與允許的最大範圍（天）：避免一次掃描整個日誌表
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 92
# 「最近負面回饋」最多列幾筆
NEGATIVE_FEEDBACK_LIMIT = 20

CATEGORIES = ("success", "failed", "clarification", "insufficient_data", "in_progress")
CATEGORY_LABELS = {
    "success": "成功", "failed": "失敗", "clarification": "補問",
    "insufficient_data": "資料不足", "in_progress": "未完成",
}

# 把一筆 requests 列分到一個類別的 SQL（與上方模組說明的口徑一致；改口徑時兩處一起改）
_CATEGORY_SQL = """
CASE
  WHEN r.status = 'started' THEN 'in_progress'
  WHEN r.status = 'failed' THEN 'failed'
  WHEN r.detail ->> 'outcome' = 'needs_clarification' THEN 'clarification'
  WHEN r.detail ->> 'outcome' IN ('insufficient_evidence', 'answer_insufficient_evidence') THEN 'insufficient_data'
  ELSE 'success'
END"""

DEFINITIONS = {
    "time_field": "requests.started_at（UTC），範圍為 [since, until)",
    "categories": {
        "success": "已完成且非補問、非資料不足（含 no_match：已套用排除條件但沒有符合者）",
        "failed": "status = failed（依 error_code 細分）",
        "clarification": "outcome = needs_clarification（先補問、不推薦）",
        "insufficient_data": "outcome = insufficient_evidence 或 answer_insufficient_evidence",
        "in_progress": "status = started（尚未完成，或處理中程序中斷）",
    },
    "latency": "只計已完成且有 latency_ms 的請求；P95 為 percentile_cont 線性內插",
    "tokens": "NULL＝未知（未取得用量，不記 0）；加總只含已知值，另列未知筆數；"
              "目前規則版解析與推薦不呼叫模型，因此多數請求的 Token 為未知",
    "privacy": "不含使用者問題、回答、模型原始輸出、送入模型的上下文、偏好原文與回饋自由說明；秘密欄位一律遮蔽",
}


class MonitorError(Exception):
    """監控查詢參數不合法；status／code／message 直接對應 API 錯誤格式。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------------------
# 秘密遮蔽
# ---------------------------------------------------------------------------
REDACTED = "[已遮蔽]"
# 鍵名像秘密就整個遮蔽。注意 input_tokens／output_tokens 是用量數字，不是秘密，所以 token 只比對特定組合。
_SECRET_KEY = re.compile(
    r"pass(word|wd)?|secret|authorization|cookie|csrf|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|(session|auth|access|refresh|bearer|id)[_-]?token|^token$|credential",
    re.IGNORECASE,
)
# 值本身看起來像秘密：Bearer 權杖、常見金鑰前綴
_SECRET_VALUE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}|\b(sk|pk|rk)-[a-z0-9_-]{12,}")
# 網址裡的帳密（scheme://user:pass@host）與查詢字串（可能帶 key=…）
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_URL_QUERY = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#]+)\?[^\s#]+")


# 遞迴遮蔽一個 JSON 值裡的秘密，回傳新的值（不修改傳入物件）。
def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return None if value is None else REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = _SECRET_VALUE.sub(REDACTED, value)
        text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
        return _URL_QUERY.sub(lambda m: f"{m.group(1)}?{REDACTED}", text)
    return value
# 核心邏輯：
# 1. 鍵名命中秘密樣式 → 值整個換成「[已遮蔽]」（None 保持 None，才看得出「本來就沒有」）。
# 2. 字串值另外掃描：Bearer 權杖、sk-… 類金鑰、網址帳密、網址查詢字串都換掉。
# 3. 數字、布林原樣保留（input_tokens 等用量不受影響）。


# ---------------------------------------------------------------------------
# 時間範圍
# ---------------------------------------------------------------------------
# 解析並檢查統計範圍；沒給就用「現在往前 DEFAULT_RANGE_DAYS 天」。回傳 (since, until)，皆為 UTC。
def resolve_range(since: datetime | None, until: datetime | None,
                  now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    until = _as_utc(until) if until is not None else now
    since = _as_utc(since) if since is not None else until - timedelta(days=DEFAULT_RANGE_DAYS)
    if since >= until:
        raise MonitorError(422, "validation_error", "統計範圍的開始時間必須早於結束時間")
    if until - since > timedelta(days=MAX_RANGE_DAYS):
        raise MonitorError(422, "validation_error", f"統計範圍最多 {MAX_RANGE_DAYS} 天")
    return since, until
# 核心邏輯：沒帶時區的時間一律視為 UTC（畫面上也標 UTC），避免伺服器時區不同造成統計飄移。


# 沒有時區資訊的時間補上 UTC；有時區的轉成 UTC。
def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 統計摘要
# ---------------------------------------------------------------------------
# 指定時間範圍內的監控摘要：請求數、分類、耗時、Token、結果與錯誤細分、回饋。
def summary(conn: Connection, since: datetime, until: datetime) -> dict:
    params = {"since": since, "until": until}
    # 類別名稱來自程式內常數 CATEGORIES（不是使用者輸入），可以安全地組進 SQL
    category_counts = ", ".join(f"count(*) FILTER (WHERE category = '{c}') AS {c}" for c in CATEGORIES)
    totals = conn.execute(
        f"""SELECT count(*) AS total, {category_counts},
                   count(latency_ms) AS latency_n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS latency_p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS latency_p95,
                   max(latency_ms) AS latency_max,
                   sum(input_tokens) AS input_tokens_sum, count(input_tokens) AS input_tokens_known,
                   sum(output_tokens) AS output_tokens_sum, count(output_tokens) AS output_tokens_known,
                   count(*) FILTER (WHERE model_name IS NOT NULL) AS model_requests
              FROM (SELECT r.*, {_CATEGORY_SQL} AS category FROM requests r
                     WHERE r.started_at >= %(since)s AND r.started_at < %(until)s) r""",
        params,
    ).fetchone()
    total = totals["total"]
    by_outcome = conn.execute(
        f"""SELECT {_CATEGORY_SQL} AS category, coalesce(r.detail ->> 'outcome', '（無）') AS outcome,
                   count(*) AS n
              FROM requests r WHERE r.started_at >= %(since)s AND r.started_at < %(until)s
             GROUP BY 1, 2 ORDER BY n DESC, outcome""",
        params,
    ).fetchall()
    by_error = conn.execute(
        """SELECT r.error_code, r.error_detail ->> 'stage' AS stage, count(*) AS n,
                  max(r.started_at) AS last_seen
             FROM requests r
            WHERE r.started_at >= %(since)s AND r.started_at < %(until)s AND r.error_code IS NOT NULL
            GROUP BY 1, 2 ORDER BY n DESC, r.error_code""",
        params,
    ).fetchall()
    return {
        "range": {"since": since.isoformat(), "until": until.isoformat(), "timezone": "UTC",
                  "generated_at": datetime.now(timezone.utc).isoformat()},
        "requests": {
            "total": total,
            "categories": [{"key": c, "label": CATEGORY_LABELS[c], "n": totals[c],
                            "rate": _rate(totals[c], total)} for c in CATEGORIES],
            "by_outcome": by_outcome,
            "by_error": by_error,
        },
        "latency_ms": {
            "n": totals["latency_n"],
            "p50": _round(totals["latency_p50"]),
            "p95": _round(totals["latency_p95"]),
            "max": totals["latency_max"],
        },
        "tokens": {
            "input_sum": totals["input_tokens_sum"],          # 全部未知時為 None（不是 0）
            "input_known": totals["input_tokens_known"],
            "input_unknown": total - totals["input_tokens_known"],
            "output_sum": totals["output_tokens_sum"],
            "output_known": totals["output_tokens_known"],
            "output_unknown": total - totals["output_tokens_known"],
            "model_requests": totals["model_requests"],
        },
        "feedback": _feedback_summary(conn, params),
        "definitions": DEFINITIONS,
    }
# 核心邏輯：
# 1. 先用 _CATEGORY_SQL 替每筆請求貼上唯一的類別，再一次算出各類數量、耗時百分位與 Token 加總。
# 2. SQL 的 sum() 在沒有任何非 NULL 值時回 NULL，正好對應「全部未知」，不另外轉成 0。
# 3. 結果與錯誤再各自分組，方便看出失敗集中在哪個階段（error_detail.stage）。


# 回饋摘要：正負回饋數、負面原因代碼分布、最近的負面回饋（不含自由說明原文）。
def _feedback_summary(conn: Connection, params: dict) -> dict:
    counts = conn.execute(
        """SELECT count(*) FILTER (WHERE f.rating = 1) AS positive,
                  count(*) FILTER (WHERE f.rating = -1) AS negative
             FROM feedback f JOIN requests r ON r.id = f.request_id
            WHERE r.started_at >= %(since)s AND r.started_at < %(until)s""",
        params,
    ).fetchone()
    reasons = conn.execute(
        """SELECT code, count(*) AS n
             FROM feedback f JOIN requests r ON r.id = f.request_id,
                  jsonb_array_elements_text(f.reason_codes) AS code
            WHERE r.started_at >= %(since)s AND r.started_at < %(until)s AND f.rating = -1
            GROUP BY code ORDER BY n DESC, code""",
        params,
    ).fetchall()
    recent = conn.execute(
        """SELECT f.request_id, f.updated_at, f.reason_codes, o.option_key,
                  (f.comment IS NOT NULL AND length(f.comment) > 0) AS has_comment,
                  r.detail ->> 'outcome' AS outcome
             FROM feedback f JOIN requests r ON r.id = f.request_id
             LEFT JOIN recommendation_options o ON o.id = f.option_id
            WHERE r.started_at >= %(since)s AND r.started_at < %(until)s AND f.rating = -1
            ORDER BY f.updated_at DESC LIMIT %(limit)s""",
        {**params, "limit": NEGATIVE_FEEDBACK_LIMIT},
    ).fetchall()
    return {"positive": counts["positive"], "negative": counts["negative"],
            "negative_reasons": reasons, "recent_negative": recent}
# 核心邏輯：回饋依「所屬請求的開始時間」歸到統計範圍，與請求數口徑一致；
# 自由說明（comment）只回 has_comment，原文不進管理日誌。


# 比例（四捨五入到小數 4 位）；分母為 0 時回 None，不回 0%。
def _rate(n: int, total: int) -> float | None:
    return round(n / total, 4) if total else None


# 百分位數四捨五入成整數毫秒；沒有資料時維持 None。
def _round(value: float | None) -> int | None:
    return None if value is None else int(round(value))


# ---------------------------------------------------------------------------
# 請求清單
# ---------------------------------------------------------------------------
# 指定範圍內的請求清單（新到舊），可依類別或錯誤代碼篩選。只回中繼資料，不含問題與回答原文。
def list_requests(conn: Connection, since: datetime, until: datetime, *, category: str | None = None,
                  error_code: str | None = None, limit: int = 50) -> dict:
    if category is not None and category not in CATEGORIES:
        raise MonitorError(422, "validation_error", "未知的分類")
    rows = conn.execute(
        f"""SELECT * FROM (
              SELECT r.id AS request_id, r.user_id, r.conversation_id, r.started_at, r.completed_at,
                     r.status, {_CATEGORY_SQL} AS category, r.detail ->> 'outcome' AS outcome,
                     r.detail ->> 'intent' AS intent, r.error_code, r.latency_ms,
                     r.input_tokens, r.output_tokens, r.model_name, r.retry_count, f.rating AS feedback_rating
                FROM requests r
                LEFT JOIN feedback f ON f.request_id = r.id AND f.user_id = r.user_id
               WHERE r.started_at >= %(since)s AND r.started_at < %(until)s
                 AND (%(error_code)s::text IS NULL OR r.error_code = %(error_code)s::text)) x
             WHERE (%(category)s::text IS NULL OR x.category = %(category)s::text)
             ORDER BY x.started_at DESC LIMIT %(limit)s""",
        {"since": since, "until": until, "category": category, "error_code": error_code, "limit": limit},
    ).fetchall()
    return {"range": {"since": since.isoformat(), "until": until.isoformat(), "timezone": "UTC"},
            "filters": {"category": category, "error_code": error_code, "limit": limit},
            "items": rows}
# 核心邏輯：篩選值一律走參數（不拼 SQL）；分類先在子查詢算好再篩，口徑與摘要完全相同。


# ---------------------------------------------------------------------------
# 單一請求詳情
# ---------------------------------------------------------------------------
# detail.parse_calls 每筆對外保留的欄位（不含 output：解析結果含使用者偏好原文）
_PARSE_CALL_KEYS = ("stage", "parser", "parser_version", "vocab_version", "model_provider", "model_name",
                    "input_tokens", "output_tokens", "retry_count", "latency_ms", "error")
# detail.recommendation 對外保留的欄位（trace 是取捨紀錄，管理者可看；條件原文在 hard_conditions 等欄位，不回）
_REC_KEYS = ("status", "rules_version", "reason", "candidate_count", "synthetic", "product_enabled",
             "rechecks", "latency_ms", "role", "had_prior", "error")
# detail.qa 對外保留的欄位（不含 context_sent.messages、claims、citations 的原文）
_QA_KEYS = ("prompt_version", "output_schema", "role", "config", "model_calls", "timings_ms", "outcome",
            "error_code", "usable_chunks_for_role", "retrieval", "generation", "final_revalidate")


# 讀取一筆請求的詳情（管理者用）；不存在回 404。
def request_detail(conn: Connection, request_id: uuid.UUID) -> dict:
    row = conn.execute(
        f"""SELECT r.id AS request_id, r.user_id, r.conversation_id, r.status, {_CATEGORY_SQL} AS category,
                   r.started_at, r.completed_at, r.latency_ms, r.retry_count,
                   r.index_version_id, iv.label AS index_label,
                   r.model_provider, r.model_name, r.prompt_version, r.app_version,
                   r.input_tokens, r.output_tokens, r.error_code, r.error_detail, r.detail,
                   length(r.question) AS question_chars, length(r.answer) AS answer_chars,
                   length(r.model_output) AS model_output_chars
              FROM requests r LEFT JOIN index_versions iv ON iv.id = r.index_version_id
             WHERE r.id = %s""",
        (request_id,),
    ).fetchone()
    if row is None:
        raise MonitorError(404, "not_found", "找不到這筆請求")
    detail = row.pop("detail") or {}
    hits = conn.execute(
        """SELECT h.retriever, h.rank, c.chunk_key, d.doc_key, dv.version_label, h.score,
                  h.passed_filter, h.filter_reason
             FROM retrieval_hits h
             JOIN chunks c ON c.id = h.chunk_id
             JOIN document_versions dv ON dv.id = c.document_version_id
             JOIN documents d ON d.id = dv.document_id
            WHERE h.request_id = %s ORDER BY h.retriever, h.rank""",
        (request_id,),
    ).fetchall()
    recs = conn.execute(
        """SELECT rr.rank, o.option_key, o.status AS option_status,
                  (SELECT count(*) FROM request_citations rc
                    WHERE rc.request_id = rr.request_id AND rc.rank = rr.rank) AS citations
             FROM request_recommendations rr JOIN recommendation_options o ON o.id = rr.option_id
            WHERE rr.request_id = %s ORDER BY rr.rank""",
        (request_id,),
    ).fetchall()
    fb = conn.execute(
        """SELECT f.rating, f.reason_codes, o.option_key,
                  (f.comment IS NOT NULL AND length(f.comment) > 0) AS has_comment, f.created_at, f.updated_at
             FROM feedback f LEFT JOIN recommendation_options o ON o.id = f.option_id
            WHERE f.request_id = %s""",
        (request_id,),
    ).fetchone()
    return redact({
        **row,
        "outcome": detail.get("outcome"),
        "intent": detail.get("intent"),
        "secondary_intents": detail.get("secondary_intents", []),
        "answer_source": detail.get("answer_source"),
        "schema_version": detail.get("schema_version"),
        "clarification_count": len(detail.get("clarification") or []),
        "limitations_count": len(detail.get("limitations") or []),
        "parse_calls": [{k: c.get(k) for k in _PARSE_CALL_KEYS} for c in detail.get("parse_calls") or []],
        "recommendation": _recommendation_view(detail.get("recommendation")),
        "qa": _qa_view(detail.get("qa")),
        "retrieval_hits": hits,
        "recommendations": recs,
        "feedback": fb,
    })
# 核心邏輯：
# 1. 中繼資料（狀態、耗時、模型、版本、Token、錯誤）原樣回傳；問題、回答、模型原始輸出只回字數。
# 2. detail JSON 依白名單挑欄位（_PARSE_CALL_KEYS／_REC_KEYS／_QA_KEYS），新增到日誌的欄位預設不外露。
# 3. 召回、推薦、回饋從關聯表讀出，用 chunk_key／option_key 定位，不回片段原文。
# 4. 最後整份再經過 redact()，就算白名單內的欄位意外帶到秘密也會被遮蔽。


# 推薦日誌的管理者檢視：保留狀態、版本、取捨紀錄；推薦方向只留 rank 與候選代碼。
def _recommendation_view(rec_log: dict | None) -> dict | None:
    if not rec_log:
        return None
    view = {k: rec_log.get(k) for k in _REC_KEYS if k in rec_log}
    view["recommendations"] = [{"rank": r.get("rank"), "option_key": r.get("option_key")}
                               for r in rec_log.get("recommendations") or []]
    view["trace"] = rec_log.get("trace") or []
    return view


# 問答日誌的管理者檢視：保留檢索、模型呼叫、驗證與錯誤；送入模型的訊息與回答主張不回。
def _qa_view(qa_log: dict | None) -> dict | None:
    if not qa_log:
        return None
    view = {k: qa_log.get(k) for k in _QA_KEYS if k in qa_log}
    sent = qa_log.get("context_sent") or {}
    view["context_evidence"] = [{k: e.get(k) for k in ("label", "chunk_key", "doc_key", "version_label")}
                                for e in sent.get("evidence") or []]
    view["citations"] = [{k: c.get(k) for k in ("number", "label", "chunk_key", "doc_key", "version_label")}
                         for c in qa_log.get("citations") or []]
    view["claims_count"] = len(qa_log.get("claims") or [])
    return view


# ---------------------------------------------------------------------------
# 掛載端點
# ---------------------------------------------------------------------------
# 把監控端點掛到 app；require_admin、get_conn 由 main.create_app 傳入，to_api_error 把 MonitorError 轉成 API 錯誤。
def register(app: FastAPI, *, require_admin: Callable, get_conn: Callable,
             to_api_error: Callable[[int, str, str], Exception]) -> None:

    # 執行查詢函式，把 MonitorError 轉成 API 統一錯誤格式。
    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MonitorError as exc:
            raise to_api_error(exc.status, exc.code, exc.message)

    @app.get("/api/admin/monitor/summary")
    def monitor_summary(since: datetime | None = Query(None), until: datetime | None = Query(None),
                        _admin=Depends(require_admin), conn: Connection = Depends(get_conn)):
        start, end = call(resolve_range, since, until)
        return summary(conn, start, end)

    @app.get("/api/admin/monitor/requests")
    def monitor_requests(since: datetime | None = Query(None), until: datetime | None = Query(None),
                         category: str | None = Query(None, max_length=32),
                         error_code: str | None = Query(None, max_length=64),
                         limit: int = Query(50, ge=1, le=200),
                         _admin=Depends(require_admin), conn: Connection = Depends(get_conn)):
        start, end = call(resolve_range, since, until)
        return call(list_requests, conn, start, end, category=category, error_code=error_code, limit=limit)

    @app.get("/api/admin/monitor/requests/{request_id}")
    def monitor_request_detail(request_id: uuid.UUID, _admin=Depends(require_admin),
                               conn: Connection = Depends(get_conn)):
        return call(request_detail, conn, request_id)
# 核心邏輯：三個端點都只接受 GET、都依賴 require_admin（一般使用者 403、未登入 401）；
# 時間範圍先經 resolve_range 檢查，查詢函式本身不碰認證，方便單獨測試。
