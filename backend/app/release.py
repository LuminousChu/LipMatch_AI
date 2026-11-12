"""S9 版本發布治理：待發布版本 → 審核 → 向量完成 → 驗證 → 切換；停用、可見範圍、有效期間；安全切回舊版。

流程（一份文件一次）
  1. 待發布：`python -m app.ingest import` 匯入成 draft 版本（片段全部 pending）。
  2. 審核：管理者逐筆核准／退回（審核頁或 `ingest review`），清洗不等於核准。
  3. 向量：`prepare` 建一個新的 building 索引 = 「切換後應可檢索的全部片段」的向量。
     內容指紋（content_sha256，含 search_text）相同且模型相同的向量直接沿用，其餘才呼叫嵌入模型。
     目前沒有 active 索引時（嵌入模型未定，DEC-11），向量步驟「不適用」並明確記錄，不假裝完成。
  4. 驗證＋切換：`switch` 在**同一個資料庫交易**內重新驗證全部條件、把舊 published 改 retired、
     新版本改 published、啟用新索引（舊索引 retired），並寫入發布事件。任何一步失敗 → 整個交易回滾，
     舊版本與舊索引原封不動繼續服務；被拒絕的嘗試另寫一筆 refused 事件。
  5. 切回舊版：`rollback` 走同一條路（先 prepare 含舊版片段的索引，再交易切換），只是目標是 retired 版本。

一致性保證
  - 切換在單一交易內完成；並取得與 retrieval.activate_index 相同的 advisory lock（index_versions）
    以及與匯入相同的文件鎖（ingest:<doc_key>），同時間不會有兩個切換或匯入交錯。
  - 已發布片段永遠不原地覆寫：內容欄位由資料庫觸發器擋下；新內容一律是新版本。
  - prepare 的索引記下「以哪個 active 索引為基準」；切換時基準已變（其他文件先切換過）就拒絕，要求重新 prepare。

候選規則
  - 被取代（或停用）版本的片段若是 active 推薦候選的證據，切換後該候選的證據立即失效（recommend 每次請求都重查）。
    為避免無聲地讓推薦全部變成「證據不足」，切換前會列出受影響候選，須明確加 --accept-candidate-impact 才切換。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import retrieval

MODES = ("publish", "rollback")


class ReleaseError(RuntimeError):
    """發布／切換被拒絕。舊版本與舊索引維持原狀；detail 會寫進 refused 事件。"""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# 查詢輔助
# ---------------------------------------------------------------------------
# 取得文件列；找不到就拒絕。連線須使用 dict_row。
def _document(conn: psycopg.Connection, doc_key: str) -> dict:
    row = conn.execute(
        """SELECT id, doc_key, visibility, is_active, rag_eligible, valid_from, valid_until
             FROM documents WHERE doc_key = %s""", (doc_key,)).fetchone()
    if row is None:
        raise ReleaseError(f"找不到文件：{doc_key}")
    return row


# 取得指定文件的某個版本；找不到就拒絕。
def _version(conn: psycopg.Connection, document_id: int, label: str) -> dict:
    row = conn.execute(
        """SELECT id, version_label, status, published_at, retired_at
             FROM document_versions WHERE document_id = %s AND version_label = %s""",
        (document_id, label)).fetchone()
    if row is None:
        raise ReleaseError(f"找不到版本：{label}")
    return row


# 取得文件目前的 published 版本（沒有則 None）。
def _published(conn: psycopg.Connection, document_id: int) -> dict | None:
    return conn.execute(
        """SELECT id, version_label, status FROM document_versions
            WHERE document_id = %s AND status = 'published'""", (document_id,)).fetchone()


# 一個版本的片段審核統計；approved 只算「已核准且啟用」（停用的片段不會被檢索）。
def _counts(conn: psycopg.Connection, version_id: int) -> dict:
    return conn.execute(
        """SELECT count(*) FILTER (WHERE review_status = 'approved' AND is_active) AS approved,
                  count(*) FILTER (WHERE review_status = 'pending') AS pending,
                  count(*) FILTER (WHERE review_status = 'rejected') AS rejected,
                  count(*) FILTER (WHERE NOT is_active) AS inactive
             FROM chunks WHERE document_version_id = %s""", (version_id,)).fetchone()


# 以某版本片段為證據的推薦候選（active／draft），切換或停用後這些證據會失效。
def candidate_impact(conn: psycopg.Connection, version_ids: list[int]) -> list[dict]:
    if not version_ids:
        return []
    return conn.execute(
        """SELECT o.option_key, o.status, count(*) AS evidence
             FROM recommendation_option_evidence e
             JOIN chunks c ON c.id = e.chunk_id
             JOIN recommendation_options o ON o.id = e.option_id
            WHERE c.document_version_id = ANY(%s) AND o.status IN ('active', 'draft')
            GROUP BY o.option_key, o.status ORDER BY o.option_key""", (version_ids,)).fetchall()


# 寫入一筆發布事件；回傳事件 id。事件表只能新增（資料庫觸發器保證）。
def _event(conn: psycopg.Connection, document_id: int, event_type: str, *, actor: str,
           from_version_id: int | None = None, to_version_id: int | None = None,
           index_before_id: int | None = None, index_after_id: int | None = None,
           reason: str | None = None, detail: dict | None = None) -> int:
    return conn.execute(
        """INSERT INTO document_release_events
                  (document_id, event_type, from_version_id, to_version_id, index_before_id, index_after_id,
                   actor, reason, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (document_id, event_type, from_version_id, to_version_id, index_before_id, index_after_id,
         _actor(actor), reason, Jsonb(detail or {}))).fetchone()["id"]


