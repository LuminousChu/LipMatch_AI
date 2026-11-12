"""修復 B：候選取得／重排／裝箱分離（D16）。

不連資料庫、不呼叫模型。名次與 token 取自保存的 gap_locate.json（G1 第二十五批）：
H06 候選池只有 5 段（750 token），第 6 名 seg1:138 有 1,639 token → 舊規則停止，第 29 名的 seg1:338 看不到；
H04 的持有片段 seg1:1299（第 15 名、334 token）本身就是停止片段。片段原文以 SYNTHETIC 文字代替。
MOCK：MockEmbedder／ListStore／MockReranker 沒有語意，只驗流程。
"""
from __future__ import annotations

import pytest

from dataclasses import dataclass

from app import retrieval_policy as rp
from app.retrieval_policy import Candidate, PolicyError


def _ranked(tokens: list[int], ids: list[str] | None = None) -> list[Candidate]:
    ids = ids or [f"u{i}" for i in range(1, len(tokens) + 1)]
    return [Candidate(unit_id=u, text=f"SYNTHETIC {u}", pool_rank=i, tokens=t)
            for i, (u, t) in enumerate(zip(ids, tokens), 1)]


def _h06_ranked() -> list[Candidate]:
    """H06：前 5 名合計 750 token，第 6 名 seg1:138 1,639 token，第 29 名 seg1:338（77 token）。"""
    tokens = [150] * 5 + [1639] + [100] * 22 + [77] + [100] * 5
    ids = [f"r{i}" for i in range(1, 35)]
    ids[5], ids[28] = "seg1:138", "seg1:338"
    return _ranked(tokens, ids)


class MockEmbedder:
    """MOCK：固定回傳一個向量，沒有語意。"""
    model, dim = "MOCK", 3

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


@dataclass(frozen=True)
class FakeHit:
    rank: int
    chunk_id: int
    chunk_key: str
    score: float


class ListStore:
    """MOCK：仿 D 的 retrieval.CandidateStore（search／units）；依預先排好的順序回傳前 k 列並記錄 k。

    rows：{"unit_id", "text", "score"?, "tokens"?（放進 support_unit.tokens_cl100k）, "meta"?（放進 support_unit）}。"""

    def __init__(self, rows):
        self.rows, self.asked, self.embed_calls = rows, [], 0

    def search(self, query, k, embedder):
        self.asked.append(k)
        embedder.embed([query])
        return [FakeHit(rank=i, chunk_id=i, chunk_key=r["unit_id"], score=r.get("score", 0.0))
                for i, r in enumerate(self.rows[:k], 1)]

    def units(self, hits):
        out = []
        for h in hits:
            r = self.rows[h.chunk_id - 1]
            su = dict(r.get("meta") or {})
            if r.get("tokens") is not None:
                su["tokens_cl100k"] = r["tokens"]
            out.append({"chunk_id": h.chunk_id, "chunk_key": r["unit_id"], "quote_text": r["text"],
                        "doc_key": "SYN", "version_label": "v-syn", "support_unit": su})
        return out


class MockReranker:
    model = "MOCK-rerank"

    def __init__(self, scores: dict[str, float]):
        self.scores = scores

    def score(self, query, texts):
        return [self.scores[t] for t in texts]


# ---------------------------------------------------------------------------
# D16：修前失敗重現 → 修後通過
# ---------------------------------------------------------------------------
def _pool_new(ranked: list[Candidate]) -> list[Candidate]:
    rows = [{"unit_id": c.unit_id, "text": c.text, "tokens": c.tokens} for c in ranked]
    return rp.retrieve_candidates("q", k_pool=30, embedder=MockEmbedder(), store=ListStore(rows))


def _pool_legacy(ranked: list[Candidate]) -> list[Candidate]:
    return rp.legacy_stop_pool(ranked[:30])[0]


@pytest.mark.parametrize("pool_fn", [
    pytest.param(_pool_legacy, id="legacy_stop_rule", marks=pytest.mark.xfail(strict=True, reason="D16 修前失敗：遇長段停止")),
    pytest.param(_pool_new, id="retrieval_policy"),
])
def test_long_unit_does_not_stop_later_candidates(pool_fn):
    pool = pool_fn(_h06_ranked())
    ids = [c.unit_id for c in pool]
    assert "seg1:138" in ids          # 長段本身也是候選
    assert "seg1:338" in ids          # 第 29 名仍進候選池
    assert len(pool) == 30


