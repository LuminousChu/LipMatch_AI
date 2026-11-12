"""關係選段（selector=relation）：純函式與經 API 的接線。SYNTHETIC 關係包與合成片段（不是來源原文或驗收）；
B／C 模組沿用 test_qa 的 MOCK 替身，只驗 D 的接線、條件、核准綁定與退回既有流程。"""
from __future__ import annotations

import hashlib
import json

import psycopg
import pytest
from psycopg.rows import dict_row

from app import qa, relation_select as RS
from tests.test_qa import PUBLIC_KEY, QUESTION, FakeGenerator, _ask, _claims, _mock_modules
from tests.test_qa import conn, corpus, make_client  # noqa: F401  （沿用 test_qa 的資料庫 fixture）

SHA_Q = hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()


def _pack(sha=SHA_Q, tree=None, pattern="明度"):
    """SYNTHETIC 關係包：一個需求概念、一筆紀錄綁定 PUBLIC 片段原文。"""
    return {"version": "relation-pack-v1",
            "need_cues": [{"concept": "syn_need", "pattern": pattern, "require": None}],
            "accept": {"syn_need": {"syn_need": {"basis": "explicit"}}},
            "targets_ok": {"syn_need": ["lip_product"]}, "conflict": {},
            "profile_cues": [{"dim": "undertone", "value": "cool", "pattern": "冷底"},
                             {"dim": "undertone", "value": "warm", "pattern": "暖底"},
                             {"dim": "eye_makeup", "value": "smoky_heavy", "pattern": "濃的煙燻"}],
            "satisfies": [{"dim": "eye_makeup", "have": "smoky_heavy", "satisfies": ["heavy", "smoky"]}],
            "kind_order": ["operation", "explanation"], "basis_order": {"explicit": 0, "derived": 1},
            "records": [{"recall_id": "SYN-1", "unit_id": PUBLIC_KEY, "relation_id": None, "quote": "明度",
                         "targets": ["lip_product"], "addresses": [{"concept": "syn_need", "basis": "explicit", "quote": "明度"}],
                         "method_kind": "operation", "evidence_level": "SYNTHETIC", "product_specific": False,
                         "applies_when": tree or {"all": []}, "not_supported": [], "unit_text_sha256": sha,
                         "approval": {"decision_id": "SYN-APPROVAL", "ranges": [[0, 9]], "scope": "whole_unit"}}]}


def _cand(text=QUESTION):
    return [{"unit_id": PUBLIC_KEY, "text": text, "pool_rank": 1}]


# ---------------------------------------------------------------------------
# 純函式
# ---------------------------------------------------------------------------
def test_select_matches_bound_text_and_marks_condition():
    r = RS.select(QUESTION, _cand(), _pack())
    assert r["kept"] == [PUBLIC_KEY] and r["rows"][0]["condition"] == "原文未附條件" and r["status"] == "selected"


def test_select_rejects_text_not_bound_to_pack():
    r = RS.select(QUESTION, _cand(QUESTION + "改"), _pack())
    assert r["kept"] == [] and "原文" in r["rows"][0]["reason"]


def test_no_need_concept_selects_nothing():
    r = RS.select("完全無關的問題", _cand(), _pack())
    assert r["kept"] == [] and r["status"] == "no_need_concept"


def test_condition_tree_conflict_unknown_and_any():
    cool = {"all": [{"dim": "undertone", "eq": "cool", "quote": "q"}]}
    assert RS.select(QUESTION + "我是暖底", _cand(), _pack(tree=cool))["kept"] == []           # 衝突排除
    r = RS.select(QUESTION, _cand(), _pack(tree=cool))
    assert r["kept"] == [PUBLIC_KEY] and r["rows"][0]["condition"] == "未知"                  # 未知保留並標示
    anyt = {"any": [{"dim": "undertone", "eq": "warm", "quote": "q"}, {"dim": "eye_makeup", "eq": "heavy", "quote": "q"}]}
    r = RS.select(QUESTION + "我是冷底，眼妝想畫比較濃的煙燻", _cand(), _pack(tree=anyt))
    assert r["kept"] == [PUBLIC_KEY] and r["rows"][0]["condition"] == "符合"                  # 整棵 any：一葉衝突、一葉滿足


def test_condition_notes_add_missing_dims_with_raw_words():
    prof = RS.profile("眼妝想畫比較濃的煙燻，唇色要怎麼配", _pack())
    notes = RS.condition_notes(prof, "眼妝想畫比較濃的煙燻，唇色要怎麼配", [])
    assert notes == [{"field": "notes", "value": "眼妝：濃煙燻", "values": None, "label": None,
                      "raw": "眼妝想畫比較濃的煙燻", "source": RS.VERSION}]
    have = [{"field": "undertone", "value": "cool", "raw": "我是冷底"}]
    assert RS.condition_notes(RS.profile("我是冷底", _pack()), "我是冷底", have) == []        # 已有同欄同值不重複