# 操作人標示不可空白；過長截斷（只是稽核標籤，不是身分驗證）。
def _actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise ReleaseError("必須標示操作人（--operator）")
    return actor[:200]


# 同時取得索引鎖與文件鎖（順序固定：先索引再文件，避免死結）；只在交易內有效。
def _lock(conn: psycopg.Connection, doc_key: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('index_versions'))")
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ingest:" + doc_key,))
# 核心邏輯：索引鎖與 retrieval.activate_index 相同、文件鎖與 ingest.import_document 相同，
# 所以切換、索引啟用、同一文件的匯入三者不會交錯。


# 切換後「應可檢索」的片段：其他文件目前的可用片段 ＋ 目標版本已核准且啟用的片段。
# 只供引用的片段（attributes.citation_only）不建向量，與 retrieval 的建索引與涵蓋檢查一致。
_TARGET_SQL = f"""
SELECT chunk_id, search_text, content_sha256, document_version_id
  FROM usable_chunks WHERE document_id <> %(doc)s AND {retrieval._INDEXABLE}
UNION ALL
SELECT c.id, c.search_text, c.content_sha256, c.document_version_id
  FROM chunks c
  JOIN document_versions dv ON dv.id = c.document_version_id
  JOIN documents d ON d.id = dv.document_id
 WHERE dv.id = %(ver)s AND c.review_status = 'approved' AND c.is_active
   AND {retrieval._INDEXABLE.replace("attributes", "c.attributes")}
   AND d.is_active AND d.rag_eligible
   AND (d.valid_from IS NULL OR d.valid_from <= now())
   AND (d.valid_until IS NULL OR d.valid_until > now())
"""


# ---------------------------------------------------------------------------
# 1. 計畫（唯讀）：切換前可以先看會發生什麼、有沒有阻擋條件
# ---------------------------------------------------------------------------
def plan(conn: psycopg.Connection, doc_key: str, label: str, *, mode: str = "publish",
         allow_empty: bool = False) -> dict:
    """列出切換到指定版本的條件與影響；problems 不為空代表不能切換。不寫入任何資料。"""
    if mode not in MODES:
        raise ReleaseError(f"未知模式：{mode}")
    doc = _document(conn, doc_key)
    ver = _version(conn, doc["id"], label)
    current = _published(conn, doc["id"])
    counts = _counts(conn, ver["id"])
    active = retrieval.active_index(conn)
    problems: list[str] = []
    if mode == "publish" and ver["status"] != "draft":
        problems.append(f"{doc_key} {label} 狀態是 {ver['status']}，只能發布 draft（切回舊版請用 rollback）")
    if mode == "rollback" and ver["status"] != "retired":
        problems.append(f"{doc_key} {label} 狀態是 {ver['status']}，只能切回 retired 版本")
    if not doc["is_active"]:
        problems.append(f"{doc_key} 已停用；先 reactivate 再發布")
    if not doc["rag_eligible"]:
        problems.append(f"{doc_key} 不可進 RAG（rag_eligible = false），不能發布為證據")
    if counts["approved"] == 0 and (mode == "rollback" or not allow_empty):
        problems.append(f"{doc_key} {label} 沒有任何已核准片段，發布後檢索不到內容；確定要發布請加 --allow-empty")
    return {
        "doc_key": doc_key, "mode": mode, "version_label": label, "version_status": ver["status"],
        "current_published": current["version_label"] if current else None,
        "counts": dict(counts),
        "active_index": active["label"] if active else None,
        "vectors": "required" if active else "not_applicable_no_active_index",
        "candidate_impact": [dict(r) for r in candidate_impact(conn, [current["id"]] if current else [])],
        "problems": problems,
        "_ids": {"document_id": doc["id"], "version_id": ver["id"],
                 "current_id": current["id"] if current else None, "active_index_id": active["id"] if active else None},
    }
