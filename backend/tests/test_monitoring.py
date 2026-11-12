"""S10 管理者監控：權限、分類口徑、耗時百分位、未知 Token、時間範圍、詳情不外流原文與秘密。

SYNTHETIC：請求紀錄由測試直接寫入（控制狀態、耗時、Token），或沿用 S7／S8 的合成候選跑真實流程；
只驗證監控的計算與權限，不代表任何真實使用量。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import monitoring
from .card_history import seed_card_history
from .conftest import login
from .test_results_api import MULTI, _new_conv, _say, _setup

SUMMARY = "/api/admin/monitor/summary"
LIST = "/api/admin/monitor/requests"


# 直接寫入一筆請求紀錄（含對話），回傳 request_id。started_at 預設為現在。
def _insert_request(db, user_id, *, status="succeeded", outcome="recommended", latency=None,
                    input_tokens=None, output_tokens=None, error_code=None, error_detail=None,
                    detail=None, question="SYNTHETIC 問題", answer=None, started_at=None, model_name=None):
    rid = uuid.uuid4()
    started_at = started_at or datetime.now(timezone.utc)
    completed = None if status == "started" else started_at + timedelta(seconds=1)
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        conv = c.execute("INSERT INTO conversations (user_id) VALUES (%s) RETURNING id", (user_id,)).fetchone()["id"]
        c.execute(
            """INSERT INTO requests (id, user_id, conversation_id, question, status, latency_ms, input_tokens,
                                     output_tokens, error_code, error_detail, detail, answer, started_at,
                                     completed_at, model_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (rid, user_id, conv, question, status, latency, input_tokens, output_tokens, error_code,
             Jsonb(error_detail) if error_detail else None, Jsonb({"outcome": outcome, **(detail or {})}),
             answer, started_at, completed, model_name))
    return rid


# 依類別取出摘要中的數量。
def _counts(summary: dict) -> dict:
    return {c["key"]: c["n"] for c in summary["requests"]["categories"]}


# ---------------------------------------------------------------------------
# 1. 權限：未登入 401、一般使用者 403、管理者 200
# ---------------------------------------------------------------------------
def test_monitor_endpoints_admin_only(client, users):
    paths = [SUMMARY, LIST, f"{LIST}/{uuid.uuid4()}"]
    for p in paths:
        assert client.get(p).status_code == 401
    h_user = login(client, "alice")
    for p in paths:
        r = client.get(p, headers=h_user)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    h_admin = login(client, "root")
    assert client.get(SUMMARY, headers=h_admin).status_code == 200
    assert client.get(LIST, headers=h_admin).status_code == 200
    assert client.get(f"{LIST}/{uuid.uuid4()}", headers=h_admin).status_code == 404


# ---------------------------------------------------------------------------
# 2. 分類口徑與耗時百分位
# ---------------------------------------------------------------------------
def test_summary_categories_and_latency(client, db, users):
    alice = users["alice"]
    # 成功 10 筆，耗時 100、200 … 1000 ms
    for i in range(1, 11):
        _insert_request(db, alice, latency=i * 100)
    _insert_request(db, alice, outcome="no_match", latency=None)                  # 成功（no_match 歸成功）
    _insert_request(db, alice, outcome="needs_clarification")                     # 補問
    _insert_request(db, alice, outcome="insufficient_evidence")                   # 資料不足（推薦）
    _insert_request(db, alice, outcome="answer_insufficient_evidence")            # 資料不足（問答）
    _insert_request(db, alice, status="failed", outcome="answer_failed", error_code="model_timeout",
                    error_detail={"stage": "generation"})
    _insert_request(db, alice, status="started", outcome=None)                    # 未完成

    s = client.get(SUMMARY, headers=login(client, "root")).json()
    assert s["requests"]["total"] == 16
    assert _counts(s) == {"success": 11, "failed": 1, "clarification": 1, "insufficient_data": 2, "in_progress": 1}
    assert sum(_counts(s).values()) == s["requests"]["total"]                     # 類別互斥且涵蓋全部
    assert s["latency_ms"] == {"n": 10, "p50": 550, "p95": 955, "max": 1000}     # percentile_cont 線性內插
    assert s["requests"]["by_error"][0]["error_code"] == "model_timeout"
    assert s["requests"]["by_error"][0]["stage"] == "generation"
    assert s["range"]["timezone"] == "UTC" and "definitions" in s


