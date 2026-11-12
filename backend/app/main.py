"""LipMatch AI 後端 API（S2：認證、角色、對話所有權；DEC-10：session cookie＋CSRF）。

啟動：uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
（需先設定 DATABASE_URL 並執行 python -m app.cli migrate）
規格：docs/mvp/api-contract.md
"""
from __future__ import annotations

import copy
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterator, Literal
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import answer_tasks, color_reference, exit_assist, history_access, ingest, message_notices, monitoring, preferences as pref, progress, qa, recommend as rec, release, results
from .config import Settings, load_settings
from .readiness import DbStatus, check_connection
from .login_limit import record_attempt, seconds_until_allowed
from .security import (
    burn_password_check,
    csrf_token_for,
    csrf_token_matches,
    new_token,
    token_digest,
    verify_password,
)

log = logging.getLogger("lipmatch")

API_VERSION = "0.2.0"
# 就緒檢查取得資料庫連線的最長等待秒數
READY_DB_TIMEOUT_S = 3
# 會修改資料的 HTTP 方法：一律檢查 Origin；已登入者另需 CSRF 權杖
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "X-CSRF-Token"


# ---------------------------------------------------------------------------
# 錯誤格式：{"error": {"code", "message", "request_id"[, "details"]}}，標頭帶 X-Request-ID
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        self.retry_after: int | None = None

    # 附上 Retry-After 秒數（429 用），回傳自己以便直接 raise。
    def with_retry_after(self, seconds: int) -> "ApiError":
        self.retry_after = seconds
        return self


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.state.request_id = str(uuid.uuid4())
    return rid


def _error(request: Request, status: int, code: str, message: str, details=None) -> JSONResponse:
    rid = _request_id(request)
    body = {"error": {"code": code, "message": message, "request_id": rid}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": rid})


_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


# sources-cited-v1（2026-11-09）：來源頁的「轉引文獻」一律回傳字串清單。
# 實驗資料庫有些片段的 attributes.cited_works 存成文字（"[]"、"['Liang and Wan, 2017', …]"），
# 原樣回傳時前端把它當陣列呼叫 join，來源頁一搜尋就顯示「發生未預期的錯誤」。資料不改，回傳時轉型。
def cited_works_list(value) -> list[str]:
    """把 cited_works 轉成字串清單：清單照用；JSON 或 Python 寫法的清單文字解析後用；其他非空文字當成單筆；空值回 []。"""
    import ast
    import json as _json
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    for parse in (_json.loads, ast.literal_eval):
        try:
            parsed = parse(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, (list, tuple)):
            return [str(v) for v in parsed if str(v).strip()]
        break
    return [text]


# 取得請求的來源（scheme://host[:port]）；沒有 Origin 時改看 Referer。
# 用途：判斷會修改資料的請求是不是從本站頁面發出（CSRF 防護的第一層）。
def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None
# 核心邏輯：瀏覽器送出 POST／PUT 等請求時會帶 Origin；少數情況只帶 Referer，就取它的 scheme 與主機部分。
# 兩者都沒有或 Origin 為 "null"（例如沙箱 iframe）時回傳 None，由呼叫端拒絕。


# ---------------------------------------------------------------------------
# 請求／回應模型：一律禁止多餘欄位，避免前端偷塞 role、user_id
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(_Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ConversationIn(_Strict):
    title: str | None = Field(default=None, max_length=200)
    scenario: Literal["daily", "office", "party"] | None = None


class FeedbackIn(_Strict):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2000)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    rank: int | None = Field(default=None, ge=1, le=3)   # S8：回饋針對哪一個推薦方向（1 主推、2–3 替代）


_Term = Field(min_length=1, max_length=50)


class PreferencesIn(_Strict):
    # 只套用有帶的欄位（model_fields_set）；沒帶的欄位維持原狀
    liked: list[str] = Field(default_factory=list, max_length=20)      # 想要的色系／妝感
    disliked: list[str] = Field(default_factory=list, max_length=20)   # 明確排斥（硬條件；修改情境時保留）
    finish: Literal["matte", "satin", "gloss", "any"] | None = None
    notes: str | None = Field(default=None, max_length=500)
    effects: list[str] = Field(default_factory=list, max_length=10)    # 希望效果（S6）
    skin_tone: str | None = Field(default=None, max_length=50)         # 自述膚色原話（S6；不推定底調）
    undertone: Literal["warm", "cool", "neutral", "unknown"] | None = None  # 只有使用者明說才填（S6）
    natural_lip: str | None = Field(default=None, max_length=50)       # 已知原生唇色（S6）


class ConversationPatch(_Strict):
    title: str | None = Field(default=None, max_length=200)
    scenario: Literal["daily", "office", "party"] | None = None
    preferences: PreferencesIn | None = None
    allergy_status: Literal["unanswered", "none_known", "has_known", "unsure"] | None = None
    allergy_avoid: list[str] | None = Field(default=None, max_length=20)


class MessageIn(_Strict):
    question: str = Field(min_length=1, max_length=2000)


class ReviewIn(_Strict):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=2, max_length=2000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")   # 審核人看到的內容；與目前不符就拒絕
    # 審核人畫面上看到的審核對象（文件、版本、片段編號）；後端比對 chunk_id 實際所屬，不一致就拒絕，
    # 確保「顯示的對象」與「送出的對象」是同一筆（審核頁版本提示，2026-10-18）。
    doc_key: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=100)
    version_label: str = Field(min_length=1, max_length=40)
    chunk_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]+$", max_length=200)
    # 核准「非最新版本」的片段時，審核人須在畫面上看過提示並明確確認；未確認一律拒絕
    confirm_not_latest: bool = False


class PublishIn(_Strict):
    doc_key: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    version_label: str = Field(min_length=1, max_length=40)
    # S9：目前版本是 active 推薦候選的證據時，須明確確認「切換後這些候選證據失效」才發布
    accept_candidate_impact: bool = False


class VisibilityIn(_Strict):
    visibility: Literal["all", "admin"]


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str
    session_id: int