def test_load_pack_checks_sha_and_version(tmp_path):
    p = tmp_path / "pack.json"
    p.write_text(json.dumps(_pack()), encoding="utf-8")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    assert RS.load_pack(p, sha)["_sha256"] == sha
    with pytest.raises(RS.PackError):
        RS.load_pack(p, "0" * 64)
    with pytest.raises(RS.PackError):
        RS.load_pack(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# 經 API：問題 → 檢索 → 關係選段 → 裝箱 → 回答（MOCK 模組）→ 日誌
# ---------------------------------------------------------------------------
def _config(tmp_path, pack):
    p = tmp_path / "pack.json"
    p.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    return qa.QaConfig(retry_backoff_s=0.0, timeout_s=1.0, max_retries=1, pipeline="repair", k_pool=5,
                       selector="relation", relation_pack=str(p),
                       relation_pack_sha256=hashlib.sha256(p.read_bytes()).hexdigest())


def _log(db, rid):
    with psycopg.connect(db, row_factory=dict_row) as c:
        return c.execute("SELECT detail FROM requests WHERE id = %s", (rid,)).fetchone()["detail"]["qa"]


def test_relation_selector_through_api_without_selection_model(make_client, db, corpus, monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(qa, "load_pipeline_modules", lambda: _mock_modules(seen))
    gen = FakeGenerator(_claims(PUBLIC_KEY))
    body = _ask(make_client(gen, config=_config(tmp_path, _pack())))
    assert body["status"] == "answered" and body["answer"] == "SYNTHETIC 主張〔來源：〈SYNTHETIC PUBLIC〉，p.4〕"
    assert len(gen.calls) == 1                                                   # 沒有選段模型，只有回答一次
    log = _log(db, body["request_id"])
    assert log["selection"]["selector"] == "relation" and log["selection"]["kept_unit_ids"] == [PUBLIC_KEY]
    assert log["relation_selection"]["status"] == "selected" and log["relation_selection"]["rows"][0]["approval"] == "SYN-APPROVAL"
    assert [c["stage"] for c in log["model_calls"]] == ["query_embedding", "generation"]
    assert [t["unit_id"] for t in log["unit_trace"]["answer_input"]] == [PUBLIC_KEY]


def test_relation_selector_falls_back_to_none_when_nothing_applies(make_client, db, corpus, monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(qa, "load_pipeline_modules", lambda: _mock_modules(seen))
    gen = FakeGenerator(_claims(PUBLIC_KEY))
    body = _ask(make_client(gen, config=_config(tmp_path, _pack(sha="0" * 64))))   # 綁定原文不符 → 不選
    log = _log(db, body["request_id"])
    assert log["relation_selection"]["kept"] == [] and log["selection"]["selector"] == "none"
    assert "relation_no_selection_fallback_none" in log["selection"]["flags"]


def test_relation_selector_without_pack_is_not_configured(conn, corpus):
    cfg = qa.QaConfig(retry_backoff_s=0.0, timeout_s=1.0, pipeline="repair", k_pool=5, selector="relation",
                      relation_pack="F:/no/such/pack.json")
    from tests.test_qa import MockEmbedder
    res = qa.answer_question(conn, QUESTION, "user", cfg, embedder_factory=MockEmbedder,
                             generator=FakeGenerator(_claims(PUBLIC_KEY)), explicit_conditions=[],
                             pipeline_modules=_mock_modules({}))
    assert (res.outcome, res.error_code) == ("failed", "pipeline_not_configured")
    assert "relation_pack" in res.log["retrieval"]["error"]


# ---------------------------------------------------------------------------
# route-v4（2026-11-01）：正式路由不跑規則推薦，推薦需求直接走問答；原本「規則推薦 insufficient_evidence →
# 只用關係證據補位」的特別路徑已移除，推薦需求和一般問題一樣交給問答的選段器（selector=relation 時用關係證據）。
# ---------------------------------------------------------------------------
REC_Q = "我是冷底，想擦紅色，要選哪一種紅比較適合？"     # 規則解析器判為 recommend


def _rec_pack(pattern="冷底", tree=None):
    return {**_pack(tree=tree, pattern=pattern)}


def _detail(db, rid):
    with psycopg.connect(db, row_factory=dict_row) as c:
        return c.execute("SELECT detail FROM requests WHERE id = %s", (rid,)).fetchone()["detail"]


def test_recommend_need_goes_to_qa_with_relation_selector(make_client, db, corpus, monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(qa, "load_pipeline_modules", lambda: _mock_modules(seen))
    gen = FakeGenerator(_claims(PUBLIC_KEY))
    cool = {"all": [{"dim": "undertone", "eq": "cool", "quote": "q"}]}
    body = _ask(make_client(gen, config=_config(tmp_path, _rec_pack(tree=cool))), question=REC_Q)
    assert body["intent"] == "recommend" and body["status"] == "answered"
    assert body["answer"] == "SYNTHETIC 主張〔來源：〈SYNTHETIC PUBLIC〉，p.4〕" and len(gen.calls) == 1
    d = _detail(db, body["request_id"])
    assert "recommendation" not in d and "recommendation_relation_fallback" not in d
    assert d["route"]["reason"] == "default_rag" and d["answer_source"] == "generation_model"
    assert d["qa"]["selection"]["selector"] == "relation" and d["qa"]["relation_selection"]["rows"][0]["condition"] == "符合"


def test_recommend_need_without_relation_selector_goes_to_qa(make_client, db, corpus, monkeypatch):
    from tests.test_qa import REPAIR_NONE
    monkeypatch.setattr(qa, "load_pipeline_modules", lambda: _mock_modules({}))
    gen = FakeGenerator(_claims(PUBLIC_KEY))
    body = _ask(make_client(gen, config=REPAIR_NONE), question=REC_Q)
    d = _detail(db, body["request_id"])
    assert d["route"]["reason"] == "default_rag" and "recommendation" not in d
    assert "recommendation_relation_fallback" not in d and "qa" in d