# ---------------------------------------------------------------------------
# 3. Token：未知不記 0；加總只含已知值
# ---------------------------------------------------------------------------
def test_tokens_unknown_is_not_zero(client, db, users):
    alice = users["alice"]
    h = login(client, "root")
    _insert_request(db, alice)
    _insert_request(db, alice)
    t = client.get(SUMMARY, headers=h).json()["tokens"]
    assert t["input_sum"] is None and t["output_sum"] is None                     # 全部未知 → null，不是 0
    assert (t["input_known"], t["input_unknown"]) == (0, 2)

    _insert_request(db, alice, input_tokens=120, output_tokens=30, model_name="synthetic-model")
    t = client.get(SUMMARY, headers=h).json()["tokens"]
    assert (t["input_sum"], t["output_sum"]) == (120, 30)
    assert (t["input_known"], t["input_unknown"], t["model_requests"]) == (1, 2, 1)


# ---------------------------------------------------------------------------
# 4. 時間範圍：範圍外不計；不合法範圍 422
# ---------------------------------------------------------------------------
def test_time_range_filter_and_validation(client, db, users):
    alice = users["alice"]
    now = datetime.now(timezone.utc)
    _insert_request(db, alice, started_at=now - timedelta(days=30))              # 預設 7 天外
    _insert_request(db, alice, started_at=now - timedelta(hours=1))
    h = login(client, "root")
    assert client.get(SUMMARY, headers=h).json()["requests"]["total"] == 1
    since = (now - timedelta(days=31)).isoformat()
    r = client.get(SUMMARY, headers=h, params={"since": since})
    assert r.json()["requests"]["total"] == 2
    assert r.json()["range"]["since"].startswith((now - timedelta(days=31)).date().isoformat())
    bad = client.get(SUMMARY, headers=h, params={"since": now.isoformat(), "until": (now - timedelta(days=1)).isoformat()})
    assert bad.status_code == 422
    too_long = client.get(SUMMARY, headers=h, params={"since": (now - timedelta(days=200)).isoformat()})
    assert too_long.status_code == 422


# ---------------------------------------------------------------------------
# 5. 清單篩選
# ---------------------------------------------------------------------------
def test_list_filters(client, db, users):
    alice = users["alice"]
    _insert_request(db, alice)
    failed = _insert_request(db, alice, status="failed", outcome="answer_failed", error_code="model_timeout")
    _insert_request(db, alice, status="failed", outcome="answer_failed", error_code="retrieval_not_configured")
    h = login(client, "root")
    items = client.get(LIST, headers=h, params={"category": "failed"}).json()["items"]
    assert {i["error_code"] for i in items} == {"model_timeout", "retrieval_not_configured"}
    items = client.get(LIST, headers=h, params={"error_code": "model_timeout"}).json()["items"]
    assert [i["request_id"] for i in items] == [str(failed)]
    assert "question" not in items[0] and "answer" not in items[0]
    assert client.get(LIST, headers=h, params={"category": "nope"}).status_code == 422


