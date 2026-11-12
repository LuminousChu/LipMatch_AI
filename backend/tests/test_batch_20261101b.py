"""2026-11-01 第二組決定（route-v4 之後）：

1. T4 斷線（連不上／逾時／伺服器錯誤）：不呼叫任何其他模型，改列本次會送進模型的已核准原文段落（sources_only），
   和一般回答分開、記錯誤代碼；不附色名參照。
2. DEC-11 E2B 備援停用：要 GENERATION_FALLBACK=on 才會包成 FallbackGenerator（預設關）。
3. 衝突：只有同一句自相矛盾先澄清；和先前條件相反的衝突走問答，補問附在回答後。
4. 攔截出口（商品 G01、G03）不附色名參照（route-v3 已做，這裡補 G01）。
句子是 40 題原句或標 SYNTHETIC 的開發句。
"""
from __future__ import annotations

from app import main as M
from app import preferences as pref
from app import qa

from .test_qa import (MockEmbedder, PUBLIC_KEY, QUESTION, REPAIR_NONE, _DownGenerator, _mock_modules,  # noqa: F401
                      conn, corpus)

P = pref.RuleParser()


# ---------------------------------------------------------------------------
# 1. sources_only
# ---------------------------------------------------------------------------
def test_model_unreachable_lists_approved_source_text_without_backup(conn, corpus):
    gen = _DownGenerator("model_unreachable")
    res = qa.answer_question(conn, QUESTION, "user", REPAIR_NONE, embedder_factory=MockEmbedder, generator=gen,
                             pipeline_modules=_mock_modules({}))
    assert res.outcome == "sources_only" and res.error_code == "model_unreachable" and gen.calls >= 1
    assert res.answer.startswith(qa.SOURCES_ONLY_HEAD) and res.citations
    assert all(c["quote_text"] in res.answer for c in res.citations) and "出處：" in res.answer
    assert "generation_fallback" not in res.log and res.log["sources_only"]["units"]


def test_other_model_errors_still_fail(conn, corpus):
    res = qa.answer_question(conn, QUESTION, "user", REPAIR_NONE, embedder_factory=MockEmbedder,
                             generator=_DownGenerator("model_request_rejected"), pipeline_modules=_mock_modules({}))
    assert res.outcome == "failed" and res.error_code == "model_request_rejected"


def test_sources_only_outcome_is_separate_from_answered():
    res = qa.QaResult("sources_only", "model_timeout", answer="x")
    parsed = pref.ParseResult(intent="knowledge")
    assert M._message_outcome(parsed, [], None, None, res) == (
        "sources_only", "failed", "model_timeout", M._OUTCOME_MESSAGES["sources_only"])


# ---------------------------------------------------------------------------
# 2. E2B 備援預設關
# ---------------------------------------------------------------------------
def test_fallback_disabled_unless_switched_on(monkeypatch):
    base = dict(provider="llamacpp", model="gemma-12b", base_url="https://x.example/v1",
                fallback_base_url="http://127.0.0.1:8098/v1", fallback_model="gemma-e2b")
    assert isinstance(qa.get_generator(qa.QaConfig(**base)), qa.OpenAICompatibleGenerator)
    assert isinstance(qa.get_generator(qa.QaConfig(**base, fallback_enabled=True)), qa.FallbackGenerator)
    monkeypatch.setenv("GENERATION_FALLBACK_BASE_URL", "http://127.0.0.1:8098/v1")
    assert qa.QaConfig.from_env().fallback_enabled is False


# ---------------------------------------------------------------------------
# 3. 衝突只擋同一句自相矛盾
# ---------------------------------------------------------------------------
def test_only_in_message_conflict_blocks():
    q = [{"field": "finishes", "key": "matte", "blocking": True, "question": "?"}]
    parsed = P.parse("推薦霧面的唇膏")                                                    # SYNTHETIC
    against = [{"field": "finishes", "key": "matte", "kind": "against_existing"}]
    inmsg = [{"field": "finishes", "key": "matte", "kind": "in_message"}]
    assert M._route(parsed, q, hold=False, conflicts=against)["reason"] == "starting_point_before_clarification"
    assert M._route(parsed, q, hold=False, conflicts=inmsg)["reason"] == "conflict_first"
