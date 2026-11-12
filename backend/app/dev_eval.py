"""S10 開發題評估執行器：把 20 題開發案例送進真實 API 流程，做自動檢查並輸出評估報告。

用法（在 backend/ 目錄）：
    python -m app.dev_eval                                   # 用 pgserver 起暫存 PostgreSQL，印出 Markdown
    python -m app.dev_eval --admin-url postgresql://…/postgres   # 改用指定的測試伺服器
    python -m app.dev_eval --out ../docs/mvp/s10-dev-eval.md --json ../docs/mvp/s10-dev-eval.json

做什麼
- 每次執行都建立全新的暫存資料庫（名稱 lipmatch_eval_<亂數>），套用遷移、建測試帳號，跑完就刪除；
  不連、不改任何既有資料庫（含 compose 的 lipmatch）。
- 跑兩種資料情境，結果分開列：
    formal_like  沒有任何經核對的推薦候選、沒有核准片段（＝目前正式資料庫的實際狀態）
    synthetic    載入 S7 的 SYNTHETIC 合成候選與 SYN_S7_* 合成文件（只證明流程與規則照設計運作）
- 每題用新對話，依序送出案例的訊息，對「最後一則」的回應、對話狀態與資料庫推薦紀錄做自動檢查。
- 生成模型與嵌入模型一律不設定（DEC-11 未定）：問答題必然拿不到回答，這是誠實的結果，不是評估工具的錯。

不做什麼（規範 4、S10 說明）
- 不做人工評分：逐主張引用支持、需求理解是否合理等，輸出待填表，標「未完成」，不自評。
- Hit@5 只對「有已知支持證據」的題計算；目前 20 題的預期證據全部未核對 → 分母 0，標「未執行」，不捏造分數。
- 不讀、不碰 10 題最終保留題（由幫忙者另存，不在 repo）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from . import candidates as gov
from . import preferences as pref
from . import qa
from . import recommend as rec
from .cli import create_user
from .config import Settings
from .migrate import migrate

BACKEND_DIR = Path(__file__).resolve().parent.parent
CASES_PATH = BACKEND_DIR / "eval" / "s10_dev_cases.json"
SYNTHETIC_CANDIDATES = BACKEND_DIR / "eval" / "s7_synthetic_candidates.json"
EVAL_VERSION = "dev-eval-v1"
ORIGIN = "http://eval.local"            # TestClient 請求的 Origin；只在暫存 app 的允許清單內
COOKIE = "lipmatch_session"
ENVIRONMENTS = ("formal_like", "synthetic")
ENV_LABEL = {"formal_like": "正式資料狀態（0 候選、0 核准片段）", "synthetic": "SYNTHETIC 合成候選"}

# 回應裡出現這些字樣就視為輸出了具體商品資訊（商品功能未啟用，S4 No-go）
PRODUCT_MARKERS = ("品牌：", "色號：", "售價", "價格：", "元／支", "現貨", "庫存", "加入購物車", "下單成功", "購買連結")


# ---------------------------------------------------------------------------
# 單題結果
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class CaseResult:
    case_id: str
    facet: str
    environment: str
    request_ids: list[str]
    intent: str | None
    outcome: str | None
    status: str | None
    error_code: str | None
    recommendations: list[str]
    clarifications: int
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    # 轉成可寫進 JSON 報告的字典。
    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "facet": self.facet, "environment": self.environment,
                "request_ids": self.request_ids, "intent": self.intent, "outcome": self.outcome,
                "status": self.status, "error_code": self.error_code, "recommendations": self.recommendations,
                "clarifications": self.clarifications, "passed": self.passed,
                "checks": [c.__dict__ for c in self.checks]}


# ---------------------------------------------------------------------------
# 暫存資料庫與種子
# ---------------------------------------------------------------------------
# 建立一個全新的暫存資料庫並套用遷移，回傳連線字串。
def create_eval_db(admin_url: str) -> str:
    name = f"lipmatch_eval_{secrets.token_hex(6)}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(admin_url, dbname=name)
    migrate(url)
    return url
# 核心邏輯：名稱固定前綴＋亂數，只建立不重用；drop_eval_db 也只接受這個前綴，避免誤刪既有資料庫。


# 刪除暫存資料庫；名稱不是 lipmatch_eval_ 開頭就拒絕。
def drop_eval_db(admin_url: str, db_url: str) -> None:
    name = psycopg.conninfo.conninfo_to_dict(db_url)["dbname"]
    if not str(name).startswith("lipmatch_eval_"):
        raise RuntimeError(f"拒絕刪除非評估用資料庫：{name}")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# 載入 SYNTHETIC 合成文件、核准並發布片段，再載入並啟用全部合成候選（與 S7／S8 測試相同的資料）。
def seed_synthetic(db_url: str, admin_id: int) -> int:
    spec = json.loads(SYNTHETIC_CANDIDATES.read_text(encoding="utf-8"))
    chunk_keys: dict[str, list[str]] = {}
    for c in spec["candidates"]:
        for e in c["evidence"]:
            chunk_keys.setdefault(e["doc_key"], []).append(e["chunk_key"])
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        for doc, keys in chunk_keys.items():
            doc_id = conn.execute(
                "INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible) "
                "VALUES (%s, %s, 'other', 'all', TRUE, TRUE) RETURNING id", (doc, f"SYNTHETIC {doc}")).fetchone()["id"]
            version_id = conn.execute(
                "INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version) "
                "VALUES (%s, 'v1', %s, 'dev-eval') RETURNING id",
                (doc_id, hashlib.sha256(doc.encode()).hexdigest())).fetchone()["id"]
            for i, key in enumerate(dict.fromkeys(keys)):
                text = f"SYNTHETIC evidence {key}"
                sha = hashlib.sha256(text.encode()).hexdigest()
                chunk_id = conn.execute(
                    "INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text, "
                    "content_sha256) VALUES (%s, %s, %s, 1, %s, %s, %s) RETURNING id",
                    (version_id, key, i, text, text, sha)).fetchone()["id"]
                conn.execute("SELECT review_chunk(%s, %s, 'approve', 'SYNTHETIC 評估用核准', %s)",
                             (chunk_id, admin_id, sha))
            conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s",
                         (version_id,))
        gov.load_spec(conn, spec, admin_id, allow_synthetic=True)
        for c in spec["candidates"]:
            gov.activate(conn, c["option_key"], admin_id, allow_synthetic=True)
    return len(spec["candidates"])
# 核心邏輯：合成證據只存在暫存資料庫；核准走資料庫的 review_chunk()（與正式流程同一道守門），
# 候選載入與啟用走 candidates 模組（allow_synthetic=True 明確標示是合成資料）。


# ---------------------------------------------------------------------------
# 執行案例
# ---------------------------------------------------------------------------
# 以帳號密碼登入暫存 app，回傳之後請求要帶的標頭。
def _login(client, username: str, password: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers={"Origin": ORIGIN})
    r.raise_for_status()
    token = r.cookies[COOKIE]
    client.cookies.clear()
    return {"Cookie": f"{COOKIE}={token}", "X-CSRF-Token": r.json()["csrf_token"], "Origin": ORIGIN}


# 在一個資料情境下跑完全部案例，回傳每題結果。
def run_environment(db_url: str, environment: str, cases: list[dict]) -> list[CaseResult]:
    from fastapi.testclient import TestClient

    from .main import create_app

    password = secrets.token_urlsafe(18)          # 只存在這次執行的記憶體
    admin_id = create_user(db_url, "eval_admin", "admin", password)
    create_user(db_url, "eval_user", "user", password)
    if environment == "synthetic":
        seed_synthetic(db_url, admin_id)
    settings = Settings(database_url=db_url, db_pool_max=4, session_cookie_secure=False, allowed_origins=(ORIGIN,))
    results: list[CaseResult] = []
    with TestClient(create_app(settings, qa_config=qa.QaConfig())) as client:
        user_h = _login(client, "eval_user", password)
        admin_h = _login(client, "eval_admin", password)
        for case in cases:
            results.append(_run_case(client, user_h, admin_h, db_url, environment, case))
    return results
# 核心邏輯：qa_config 明確用 QaConfig()（provider=none），不讀環境變數，避免評估時誤連外部模型或產生費用。
# 測試帳號密碼每次隨機產生，不寫入任何檔案。


# 跑一題：新對話、依序送訊息，對最後一則回應做檢查。
def _run_case(client, user_h, admin_h, db_url: str, environment: str, case: dict) -> CaseResult:
    conv_id = client.post("/api/conversations", headers=user_h, json={}).json()["id"]
    responses = []
    for message in case["messages"]:
        r = client.post(f"/api/conversations/{conv_id}/messages", headers=user_h, json={"question": message})
        responses.append(r.json() if r.status_code == 201 else {"http_status": r.status_code, "body": r.text})
    last = responses[-1]
    request_id = last.get("request_id")
    conv = client.get(f"/api/conversations/{conv_id}", headers=user_h).json()
    detail = client.get(f"/api/admin/monitor/requests/{request_id}", headers=admin_h).json() if request_id else {}
    recs = _recommended_options(db_url, request_id)
    result = CaseResult(
        case_id=case["id"], facet=case["facet"], environment=environment,
        request_ids=[x.get("request_id") for x in responses if x.get("request_id")],
        intent=last.get("intent"), outcome=last.get("status"), status=detail.get("status"),
        error_code=last.get("error_code"), recommendations=[r["option_key"] for r in recs],
        clarifications=len(last.get("clarification") or []),
    )
    ctx = {"last": last, "conv": conv, "detail": detail, "recs": recs}
    for name, arg in case.get("checks", {}).items():
        fn = CHECKS.get(name)
        if fn is None:
            result.checks.append(CheckResult(name, False, "評估工具不認得這個檢查（案例檔錯誤）"))
            continue
        passed, why = fn(ctx, arg)
        result.checks.append(CheckResult(name, passed, why))
    return result
# 核心邏輯：
# 1. 推薦方向由資料庫 request_recommendations 讀出，屬性也從資料庫候選讀，不採信 API 回應自己的說法。
# 2. 請求狀態由管理者監控詳情讀出（同時驗證 S10 監控端點在真實流程下可用）。


# 從資料庫讀出這次請求實際推薦的候選與其屬性。
def _recommended_options(db_url: str, request_id: str | None) -> list[dict]:
    if not request_id:
        return []
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT rr.rank, o.option_key, o.attributes
                 FROM request_recommendations rr JOIN recommendation_options o ON o.id = rr.option_id
                WHERE rr.request_id = %s ORDER BY rr.rank""", (request_id,)).fetchall()


