"""rec-v2 底調：cand-v2 驗證與 cand-v1 相容、rec-v2/H06、conditional 狀態，以及 API／歷史／回饋接通。

測試資料是本檔自建的 TEST_UT 文件與兩張候選（結構同正式的冷底藍調紅／暖底橘紅卡），只存在測試資料庫，不是正式驗收。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app import candidates as gov
from app import preferences as pref
from app import rec_cases
from app import recommend as rec

from .card_history import rule_card_for_request, seed_card_history
from .conftest import login

COOL, WARM, PLAIN = "test-ut-blue-red-cool", "test-ut-orange-red-warm", "test-ut-nude-no-undertone"
QUOTE = "TEST classic red lipstick for all undertones – orange-red for warms and blue-red for cools."
AI_VERIFICATION = {"reviewer": "AI 核對（測試）", "reviewer_type": "ai",
                   "delegated_by": "專案維護者", "delegation_basis": "測試：委託 AI 核對",
                   "checks": [{"item": "來源版本與 SHA", "result": "pass"}]}


def _card(key: str, name: str, undertone: str | None, color: str = "red") -> dict:
    """一張 cand-v2 候選；undertone=None 表示沒有底調條件（未知）。"""
    supports = [f"color_family:{color}"] + ([f"undertones:{undertone}"] if undertone else [])
    return {"option_key": key, "name": name, "description": f"TEST {name}", "verified_by": "TEST",
            "verified_at": "2026-10-25", "verification": copy.deepcopy(AI_VERIFICATION),
            "attributes": {"color_family": color, "finishes": [], "effects": [], "occasions": [], "skin_tones": [],
                           **({"undertones": [undertone]} if undertone else {})},
            "limitations": ["來源沒有指定色號或色值，只是方向。"],
            "evidence": [{"doc_key": "TEST_UT", "version_label": "v1", "chunk_key": "TEST_UT_1#r1",
                          "support_type": "styling_advice", "claim": f"TEST 主張：{name}", "supports": supports}]}


def _spec(*cards: dict) -> dict:
    """cand-v2 候選檔；預設是冷底藍調紅與暖底橘紅兩張。"""
    cards = cards or (_card(COOL, "藍調紅", "cool"), _card(WARM, "橘紅", "warm"))
    return {"schema_version": "cand-v2", "status": "verified", "synthetic": False, "candidates": copy.deepcopy(list(cards))}


def _decide(message: str, spec: dict | None = None) -> rec.Decision:
    """以規則解析器處理一句話，再對候選做純函式判斷（證據全部有效）。"""
    parsed = pref.RuleParser().parse(message)
    state = pref.process_parsed(pref.empty_state(), parsed, request_id=None, now="2026-10-25T00:00:00+00:00").state
    cands = rec_cases.candidates_from_spec(spec or _spec())
    return rec.decide(state, cands, intent=parsed.intent, secondary=parsed.secondary_intents, had_prior=False)


# ---------------------------------------------------------------------------
# 1. 候選檔：cand-v2 驗證、cand-v1 明確相容
# ---------------------------------------------------------------------------
def test_cand_v2_with_undertones_and_ai_verification_is_valid():
    assert gov.validate_spec(_spec()) == []


def test_cand_v1_still_accepted_without_conversion():
    v1 = _spec(_card(PLAIN, "裸色", None, "nude"))
    v1["schema_version"] = "cand-v1"
    del v1["candidates"][0]["verification"]
    assert gov.validate_spec(v1) == []
    real = gov.read_spec(Path(rec.__file__).with_name("recommendation_candidates.json"))   # 既有 cand-v1 檔
    assert real["schema_version"] == "cand-v1"
    synthetic = json.loads(rec_cases.SYNTHETIC_FILE.read_text(encoding="utf-8"))             # 既有 cand-v1 合成檔
    assert synthetic["schema_version"] == "cand-v1" and gov.validate_spec(synthetic) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: s.update(schema_version="cand-v1"), "cand-v1 不支援屬性"),
    (lambda s: s.update(schema_version="cand-v3"), "schema_version"),
    (lambda s: s["candidates"][0]["attributes"].update(undertones=["olive"]), "不在允許清單"),
    (lambda s: s["candidates"][0]["evidence"][0].update(supports=["color_family:red"]), "沒有任何證據支持"),
    (lambda s: s["candidates"][0]["verification"].pop("delegation_basis"), "委託依據"),
    (lambda s: s["candidates"][0]["verification"].update(reviewer_type="robot"), "human 或 ai"),
    (lambda s: s["candidates"][0]["verification"].update(extra="x"), "未定義的欄位"),
])
def test_cand_v2_validation_rejects(mutate, needle):
    spec = _spec()
    mutate(spec)
    errors = gov.validate_spec(spec)
    assert errors and any(needle in e for e in errors), errors


def test_cand_v1_rejects_verification_field():
    spec = _spec(_card(PLAIN, "裸色", None, "nude"))
    spec["schema_version"] = "cand-v1"
    assert any("cand-v1 不支援 verification" in e for e in gov.validate_spec(spec))


# ---------------------------------------------------------------------------
# 2. 判斷：H06、conditional、中性底、未知與 tentative
# ---------------------------------------------------------------------------
def test_known_cool_keeps_only_cool_card_and_logs_h06():
    d = _decide("我是冷底，推薦唇色")
    assert (d.status, [r["option_key"] for r in d.recommendations]) == ("recommended", [COOL])
    warm = next(t for t in d.trace if t["option_key"] == WARM)
    assert warm["reasons"] == [{"rule": "rec-v2/H06", "code": "undertone_condition_not_met", "value": "cool",
                                "label": "冷底調", "source_undertones": ["warm"]}]
    claims = d.recommendations[0]["claims"]
    assert any(c["text"] == "符合你說的底調：冷底調" for c in claims)
    texts = [c["text"] for c in claims if c["citations"]]
    assert len(texts) == len(set(texts))                       # 同一筆證據不因色系＋底調重複列兩次
    assert any("undertones:cool" in (c.get("also_supports") or []) or c.get("supports") == "undertones:cool"
               for c in claims)


def test_known_warm_keeps_only_warm_card():
    d = _decide("我是暖底，推薦唇色")
    assert (d.status, [r["option_key"] for r in d.recommendations]) == ("recommended", [WARM])


def test_neutral_with_no_neutral_evidence_is_honest_no_match_without_relax_question():
    d = _decide("我是中性底，推薦唇色")
    assert (d.status, d.reason, d.question, d.recommendations) == ("no_match", "undertone_condition_not_met", None, [])
    text = rec.render_text(d)
    assert "中性底調" in text and "不表示這些顏色不能用" in text and "放寬" not in text


def test_unknown_undertone_gives_conditional_cards_and_one_question():
    d = _decide("推薦唇色")
    assert (d.status, d.reason) == ("conditional", "user_undertone_unknown")
    assert [(r["option_key"], r["role"], r["condition"]["text"]) for r in d.recommendations] == [
        (COOL, "conditional", "如果你是冷底調"), (WARM, "conditional", "如果你是暖底調")]
    assert d.question.startswith("你的底調是冷底、暖底還是中性底")
    assert {t["decision"] for t in d.trace} == {"selected_conditional"}
    assert "你的底調還沒確定" in rec.render_text(d)


def test_tentative_undertone_is_not_treated_as_known():
    d = _decide("我覺得我比較像冷底，推薦唇色")
    assert (d.status, d.reason) == ("conditional", "user_undertone_tentative")
    assert len(d.recommendations) == 2 and "不當成確定底調" in d.question


@pytest.mark.parametrize("message", ["我是小麥色，推薦唇色", "我皮膚白皙，推薦唇色"])
def test_skin_tone_does_not_imply_undertone(message):
    d = _decide(message)
    assert d.status == "conditional"                          # 小麥色／白皙都不推定底調：兩張都列、照樣追問


def test_card_without_undertone_is_not_excluded_and_discloses():
    spec = _spec(_card(COOL, "藍調紅", "cool"), _card(PLAIN, "裸色", None, "nude"))
    d = _decide("我是暖底，推薦唇色", spec)
    assert (d.status, [r["option_key"] for r in d.recommendations]) == ("recommended", [PLAIN])
    assert any("不是依你的底調" in lim for lim in d.recommendations[0]["limitations"])


def test_neutral_with_unrestricted_card_recommends_it_not_no_match():
    spec = _spec(_card(COOL, "藍調紅", "cool"), _card(PLAIN, "裸色", None, "nude"))
    d = _decide("我是中性底，推薦唇色", spec)
    assert (d.status, [r["option_key"] for r in d.recommendations]) == ("recommended", [PLAIN])


def _effect_card(key: str, name: str, color: str, effect: str) -> dict:
    """沒有底調條件、有一個已核對效果的候選。"""
    c = _card(key, name, None, color)
    c["attributes"]["effects"] = [effect]
    c["evidence"][0]["supports"].append(f"effects:{effect}")
    return c


NATURAL, VIVID = "test-natural-pink", "test-vivid-red"
FULL = (_card(COOL, "藍調紅", "cool"), _card(WARM, "橘紅", "warm"),
        _effect_card(NATURAL, "自然粉色", "pink", "natural"), _effect_card(VIVID, "鮮明紅唇", "red", "vivid"))


def test_neutral_card_set_recommends_matching_effect_first():
    spec = _spec(*FULL)
    spec["candidates"][0]["attributes"]["undertones"] = ["cool", "neutral"]
    spec["candidates"][0]["evidence"][0]["supports"].append("undertones:neutral")
    d = _decide("我是中性底，想要自然的唇色", spec)
    assert (d.status, [r["option_key"] for r in d.recommendations]) == ("recommended", [NATURAL])
    cited = [c for c in d.recommendations[0]["claims"] if c["citations"]]
    assert len(cited) == 1 and cited[0]["also_supports"] == ["effects:natural"]   # 同一證據支持色系＋效果只列一次
    d = _decide("我是中性底，推薦唇色", spec)
    assert d.status == "recommended" and d.recommendations[0]["option_key"] == COOL   # 中性底相符者優先
    assert any(c["text"] == "符合你說的底調：中性底調" for c in d.recommendations[0]["claims"])
    assert d.effect_choice is True                                       # 效果有差異：值得問


def test_unknown_undertone_with_effect_match_is_not_conditional():
    d = _decide("想要自然的唇色", _spec(*FULL))
    # 選出的方向不限底調：底調不影響結果，照常推薦、不追問底調
    assert (d.status, d.question, [r["option_key"] for r in d.recommendations]) == ("recommended", None, [NATURAL])


def test_conditional_lists_unrestricted_card_as_any_undertone():
    d = _decide("推薦唇色", _spec(*FULL))
    assert d.status == "conditional"
    texts = {r["option_key"]: r["condition"]["text"] for r in d.recommendations}
    assert texts[COOL] == "如果你是冷底調" and texts[WARM] == "如果你是暖底調"
    assert all(t == "不限底調（來源沒有底調條件）" for k, t in texts.items() if k not in (COOL, WARM))


def test_effect_choice_false_when_cards_do_not_differ_or_none_pass():
    assert _decide("我是冷底，推薦唇色").effect_choice is False             # 兩張卡都沒有效果資料
    assert _decide("我是中性底，推薦唇色").effect_choice is False           # 沒有通過的方向


def test_hard_exclusion_mixed_with_h06_still_asks_relax():
    d = _decide("我是冷底，推薦唇色，不要紅色")
    assert (d.status, d.reason) == ("no_match", "all_candidates_excluded_by_hard_conditions")
    assert d.question and "放寬" in d.question


# ---------------------------------------------------------------------------
# 3. 資料庫＋API：載入啟用、conditional 回應、歷史、回饋
# ---------------------------------------------------------------------------
def _setup(db, admin_id):
    """建立 TEST_UT 文件（核准、發布），載入並啟用兩張 cand-v2 候選。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        doc = conn.execute("INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible) "
                           "VALUES ('TEST_UT', 'TEST 底調來源', 'other', 'all', TRUE, TRUE) RETURNING id").fetchone()["id"]
        v = conn.execute("INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version) "
                         "VALUES (%s, 'v1', %s, 'test') RETURNING id",
                         (doc, hashlib.sha256(b"test-ut").hexdigest())).fetchone()["id"]
        sha = hashlib.sha256(QUOTE.encode()).hexdigest()
        cid = conn.execute("INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, "
                           "search_text, content_sha256) VALUES (%s, 'TEST_UT_1#r1', 0, 1, %s, %s, %s) RETURNING id",
                           (v, QUOTE, QUOTE, sha)).fetchone()["id"]
        conn.execute("SELECT review_chunk(%s, %s, 'approve', '測試核准', %s)", (cid, admin_id, sha))
        conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s", (v,))
        loaded = gov.load_spec(conn, _spec(), admin_id)
        for c in _spec()["candidates"]:
            gov.activate(conn, c["option_key"], admin_id)
        row = conn.execute("SELECT attributes FROM recommendation_options WHERE option_key = %s", (COOL,)).fetchone()
    return loaded, row["attributes"]