# 核心邏輯：計畫只讀資料；switch 會在交易內「重新」呼叫一次 plan，不信任事前的結果（狀態可能已變）。


# ---------------------------------------------------------------------------
# 2. 準備向量：新的 building 索引（不影響線上 active 索引）
# ---------------------------------------------------------------------------
def prepare(conn: psycopg.Connection, doc_key: str, label: str, embedder: retrieval.Embedder, *,
            actor: str, mode: str = "publish", index_label: str | None = None, batch: int = 32,
            allow_empty: bool = False) -> dict:
    """為「切換後的片段集合」建立 building 索引；可沿用的向量直接複製，其餘才呼叫嵌入模型。

    失敗（嵌入模型錯誤、維度不符…）時整個交易回滾、不留下半套索引，並寫一筆 refused 事件；線上不受影響。
    連線須為 autocommit＋dict_row（CLI 預設）；非 autocommit 時由呼叫端 commit。
    """
    p = plan(conn, doc_key, label, mode=mode, allow_empty=allow_empty)
    ids = p["_ids"]
    try:
        if p["problems"]:
            raise ReleaseError("；".join(p["problems"]))
        active = retrieval.active_index(conn)
        if active is None:
            raise ReleaseError("目前沒有 active 索引：向量步驟不適用，可直接 switch（會記錄 vectors=not_applicable）")
        try:
            retrieval.check_compatible(active, embedder)
        except retrieval.IncompatibleIndex as exc:
            raise ReleaseError(str(exc)) from None
        index_label = index_label or f"rel-{doc_key}-{label}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        # 向量表依維度選（1024 → chunk_embeddings；768 → chunk_embeddings_v768，另記嵌入輸入 SHA 與索引指紋）
        try:
            table, _ = retrieval._store(embedder.dim)
        except retrieval.IncompatibleIndex as exc:
            raise ReleaseError(str(exc)) from None
        v768 = table == "chunk_embeddings_v768"
        fingerprint = getattr(embedder, "fingerprint", None)
        if v768 and (fingerprint is None or getattr(embedder, "side", None) != "document"):
            raise ReleaseError("768 維索引必須用帶嵌入指紋的文件端嵌入器準備（查詢前綴不能用來建索引）")
        with conn.transaction():
            new_id = conn.execute(
                """INSERT INTO index_versions (label, embedding_model, embedding_dim, notes, embedding_fingerprint)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (index_label, embedder.model, embedder.dim,
                 f"S9 {mode} {doc_key} {label}; base={active['label']}",
                 Jsonb(fingerprint) if fingerprint else None)).fetchone()["id"]
            params = {"doc": ids["document_id"], "ver": ids["version_id"]}
            conn.execute(
                f"""INSERT INTO index_version_documents (index_version_id, document_version_id)
                    SELECT DISTINCT %(new)s, document_version_id FROM ({_TARGET_SQL}) t""",
                {**params, "new": new_id})
            # 沿用向量：同模型、同維度、同內容指紋；768 維另要求同一嵌入指紋（模型檔與前綴相同），並沿用嵌入輸入 SHA
            extra_cols, extra_vals = (", input_sha256", ", ce.input_sha256") if v768 else ("", "")
            same_fp = " AND iv.embedding_fingerprint = %(fp)s" if v768 else ""
            reused = conn.execute(
                f"""INSERT INTO {table} (index_version_id, chunk_id, embedding, content_sha256{extra_cols})
                    SELECT DISTINCT ON (t.chunk_id) %(new)s, t.chunk_id, ce.embedding, t.content_sha256{extra_vals}
                      FROM ({_TARGET_SQL}) t
                      JOIN {table} ce ON ce.content_sha256 = t.content_sha256
                      JOIN index_versions iv ON iv.id = ce.index_version_id
                       AND iv.embedding_model = %(model)s AND iv.embedding_dim = %(dim)s AND iv.id <> %(new)s{same_fp}
                     ORDER BY t.chunk_id, (iv.status = 'active') DESC, iv.id DESC""",
                {**params, "new": new_id, "model": embedder.model, "dim": embedder.dim,
                 "fp": Jsonb(fingerprint) if fingerprint else None}).rowcount
            missing = conn.execute(
                f"""SELECT t.chunk_id, t.search_text, t.content_sha256 FROM ({_TARGET_SQL}) t
                     WHERE NOT EXISTS (SELECT 1 FROM {table} ce
                                        WHERE ce.index_version_id = %(new)s AND ce.chunk_id = t.chunk_id)
                     ORDER BY t.chunk_id""", {**params, "new": new_id}).fetchall()
            for start in range(0, len(missing), batch):
                part = missing[start:start + batch]
                vectors = embedder.embed([r["search_text"] for r in part])
                if len(vectors) != len(part):
                    raise ReleaseError("嵌入模型回傳的向量數量與輸入不符")
                with conn.cursor() as cur:
                    if v768:
                        cur.executemany(
                            """INSERT INTO chunk_embeddings_v768 (index_version_id, chunk_id, embedding,
                                                                  content_sha256, input_sha256)
                               VALUES (%s, %s, %s::vector, %s, %s)""",
                            [(new_id, r["chunk_id"], retrieval._vector_literal(v, embedder.dim), r["content_sha256"],
                              embedder.input_sha256(r["search_text"])) for r, v in zip(part, vectors)])
                    else:
                        cur.executemany(
                            """INSERT INTO chunk_embeddings (index_version_id, chunk_id, embedding, content_sha256)
                               VALUES (%s, %s, %s::vector, %s)""",
                            [(new_id, r["chunk_id"], retrieval._vector_literal(v, embedder.dim), r["content_sha256"])
                             for r, v in zip(part, vectors)])
            detail = {"mode": mode, "index_label": index_label, "base_index_id": active["id"],
                      "base_index_label": active["label"], "reused": reused, "embedded": len(missing),
                      "embedding_model": embedder.model}
            _event(conn, ids["document_id"], "prepare", actor=actor, from_version_id=ids["current_id"],
                   to_version_id=ids["version_id"], index_before_id=active["id"], index_after_id=new_id,
                   detail=detail)
    except ReleaseError as exc:
        _refused(conn, ids["document_id"], "prepare", label, actor, str(exc), exc.detail)
        raise
    except (psycopg.Error, retrieval.RetrievalError) as exc:
        _refused(conn, ids["document_id"], "prepare", label, actor, type(exc).__name__)
        raise ReleaseError(f"準備向量失敗（{type(exc).__name__}），線上版本與索引未變動") from exc
    except Exception as exc:  # 嵌入模型的任何錯誤（逾時、連線…）都不能留下半套索引
        _refused(conn, ids["document_id"], "prepare", label, actor, type(exc).__name__)
        raise ReleaseError(f"嵌入模型呼叫失敗（{type(exc).__name__}），線上版本與索引未變動") from exc
    return {"doc_key": doc_key, "version_label": label, "status": "building", **detail}
# 核心邏輯：
# 1. 目標集合 = 其他文件目前可用片段 ＋ 目標版本已核准片段；被取代的舊版片段不放進新索引。
# 2. 向量沿用條件：同模型、同維度、同內容指紋（content_sha256 涵蓋 search_text）；active 索引優先。
# 3. 全部在一個交易：嵌入到一半失敗也不會留下半套 building 索引。


# ---------------------------------------------------------------------------
# 3. 驗證＋切換（同一交易）
# ---------------------------------------------------------------------------
def switch(conn: psycopg.Connection, doc_key: str, label: str, *, actor: str, mode: str = "publish",
           index_label: str | None = None, accept_candidate_impact: bool = False,
           allow_empty: bool = False, reason: str | None = None) -> dict:
    """在單一交易內重新驗證並切換文件版本（與索引）。失敗時舊版本、舊索引都不變，並記錄 refused 事件。

    非 autocommit 連線（例如 API 連線池）：交易以 savepoint 形式進行；refused 事件寫在交易外，
    呼叫端須 commit 才會保留拒絕紀錄（main.review_publish 已處理）。
    """
    if mode == "rollback" and not (reason and len(reason.strip()) >= 2):
        raise ReleaseError("切回舊版必須填寫原因（--reason）")
    document_id = None
    try:
        with conn.transaction():
            _lock(conn, doc_key)
            p = plan(conn, doc_key, label, mode=mode, allow_empty=allow_empty)
            ids = p["_ids"]
            document_id = ids["document_id"]
            if p["problems"]:
                raise ReleaseError("；".join(p["problems"]))
            live = [c["option_key"] for c in p["candidate_impact"] if c["status"] == "active"]
            if live and not accept_candidate_impact:
                raise ReleaseError(
                    f"目前版本 {p['current_published']} 是 active 推薦候選 {live} 的證據；切換後這些候選的證據會失效"
                    "（推薦改回證據不足）。確認後加 --accept-candidate-impact，或先由 B 線把候選證據改到新版本",
                    {"candidate_impact": live})
            active = retrieval.active_index(conn)
            new_index = _check_prepared_index(conn, active, index_label, ids)
            if ids["current_id"] is not None:
                conn.execute("UPDATE document_versions SET status = 'retired', retired_at = now() WHERE id = %s",
                             (ids["current_id"],))
            if mode == "rollback":
                conn.execute("SELECT set_config('lipmatch.version_rollback', 'on', true)")
                conn.execute("""UPDATE document_versions SET status = 'published', published_at = now(),
                                       retired_at = NULL WHERE id = %s""", (ids["version_id"],))
                conn.execute("SELECT set_config('lipmatch.version_rollback', 'off', true)")
            else:
                conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s",
                             (ids["version_id"],))
            coverage = None
            if new_index is not None:
                try:
                    coverage = retrieval.activate_index(conn, index_label)
                except retrieval.IndexNotReady as exc:
                    raise ReleaseError(f"新索引未涵蓋切換後全部可用片段，不切換：{exc}") from None
            usable = conn.execute("SELECT count(*) AS n FROM usable_chunks WHERE document_version_id = %s",
                                  (ids["version_id"],)).fetchone()["n"]
            if usable == 0 and not allow_empty:
                raise ReleaseError(f"切換後 {doc_key} {label} 沒有可用片段，不切換")
            detail = {"mode": mode, "counts": p["counts"], "usable_after": usable,
                      "vectors": "activated" if new_index else "not_applicable_no_active_index",
                      "index_label": index_label, "coverage": coverage,
                      "candidate_impact": [c["option_key"] for c in p["candidate_impact"]],
                      "accepted_candidate_impact": bool(accept_candidate_impact and p["candidate_impact"])}
            _event(conn, document_id, mode, actor=actor, from_version_id=ids["current_id"],
                   to_version_id=ids["version_id"], index_before_id=ids["active_index_id"],
                   index_after_id=new_index["id"] if new_index else ids["active_index_id"],
                   reason=reason, detail=detail)
    except ReleaseError as exc:
        _refused(conn, document_id, mode, label, actor, str(exc), exc.detail, doc_key=doc_key)
        raise
    except psycopg.Error as exc:
        _refused(conn, document_id, mode, label, actor, type(exc).__name__, doc_key=doc_key)
        raise ReleaseError(f"資料庫拒絕切換（{type(exc).__name__}），舊版本與索引未變動") from exc
    return {"doc_key": doc_key, "mode": mode, "published": label, "retired": p["current_published"],
            **p["counts"], "usable_after": usable, "vectors": detail["vectors"], "index_label": index_label,
            "candidate_impact": detail["candidate_impact"]}
# 核心邏輯：
# 1. 交易一開始就上鎖，再「重新」做一次 plan：事前檢查過不代表此刻仍成立。
# 2. 有 active 索引時，必須提供 prepare 過、基準仍是目前 active 的 building 索引；
#    版本切換後由 activate_index 檢查新索引涵蓋「切換後全部可用片段」，缺任何一筆就整個回滾。
# 3. 沒有 active 索引（嵌入模型未定）時，向量步驟記為 not_applicable，不指定索引。
# 4. rollback 只在交易內暫時打開 lipmatch.version_rollback，讓觸發器允許 retired → published。


# 檢查 prepare 過的索引能不能用在這次切換；沒有 active 索引時回傳 None。
def _check_prepared_index(conn: psycopg.Connection, active: dict | None, index_label: str | None,
                          ids: dict) -> dict | None:
    if active is None:
        if index_label:
            raise ReleaseError("目前沒有 active 索引：向量步驟不適用，不要指定 --index")
        return None
    if not index_label:
        raise ReleaseError(f"目前有 active 索引 {active['label']}：須先 prepare 新索引並以 --index 指定")
    idx = conn.execute("SELECT id, status, embedding_model, embedding_dim FROM index_versions WHERE label = %s",
                       (index_label,)).fetchone()
    if idx is None or idx["status"] != "building":
        raise ReleaseError(f"索引 {index_label} 不存在或不是 building")
    if (idx["embedding_model"], idx["embedding_dim"]) != (active["embedding_model"], active["embedding_dim"]):
        raise ReleaseError("新索引的嵌入模型與目前 active 索引不同；換模型請走 S4 索引重建流程")
    prep = conn.execute(
        """SELECT detail, to_version_id FROM document_release_events
            WHERE event_type = 'prepare' AND index_after_id = %s ORDER BY id DESC LIMIT 1""",
        (idx["id"],)).fetchone()
    if prep is None or prep["to_version_id"] != ids["version_id"]:
        raise ReleaseError(f"索引 {index_label} 不是為這個版本 prepare 的")
    if prep["detail"].get("base_index_id") != active["id"]:
        raise ReleaseError(f"索引 {index_label} 準備時的基準索引已不是目前 active（{active['label']}），請重新 prepare")
    return idx


# 寫一筆 refused 事件（在交易外）；找不到文件時不寫（外鍵需要文件 id）。
def _refused(conn: psycopg.Connection, document_id: int | None, action: str, label: str, actor: str,
             error: str, extra: dict | None = None, *, doc_key: str | None = None) -> None:
    try:
        if document_id is None and doc_key:
            row = conn.execute("SELECT id FROM documents WHERE doc_key = %s", (doc_key,)).fetchone()
            document_id = row["id"] if row else None
        if document_id is None:
            return
        _event(conn, document_id, "refused", actor=actor or "unknown",
               detail={"action": action, "version_label": label, "error": error[:1000], **(extra or {})})
    except (psycopg.Error, ReleaseError):
        pass  # 拒絕紀錄寫不進去不能蓋過原本的拒絕原因
# 核心邏輯：被拒絕的嘗試也留紀錄，日後能回答「為什麼舊版還在線上」。


def rollback(conn: psycopg.Connection, doc_key: str, to_label: str, *, actor: str, reason: str,
             index_label: str | None = None, accept_candidate_impact: bool = False) -> dict:
    """安全切回舊版：把 retired 的 to_label 重新上線、目前 published 改 retired（同一交易）。"""
    return switch(conn, doc_key, to_label, actor=actor, mode="rollback", index_label=index_label,
                  accept_candidate_impact=accept_candidate_impact, reason=reason)


# ---------------------------------------------------------------------------
# 4. 停用、恢復、可見範圍、有效期間（立即生效：檢索、推薦、歷史查看都即時重查）
# ---------------------------------------------------------------------------
def _doc_change(conn: psycopg.Connection, doc_key: str, event_type: str, sql: str, params: tuple, *,
                actor: str, reason: str | None, before_cols: tuple[str, ...]) -> dict:
    """在一個交易內改文件欄位並寫事件；回傳變更前後的值與受影響候選。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ingest:" + doc_key,))
        doc = _document(conn, doc_key)
        before = {k: doc[k] for k in before_cols}
        conn.execute(sql, (*params, doc["id"]))
        after_row = _document(conn, doc_key)
        after = {k: after_row[k] for k in before_cols}
        current = _published(conn, doc["id"])
        impact = [r["option_key"] for r in candidate_impact(conn, [current["id"]] if current else [])]
        _event(conn, doc["id"], event_type, actor=actor, reason=reason,
               detail={"before": _jsonable(before), "after": _jsonable(after), "candidate_impact": impact})
    return {"doc_key": doc_key, "event": event_type, "before": _jsonable(before), "after": _jsonable(after),
            "candidate_impact": impact}


# 把 datetime 轉成 ISO 字串，讓事件 detail 可存成 JSON。
def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def deactivate(conn: psycopg.Connection, doc_key: str, *, actor: str, reason: str) -> dict:
    """停用來源文件：所有版本立即不可檢索、不可當推薦證據；歷史回答引用到它的會被遮蔽。"""
    if not (reason and len(reason.strip()) >= 2):
        raise ReleaseError("停用必須填寫原因（--reason）")
    return _doc_change(conn, doc_key, "deactivate", "UPDATE documents SET is_active = FALSE WHERE id = %s", (),
                       actor=actor, reason=reason, before_cols=("is_active",))


def reactivate(conn: psycopg.Connection, doc_key: str, *, actor: str, reason: str) -> dict:
    """恢復來源文件（目前 published 版本與其已核准片段重新可用）。"""
    if not (reason and len(reason.strip()) >= 2):
        raise ReleaseError("恢復必須填寫原因（--reason）")
    return _doc_change(conn, doc_key, "reactivate", "UPDATE documents SET is_active = TRUE WHERE id = %s", (),
                       actor=actor, reason=reason, before_cols=("is_active",))


def set_visibility(conn: psycopg.Connection, doc_key: str, visibility: str, *, actor: str,
                   reason: str | None = None) -> dict:
    """設定可見範圍（all／admin）。改成 admin 後一般使用者立即看不到，包含歷史回答中的引用。"""
    if visibility not in ("all", "admin"):
        raise ReleaseError("visibility 只能是 all 或 admin")
    return _doc_change(conn, doc_key, "visibility", "UPDATE documents SET visibility = %s WHERE id = %s",
                       (visibility,), actor=actor, reason=reason, before_cols=("visibility",))


def set_validity(conn: psycopg.Connection, doc_key: str, valid_from: datetime | None, valid_until: datetime | None,
                 *, actor: str, reason: str | None = None) -> dict:
    """設定有效期間（NULL 表示不限）。期間外的文件不可檢索、不可當證據。"""
    if valid_from and valid_until and valid_until <= valid_from:
        raise ReleaseError("valid_until 必須晚於 valid_from")
    return _doc_change(conn, doc_key, "validity",
                       "UPDATE documents SET valid_from = %s, valid_until = %s WHERE id = %s",
                       (valid_from, valid_until), actor=actor, reason=reason,
                       before_cols=("valid_from", "valid_until"))


# ---------------------------------------------------------------------------
# 5. 歷程
# ---------------------------------------------------------------------------
def history(conn: psycopg.Connection, doc_key: str) -> list[dict]:
    """一份文件的發布／停用事件（由舊到新），版本與索引以標籤顯示。"""
    doc = _document(conn, doc_key)
    return conn.execute(
        """SELECT e.id, e.event_type, fv.version_label AS from_version, tv.version_label AS to_version,
                  ib.label AS index_before, ia.label AS index_after, e.actor, e.reason, e.detail, e.created_at
             FROM document_release_events e
             LEFT JOIN document_versions fv ON fv.id = e.from_version_id
             LEFT JOIN document_versions tv ON tv.id = e.to_version_id
             LEFT JOIN index_versions ib ON ib.id = e.index_before_id
             LEFT JOIN index_versions ia ON ia.id = e.index_after_id
            WHERE e.document_id = %s ORDER BY e.id""", (doc["id"],)).fetchall()


def versions(conn: psycopg.Connection, doc_key: str) -> list[dict]:
    """一份文件的所有版本狀態與片段統計（決定要切回哪一版時用）。"""
    doc = _document(conn, doc_key)
    return conn.execute(
        """SELECT dv.version_label, dv.status, dv.published_at, dv.retired_at,
                  count(c.id) FILTER (WHERE c.review_status = 'approved' AND c.is_active) AS approved,
                  count(c.id) FILTER (WHERE c.review_status = 'pending') AS pending,
                  count(c.id) FILTER (WHERE c.review_status = 'rejected') AS rejected
             FROM document_versions dv LEFT JOIN chunks c ON c.document_version_id = dv.id
            WHERE dv.document_id = %s GROUP BY dv.id ORDER BY dv.id""", (doc["id"],)).fetchall()


# ---------------------------------------------------------------------------
# 指令列
# ---------------------------------------------------------------------------
def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("請設定 DATABASE_URL")
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# 解析 ISO 時間（例如 2026-12-31T00:00:00+08:00）；空字串或 none 表示不限。
def _when(text: str | None) -> datetime | None:
    if text is None or text.strip().lower() in ("", "none", "null"):
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ReleaseError(f"時間必須帶時區：{text}")
    return value


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.release", description="S9 版本發布、停用與切回舊版")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def doc_cmd(name: str, help_text: str, *, version: bool = False, operator: bool = False):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--doc", required=True)
        if version:
            p.add_argument("--version", required=True)
        if operator:
            p.add_argument("--operator", default=None, help="操作人標示（預設為作業系統帳號）")
        return p

    doc_cmd("versions", "列出文件各版本狀態")
    doc_cmd("history", "列出發布／停用事件")
    p = doc_cmd("plan", "切換前檢查（唯讀）", version=True)
    p.add_argument("--mode", choices=MODES, default="publish")
    p.add_argument("--allow-empty", action="store_true")
    p = doc_cmd("prepare", "建立新的 building 索引（需要嵌入模型）", version=True, operator=True)
    p.add_argument("--mode", choices=MODES, default="publish")
    p.add_argument("--index", help="新索引標籤（省略則自動產生）")
    p = doc_cmd("switch", "驗證並切換到新版本（單一交易）", version=True, operator=True)
    p.add_argument("--index", help="prepare 產生的索引標籤；沒有 active 索引時不要給")
    p.add_argument("--accept-candidate-impact", action="store_true")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--reason")
    p = doc_cmd("rollback", "安全切回 retired 舊版（單一交易）", version=True, operator=True)
    p.add_argument("--index")
    p.add_argument("--accept-candidate-impact", action="store_true")
    p.add_argument("--reason", required=True)
    for name, text in (("deactivate", "停用文件"), ("reactivate", "恢復文件")):
        p = doc_cmd(name, text, operator=True)
        p.add_argument("--reason", required=True)
    p = doc_cmd("set-visibility", "設定可見範圍", operator=True)
    p.add_argument("--visibility", required=True, choices=["all", "admin"])
    p.add_argument("--reason")
    p = doc_cmd("set-validity", "設定有效期間（ISO 時間含時區；none = 不限）", operator=True)
    p.add_argument("--from", dest="valid_from")
    p.add_argument("--until", dest="valid_until")
    p.add_argument("--reason")
    args = ap.parse_args(argv)
    actor = f"cli:{getattr(args, 'operator', None) or getpass.getuser()}"

    try:
        with _connect() as conn:
            if args.cmd == "versions":
                _print(versions(conn, args.doc))
            elif args.cmd == "history":
                _print(history(conn, args.doc))
            elif args.cmd == "plan":
                out = plan(conn, args.doc, args.version, mode=args.mode, allow_empty=args.allow_empty)
                out.pop("_ids")
                _print(out)
            elif args.cmd == "prepare":
                _print(prepare(conn, args.doc, args.version, retrieval.get_embedder(), actor=actor,
                               mode=args.mode, index_label=args.index))
            elif args.cmd == "switch":
                _print(switch(conn, args.doc, args.version, actor=actor, index_label=args.index,
                              accept_candidate_impact=args.accept_candidate_impact,
                              allow_empty=args.allow_empty, reason=args.reason))
            elif args.cmd == "rollback":
                _print(rollback(conn, args.doc, args.version, actor=actor, reason=args.reason,
                                index_label=args.index, accept_candidate_impact=args.accept_candidate_impact))
            elif args.cmd == "deactivate":
                _print(deactivate(conn, args.doc, actor=actor, reason=args.reason))
            elif args.cmd == "reactivate":
                _print(reactivate(conn, args.doc, actor=actor, reason=args.reason))
            elif args.cmd == "set-visibility":
                _print(set_visibility(conn, args.doc, args.visibility, actor=actor, reason=args.reason))
            elif args.cmd == "set-validity":
                _print(set_validity(conn, args.doc, _when(args.valid_from), _when(args.valid_until),
                                    actor=actor, reason=args.reason))
    except (ReleaseError, retrieval.RetrievalError, psycopg.Error, ValueError) as exc:
        sys.exit(f"失敗（線上版本未變動）：{exc}")


if __name__ == "__main__":
    main()