# ---------------------------------------------------------------------------
# 6. 詳情：不回問題／回答／模型輸出原文，秘密遮蔽
# ---------------------------------------------------------------------------
def test_detail_hides_text_and_redacts_secrets(client, db, users):
    secret_key = "sk-THISISASECRETKEY123456"
    detail = {
        "intent": "knowledge",
        "parse_calls": [{"stage": "preference_parse", "parser": "rule", "output": {"liked": ["私密偏好原文"]},
                         "input_tokens": None, "latency_ms": 3}],
        "qa": {"prompt_version": "qa-v1", "config": {"provider": "openai_compatible", "api_key": secret_key,
                                                     "base_url": "http://bob:hunter2@llm.local/v1?key=abc"},
               "model_calls": [{"stage": "generation", "input_tokens": 10, "output_tokens": 5,
                                "error": None}],
               "context_sent": {"messages": [{"role": "user", "content": "送入模型的上下文原文"}],
                                "evidence": [{"label": "S1", "chunk_id": 9, "chunk_key": "K1", "doc_key": "D1"}]},
               "claims": [{"text": "回答主張原文"}],
               "outcome": "answered"},
    }
    rid = _insert_request(db, users["alice"], outcome="answered", detail=detail, question="使用者問題原文",
                          answer="回答原文", error_detail={"stage": "generation",
                                                       "authorization": "Bearer abcdefghijklmnop"})
    r = client.get(f"{LIST}/{rid}", headers=login(client, "root"))
    assert r.status_code == 200
    raw = r.text
    for leaked in ("使用者問題原文", "回答原文", "私密偏好原文", "送入模型的上下文原文", "回答主張原文",
                   secret_key, "hunter2", "abcdefghijklmnop", "key=abc"):
        assert leaked not in raw, leaked
    body = r.json()
    assert body["question_chars"] == len("使用者問題原文") and body["answer_chars"] == len("回答原文")
    assert body["qa"]["config"]["api_key"] == monitoring.REDACTED
    assert body["qa"]["model_calls"][0]["input_tokens"] == 10                     # 用量數字不被當成秘密
    assert body["qa"]["context_evidence"] == [{"label": "S1", "chunk_key": "K1", "doc_key": "D1",
                                               "version_label": None}]
    assert body["qa"]["claims_count"] == 1
    assert body["error_detail"]["authorization"] == monitoring.REDACTED


# ---------------------------------------------------------------------------
# 7. 真實流程（SYNTHETIC 候選）＋負面回饋：計數、原因分布，自由說明不外流
# ---------------------------------------------------------------------------
def test_real_flow_and_negative_feedback(client, db, users):
    _setup(db, users)
    h = login(client, "alice")
    cid = _new_conv(client, h)
    first = _say(client, h, cid, MULTI)
    # route-v4 起正式路由不出推薦卡：改寫成舊版推薦卡紀錄，測監控對有名次的回饋（歷史資料仍有）
    assert seed_card_history(db, first["request_id"]).status == "recommended"
    r = client.put(f"/api/requests/{first['request_id']}/feedback", headers=h,
                   json={"rating": -1, "rank": 1, "reason_codes": ["not_fit", "too_generic"],
                         "comment": "自由說明原文"})
    assert r.status_code == 200

    ha = login(client, "root")
    s = client.get(SUMMARY, headers=ha).json()
    assert s["requests"]["total"] == 1 and _counts(s)["success"] == 1
    assert s["latency_ms"]["n"] == 1
    assert s["tokens"]["input_sum"] is None                                       # 規則版不呼叫模型 → 未知
    fb = s["feedback"]
    assert (fb["positive"], fb["negative"]) == (0, 1)
    assert {x["code"] for x in fb["negative_reasons"]} == {"not_fit", "too_generic"}
    assert fb["recent_negative"][0]["has_comment"] is True
    assert "自由說明原文" not in json.dumps(s, ensure_ascii=False)

    d = client.get(f"{LIST}/{first['request_id']}", headers=ha).json()
    assert d["recommendation"]["status"] == "recommended"
    assert [x["rank"] for x in d["recommendations"]] == [1, 2]
    assert d["feedback"]["rating"] == -1 and d["feedback"]["has_comment"] is True
    text = json.dumps(d, ensure_ascii=False)
    assert MULTI not in text and "自由說明原文" not in text


# ---------------------------------------------------------------------------
# 8. 遮蔽函式本身
# ---------------------------------------------------------------------------
def test_redact_rules():
    out = monitoring.redact({"password": "x", "input_tokens": 5, "output_tokens": None, "chunk_key": "K",
                             "nested": [{"Authorization": "Bearer zzzzzzzzzz"}], "csrf_token": "c",
                             "note": "call with Bearer abcdefghij123 now", "session_token": None})
    assert out["password"] == monitoring.REDACTED and out["csrf_token"] == monitoring.REDACTED
    assert out["input_tokens"] == 5 and out["output_tokens"] is None and out["chunk_key"] == "K"
    assert out["nested"][0]["Authorization"] == monitoring.REDACTED
    assert "abcdefghij123" not in out["note"]
    assert out["session_token"] is None
