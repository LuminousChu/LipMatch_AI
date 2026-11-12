"""need-rel-v1（2026-10-26）：裝箱前的需求相關性排序。片段都是 SYNTHETIC，屬性標籤格式同 support_units 的 support_scope。

整支測試在檢查什麼：
1. 問題線索 → 片段屬性標籤；「乾燥玫瑰」是色名，不當成嘴唇乾。
2. 命中條件／操作／用途的片段排前面；問做法時，命中的研究觀察、背景、商品宣稱排在做法類之後；只命中部位不觸發排後。
3. 使用者明說底調、片段條件列了不同底調 → 排到最後；相符的加權。
4. 沒有任何命中 → 原向量名次不變；pool_rank 與片段物件本身不改。
"""
from __future__ import annotations

from app import need_relevance as NR
from app.retrieval_policy import Candidate


def cand(uid, rank, scope=None, role=None):
    return Candidate(unit_id=uid, text=f"SYNTHETIC {uid}", pool_rank=rank, tokens=10,
                     meta={"support_scope": scope, "content_role": role})


def ids(cs):
    return [c.unit_id for c in cs]


def test_scope_tags_and_need_tags():
    assert NR.scope_tags("部位：嘴唇、眼部；條件：眼妝重；原文條件：with smoky eyes") == {
        "部位": {"嘴唇", "眼部"}, "條件": {"眼妝重"}, "原文條件": {"with smoky eyes"}}
    assert NR.need_tags("我嘴唇容易乾，想擦霧面") == ["乾唇", "保濕", "嘴唇"]
    assert NR.need_tags("MLBB 和乾燥玫瑰色差在哪？") == []                 # 色名不是唇部乾燥
    assert NR.need_tags("色號名字很抽象，我要怎麼知道它實際是什麼顏色？") == ["試色"]


def test_method_question_moves_direct_method_ahead_of_research():
    cs = [cand("research-1", 1, "部位：嘴唇", "研究觀察"),
          cand("research-try", 2, "部位：嘴唇；操作：試色", "研究觀察"),
          cand("generic-lip", 3, "部位：嘴唇；條件：暖底", "選色"),
          cand("swatch-method", 15, "部位：嘴唇；操作：試色", "選色")]
    out, log = NR.order_for_pack("口紅色號很抽象，要怎麼知道實際顏色？", [], cs)
    assert ids(out) == ["swatch-method", "research-try", "research-1", "generic-lip"]
    assert [c.pool_rank for c in out] == [15, 2, 1, 3]                     # 原名次保留
    assert log["need_tags"] == ["試色", "嘴唇"] and log["method_question"] is True
    # 只命中部位（嘴唇）的研究段不排後：research-1 仍依原名次排在 generic-lip 前


def test_user_undertone_conflict_goes_last_and_match_is_weighted():
    cs = [cand("warm-only", 1, "部位：嘴唇；條件：暖底"), cand("plain", 2, "部位：嘴唇"),
          cand("cool", 3, "部位：嘴唇；條件：冷底、中性底")]
    cool = [{"field": "undertone", "value": "cool", "raw": "我是冷底"}]
    out, log = NR.order_for_pack("我是冷底，想擦紅色", cool, cs)
    assert ids(out) == ["cool", "plain", "warm-only"]
    assert log["user_undertone"] == "冷底"
    assert [r["condition_conflict"] for r in log["order"]] == [False, False, True]


def test_no_cue_keeps_vector_order():
    cs = [cand("b", 1, "部位：眼部"), cand("a", 2, None), cand("c", 3, "色名：紅色")]
    out, log = NR.order_for_pack("豆沙色跟奶茶色差在哪裡？", [], cs)
    assert ids(out) == ["b", "a", "c"] and log["need_tags"] == []


def test_support_scope_survives_candidate_building_end_to_end():
    """2026-10-26 實機發現：retrieve_candidates 原本沒把 support_scope 帶進 Candidate.meta，排序在應用裡全為 0 分。
    這裡從 store 的 support_unit 一路走 retrieve_candidates → order_for_pack，確認屬性真的有讀到。"""
    from app import retrieval_policy as rp

    from .test_retrieval_policy import ListStore, MockEmbedder
    rows = [{"unit_id": "research", "text": "SYNTHETIC research", "tokens": 10,
             "meta": {"support_scope": "部位：嘴唇", "content_role": "研究觀察"}},
            {"unit_id": "swatch", "text": "SYNTHETIC swatch", "tokens": 10,
             "meta": {"support_scope": "部位：嘴唇；操作：試色", "content_role": "選色"}}]
    cands = rp.retrieve_candidates("q", k_pool=5, embedder=MockEmbedder(), store=ListStore(rows))
    assert cands[1].meta["support_scope"] == "部位：嘴唇；操作：試色"
    out, log = NR.order_for_pack("色號很抽象，要怎麼知道實際顏色？", [], cands)
    assert ids(out) == ["swatch", "research"] and log["order"][0]["score"] == 2


def test_flattened_store_rows_also_carry_attributes():
    """D 的 CandidateStore.units() 實際交回攤平列（unit_view，沒有 support_unit 子物件）；屬性與 cl100k 都要讀得到。"""
    from app import retrieval_policy as rp

    from .test_retrieval_policy import FakeHit, MockEmbedder

    class FlatStore:
        def search(self, query, k, embedder):
            embedder.embed([query])
            return [FakeHit(rank=1, chunk_id=1, chunk_key="swatch", score=0.5)]

        def units(self, hits):
            return [{"chunk_id": 1, "chunk_key": "swatch", "quote_text": "SYNTHETIC swatch", "doc_key": "SYN",
                     "version_label": "v-syn", "support_scope": "部位：嘴唇；操作：試色", "content_role": "選色",
                     "conditions": ["When shopping online"], "product_mentions": [], "tokens_cl100k": 7}]
    c = rp.retrieve_candidates("q", k_pool=5, embedder=MockEmbedder(), store=FlatStore())[0]
    assert (c.meta["support_scope"], c.meta["content_role"], c.meta["conditions"]) == (
        "部位：嘴唇；操作：試色", "選色", ["When shopping online"])
    assert c.tokens == 7 and c.tokens_source == "cl100k"           # 2026-10-26：攤平列的 cl100k 也要讀到


def test_v2_weak_part_tags_do_not_override_vector_rank():
    """need-rel-v2：只命中「部位」的弱訊號不改順序；命中條件／操作／用途才往前提。"""
    cs = [cand("eye-day", 1, "部位：眼部；條件：眼妝重、白天日常"),
          cand("lip-eye-generic", 2, "部位：嘴唇、眼部"),
          cand("blush-or-eye-soften", 3, "部位：眼部、臉頰、腮紅"),
          cand("lip-eye-heavy", 4, "部位：嘴唇、眼部；條件：眼妝重")]
    out, log = NR.order_for_pack("眼妝很濃的煙燻，唇色要怎麼配？", [], cs)
    # 強標籤（眼妝重）兩段依原名次在前；其後兩段只有部位，維持原名次（v1 會把部位多的 lip-eye-generic 排在 blush 前）
    assert ids(out) == ["eye-day", "lip-eye-heavy", "lip-eye-generic", "blush-or-eye-soften"]
    assert [r["strong_score"] for r in log["order"]] == [2, 2, 0, 0]
    assert NR.NEED_REL_VERSION == "need-rel-v4"   # 2026-10-27 v3：本例排序不變