# ---------------------------------------------------------------------------
# 自動檢查（每個函式回傳 (是否通過, 說明)）
# ---------------------------------------------------------------------------
# 系統「自己說出」的文字（訊息、回答、限制、補問、推薦名稱與主張、假設），用來做文字類檢查。
# 不含需求摘要：摘要會逐字引用使用者原話（例如注入句），算進來會把使用者自己的話誤判成系統宣稱。
def _visible_text(last: dict) -> str:
    parts = [last.get("message") or "", last.get("answer") or ""]
    parts += [str(x) for x in last.get("limitations") or []]
    parts += [q.get("question", "") if isinstance(q, dict) else str(q) for q in last.get("clarification") or []]
    recommendation = last.get("recommendation") or {}
    parts += [str(x) for x in recommendation.get("limitations") or []]
    for r in recommendation.get("recommendations") or []:
        parts.append(str(r.get("name") or ""))
        parts += [str(cl.get("text") or "") for cl in r.get("claims") or []]
    parts += [str(a.get("text") or a) if isinstance(a, dict) else str(a)
              for a in recommendation.get("assumptions") or []]
    summary = last.get("summary") or {}
    for row in summary.get("rows") or [] if isinstance(summary, dict) else []:
        # 摘要只取系統產生的欄名、顯示值、狀態說明；原話（raw）是使用者的字，不算系統宣稱
        parts += [str(row.get("label") or ""), str(row.get("value") or ""), str(row.get("note") or "")]
    return "\n".join(parts)


