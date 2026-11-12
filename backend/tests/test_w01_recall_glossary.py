"""W01 召回修正（詞表 v3.2、hybrid-v4）：口語色彩別名、否定概念仍檢索但保留排除條件、品類泛稱不啟動、截斷追溯。

純程式測試，不連資料庫、不呼叫模型；hybrid_search 的兩路結果用假資料替換。
"""
from __future__ import annotations

from app import qa, query_glossary as qg, retrieval

W01 = "我想找「紅色」口紅，可是不要那種偏粉或偏橘的紅，這樣要怎麼說比較準？"


def concepts(query: str) -> dict[str, str]:
    """{命中原詞: 極性}，方便斷言。"""
    return {c["text"]: c["polarity"] for c in qg.analyze(query).concepts}


def test_w01_colloquial_orange_red_is_recognized_and_negated():
    """原 W01：「偏橘的紅」對到 orange-red，否定跨過「偏粉或」仍成立；負向概念進關鍵詞路。"""
    exp = qg.analyze(W01)
    assert concepts(W01)["偏橘的紅"] == "exclude"
    assert "orange-red" in exp.negated_phrases and "orange-red" not in exp.english_phrases
    plan = qa.keyword_plan(exp)
    assert "orange-red" in plan["core"] and plan["rule"] == "negated_concept_only"
    assert "lipstick" in plan["weak"]


def test_negated_concept_kept_as_exclusion_condition():
    """「不要偏橘的紅」：檢索 orange-red，但使用者條件是排除，不進提示用的術語對照。"""
    exp = qg.analyze("不要偏橘的紅")
    plan = qa.keyword_plan(exp)
    assert plan["core"] == ["orange-red"] and plan["rule"] == "negated_concept_only"
    log = qa._concept_log(exp, plan["core"])
    assert log == [{"text": "偏橘的紅", "english": ["orange-red"], "polarity": "exclude",
                    "retrieval": True, "user_condition": "排除"}]
    assert qa.term_lookup(exp) == []


def test_negative_and_positive_concepts_kept_separately():
    """「不要偏橘的紅，但想要藍調紅」：兩個概念、兩種極性分開保留。"""
    q = "不要偏橘的紅，但想要藍調紅"
    assert concepts(q) == {"偏橘的紅": "exclude", "藍調紅": "include"}
    exp = qg.analyze(q)
    assert exp.english_phrases == ("blue-red",) and exp.negated_phrases == ("orange-red",)
    plan = qa.keyword_plan(exp)
    assert plan["core"] == ["blue-red", "orange-red"]
    assert [t["english"] for t in qa.term_lookup(exp)] == [["blue-red"]]


def test_pinkish_red_not_converted_to_blue_red():
    """「不要偏粉的紅」：沒有可靠對照，不變成 blue-red，也不啟動關鍵詞路。"""
    exp = qg.analyze("不要偏粉的紅")
    assert all("blue-red" not in c["english"] for c in exp.concepts)
    assert qa.keyword_plan(exp)["core"] == []


def test_generic_category_alone_does_not_start_keyword_route():
    """只有「口紅」：品類泛稱，維持不啟動。"""
    plan = qa.keyword_plan(qg.analyze("口紅"))
    assert plan == {"core": [], "weak": ["lipstick"], "rule": "generic_only_vector_route"}


def test_full_compound_preferred_over_single_words():
    """完整複合詞優先：「橘色調的紅」整串對到 orange-red，不拆成 orange、red。"""
    exp = qg.analyze("想要橘色調的紅")
    assert [m.text for m in exp.matches if m.key == "orange_red"] == ["橘色調的紅"]
    assert exp.english_phrases == ("orange-red",)


def test_negation_scope_not_whole_sentence():
    """否定只看術語前緊接的文字：句首「不要太亮」不會讓後面的「想要藍調紅」變成否定。"""
    assert concepts("不要太亮，我想要藍調紅")["藍調紅"] == "include"


def test_hybrid_keeps_cut_candidates_with_reason(monkeypatch):
    """hybrid_search 先合併去重再截斷；截掉的候選留在追溯裡，附融合名次與原因。"""
    def hit(cid, rank):
        return retrieval.Hit(chunk_id=cid, chunk_key=f"U{cid}", doc_key="D", version_label="v", page_start=None,
                             page_end=None, content_sha256="x", index_label="i", distance=0.1, rank=rank)
    vec = [hit(i, i) for i in range(1, 6)]
    kw = [hit(9, 1), hit(3, 2)]
    monkeypatch.setattr(retrieval, "active_index", lambda conn: {"id": 1, "embedding_dim": 2})
    monkeypatch.setattr(retrieval, "check_compatible", lambda index, embedder: None)
    monkeypatch.setattr(retrieval, "_vector_search", lambda *a, **k: vec)
    monkeypatch.setattr(retrieval, "_keyword_search", lambda *a, **k: kw)

    class Emb:
        def embed(self, texts):
            return [[0.0, 1.0]]
    merged, trace = retrieval.hybrid_search(None, "q", ["orange-red"], "user", Emb(), k=3)
    assert [h.chunk_key for h in merged] == ["U3", "U1", "U9"]
    assert len({r["unit_id"] for r in trace["merged"] + trace["cut"]}) == 6          # 去重：U3 只算一次
    assert trace["cut"][0]["fused_rank"] == 4 and "候選池截斷" in trace["cut"][0]["reason"]
