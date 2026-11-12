"""S5 問答 × S8 結果檢視的整合測試（SYNTHETIC／MOCK；FakeGenerator 不是真實模型）。

- 送出當下與回頭查看（GET /api/requests/{id}）都只回白名單欄位：不含 chunk_id、label、分數、模型上下文。
- 來源改成限管理者後，歷史問答的引用變成「目前無法查看」，原文不再出現；編號保留，回答中的〔來源 n〕仍對得上。
- 失敗說明（例如逾時）存進日誌，回頭查看時看得到同一段說明。
"""
from __future__ import annotations

import json

from tests.conftest import login
from tests.test_qa import (FakeGenerator, QUESTION, conn, corpus, make_client, ok_output)  # noqa: F401（共用 fixture）
from app import qa


def _ask_and_view(client, user="alice"):
    with client:
        h = login(client, user)
        cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
        r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": QUESTION})
        assert r.status_code == 201, r.text
        body = r.json()
        view = client.get(f"/api/requests/{body['request_id']}", headers=h)
        assert view.status_code == 200, view.text
    return body, view.json(), h


def test_qa_response_and_history_use_whitelist(make_client, db, corpus):
    body, view, _ = _ask_and_view(make_client(FakeGenerator(ok_output("E1"))))
    assert body["status"] == "answered" and view["status"] == "answered"
    assert view["answer"] == body["answer"] and view["message"] == body["message"]
    for payload in (body["qa"], view["qa"]):
        cite = payload["citations"][0]
        assert cite["available"] is True and cite["number"] == 1
        assert (cite["doc_key"], cite["version_label"], cite["locator"]) == ("PUBLIC", "v1", "p.4")
        assert cite["quote_text"] == QUESTION and payload["content_support"] == "not_verified"
        raw = json.dumps(payload, ensure_ascii=False)
        for leaked in ('"chunk_id"', '"score"', '"label"', "context_sent", "model_calls"):
            assert leaked not in raw


def test_history_qa_citation_rechecked_after_source_restricted(make_client, db, conn, corpus):
    body, _, _ = _ask_and_view(make_client(FakeGenerator(ok_output("E1"))))
    conn.execute("UPDATE documents SET visibility = 'admin' WHERE doc_key = 'PUBLIC'")
    with make_client() as client:  # TestClient 關閉後連線池不能重用，回頭查看用新的 client
        h = login(client, "alice")
        view = client.get(f"/api/requests/{body['request_id']}", headers=h).json()
    # S9：依據此刻不可用 → 整則遮蔽：回答文字、引用、原文都不回，只留結果類型與說明
    assert view["masked"] is True and view["answer"] is None
    assert view["qa"] == {"outcome": "answered", "citations": [], "content_support": None}
    # 原文不再出現（合成資料的片段原文與問題相同，所以排除使用者自己的 question 欄位再比對）
    assert QUESTION not in json.dumps({k: v for k, v in view.items() if k != "question"}, ensure_ascii=False)


def test_failed_answer_message_kept_for_history(make_client, db, corpus):
    gen = FakeGenerator(qa.ModelError("model_timeout", "t", transient=True))
    body, view, _ = _ask_and_view(make_client(gen))
    assert body["status"] == "answer_failed" and view["request_status"] == "failed"
    assert view["message"] == body["message"] and "逾時" in view["message"]
    assert view["qa"] == {"outcome": "failed", "citations": [], "content_support": None}
