"""S7 推薦候選的載入與治理（B 線核對後的候選 → 資料庫 draft → 管理者啟用 → 推薦使用）。

    B 線填寫的候選檔（cand-v1 JSON）
        │  validate：屬性必須在詞彙表內；每個宣告的屬性值都要有證據的 supports 對應；
        │            證據必須指定文件、版本、片段；合成資料必須標示
        ▼
    load（管理者）→ recommendation_options（draft）＋ recommendation_option_evidence
        │  已啟用／已退役的候選內容不可覆寫，要改就用新的 option_key（發布版不可覆寫）
        ▼
    activate（管理者）：全部證據此刻都可用（核准、已發布版本、文件啟用、有效期間），
        │                每個屬性值至少有一筆可用證據 → status = active
        ▼
    推薦時 app.recommend 仍會逐筆重新檢查證據；之後來源被停用，候選會自動降級或被排除。

    status：列出每個候選的證據有效數、失效原因、目前會失效的屬性（治理報表）。
    retire（管理者）：停止使用，不刪除（保留 request_recommendations 的追溯）。

這個模組不產生任何搭配規則：沒有人工填寫的候選，就沒有推薦（交付 docs/mvp/s7-candidate-fill-template.md 待填）。

用法：
    python -m app.candidates validate --file app/recommendation_candidates.json
    python -m app.candidates load --file <檔案> --admin <管理者帳號>
    python -m app.candidates activate --key <option_key> --admin <管理者帳號>
    python -m app.candidates retire --key <option_key> --admin <管理者帳號>
    python -m app.candidates status
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import preferences as pref
from . import recommend as rec
from .ingest import IngestError, authenticate_admin

FILE_STATUSES = ("awaiting_b_line", "verified", "synthetic_test")
_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
_MAX_CLAIM = 300
# 屬性的允許值：色系／妝感／效果來自 S6 詞彙表，場合與資料表 CHECK 相同，膚色只接受詞彙表裡的自述膚色詞
_ALLOWED = {
    "color_family": {k for (kind, k) in pref.VOCAB["labels"] if kind == "color"},
    "finishes": {k for (kind, k) in pref.VOCAB["labels"] if kind == "finish"},
    "effects": {k for (kind, k) in pref.VOCAB["labels"] if kind == "effect"},
    "occasions": set(pref.OCCASIONS),
    "skin_tones": set(pref.VOCAB["skin_tones"]),
    "undertones": set(pref.UNDERTONE_LABELS),   # cand-v2：來源支持的適用使用者底調（cool／warm／neutral）
}
# cand-v2 起才有的屬性；cand-v1 檔案出現就拒絕（v1 沒有底調語意，沒有＝未知）
_V2_ONLY_ATTRS = ("undertones",)
_VERIFICATION_KEYS = ("reviewer", "reviewer_type", "delegated_by", "delegation_basis", "checks")


class CandidateError(RuntimeError):
    """候選檔或治理操作不合規則；訊息會列出全部問題，整批不生效。"""


# ---------------------------------------------------------------------------
# 檔案驗證（不連資料庫）
# ---------------------------------------------------------------------------
# 讀取並驗證候選檔；有任何錯誤就整份拒絕。
def read_spec(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateError(f"讀不到候選檔：{exc}") from exc
    errors = validate_spec(data)
    if errors:
        raise CandidateError("候選檔有錯，未載入任何一筆：\n" + "\n".join(errors))
    return data


# 檢查候選檔格式與「每個屬性都有證據」的規則；回傳錯誤清單（空清單代表通過）。
def validate_spec(data: dict) -> list[str]:
    errors: list[str] = []
    schema = data.get("schema_version")
    if schema not in rec.SUPPORTED_CANDIDATE_SCHEMAS:   # cand-v2 明確接受 cand-v1（原規則驗證、不轉換內容）
        errors.append(f"schema_version 必須是 {rec.SUPPORTED_CANDIDATE_SCHEMAS} 之一")
    status = data.get("status")
    if status not in FILE_STATUSES:
        errors.append(f"status 必須是 {FILE_STATUSES} 之一")
    synthetic = data.get("synthetic")
    if not isinstance(synthetic, bool):
        errors.append("synthetic 必須是 true／false")
    if (status == "synthetic_test") != (synthetic is True):
        errors.append("只有 status = synthetic_test 的檔案可以（而且必須）標 synthetic = true")
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return errors + ["candidates 必須是陣列"]
    if status == "awaiting_b_line" and cands:
        errors.append("status = awaiting_b_line 的檔案不能有候選；B 線核對完成後改成 verified")
    seen: set[str] = set()
    for i, c in enumerate(cands):
        errors += [f"candidates[{i}]（{c.get('option_key', '?')}）：{e}"
                   for e in _validate_candidate(c, synthetic, schema=schema)]
        if c.get("option_key") in seen:
            errors.append(f"candidates[{i}]：option_key 重複：{c.get('option_key')}")
        seen.add(c.get("option_key"))
    return errors
# 核心邏輯：檔案狀態分三種——等待 B 線（必須是空的）、已核對（正式）、合成測試（必須標 synthetic）；
# 不會有「沒標示的合成資料」或「沒核對卻當正式」的檔案被載入。


# 驗證單一候選。
def _validate_candidate(c: dict, synthetic: bool, *, schema: str | None = None) -> list[str]:
    """schema＝cand-v1：沿用原規則，不得有 cand-v2 才有的屬性（undertones）與 verification。
    cand-v2：undertones 可有可無（沒寫＝未知）；verification 記核對者真實身分、類型與委託依據。"""
    errs: list[str] = []
    attrs0 = c.get("attributes") or {}
    if schema == "cand-v1":
        bad = [a for a in _V2_ONLY_ATTRS if a in attrs0]
        if bad:
            errs.append(f"cand-v1 不支援屬性 {bad}；要標底調請改用 cand-v2")
        if "verification" in c:
            errs.append("cand-v1 不支援 verification；請改用 cand-v2")
    elif "verification" in c:
        ver = c["verification"]
        if not isinstance(ver, dict) or any(not ver.get(k) for k in ("reviewer", "reviewer_type")):
            errs.append("verification 必須是物件，且有 reviewer（真實身分）與 reviewer_type（human／ai）")
        elif ver["reviewer_type"] not in ("human", "ai"):
            errs.append("verification.reviewer_type 只能是 human 或 ai")
        elif ver["reviewer_type"] == "ai" and not (ver.get("delegated_by") and ver.get("delegation_basis")):
            errs.append("AI 核對必須記錄 delegated_by 與 delegation_basis（委託依據）")
        extra_v = set(ver) - set(_VERIFICATION_KEYS) if isinstance(ver, dict) else set()
        if extra_v:
            errs.append(f"verification 有未定義的欄位：{sorted(extra_v)}")
    key = c.get("option_key") or ""
    if not _KEY_RE.match(key):
        errs.append("option_key 只能用小寫英數、底線、連字號")
    if synthetic != key.startswith("synthetic-"):
        errs.append("合成候選的 option_key 必須以 synthetic- 開頭；正式候選不可以")
    for f in ("name", "description"):
        if not str(c.get(f) or "").strip():
            errs.append(f"{f} 不可空白")
    if not synthetic:
        if not str(c.get("verified_by") or "").strip() or not str(c.get("verified_at") or "").strip():
            errs.append("正式候選必須填 verified_by（核對人）與 verified_at（核對日期）")
    attrs = c.get("attributes") or {}
    declared: set[str] = set()
    color = attrs.get("color_family")
    if color not in _ALLOWED["color_family"]:
        errs.append(f"attributes.color_family 必須是詞彙表色系鍵之一：{sorted(_ALLOWED['color_family'])}")
    else:
        declared.add(f"color_family:{color}")
    for attr in rec.LIST_ATTRS:
        values = attrs.get(attr, [])
        if not isinstance(values, list):
            errs.append(f"attributes.{attr} 必須是陣列（未知就給空陣列）")
            continue
        bad = [v for v in values if v not in _ALLOWED[attr]]
        if bad:
            errs.append(f"attributes.{attr} 有不在允許清單的值：{bad}")
        declared |= {f"{attr}:{v}" for v in values if v in _ALLOWED[attr]}
    extra = set(attrs) - {"color_family", *rec.LIST_ATTRS}
    if extra:
        errs.append(f"attributes 有未定義的欄位：{sorted(extra)}")
    if not isinstance(c.get("limitations", []), list):
        errs.append("limitations 必須是陣列")
    evidence = c.get("evidence") or []
    if not evidence:
        errs.append("至少要有一筆證據")
    covered: set[str] = set()
    for j, e in enumerate(evidence):
        for f in ("doc_key", "version_label", "chunk_key", "claim"):
            if not str(e.get(f) or "").strip():
                errs.append(f"evidence[{j}].{f} 不可空白")
        if len(str(e.get("claim") or "")) > _MAX_CLAIM:
            errs.append(f"evidence[{j}].claim 超過 {_MAX_CLAIM} 字（只寫可被片段直接支持的一句主張）")
        if e.get("support_type") not in rec.EVIDENCE_TYPES:
            errs.append(f"evidence[{j}].support_type 必須是 {rec.EVIDENCE_TYPES} 之一")
        supports = e.get("supports") or []
        if not supports:
            errs.append(f"evidence[{j}].supports 不可空白（這筆證據支持哪個屬性值）")
        unknown = [s for s in supports if s not in declared]
        if unknown:
            errs.append(f"evidence[{j}].supports 指向未宣告的屬性值：{unknown}")
        covered |= set(supports)
    missing = sorted(declared - covered)
    if missing:
        errs.append(f"這些屬性值沒有任何證據支持（沒有證據就不能宣告）：{missing}")
    return errs
# 核心邏輯（規範 2、規則 G03）：宣告的每個屬性值都必須被至少一筆證據的 supports 指到；
# 反過來，證據也只能支持已宣告的屬性。這樣「憑記憶寫上去的屬性」無法通過驗證。


# 候選內容指紋：同樣內容重複載入不建立新資料；已啟用候選內容不同時拒絕覆寫。
def spec_fingerprint(c: dict) -> str:
    keep = {k: c.get(k) for k in ("option_key", "name", "description", "attributes", "limitations", "evidence")}
    return hashlib.sha256(json.dumps(keep, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 資料庫操作
# ---------------------------------------------------------------------------
# 把證據的（文件、版本、片段）對到 chunks.id；文件必須可進 RAG。
def _resolve_chunk(conn: psycopg.Connection, e: dict) -> tuple[int | None, str | None]:
    row = conn.execute(
        """SELECT c.id, d.rag_eligible FROM chunks c
             JOIN document_versions dv ON dv.id = c.document_version_id
             JOIN documents d ON d.id = dv.document_id
            WHERE d.doc_key = %s AND dv.version_label = %s AND c.chunk_key = %s""",
        (e["doc_key"], e["version_label"], e["chunk_key"])).fetchone()
    if row is None:
        return None, f"找不到片段 {e['doc_key']} {e['version_label']} {e['chunk_key']}"
    if not row["rag_eligible"]:
        return None, f"{e['doc_key']} 不可進 RAG（研究定稿、題組、示範文章等不得作為證據）"
    return row["id"], None


# 載入候選檔：新候選建立為 draft；draft 可整筆替換；active／retired 內容不同則拒絕。全部在同一個交易。
def load_spec(conn: psycopg.Connection, data: dict, admin_id: int, *, allow_synthetic: bool = False) -> list[dict]:
    if data.get("synthetic") and not allow_synthetic:
        raise CandidateError("這是合成候選檔；只有測試或展示演算法時才能加 --allow-synthetic 載入")
    results: list[dict] = []
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    with conn.transaction():
        for c in data["candidates"]:
            chunk_ids = []
            for e in c["evidence"]:
                cid, err = _resolve_chunk(conn, e)
                if err:
                    errors.append(f"{c['option_key']}：{err}")
                chunk_ids.append(cid)
            if errors:
                continue
            fp = spec_fingerprint(c)
            existing = conn.execute("SELECT id, status, attributes FROM recommendation_options WHERE option_key = %s",
                                    (c["option_key"],)).fetchone()
            if existing and existing["attributes"].get("content_sha256") == fp:
                results.append({"option_key": c["option_key"], "result": "unchanged", "status": existing["status"]})
                continue
            if existing and existing["status"] != "draft":
                errors.append(f"{c['option_key']} 已是 {existing['status']}，內容不可覆寫；請用新的 option_key，"
                              "啟用新版後再退役舊版")
                continue
            attrs = {**c["attributes"], "schema_version": data.get("schema_version") or rec.CANDIDATE_SCHEMA_VERSION,
                     "limitations": c.get("limitations", []), "synthetic": bool(data.get("synthetic")),
                     "verified_by": c.get("verified_by"), "verified_at": c.get("verified_at"),
                     **({"verification": c["verification"]} if c.get("verification") else {}),
                     "content_sha256": fp,
                     "evidence_map": [{"chunk_id": cid, "claim": e["claim"], "supports": e["supports"]}
                                      for cid, e in zip(chunk_ids, c["evidence"])],
                     "governance": {"loaded_by": admin_id, "loaded_at": now}}
            if existing:
                option_id = existing["id"]
                conn.execute("UPDATE recommendation_options SET name = %s, description = %s, attributes = %s "
                             "WHERE id = %s", (c["name"], c["description"], Jsonb(attrs), option_id))
                conn.execute("DELETE FROM recommendation_option_evidence WHERE option_id = %s", (option_id,))
                result = "replaced_draft"
            else:
                option_id = conn.execute(
                    "INSERT INTO recommendation_options (option_key, name, description, attributes) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (c["option_key"], c["name"], c["description"], Jsonb(attrs))).fetchone()["id"]
                result = "created_draft"
            for cid, e in zip(chunk_ids, c["evidence"]):
                conn.execute(
                    "INSERT INTO recommendation_option_evidence (option_id, chunk_id, support_type, claim, created_by) "
                    "VALUES (%s, %s, %s, %s, %s)", (option_id, cid, e["support_type"], e["claim"], admin_id))
            results.append({"option_key": c["option_key"], "result": result, "status": "draft"})
        if errors:
            raise CandidateError("載入失敗，全部未生效：\n" + "\n".join(errors))
    return results
# 核心邏輯：
# 1. 先把每筆證據對到資料庫片段（找不到或文件不可進 RAG 就整批失敗）。
# 2. 內容指紋相同 → unchanged（冪等）；已啟用或退役的候選不可改內容（規範 3：發布版不可覆寫）。
# 3. 新載入一律是 draft，不會自動啟用；證據對應（哪筆證據支持哪個屬性）存在 attributes.evidence_map。


# 啟用候選：必須是 draft、所有證據此刻可用、每個屬性值至少有一筆可用證據。
def activate(conn: psycopg.Connection, option_key: str, admin_id: int, *, allow_synthetic: bool = False) -> dict:
    cands = rec.load_candidates(conn, "admin", statuses=("draft", "active", "retired"))
    cand = next((c for c in cands if c.option_key == option_key), None)
    if cand is None:
        raise CandidateError(f"找不到候選：{option_key}")
    if cand.status != "draft":
        raise CandidateError(f"{option_key} 狀態是 {cand.status}，只有 draft 可以啟用")
    if cand.synthetic and not allow_synthetic:
        raise CandidateError("合成候選只能在測試環境以 --allow-synthetic 啟用")
    problems = [f"證據失效：{e.doc_key} {e.chunk_key}（{e.invalid_reason}）" for e in cand.evidence if not e.valid]
    unsupported = _unsupported_tokens(cand)
    problems += [f"屬性沒有可用證據：{t}" for t in unsupported]
    if not cand.evidence:
        problems.append("沒有任何證據")
    if problems:
        raise CandidateError(f"{option_key} 不能啟用：\n" + "\n".join(problems))
    _set_status(conn, cand.option_id, "active", admin_id)
    return {"option_key": option_key, "status": "active", "evidence": len(cand.evidence)}
# 核心邏輯（規則 G04）：啟用時要求「全部」證據都已核准且在已發布版本中，而不是只要一筆；
# 這樣 draft 狀態的片段、待審片段不會混進正式候選。啟用後若來源才失效，由推薦時的逐筆檢查處理。


# 宣告了但目前沒有任何可用證據支持的屬性值。
def _unsupported_tokens(cand: rec.Candidate) -> list[str]:
    declared = [f"color_family:{cand.attributes.get('color_family')}"]
    declared += [f"{a}:{v}" for a in rec.LIST_ATTRS for v in cand.attributes.get(a) or []]
    valid = {t for e in cand.evidence if e.valid for t in e.supports}
    return [t for t in declared if t not in valid]


# 退役候選（不刪除，保留歷史推薦的追溯）。
def retire(conn: psycopg.Connection, option_key: str, admin_id: int) -> dict:
    row = conn.execute("SELECT id, status FROM recommendation_options WHERE option_key = %s",
                       (option_key,)).fetchone()
    if row is None:
        raise CandidateError(f"找不到候選：{option_key}")
    if row["status"] == "retired":
        return {"option_key": option_key, "status": "retired", "result": "unchanged"}
    _set_status(conn, row["id"], "retired", admin_id)
    return {"option_key": option_key, "status": "retired", "result": "retired"}


# 更新候選狀態，並在 attributes.governance 記下操作者與時間（內容指紋不變）。
def _set_status(conn: psycopg.Connection, option_id: int, status: str, admin_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE recommendation_options
              SET status = %s,
                  attributes = jsonb_set(attributes, '{governance}',
                                         COALESCE(attributes->'governance', '{}'::jsonb) || %s)
            WHERE id = %s""",
        (status, Jsonb({f"{status}_by": admin_id, f"{status}_at": now}), option_id))


