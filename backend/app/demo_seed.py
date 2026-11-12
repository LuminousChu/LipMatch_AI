"""SYNTHETIC 展示種子：只供本機展示／測試環境，建立合成文件、核准、發布並啟用合成候選。

  python -m app.demo_seed --admin <管理者帳號> --password-stdin < 密碼檔
  python -m app.demo_seed --admin <管理者帳號>                     # 互動輸入密碼
  python -m app.demo_seed --admin <管理者帳號> --file eval/s7_synthetic_candidates.json

做的事（全部冪等，重跑不重複建立）：
1. 驗證管理者帳號密碼（與 S3 審核相同）。
2. 讀合成候選檔（預設 eval/s7_synthetic_candidates.json，檔案必須 synthetic=true），
   只接受 SYN_ 開頭的 doc_key，其他一律拒絕、什麼都不寫。
3. 建立 SYN_ 文件（visibility=all、可進 RAG、bibliography 標 synthetic）與 v1 版本；
   依候選檔的 chunk_key 建片段，文字固定為「SYNTHETIC 合成證據（展示用，非正式來源）：<chunk_key>」
   （e2e/tests/flow.spec.ts 依賴這個字樣）。
4. 以資料庫函式 review_chunk 逐筆核准，理由標明 SYNTHETIC 展示資料、非正式核准；不碰任何非 SYN_ 片段。
5. 發布 v1，再以 candidates.load_spec／activate（allow_synthetic=True）啟用合成候選。

不是正式驗收：合成文件與候選只證明流程照設計運作。正式環境不要執行（會在推薦結果出現合成方向）。
評估檔 backend/eval/ 不在映像檔內，容器內執行時要掛載：
  docker compose run --rm -v <專案>/backend/eval:/app/eval:ro backend python -m app.demo_seed --admin ops --password-stdin
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import candidates as gov
from . import ingest
from .rec_cases import SYNTHETIC_FILE

DOC_PREFIX = "SYN_"
VERSION_LABEL = "v1"
ETL_VERSION = "synthetic-demo-seed"
CHUNK_TEXT = "SYNTHETIC 合成證據（展示用，非正式來源）：{chunk_key}"
REVIEW_REASON = "SYNTHETIC 展示資料（非正式核准）：demo_seed 建立的合成片段，只供本機展示與測試"
BIBLIOGRAPHY = {"synthetic": True, "created_by": "app.demo_seed",
                "note": "SYNTHETIC 合成展示資料，不是真實文獻，不可作正式證據或正式驗收"}


class SeedError(Exception):
    """種子資料無法建立（輸入不合規、或資料庫既有內容與種子不一致）。"""


# 片段固定文字與其內容指紋（審核時用來比對內容沒被改過）。
def chunk_text(chunk_key: str) -> tuple[str, str]:
    text = CHUNK_TEXT.format(chunk_key=chunk_key)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# 從候選檔整理出「文件 → 依出現順序、不重複的 chunk_key」；任何不合規就整份拒絕。
def plan_documents(spec: dict) -> dict[str, list[str]]:
    if spec.get("synthetic") is not True:
        raise SeedError("候選檔不是 synthetic=true；展示種子只接受合成候選檔")
    errs = gov.validate_spec(spec)
    if errs:
        raise SeedError("候選檔驗證失敗：\n" + "\n".join(errs))
    docs: dict[str, list[str]] = {}
    bad: set[str] = set()
    for c in spec["candidates"]:
        for e in c["evidence"]:
            if not e["doc_key"].startswith(DOC_PREFIX):
                bad.add(e["doc_key"])
            elif e["version_label"] != VERSION_LABEL:
                raise SeedError(f"{e['chunk_key']} 的版本是 {e['version_label']}；展示種子只建 {VERSION_LABEL}")
            keys = docs.setdefault(e["doc_key"], [])
            if e["chunk_key"] not in keys:
                keys.append(e["chunk_key"])
    if bad:
        raise SeedError(f"只接受 {DOC_PREFIX} 開頭的合成文件，拒絕：{sorted(bad)}（未寫入任何資料）")
    return docs
# 核心邏輯：先完整檢查再寫入；只要有一個 doc_key 不是 SYN_ 開頭，就不建立任何東西，
# 避免展示種子被拿來替真實文獻建片段或核准真實片段（規範 4：不批量放行）。


# 建立（或取得）一份合成文件；既有同名文件若不是本指令建立的合成文件就拒絕。
def _ensure_document(conn: psycopg.Connection, doc_key: str) -> tuple[int, str]:
    row = conn.execute(
        "INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible, bibliography) "
        "VALUES (%s, %s, 'other', 'all', TRUE, TRUE, %s) ON CONFLICT (doc_key) DO NOTHING RETURNING id",
        (doc_key, f"SYNTHETIC 展示資料 {doc_key}", Jsonb(BIBLIOGRAPHY))).fetchone()
    if row:
        return row["id"], "created"
    row = conn.execute("SELECT id, bibliography FROM documents WHERE doc_key = %s", (doc_key,)).fetchone()
    if row["bibliography"].get("synthetic") is not True:
        raise SeedError(f"{doc_key} 已存在但不是合成展示文件（bibliography 沒有 synthetic=true），不覆寫")
    return row["id"], "existing"
# 核心邏輯：doc_type 不能用 'synthetic'（資料庫限制 synthetic 文件不可進 RAG），
# 所以合成身分記在 title 與 bibliography.synthetic；重跑時靠這個標記確認是自己建的。


# 建立（或取得）v1 版本，回傳 (version_id, status)。
def _ensure_version(conn: psycopg.Connection, document_id: int, doc_key: str) -> tuple[int, str]:
    row = conn.execute("SELECT id, status FROM document_versions WHERE document_id = %s AND version_label = %s",
                       (document_id, VERSION_LABEL)).fetchone()
    if row:
        return row["id"], row["status"]
    sha = hashlib.sha256(f"synthetic-demo:{doc_key}".encode("utf-8")).hexdigest()
    row = conn.execute(
        "INSERT INTO document_versions (document_id, version_label, source_sha256, source_filename, etl_version) "
        "VALUES (%s, %s, %s, NULL, %s) RETURNING id, status",
        (document_id, VERSION_LABEL, sha, ETL_VERSION)).fetchone()
    return row["id"], row["status"]


# 建立缺少的片段並逐筆核准；回傳 (新建數, 新核准數)。既有片段內容不同或已被駁回時拒絕。
def _ensure_chunks(conn: psycopg.Connection, version_id: int, version_status: str, doc_key: str,
                   chunk_keys: list[str], admin_id: int) -> tuple[int, int]:
    created = approved = 0
    for ordinal, key in enumerate(chunk_keys):
        text, sha = chunk_text(key)
        row = conn.execute("SELECT id, content_sha256, review_status FROM chunks "
                           "WHERE document_version_id = %s AND chunk_key = %s", (version_id, key)).fetchone()
        if row is None:
            if version_status != "draft":
                raise SeedError(f"{doc_key} {VERSION_LABEL} 已是 {version_status}，不能補片段 {key}（發布版不可覆寫）")
            row = conn.execute(
                "INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text, "
                "content_sha256, attributes) VALUES (%s, %s, %s, 1, %s, %s, %s, %s) "
                "RETURNING id, content_sha256, review_status",
                (version_id, key, ordinal, text, text, sha, Jsonb({"synthetic": True}))).fetchone()
            created += 1
        if row["content_sha256"] != sha:
            raise SeedError(f"{doc_key} {key} 的內容與展示種子不同，不覆寫")
        if row["review_status"] == "rejected":
            raise SeedError(f"{doc_key} {key} 已被駁回；不自動改判，請人工處理")
        if row["review_status"] == "pending":
            outcome = conn.execute("SELECT review_chunk(%s, %s, 'approve', %s, %s) AS r",
                                   (row["id"], admin_id, REVIEW_REASON, sha)).fetchone()["r"]
            approved += outcome == "approved"
    return created, approved
# 核心邏輯：核准一律走資料庫函式 review_chunk（寫 chunk_review_events、檢查內容指紋與審核人是管理者），
# 每筆一個呼叫並帶 SYNTHETIC 理由；不直接 UPDATE review_status。只處理本指令建立的 SYN_ 片段。


# 執行整個種子流程，回傳摘要；admin_id 須已通過密碼驗證。
def seed(conn: psycopg.Connection, spec: dict, admin_id: int) -> dict:
    plan = plan_documents(spec)
    docs_out = []
    with conn.transaction():
        for doc_key, chunk_keys in plan.items():
            document_id, doc_result = _ensure_document(conn, doc_key)
            version_id, status = _ensure_version(conn, document_id, doc_key)
            created, approved = _ensure_chunks(conn, version_id, status, doc_key, chunk_keys, admin_id)
            published = False
            if status == "draft":
                ingest.publish(conn, doc_key, VERSION_LABEL)
                published = True
            elif status == "retired":
                raise SeedError(f"{doc_key} {VERSION_LABEL} 已退役，展示種子不會重新發布")
            docs_out.append({"doc_key": doc_key, "document": doc_result, "chunks": len(chunk_keys),
                             "chunks_created": created, "chunks_approved": approved,
                             "published_now": published})
    loaded = gov.load_spec(conn, spec, admin_id, allow_synthetic=True)
    cands_out = []
    for item in loaded:
        if item["status"] == "draft":
            gov.activate(conn, item["option_key"], admin_id, allow_synthetic=True)
            cands_out.append({"option_key": item["option_key"], "result": "activated"})
        else:
            cands_out.append({"option_key": item["option_key"], "result": f"unchanged ({item['status']})"})
    return {"synthetic": True, "note": "SYNTHETIC 展示資料，非正式來源、非正式驗收",
            "documents": docs_out, "candidates": cands_out}
# 核心邏輯：
# 1. 文件／片段／核准／發布在同一個交易，任何一步失敗全部不生效。
# 2. 候選沿用 S7 治理：load_spec 內容相同回 unchanged；draft 才啟用；已退役的不會被重新啟用。
# 3. 每一步都先查再建，所以重跑只會回報 existing／unchanged，不會多出資料或審核事件。


# 讀管理者密碼（只從 stdin 或互動輸入，不放參數）。
def _read_password(admin: str, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(f"{admin} 的密碼：")


# 命令列入口。
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.demo_seed",
                                 description="SYNTHETIC 展示種子（只供本機展示／測試環境）")
    ap.add_argument("--admin", required=True, help="管理者帳號（密碼另外輸入）")
    ap.add_argument("--password-stdin", action="store_true")
    ap.add_argument("--file", type=Path, default=SYNTHETIC_FILE, help="合成候選檔（預設 eval/s7_synthetic_candidates.json）")
    args = ap.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("需要環境變數 DATABASE_URL")
    if not args.file.is_file():
        sys.exit(f"找不到合成候選檔 {args.file}；容器內請掛載 backend/eval（見本檔說明）")
    try:
        spec = json.loads(args.file.read_text(encoding="utf-8"))
        plan_documents(spec)   # 先檢查輸入，再要求密碼
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            admin_id = ingest.authenticate_admin(conn, args.admin, _read_password(args.admin, args.password_stdin))
            result = seed(conn, spec, admin_id)
    except (SeedError, gov.CandidateError, ingest.IngestError, psycopg.Error, json.JSONDecodeError) as exc:
        sys.exit(f"失敗：{exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