def test_legacy_stop_rule_reproduces_saved_h06_pool():
    pool, stop = rp.legacy_stop_pool(_h06_ranked())
    assert [c.pool_rank for c in pool] == [1, 2, 3, 4, 5]
    assert sum(c.tokens for c in pool) == 750
    assert stop.unit_id == "seg1:138" and stop.pool_rank == 6


def test_h04_stop_unit_itself_enters_pool():
    # H04：前 14 名 1,870 token，seg1:1299 第 15 名 334 token（只剩 178）
    tokens = [135] * 13 + [115] + [334] + [80] * 5
    ids = [f"r{i}" for i in range(1, 21)]
    ids[14] = "seg1:1299"
    ranked = _ranked(tokens, ids)
    assert sum(tokens[:14]) == 1870
    pool, stop = rp.legacy_stop_pool(ranked)
    assert stop.unit_id == "seg1:1299" and "seg1:1299" not in [c.unit_id for c in pool]
    assert "seg1:1299" in [c.unit_id for c in _pool_new(ranked)]


# ---------------------------------------------------------------------------
# 候選取得
# ---------------------------------------------------------------------------
def test_retrieve_candidates_embeds_once_and_keeps_order():
    emb = MockEmbedder()
    store = ListStore([{"unit_id": "a", "text": "SYNTHETIC a", "score": 0.9, "tokens": 5000,
                        "meta": {"conditions": ["cool skin"], "content_role": "選色"}},
                       {"unit_id": "b", "text": "SYNTHETIC b", "score": 0.8},
                       {"unit_id": "a", "text": "SYNTHETIC a dup"},
                       {"unit_id": "c", "text": "SYNTHETIC c", "score": 0.7}])
    out = rp.retrieve_candidates("q", k_pool=4, embedder=emb, store=store)
    assert emb.calls == 1 and store.asked == [4]
    assert [(c.unit_id, c.pool_rank) for c in out] == [("a", 1), ("b", 2), ("c", 3)]
    assert out[0].tokens == 5000 and out[0].tokens_source == "cl100k"     # 超過 2048 也照收
    assert out[0].meta["conditions"] == ["cool skin"] and out[0].meta["content_role"] == "選色"
    assert out[0].text == "SYNTHETIC a" and out[0].vector_score == 0.9    # text＝quote_text 原文
    assert out[1].tokens == rp.approx_tokens("SYNTHETIC b") and out[1].tokens_source == "approx"


def test_store_missing_unit_text_is_error():
    class Lossy(ListStore):
        def units(self, hits):
            return super().units(hits)[:-1]
    with pytest.raises(PolicyError, match="沒有交回"):
        rp.retrieve_candidates("q", k_pool=2, embedder=MockEmbedder(),
                               store=Lossy([{"unit_id": "a", "text": "A"}, {"unit_id": "b", "text": "B"}]))


@pytest.mark.parametrize("kwargs, msg", [({"k_pool": 0}, "k_pool"), ({"k_pool": 3, "query": " "}, "查詢")])
def test_retrieve_candidates_rejects_bad_input(kwargs, msg):
    q = kwargs.pop("query", "q")
    with pytest.raises(PolicyError, match=msg):
        rp.retrieve_candidates(q, embedder=MockEmbedder(), store=ListStore([]), **kwargs)


def test_row_without_text_is_rejected():
    with pytest.raises(PolicyError, match="沒有原文"):
        rp.candidates_from_rows([{"unit_id": "x", "text": "  "}], k_pool=3)


# ---------------------------------------------------------------------------
# 重排
# ---------------------------------------------------------------------------
def test_rerank_keeps_pool_rank_and_breaks_ties_by_pool_rank():
    ranked = _ranked([10, 10, 10, 10])
    rr = rp.rerank("q", ranked, reranker=MockReranker({"SYNTHETIC u1": -1.0, "SYNTHETIC u2": 2.0,
                                                        "SYNTHETIC u3": 0.5, "SYNTHETIC u4": 0.5}))
    assert [(c.unit_id, c.pool_rank, c.rerank_rank) for c in rr] == [
        ("u2", 2, 1), ("u3", 3, 2), ("u4", 4, 3), ("u1", 1, 4)]
    assert rr[0].rerank_score == 2.0