def _say(client, h, cid, text):
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_db_load_keeps_schema_and_verification(db, users):
    loaded, attrs = _setup(db, users["root"])
    assert [x["result"] for x in loaded] == ["created_draft", "created_draft"]
    assert attrs["schema_version"] == "cand-v2" and attrs["undertones"] == ["cool"]
    assert attrs["verification"]["reviewer_type"] == "ai" and attrs["verification"]["delegated_by"]


def test_api_conditional_then_answer_undertone(client, db, users):
    """route-v4：正式路由不出推薦卡（底調未定、回答底調後都走問答）；規則推薦本身與舊推薦卡紀錄的歷史檢視照舊。"""
    _setup(db, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]

    first = _say(client, h, cid, "推薦唇色")
    assert first.get("recommendation") in (None, {})
    d1 = rule_card_for_request(db, first["request_id"])
    assert [(r["option_key"], r["role"], r["condition"]["labels"]) for r in d1.recommendations] == [
        (COOL, "conditional", ["冷底調"]), (WARM, "conditional", ["暖底調"])]
    # 一般使用者可以展開引用原文；支持單位的片段鍵含 #（前端以 URLSearchParams 編碼成 %23）
    src = client.get("/api/sources/citation", headers=h,
                     params={"doc": "TEST_UT", "version": "v1", "chunk": "TEST_UT_1#r1"})
    assert src.status_code == 200 and src.json()["quote_text"] == QUOTE

    second = _say(client, h, cid, "我是冷底")
    assert second.get("recommendation") in (None, {})
    with psycopg.connect(db, row_factory=dict_row) as c:
        d2_log = c.execute("SELECT detail FROM requests WHERE id = %s", (second["request_id"],)).fetchone()["detail"]
    assert d2_log["route"]["reason"] == "update_after_recommend"       # 接回推薦需求，用冷底重新問答
    # 舊版推薦卡紀錄（歷史資料）：卡片還原；回饋可以指定方向的 rank
    d2 = seed_card_history(db, second["request_id"], had_prior=True)
    assert d2.status == "recommended" and [r["option_key"] for r in d2.recommendations] == [COOL]
    view = client.get(f"/api/requests/{second['request_id']}", headers=h).json()
    assert view["status"] == "recommended" and [r["option_key"] for r in view["recommendation"]["recommendations"]] == [COOL]
    r = client.put(f"/api/requests/{second['request_id']}/feedback", headers=h, json={"rating": 1, "rank": 1})
    assert r.status_code == 200 and r.json()["rank"] == 1

    hist = client.get(f"/api/conversations/{cid}", headers=h).json()["requests"]
    assert [x["feedback_rating"] for x in hist] == [None, 1]
    with psycopg.connect(db, row_factory=dict_row) as c:
        recs = c.execute("""SELECT rr.rank, o.option_key FROM request_recommendations rr
                              JOIN recommendation_options o ON o.id = rr.option_id
                             WHERE rr.request_id = %s ORDER BY rr.rank""", (second["request_id"],)).fetchall()
        first_recs = c.execute("SELECT count(*) AS n FROM request_recommendations WHERE request_id = %s",
                               (first["request_id"],)).fetchone()["n"]
    assert [(x["rank"], x["option_key"]) for x in recs] == [(1, COOL)] and first_recs == 0   # 正式路由不寫推薦紀錄
    assert d2.to_dict()["rules_version"] == "rec-v2"
    h06 = [t for t in d2.trace if t["option_key"] == WARM][0]["reasons"]
    assert h06[0]["rule"] == "rec-v2/H06"


