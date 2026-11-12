"""S6 API 測試：提問時解析偏好並保存於對話內、解析呼叫寫入日誌、衝突不選邊、不跨對話共用。

所有語句為 SYNTHETIC 開發案例。
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app import preferences as pref

from .conftest import login


def _conv(client, headers, **body):
    r = client.post("/api/conversations", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _say(client, headers, cid, text):
    r = client.post(f"/api/conversations/{cid}/messages", headers=headers, json={"question": text})
    assert r.status_code == 201, r.text
    return r.json()


def _values(state, field):
    return [i["value"] for i in state[field]]


def _request_row(db, request_id):
    with psycopg.connect(db, row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM requests WHERE id = %s", (request_id,)).fetchone()


def test_change_occasion_keeps_no_matte(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    first = _say(client, h, cid, "上班用的唇色，不要霧面")
    # route-v4：推薦需求走問答（測試庫沒有核准片段 → 缺資料），不跑規則推薦
    assert first["intent"] == "recommend" and first["status"] == "answer_insufficient_evidence"
    body = _say(client, h, cid, "改成聚會")
    # 上一輪是推薦需求，修改條件後用新條件重新問答（route-v4 update_after_recommend）
    assert body["status"] == "answer_insufficient_evidence" and body["error_code"] is None
    assert [a["field"] for a in body["applied_changes"]] == ["occasion"]
    prefs = body["preferences"]
    assert prefs["occasion"]["value"] == "party" and _values(prefs, "disliked_finishes") == ["matte"]
    conv = client.get(f"/api/conversations/{cid}", headers=h).json()
    assert conv["scenario"] == "party" and conv["scenario_source"] == "user"   # 原有欄位同步
    assert _values(conv["preferences"], "disliked_finishes") == ["matte"]


def test_cancel_no_matte_removes_restriction(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    _say(client, h, cid, "不要霧面")
    body = _say(client, h, cid, "取消不要霧面")
    assert body["preferences"]["disliked_finishes"] == [] and body["preferences"]["liked_finishes"] == []
    assert [c["op"] for c in body["applied_changes"]] == ["remove"]


def test_unanswered_allergy_stays_unanswered(client, users, db):
    h = login(client, "alice")
    cid = _conv(client, h)
    for text in ("上班用的自然唇色", "不要橘色", "改成聚會"):
        _say(client, h, cid, text)
    conv = client.get(f"/api/conversations/{cid}", headers=h).json()
    assert (conv["allergy_status"], conv["allergy_avoid"]) == ("unanswered", [])
    rows = {r["field"]: r for r in conv["summary"]["rows"]}
    assert rows["allergy"]["value"] == "尚未回答"


def test_conflict_is_not_resolved_by_system(client, users, db):
    h = login(client, "alice")
    cid = _conv(client, h)
    body = _say(client, h, cid, "要霧面，不要霧面")
    assert body["status"] == "needs_clarification" and len(body["clarification"]) == 1
    assert body["preferences"]["liked_finishes"] == [] and body["preferences"]["disliked_finishes"] == []
    assert _request_row(db, body["request_id"])["status"] == "succeeded"
    body = _say(client, h, cid, "以不要霧面為準")
    assert _values(body["preferences"], "disliked_finishes") == ["matte"] and body["preferences"]["conflicts"] == []


def test_against_existing_conflict_keeps_old_condition(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    _say(client, h, cid, "不要霧面")
    body = _say(client, h, cid, "推薦霧面的唇膏")
    # 2026-11-01：和先前條件相反不擋回答（只有同一句自相矛盾才先澄清）；走問答，衝突補問照樣附上
    assert body["status"] == "answer_insufficient_evidence" and body["clarification"]
    assert _values(body["preferences"], "disliked_finishes") == ["matte"]
    assert body["preferences"]["liked_finishes"] == []


def test_parse_call_is_logged(client, users, db):
    h = login(client, "alice")
    cid = _conv(client, h)
    body = _say(client, h, cid, "我是小麥色，推薦唇彩")
    row = _request_row(db, body["request_id"])
    assert row["question"] == "我是小麥色，推薦唇彩"   # 使用者原話
    assert row["status"] == "succeeded" and row["error_code"] is None   # route-v4：問答缺資料也是成功的業務結果
    assert "recommendation" not in row["detail"] and row["detail"]["route"]["recommendation"] == "skipped_by_route"
    assert row["input_tokens"] is None and row["output_tokens"] is None   # 未知，不記 0
    detail = row["detail"]
    call = detail["parse_calls"][0]
    assert (call["parser"], call["parser_version"], call["error"]) == ("rule", pref.PARSER_VERSION, None)
    assert call["input_tokens"] is None and call["model_name"] is None and call["latency_ms"] >= 0
    assert call["output"]["intent"] == "recommend"
    assert detail["preferences_before"]["skin_tone"]["value"] is None
    assert detail["outcome"] == "answer_insufficient_evidence" and detail["intent"] == "recommend"
    snap = row["preferences_snapshot"]["preferences"]
    assert snap["skin_tone"]["value"] == "小麥色" and snap["undertone"]["value"] is None
    hist = client.get(f"/api/conversations/{cid}", headers=h).json()["requests"]
    assert (hist[0]["outcome"], hist[0]["intent"]) == ("answer_insufficient_evidence", "recommend")


def test_parse_failure_is_logged_and_state_unchanged(client, users, db, monkeypatch):
    from app import main

    # 模擬解析器故障：請求仍要留下紀錄，偏好不能被改動
    def boom(_text):
        raise RuntimeError("parser down")
    monkeypatch.setattr(main._PARSER, "parse", boom)
    h = login(client, "alice")
    cid = _conv(client, h)
    body = _say(client, h, cid, "不要霧面")
    assert body["status"] == "parse_failed" and body["error_code"] == "preference_parse_failed"
    row = _request_row(db, body["request_id"])
    assert row["status"] == "failed" and row["detail"]["parse_calls"][0]["error"] == "RuntimeError"
    conv = client.get(f"/api/conversations/{cid}", headers=h).json()
    assert conv["preferences"]["disliked_finishes"] == []


def test_preferences_do_not_cross_conversations(client, users):
    h = login(client, "alice")
    first = _conv(client, h)
    _say(client, h, first, "不要霧面，我對羊毛脂過敏")
    second = _conv(client, h)
    conv = client.get(f"/api/conversations/{second}", headers=h).json()
    assert conv["preferences"]["disliked_finishes"] == []
    assert (conv["allergy_status"], conv["allergy_avoid"]) == ("unanswered", [])


def test_other_user_cannot_read_or_change(client, users):
    alice, bob = login(client, "alice"), login(client, "bob")
    cid = _conv(client, alice)
    _say(client, alice, cid, "不要霧面")
    assert client.get(f"/api/conversations/{cid}", headers=bob).status_code == 404
    r = client.post(f"/api/conversations/{cid}/messages", headers=bob, json={"question": "取消不要霧面"})
    assert r.status_code == 404
    conv = client.get(f"/api/conversations/{cid}", headers=alice).json()
    assert _values(conv["preferences"], "disliked_finishes") == ["matte"]


def test_form_and_chat_share_rules(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={
        "preferences": {"disliked": ["霧面"], "skin_tone": "小麥色", "effects": ["自然"]}})
    assert r.status_code == 200, r.text
    assert _values(r.json()["preferences"], "disliked_finishes") == ["matte"]
    assert r.json()["preferences"]["undertone"]["value"] is None
    body = _say(client, h, cid, "改成聚會")
    assert _values(body["preferences"], "disliked_finishes") == ["matte"]
    assert body["preferences"]["skin_tone"]["value"] == "小麥色"
    r = client.patch(f"/api/conversations/{cid}", headers=h, json={
        "preferences": {"liked": ["霧面"], "disliked": ["霧面"]}})
    assert r.status_code == 422 and "霧面" in r.json()["error"]["message"]


def test_summary_rows_returned(client, users):
    h = login(client, "alice")
    cid = _conv(client, h)
    body = _say(client, h, cid, "不要太紅")
    fields = [r["field"] for r in body["summary"]["rows"]]
    assert fields[:2] == ["occasion", "skin_tone"] and "allergy" in fields
    assert body["summary"]["ambiguities"][0]["raw"] == "不要太紅"
    assert body["clarification"][0]["blocking"] is True