def test_saved_scores_reuse_same_order_rule():
    # N01 B 池保存分數（rerank_result.json）前 4：345 0.2652、334 -3.2892、352 -0.8907、330 -1.7585
    ranked = _ranked([76, 62, 72, 82], ["seg1:345", "seg1:334", "seg1:352", "seg1:330"])
    rr = rp.apply_rerank_scores(ranked, [0.2652, -3.2892, -0.8907, -1.7585])
    assert [c.unit_id for c in rr] == ["seg1:345", "seg1:352", "seg1:330", "seg1:334"]


def test_rerank_count_mismatch():
    class Bad:
        model = "MOCK"

        def score(self, q, texts):
            return [0.0]
    with pytest.raises(PolicyError):
        rp.rerank("q", _ranked([1, 1]), reranker=Bad())


# ---------------------------------------------------------------------------
# 裝箱
# ---------------------------------------------------------------------------
def test_pack_skips_long_and_continues_without_truncation():
    sel = _ranked([600, 1639, 900, 400, 300])
    plan = rp.plan_pack(sel, max_tokens=2048, max_units=7)
    assert [c.unit_id for c in plan.packed] == ["u1", "u3", "u4"]
    assert plan.used_tokens == 1900
    assert [(s["unit_id"], s["reason"]) for s in plan.skipped] == [
        ("u2", "exceeds_remaining_budget"), ("u5", "exceeds_remaining_budget")]
    assert all(c.text == f"SYNTHETIC {c.unit_id}" for c in plan.packed)   # 原文不截斷


def test_pack_single_unit_over_budget_is_skipped_not_cut():
    plan = rp.plan_pack(_ranked([3000, 100]), max_tokens=2048)
    assert [c.unit_id for c in plan.packed] == ["u2"]
    assert plan.skipped[0]["reason"] == "unit_exceeds_budget"


def test_pack_does_not_fill_to_seven():
    assert len(rp.pack_budget(_ranked([50, 50, 50]))) == 3


def test_pack_unit_cap():
    plan = rp.plan_pack(_ranked([10] * 9), max_units=7)
    assert len(plan.packed) == 7
    assert [s["reason"] for s in plan.skipped] == ["unit_cap", "unit_cap"]


# ---------------------------------------------------------------------------
# 可達性與 k 凍結
# ---------------------------------------------------------------------------
def test_reachability_marks_pool_outside_as_unresolved():
    # 保存名次（舊表示）：N01 seg1:346 第 65、W01 seg1:162 第 87、P01 seg1:371 第 123、M02 粉底表第 411
    r = rp.reachability({"seg1:346": 65, "seg1:162": 87, "seg1:371": 123, "tx001_table": 411, "x": None}, 30)
    assert {k: v["status"] for k, v in r.items()} == {
        "seg1:346": "pool_outside", "seg1:162": "pool_outside", "seg1:371": "pool_outside",
        "tx001_table": "pool_outside", "x": "missing_in_representation"}


def test_freeze_k_pool_uses_ranks_within_cap_only():
    out = rp.freeze_k_pool({"a": 12, "b": 33, "c": 250}, k_min=20, k_cap=40)
    assert out["k_pool"] == 33
    assert out["targets"]["c"]["status"] == "pool_outside"
    assert rp.freeze_k_pool({"a": 3}, k_min=20, k_cap=40)["k_pool"] == 20
    assert rp.freeze_k_pool({"a": 300}, k_min=20, k_cap=40)["k_pool"] == 20
    with pytest.raises(PolicyError):
        rp.freeze_k_pool({}, k_min=50, k_cap=40)


def test_approx_tokens_counts_cjk_per_char():
    assert rp.approx_tokens("冷底紅") == 3
    assert rp.approx_tokens("blue red") == 2


def test_selection_reachability_without_rerank():
    # 應用不重排：選段只看向量順序下 2048 內放得下的完整單位；長段略過，後面短段仍可送判
    ranked = _ranked([900, 900, 1500, 200, 300, 100], ["a", "b", "long", "t1", "t2", "t3"])
    r = rp.selection_reachability(ranked, ["t1", "t2", "t3", "long", "zz"], k_pool=5, max_input_tokens=2048)
    assert {k: v["status"] for k, v in r.items()} == {
        "t1": "judged", "t2": "outside_selection_window", "t3": "pool_outside",
        "long": "outside_selection_window", "zz": "missing_in_representation"}


def test_budget_window_has_no_unit_cap():
    judged, skipped = rp.budget_window(_ranked([10] * 12), 2048)
    assert len(judged) == 12 and skipped == []