# 治理報表：每個候選的狀態、證據有效數、失效原因、目前失效的屬性（以管理者可見範圍檢查）。
def governance_status(conn: psycopg.Connection) -> list[dict]:
    out = []
    for c in rec.load_candidates(conn, "admin", statuses=("draft", "active", "retired")):
        invalid = [{"doc_key": e.doc_key, "chunk_key": e.chunk_key, "reason": e.invalid_reason, "supports": e.supports}
                   for e in c.evidence if not e.valid]
        unsupported = _unsupported_tokens(c)
        out.append({"option_key": c.option_key, "name": c.name, "status": c.status, "synthetic": c.synthetic,
                    "evidence_total": len(c.evidence), "evidence_valid": len(c.evidence) - len(invalid),
                    "invalid_evidence": invalid, "unsupported_attributes": unsupported,
                    "usable_for_recommendation": c.status == "active" and c.verified()["color_family"] is not None,
                    "degraded": c.status == "active" and bool(unsupported)})
    return out
# 核心邏輯：active 但有屬性失去證據 → degraded（推薦時該屬性視為未知）；色系失去證據 → 不會被推薦。


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# 連線到 DATABASE_URL（與 ingest 相同）。
def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise CandidateError("需要環境變數 DATABASE_URL")
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


# 讀管理者密碼並驗證（密碼只從 stdin 或互動輸入，不放參數）。
def _admin(conn: psycopg.Connection, args) -> int:
    password = (sys.stdin.readline().rstrip("\r\n") if args.password_stdin
                else getpass.getpass(f"{args.admin} 的密碼："))
    return authenticate_admin(conn, args.admin, password)


