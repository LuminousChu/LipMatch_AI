"""hybrid-v1／v2（2026-10-27）：術語表英文說法的關鍵詞路＋RRF 合併、回答提示的術語對照、QA_GLOSSARY 開關。

測試資料全是合成（SYNTHETIC）；MockEmbedder 沒有語意，只用來確認關鍵詞路能把向量路排不到的片段帶進池，
以及關鍵詞路和向量路一樣只在角色可用、已核准發布的片段裡找。檢索品質要另跑只檢索對照，不由本檔判斷。
"""
from __future__ import annotations

import hashlib

import psycopg
import pytest
from psycopg.rows import dict_row

from app import answer_policy as AP
from app import qa, query_glossary, retrieval
from app.retrieval import Hit
from tests.test_answer_policy import UNITS
from tests.test_retrieval import SHA, MockEmbedder


@pytest.fixture
def conn(db, users):
    """自動提交的資料庫連線（同 test_retrieval）。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _chunk(conn, key, text, admin_id, *, visibility="all", publish=True):
    """建立一份含單一已核准片段的合成文件，片段原文與檢索文字都是 text。"""
    doc_id = conn.execute(
        """INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible)
           VALUES (%s, %s, 'academic_paper', %s, TRUE, TRUE) RETURNING id""",
        (key, f"SYNTHETIC {key}", visibility)).fetchone()["id"]
    v = conn.execute(
        """INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version)
           VALUES (%s, 'v1', %s, 'test') RETURNING id""", (doc_id, SHA)).fetchone()["id"]
    sha = hashlib.sha256(text.encode()).hexdigest()
    chunk = conn.execute(
        """INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text,
                               content_sha256) VALUES (%s, %s, 0, 1, %s, %s, %s) RETURNING id""",
        (v, key, text, text, sha)).fetchone()["id"]
    conn.execute("SELECT review_chunk(%s, %s, 'approve', '合成測試核准', %s)", (chunk, admin_id, sha))
    if publish:
        conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s", (v,))


@pytest.fixture
def corpus(conn, users):
    """10 段無關片段＋1 段含 hyaluronic acid；另有管理員限定與草稿各一段也含該詞（使用者不能拿到）。"""
    admin = users["root"]
    for i in range(10):
        _chunk(conn, f"FILLER{i}", f"SYNTHETIC filler paragraph number {i} about swatches", admin)
    _chunk(conn, "HA", "SYNTHETIC nourishing matte formulas with oils or hyaluronic acid", admin)
    _chunk(conn, "HA_ADMIN", "SYNTHETIC admin note on hyaluronic acid", admin, visibility="admin")
    _chunk(conn, "HA_DRAFT", "SYNTHETIC draft on hyaluronic acid", admin, publish=False)
    emb = MockEmbedder()
    retrieval.build_index(conn, "idx-hybrid", emb)
    retrieval.activate_index(conn, "idx-hybrid")
    return emb


def _hit(cid, rank):
    """合成命中（只填 RRF 會用到的欄位）。"""
    return Hit(rank=rank, chunk_id=cid, chunk_key=f"U{cid}", doc_key="D", version_label="v1", page_start=None,
               page_end=None, content_sha256="x", index_label="i", distance=0.5)


def test_rrf_both_routes_rank_first_and_trace_records_ranks():
    merged, trace = retrieval.rrf_merge([_hit(1, 1), _hit(2, 2), _hit(3, 3)], [_hit(3, 1), _hit(4, 2)], k=4)
    assert [h.chunk_key for h in merged] == ["U3", "U1", "U2", "U4"]   # U2、U4 同分時向量名次在前
    assert [h.rank for h in merged] == [1, 2, 3, 4]
    assert trace[0] == {"unit_id": "U3", "vector_rank": 3, "keyword_rank": 1, "rrf": round(1 / 63 + 1 / 61, 6)}
    assert trace[3]["vector_rank"] is None


def test_rrf_without_keyword_keeps_vector_order():
    vec = [_hit(i, i) for i in range(1, 6)]
    merged, _ = retrieval.rrf_merge(vec, [], k=5)
    assert [h.chunk_id for h in merged] == [1, 2, 3, 4, 5]


def test_keyword_route_brings_term_into_pool_within_governance(conn, corpus):
    query = "我嘴唇容易乾，有含玻尿酸的嗎"
    vector_only = [h.chunk_key for h in retrieval.search(conn, query, "user", corpus, k=3)]
    hits, trace = retrieval.hybrid_search(conn, query, ["hyaluronic acid"], "user", corpus, k=3)
    keys = [h.chunk_key for h in hits]
    assert "HA" in keys and ("HA" not in vector_only or vector_only.index("HA") >= keys.index("HA"))
    assert trace["keyword_count"] == 1          # 管理員限定、草稿都不在使用者的關鍵詞結果
    admin_keys, _ = retrieval.hybrid_search(conn, query, ["hyaluronic acid"], "admin", corpus, k=20)
    assert {"HA", "HA_ADMIN"} <= {h.chunk_key for h in admin_keys}
    assert "HA_DRAFT" not in {h.chunk_key for h in admin_keys}
    ha = next(h for h in hits if h.chunk_key == "HA")
    assert 0.0 <= ha.distance <= 2.0           # 合併後 distance 仍是 cosine 距離


def test_candidate_store_without_phrases_is_vector_only(conn, corpus):
    store = retrieval.CandidateStore(conn, "user", corpus)
    plain = [h.chunk_key for h in store.search("SYNTHETIC filler", 5, corpus)]
    assert store.hybrid_trace is None
    assert plain == [h.chunk_key for h in retrieval.search(conn, "SYNTHETIC filler", "user", corpus, k=5)]
    store2 = retrieval.CandidateStore(conn, "user", corpus, keyword_phrases=["hyaluronic acid"])
    store2.search("SYNTHETIC filler", 5, corpus)
    assert store2.hybrid_trace["version"] == retrieval.HYBRID_VERSION
    assert store2.units(store2.returned_hits())   # 兩路交出的命中都能取原文（store 有記下）


def test_term_lookup_skips_negated_and_literal_and_keeps_note():
    terms = qa.term_lookup(query_glossary.analyze("我是小麥色皮膚，想找 HEX 色碼，不要霧面"))
    by_term = {t["user_term"]: t for t in terms}
    assert "小麥色皮膚" in by_term and "tan" in by_term["小麥色皮膚"]["note"]
    assert "霧面" not in by_term            # 否定詞不給對照
    assert all(t["user_term"] != "HEX" for t in terms)   # 原句本來就是英文，不用對照


def test_answer_prompt_unchanged_without_lookup_and_adds_lookup_after_evidence():
    sel = {"kept_unit_ids": ["a"], "items": []}
    base = AP.build_answer_prompt("q", {}, sel, UNITS)
    assert AP.build_answer_prompt("q", {}, sel, UNITS, term_lookup=[]).sha256 == base.sha256
    lookup = [{"user_term": "玻尿酸", "english": ["hyaluronic acid"], "note": ""}]
    p = AP.build_answer_prompt("q", {}, sel, UNITS, term_lookup=lookup)
    user = p.messages[1]["content"]
    assert user.index('"evidence"') < user.index('"term_lookup"')
    assert "hyaluronic acid" in user and '"note"' not in user   # 空備註不送
    assert p.messages[0] == base.messages[0]                      # 系統提示不變


def test_qa_glossary_env_switch(monkeypatch):
    monkeypatch.delenv("QA_GLOSSARY", raising=False)
    assert qa.QaConfig.from_env().glossary is False
    monkeypatch.setenv("QA_GLOSSARY", "on")
    cfg = qa.QaConfig.from_env()
    assert cfg.glossary is True and cfg.log_view()["glossary"] is True
    monkeypatch.setenv("QA_GLOSSARY", "yes")
    with pytest.raises(RuntimeError, match="QA_GLOSSARY"):
        qa.QaConfig.from_env()


# ---------- hybrid-v2：泛稱詞降權 ----------

@pytest.mark.parametrize("question,core,weak,rule", [
    ("原生唇色比較深適合什麼口紅？", ["naturally pigmented", "pigmented lips"], ["lipstick"], "generic_downweighted"),
    ("唇膏與唇蜜有什麼不同？", ["lipstick", "lip gloss"], [], "categories_are_focus"),
    ("rosewood lipstick 是什麼顏色？", ["rosewood"], ["lipstick"], "generic_downweighted"),
    ("想找口紅推薦", [], ["lipstick"], "generic_only_vector_route"),
    ("深唇色適合什麼口紅？", [], ["lipstick"], "generic_only_vector_route"),   # 深唇色是詞表保留的歧義詞，沒有英文
])
def test_keyword_plan_downweights_generic_product_terms(question, core, weak, rule):
    plan = qa.keyword_plan(query_glossary.analyze(question))
    assert (plan["core"], plan["weak"], plan["rule"]) == (core, weak, rule)


# ---------- hybrid-v3：場合詞降權（T4 詞表開 N03／P03：office 帶進沙龍接待、照度實驗片段） ----------

@pytest.mark.parametrize("question,core,weak,rule", [
    ("上班想擦不要太亮的顏色", [], ["office"], "context_only_vector_route"),
    ("上班用的口紅要選什麼顏色？", [], ["lipstick", "office"], "context_only_vector_route"),
    ("我是暖底，上班適合什麼顏色？", ["warm undertone"], ["office"], "context_downweighted"),
    ("rosewood lipstick 適合上班嗎？", ["rosewood"], ["lipstick", "office"], "generic_downweighted"),
    ("不是上班用，是約會要擦的", [], [], "no_phrases"),   # 被否定的場合詞不進任何一路
])
def test_keyword_plan_downweights_context_terms(question, core, weak, rule):
    plan = qa.keyword_plan(query_glossary.analyze(question))
    assert (plan["core"], plan["weak"], plan["rule"]) == (core, weak, rule)


def test_context_only_question_keeps_vector_order(conn, weak_corpus):
    """只剩場合詞時沒有核心詞：CandidateStore 走純向量路，名次和 search() 相同、沒有 hybrid 追溯。"""
    plan = qa.keyword_plan(query_glossary.analyze("上班想擦不要太亮的顏色"))
    store = retrieval.CandidateStore(conn, "user", weak_corpus, keyword_phrases=plan["core"],
                                     weak_phrases=plan["weak"])
    hits = store.search("q", 3, weak_corpus)
    assert [h.chunk_key for h in hits] == [h.chunk_key for h in retrieval.search(conn, "q", "user", weak_corpus, k=3)]
    assert store.hybrid_trace is None


@pytest.fixture
def weak_corpus(conn, users):
    """三段：核心詞＋泛稱詞、只有核心詞、只有泛稱詞（各重複幾次泛稱詞，讓只有泛稱的那段 ts_rank 最高）。"""
    admin = users["root"]
    _chunk(conn, "BOTH", "SYNTHETIC a rosewood lipstick shade", admin)
    _chunk(conn, "CORE", "SYNTHETIC a rosewood tone for daytime", admin)
    _chunk(conn, "WEAK", "SYNTHETIC lipstick lipstick lipstick lipstick", admin)
    emb = MockEmbedder()
    retrieval.build_index(conn, "idx-weak", emb)
    retrieval.activate_index(conn, "idx-weak")
    return emb


def test_weak_only_chunk_is_not_keyword_hit_and_weak_boosts_core(conn, weak_corpus):
    _, trace = retrieval.hybrid_search(conn, "q", ["rosewood"], "user", weak_corpus, k=3, weak_phrases=["lipstick"])
    kw = {r["unit_id"]: r["keyword_rank"] for r in trace["merged"]}
    assert kw["WEAK"] is None                       # 只有泛稱詞：不算關鍵詞命中
    assert kw["BOTH"] == 1 and kw["CORE"] == 2      # 泛稱詞只幫已命中核心詞的片段往前
    assert trace["keyword_count"] == 2 and trace["weak_phrases"] == ["lipstick"]
    store = retrieval.CandidateStore(conn, "user", weak_corpus, keyword_phrases=[], weak_phrases=["lipstick"])
    store.search("q", 3, weak_corpus)
    assert store.hybrid_trace is None               # 只有泛稱詞：關鍵詞路不跑
