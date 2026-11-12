"""S7 推薦：硬排除、排序、主推與替代、證據資格（來源停用／過期／角色）、候選治理、API。

SYNTHETIC：本檔的候選與證據全部是合成資料（eval/s7_synthetic_candidates.json、SYN_S7_* 文件），
只驗證規則與資格檢查照設計運作，不是正式推薦、不能當正式驗收。
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

from .card_history import rule_card_for_request
from .conftest import login

SPEC = json.loads(rec_cases.SYNTHETIC_FILE.read_text(encoding="utf-8"))
REAL_FILE = Path(rec.__file__).with_name("recommendation_candidates.json")
SHA = hashlib.sha256(b"synthetic-s7").hexdigest()
NUDE, ROSE, RED = "synthetic-nude-satin", "synthetic-rose-satin", "synthetic-red-gloss"
BEAN, CORAL = "synthetic-bean-paste-matte", "synthetic-coral-unknown"


# 用 S6 規則解析器把一句話變成偏好狀態，回傳 (state, parsed)。
def _state(*messages: str):
    state, parsed = pref.empty_state(), None
    for m in messages:
        parsed = pref.RuleParser().parse(m)
        state = pref.process_parsed(state, parsed, request_id=None, now="2026-10-15T00:00:00+00:00").state
    return state, parsed


# 對一組訊息做純函式判斷（全部證據有效，或依 invalid 指定失效）。
def _decide(*messages: str, invalid=None, cands=None, had_prior=False):
    state, parsed = _state(*messages)
    cands = rec_cases.candidates_from_spec(SPEC, invalid) if cands is None else cands
    return rec.decide(state, cands, intent=parsed.intent, secondary=parsed.secondary_intents, had_prior=had_prior)


def _keys(d: rec.Decision) -> list[str]:
    return [r["option_key"] for r in d.recommendations]


def _trace(d: rec.Decision, key: str) -> dict:
    return next(t for t in d.trace if t["option_key"] == key)


# ---------------------------------------------------------------------------
# 1. 驗收情境（與 docs/mvp/s7-decision-log.md 相同）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sc", rec_cases.SCENARIOS, ids=[s["id"] for s in rec_cases.SCENARIOS])
def test_acceptance_scenarios(sc):
    result = rec_cases.run_scenario(sc, SPEC)
    assert result["check"] == []


# ---------------------------------------------------------------------------
# 2. 判斷規則（純函式）
# ---------------------------------------------------------------------------
def test_unknown_finish_cannot_prove_not_matte():
    d = _decide("上班用的自然唇色，不要霧面")
    assert {r["code"] for r in _trace(d, CORAL)["reasons"]} == {"finish_unknown"}
    assert CORAL not in _keys(d)
    # 沒有排斥妝感時，妝感未知不構成排除
    d2 = _decide("推薦唇色")
    assert _trace(d2, CORAL)["decision"] != "excluded"


def test_disliked_color_and_avoided_effect_are_hard():
    d = _decide("推薦唇色，不要裸色，不要鮮豔")
    assert _trace(d, NUDE)["reasons"][0]["code"] == "disliked_color"
    assert _trace(d, RED)["reasons"][0]["code"] == "avoided_effect"
    assert NUDE not in _keys(d) and RED not in _keys(d)


def test_no_padding_and_at_most_two_alternatives():
    d = _decide("推薦唇色")       # 沒有任何軟偏好：通過者都可當替代，但最多 2 個
    assert len(d.recommendations) == 3
    assert [t["not_selected_reason"] for t in d.trace if t["decision"] == "not_selected"] == \
        ["alternatives_full", "alternatives_full"]
    d2 = _decide("上班用的自然唇色，不要霧面")   # 紅色一項都不符合 → 不湊數
    assert _trace(d2, RED)["not_selected_reason"] == "matches_no_stated_preference"


def test_alternative_must_differ_in_look():
    cands = rec_cases.candidates_from_spec(SPEC)
    twin = copy.deepcopy(next(c for c in cands if c.option_key == NUDE))
    twin.option_key, twin.option_id = "synthetic-nude-satin-twin", 99
    d = _decide("上班用的自然唇色，不要霧面", cands=cands + [twin])
    assert _trace(d, "synthetic-nude-satin-twin")["not_selected_reason"] == f"same_look_as:{NUDE}"


# 2026-10-18 K1 修訂：舊預期 CORAL 與 NUDE、ROSE 同分（日常證據打破平手）；不假設日常後五個候選全部同分。
def test_tie_is_stable_and_disclosed():
    d = _decide("推薦唇色")
    first = d.recommendations[0]
    assert first["option_key"] == BEAN and first["tied_with"] == [CORAL, NUDE, RED, ROSE]
    assert "同樣符合" in first["reason"]


# 2026-10-18 K1 修訂：舊版斷言「場合未指定，暫以日常為預設」並揭露；現在不假設任何場合，也就沒有預設可揭露。
def test_unspecified_occasion_is_not_assumed():
    d = _decide("推薦唇色")
    assert d.assumptions == []
    assert "預設" not in rec.render_text(d) and "日常" not in rec.render_text(d)
    # 沒有場合偏好，所以不會出現「符合你提出的場合」
    texts = [c["text"] for r in d.recommendations for c in r["claims"]]
    assert not any(t.startswith("符合你提出的場合") for t in texts)


def test_skin_tone_without_evidence_is_not_a_reason():
    d = _decide("我是小麥色，推薦唇色")
    for r in d.recommendations:
        assert any("不是依你的膚色推薦" in lim for lim in r["limitations"])
        assert not any(c.get("supports", "").startswith("skin_tones") for c in r["claims"])
    assert "暖" not in rec.render_text(d)   # 不從小麥色推定暖底調


def test_skin_tone_with_evidence_is_cited():
    spec = copy.deepcopy(SPEC)
    rose = next(c for c in spec["candidates"] if c["option_key"] == ROSE)
    rose["attributes"]["skin_tones"] = ["小麥色"]
    rose["evidence"].append({"doc_key": "SYN_S7_A", "version_label": "v1", "chunk_key": "SYN_S7_A_ROSE_skin",
                             "support_type": "styling_advice", "claim": "【SYNTHETIC】合成膚色證據",
                             "supports": ["skin_tones:小麥色"]})
    # 2026-10-18 K1 修訂：舊句「我是小麥色，推薦唇色」靠預設日常把 ROSE 排進前三；改為說出緞光，讓 ROSE 依偏好入選
    d = _decide("我是小麥色，推薦緞光唇色", cands=rec_cases.candidates_from_spec(spec))
    r = next(x for x in d.recommendations if x["option_key"] == ROSE)
    assert any(c.get("supports") == "skin_tones:小麥色" and c["citations"] for c in r["claims"])
    assert not any("膚色" in lim for lim in r["limitations"])


def test_knowledge_question_not_forced_and_update_only_when_in_flow():
    assert _decide("豆沙色是什麼意思").status == "not_applicable"
    assert _decide("改成聚會").status == "not_applicable"            # 從沒請求推薦：只更新條件
    assert _decide("改成聚會", had_prior=True).status == "recommended"


def test_conflict_blocks_recommendation():
    state, _ = _state("要霧面，不要霧面")
    d = rec.decide(state, rec_cases.candidates_from_spec(SPEC), intent="product_request", secondary=[])
    assert d.status == "needs_clarification" and d.recommendations == []


def test_products_never_output_and_no_safety_claim():
    d = _decide("我對羊毛脂過敏，推薦哪一款唇膏，預算 500 元")
    assert d.status == "recommended" and d.product_enabled is False
    assert any("成分核對功能未啟用" in n for n in d.limitations)
    assert any("商品功能未啟用" in n for n in d.limitations)
    text = json.dumps(d.to_dict(), ensure_ascii=False)
    for banned in ("不會過敏", "低敏", "安全無虞", "品牌：", "色號："):
        assert banned not in text


def test_no_evidence_means_no_recommendation():
    invalid = {e["chunk_key"]: "document_inactive" for c in SPEC["candidates"] for e in c["evidence"]}
    d = _decide("上班用的自然唇色，不要霧面", invalid=invalid)
    assert d.status == "insufficient_evidence" and d.reason == "no_candidate_with_valid_evidence"
    assert {t["reasons"][0]["rule"] for t in d.trace} == {"G01"}


def test_every_cited_claim_comes_from_valid_evidence():
    invalid = {"SYN_S7_A_NUDE_SATIN_occasions_office": "chunk_not_approved"}
    d = _decide("上班用的自然唇色，不要霧面", invalid=invalid)
    cands = {c.option_key: c for c in rec_cases.candidates_from_spec(SPEC, invalid)}
    for r in d.recommendations:
        valid_ids = {e.chunk_id for e in cands[r["option_key"]].evidence if e.valid}
        for cl in r["claims"]:
            assert {c["chunk_id"] for c in cl["citations"]} <= valid_ids
    nude = next(r for r in d.recommendations if r["option_key"] == NUDE)
    assert "occasion" not in nude["matched"]   # 失效的「職場」證據不能再當理由


# ---------------------------------------------------------------------------
# 3. 候選檔驗證（不連資料庫）
# ---------------------------------------------------------------------------
def _real_candidate(**over):
    c = {"option_key": "nude-satin", "name": "n", "description": "d", "verified_by": "B", "verified_at": "2026-10-20",
         "attributes": {"color_family": "nude", "finishes": ["satin"], "effects": [], "occasions": [], "skin_tones": []},
         "limitations": [],
         "evidence": [{"doc_key": "X", "version_label": "v1", "chunk_key": "X_1", "support_type": "styling_advice",
                       "claim": "c", "supports": ["color_family:nude", "finishes:satin"]}]}
    c.update(over)
    return {"schema_version": "cand-v1", "status": "verified", "synthetic": False, "candidates": [c]}


def test_real_candidate_file_is_empty_and_awaiting_b_line():
    data = gov.read_spec(REAL_FILE)
    assert (data["status"], data["synthetic"], data["candidates"]) == ("awaiting_b_line", False, [])


def test_synthetic_file_is_valid_and_marked():
    assert gov.validate_spec(SPEC) == []
    assert SPEC["synthetic"] is True and all(c["option_key"].startswith("synthetic-") for c in SPEC["candidates"])


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: s["candidates"][0]["attributes"].update(effects=["natural"]), "沒有任何證據支持"),
    (lambda s: s["candidates"][0]["evidence"][0]["supports"].append("occasions:party"), "未宣告的屬性值"),
    (lambda s: s["candidates"][0]["attributes"].update(color_family="大地色"), "color_family"),
    (lambda s: s["candidates"][0].update(verified_by=""), "verified_by"),
    (lambda s: s["candidates"][0]["evidence"][0].update(support_type="personal_preference"), "support_type"),
    (lambda s: s["candidates"][0].update(option_key="synthetic-x"), "synthetic-"),
    (lambda s: s.update(status="awaiting_b_line"), "awaiting_b_line"),
    (lambda s: s.update(synthetic=True), "synthetic"),
    (lambda s: s["candidates"][0].update(evidence=[]), "至少要有一筆證據"),
])
def test_validation_rejects_unsupported_or_unmarked(mutate, needle):
    spec = _real_candidate()
    assert gov.validate_spec(spec) == []
    mutate(spec)
    errors = gov.validate_spec(spec)
    assert errors and any(needle in e for e in errors), errors


# ---------------------------------------------------------------------------
# 4. 資料庫：候選治理與證據資格
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(db, users):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


# 建立合成候選檔用到的 SYN_S7_A／SYN_S7_B 文件與片段；approve／publish 控制是否可用。
def _make_docs(conn, admin_id, *, approve=True, publish=True):
    keys: dict[str, list[str]] = {}
    for c in SPEC["candidates"]:
        for e in c["evidence"]:
            keys.setdefault(e["doc_key"], []).append(e["chunk_key"])
    for doc, chunk_keys in keys.items():
        doc_id = conn.execute(
            "INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible) "
            "VALUES (%s, %s, 'other', 'all', TRUE, TRUE) RETURNING id", (doc, f"SYNTHETIC {doc}")).fetchone()["id"]
        v = conn.execute("INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version) "
                         "VALUES (%s, 'v1', %s, 'test') RETURNING id", (doc_id, SHA)).fetchone()["id"]
        for i, k in enumerate(chunk_keys):
            text = f"SYNTHETIC evidence {k}"
            sha = hashlib.sha256(text.encode()).hexdigest()
            cid = conn.execute(
                "INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text, "
                "content_sha256) VALUES (%s, %s, %s, 1, %s, %s, %s) RETURNING id",
                (v, k, i, text, text, sha)).fetchone()["id"]
            if approve:
                conn.execute("SELECT review_chunk(%s, %s, 'approve', '合成測試核准', %s)", (cid, admin_id, sha))
        if publish:
            conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s", (v,))


# 載入並啟用全部合成候選。
def _activate_all(conn, admin_id):
    gov.load_spec(conn, SPEC, admin_id, allow_synthetic=True)
    for c in SPEC["candidates"]:
        gov.activate(conn, c["option_key"], admin_id, allow_synthetic=True)


def _run(conn, message, role="user", had_prior=False):
    state, parsed = _state(message)
    return rec.run(conn, state, role=role, intent=parsed.intent, secondary=parsed.secondary_intents,
                   had_prior=had_prior)


def test_load_requires_synthetic_flag_and_is_idempotent(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    with pytest.raises(gov.CandidateError, match="allow-synthetic"):
        gov.load_spec(conn, SPEC, admin)
    first = gov.load_spec(conn, SPEC, admin, allow_synthetic=True)
    assert {r["result"] for r in first} == {"created_draft"}
    again = gov.load_spec(conn, SPEC, admin, allow_synthetic=True)
    assert {r["result"] for r in again} == {"unchanged"}
    assert conn.execute("SELECT count(*) AS n FROM recommendation_options").fetchone()["n"] == 5
    # draft 不會被推薦
    assert _run(conn, "上班用的自然唇色，不要霧面").status == "insufficient_evidence"


def test_load_rejects_missing_chunk_and_non_rag_document(conn, users):
    admin = users["root"]
    with pytest.raises(gov.CandidateError, match="找不到片段"):
        gov.load_spec(conn, SPEC, admin, allow_synthetic=True)
    _make_docs(conn, admin)
    conn.execute("UPDATE documents SET rag_eligible = FALSE, rag_exclusion_reason = '研究定稿' WHERE doc_key = 'SYN_S7_B'")
    with pytest.raises(gov.CandidateError, match="不可進 RAG"):
        gov.load_spec(conn, SPEC, admin, allow_synthetic=True)
    assert conn.execute("SELECT count(*) AS n FROM recommendation_options").fetchone()["n"] == 0  # 整批未生效


def test_activation_requires_all_evidence_usable(conn, users):
    admin = users["root"]
    _make_docs(conn, admin, approve=False)
    gov.load_spec(conn, SPEC, admin, allow_synthetic=True)
    with pytest.raises(gov.CandidateError, match="chunk_not_approved"):
        gov.activate(conn, NUDE, admin, allow_synthetic=True)
    with pytest.raises(gov.CandidateError, match="allow-synthetic"):
        gov.activate(conn, NUDE, admin)


def test_active_candidate_cannot_be_overwritten(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    changed = copy.deepcopy(SPEC)
    changed["candidates"][0]["name"] = "改名"
    with pytest.raises(gov.CandidateError, match="不可覆寫"):
        gov.load_spec(conn, changed, admin, allow_synthetic=True)
    with pytest.raises(gov.CandidateError, match="只有 draft"):
        gov.activate(conn, NUDE, admin, allow_synthetic=True)


def test_db_hard_exclusion_and_ranking(conn, users):
    _make_docs(conn, users["root"])
    _activate_all(conn, users["root"])
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert d.status == "recommended" and _keys(d) == [NUDE, ROSE] and d.synthetic
    assert {t["option_key"] for t in d.trace if t["decision"] == "excluded"} == {BEAN, CORAL}


def test_source_deactivation_blocks_immediately(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    assert _keys(_run(conn, "上班用的自然唇色，不要霧面"))[0] == NUDE
    conn.execute("UPDATE documents SET is_active = FALSE WHERE doc_key = 'SYN_S7_B'")   # 裸色的色系證據
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert NUDE not in _keys(d) and _keys(d)[0] == ROSE
    t = _trace(d, NUDE)
    assert t["reasons"][0]["code"] == "color_family_unverified"
    assert t["invalid_evidence"][0]["reason"] == "document_inactive"
    status = {s["option_key"]: s for s in gov.governance_status(conn)}[NUDE]
    assert status["usable_for_recommendation"] is False and status["degraded"] is True


@pytest.mark.parametrize("change,reason", [
    ("UPDATE chunks SET review_status = 'rejected' WHERE chunk_key = %s", "chunk_not_approved"),
    ("UPDATE chunks SET is_active = FALSE WHERE chunk_key = %s", "chunk_inactive"),
])
def test_chunk_level_invalidation_degrades_attribute(conn, users, change, reason):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    key = "SYN_S7_A_NUDE_SATIN_occasions_office"
    if "rejected" in change:   # 退回要走審核函式（有審核歷程），不能直接改欄位
        row = conn.execute("SELECT id, content_sha256 FROM chunks WHERE chunk_key = %s", (key,)).fetchone()
        conn.execute("SELECT review_chunk(%s, %s, 'reject', '合成測試退回', %s)", (row["id"], admin, row["content_sha256"]))
    else:
        conn.execute(change, (key,))
    d = _run(conn, "上班用的自然唇色，不要霧面")
    nude = next(r for r in d.recommendations if r["option_key"] == NUDE)
    assert "occasion" not in nude["matched"] and nude["tied_with"] == [ROSE]
    assert {e["reason"] for e in _trace(d, NUDE)["invalid_evidence"]} == {reason}


def test_expired_document_and_unpublished_version(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    conn.execute("UPDATE documents SET valid_until = now() - interval '1 day' WHERE doc_key = 'SYN_S7_B'")
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert _trace(d, NUDE)["invalid_evidence"][0]["reason"] == "outside_valid_period"
    conn.execute("UPDATE documents SET valid_until = NULL WHERE doc_key = 'SYN_S7_B'")
    conn.execute("UPDATE document_versions SET status = 'retired', retired_at = now() WHERE document_id = "
                 "(SELECT id FROM documents WHERE doc_key = 'SYN_S7_B')")
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert _trace(d, NUDE)["invalid_evidence"][0]["reason"] == "version_not_published"


def test_role_visibility_applies_to_evidence(conn, users):
    _make_docs(conn, users["root"])
    _activate_all(conn, users["root"])
    conn.execute("UPDATE documents SET visibility = 'admin' WHERE doc_key = 'SYN_S7_B'")
    user_d = _run(conn, "上班用的自然唇色，不要霧面", role="user")
    admin_d = _run(conn, "上班用的自然唇色，不要霧面", role="admin")
    assert NUDE not in _keys(user_d)
    assert _trace(user_d, NUDE)["invalid_evidence"][0]["reason"] == "role_not_permitted"
    assert _keys(admin_d)[0] == NUDE
    with pytest.raises(rec.RecommendationError):
        _run(conn, "推薦唇色", role="superuser")


def test_retired_candidate_not_used(conn, users):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    gov.retire(conn, NUDE, admin)
    assert NUDE not in [t["option_key"] for t in _run(conn, "上班用的自然唇色，不要霧面").trace]


def test_evidence_changed_before_return_is_recomputed(conn, users, monkeypatch):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    real_load, calls = rec.load_candidates, []

    # 第一次載入後立刻停用裸色的色系證據，模擬「判斷到回傳之間」來源被下架。
    def racing_load(c, role, statuses=("active",)):
        out = real_load(c, role, statuses)
        calls.append(1)
        if len(calls) == 1:
            conn.execute("UPDATE documents SET is_active = FALSE WHERE doc_key = 'SYN_S7_B'")
        return out

    monkeypatch.setattr(rec, "load_candidates", racing_load)
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert d.rechecks == 1 and NUDE not in _keys(d) and len(calls) == 2


def test_evidence_keeps_changing_raises(conn, users, monkeypatch):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    # 每次回傳前檢查都判定引用失效：重算永遠不穩定 → 不回傳推薦（而不是回傳舊依據）
    monkeypatch.setattr(rec, "stale_citations", lambda c, ids, role: list(ids))
    with pytest.raises(rec.RecommendationError):
        _run(conn, "上班用的自然唇色，不要霧面")


def test_all_evidence_removed_mid_request_gives_no_recommendation(conn, users, monkeypatch):
    admin = users["root"]
    _make_docs(conn, admin)
    _activate_all(conn, admin)
    real_load, calls = rec.load_candidates, []

    # 第一次載入後把兩份文件都下架：重算後沒有任何有效證據 → insufficient_evidence，不沿用第一次的結果。
    def racing_load(c, role, statuses=("active",)):
        out = real_load(c, role, statuses)
        calls.append(1)
        if len(calls) == 1:
            conn.execute("UPDATE documents SET is_active = FALSE")
        return out

    monkeypatch.setattr(rec, "load_candidates", racing_load)
    d = _run(conn, "上班用的自然唇色，不要霧面")
    assert (d.status, d.rechecks, d.recommendations) == ("insufficient_evidence", 1, [])


# ---------------------------------------------------------------------------
# 5. API：推薦結果、紀錄、偏好變更後重算
# ---------------------------------------------------------------------------
def _say(client, h, cid, text):
    r = client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_api_recommend_then_change_preference(client, db, users):
    """route-v4：正式路由不出推薦卡；規則推薦本身（離線對照用）以當時條件計算，修改條件後排序跟著變、排除條件保留。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        _make_docs(c, users["root"])
        _activate_all(c, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    first = _say(client, h, cid, "上班用的自然唇色，不要霧面")
    assert first["recommendation"] is None
    d1 = rule_card_for_request(db, first["request_id"])
    assert d1.status == "recommended" and [r["option_key"] for r in d1.recommendations] == [NUDE, ROSE]
    assert rec.render_text(d1).startswith("已排除你不要的：霧面")

    second = _say(client, h, cid, "改成聚會")
    assert second["recommendation"] is None
    d2 = rule_card_for_request(db, second["request_id"], had_prior=True)
    assert [r["option_key"] for r in d2.recommendations] == [ROSE, NUDE, RED]
    assert [h_["label"] for h_ in d2.hard_conditions] == ["霧面"]                     # 不要霧面保留
    with psycopg.connect(db, row_factory=dict_row) as c:
        row = c.execute("SELECT * FROM requests WHERE id = %s", (second["request_id"],)).fetchone()
        n = c.execute("SELECT count(*) AS n FROM request_recommendations").fetchone()["n"]
    assert row["detail"]["route"]["reason"] == "update_after_recommend" and "recommendation" not in row["detail"]
    assert n == 0                                                                     # 正式路由不寫推薦名次
    assert {t["option_key"] for t in d2.trace} == {c["option_key"] for c in SPEC["candidates"]}


def test_api_knowledge_question_has_no_recommendation(client, db, users):
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    body = _say(client, h, cid, "豆沙色是什麼意思")
    # S5 起知識問答走問答流程；此測試沒有任何核准片段 → 缺資料（原本是 not_answered）
    assert body["status"] == "answer_insufficient_evidence" and body["recommendation"] is None
    with psycopg.connect(db, row_factory=dict_row) as c:
        row = c.execute("SELECT detail FROM requests WHERE id = %s", (body["request_id"],)).fetchone()
    assert "recommendation" not in row["detail"]


def test_api_clarification_defers_then_update_recommends(client, db, users):
    """真衝突先澄清；回答後接回推薦需求重新問答（route-v4），規則推薦依回答排除豆沙霧面。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        _make_docs(c, users["root"])
        _activate_all(c, users["root"])
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    first = _say(client, h, cid, "推薦霧面，不要霧面的唇膏")
    assert first["status"] == "needs_clarification" and first["recommendation"] is None
    second = _say(client, h, cid, "以不要霧面為準")
    assert second["recommendation"] is None
    d2 = rule_card_for_request(db, second["request_id"], had_prior=True)
    assert d2.status == "recommended" and all(r["option_key"] != BEAN for r in d2.recommendations)


def test_api_evidence_instability_is_logged_as_failure(client, db, users, monkeypatch):
    """規則推薦的證據一直變動 → rec.run 放棄並報錯（離線對照會記成失敗）；正式路由不跑規則推薦，不受影響。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        _make_docs(c, users["root"])
        _activate_all(c, users["root"])

    # 讓 run() 每次都判定引用失效，模擬持續變動的來源。
    monkeypatch.setattr(rec, "stale_citations", lambda conn, ids, role: list(ids))
    h = login(client, "alice")
    cid = client.post("/api/conversations", headers=h, json={}).json()["id"]
    body = _say(client, h, cid, "上班用的自然唇色，不要霧面")
    assert body["status"] != "recommendation_error" and body["recommendation"] is None
    with pytest.raises(rec.RecommendationError):
        rule_card_for_request(db, body["request_id"])