def _check_intent_in(ctx, allowed):
    got = ctx["last"].get("intent")
    return got in allowed, f"意圖 {got}（允許 {allowed}）"


def _check_outcome_in(ctx, allowed):
    got = ctx["last"].get("status")
    return got in allowed, f"結果 {got}（允許 {allowed}）"


def _check_not_failed(ctx, _arg):
    status = ctx["detail"].get("status")
    return status != "failed", f"requests.status = {status}，error_code = {ctx['last'].get('error_code')}"


def _check_no_recommendation(ctx, _arg):
    n = len(ctx["recs"])
    return n == 0, f"推薦方向 {n} 個"


def _check_max_recommendations(ctx, limit):
    n = len(ctx["recs"])
    return n <= limit, f"推薦方向 {n} 個（上限 {limit}）"


# 推薦出的候選屬性不得含被排除的值（屬性從資料庫讀）。
def _check_forbidden_attributes(ctx, forbidden: dict):
    hits = []
    for r in ctx["recs"]:
        attrs = r["attributes"] or {}
        for key, banned in forbidden.items():
            value = attrs.get(key)
            values = value if isinstance(value, list) else [value]
            bad = sorted(set(values) & set(banned))
            if bad:
                hits.append(f"{r['option_key']}.{key}={bad}")
    if not ctx["recs"]:
        return True, "沒有推薦方向（無可檢查）"
    return not hits, "違反：" + "、".join(hits) if hits else "推薦方向都避開排除條件"