def test_source_deactivation_removes_undertone_cards(db, users):
    _setup(db, users["root"])
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("UPDATE chunks SET is_active = FALSE WHERE chunk_key = 'TEST_UT_1#r1'")
        parsed = pref.RuleParser().parse("推薦唇色")
        state = pref.process_parsed(pref.empty_state(), parsed, request_id=None, now="2026-10-25T00:00:00+00:00").state
        d = rec.run(conn, state, role="user", intent=parsed.intent, secondary=parsed.secondary_intents,
                    had_prior=False)
    # 來源停用後底調依據也一起失效：不再出現條件式方向，也不殘留「如果你是冷底」
    assert (d.status, d.recommendations) == ("insufficient_evidence", [])


def test_api_neutral_gets_undertone_no_match_message(client, db, users):
    """規則推薦：中性底在只有冷暖條件的卡片裡 → no_match（底調條件不符），不問放寬；正式路由走問答。"""
    _setup(db, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    body = _say(client, h, cid, "我是中性底，推薦唇色")
    assert body.get("recommendation") in (None, {})
    d = rule_card_for_request(db, body["request_id"])
    assert (d.status, d.reason, d.question) == ("no_match", "undertone_condition_not_met", None)


def test_api_effect_question_only_when_cards_differ(client, db, users):
    """Q5（2026-11-01）：推薦需求的「希望效果」補問保留，附在問答之後（非阻斷）；規則卡在效果上確實有分支。"""
    _setup(db, users["root"])
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as conn:
        extra = _spec(_effect_card(NATURAL, "自然粉色", "pink", "natural"), _effect_card(VIVID, "鮮明紅唇", "red", "vivid"))
        gov.load_spec(conn, extra, users["root"])
        for c in extra["candidates"]:
            gov.activate(conn, c["option_key"], users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    body = _say(client, h, cid, "我是冷底，推薦唇色")
    assert [q["question"] for q in body["clarification"]] == ["希望效果比較自然低調，還是鮮明有精神？"]
    assert rule_card_for_request(db, body["request_id"]).effect_choice


# ---------------------------------------------------------------------------
# brief-v1（使用者 2026-10-25 真人判定：卡片只要「顏色＋為什麼」，限制與來源收進參考依據）
# ---------------------------------------------------------------------------
def test_brief_warm_gives_color_and_reason_only():
    d = _decide("我是暖底，推薦唇色")
    assert d.brief["headline"] is None
    assert d.brief["items"] == [{"rank": 1, "role": "primary", "name": "橘紅", "condition": None, "why": "TEST 主張：橘紅"}]
    text = rec.render_text(d)
    assert text == "推薦：橘紅——TEST 主張：橘紅" and "限制" not in text


def test_brief_neutral_headline_lists_cards_that_fit_neutral():
    def both(key, name, ut):
        c = _card(key, name, ut)
        c["attributes"]["undertones"] = [ut, "neutral"]
        c["evidence"].append({"doc_key": "TEST_UT", "version_label": "v1", "chunk_key": "TEST_UT_1#r2",
                              "support_type": "styling_advice", "claim": "來源說中性底調幾乎什麼顏色都能駕馭。",
                              "supports": ["undertones:neutral"]})
        return c
    d = _decide("我是中性底，推薦唇色", _spec(both(COOL, "藍調紅", "cool"), both(WARM, "橘紅", "warm")))
    assert d.status == "recommended"
    assert d.brief["headline"] == "中性底調幾乎什麼顏色都能駕馭；藍調紅、橘紅都適合。" and d.brief["items"] == []
    assert rec.render_text(d) == d.brief["headline"]


def test_brief_conditional_keeps_condition_and_question():
    d = _decide("推薦唇色")
    text = rec.render_text(d)
    assert "你的底調還沒確定" in text and "如果你是冷底調：藍調紅" in text and text.endswith(d.question)
