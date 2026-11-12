"""S8 完整使用流程：結果檢視、引用查看、回饋驗證、受限片段不外流。

SYNTHETIC：沿用 S7 的合成候選與 SYN_S7_A／SYN_S7_B 合成文件，只驗證流程、權限與紀錄一致性，
不是正式推薦、不能當正式驗收。
"""
from __future__ import annotations

import json
import uuid

import psycopg
from psycopg.rows import dict_row

from .card_history import seed_card_history
from .conftest import login
from .test_recommend import NUDE, RED, ROSE, _activate_all, _make_docs

MULTI = "上班用的自然唇色，不要霧面"   # 多條件：場合（上班）＋效果（自然）＋排除（霧面）


# 建好合成文件與候選（全部可用、開放給所有人）。
def _setup(db, users):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        _make_docs(c, users["root"])
        _activate_all(c, users["root"])


def _new_conv(client, h) -> str:
    return client.post("/api/conversations", headers=h, json={}).json()["id"]


def _say(client, h, cid, text) -> dict:
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": text})
    assert r.status_code == 201, r.text
    return r.json()


def _sql(db, query, params=()):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        return c.execute(query, params).fetchall()


# ---------------------------------------------------------------------------
# 1. 完整流程：多條件推薦 → 修改偏好 → 回饋，日誌、推薦紀錄、回饋三者一致
# ---------------------------------------------------------------------------
def test_full_flow_recommend_modify_feedback_consistent(client, db, users):
    _setup(db, users)
    h = login(client, "alice")
    cid = _new_conv(client, h)

    # route-v4 起正式路由不出推薦卡：兩則請求送出後改寫成舊版推薦卡紀錄（歷史資料），檢查歷史檢視與回饋
    sent = _say(client, h, cid, MULTI)
    seed_card_history(db, sent["request_id"])
    first = {**client.get(f"/api/requests/{sent['request_id']}", headers=h).json(), "request_id": sent["request_id"]}
    assert first["status"] == "recommended"
    rec1 = first["recommendation"]
    assert [r["option_key"] for r in rec1["recommendations"]] == [NUDE, ROSE]
    assert "trace" not in rec1                                       # 取捨紀錄只留在日誌
    cits = [c for r in rec1["recommendations"] for cl in r["claims"] for c in cl["citations"]]
    assert cits and all(c["available"] is True and "chunk_id" not in c for c in cits)

    sent2 = _say(client, h, cid, "改成聚會")                           # 一次偏好修改（route-v4：重新問答）
    seed_card_history(db, sent2["request_id"], had_prior=True)
    second = {**client.get(f"/api/requests/{sent2['request_id']}", headers=h).json(), "request_id": sent2["request_id"]}
    assert [r["option_key"] for r in second["recommendation"]["recommendations"]] == [ROSE, NUDE, RED]

    # 回頭查看第一次結果：內容與當時回應一致（重新整理後還原）
    view = client.get(f"/api/requests/{first['request_id']}", headers=h).json()
    assert view["status"] == "recommended" and view["question"] == MULTI
    assert view["recommendation"]["recommendations"] == rec1["recommendations"]
    assert view["answer"] == first["answer"] and view["feedback"] is None

    # 對第二次結果的替代方向（rank 2 = NUDE）給回饋
    r = client.put(f"/api/requests/{second['request_id']}/feedback", headers=h,
                   json={"rating": 1, "rank": 2, "reason_codes": ["useful_alternative"], "comment": "SYNTHETIC 試用"})
    assert r.status_code == 200 and r.json()["rank"] == 2

    fb = _sql(db, """SELECT f.request_id, f.rating, o.option_key, rr.rank, rq.detail -> 'recommendation' AS log
                       FROM feedback f
                       JOIN recommendation_options o ON o.id = f.option_id
                       JOIN request_recommendations rr ON rr.request_id = f.request_id AND rr.option_id = f.option_id
                       JOIN requests rq ON rq.id = f.request_id""")
    assert len(fb) == 1
    row = fb[0]
    assert (str(row["request_id"]), row["option_key"], row["rank"]) == (second["request_id"], NUDE, 2)
    logged = [x["option_key"] for x in row["log"]["recommendations"]]
    assert logged[row["rank"] - 1] == NUDE                            # 日誌、推薦紀錄、回饋指向同一方向

    again = client.get(f"/api/requests/{second['request_id']}", headers=h).json()
    assert again["feedback"]["rating"] == 1 and again["feedback"]["rank"] == 2
    hist = client.get(f"/api/conversations/{cid}", headers=h).json()["requests"]
    assert [x["feedback_rating"] for x in hist] == [None, 1]