def _check_max_clarification(ctx, limit):
    n = len(ctx["last"].get("clarification") or [])
    return n <= limit, f"補問 {n} 題（上限 {limit}）"


def _check_requires_clarification(ctx, _arg):
    n = len(ctx["last"].get("clarification") or [])
    return n >= 1, f"補問 {n} 題"


# rule-v5（K1）：資料表欄位 scenario_source = default 代表「未指定單一場合」（欄位不可為空才寫佔位值 daily），
# 因此這時的值視為 None，不能當成日常。
def _check_scenario(ctx, want: dict):
    conv = ctx["conv"]
    source = conv.get("scenario_source")
    got = {"value": conv.get("scenario") if source == "user" else None, "source": source}
    ok = got["value"] == want["value"] and ("source" not in want or got["source"] == want["source"])
    return ok, f"情境 {got}（期望 {want}）"


# 情境是預設時，給使用者看的文字必須提到「預設」。
def _check_default_disclosed(ctx, _arg):
    if ctx["conv"].get("scenario_source") != "default":
        return True, "情境由使用者指定，不需揭露預設"
    text = _visible_text(ctx["last"])
    return "預設" in text, "回應有揭露預設情境" if "預設" in text else "情境是預設，但回應沒有提到「預設」"


# rule-v5（K1）：場合未指定時，回應不可聲稱套用了預設情境（舊版會寫「暫以「日常」為預設情境」）。
def _check_no_default_scenario(ctx, _arg):
    text = _visible_text(ctx["last"])
    bad = [w for w in ("為預設情境", "預設情境", "預設：日常") if w in text]
    return not bad, "回應沒有聲稱預設情境" if not bad else f"回應提到 {bad}"


# 底調必須維持未知（不得由膚色推定）。
def _check_undertone_not_inferred(ctx, _arg):
    prefs = ctx["conv"].get("preferences") or {}
    undertone = prefs.get("undertone") or {}
    value = undertone.get("value") if isinstance(undertone, dict) else undertone
    return value in (None, "unknown"), f"底調 = {value}"


def _check_allergy_status(ctx, allowed):
    got = ctx["conv"].get("allergy_status")
    return got in allowed, f"過敏狀態 {got}（允許 {allowed}）"


