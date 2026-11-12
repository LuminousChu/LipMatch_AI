"""S9 歷史存取：舊回答保留版本追溯，但「查看」一律依目前的權限與資料狀態。

為什麼需要
  回答、推薦理由與引用在當下是依當時的權限與片段產生的。之後來源可能被停用、過期、改成管理者限定、
  片段被退回，或文件換了新版本；使用者也可能被降級或停用。若歷史畫面直接重放日誌，就等於
  「透過舊回答看到後來受限的內容」。所以每次查看都要重新判斷。

判斷規則（resolve）
  每個引用以 (doc_key, version_label, chunk_key) 找回當時的片段，再看「此刻」：
  - available  ：片段仍可用（已核准、啟用、版本已發布、文件啟用且在有效期間、角色看得到）。
  - superseded ：當時的版本已被新版本取代，但目前版本有內容指紋完全相同、且角色看得到的可用片段；
                 引用改指向目前版本，並註明原本引用的版本（版本追溯不中斷、內容沒變所以可以顯示）。
  - unavailable：其他情況（停用、過期、未生效、改成限管理者、片段退回或停用、新版本內容不同、帳號停用…）。
  回答中任何一個引用（含問答時送進模型的上下文片段）是 unavailable → 整則回答遮蔽：
  不回回答文字、推薦主張、限制說明與引用定位，只回固定說明；不區分「不存在」與「無權限」。

版本追溯
  日誌（requests.detail、retrieval_hits、request_recommendations）完整保留，不因遮蔽而改寫；
  管理者可用 admin_trace 查一個請求當時用了哪些版本、索引、片段指紋，以及它們此刻的狀態。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

# 角色 → 可見的文件範圍（與 retrieval、recommend、results 一致）
ROLE_VISIBILITY: dict[str, tuple[str, ...]] = {"user": ("all",), "admin": ("all", "admin")}

AVAILABLE, SUPERSEDED, UNAVAILABLE = "available", "superseded", "unavailable"

MASK_NOTE = ("這則回答引用的來源已更新、停用或不在你目前的查看範圍。為避免顯示已失效或受限的內容，"
             "原回答已隱藏；可以重新提問，取得依目前來源的結果。")
UNAVAILABLE_NOTE = "此來源目前無法查看（可能已停用、過期或不在你的查看範圍）。"

Key = tuple[str, str, str]  # (doc_key, version_label, chunk_key)


@dataclass
class Resolution:
    """一個歷史引用此刻的狀態。current 是要顯示的可用片段（available 為本身、superseded 為目前版本的同內容片段）。"""
    state: str
    reason: str | None = None
    current: dict | None = None
    cited: dict | None = None        # 當時引用的片段列（含版本狀態），供管理者追溯


@dataclass
class Assessment:
    """一則歷史回答的整體判斷：masked = 有任何引用此刻不可用。"""
    masked: bool
    resolutions: dict[Key, Resolution] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
def current_role(conn: psycopg.Connection, user_id: int) -> str | None:
    """此刻資料庫中的角色；帳號停用或不存在回傳 None（視為什麼都看不到）。"""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT role FROM users WHERE id = %s AND is_active", (user_id,)).fetchone()
    return row["role"] if row else None
# 核心邏輯：請求處理中途角色被改（例如管理者被降級），回傳前以這個值重新判斷，而不是沿用請求開始時的角色。


# ---------------------------------------------------------------------------
# 引用判斷
# ---------------------------------------------------------------------------
_CITED_SQL = """
SELECT d.doc_key, dv.version_label, c.chunk_key, c.id AS chunk_id, c.content_sha256, c.review_status,
       c.is_active AS chunk_active, dv.status AS version_status, dv.published_at, dv.retired_at,
       d.id AS document_id, d.visibility, d.is_active AS doc_active, d.rag_eligible,
       (d.valid_from IS NOT NULL AND d.valid_from > now()) AS not_yet_valid,
       (d.valid_until IS NOT NULL AND d.valid_until <= now()) AS expired,
       (u.chunk_id IS NOT NULL) AS usable
  FROM chunks c
  JOIN document_versions dv ON dv.id = c.document_version_id
  JOIN documents d ON d.id = dv.document_id
  LEFT JOIN usable_chunks u ON u.chunk_id = c.id
 WHERE (d.doc_key, dv.version_label, c.chunk_key) IN (SELECT * FROM unnest(%s::text[], %s::text[], %s::text[]))
