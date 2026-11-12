"""最低向量相似度門檻（2026-11-01 決定 0.26；依 data/review/minsim-20261101/minsim_report.md）。

1. cosine 低於門檻、沒有關鍵詞路命中 → 不進後續（選段、裝箱、模型），並記進日誌（段落、cosine）。
2. 關鍵詞路有命中 → 不論 cosine 多低都保留。
3. 40 題正解最低的 W07 PR017_h0120#r3（cosine 0.286）不會被擋。
4. 設定：QA_MIN_VECTOR_COSINE 預設 0.26，off＝不擋；直接建 QaConfig 時預設不擋。
"""
from __future__ import annotations

from app import qa

from .test_qa import MockEmbedder, QUESTION, REPAIR_NONE, FakeGenerator, _mock_modules, conn, corpus  # noqa: F401

C = lambda uid: {"unit_id": uid}   # noqa: E731


def test_low_cosine_without_keyword_hit_is_dropped_and_logged():
    kept, blocked = qa.min_cosine_filter([C("a"), C("b")], {"a": 0.10, "b": 0.50}, set(), 0.26)
    assert [c["unit_id"] for c in kept] == ["b"] and blocked == [{"unit_id": "a", "cosine": 0.1}]


def test_keyword_hit_is_kept_even_with_low_cosine():
    kept, blocked = qa.min_cosine_filter([C("a")], {"a": 0.05}, {"a"}, 0.26)
    assert kept == [C("a")] and blocked == []


def test_lowest_gold_w07_is_not_blocked():
    kept, _ = qa.min_cosine_filter([C("PR017_h0120#r3")], {"PR017_h0120#r3": 0.286}, set(), qa.DEFAULT_MIN_VECTOR_COSINE)
    assert kept and qa.DEFAULT_MIN_VECTOR_COSINE == 0.26


def test_config_default_and_off(monkeypatch):
    assert qa.QaConfig().min_vector_cosine is None
    assert qa.QaConfig.from_env().min_vector_cosine == 0.26
    monkeypatch.setenv("QA_MIN_VECTOR_COSINE", "off")
    assert qa.QaConfig.from_env().min_vector_cosine is None


def test_pipeline_logs_blocked_units_and_does_not_send_them(conn, corpus):
    import dataclasses
    cfg = dataclasses.replace(REPAIR_NONE, min_vector_cosine=1.01)          # 全部低於門檻（測試用的極端值）
    gen = FakeGenerator()
    res = qa.answer_question(conn, QUESTION, "user", cfg, embedder_factory=MockEmbedder, generator=gen,
                             pipeline_modules=_mock_modules({}))
    blocked = res.log["min_vector_cosine"]["blocked"]
    assert blocked and all("cosine" in b for b in blocked) and gen.calls == []