# 沒有生成模型時不可出現回答；推薦的規則模板文字不算模型回答。
def _check_no_fabricated_answer(ctx, _arg):
    answer = ctx["last"].get("answer")
    source = ctx["detail"].get("answer_source")
    if not answer:
        return True, "沒有回答內容"
    return source == "rule_template", f"出現回答內容（answer_source = {source}）"


def _check_no_product_output(ctx, _arg):
    text = _visible_text(ctx["last"])
    found = [m for m in PRODUCT_MARKERS if m in text]
    recommendation = ctx["last"].get("recommendation") or {}
    enabled = recommendation.get("product_enabled") is True
    ok = not found and not enabled
    return ok, ("沒有商品資訊" if ok else f"商品標記 {found}、product_enabled = {enabled}")


def _check_text_excludes(ctx, banned):
    text = _visible_text(ctx["last"])
    found = [b for b in banned if b in text]
    return not found, ("沒有禁用字樣" if not found else f"出現 {found}")


def _check_text_includes_any(ctx, wanted):
    text = _visible_text(ctx["last"])
    found = [w for w in wanted if w in text]
    return bool(found), (f"含 {found}" if found else f"回應沒有任何一個：{wanted}")


CHECKS: dict[str, Callable[[dict, Any], tuple[bool, str]]] = {
    "intent_in": _check_intent_in,
    "outcome_in": _check_outcome_in,
    "not_failed": _check_not_failed,
    "no_recommendation": _check_no_recommendation,
    "max_recommendations": _check_max_recommendations,
    "forbidden_attributes": _check_forbidden_attributes,
    "max_clarification": _check_max_clarification,
    "requires_clarification": _check_requires_clarification,
    "scenario": _check_scenario,
    "no_default_scenario": _check_no_default_scenario,
    "default_disclosed": _check_default_disclosed,
    "undertone_not_inferred": _check_undertone_not_inferred,
    "allergy_status": _check_allergy_status,
    "no_fabricated_answer": _check_no_fabricated_answer,
    "no_product_output": _check_no_product_output,
    "text_excludes": _check_text_excludes,
    "text_includes_any": _check_text_includes_any,
}

# 生成／嵌入模型未設定（DEC-11）造成的錯誤代碼：屬環境限制，與系統行為是否正確分開統計
MODEL_ENV_ERRORS = {"retrieval_not_configured", "retrieval_not_ready", "model_not_configured"}


# 失敗分類：環境限制（模型未定）或行為不符預期。
def classify_failure(result: "CaseResult", check: CheckResult) -> str:
    if check.name == "not_failed" and result.error_code in MODEL_ENV_ERRORS:
        return "環境限制（DEC-11 模型未定）"
    return "行為不符預期"


# 屬於「條件遵守」的檢查（排除、情境、過敏、底調、補問上限、商品、不捏造）
CONDITION_CHECKS = {"forbidden_attributes", "scenario", "undertone_not_inferred", "allergy_status",
                    "max_recommendations", "max_clarification", "no_product_output", "no_fabricated_answer",
                    "no_recommendation", "default_disclosed", "no_default_scenario", "text_excludes"}


# ---------------------------------------------------------------------------
# 彙整與報告
# ---------------------------------------------------------------------------
# 版本與執行條件（寫進報告，作為凍結與重現依據）。
def run_metadata(cases_doc: dict) -> dict:
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND_DIR, capture_output=True,
                                text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=BACKEND_DIR, capture_output=True,
                                    text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = "unknown", None
    return {
        "eval_version": EVAL_VERSION, "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit, "git_dirty": dirty,
        "cases_file": f"backend/eval/{CASES_PATH.name}", "cases_sha256_16": sha(CASES_PATH),
        "cases_schema": cases_doc.get("schema_version"),
        "parser": f"{pref.PARSER_NAME} {pref.PARSER_VERSION}", "pref_schema": pref.PREF_SCHEMA_VERSION,
        "vocab_version": pref.VOCAB["version"], "rules_version": rec.RULES_VERSION,
        "prompt_version": qa.PROMPT_VERSION, "qa_output_schema": qa.OUTPUT_SCHEMA_VERSION,
        "generation_model": "未設定（DEC-11）", "embedding_model": "未設定（DEC-11）",
        "synthetic_candidates_sha256_16": sha(SYNTHETIC_CANDIDATES),
        "product_enabled": rec.PRODUCT_ENABLED,
    }