# 命令列入口。
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.candidates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="只驗證候選檔（不連資料庫）")
    p.add_argument("--file", type=Path, required=True)
    for name, help_text in (("load", "載入成 draft"), ("activate", "啟用 draft 候選"), ("retire", "退役候選")):
        p = sub.add_parser(name, help=help_text)
        if name == "load":
            p.add_argument("--file", type=Path, required=True)
        else:
            p.add_argument("--key", required=True)
        p.add_argument("--admin", required=True)
        p.add_argument("--password-stdin", action="store_true")
        if name != "retire":
            p.add_argument("--allow-synthetic", action="store_true")
    sub.add_parser("status", help="候選治理報表")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            data = read_spec(args.file)
            print(json.dumps({"status": data["status"], "synthetic": data["synthetic"],
                              "candidates": len(data["candidates"])}, ensure_ascii=False))
            return
        with _connect() as conn:
            if args.cmd == "status":
                result = governance_status(conn)
            elif args.cmd == "load":
                data = read_spec(args.file)
                result = load_spec(conn, data, _admin(conn, args), allow_synthetic=args.allow_synthetic)
            elif args.cmd == "activate":
                result = activate(conn, args.key, _admin(conn, args), allow_synthetic=args.allow_synthetic)
            else:
                result = retire(conn, args.key, _admin(conn, args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (CandidateError, IngestError, psycopg.Error) as exc:
        sys.exit(f"失敗：{exc}")


if __name__ == "__main__":
    main()
