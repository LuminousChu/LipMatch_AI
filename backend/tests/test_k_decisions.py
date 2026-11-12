"""2026-10-18 K1～K5 決策（解析器 rule-v5）的正反例，以及照片回覆限制。

句子都是 SYNTHETIC 開發句或開發題原句，不是使用者資料。
決策原文與舊規則見 docs/mvp/handoffs/2026-10-18_K1-K5場合與底調決策實作.md。
"""
from __future__ import annotations

from app import preferences as pref
from app import recommend as rec

from .conftest import login

P = pref.RuleParser()
NOW = "2026-10-18T00:00:00+00:00"


# 依序送出多則訊息（每則都走 process_parsed，等同 API 的一輪），回傳最後一輪的 TurnResult。
def _turns(*messages: str) -> pref.TurnResult:
    state = pref.empty_state()
    turn = None
    for i, m in enumerate(messages):
        turn = pref.process_parsed(state, P.parse(m), request_id=f"r{i}", now=NOW)
        state = turn.state
    return turn


# 取偏好狀態中場合的所有值（單一場合或多情境）。
def _occ(state: dict) -> list[str]:
    o = state["occasion"]
    return list(o.get("values") or ([o["value"]] if o.get("value") else []))


# ---------------------------------------------------------------------------
# K1：未提供場合就保持未知；不自動填日常、不強制補問；「拍照」是使用情境
# ---------------------------------------------------------------------------
def test_k1_new_state_occasion_unknown():
    s = pref.empty_state()
    assert s["occasion"]["value"] is None and s["occasion"]["status"] == "unknown"


def test_k1_recommend_without_occasion_not_defaulted_or_forced_to_ask():
    t = _turns("推薦唇色")
    assert _occ(t.state) == [] and t.state["occasion"]["status"] == "unknown"
    assert not any(q["field"] == "occasion" for q in t.clarification)      # 不強制補問場合
    assert not any(q["blocking"] for q in t.clarification)                  # 能回答就先回答
    assert not any("日常" in x or "預設" in x for x in t.limitations)


def test_k1_photo_scenario_is_normal_recommendation_without_occasion():
    t = _turns("拍團體照時嘴唇總是很蒼白，什麼唇色比較上相？")
    assert t.parsed.intent == "recommend" and _occ(t.state) == [] and t.state["conflicts"] == []


def test_k1_said_daily_is_user_occasion():
    # 反例：使用者自己說日常，就是使用者提供的場合（不是預設）
    t = _turns("推薦日常唇色")
    assert t.state["occasion"]["value"] == "daily" and t.state["occasion"]["status"] == "user"


def test_k1_unspecified_round_trip_through_columns_stays_unknown():
    # 資料表欄位不可為空：未指定時寫佔位值 daily／default，讀回來仍是未知，不變成日常
    cols = pref.dump_state(pref.empty_state())
    assert (cols["scenario"], cols["scenario_source"]) == ("daily", "default")
    assert pref.load_state(cols)["occasion"]["status"] == "unknown"


def test_k1_legacy_default_daily_loads_as_unknown():
    legacy = pref.empty_state()
    legacy["occasion"] = {**legacy["occasion"], "value": "daily", "status": "default"}   # 舊版存的預設日常
    s = pref.load_state({"scenario": "daily", "scenario_source": "default", "preferences": legacy})
    assert s["occasion"]["value"] is None and s["occasion"]["status"] == "unknown"


def test_k1_user_column_occasion_still_loaded():
    s = pref.load_state({"scenario": "office", "scenario_source": "user", "preferences": {}})
    assert s["occasion"]["value"] == "office" and s["occasion"]["status"] == "user"


def test_k1_recommendation_has_no_default_assumption():
    t = _turns("推薦唇色")
    _hard, soft, assumptions = rec._conditions(t.state)
    assert assumptions == [] and not any(s["field"] == "occasion" for s in soft)


# ---------------------------------------------------------------------------
# K2：明確撤回場合、沒有新場合 → 清除舊場合，保留其他偏好；這是更新，不是知識問句
# ---------------------------------------------------------------------------
def test_k2_withdraw_occasion_clears_it_and_keeps_others():
    t = _turns("我想要聚會用的唇色", "不要霧面", "忽略我剛剛說的聚會")
    assert t.parsed.intent == "update"
    assert _occ(t.state) == [] and t.state["occasion"]["status"] == "unknown"
    assert [i["value"] for i in t.state["disliked_finishes"]] == ["matte"]


def test_k2_cancel_wording_same_as_ignore():
    t = _turns("我想要聚會用的唇色", "取消聚會")
    assert _occ(t.state) == [] and t.parsed.intent == "update"


def test_k2_withdraw_other_occasion_is_noop():
    # 反例：撤回的不是目前的場合 → 不動
    t = _turns("上班用的唇色", "忽略我剛剛說的聚會")
    assert _occ(t.state) == ["office"]


def test_k2_withdraw_with_new_occasion_sets_new():
    t = _turns("我想要聚會用的唇色", "忽略我剛剛說的聚會，改回上班用")
    assert _occ(t.state) == ["office"] and t.state["occasion"]["status"] == "user"


# ---------------------------------------------------------------------------
# K3：「A 改回 B」是替換；「A、B 都要」是多情境，不自動判衝突、不強迫選一個
# ---------------------------------------------------------------------------
def test_k3_change_back_is_replacement():
    t = _turns("聚會改回上班用")
    assert _occ(t.state) == ["office"] and t.state["conflicts"] == []