# ---------------------------------------------------------------------------
# 2. 回饋權限與驗證
# ---------------------------------------------------------------------------
def test_feedback_requires_owner_finished_request_and_valid_rank(client, db, users):
    _setup(db, users)
    alice, bob = login(client, "alice"), login(client, "bob")
    cid = _new_conv(client, alice)
    done = _say(client, alice, cid, MULTI)["request_id"]
    knowledge = _say(client, alice, cid, "豆沙色是什麼意思")["request_id"]

    put = lambda rid, h, body: client.put(f"/api/requests/{rid}/feedback", headers=h, json=body)
    assert put(done, bob, {"rating": 1}).status_code == 404                       # 不是自己的
    assert put(uuid.uuid4(), alice, {"rating": 1}).status_code == 404             # 不存在（回應相同）
    assert put(done, alice, {"rating": 1, "rank": 3}).status_code == 422          # 這次只有 2 個方向
    assert put(knowledge, alice, {"rating": 1, "rank": 1}).status_code == 422     # 沒有推薦的請求不能指定方向
    assert put(done, alice, {"rating": 1, "rank": 4}).status_code == 422          # 超出範圍
    assert put(done, alice, {"rating": 1, "reason_codes": ["<script>"]}).status_code == 422
    assert put(knowledge, alice, {"rating": -1, "reason_codes": ["too_generic"]}).status_code == 200

    # 進行中（started）的請求還沒有結果，不能回饋
    started = uuid.uuid4()
    _sql(db, "INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s, %s, %s, 'q') RETURNING id",
         (started, users["alice"], cid))
    r = put(started, alice, {"rating": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "request_not_finished"
    # 沒帶 CSRF 一律拒絕
    no_csrf = {k: v for k, v in alice.items() if k != "X-CSRF-Token"}
    assert put(done, no_csrf, {"rating": 1}).status_code == 403
    assert _sql(db, "SELECT count(*) AS n FROM feedback")[0]["n"] == 1


def test_request_view_only_for_owner(client, db, users):
    _setup(db, users)
    alice, bob, root = login(client, "alice"), login(client, "bob"), login(client, "root")
    rid = _say(client, alice, _new_conv(client, alice), MULTI)["request_id"]
    assert client.get(f"/api/requests/{rid}", headers=alice).status_code == 200
    for other in (bob, root):                      # 管理者也不能經由使用者端點讀別人的請求
        assert client.get(f"/api/requests/{rid}", headers=other).status_code == 404
    assert client.get(f"/api/requests/{rid}").status_code == 401
    body = client.get(f"/api/requests/{rid}", headers=alice).json()
    assert not ({"detail", "preferences_snapshot", "model_output"} & body.keys())   # 只回畫面需要的欄位
    dumped = json.dumps(body, ensure_ascii=False, default=str)
    for leaked in ("preferences_before", "parse_calls", "\"trace\"", "option_id", "chunk_id"):
        assert leaked not in dumped


# ---------------------------------------------------------------------------
# 3. 受限片段不帶給一般使用者（回應、結果檢視、引用查看）
# ---------------------------------------------------------------------------
def test_restricted_evidence_never_reaches_user(client, db, users):
    _setup(db, users)
    _sql(db, "UPDATE documents SET visibility = 'admin' WHERE doc_key = 'SYN_S7_B' RETURNING id")
    h = login(client, "alice")
    body = _say(client, h, _new_conv(client, h), MULTI)
    seed_card_history(db, body["request_id"])          # route-v4：舊版推薦卡紀錄（日誌含這筆證據因權限失效）
    # 受限方向不能出現在回應或結果檢視
    shown = (body.get("recommendation") or {}).get("recommendations") or []
    assert NUDE not in [r["option_key"] for r in shown]
    # 日誌（管理用）仍完整記錄這筆證據因權限失效
    log = _sql(db, "SELECT detail FROM requests WHERE id = %s", (body["request_id"],))[0]["detail"]
    assert "SYN_S7_B" in json.dumps(log) and "role_not_permitted" in json.dumps(log)
    # 但回應與結果檢視都看不到受限文件的任何鍵值
    view = client.get(f"/api/requests/{body['request_id']}", headers=h).json()
    for payload in (body, view):
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
        assert "SYN_S7_B" not in dumped and "role_not_permitted" not in dumped


def test_history_citation_rechecked_after_source_restricted(client, db, users):
    _setup(db, users)
    alice, root = login(client, "alice"), login(client, "root")
    sent = _say(client, alice, _new_conv(client, alice), MULTI)
    seed_card_history(db, sent["request_id"])          # route-v4：舊版推薦卡紀錄
    body = {**client.get(f"/api/requests/{sent['request_id']}", headers=alice).json(), "request_id": sent["request_id"]}
    cit = body["recommendation"]["recommendations"][0]["claims"][0]["citations"][0]
    q = f"/api/sources/citation?doc={cit['doc_key']}&version={cit['version_label']}&chunk={cit['chunk_key']}"
    ok = client.get(q, headers=alice)
    assert ok.status_code == 200 and ok.json()["quote_text"].startswith("SYNTHETIC evidence")
    assert not ({"source_path", "raw_path", "chunk_id", "id", "review_status"} & ok.json().keys())

    # 事後把整批來源改成管理者限定：S9 起整則舊回答遮蔽（推薦主張、回答文字、引用定位都不回），只留說明
    _sql(db, "UPDATE documents SET visibility = 'admin' RETURNING id")
    view = client.get(f"/api/requests/{body['request_id']}", headers=alice).json()
    assert view["masked"] is True and view["masked_note"]
    assert view["recommendation"] is None and view["answer"] is None and view["limitations"] == []
    assert "SYN_S7_" not in json.dumps(view, ensure_ascii=False)
    assert client.get(q, headers=alice).status_code == 404              # 與「不存在」相同的回應
    assert client.get(q, headers=root).status_code == 200               # 管理者仍可查看


def test_citation_endpoint_rejects_unusable_and_bad_input(client, db, users):
    _setup(db, users)
    h = login(client, "alice")
    key = _sql(db, "SELECT chunk_key FROM chunks LIMIT 1")[0]["chunk_key"]
    doc = "SYN_S7_A" if key.startswith("SYN_S7_A") else "SYN_S7_B"
    base = f"/api/sources/citation?doc={doc}&version=v1&chunk={key}"
    assert client.get(base).status_code == 401                                   # 未登入
    assert client.get(base, headers=h).status_code == 200
    assert client.get(base.replace("v1", "v9"), headers=h).status_code == 404     # 版本不符
    assert client.get(f"/api/sources/citation?doc=../etc&version=v1&chunk={key}", headers=h).status_code == 422
    # 片段被退回（未核准）→ 一般使用者與管理者都看不到
    row = _sql(db, "SELECT id, content_sha256 FROM chunks WHERE chunk_key = %s", (key,))[0]
    _sql(db, "SELECT review_chunk(%s, %s, 'reject', '合成測試退回', %s) AS x",
         (row["id"], users["root"], row["content_sha256"]))
    assert client.get(base, headers=h).status_code == 404
    assert client.get(base, headers=login(client, "root")).status_code == 404
