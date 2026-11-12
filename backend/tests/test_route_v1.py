"""route-v1／v2（使用者 2026-10-26）：知識題不走推薦卡；推薦卡沒涵蓋需求改走問答；追問前先給有依據的起點。
route-v2（方案 B）：卡片只有 recommended 且主要方向沒有 unmet 才出卡；conditional 或有 unmet 改走問答。
route-v4（2026-11-01，方案第二版）：正式路由不跑規則推薦、不出推薦卡，推薦需求一律走問答；
推薦需求之後只改條件，接上一輪提問重新問答。下面第 3 點的出卡測試改成 route-v4 的行為。

整支測試在檢查什麼（句子是主 20 題原句或 SYNTHETIC 開發句；推薦卡用 test_rec_v2_undertone 的 TEST 冷暖紅兩張卡）：
1. 解析 rule-v8：「不要偏粉或偏橘的紅」只排斥偏粉、偏橘，不和「想找紅色」衝突；「怎麼說／怎麼形容」算知識。
2. 知識、比較（含混合意圖）→ 問答，不出推薦卡；比較不再回「模型未啟用」。
3. 推薦需求（route-v4）：一律問答，不跑規則推薦（detail 沒有 recommendation，route 記 skipped_by_route）。
4. 非衝突的阻斷補問（有精神、平常擦法）→ 先檢索給起點，補問照樣附上；真的衝突（同時要與不要）→ 先澄清、不檢索。
5. 「改成聚會用」「你剛剛給的第一個…」這種接上一輪的句子，檢索時接上這個對話上一則提問。
測試庫只有兩段 TEST 卡片來源、沒有生成模型：問答會是證據不足或失敗，這裡只看路由與送進檢索的查詢。
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from app import preferences as pref

from .conftest import login
from .test_rec_v2_undertone import COOL, _say, _setup

P = pref.RuleParser()
W01 = "我想找「紅色」口紅，可是不要那種偏粉或偏橘的紅，這樣要怎麼說比較準？"
W03 = "豆沙色跟奶茶色差在哪裡？"
M01 = "我是小麥色皮膚，推薦什麼？"
M03 = "我是冷底，想擦紅色，要選哪一種紅比較適合？"
N04 = "最近心情很低落，想擦一個讓自己看起來有精神一點的顏色。"
P02 = "我是冷底，可是很想擦橘色系，有辦法擦得比較協調嗎？"
P04_T2 = "你剛剛給的第一個和第二個方向，哪個比較好搭我已經有的粉色腮紅？"
# rule-v14（K1）：P02 是問做法的問句（有辦法…嗎），不再因帶了冷底、橘色就判成推薦；要測「推薦卡色系不符」改用明說推薦的開發句
P02_REC = "我是冷底，可是很想擦橘色系，推薦什麼唇色？"   # SYNTHETIC 開發句


# ---------------------------------------------------------------------------
# 1. 解析 rule-v8
# ---------------------------------------------------------------------------
def test_qualified_head_color_is_not_disliked():
    p = P.parse(W01)
    assert p.conflicts == []
    assert {(c["field"], c["key"]) for c in p.changes} == {
        ("liked_colors", "red"), ("disliked_colors", "pink"), ("disliked_colors", "orange")}
    assert (p.intent, p.secondary_intents) == ("knowledge", ["recommend"])


def test_plain_dislike_still_recorded():
    p = P.parse("我想找唇色，不要紅色")
    assert ("disliked_colors", "red") in {(c["field"], c["key"]) for c in p.changes}


@pytest.mark.parametrize("text", ["這種顏色要怎麼形容？", "乾燥玫瑰色要怎麼描述比較準？"])
def test_how_to_describe_is_knowledge(text):
    assert P.parse(text).intent == "knowledge"


# ---------------------------------------------------------------------------
# 2～5. API 路由
# ---------------------------------------------------------------------------
@pytest.fixture
def seen_queries(monkeypatch):
    """攔截送進 qa.answer_question 的查詢（仍照常執行）。"""
    from app import qa
    seen: list[str] = []
    real = qa.answer_question

    def spy(conn, question, role, config, **kw):
        """記下查詢後照常執行。"""
        seen.append(question)
        return real(conn, question, role, config, **kw)

    monkeypatch.setattr(qa, "answer_question", spy)
    return seen


@pytest.fixture
def chat(client, db, users):
    """有 TEST 冷暖紅卡片的對話；回傳 (送訊息函式, 讀 detail 函式)。"""
    _setup(db, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]

    def detail(body):
        """讀這則請求的 detail（唯讀）。"""
        with psycopg.connect(db, row_factory=dict_row) as conn:
            return conn.execute("SELECT detail FROM requests WHERE id = %s", (body["request_id"],)).fetchone()["detail"]
    return (lambda text: _say(client, h, cid, text)), detail


def test_compare_goes_to_qa_not_model_not_configured(chat, seen_queries):
    say, detail = chat
    body = say(W03)
    assert seen_queries == [W03]
    assert body["error_code"] != "model_not_configured" and body["status"] != "not_answered"
    assert detail(body)["route"]["reason"] == "knowledge_or_compare"


def test_knowledge_with_recommend_goes_to_qa_without_cards(chat, seen_queries):
    say, detail = chat
    body = say(W01)
    assert seen_queries == [W01]
    assert body.get("recommendation") in (None, {}) and body["clarification"] == []


@pytest.mark.parametrize("text", [M03, "推薦唇色", "我是冷底，想找上班也能擦的紅色", P02_REC, M01])
def test_route_v4_recommend_need_goes_to_qa_without_rule_card(chat, seen_queries, text):
    """route-v4：推薦需求（卡片涵蓋、conditional、unmet、卡片看不到的膚色）一律問答；不跑規則推薦、不出卡。"""
    say, detail = chat
    body = say(text)
    assert seen_queries == [text] and body.get("recommendation") in (None, {})
    d = detail(body)
    assert d["route"]["reason"] == "default_rag" and d["route"]["recommendation"] == "skipped_by_route"
    assert "recommendation" not in d and d["route"]["version"] == "route-v4"


def test_rule_v14_p02_how_to_question_goes_straight_to_qa(chat, seen_queries):
    """rule-v14：P02 問做法，直接走問答、不跑推薦（之前是推薦 → 卡片色系不符才退到問答）。"""
    say, detail = chat
    body = say(P02)
    assert seen_queries == [P02]
    d = detail(body)
    assert (d["intent"], d["route"]["reason"]) == ("knowledge", "knowledge_or_compare")
    assert "recommendation" not in d


def test_vitality_gives_starting_point_and_keeps_question(chat, seen_queries):
    say, detail = chat
    body = say(N04)
    assert seen_queries == [N04]
    assert any("好氣色" in q["question"] for q in body["clarification"])
    assert detail(body)["route"]["reason"] == "starting_point_before_clarification"


def test_real_conflict_clarifies_first_without_retrieval(chat, seen_queries):
    say, detail = chat
    body = say("推薦唇色，我想要霧面，不要霧面")
    assert seen_queries == []
    assert body["status"] == "needs_clarification"
    assert detail(body)["route"]["reason"] == "conflict_first"


def test_refers_back_query_includes_previous_question(chat, seen_queries):
    say, detail = chat
    say(M03)                                       # route-v4：第一輪也走問答（測試庫沒有生成模型，沒有交付方向）
    body = say(P04_T2)
    # 上一輪沒有交付方向時，只接上一輪提問（有交付時帶方向，見 test_route_v2_previous_qa_claims_are_carried）
    assert seen_queries[0] == M03 and seen_queries[1].startswith(M03) and seen_queries[1].endswith(P04_T2)
    d = detail(body)
    assert d["route"]["qa_question"] == seen_queries[1] and d["route"]["carry_over"]["previous_question"] == M03


def test_update_query_includes_previous_question(chat, seen_queries):
    say, _ = chat
    say(P02_REC)
    say("改成聚會用")
    assert seen_queries == [P02_REC, f"{P02_REC}；改成聚會用"]


def test_route_v2_occasion_update_keeps_other_conditions(chat, seen_queries):
    """P03：上一輪「上班用、不要太亮」→ 本輪「改成聚會用」：只換場合詞，「不要太亮」保留，不再帶「上班」。"""
    say, detail = chat
    say("我想要上班用、不要太亮的唇色")
    body = say("改成聚會用")
    assert seen_queries == ["我想要上班用、不要太亮的唇色", "我想要聚會用、不要太亮的唇色"]
    assert detail(body)["route"]["carry_over"]["swapped"] == ["上班", "聚會"]


def test_route_v2_previous_qa_claims_are_carried(chat, seen_queries, monkeypatch):
    """上一輪走問答時，帶的是上一輪交付的非背景主張（背景主張不帶）。"""
    from app import main as M
    detail = {"answer_source": "generation_model",
              "qa": {"claims": [{"text": "如果是冷底，選藍調紅。", "role": "unjudged"},
                                {"text": "眼影換成柔和色。", "role": "background"}]}}
    assert M._previous_directions(detail) == ["如果是冷底，選藍調紅。"]
    assert M._previous_directions({"answer_source": None, "recommendation": {"recommendations": [{"name": "x"}]}}) == []


def test_pure_update_without_recommendation_does_not_retrieve(client, users, seen_queries):
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    body = _say(client, h, cid, "我不要霧面")
    assert seen_queries == [] and body["status"] == "preferences_updated"


def test_route_v4_decisions_without_card():
    """route-v4 的判斷不需要推薦結果：出口 → 衝突 → 知識比較 → 只改條件（推薦需求後重新問答）→ 預設問答。"""
    from app import main as M
    rp = lambda text: P.parse(text)                                     # noqa: E731
    assert M.ROUTE_VERSION == "route-v4"
    assert M._route(rp("我不要霧面"), [], hold=False, conflicts=[])["reason"] == "preferences_only"
    assert M._route(rp("改成聚會用"), [], hold=False, conflicts=[], after_recommend=True)["reason"] == "update_after_recommend"
    assert M._route(rp(M03), [], hold=False, conflicts=[])["reason"] == "default_rag"
    assert M._route(rp(W03), [], hold=False, conflicts=[])["reason"] == "knowledge_or_compare"
    assert M._was_recommend_need({"detail": {"intent": "knowledge", "secondary_intents": ["recommend"]}})
    assert not M._was_recommend_need({"detail": {"intent": "knowledge", "secondary_intents": []}})


def test_rule_v9_not_dramatic_is_avoided_vivid_and_still_knowledge():
    """rule-v9：「不要太誇張」記成不想要鮮明亮眼；「有什麼擦法」仍是知識（P05 不因多一個條件被判成修改）。"""
    p = P.parse("我想試試看不一樣的紅唇，但不要太誇張，有什麼擦法？")
    assert p.intent == "knowledge"
    assert ("avoided_effects", "vivid") in [(c["field"], c.get("key")) for c in p.changes]
    assert pref.PARSER_VERSION == "rule-v15" and pref.VOCAB["version"] == "pref-vocab-draft-4"


def test_avoid_v1_pending_ambiguity_passed_to_qa():
    """「不要太亮」還沒確認意思時，也以 pending_ambiguity 交給問答（模型看得到、後處理用來檢查）。"""
    from app import main as M
    state = pref.process_parsed(pref.empty_state(), P.parse("我想要上班用、不要太亮的唇色"), request_id="t",
                                now="2026-10-26T00:00:00+00:00", text="我想要上班用、不要太亮的唇色").state
    got = [c for c in M._explicit_conditions(state) if c["field"] == "pending_ambiguity"]
    assert got and got[0]["value"] == "brightness" and got[0]["label"] == "不要太亮"


def test_carry_v2_retrieval_question_without_directions():
    """carry-v2（2026-11-03，P04）：指回上一輪時，給模型的問句帶上一輪方向，檢索問句只用「上一題＋本題」。"""
    from app import main as M
    from app import preferences as P
    prev = {"question": "我是冷底，想擦紅色，要選哪一種紅比較適合？",
            "detail": {"answer_source": "generation_model",
                       "qa": {"claims": [{"text": "如果是冷底，選擇藍調的紅色會比較適合。", "role": "unjudged"}]}}}
    q2 = "你剛剛給的第一個方向，好搭我已經有的粉色腮紅嗎？"   # SYNTHETIC
    qa_q, carry = M._carry_question(prev, q2, P.RuleParser().parse(q2), {})
    assert carry["method"] == "previous_directions"
    assert "藍調的紅色" in qa_q and q2 in qa_q                                  # 模型看得到方向
    assert carry["retrieval_question"] == f"{prev['question']}；{q2}"            # 檢索不帶方向句