def test_k3_both_occasions_is_multi_situation_not_conflict():
    t = _turns("我想要聚會用，也想要上班用")
    assert sorted(_occ(t.state)) == ["office", "party"] and t.state["occasion"]["status"] == "user"
    assert t.state["conflicts"] == [] and not any(q["blocking"] for q in t.clarification)


def test_k3_multi_situation_is_kept_after_storage_round_trip():
    state = _turns("上班後去聚會要擦什麼").state
    cols = pref.dump_state(state)
    assert cols["scenario_source"] == "default"                   # 欄位放不下多情境：只寫佔位值
    assert sorted(_occ(pref.load_state(cols))) == ["office", "party"]   # JSON 保留原始需求


def test_k3_multi_situation_used_as_soft_preference_any_match():
    state = _turns("我想要聚會用，也想要上班用").state
    _hard, soft, _ = rec._conditions(state)
    occ = [s for s in soft if s["field"] == "occasion"]
    assert len(occ) == 1 and sorted(occ[0]["values"]) == ["office", "party"]


def test_k3_multi_then_single_replaces():
    t = _turns("我想要聚會用，也想要上班用", "改成上班")
    assert _occ(t.state) == ["office"]


def test_k3_withdraw_one_of_multi_keeps_the_other():
    t = _turns("我想要聚會用，也想要上班用", "忽略聚會")
    assert _occ(t.state) == ["office"] and t.state["occasion"]["status"] == "user"


# ---------------------------------------------------------------------------
# K4：「覺得／比較像冷底」是暫定自述，保留不確定性，不當成確定底調
# ---------------------------------------------------------------------------
def test_k4_hedged_undertone_is_tentative_not_value():
    t = _turns("我覺得我比較像冷底")
    ut = t.state["undertone"]
    assert ut["value"] is None and ut["status"] == "tentative" and ut["tentative"] == "cool"
    assert ut["raw"] == "我覺得我比較像冷底"
    row = next(r for r in pref.summarize(t.state)["rows"] if r["field"] == "undertone")
    assert row["status"] == "tentative" and "不確定" in row["value"] and "冷底" in row["value"]


def test_k4_other_hedges():
    for text in ("我應該是暖底吧", "我好像是中性底"):
        ut = _turns(text).state["undertone"]
        assert ut["value"] is None and ut["status"] == "tentative", text


def test_k4_plain_self_statement_still_definite():
    ut = _turns("我是冷底").state["undertone"]
    assert ut["value"] == "cool" and ut["status"] == "user" and "tentative" not in ut


def test_k4_definite_statement_replaces_tentative():
    ut = _turns("我覺得我比較像冷底", "我是暖底").state["undertone"]
    assert ut["value"] == "warm" and ut["status"] == "user" and "tentative" not in ut


# ---------------------------------------------------------------------------
# K5：一般知識問句裡的膚色、底調詞不寫入；明確自述才寫入；混合句分開處理
# ---------------------------------------------------------------------------
def test_k5_general_question_writes_nothing():
    for text in ("小麥色是不是暖底", "暖底適合什麼唇色", "黃皮適合什麼顏色"):
        s = _turns(text).state
        assert s["skin_tone"]["value"] is None and s["undertone"]["value"] is None, text


def test_k5_mixed_sentence_records_skin_only():
    s = _turns("我是小麥色，是不是暖底？").state
    assert s["skin_tone"]["value"] == "小麥色" and s["undertone"]["value"] is None


def test_k5_explicit_self_statement_written():
    s = _turns("我是暖底，適合什麼唇色").state
    assert s["undertone"]["value"] == "warm"
    assert _turns("我是小麥色皮膚，推薦什麼？").state["skin_tone"]["value"] == "小麥色"


# ---------------------------------------------------------------------------
# 照片：系統沒有照片功能，回覆不能暗示看過或分析過圖片；純拍照情境正常處理
# ---------------------------------------------------------------------------
def test_photo_request_reply_says_no_image_seen():
    t = _turns("我上傳照片，幫我判斷膚色")
    assert t.parsed.intent == "outside_scope"
    assert any("沒有接收、看過或分析任何圖片" in x for x in t.limitations)


def test_photo_mention_not_caught_as_request_still_discloses():
    # 照片請求被規則漏判、走一般推薦時，仍要說明沒有看過圖片
    t = _turns("瞧瞧我的自拍，推薦唇色")
    assert t.parsed.intent == "recommend"
    assert any("沒有接收、看過或分析任何圖片" in x for x in t.limitations)


def test_pure_shooting_scenario_has_no_photo_disclaimer():
    t = _turns("拍團體照時嘴唇總是很蒼白，什麼唇色比較上相？")
    assert not any("圖片" in x for x in t.limitations)


# ---------------------------------------------------------------------------
# API：新對話場合未指定；回覆與摘要不寫成日常
# ---------------------------------------------------------------------------
def test_api_new_conversation_occasion_unspecified(client, users):
    h = login(client, "alice")
    cid = client.post("/api/conversations", json={}, headers=h).json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": "推薦唇色"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["preferences"]["occasion"]["status"] == "unknown"
    assert "暫以「日常」為預設" not in r.text
    conv = client.get(f"/api/conversations/{cid}", headers=h).json()
    row = next(x for x in conv["summary"]["rows"] if x["field"] == "occasion")
    assert row["value"] == "未指定" and row["status"] == "unknown"