"""

_SHOW_COLS = """u.chunk_id, u.document_id, u.content_sha256, u.doc_key, u.version_label, u.chunk_key, u.title,
                u.page_start, u.page_end, u.html_locator, u.section, u.quote_text, u.visibility,
                u.bibliography"""


# 依序找出片段此刻不能顯示的原因；可顯示時回傳 None。原因只給日誌與管理者看。
def _blocking_reason(row: dict, visibility: tuple[str, ...]) -> str | None:
    if not row["doc_active"]:
        return "document_inactive"
    if not row["rag_eligible"]:
        return "document_not_rag_eligible"
    if row["expired"]:
        return "document_expired"
    if row["not_yet_valid"]:
        return "document_not_yet_valid"
    if row["visibility"] not in visibility:
        return "role_not_permitted"
    if row["review_status"] != "approved":
        return "chunk_not_approved"
    if not row["chunk_active"]:
        return "chunk_inactive"
    return None


def resolve(conn: psycopg.Connection, keys: Iterable[Key], role: str | None) -> dict[Key, Resolution]:
    """判斷一組歷史引用此刻的狀態（available／superseded／unavailable）。role=None 表示帳號已停用。"""
    keys = sorted({tuple(k) for k in keys if all(k)})
    if not keys:
        return {}
    visibility = ROLE_VISIBILITY.get(role or "", ())
    with conn.cursor(row_factory=dict_row) as cur:
        cited = {(r["doc_key"], r["version_label"], r["chunk_key"]): r for r in cur.execute(
            _CITED_SQL, ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys])).fetchall()}
        usable_ids = [r["chunk_id"] for r in cited.values() if r["usable"]]
        shown = {r["chunk_id"]: r for r in cur.execute(
            f"SELECT {_SHOW_COLS} FROM usable_chunks u WHERE u.chunk_id = ANY(%s)", (usable_ids,)).fetchall()}
        retired = [r for r in cited.values() if r["version_status"] == "retired"]
        successors = {(r["document_id"], r["content_sha256"]): r for r in cur.execute(
            f"""SELECT {_SHOW_COLS} FROM usable_chunks u
                 WHERE (u.document_id, u.content_sha256) IN (SELECT * FROM unnest(%s::bigint[], %s::text[]))""",
            ([r["document_id"] for r in retired], [r["content_sha256"] for r in retired])).fetchall()}
    out: dict[Key, Resolution] = {}
    for k in keys:
        row = cited.get(k)
        if role is None:
            out[k] = Resolution(UNAVAILABLE, "user_inactive", cited=row)
        elif not visibility:
            out[k] = Resolution(UNAVAILABLE, "unknown_role", cited=row)
        elif row is None:
            out[k] = Resolution(UNAVAILABLE, "not_found")
        elif (reason := _blocking_reason(row, visibility)) is not None:
            out[k] = Resolution(UNAVAILABLE, reason, cited=row)
        elif row["usable"]:
            out[k] = Resolution(AVAILABLE, current=shown[row["chunk_id"]], cited=row)
        elif row["version_status"] == "retired":
            succ = successors.get((row["document_id"], row["content_sha256"]))
            out[k] = (Resolution(SUPERSEDED, current=succ, cited=row) if succ is not None
                      else Resolution(UNAVAILABLE, "superseded_content_changed", cited=row))
        else:
            out[k] = Resolution(UNAVAILABLE, "version_not_published", cited=row)
    return out
# 核心邏輯：
# 1. 一次查詢拿到全部引用「當時的片段」與它們此刻的狀態；不可顯示原因依固定順序判斷（文件 → 期間 → 權限 → 片段）。
# 2. 版本被取代時只在「目前版本有完全相同內容」時沿用（superseded），內容改過就視為失效，不顯示舊內容。
# 3. 角色只來自後端（current_role），帳號停用＝全部不可用。


# ---------------------------------------------------------------------------
# 從日誌取出一則回答用到的全部片段
# ---------------------------------------------------------------------------
def _key(c: dict) -> Key | None:
    k = (c.get("doc_key"), c.get("version_label"), c.get("chunk_key"))
    return k if all(k) else None


def refs_in_detail(detail: dict | None) -> list[Key]:
    """推薦引用、問答引用、問答送進模型的上下文片段；都要此刻可用，回答才可顯示。"""
    detail = detail or {}
    found: list[Key | None] = []
    rec = detail.get("recommendation") or {}
    for r in rec.get("recommendations") or []:
        for cl in r.get("claims") or []:
            found += [_key(c) for c in cl.get("citations") or []]
    qa_log = detail.get("qa") or {}
    found += [_key(c) for c in qa_log.get("citations") or []]
    found += [_key(c) for c in (qa_log.get("context_sent") or {}).get("evidence") or []]
    return list(dict.fromkeys(k for k in found if k))
# 核心邏輯：上下文片段即使沒有被引用，也可能影響模型寫出的句子，所以一併檢查（「上下文查看依目前權限」）。


def assess(conn: psycopg.Connection, detail: dict | None, role: str | None) -> Assessment:
    """判斷一則歷史回答此刻能不能顯示。"""
    res = resolve(conn, refs_in_detail(detail), role)
    return Assessment(masked=any(r.state == UNAVAILABLE for r in res.values()), resolutions=res)


# ---------------------------------------------------------------------------
# 管理者追溯（不含問題與回答原文；只看版本、索引、片段指紋與此刻狀態）
# ---------------------------------------------------------------------------
def admin_trace(conn: psycopg.Connection, request_id) -> dict | None:
    """一個請求當時用了哪些文件版本、索引、片段（含指紋），以及這些片段此刻對管理者／一般使用者的狀態。"""
    with conn.cursor(row_factory=dict_row) as cur:
        req = cur.execute(
            """SELECT r.id AS request_id, r.user_id, r.status, r.error_code, r.started_at, r.completed_at,
                      r.prompt_version, r.app_version, r.model_provider, r.model_name,
                      iv.label AS index_label, iv.status AS index_status, iv.embedding_model,
                      r.detail -> 'outcome' AS outcome, r.detail -> 'recommendation' ->> 'rules_version' AS rules_version,
                      r.detail AS detail
                 FROM requests r LEFT JOIN index_versions iv ON iv.id = r.index_version_id
                WHERE r.id = %s""", (request_id,)).fetchone()
        if req is None:
            return None
        hits = cur.execute(
            """SELECT h.retriever, h.rank, h.passed_filter, h.filter_reason, h.score,
                      d.doc_key, dv.version_label, c.chunk_key, c.content_sha256
                 FROM retrieval_hits h JOIN chunks c ON c.id = h.chunk_id
                 JOIN document_versions dv ON dv.id = c.document_version_id
                 JOIN documents d ON d.id = dv.document_id
                WHERE h.request_id = %s ORDER BY h.retriever, h.rank""", (request_id,)).fetchall()
        options = cur.execute(
            """SELECT rr.rank, o.option_key, o.status AS option_status_now
                 FROM request_recommendations rr JOIN recommendation_options o ON o.id = rr.option_id
                WHERE rr.request_id = %s ORDER BY rr.rank""", (request_id,)).fetchall()
    detail = req.pop("detail") or {}
    keys = refs_in_detail(detail) + [(h["doc_key"], h["version_label"], h["chunk_key"]) for h in hits]
    as_admin, as_user = resolve(conn, keys, "admin"), resolve(conn, keys, "user")
    owner_role = current_role(conn, req["user_id"])
    as_owner = resolve(conn, keys, owner_role)

    def describe(k: Key) -> dict:
        a = as_admin.get(k) or Resolution(UNAVAILABLE, "not_found")
        cited = a.cited or {}
        return {"doc_key": k[0], "version_label": k[1], "chunk_key": k[2],
                "content_sha256": cited.get("content_sha256"), "version_status_now": cited.get("version_status"),
                "version_published_at": cited.get("published_at"), "version_retired_at": cited.get("retired_at"),
                "state_for_admin": a.state, "reason_for_admin": a.reason,
                "state_for_user_role": as_user[k].state, "state_for_owner_now": as_owner[k].state,
                "current_version_label": (a.current or {}).get("version_label")}
    cited_keys = refs_in_detail(detail)
    return {**req, "owner_role_now": owner_role,
            "owner_view_masked": any(as_owner[k].state == UNAVAILABLE for k in cited_keys),
            "citations": [describe(k) for k in cited_keys],
            "retrieval_hits": [{**h, **{x: describe((h["doc_key"], h["version_label"], h["chunk_key"]))[x]
                                         for x in ("state_for_admin", "reason_for_admin", "version_status_now")}}
                               for h in hits],
            "recommendations": options}
# 核心邏輯：日誌原樣保留，追溯只「讀」；同一個片段同時列出對管理者、一般使用者角色、請求擁有者（此刻角色）
# 的狀態，方便確認「擁有者現在看到的是遮蔽版」。問題與回答原文不在這裡回傳（管理日誌不含個人對話內容）。