# 依面向、檢查類別彙整成分子／分母。
def aggregate(results: list[CaseResult], cases: list[dict]) -> dict:
    out: dict[str, Any] = {}
    for env in ENVIRONMENTS:
        rows = [r for r in results if r.environment == env]
        facets = {}
        for facet in sorted({r.facet for r in rows}):
            fr = [r for r in rows if r.facet == facet]
            facets[facet] = {"passed": sum(r.passed for r in fr), "total": len(fr)}
        cond = [c for r in rows for c in r.checks if c.name in CONDITION_CHECKS]
        out[env] = {
            "cases_passed": sum(r.passed for r in rows), "cases_total": len(rows), "facets": facets,
            "condition_checks_passed": sum(c.passed for c in cond), "condition_checks_total": len(cond),
        }
    known = [c for c in cases if c.get("expected_evidence", {}).get("known")]
    out["hit_at_5"] = {"numerator": None, "denominator": len(known),
                       "status": "未執行" if not known else "待實作",
                       "reason": "20 題的預期支持證據都還沒核對（dev-cases-draft.md D2–D3 待辦），且嵌入模型未定（DEC-11）、"
                                 "正式核准片段 0 筆，沒有可計算的題目；不以 0% 或其他數字代替。"}
    manual = [c["id"] for c in cases if c.get("manual_claims")]
    out["citation_support"] = {"status": "未完成（需人工）", "cases_needing_review": manual,
                               "numerator": None, "denominator": None,
                               "reason": "逐主張引用支持須人工核對；本次沒有任何模型回答或正式證據可核對。"}
    return out


# 產生 Markdown 報告。
def to_markdown(meta: dict, summary: dict, results: list[CaseResult], cases: list[dict]) -> str:
    lines = [
        "# S10 開發題評估（自動檢查）",
        "",
        "> **SYNTHETIC／開發題，不是正式驗收。** 20 題開發案例已曝光、可用來調整；最終 10 題保留題由幫忙者另存，未在此使用。",
        "> 人工評分（逐主張引用支持、需求理解合理性）與自動檢查分開，人工部分見 `s10-dev-eval-manual.md`，目前**未完成**。",
        "> 本報告由 `python -m app.dev_eval` 產生；重跑會覆蓋，手寫說明請放在 handoff。",
        "",
        "## 執行條件與版本",
        "",
        "| 項目 | 值 |",
        "|---|---|",
    ]
    lines += [f"| {k} | `{v}` |" for k, v in meta.items()]
    lines += ["", "## 摘要（分子／分母）", "",
              "| 資料情境 | 全部檢查通過的題數 | 條件遵守檢查 | A 知識 | B 推薦 | C 商品 | D 模糊／範圍外 |",
              "|---|---|---|---|---|---|---|"]
    for env in ENVIRONMENTS:
        s = summary[env]
        f = s["facets"]
        cells = [f"{f[k]['passed']}/{f[k]['total']}" if k in f else "—" for k in "ABCD"]
        lines.append(f"| {ENV_LABEL[env]} | {s['cases_passed']}/{s['cases_total']} | "
                     f"{s['condition_checks_passed']}/{s['condition_checks_total']} | " + " | ".join(cells) + " |")
    h5, cs = summary["hit_at_5"], summary["citation_support"]
    lines += ["",
              f"- **Hit@5**：{h5['status']}（分母 {h5['denominator']}）。{h5['reason']}",
              f"- **逐主張引用支持／缺引用**：{cs['status']}。{cs['reason']}",
              "- **四面向說明**：A 知識題在沒有生成與嵌入模型時只能回報無法回答（檢查重點是不捏造）；"
              "B 推薦題在正式資料狀態下應誠實回「目前沒有可引用的方向」；"
              "C 商品題檢查不輸出商品；D 檢查補問、範圍外與注入。",
              "", "## 逐題結果", "",
              "| 題 | 面向 | 資料情境 | 意圖 | 結果 | error_code | 推薦 | 補問 | 通過 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.case_id} | {r.facet} | {r.environment} | {r.intent or '—'} | {r.outcome or '—'} | "
                     f"{r.error_code or '—'} | {', '.join(r.recommendations) or '—'} | {r.clarifications} | "
                     f"{'✅' if r.passed else '❌'} |")
    failures = [(r, c) for r in results for c in r.checks if not c.passed]
    lines += ["", f"## 完整失敗清單（{len(failures)} 項）", ""]
    if not failures:
        lines.append("（無）")
    else:
        kinds: dict[str, int] = {}
        for r, c in failures:
            kinds[classify_failure(r, c)] = kinds.get(classify_failure(r, c), 0) + 1
        lines.append("失敗分類：" + "、".join(f"{k} {v} 項" for k, v in kinds.items()) + "。")
        lines += ["", "| 題 | 資料情境 | 檢查 | 分類 | 說明 | request_id（暫存庫，已刪） |", "|---|---|---|---|---|---|"]
        for r, c in failures:
            rid = r.request_ids[-1] if r.request_ids else "—"
            lines.append(f"| {r.case_id} | {r.environment} | {c.name} | {classify_failure(r, c)} | "
                         f"{c.detail.replace('|', '／')} | `{rid[:8]}` |")
    lines += ["", "## 每題檢查明細", ""]
    for r in results:
        lines.append(f"- **{r.case_id}／{r.environment}**：" + "；".join(
            f"{'✓' if c.passed else '✗'} {c.name}（{c.detail.replace('|', '／')}）" for c in r.checks))
    lines.append("")
    return "\n".join(lines)