def create_app(settings: Settings | None = None, qa_config: "qa.QaConfig | None" = None) -> FastAPI:
    settings = settings or load_settings()
    # S5 問答設定（生成模型、逾時、重試、檢索數量）；金鑰只在記憶體，不寫日誌
    qa_config = qa_config or qa.QaConfig.from_env()
    # color-ref-v1：只有設定 COLOR_REFERENCE_CSV＋SHA-256 且指紋相符才啟用；讀不到或不符就不附參照（記在日誌）
    try:
        color_ref, color_ref_status = color_reference.ColorReference.from_env()
    except (OSError, KeyError, ValueError) as exc:
        color_ref, color_ref_status = None, {"status": "disabled", "reason": type(exc).__name__}
    log.info("color reference: %s", color_ref_status)
    pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row},
        open=False,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        pool.open(wait=True)
        try:
            yield
        finally:
            pool.close()

    # Secure cookie 可用 __Host- 前綴（瀏覽器會強制 Secure、Path=/、不可設 Domain）；本機 http 不能用
    cookie_name = "__Host-lipmatch_session" if settings.session_cookie_secure else "lipmatch_session"

    # 全域依賴：所有會修改資料的請求（含登入）都必須來自允許的 Origin。
    # 用途：CSRF 防護的第一層；第二層是 current_user 裡的 CSRF 權杖檢查。
    def require_allowed_origin(request: Request) -> None:
        if request.method not in UNSAFE_METHODS:
            return
        if _request_origin(request) not in settings.allowed_origins:
            raise ApiError(403, "csrf_failed", "請求來源不被允許")
    # 核心邏輯：GET 等唯讀請求不檢查；其餘請求的 Origin（或 Referer）必須完全等於 APP_ORIGINS 其中之一。
    # 沒設定 APP_ORIGINS 時清單是空的，所有會修改資料的請求都會被拒絕（預設拒絕）。

    app = FastAPI(
        title="LipMatch AI API",
        version=API_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(require_allowed_origin)],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        # 一律由伺服器產生，不沿用用戶端送來的 X-Request-ID
        rid = _request_id(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        response = _error(request, exc.status, exc.code, exc.message)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return _error(request, exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # 只回欄位位置與錯誤類型，不回送使用者輸入的值（可能含密碼）
        details = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        return _error(request, 422, "validation_error", "請求格式不正確", details)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error request_id=%s", _request_id(request))
        return _error(request, 500, "internal_error", "伺服器發生錯誤，請以 request_id 回報")

    # -----------------------------------------------------------------------
    # 依賴：資料庫連線、目前使用者、管理者
    # -----------------------------------------------------------------------
    def get_conn() -> Iterator[Connection]:
        with pool.connection() as conn:
            yield conn

    # 取得目前登入者：只認 session cookie，不接受 Authorization 標頭。
    # 用途：所有需要登入的端點都依賴它；會修改資料的請求在這裡另外檢查 CSRF 權杖。
    def current_user(request: Request, conn: Connection = Depends(get_conn)) -> CurrentUser:
        token = request.cookies.get(cookie_name, "")
        if not token:
            raise ApiError(401, "unauthorized", "需要登入")
        row = conn.execute(
            """SELECT u.id, u.username, u.role, s.id AS session_id
                 FROM auth_sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_sha256 = %s AND s.revoked_at IS NULL
                  AND s.expires_at > now() AND u.is_active""",
            (token_digest(token),),
        ).fetchone()
        if row is None:
            raise ApiError(401, "unauthorized", "登入已失效，請重新登入")
        if request.method in UNSAFE_METHODS and not csrf_token_matches(token, request.headers.get(CSRF_HEADER, "")):
            raise ApiError(403, "csrf_failed", "缺少或錯誤的 CSRF 權杖")
        request.state.session_token = token
        return CurrentUser(**row)
    # 核心邏輯：
    # 1. 從 cookie 取出 session 權杖，資料庫只比對它的 SHA-256；過期、撤銷、帳號停用都視為未登入。
    # 2. 會修改資料的請求，X-CSRF-Token 必須等於由這個 session 權杖衍生的值，否則回 403。
    # 3. 角色只從資料庫的 users.role 取得，請求內容、標頭、其他 cookie 一律不採信。

    def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role != "admin":
            raise ApiError(403, "forbidden", "需要管理者權限")
        return user

    # -----------------------------------------------------------------------
    # 端點
    # -----------------------------------------------------------------------
    @app.get("/api/health")
    def health(conn: Connection = Depends(get_conn)):
        conn.execute("SELECT 1")
        return {"status": "ok", "api_version": API_VERSION}

    # 就緒檢查（S1）：資料庫可查詢、pgvector 可用、遷移已套用、帳號非超級使用者才回 200。
    # 用途：容器 healthcheck 與前端狀態頁；資料庫斷線時在 READY_DB_TIMEOUT_S 秒內回 503，不等連線池預設的 30 秒。
    @app.get("/api/ready")
    def ready():
        started = time.perf_counter()
        try:
            with pool.connection(timeout=READY_DB_TIMEOUT_S) as conn:
                status = check_connection(conn)
        except psycopg.Error as exc:
            # 只記錄例外類別：psycopg 的錯誤訊息可能帶有主機與帳號
            log.warning("readiness check failed: %s", type(exc).__name__)
            status = DbStatus(ok=False, detail=f"資料庫無法連線（{type(exc).__name__}）")
        else:
            status = replace(status, latency_ms=int((time.perf_counter() - started) * 1000))
        body = {
            "status": "ready" if status.ok else "not_ready",
            "api_version": API_VERSION,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "database": status.to_dict(),
        }
        return JSONResponse(body, status_code=200 if status.ok else 503)
    # 核心邏輯：
    # 1. 從連線池取連線時設定短逾時；連線池的 PoolTimeout 也是 psycopg.Error 的子類別，會一併攔下。
    # 2. 連得上就交給 check_connection 判斷；連不上只回報例外類別名稱，不回傳原始錯誤訊息。
    # 3. 200／503 都回傳同一個固定格式，前端與 healthcheck 只需要看 status 與 database.ok。

    # 取得用戶端 IP（登入限流用）。
    def _client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            forwarded = request.headers.get("x-real-ip", "").strip()
            if forwarded:
                return forwarded[:64]
        return (request.client.host if request.client else "unknown")[:64]
    # 核心邏輯：只有設定 TRUST_PROXY_HEADERS 時才採信 nginx 加的 X-Real-IP；
    # 否則用 TCP 連線的來源位址，避免任何人自填標頭偽造 IP 來繞過限流。

    # 以 HttpOnly、SameSite=Strict cookie 發出 session 權杖。
    def _set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            cookie_name, token,
            max_age=settings.session_ttl_hours * 3600,
            path="/", secure=settings.session_cookie_secure, httponly=True, samesite="strict",
        )
    # 核心邏輯：HttpOnly 讓頁面腳本讀不到權杖；SameSite=Strict 讓跨站請求不帶 cookie；
    # Secure 依設定開啟（遠端 HTTPS 必開）；到期時間與資料庫的 expires_at 一致。

    # 登入：驗證帳號密碼，成功後以 HttpOnly cookie 發給新的 session，並回傳 CSRF 權杖。
    # 用途：唯一取得登入狀態的方式；沒有註冊與忘記密碼。
    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response, conn: Connection = Depends(get_conn)):
        username = body.username.strip().lower()
        client_ip = _client_ip(request)
        wait = seconds_until_allowed(conn, username, client_ip, settings.login_limits)
        if wait is not None:
            raise ApiError(429, "too_many_attempts", "登入嘗試次數過多，請稍後再試").with_retry_after(wait)

        row = conn.execute(
            "SELECT id, username, role, password_hash, is_active FROM users WHERE username = %s", (username,)
        ).fetchone()
        if row is None:
            burn_password_check(body.password)
            ok = False
        else:
            ok = verify_password(body.password, row["password_hash"]) and row["is_active"]
        record_attempt(conn, username, client_ip, succeeded=bool(ok))
        if not ok:
            conn.commit()  # 失敗紀錄要保留；之後拋出例外會讓連線回滾
            raise ApiError(401, "invalid_credentials", "帳號或密碼錯誤")

        old_token = request.cookies.get(cookie_name)
        if old_token:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = now() WHERE token_sha256 = %s AND revoked_at IS NULL",
                (token_digest(old_token),),
            )
        token, digest = new_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
        conn.execute(
            "INSERT INTO auth_sessions (user_id, token_sha256, expires_at) VALUES (%s, %s, %s)",
            (row["id"], digest, expires_at),
        )
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))
        _set_session_cookie(response, token)
        return {
            "csrf_token": csrf_token_for(token),
            "expires_at": expires_at,
            "user": {"id": row["id"], "username": row["username"], "role": row["role"]},
        }
    # 核心邏輯：
    # 1. 先查限流：帳號或來源 IP 失敗太多次就直接回 429（帶 Retry-After），連密碼都不驗證。
    # 2. 帳號不存在也跑一次雜湊並記錄失敗，回應與耗時和「密碼錯誤」相同，不透露帳號是否存在。
    # 3. 成功時撤銷瀏覽器原本帶來的舊 session（防 session fixation），再發新的 session。
    # 4. session 權杖只放在 HttpOnly cookie，回應本文不含權杖；只回 CSRF 權杖給前端放在記憶體。

    # 登出：撤銷目前的 session 並清除 cookie。需要 CSRF 權杖（由 current_user 檢查）。
    @app.post("/api/auth/logout", status_code=204)
    def logout(user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)):
        conn.execute("UPDATE auth_sessions SET revoked_at = now() WHERE id = %s", (user.session_id,))
        response = Response(status_code=204)
        response.delete_cookie(cookie_name, path="/", secure=settings.session_cookie_secure,
                               httponly=True, samesite="strict")
        return response
    # 核心邏輯：資料庫端撤銷才是真正登出；清 cookie 只是讓瀏覽器不再送出已失效的權杖。

    # 目前登入者資料，並附上 CSRF 權杖。
    # 用途：前端重新整理後記憶體裡的 CSRF 權杖會消失，用這個端點重新取得。
    @app.get("/api/me")
    def me(request: Request, user: CurrentUser = Depends(current_user)):
        return {
            "id": user.id, "username": user.username, "role": user.role,
            "csrf_token": csrf_token_for(request.state.session_token),
        }
    # 核心邏輯：CSRF 權杖由 session 權杖衍生，同一個 session 每次取得的值都相同；
    # 跨站頁面讀不到同源 API 的回應（未開放 CORS），所以拿不到這個值。

    _CONV_COLS = """id, title, scenario, scenario_source, preferences, allergy_status,
                    allergy_avoid, created_at, updated_at"""

    # 對話回應：資料庫欄位＋後端產生的需求摘要（前端只顯示，不自行解讀偏好狀態）。
    def _conv_out(conv: dict) -> dict:
        state = pref.load_state(conv)
        return {**conv, "preferences": state, "summary": pref.summarize(state)}

    @app.get("/api/conversations")
    def list_conversations(user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)):
        rows = conn.execute(
            f"SELECT {_CONV_COLS} FROM conversations WHERE user_id = %s AND hidden_at IS NULL ORDER BY updated_at DESC",
            (user.id,),
        ).fetchall()
        return {"items": rows}

    # 刪除對話（2026-11-07）：只記隱藏時間（0007），不刪對話與請求紀錄（規範 1、8：管理日誌要能追溯）；
    # 隱藏後使用者端一律當作不存在。不是自己的或已刪除的對話回 404，不透露是否存在；重複刪除也回 404。
    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def hide_conversation(
        conversation_id: uuid.UUID, user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)
    ):
        row = conn.execute(
            "UPDATE conversations SET hidden_at = now() WHERE id = %s AND user_id = %s AND hidden_at IS NULL RETURNING id",
            (conversation_id, user.id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "找不到對話")
        return Response(status_code=204)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(
        body: ConversationIn, user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)
    ):
        # rule-v5（K1）：沒給場合時寫佔位值 daily／default（欄位不可為空），代表「未指定」，讀回時視為未知、不當成日常
        scenario, source = (body.scenario, "user") if body.scenario else ("daily", "default")
        return _conv_out(conn.execute(
            f"""INSERT INTO conversations (user_id, title, scenario, scenario_source)
                VALUES (%s, %s, %s, %s) RETURNING {_CONV_COLS}""",
            (user.id, body.title, scenario, source),
        ).fetchone())

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(
        conversation_id: uuid.UUID, user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)
    ):
        # 不是自己的對話一律回 404，不透露該對話是否存在
        conv = conn.execute(
            f"SELECT {_CONV_COLS} FROM conversations WHERE id = %s AND user_id = %s AND hidden_at IS NULL",
            (conversation_id, user.id)
        ).fetchone()
        if conv is None:
            raise ApiError(404, "not_found", "找不到對話")
        conv = _conv_out(conv)
        rows = conn.execute(
            """SELECT r.id AS request_id, r.question, r.status, r.answer, r.started_at, r.completed_at,
                      r.detail ->> 'outcome' AS outcome, r.detail ->> 'intent' AS intent,
                      f.rating AS feedback_rating, r.detail
                 FROM requests r
                 LEFT JOIN feedback f ON f.request_id = r.id AND f.user_id = r.user_id
                WHERE r.conversation_id = %s AND r.user_id = %s ORDER BY r.started_at""",
            (conversation_id, user.id),
        ).fetchall()
        conv["requests"] = [_history_row(conn, r, user.role) for r in rows]
        return conv

    # S9：對話紀錄中的每一則舊回答都依此刻權限重新判斷；有依據已不可用就只回遮蔽說明（日誌不改）。
    def _history_row(conn: Connection, row: dict, role: str) -> dict:
        detail = row.pop("detail") or {}
        verdict = history_access.assess(conn, detail, role)
        if verdict.masked:
            row["answer"] = None
        row["answer_masked"] = verdict.masked
        return row

    @app.put("/api/requests/{request_id}/feedback")
    def put_feedback(
        request_id: uuid.UUID,
        body: FeedbackIn,
        user: CurrentUser = Depends(current_user),
        conn: Connection = Depends(get_conn),
    ):
        option_id = _result_call(results.validate_feedback, conn, request_id, user.id, body.rank, body.reason_codes)
        saved = conn.execute(
            """INSERT INTO feedback (request_id, user_id, rating, option_id, reason_codes, comment)
               VALUES (%s, %s, %s, %s, to_jsonb(%s::text[]), %s)
               ON CONFLICT (request_id, user_id) DO UPDATE
                  SET rating = EXCLUDED.rating, option_id = EXCLUDED.option_id,
                      reason_codes = EXCLUDED.reason_codes, comment = EXCLUDED.comment
               RETURNING request_id, rating, reason_codes, comment, created_at, updated_at""",
            (request_id, user.id, body.rating, option_id, body.reason_codes, body.comment),
        ).fetchone()
        return {**saved, "rank": body.rank}
    # 核心邏輯（S8）：只有擁有者、對已完成的請求才能回饋；指定 rank 時必須是這次實際推薦的方向，
    # 寫入 option_id 讓回饋能對回 request_recommendations 與日誌。同一請求重送會覆蓋。

    # S8：把 results 模組的 ResultError 轉成 API 統一錯誤格式。
    def _result_call(fn, *args):
        try:
            return fn(*args)
        except results.ResultError as exc:
            raise ApiError(exc.status, exc.code, exc.message)

    # S8：單一請求的結果（推薦頁回頭查看、重新整理後還原）。只有擁有者讀得到，引用每次重新判斷可用性。
    @app.get("/api/requests/{request_id}")
    def get_request(request_id: uuid.UUID, user: CurrentUser = Depends(current_user),
                    conn: Connection = Depends(get_conn)):
        return _result_call(results.request_view, conn, request_id, user.id, user.role)

    # progress-hint-v1（2026-11-08）：等待回答時的進度（階段＋這次參考的來源標題），只有發問者本人查得到。
    # 權杖由前端產生、只暫存在記憶體；不存在、過期或不是本人的一律 404（不透露是否存在）。
    @app.get("/api/progress/{token}")
    def get_progress(token: str, user: CurrentUser = Depends(current_user)):
        out = progress.view(token, user.id)
        if out is None:
            raise ApiError(404, "not_found", "找不到這個進度")
        return out

    @app.get("/api/admin/users")
    def admin_users(_admin: CurrentUser = Depends(require_admin), conn: Connection = Depends(get_conn)):
        rows = conn.execute(
            "SELECT id, username, role, is_active, created_at, last_login_at FROM users ORDER BY id"
        ).fetchall()
        return {"items": rows}

    @app.get("/api/admin/requests")
    def admin_requests(
        limit: int = Query(50, ge=1, le=200),
        _admin: CurrentUser = Depends(require_admin),
        conn: Connection = Depends(get_conn),
    ):
        # 管理日誌只給營運所需的中繼資料；問題、回答與偏好原文不在此端點
        rows = conn.execute(
            """SELECT id AS request_id, user_id, conversation_id, status, index_version_id,
                      model_provider, model_name, prompt_version, app_version,
                      input_tokens, output_tokens, latency_ms, retry_count, error_code,
                      started_at, completed_at
                 FROM requests ORDER BY started_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()
        return {"items": rows}

    # S10：管理者限定的監控端點（統計摘要、請求清單、單一請求詳情）；實作與口徑見 monitoring.py
    monitoring.register(app, require_admin=require_admin, get_conn=get_conn, to_api_error=ApiError)

    # -----------------------------------------------------------------------
    # 對話設定與提問（UI 第一版；模型未定，提問只記錄、不產生回答）
    # -----------------------------------------------------------------------
    def _own_conversation(conn: Connection, conversation_id: uuid.UUID, user: CurrentUser) -> dict:
        conv = conn.execute(
            f"SELECT {_CONV_COLS} FROM conversations WHERE id = %s AND user_id = %s AND hidden_at IS NULL FOR UPDATE",
            (conversation_id, user.id),
        ).fetchone()
        if conv is None:
            raise ApiError(404, "not_found", "找不到對話")
        return conv

    # 把偏好狀態寫回對話列：場合與過敏寫回原有欄位（有 CHECK 約束），完整狀態寫入 preferences JSON。
    def _save_state(conn: Connection, conversation_id: uuid.UUID, user: CurrentUser, state: dict,
                    title: str | None = None, set_title: bool = False) -> dict:
        cols = pref.dump_state(state)
        title_sql = ", title = %s" if set_title else ""
        params = [cols["scenario"], cols["scenario_source"], Jsonb(cols["preferences"]),
                  cols["allergy_status"], Jsonb(cols["allergy_avoid"])]
        if set_title:
            params.append(title)
        return conn.execute(
            f"""UPDATE conversations
                   SET scenario = %s, scenario_source = %s, preferences = %s,
                       allergy_status = %s, allergy_avoid = %s{title_sql}
                 WHERE id = %s AND user_id = %s RETURNING {_CONV_COLS}""",
            (*params, conversation_id, user.id),
        ).fetchone()
    # 核心邏輯：狀態只存在這個對話列（不建立跨對話記憶）；SQL 欄位名固定，使用者輸入一律走參數。

    # 修改對話的情境、偏好、過敏狀態（需求設定表單）。只改有帶的欄位：改情境不會清掉原本的排斥條件。
    @app.patch("/api/conversations/{conversation_id}")
    def patch_conversation(
        conversation_id: uuid.UUID, body: ConversationPatch,
        user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn),
    ):
        conv = _own_conversation(conn, conversation_id, user)
        fields = body.model_fields_set
        form: dict = {}
        if body.preferences is not None:
            form.update({k: getattr(body.preferences, k) for k in body.preferences.model_fields_set})
        if body.scenario:
            form["scenario"] = body.scenario
        if "allergy_status" in fields and body.allergy_status:
            form["allergy_status"] = body.allergy_status
        if body.allergy_avoid is not None:
            form["allergy_avoid"] = body.allergy_avoid
        try:
            outcome = pref.apply_form(pref.load_state(conv), form, now=datetime.now(timezone.utc).isoformat())
        except pref.FormInvalid as exc:
            raise ApiError(422, "validation_error", str(exc))
        if not form and "title" not in fields:
            return _conv_out(conv)
        saved = _save_state(conn, conversation_id, user, outcome.state,
                            title=body.title, set_title="title" in fields)
        return {**_conv_out(saved), "applied_changes": outcome.applied}
    # 核心邏輯：
    # 1. 先鎖定並確認是自己的對話（他人一律 404）。
    # 2. 表單與自然語句走同一個偏好模組（pref.apply_form）：只改有帶的欄位、同詞同時要與不要回 422、
    #    過敏狀態為未回答／無已知時不能有需避開項目（規範 6 的四種狀態不混淆）。
    # 3. 情境由使用者設定後 scenario_source 改為 user，畫面不再標「預設」。

    # 提問：請求一開始就寫入 requests（規範 8），接著解析意圖與偏好變更、合併到對話狀態、決定補問。
    # 知識、比較走 S5 問答流程（qa.py）；推薦卡涵蓋需求才用卡，否則也走問答（route-v1，見 _route）。
    @app.post("/api/conversations/{conversation_id}/messages", status_code=201)
    def post_message(
        conversation_id: uuid.UUID, body: MessageIn, request: Request,
        user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn),
    ):
        # progress-hint-v1：前端有帶進度權杖時，處理過程的階段與來源標題暫存起來給 GET /api/progress 查；
        # 沒帶或格式不對就照原本處理（回答內容與流程都不變）
        with progress.tracking(request.headers.get(progress.HEADER), user.id):
            return _post_message(conversation_id, body, request, user, conn)

    def _post_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                      user: CurrentUser, conn: Connection):
        """提問的實際處理（原本 post_message 的內容，未改動）。"""
        started = time.perf_counter()
        conv = _own_conversation(conn, conversation_id, user)
        question = body.question.strip()
        before_cols = {k: conv[k] for k in ("scenario", "scenario_source", "preferences",
                                            "allergy_status", "allergy_avoid")}
        rid = uuid.UUID(_request_id(request))
        conn.execute(
            """INSERT INTO requests (id, user_id, conversation_id, question, preferences_snapshot, app_version)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (rid, user.id, conversation_id, question, Jsonb(before_cols), API_VERSION),
        )
        now = datetime.now(timezone.utc).isoformat()
        # wording-entry-v1（2026-10-30）：明確只要求整理使用者自己文字的指令走獨立入口，在解析與路由之前結束；
        # 不改偏好、不推薦、不檢索、不呼叫模型。「我想…要怎麼說」這類問句不算，照常解析並進檢索。
        wording = answer_tasks.wording_only_request(question)
        if wording is not None:
            return _wording_only_message(conn, rid, started, conversation_id, conv, user, wording)
        before = pref.load_state(conv)
        parse_call, parsed = _run_parser(question)
        if parsed is None:
            return _finish_message(conn, rid, started, conversation_id, status="failed",
                                   error_code="preference_parse_failed", outcome="parse_failed",
                                   detail={"parse_calls": [parse_call]},
                                   body={"message": "需求解析失敗，條件沒有變更，請稍後再試或改用需求設定表單。"})

        turn = pref.process_parsed(before, parsed, request_id=str(rid), now=now, text=question)
        parsed = turn.parsed   # followup-v1：回答上一輪本輪問題時，意圖會接回原本的需求
        merged, clarification, state, limits = turn.merge, turn.clarification, turn.state, turn.limitations
        saved = _save_state(conn, conversation_id, user, state)
        # followup-v2：只說「算了」取消先前任務時，這一輪不推薦也不檢索（hold_answer）
        # route-v3（2026-11-01）：已判成索取非公開資料、範圍外或商品要求 → 在推薦前結束，不算推薦卡、不檢索；
        # 之前卡片先算好、出口沒清掉，商品要求會漏出推薦卡與品牌色號（G01、G03）
        blocked = _blocked_exit(parsed)
        # route-v4（2026-11-01）：正式路由不跑規則推薦、不出推薦卡；規則版只在離線對照腳本裡產生
        # （data/review/<任務>/，規則版 vs 非規則版）。攔截出口的「希望效果」這類非阻斷補問延後（不記問過）。
        # Q5：原推薦卡用的「希望效果」補問只在這則本身是推薦需求時保留（附在回答後）；知識、比較為主的問題不問它。
        deferred = ([q for q in clarification if not q["blocking"]] if blocked else
                    [q for q in clarification if not q["blocking"] and q.get("field") == "effects"
                     and parsed.intent != "recommend"])
        if deferred:
            clarification, state = _defer_nonblocking(clarification, state, deferred)
            saved = _save_state(conn, conversation_id, user, state)
        qa_res = None
        reasked = (turn.followup or {}).get("result") in _REASK_RESULTS
        route = _route(parsed, clarification, hold=turn.hold_answer or reasked,
                       conflicts=state.get("conflicts") or [],
                       after_recommend=_last_need_was_recommend(conn, conversation_id, user.id, rid))
        if route["use_qa"]:
            qa_question = turn.qa_question or question
            if not turn.qa_question and (parsed.intent == "update" or _RE_REFERS_BACK.search(question)):
                # 「改成聚會用」「你剛剛給的第一個方向…」單獨檢索沒有意義：接上這個對話上一則提問
                # route-v2：改場合時把上一則提問裡的舊場合詞換掉（保留「不要太亮」等其他條件）；
                # 指回上一輪時把上一輪交付的方向（推薦卡名稱或問答主張）帶進本輪
                prev = _previous_turn(conn, conversation_id, user.id, rid)
                qa_question, route["carry_over"] = _carry_question(prev, question, parsed, before)
            elif not turn.qa_question:
                # carry-v3（2026-11-07）：先只說條件（例如「聚會時想要溫柔又有氣色」，只記條件、不回答），
                # 再問「那要擦什麼顏色好」——只用本句檢索太籠統，撈到眼妝段落。檢索時把前面純說條件的幾句接在前面；
                # 給模型的問題仍是使用者本句，條件另由 explicit_conditions 交給模型（不改提示）。
                cond_turns = _condition_only_turns(conn, conversation_id, user.id, rid)
                if cond_turns:
                    route["carry_over"] = {"method": "join_condition_turns", "condition_questions": cond_turns,
                                           "retrieval_question": "；".join([*cond_turns, question])}
            route["qa_question"] = qa_question
            # followup-v1：回答「平常怎麼擦」時，檢索用「原問題＋平常擦法」，不是只用「都塗滿」這幾個字
            # ask-once-v1（2026-10-27）：系統這一輪已有要顯示的補問時，問答不再交付模型自己的補問
            qa_res = qa.answer_question(conn, qa_question, user.role, qa_config,
                                        explicit_conditions=_explicit_conditions(state),
                                        system_question=bool(clarification),
                                        retrieval_query=(route.get("carry_over") or {}).get("retrieval_question"),
                                        allergy_status=_allergy_status(state))   # product-ref-v1：過敏時不提商品
            qa.record(conn, rid, qa_res)
            if qa_res.outcome == "answered":
                # Q5（2026-11-01）：原本推薦卡用的補問（希望效果、有精神、平常擦法、模糊詞）改成附在回答後的
                # 非阻斷補問——先給有依據的起點，再問一個關鍵問題（規範 5）。只改這一輪顯示與紀錄的 blocking，
                # 待回答紀錄（pending_turn）不動，使用者下一輪回答仍會接回原本的需求。真衝突不會走到這裡（conflict_first）。
                clarification = [{**q, "blocking": False} for q in clarification]
        outcome, status, error_code, message = _message_outcome(parsed, clarification, None, None, qa_res)
        # exit-assist-v1（2026-11-02）：商品／範圍外出口仍維持出口狀態，另外附上範圍內可以回答的部分
        assisted = False
        if blocked and outcome in ("product_not_enabled", "outside_scope"):
            keys = [m["key"] for m in (color_ref.lookup(question) if color_ref is not None else []) if m["shades"]]
            assist_q = exit_assist.in_scope_question(question, outcome, keys)
            route["exit_assist"] = {"version": exit_assist.ASSIST_VERSION, "question": assist_q}
            if assist_q:
                # product-ref-v1：這是被攔下的購買要求的代答，不提具體商品（只攔推薦購買，11-01 商品界線）
                # assist-brief-v1（2026-11-08，G03「論述有點太多」）：代答只交付直接回答，不列背景段與模型限制
                assist_res = qa.answer_question(conn, assist_q, user.role, qa_config,
                                                explicit_conditions=_explicit_conditions(state), system_question=True,
                                                allergy_status=_allergy_status(state), allow_product_reference=False,
                                                assist_brief=True)
                if assist_q == exit_assist.MEDICAL_ASSIST_QUESTION and assist_res.answer:
                    # exit-assist-v3（O02）：嘴唇紅腫脫皮的代答不列去角質／磨砂（會更刺激），拿掉的行記進紀錄
                    assist_res.answer, gone = exit_assist.drop_irritating(assist_res.answer)
                    route["exit_assist"]["dropped_irritating"] = gone
                    if not assist_res.answer.strip():
                        assist_res.outcome = "insufficient_evidence"
                qa.record(conn, rid, assist_res)
                route["exit_assist"]["outcome"] = assist_res.outcome
                if assist_res.outcome == "answered":
                    qa_res, assisted = assist_res, True
        # insufficient-assist-v1（2026-11-03，P08）：要研究數字而證據不足時，缺什麼照舊列在限制，
        # 另外拿掉數字要求再問一次，附上有來源的一般建議（狀態仍是證據不足）
        general_answer = None
        assist_q = (exit_assist.insufficient_question(route.get("qa_question") or question)
                    if not blocked and outcome == "answer_insufficient_evidence" and qa_res is not None else None)
        if assist_q:
            route["insufficient_assist"] = {"version": exit_assist.INSUFFICIENT_ASSIST_VERSION, "question": assist_q}
            # assist-brief-v1（2026-11-08，P08）：一般建議那次問答的限制是另一個問題的缺口，不列進原題的限制
            assist_res = qa.answer_question(conn, assist_q, user.role, qa_config,
                                            explicit_conditions=_explicit_conditions(state), system_question=True,
                                            allergy_status=_allergy_status(state), assist_brief=True)
            # 第一次問答的召回已寫進 retrieval_hits；第二次不再寫（主鍵會撞號），兩次的完整日誌都留在 detail
            qa.record(conn, rid, assist_res, write_hits=False)
            route["insufficient_assist"].update(outcome=assist_res.outcome, first_qa=qa_res.log)
            if assist_res.outcome == "answered" and assist_res.answer:
                # insufficient-lead-v1（2026-11-05，P08）：第一條「缺什麼」移到回答最前面，限制區不再重複
                general_answer, rest, lead = exit_assist.insufficient_general_answer(qa_res.limitations,
                                                                                    assist_res.answer)
                route["insufficient_assist"]["lead"] = {"version": exit_assist.INSUFFICIENT_LEAD_VERSION, "text": lead}
                limits = list(dict.fromkeys(limits + rest))                 # 其餘「缺什麼」照舊列在限制
                qa_res = assist_res                                         # 引用改看一般建議實際用到的片段
                message = f"{message}{exit_assist.INSUFFICIENT_ASSIST_NOTE}"
        # notice-v1（2026-11-02）：注入字樣加警語、非唇彩對象點名；只改說明句，不改路由與回答
        message, notice_log = message_notices.decorate(message, question, outcome, assisted=assisted)
        answer = None
        if qa_res is not None:
            limits = list(dict.fromkeys(limits + qa_res.limitations))
            answer = general_answer or qa_res.answer
        # color-ref-v1（2026-10-26）：問題提到清冊裡的色名時，由程式附上色名參照（不經模型、不是推薦）
        color_ref_log = None
        # route-v3：攔截出口（商品、範圍外、非公開資料）不附色名參照；正常問答的參照政策不變
        # 原文段落出口（sources_only）也不附：那不是回答，只是原文
        # exit-assist-v1：商品出口附了範圍內回答時，也附色名參照（給其他品牌色碼當參考可以，G03）
        if color_ref is not None and (not blocked or (assisted and outcome == "product_not_enabled"))                 and not (qa_res is not None and qa_res.outcome == "sources_only"):
            # color-ref-v6（2026-11-08）：問題提到的色名清冊裡有色號就附，不看有沒有問色號；
            # general_advice 只在日誌記原因（color-ref-v5 起）
            ref_text, color_ref_log = color_ref.block(question, general_advice=general_answer is not None)
            if ref_text:
                # color-ref-v2（W02）：附了色塊參照，就不能再說「資料沒有 HEX」；改寫成固定句，交付文字同步替換
                limits, rewritten = color_reference.reconcile_limitations(limits, color_ref_log)
                for r in rewritten:
                    answer = answer.replace(r["before"], r["after"]) if answer else answer
                color_ref_log["limitations_rewritten"] = rewritten
                answer = f"{answer}\n\n{ref_text}" if answer else ref_text
        detail = {
            "outcome": outcome, "message": message,  # S8：回頭查看結果時顯示同一段說明（含失敗原因）
            "notices": notice_log or None,
            "intent": parsed.intent, "secondary_intents": parsed.secondary_intents,
            "schema_version": pref.PREF_SCHEMA_VERSION, "parse_calls": [parse_call],
            "preferences_before": before, "applied_changes": merged.applied,
            "new_conflicts": merged.new_conflicts, "resolved": merged.resolved,
            "clarification": clarification, "limitations": limits,
            # followup-v1：這則訊息若在回答上一輪的本輪問題，記下怎麼接續（確認／否認／記下平常擦法／放棄）
            "followup": turn.followup, "qa_question": turn.qa_question,
            "route": route, "color_reference": color_ref_log,
        }
        if qa_res is not None:
            detail["qa"] = qa_res.log
            # wording-entry-v1：問答不再有原話複述出口，交付的回答一律來自生成（或關係證據的程式組句）
            detail["answer_source"] = (("source_excerpts" if qa_res.outcome == "sources_only" else "generation_model")
                                       if answer else None)
        return _finish_message(
            conn, rid, started, conversation_id, status=status, error_code=error_code, outcome=outcome,
            detail=detail, snapshot=pref.dump_state(state), answer=answer,
            body={"message": message, "intent": parsed.intent, "secondary_intents": parsed.secondary_intents,
                  "applied_changes": merged.applied, "new_conflicts": merged.new_conflicts,
                  "resolved": merged.resolved, "clarification": clarification,
                  "summary": pref.summarize(state), "preferences": _conv_out(saved)["preferences"],
                  # S8：對外只回清理過的推薦與問答引用（白名單）；S9：回傳前以「此刻」角色再判斷一次，
                  # 處理中途來源被停用／改權限、或使用者被降級，就整則遮蔽，不回傳已失效的依據
                  **results.public_result(conn, detail, answer, history_access.current_role(conn, user.id))})
    # 核心邏輯：
    # 1. requests 先寫入（status=started），之後任何步驟失敗都留得下紀錄；request_id 等於 X-Request-ID。
    # 2. 解析只產生提案，pref.apply_parse 才寫入狀態：沒提到的欄位不動、衝突不套用、未回答過敏不變成無已知。
    # 3. 推薦／修改／比較才補問（最多 2 題）；純知識問答不被偏好衝突卡住。
    # 4. 每一次解析呼叫（目前是規則版）都寫進 detail.parse_calls：版本、耗時、錯誤、輸出；token 記 null（未知）。
    # 5. route-v4（2026-11-01）：正式路由不跑規則推薦（S7）、不出推薦卡；route["recommendation"] 記 skipped_by_route。
    #    規則推薦卡只在離線對照腳本裡和非規則版並排比較。
    # 6. S5：攔截出口、真衝突、只改條件以外一律走 qa.answer_question：核准檢索 → 生成模型 → 引用驗證。
    #    檢索、送入模型的上下文、每次模型呼叫（token、耗時、重試、錯誤）、原始輸出都寫進 detail.qa 與
    #    requests 的模型欄位；失敗時 requests.status = failed，回應帶 request_id 與可理解的提示。

    # 執行偏好解析並產生一筆解析呼叫紀錄；解析器拋錯時回傳 (紀錄, None)，不讓整個請求消失。
    def _run_parser(question: str) -> tuple[dict, pref.ParseResult | None]:
        t0 = time.perf_counter()
        parsed, error = None, None
        try:
            parsed = _PARSER.parse(question)
        except Exception as exc:  # 解析器錯誤要記錄，不外洩細節
            log.exception("preference parse failed")
            error = type(exc).__name__
        call = {
            "stage": "preference_parse", "parser": _PARSER.name, "parser_version": _PARSER.version,
            "vocab_version": pref.VOCAB["version"], "model_provider": None, "model_name": None,
            "input_tokens": None, "output_tokens": None, "retry_count": 0,
            "latency_ms": int((time.perf_counter() - t0) * 1000), "error": error,
            "output": parsed.to_dict() if parsed else None,
        }
        return call, parsed
    # 核心邏輯：規則版不呼叫外部模型，所以 model_* 與 token 都是 null（未知，不記 0）；
    # 換成模型解析後，同一個紀錄格式填入實際的模型、token、重試次數。

    # 把請求標記為完成並組出回應本文。
    def _finish_message(conn: Connection, rid: uuid.UUID, started: float, conversation_id: uuid.UUID, *,
                        status: str, error_code: str | None, outcome: str, detail: dict, body: dict,
                        snapshot: dict | None = None, answer: str | None = None) -> dict:
        latency = int((time.perf_counter() - started) * 1000)
        error_detail = ({"stage": "generation", "decision": "DEC-11"} if error_code == "model_not_configured"
                        else {"stage": "recommendation"} if error_code == "recommendation_evidence_changed"
                        else {"stage": _QA_ERROR_STAGE[error_code]} if error_code in _QA_ERROR_STAGE
                        else {"stage": "preference_parse"} if error_code else None)
        conn.execute(
            """UPDATE requests SET status = %s, error_code = %s, error_detail = %s, detail = %s,
                      preferences_snapshot = COALESCE(%s, preferences_snapshot), answer = %s,
                      completed_at = now(), latency_ms = %s
                WHERE id = %s""",
            (status, error_code, Jsonb(error_detail) if error_detail else None, Jsonb(detail),
             Jsonb(snapshot) if snapshot is not None else None, answer, latency, rid),
        )
        conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,))
        return {"request_id": str(rid), "status": outcome, "error_code": error_code, **body}
    # 核心邏輯：requests.status 只用資料庫允許的 succeeded／failed；更細的結果（需補問、已更新條件…）
    # 放在 detail.outcome 並回傳給前端。

    def _wording_only_message(conn: Connection, rid: uuid.UUID, started: float, conversation_id: uuid.UUID,
                              conv: dict, user: CurrentUser, wording: str) -> dict:
        """wording-entry-v1：完成一則「只要求整理使用者自己文字」的請求並回傳 API 回應。

        這條入口不解析偏好、不推薦、不檢索、不呼叫模型，所以：
        - outcome 用獨立的 wording_only（不是 answered），message 明說沒有查資料、沒有引用來源，
          不會再出現「每一句都標了來源編號」卻沒有引用的矛盾；
        - detail 記下入口版本、整理後的文字、route.reason＝wording_only、answer_source＝user_wording，
          以及「檢索未執行」的原因（規範 8：沒做的步驟也要留下紀錄，不留空白）；
        - 對話偏好不變（要整理的文字不當成新的需求條件），回應裡照樣附上目前的條件摘要，前端格式一致。
        """
        answer = answer_tasks.wording_answer(wording)
        message = _OUTCOME_MESSAGES["wording_only"]
        detail = {
            "outcome": "wording_only", "message": message, "intent": "wording", "secondary_intents": [],
            "parse_calls": [], "parse": {"status": "skipped", "reason": "wording_only_request"},
            "applied_changes": [], "new_conflicts": [], "resolved": [], "clarification": [], "limitations": [],
            "route": {"version": ROUTE_VERSION, "use_qa": False, "reason": "wording_only", "qa_question": None},
            "retrieval": {"status": "skipped", "reason": "wording_only_request"},
            "wording": {"version": answer_tasks.WORDING_VERSION, "text": wording, "source": "user_text"},
            "answer_source": "user_wording",
        }
        conv_out = _conv_out(conv)
        return _finish_message(
            conn, rid, started, conversation_id, status="succeeded", error_code=None, outcome="wording_only",
            detail=detail, answer=answer,
            body={"message": message, "intent": "wording", "secondary_intents": [], "applied_changes": [],
                  "new_conflicts": [], "resolved": [], "clarification": [],
                  "summary": conv_out["summary"], "preferences": conv_out["preferences"],
                  **results.public_result(conn, detail, answer, history_access.current_role(conn, user.id))})


    # -----------------------------------------------------------------------
    # 來源瀏覽：只列可用片段（已核准、已發布、啟用、有效期間內），並依登入角色過濾 visibility
    # -----------------------------------------------------------------------
    def _visible(user: CurrentUser) -> list[str]:
        return ["all", "admin"] if user.role == "admin" else ["all"]

    @app.get("/api/sources/documents")
    def source_documents(user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn)):
        rows = conn.execute(
            """SELECT doc_key, title, version_label, visibility, count(*) AS chunks,
                      bibliography ->> 'url' AS url, bibliography ->> 'year' AS year
                 FROM usable_chunks WHERE visibility = ANY(%s)
                GROUP BY doc_key, title, version_label, visibility, bibliography ORDER BY doc_key""",
            (_visible(user),),
        ).fetchall()
        return {"items": rows}

    @app.get("/api/sources/chunks")
    def source_chunks(
        doc: str | None = Query(None, pattern=r"^[A-Za-z0-9_-]+$"),
        q: str | None = Query(None, max_length=100),
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50),
        user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn),
    ):
        where, params = ["visibility = ANY(%s)"], [_visible(user)]
        if doc:
            where.append("doc_key = %s")
            params.append(doc)
        if q and q.strip():
            # 關鍵字瀏覽（不是語意檢索）；% 與 _ 視為一般字元
            pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(quote_text ILIKE %s OR section ILIKE %s)")
            params += [pattern, pattern]
        cond = " AND ".join(where)
        total = conn.execute(f"SELECT count(*) AS n FROM usable_chunks WHERE {cond}", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT chunk_key, doc_key, title, version_label, page_start, page_end, html_locator, section,
                       quote_text, attributes -> 'citation' AS citation, attributes -> 'cited_works' AS cited_works,
                       attributes -> 'data_note' AS data_note
                  FROM usable_chunks WHERE {cond} ORDER BY doc_key, ordinal OFFSET %s LIMIT %s""",
            (*params, offset, limit),
        ).fetchall()
        for r in rows:   # sources-cited-v1：轉引文獻一律回傳字串清單
            r["cited_works"] = cited_works_list(r.get("cited_works"))
        return {"total": total, "items": rows}

    # S8：查看推薦引用的片段原文與定位。後端以 usable_chunks＋角色重新判斷，看不到一律 404。
    @app.get("/api/sources/citation")
    def source_citation(
        doc: str = Query(..., pattern=r"^[A-Za-z0-9_-]+$", max_length=64),
        version: str = Query(..., min_length=1, max_length=40),
        # 修復 D 的支持單位片段鍵是 <chunk>#r<n>（例如 PR003_h0108#r4），所以允許 #
        chunk: str = Query(..., pattern=r"^[A-Za-z0-9_.:#-]+$", max_length=200),
        user: CurrentUser = Depends(current_user), conn: Connection = Depends(get_conn),
    ):
        return _result_call(results.citation_text, conn, doc, version, chunk, user.role)

    # -----------------------------------------------------------------------
    # 管理者審核（取代 CSV；每筆都要人工決定與理由，沒有批次核准）
    # -----------------------------------------------------------------------
    @app.get("/api/admin/review/documents")
    def review_documents(_admin: CurrentUser = Depends(require_admin), conn: Connection = Depends(get_conn)):
        return {"items": ingest.status(conn)}

    @app.get("/api/admin/review/chunks")
    def review_chunks(
        doc: str = Query(..., pattern=r"^[A-Za-z0-9_-]+$"),
        version: str | None = Query(None, max_length=40),
        status: Literal["pending", "approved", "rejected"] = "pending",
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50),
        _admin: CurrentUser = Depends(require_admin), conn: Connection = Depends(get_conn),
    ):
        # 先確定要列的是哪個版本（未指定＝最新），清單與版本資訊都以這個版本為準，避免兩者不一致
        info = ingest.version_info(conn, doc, version)
        if info is None:
            raise ApiError(404, "not_found", "找不到文件版本")
        rows = ingest.review_rows(conn, doc, info["version_label"], [status])
        page = rows[offset:offset + limit]
        meta = {r["chunk_key"]: r for r in conn.execute(
            """SELECT c.id, c.chunk_key, c.reviewed_at, c.review_note, u.username AS reviewed_by
                 FROM chunks c LEFT JOIN users u ON u.id = c.reviewed_by
                WHERE c.document_version_id = %s AND c.chunk_key = ANY(%s)""",
            (info["version_id"], [r["chunk_key"] for r in page]),
        )}
        items = [{**{k: r[k] for k in ("doc_key", "chunk_key", "version_label", "pages", "html_locator", "section",
                                        "review_status", "prior_decision", "content_sha256", "quote_text")},
                  "flags": (r["flags"] or "").split(), "version_status": info["status"],
                  **{k: meta[r["chunk_key"]][k] for k in ("id", "reviewed_by", "reviewed_at", "review_note")}}
                 for r in page]
        return {"total": len(rows), "items": items, "flag_labels": ingest.FLAG_LABELS,
                "version": _public_version(info)}
    # 核心邏輯：回應附上這個版本的狀態、是否最新、最新草稿與目前發布版本，審核頁據此顯示提示；
    # 每筆片段也帶 doc_key／version_label，送出審核時原樣帶回給後端比對。

    @app.post("/api/admin/review/chunks/{chunk_id}")
    def review_chunk(
        chunk_id: int, body: ReviewIn,
        admin: CurrentUser = Depends(require_admin), conn: Connection = Depends(get_conn),
    ):
        try:
            with conn.transaction():
                # 與匯入同一把鎖：比對期間不會有新版本匯入，「是否最新」的判斷到寫入為止都成立
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ingest:" + body.doc_key,))
                _check_review_target(conn, chunk_id, body)
                outcome = conn.execute(
                    "SELECT review_chunk(%s, %s, %s, %s, %s) AS r",
                    (chunk_id, admin.id, body.decision, body.reason.strip(), body.content_sha256),
                ).fetchone()["r"]
        except psycopg.errors.NoDataFound:
            raise ApiError(404, "not_found", "找不到片段")
        except psycopg.errors.CheckViolation as exc:
            raise ApiError(409, "review_conflict", _review_conflict_message(str(exc)))
        return {"chunk_id": chunk_id, "result": outcome, "doc_key": body.doc_key,
                "version_label": body.version_label, "chunk_key": body.chunk_key}
    # 核心邏輯：審核人一律是目前登入的管理者（不是請求欄位）；送出的文件／版本／片段編號必須是 chunk_id
    # 實際所屬（否則 409 review_target_mismatch）；核准非最新版本須明確確認（否則 409
    # review_not_latest_version）；內容指紋不符、已發布版本不允許的轉換（例如 rejected 再放行）由資料庫
    # 函式擋下並回 409；每次變更都寫入 chunk_review_events。以上任一檢查失敗，整筆不寫入。

    @app.get("/api/admin/review/chunks/{chunk_id}/events")
    def review_events(chunk_id: int, _admin: CurrentUser = Depends(require_admin),
                      conn: Connection = Depends(get_conn)):
        rows = conn.execute(
            """SELECT e.from_status, e.to_status, e.reason, e.created_at, u.username AS reviewer
                 FROM chunk_review_events e JOIN users u ON u.id = e.reviewer_id
                WHERE e.chunk_id = %s ORDER BY e.created_at""",
            (chunk_id,),
        ).fetchall()
        return {"items": rows}

    @app.post("/api/admin/review/publish")
    def review_publish(body: PublishIn, admin: CurrentUser = Depends(require_admin),
                       conn: Connection = Depends(get_conn)):
        try:
            return release.switch(conn, body.doc_key, body.version_label, actor=f"admin:{admin.username}",
                                  accept_candidate_impact=body.accept_candidate_impact)
        except release.ReleaseError as exc:
            conn.commit()  # 保留 refused 事件；切換本身已在交易內回滾，舊版本不變
            raise ApiError(409, "publish_refused", str(exc))
    # 核心邏輯（S9）：發布走 release.switch——同一交易內重新驗證、切換版本（有 active 索引時須先在
    # CLI prepare 新索引，這裡會拒絕）；失敗時舊版本與索引不變，並留下 refused 事件。

    # S9：一份文件的發布／停用事件與版本狀態（管理者限定）。
    @app.get("/api/admin/documents/{doc_key}/releases")
    def document_releases(doc_key: str, _admin: CurrentUser = Depends(require_admin),
                          conn: Connection = Depends(get_conn)):
        try:
            return {"versions": release.versions(conn, doc_key), "events": release.history(conn, doc_key)}
        except release.ReleaseError:
            raise ApiError(404, "not_found", "找不到文件")

    # S9：管理者追溯一個請求當時用的版本、索引、片段指紋與此刻狀態（不含問題與回答原文）。
    @app.get("/api/admin/requests/{request_id}/trace")
    def request_trace(request_id: uuid.UUID, _admin: CurrentUser = Depends(require_admin),
                      conn: Connection = Depends(get_conn)):
        trace = history_access.admin_trace(conn, request_id)
        if trace is None:
            raise ApiError(404, "not_found", "找不到請求")
        return trace

    @app.put("/api/admin/documents/{doc_key}/visibility")
    def set_visibility(doc_key: str, body: VisibilityIn, admin: CurrentUser = Depends(require_admin),
                       conn: Connection = Depends(get_conn)):
        try:
            out = release.set_visibility(conn, doc_key, body.visibility, actor=f"admin:{admin.username}")
        except release.ReleaseError:
            raise ApiError(404, "not_found", "找不到文件")
        return {"doc_key": doc_key, "visibility": out["after"]["visibility"],
                "candidate_impact": out["candidate_impact"]}
    # 核心邏輯（S9）：可見範圍變更寫入發布事件；改成 admin 後一般使用者的檢索、推薦證據與歷史回答立即受限。

    return app


# 審核頁要顯示的版本資訊（不含內部 id）。
def _public_version(info: dict) -> dict:
    return {k: info[k] for k in ("doc_key", "version_label", "status", "is_active", "is_latest",
                                 "latest_label", "latest_draft_label", "published_label")}


# 比對送出的審核對象與 chunk_id 實際所屬是否一致，並檢查「非最新版本」的核准是否經過確認。
# 必須在呼叫 review_chunk() 之前、同一個交易內執行；不符時丟 ApiError，交易回滾、不寫入任何審核紀錄。
def _check_review_target(conn: Connection, chunk_id: int, body: ReviewIn) -> None:
    actual = conn.execute(
        """SELECT d.doc_key, dv.version_label, c.chunk_key
             FROM chunks c JOIN document_versions dv ON dv.id = c.document_version_id
             JOIN documents d ON d.id = dv.document_id WHERE c.id = %s""",
        (chunk_id,),
    ).fetchone()
    if actual is None:
        raise ApiError(404, "not_found", "找不到片段")
    if (actual["doc_key"], actual["version_label"], actual["chunk_key"]) != (
            body.doc_key, body.version_label, body.chunk_key):
        raise ApiError(409, "review_target_mismatch",
                       "送出的文件、版本或片段編號與實際審核對象不一致，請重新整理後再審")
    info = ingest.version_info(conn, body.doc_key, body.version_label)
    if body.decision == "approve" and not info["is_latest"] and not body.confirm_not_latest:
        raise ApiError(409, "review_not_latest_version",
                       f"{body.doc_key} {body.version_label} 不是最新版本（最新為 {info['latest_label']}），"
                       "請重新整理確認後再決定是否核准")


# 把資料庫守門觸發器的英文訊息轉成給審核人看的中文說明；其餘原因不外洩細節。
def _review_conflict_message(raw: str) -> str:
    if "content changed" in raw:
        return "片段內容和你看到的版本不同，請重新整理後再審"
    if "cannot go from" in raw:
        return "已發布版本的片段不能從「退回」改回「核准」，退役版本則不能再變更"
    if "not an active admin" in raw:
        return "審核人必須是啟用中的管理者"
    return "審核狀態無法變更"


# 修復 D：交給 repair 問答管線的「使用者明說的條件」（contract-v1 explicit_conditions 格式）——
# 單值欄位只取狀態為 user（使用者自己說或表單填的）；清單欄位（喜歡／避免的顏色、質地、效果）只取有來源、
# 不是舊資料轉入的項目。保留原話（raw），不把「小麥色」等自述翻成底調；其餘一律不送（保持未知）。
def _allergy_status(state: dict) -> str | None:
    """使用者過敏狀態（unanswered／none_known／has_known／unsure）；product-ref-v1 用來決定能不能提具體商品。"""
    item = state.get("allergy")
    return item.get("status") if isinstance(item, dict) else None


def _explicit_conditions(state: dict) -> list[dict]:
    out = []
    for field_name, item in state.items():
        if field_name == "allergy":
            continue
        if isinstance(item, dict) and item.get("status") == "user":
            out.append({"field": field_name, "value": item.get("value"), "values": item.get("values"),
                        "label": item.get("label"), "raw": item.get("raw"), "source": item.get("source")})
        elif field_name in pref.LIST_FIELDS and isinstance(item, list):
            out += [{"field": field_name, "value": x.get("value"), "values": None, "label": x.get("label"),
                     "raw": x.get("raw"), "source": x.get("source")}
                    for x in item if isinstance(x, dict) and x.get("source") not in (None, "legacy")]
    # avoid-v1：還沒確認意思的說法（「不要太亮」）也是使用者說過的限制，標成 pending_ambiguity（值是模糊類型，不推定意思）
    out += [{"field": "pending_ambiguity", "value": a.get("kind"), "values": None, "label": a.get("label"),
             "raw": a.get("raw"), "source": "chat"}
            for a in state.get("ambiguities") or [] if isinstance(a, dict) and a.get("kind")]
    # rule-v11：對象不在詞表的否定說法（「不會太搶戲」），照原話交給模型；source＝chat，不進選段片語
    out += [{"field": "unclassified_avoid", "value": None, "values": None, "label": u.get("raw"),
             "raw": u.get("raw"), "source": "chat"}
            for u in state.get("unclassified_avoid") or [] if isinstance(u, dict) and u.get("raw")]
    return out


# 偏好解析器：生成模型未定（DEC-11），先用可重現的規則版；換模型版時輸出格式相同（pref.ParseResult）。
_PARSER = pref.RuleParser()

_OUTCOME_MESSAGES = {
    "needs_clarification": "需求裡有需要先確認的地方，請回答下面的問題；在確認前不會自行選邊。",
    "preferences_updated": "已更新這個對話的需求條件，其他條件維持不變。",
    # route-v4：補上「對象不是唇彩」（第 4 類，O01）
    # notice-v3（2026-11-02）：語氣委婉，不列清單、不說「可以直接問」
    "outside_scope": "這部分超出我能協助的範圍；我能協助的是唇彩的色系與妝感。",
    # route-v3（2026-11-01）：商品要求不再給推薦卡，直接說明功能未開放
    # notice-v2（2026-11-02，I01）：只寫「商品功能未開放」
    "product_not_enabled": "商品功能未開放。",
    # route-v3：索取其他使用者的對話或非公開的系統設定；權限只依登入帳號，訊息中的身分或授權說法不會改變權限
    # notice-v2（2026-11-02，I03）：不告訴使用者權限怎麼判定
    "restricted_request": "這個要求涉及其他使用者的對話或非公開的系統設定，無法在對話中提供。",
    "not_answered": "需求條件已記錄。生成與嵌入模型尚未啟用（DEC-11），本次沒有產生推薦、回答或引用。",
    "recommended": "以下是依你的條件、由規則篩選排序的方向（只引用經核對且仍有效的來源；未經生成模型改寫）。",
    "no_match": "沒有同時符合全部排除條件的已核對方向；不會自行放寬條件。",
    # rec-v2：只因來源的底調條件不符而沒有方向（不是排除條件，不問放寬）
    "no_match_undertone": "目前已核對的方向都有來源的底調條件，沒有適用於你底調的方向；這只表示來源沒有涵蓋，不表示這些顏色不能用。",
    # rec-v2：底調未知或不確定，依來源的底調條件分開列出並追問
    "conditional": "你的底調還沒確定，以下方向依來源的底調條件分開列出（只引用經核對且仍有效的來源；未經生成模型改寫）；"
                   "確定底調後會依你的底調篩選。",
    "insufficient_evidence": "目前沒有可以引用有效來源的推薦方向，這次不提供推薦，也不會用模型補知識。",
    "recommendation_error": "推薦依據在處理期間變動，這次不回傳推薦，請稍後再試。",
    "answered": "以下回答只根據已核准的來源片段整理，每一句都標了來源編號；引用編號已核對在本次許可的來源內，"
                "但句子是否完全被來源支持尚未經人工核對。",
    # 2026-11-01：生成模型連不上時列出已核准原文段落，不是整理過的答案
    "sources_only": "AI 暫時無法回答，以下是找到的相關原文段落，請直接閱讀；這不是整理過的答案。",
    # notice-v3（2026-11-02，P08）：指出缺少的資料在哪裡看，不只說不回答
    "answer_insufficient_evidence": "目前的資料不足以完整回答這個問題，缺少的部分列在下方；不會用模型記憶補充。",
    # wording-entry-v1（2026-10-30）：明確的整理原話指令；沒有來源，不能沿用 answered 的「每句都有來源」說明
    "wording_only": "以下只依你提供的文字整理：這次沒有查資料、沒有引用來源，也沒有新增色彩分類。",
    # 防呆：問答回 answered 卻沒有任何引用時，不宣稱每句都有來源（正常流程不應發生）
    "answered_without_citations": "這次的回答沒有附上來源引用，不能當成已核准來源的內容；"
                                  "請以參考依據為準，必要時再問一次。",
}

# S5 問答失敗時給使用者的說明（依錯誤代碼）；都會附上 request_id 方便回報。
_QA_ERROR_MESSAGES = {
    "retrieval_not_configured": "檢索用的嵌入模型尚未設定（DEC-11），這次無法查找來源，沒有產生回答。",
    "retrieval_not_ready": "來源索引尚未就緒，這次無法查找來源，沒有產生回答。",
    "retrieval_failed": "查找來源時發生錯誤，這次沒有產生回答，請稍後再試。",
    "model_not_configured": "生成模型尚未設定（DEC-11），找到的來源沒有被整理成回答。",
    "model_timeout": "生成模型回應逾時（已依上限重試），這次沒有產生回答，請稍後再試。",
    "model_rate_limited": "生成模型暫時忙碌（已依上限重試），這次沒有產生回答，請稍後再試。",
    "model_server_error": "生成模型暫時無法使用（已依上限重試），這次沒有產生回答，請稍後再試。",
    "model_unreachable": "無法連線到生成模型（已依上限重試），這次沒有產生回答，請稍後再試。",
    "model_request_rejected": "生成模型拒絕了這次請求，這次沒有產生回答。",
    "model_bad_response": "生成模型的回應格式錯誤，這次沒有產生回答。",
    "model_unexpected_error": "呼叫生成模型時發生錯誤，這次沒有產生回答。",
    "model_output_invalid": "模型的回答沒有通過引用檢查（缺引用或引用了不允許的來源），為避免提供沒有依據的內容，這次不顯示。",
    # 2026-10-26：repair 管線把模型輸出失敗分成三種，不再統稱引用檢查失敗
    "model_output_truncated": "模型的回答寫到長度上限被截斷、內容不完整，為避免提供半截的內容，這次不顯示。",
    "model_output_malformed": "模型的回答格式不完整或不符規定，無法核對來源，這次不顯示。",
    "model_citation_invalid": "模型的回答引用了這次沒有提供的來源，為避免提供沒有依據的內容，這次不顯示。",
    "evidence_changed": "引用的來源在處理期間被下架或變更，這次不顯示回答，請再問一次。",
    # 修復 D：repair 問答管線（QA_PIPELINE=repair）
    "pipeline_not_configured": "問答流程設定不完整（候選池大小或選段／回答模組未就緒），這次沒有產生回答。",
    "pipeline_contract_violation": "問答流程的中間結果沒有通過來源一致性檢查，為避免提供沒有依據的內容，這次不顯示。",
    "pipeline_module_error": "問答流程的檢索或回答模組發生錯誤，這次沒有產生回答。",
    "selection_output_invalid": "判讀來源是否適用的步驟輸出格式錯誤，為避免用到未判讀的來源，這次沒有產生回答。",
    "selection_failed": "判讀來源是否適用的步驟失敗（模型錯誤或沒有可判讀的來源），為避免用到未判讀的來源，這次沒有產生回答。",
}
_QA_ERROR_STAGE = {code: ("retrieval" if code.startswith("retrieval") else
                          "pipeline" if code.startswith("pipeline") else
                          "selection" if code.startswith("selection") else
                          "final_revalidate" if code == "evidence_changed" else "generation")
                   for code in _QA_ERROR_MESSAGES}


# route-v1（使用者 2026-10-26）：決定這一輪用問答回答，還是用推薦卡（或只補問／只更新條件）。
# route-v2（2026-10-26，方案 B）：推薦卡只在 status＝recommended 且主要方向沒有 unmet 條件時直接出卡；
# conditional（底調未定、依來源條件分列）或主要方向有任何沒符合的條件 → 改走問答。
# route-v3（2026-11-01）：索取非公開資料另有出口；商品／範圍外／非公開資料在推薦前就結束（見 _blocked_exit）。
# route-v4（2026-11-01，方案第二版）：正式路由預設走問答（RAG），不跑規則推薦、不出推薦卡；
# 規則推薦卡改在離線對照腳本裡和非規則版比較。
ROUTE_VERSION = "route-v4"
_RE_REFERS_BACK = re.compile(r"剛剛|剛才|上面|前面|你說的|你給的|第一個|第二個")   # 指向上一輪回答的說法
# 待回答的問題沒得到可用回答、這輪只再問一次（followup-v2）：起點在第一輪已給過，不再檢索
_REASK_RESULTS = frozenset({"type_mismatch", "vague", "unclear"})


# 已被規則判成不能回答的需求時，回傳攔截出口名稱；否則 None。post_message 用它決定跳過推薦與色名參照。
def _blocked_exit(parsed: pref.ParseResult) -> str | None:
    intents = {parsed.intent, *parsed.secondary_intents}
    if "restricted_request" in intents:
        return "restricted_request"
    if intents & {"outside_scope", "product_request"}:
        return "outside_scope_or_product"
    return None
# 核心邏輯：只看意圖（由後端規則判定），不看訊息裡自稱的身分；索取非公開資料優先於範圍外與商品。


# route-v4：一則請求是不是推薦需求（使用者在找色系方向）。取代 route-v3 的「這個對話跑過規則推薦」：
# 推薦需求之後只改條件（「改成聚會用」），要用新條件重新回答，不能只記條件。
def _was_recommend_need(prev: dict | None) -> bool:
    d = (prev or {}).get("detail") or {}
    return "recommend" in {d.get("intent"), *(d.get("secondary_intents") or [])}


# 往前找這個對話最近一則「不是純修改條件」的請求，看它是不是推薦需求；連續幾則都在改條件
# （「改成聚會用」→ 回答補問「不要鮮豔」）時，仍接回最初的推薦需求。
def _last_need_was_recommend(conn: Connection, conversation_id: uuid.UUID, user_id: int, rid: uuid.UUID) -> bool:
    rows = conn.execute(
        """SELECT detail FROM requests WHERE conversation_id = %s AND user_id = %s AND id <> %s
            ORDER BY started_at DESC LIMIT 10""", (conversation_id, user_id, rid)).fetchall()
    for r in rows:
        d = r["detail"] or {}
        if d.get("intent") == "update" and not d.get("secondary_intents"):
            continue
        return _was_recommend_need({"detail": d})
    return False


def _route(parsed: pref.ParseResult, clarification: list[dict], *, hold: bool, conflicts: list[dict],
           after_recommend: bool = False) -> dict:
    intents = {parsed.intent, *parsed.secondary_intents}
    out = {"version": ROUTE_VERSION, "use_qa": False, "reason": None, "qa_question": None,
           "recommendation": "skipped_by_route"}

    def decide(use_qa: bool, reason: str) -> dict:
        out.update(use_qa=use_qa, reason=reason)
        return out

    if hold:
        return decide(False, "hold_answer")
    blocked = _blocked_exit(parsed)
    if blocked:
        return decide(False, blocked)
    # 2026-11-01：色彩不是死規定，只有同一句自相矛盾（同時要 X 又不要 X，kind＝in_message）才先澄清；
    # 和先前條件相反等其他衝突照樣走問答給起點，衝突補問附在回答後（非阻斷）。過敏與明說不要的排除照舊。
    conflict_keys = {(c["field"], c.get("key")) for c in conflicts if c.get("kind") == "in_message"}
    if any(q["blocking"] and (q["field"], q.get("key")) in conflict_keys for q in clarification):
        return decide(False, "conflict_first")
    if intents & {"knowledge", "compare"}:
        return decide(True, "knowledge_or_compare")
    if parsed.intent == "update" and not parsed.secondary_intents:
        return decide(True, "update_after_recommend") if after_recommend else decide(False, "preferences_only")
    if any(q["blocking"] for q in clarification):
        return decide(True, "starting_point_before_clarification")
    return decide(True, "default_rag")
# 核心邏輯（route-v4 優先序）：
# 1. 取消 → 不回答（hold）；索取非公開資料、範圍外、商品 → 攔截出口（不問答）；同一句自相矛盾 → 先澄清（規範 5）。
# 2. 知識或比較（含混合意圖）→ 問答。
# 3. 純修改條件（沒有其他意圖）→ 只更新條件，不回答；上一輪是推薦需求時，接上一輪提問用新條件重新問答。
# 4. 有非衝突的阻斷補問（模糊詞、有精神、平常擦法）→ 問答先給起點，補問附在回答後（Q5）。
# 5. 其餘（含推薦需求）→ 預設問答；不跑規則推薦、不出推薦卡。



# 這個對話（同一使用者）上一則請求的提問與 detail；沒有就回 None。
def _previous_turn(conn: Connection, conversation_id: uuid.UUID, user_id: int, rid: uuid.UUID) -> dict | None:
    row = conn.execute(
        """SELECT question, detail FROM requests WHERE conversation_id = %s AND user_id = %s AND id <> %s
            ORDER BY started_at DESC LIMIT 1""", (conversation_id, user_id, rid)).fetchone()
    return {"question": row["question"], "detail": row["detail"] or {}} if row else None


MAX_CONDITION_TURNS = 3


def _condition_only_turns(conn: Connection, conversation_id: uuid.UUID, user_id: int, rid: uuid.UUID) -> list[str]:
    """carry-v3：本輪之前「只記條件、沒有回答」的連續幾則（route reason＝preferences_only），由舊到新，最多 3 則。
    遇到任何一則有走問答或出口的請求就停（那一則之前的條件已經用在它的回答裡了）。"""
    rows = conn.execute(
        """SELECT question, detail FROM requests WHERE conversation_id = %s AND user_id = %s AND id <> %s
            ORDER BY started_at DESC LIMIT %s""", (conversation_id, user_id, rid, MAX_CONDITION_TURNS + 1)).fetchall()
    out: list[str] = []
    for r in rows:
        if ((r["detail"] or {}).get("route") or {}).get("reason") != "preferences_only" or len(out) >= MAX_CONDITION_TURNS:
            break
        out.append(r["question"])
    return out[::-1]
# 核心邏輯：只看同一對話、同一使用者自己的請求（規範 1）；只接「純說條件」那幾句，不接上一輪的回答內容。


_ORDINALS = ("一", "二", "三")
MAX_CARRY_DIRECTIONS = 3


def _previous_directions(detail: dict) -> list[str]:
    """上一輪實際交付給使用者的方向：出卡 → 推薦卡名稱（依名次）；問答 → 非背景主張文字。最多 3 個。"""
    if detail.get("answer_source") == "rule_template" and detail.get("recommendation"):
        recs = sorted(detail["recommendation"].get("recommendations") or [], key=lambda r: r.get("rank") or 0)
        return [r["name"] for r in recs if r.get("name")][:MAX_CARRY_DIRECTIONS]
    if detail.get("answer_source") == "generation_model":
        claims = (detail.get("qa") or {}).get("claims") or []
        return [c["text"][:60] for c in claims if c.get("text") and c.get("role") != "background"][:MAX_CARRY_DIRECTIONS]
    return []


def _swap_occasion(prev_question: str, question: str, parsed: pref.ParseResult, before: dict) -> list[str] | None:
    """本輪把場合改掉（「改成聚會用」）：在上一則提問找舊場合的詞、在本輪找新場合的詞，回傳 [舊詞, 新詞]；找不到回 None。"""
    change = next((c for c in parsed.changes if c["field"] == "occasion" and c.get("op") == "set"), None)
    old = (before.get("occasion") or {}).get("value")
    if change is None or not old or old == change.get("value"):
        return None

    def term_in(text: str, key: str) -> str | None:
        """text 裡屬於場合 key 的最長詞。"""
        hits = [t for t, (kind, k, _) in pref.VOCAB["terms"].items() if kind == "occasion" and k == key and t in text]
        return max(hits, key=len) if hits else None

    old_t, new_t = term_in(prev_question, old), term_in(question, change["value"])
    return [old_t, new_t] if old_t and new_t else None


def _carry_question(prev: dict | None, question: str, parsed: pref.ParseResult, before: dict) -> tuple[str, dict | None]:
    """組本輪送問答的查詢與接續紀錄：
    1. 改場合 → 上一則提問的舊場合詞換成新場合詞（「我想要上班用、不要太亮」→「我想要聚會用、不要太亮」），其他條件原樣保留；
    2. 指回上一輪（剛剛、第一個…）→「上一則提問；上一輪給的方向：第一個「藍調紅」、第二個「鮮明紅唇」；本輪句子」；
    3. 其他 → 「上一則提問；本輪句子」（route-v1 原做法）。"""
    if prev is None:
        return question, None
    base = _previous_base(prev)
    swap = _swap_occasion(base, question, parsed, before) if parsed.intent == "update" else None
    if swap:
        return base.replace(swap[0], swap[1], 1), {"method": "swap_occasion", "previous_question": base, "swapped": swap}
    directions = _previous_directions(prev["detail"]) if _RE_REFERS_BACK.search(question) else []
    if directions:
        listed = "、".join(f"第{_ORDINALS[i]}個「{d}」" for i, d in enumerate(directions))
        # carry-v2（2026-11-03，P04）：方向整句只給模型看；檢索用「上一題＋本題」，避免被方向句拉偏
        return (f"{base}；上一輪給的方向：{listed}；{question}",
                {"method": "previous_directions", "previous_question": base, "directions": directions,
                 "retrieval_question": f"{base}；{question}"})
    return f"{base}；{question}", {"method": "join_previous_question", "previous_question": base}
# 核心邏輯：只用這個對話裡、同一使用者自己的上一則請求（規範 1）；方向取「實際交付的」那一種（卡或問答），
# 沒交付就不帶，不從推薦判斷裡猜。改場合只換場合詞，不動其他字，所以「不要太亮」這類條件保留到本輪。

MAX_CARRY_SEGMENTS = 4


def _previous_base(prev: dict) -> str:
    """carry-v4（2026-11-07）：上一則本身也是接續題時，接它「實際送問答的整串」，不是只接它的原句。
    例：「明天有約會…該擦什麼顏色」→「不要太亮」→ 系統補問 →「是的」：
    舊版第三輪只接「不要太亮；是的」，把約會、要擦什麼顏色都丟了，回答變成來源不足。
    整串最多保留最後 4 段（「；」分隔），避免多輪後查詢無限變長。上一輪的方向句（只給模型看的）不帶。"""
    route = (prev.get("detail") or {}).get("route") or {}
    carried = route.get("carry_over") or {}
    chain = carried.get("retrieval_question") or (route.get("qa_question") if carried else None)
    if not chain:
        return prev["question"]
    return "；".join(chain.split("；")[-MAX_CARRY_SEGMENTS:])
# 核心邏輯：沒有接續過的上一則 → 照舊用它的原句（P04 第二輪、改場合、指回上一輪等既有行為不變）。


# rec-v2：把指定的非阻斷補問延後——從本輪拿掉，並退回「問過」次數與待回答紀錄，之後的推薦輪仍會再問。
def _defer_nonblocking(clarification: list[dict], state: dict, dropped: list[dict]) -> tuple[list[dict], dict]:
    new = copy.deepcopy(state)
    for q in dropped:
        n = new["asked"].get(q["field"], 0) - 1
        if n > 0:
            new["asked"][q["field"]] = n
        else:
            new["asked"].pop(q["field"], None)
        pending = new.get("pending_turn")
        if pending and pending.get("question") == q["question"]:
            new["pending_turn"] = None
    return [q for q in clarification if q not in dropped], new


# 依意圖、補問與推薦結果決定這次請求的結果：(outcome, requests.status, error_code, 給使用者的訊息)。
def _message_outcome(parsed: pref.ParseResult, clarification: list[dict], decision: "rec.Decision | None" = None,
                     rec_error: str | None = None, qa_res: "qa.QaResult | None" = None
                     ) -> tuple[str, str, str | None, str]:
    if qa_res is not None:
        if qa_res.outcome == "answered" and not qa_res.citations:
            # wording-entry-v1 防呆：沒有引用就不能用「每一句都標了來源編號」的說明，outcome 也分開記
            return "answered_without_citations", "succeeded", None, _OUTCOME_MESSAGES["answered_without_citations"]
        if qa_res.outcome == "answered":
            return "answered", "succeeded", None, _OUTCOME_MESSAGES["answered"]
        if qa_res.outcome == "sources_only":
            # 2026-11-01：生成模型連不上時只列原文段落；和一般回答分開，記成失敗並保留錯誤代碼（規範 8）
            return "sources_only", "failed", qa_res.error_code, _OUTCOME_MESSAGES["sources_only"]
        if qa_res.outcome == "insufficient_evidence":
            return "answer_insufficient_evidence", "succeeded", None, _OUTCOME_MESSAGES["answer_insufficient_evidence"]
        code = qa_res.error_code or "model_unexpected_error"
        return "answer_failed", "failed", code, _QA_ERROR_MESSAGES.get(code, "這次沒有產生回答。") + "（可用 request_id 回報）"
    if rec_error:
        return "recommendation_error", "failed", rec_error, _OUTCOME_MESSAGES["recommendation_error"]
    if decision is not None and decision.status in ("recommended", "no_match", "insufficient_evidence", "conditional"):
        key = ("no_match_undertone" if decision.status == "no_match" and decision.reason == "undertone_condition_not_met"
               else decision.status)
        return decision.status, "succeeded", None, _OUTCOME_MESSAGES[key]
    # route-v3：攔截出口不推薦也不生成，結果直接說明為什麼不提供；不能落到「模型未設定」
    intents = {parsed.intent, *parsed.secondary_intents}
    if "restricted_request" in intents:
        return "restricted_request", "succeeded", None, _OUTCOME_MESSAGES["restricted_request"]
    if "outside_scope" in intents:
        return "outside_scope", "succeeded", None, _OUTCOME_MESSAGES["outside_scope"]
    if "product_request" in intents:
        return "product_not_enabled", "succeeded", None, _OUTCOME_MESSAGES["product_not_enabled"]
    if any(q["blocking"] for q in clarification) or (decision is not None
                                                     and decision.status == "needs_clarification"):
        outcome = "needs_clarification"
    elif parsed.intent == "update" and not parsed.secondary_intents:
        outcome = "preferences_updated"
    else:
        outcome = "not_answered"
    if outcome == "not_answered":
        return outcome, "failed", "model_not_configured", _OUTCOME_MESSAGES[outcome]
    return outcome, "succeeded", None, _OUTCOME_MESSAGES[outcome]
# 核心邏輯：
# 1. 有阻斷性補問（衝突、會左右排除的模糊詞）→ 先問，不生成。
# 2. 純修改條件、範圍外說明，不需要模型，算成功完成。
#    route-v3：索取非公開資料、範圍外、商品要求是攔截出口，排在補問之前，各自回固定說明（succeeded）。
# 3. 知識問答、比較需要生成模型；目前未啟用，記 failed / model_not_configured（不假裝有回答）。
# 4. S7：推薦由規則完成（recommended／no_match／insufficient_evidence 都是誠實的業務結果，算成功）；
#    推薦依據在處理中一再變動才記 failed / recommendation_evidence_changed。
# 5. S5：知識問答有結果時優先採用：answered、answer_insufficient_evidence（缺資料，誠實的業務結果，算成功）、
#    answer_failed（檢索或模型失敗，記 failed＋具體錯誤代碼）。