# 核心邏輯：每個百分比都以「分子/分母」呈現；未能計算的指標寫「未執行／未完成」與原因，不填數字。


# ---------------------------------------------------------------------------
# 進入點
# ---------------------------------------------------------------------------
# 跑完整評估：兩種資料情境各用一個暫存資料庫。回傳 (metadata, summary, results)。
def evaluate(admin_url: str) -> tuple[dict, dict, list[CaseResult]]:
    cases_doc = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = cases_doc["cases"]
    results: list[CaseResult] = []
    for env in ENVIRONMENTS:
        db_url = create_eval_db(admin_url)
        try:
            results += run_environment(db_url, env, cases)
        finally:
            drop_eval_db(admin_url, db_url)
    return run_metadata(cases_doc), aggregate(results, cases), results


# 取得 PostgreSQL 管理連線：有指定就用，否則用 pgserver 在暫存目錄起一個（與後端測試相同）。
def _admin_url(arg: str | None):
    if arg:
        return arg, None
    import tempfile

    import pgserver  # 開發依賴（requirements-dev.txt）

    tmp = tempfile.mkdtemp(prefix="lipmatch_eval_pg_")
    server = pgserver.get_server(tmp, cleanup_mode="stop")
    return server.get_uri(), server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="S10 開發題評估（自動檢查）")
    ap.add_argument("--admin-url", default=os.environ.get("LIPMATCH_TEST_PG_ADMIN_URL"),
                    help="測試用 PostgreSQL 管理連線（只會建立／刪除 lipmatch_eval_ 開頭的資料庫）")
    ap.add_argument("--out", help="Markdown 報告輸出路徑（預設印到標準輸出）")
    ap.add_argument("--json", dest="json_out", help="JSON 結果輸出路徑")
    args = ap.parse_args(argv)
    admin_url, server = _admin_url(args.admin_url)
    try:
        meta, summary, results = evaluate(admin_url)
    finally:
        if server is not None:
            server.cleanup()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    md = to_markdown(meta, summary, results, cases)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"meta": meta, "summary": summary,
                                                   "results": [r.to_dict() for r in results]},
                                                  ensure_ascii=False, indent=2, default=str) + "\n",
                                       encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
