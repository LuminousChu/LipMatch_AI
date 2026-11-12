"""S4 檢索的權限、隔離、索引就緒與相容性。

MOCK：本檔的嵌入器以雜湊產生向量，只用來驗證「哪些片段可以被搜尋到、送進模型」的過濾邏輯，
完全沒有語意能力，不能當成真實檢索品質的驗收（真實召回見 docs/mvp/s4-retrieval.md）。
文件內容皆為 SYNTHETIC。
"""
from __future__ import annotations

import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app import retrieval, retrieval_bench
from app.retrieval import IncompatibleIndex, IndexNotReady, RetrievalError

SHA = hashlib.sha256(b"synthetic").hexdigest()


class MockEmbedder:
    """MOCK：同一段文字得到同一個向量；不同文字幾乎正交。沒有語意。"""

    def __init__(self, model: str = "MOCK-hash", dim: int = 1024):
        self.model, self.dim = model, dim

    def embed(self, texts):
        out = []
        for t in texts:
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            vec = [((seed[i % 32] ^ (i * 31 % 256)) - 127.5) / 127.5 for i in range(self.dim)]
            out.append(vec)
        return out


@pytest.fixture
def conn(db, users):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _doc(conn, key, admin_id, *, visibility="all", publish=True, approve=True, active=True, valid_until=None):
    """建立一份含單一片段的合成文件；回傳 chunk_key（= 片段文字，方便用相同文字查詢）。"""
    doc_id = conn.execute(
        """INSERT INTO documents (doc_key, title, doc_type, visibility, is_active, rag_eligible, valid_until)
           VALUES (%s, %s, 'academic_paper', %s, %s, TRUE, %s) RETURNING id""",
        (key, f"SYNTHETIC {key}", visibility, active, valid_until),
    ).fetchone()["id"]
    v = conn.execute(
        """INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version)
           VALUES (%s, 'v1', %s, 'test') RETURNING id""", (doc_id, SHA)).fetchone()["id"]
    text = f"SYNTHETIC text of {key}"
    sha = hashlib.sha256(text.encode()).hexdigest()
    chunk = conn.execute(
        """INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text,
                               content_sha256) VALUES (%s, %s, 0, 1, %s, %s, %s) RETURNING id""",
        (v, key, text, text, sha)).fetchone()["id"]
    if approve:
        conn.execute("SELECT review_chunk(%s, %s, 'approve', '合成測試核准', %s)", (chunk, admin_id, sha))
    if publish:
        conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s", (v,))
    return text


@pytest.fixture
def corpus(conn, users):
    admin = users["root"]
    return {
        "public": _doc(conn, "PUBLIC", admin),
        "admin_only": _doc(conn, "ADMIN_ONLY", admin, visibility="admin"),
        "draft": _doc(conn, "DRAFT_ONLY", admin, publish=False),
        "pending": _doc(conn, "PENDING", admin, approve=False),
        "inactive": _doc(conn, "INACTIVE_DOC", admin, active=False),
        "expired": _doc(conn, "EXPIRED", admin, valid_until="2000-01-01"),
    }


def _ready_index(conn, label="idx1", embedder=None):
    emb = embedder or MockEmbedder()
    built = retrieval.build_index(conn, label, emb)
    retrieval.activate_index(conn, label)
    return built, emb


def _keys(hits):
    return {h.chunk_key for h in hits}


# ---------------------------------------------------------------------------
def test_formal_embedder_reports_blocker():
    with pytest.raises(retrieval.EmbeddingUnavailable, match="DEC-11"):
        retrieval.get_embedder()


def test_index_only_contains_usable_chunks(conn, corpus):
    built, _ = _ready_index(conn)
    assert built["embedded"] == 2  # PUBLIC、ADMIN_ONLY；草稿、待審、停用、過期都不建向量
    keys = {r["chunk_key"] for r in conn.execute("SELECT chunk_key FROM searchable_chunks")}
    assert keys == {"PUBLIC", "ADMIN_ONLY"}


@pytest.mark.parametrize("role,expected", [("user", {"PUBLIC"}), ("admin", {"PUBLIC", "ADMIN_ONLY"})])
def test_search_only_within_role_candidates(conn, corpus, role, expected):
    _, emb = _ready_index(conn)
    for text in corpus.values():  # 用每份文件自己的原文去查，最接近的就是它本身
        assert _keys(retrieval.search(conn, text, role, emb, k=10)) == expected


def test_isolated_documents_never_reach_context(conn, corpus):
    _, emb = _ready_index(conn)
    hits = retrieval.search(conn, corpus["admin_only"], "user", emb, k=10)
    passed, blocked = retrieval.revalidate(conn, hits, "user")
    context = retrieval.context_chunks(conn, passed)
    assert {c["chunk_key"] for c in context} == {"PUBLIC"} and blocked == {}
    assert all("ADMIN_ONLY" not in json.dumps(c, default=str) for c in context)


def test_unknown_role_rejected(conn, corpus):
    _, emb = _ready_index(conn)
    with pytest.raises(RetrievalError):
        retrieval.search(conn, "x", "superuser", emb)


def test_changes_after_search_are_caught_before_model(conn, corpus, users):
    _, emb = _ready_index(conn)
    hits = retrieval.search(conn, corpus["public"], "admin", emb, k=10)
    assert _keys(hits) == {"PUBLIC", "ADMIN_ONLY"}
    # 搜尋之後、送模型之前：一份被退回，一份文件改成過期
    pub = conn.execute("SELECT id, content_sha256 FROM chunks WHERE chunk_key = 'PUBLIC'").fetchone()
    conn.execute("SELECT review_chunk(%s, %s, 'reject', '發現錯誤', %s)", (pub["id"], users["root"], pub["content_sha256"]))
    conn.execute("UPDATE documents SET valid_until = now() - interval '1 second', "
                 "valid_from = now() - interval '1 day' WHERE doc_key = 'ADMIN_ONLY'")
    passed, blocked = retrieval.revalidate(conn, hits, "admin")
    assert passed == [] and set(blocked.values()) == {"no_longer_usable"}
    assert retrieval.search(conn, corpus["public"], "admin", emb, k=10) == []  # 下一次搜尋也不再出現


def test_visibility_change_caught_by_revalidate(conn, corpus):
    _, emb = _ready_index(conn)
    hits = retrieval.search(conn, corpus["public"], "user", emb)
    conn.execute("UPDATE documents SET visibility = 'admin' WHERE doc_key = 'PUBLIC'")
    passed, blocked = retrieval.revalidate(conn, hits, "user")
    assert passed == [] and list(blocked.values()) == ["not_visible_to_role"]


def test_incompatible_query_model_rejected(conn, corpus):
    _ready_index(conn)
    with pytest.raises(IncompatibleIndex):
        retrieval.search(conn, "x", "user", MockEmbedder(model="MOCK-other"))


def test_dimension_must_match_schema(conn, corpus):
    with pytest.raises(IncompatibleIndex):  # 768 維必須帶嵌入指紋、用文件端嵌入器（0006）
        retrieval.build_index(conn, "idx768", MockEmbedder(dim=768))
    with pytest.raises(IncompatibleIndex):  # 沒有對應向量表的維度
        retrieval.build_index(conn, "idx512", MockEmbedder(dim=512))
    assert conn.execute("SELECT count(*) AS n FROM index_versions").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# 修復 D：768 維索引（0006，xVITA）
# ---------------------------------------------------------------------------
class Mock768:
    """MOCK：模擬 model_adapters.PrefixedEmbedder 的介面（前綴、指紋、input_sha256）；向量是雜湊，沒有語意。"""

    def __init__(self, side="document", model_sha="a" * 64, doc_prefix="title: none | text: ",
                 query_prefix="task: search result | query: "):
        self.model, self.dim, self.side = "MOCK-xvita", 768, side
        self.fingerprint = {"model": self.model, "model_sha256": model_sha, "dim": 768,
                            "doc_prefix": doc_prefix, "query_prefix": query_prefix}
        self.prefix = doc_prefix if side == "document" else query_prefix
        self._inner = MockEmbedder(model=self.model, dim=768)

    def input_sha256(self, text):
        return hashlib.sha256((self.prefix + text).encode("utf-8")).hexdigest()

    # 文件端與查詢端前綴不同，但 MOCK 讓同一段原文得到同一向量，才能用原文查到自己
    def embed(self, texts):
        return self._inner.embed(texts)


def test_768_index_build_activate_search_and_isolation(conn, corpus):
    built = retrieval.build_index(conn, "x768", Mock768())
    assert built["embedded"] == 2 and built["embedding_dim"] == 768
    rows = conn.execute("SELECT chunk_id, input_sha256 FROM chunk_embeddings_v768").fetchall()
    texts = {r["chunk_id"]: r["search_text"] for r in conn.execute("SELECT chunk_id, search_text FROM usable_chunks")}
    assert all(r["input_sha256"] == Mock768().input_sha256(texts[r["chunk_id"]]) for r in rows)
    assert conn.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"] == 0   # 不寫進 1024 表
    retrieval.activate_index(conn, "x768")
    q = Mock768(side="query")
    for role, expected in (("user", {"PUBLIC"}), ("admin", {"PUBLIC", "ADMIN_ONLY"})):
        assert _keys(retrieval.search(conn, corpus["public"], role, q, k=10)) == expected
    top = retrieval.search(conn, corpus["admin_only"], "admin", q, k=1)
    assert [h.chunk_key for h in top] == ["ADMIN_ONLY"] and top[0].index_label == "x768"


def test_768_rejects_mismatched_fingerprint_and_query_side_build(conn, corpus):
    with pytest.raises(IncompatibleIndex, match="文件端"):
        retrieval.build_index(conn, "q768", Mock768(side="query"))
    retrieval.build_index(conn, "x768", Mock768())
    retrieval.activate_index(conn, "x768")
    for other in (Mock768(side="query", model_sha="b" * 64), Mock768(side="query", query_prefix="query: ")):
        with pytest.raises(IncompatibleIndex, match="指紋"):
            retrieval.search(conn, "x", "user", other)
    with pytest.raises(IncompatibleIndex):            # 1024 維查詢不能查 768 索引
        retrieval.search(conn, "x", "user", MockEmbedder(model="MOCK-xvita"))


def test_switch_between_1024_and_768_keeps_old_index(conn, corpus):
    _ready_index(conn, "b1024")
    retrieval.build_index(conn, "x768", Mock768())
    out = retrieval.activate_index(conn, "x768")
    assert out["retired"] == "b1024"
    assert conn.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"] == 2   # 舊向量保留
    assert retrieval.search(conn, corpus["public"], "user", Mock768(side="query"))[0].chunk_key == "PUBLIC"
    with pytest.raises(IncompatibleIndex):
        retrieval.search(conn, corpus["public"], "user", MockEmbedder())


def test_candidate_store_only_serves_its_own_hits(conn, corpus):
    retrieval.build_index(conn, "x768", Mock768())
    retrieval.activate_index(conn, "x768")
    store = retrieval.CandidateStore(conn, "user")
    hits = store.search(corpus["public"], 10, Mock768(side="query"))
    units = store.units(hits)
    assert [u["chunk_key"] for u in units] == ["PUBLIC"] and units[0]["search_text"] == corpus["public"]
    assert units[0]["content_sha256"] and units[0]["support_unit_present"] is False
    assert (units[0]["unit_id"], units[0]["text"], units[0]["source_id"]) == ("PUBLIC", corpus["public"], "PUBLIC")
    # B 的介面：store.vector_search(查詢向量, k) → {unit_id, text, score, tokens, meta}，同樣受角色與指紋限制
    rows = retrieval.CandidateStore(conn, "user", Mock768(side="query")).vector_search(
        Mock768(side="query").embed([corpus["admin_only"]])[0], 10)
    assert [r["unit_id"] for r in rows] == ["PUBLIC"] and rows[0]["tokens"] is None
    assert rows[0]["meta"]["conditions"] == [] and rows[0]["meta"]["version_label"] == "v1"
    with pytest.raises(IncompatibleIndex, match="指紋"):
        retrieval.CandidateStore(conn, "user", Mock768(side="query", model_sha="b" * 64)).vector_search([0.1] * 768, 5)
    with pytest.raises(RetrievalError, match="嵌入器"):
        retrieval.CandidateStore(conn, "user").vector_search([0.1] * 768, 5)
    other = retrieval.search(conn, corpus["admin_only"], "admin", Mock768(side="query"), k=10)
    with pytest.raises(RetrievalError, match="本次搜尋"):
        store.units(other)


def test_no_active_index_and_not_ready_index(conn, corpus, users):
    emb = MockEmbedder()
    with pytest.raises(IndexNotReady):
        retrieval.search(conn, "x", "user", emb)
    retrieval.build_index(conn, "idx1", emb)
    # 建索引後才核准發布的新片段：新索引缺它的向量 → 不能啟用
    _doc(conn, "LATE", users["root"])
    with pytest.raises(IndexNotReady, match="缺 1"):
        retrieval.activate_index(conn, "idx1")
    assert retrieval.index_coverage(conn, "idx1") == {"usable": 3, "embedded": 2, "missing": 1}


def test_new_index_replaces_old_and_old_vectors_not_used(conn, corpus):
    _ready_index(conn, "idx1")
    result = retrieval.activate_index(conn, retrieval.build_index(conn, "idx2", MockEmbedder())["label"])
    assert result["retired"] == "idx1"
    labels = {r["index_label"] for r in conn.execute("SELECT index_label FROM searchable_chunks")}
    assert labels == {"idx2"}
    with pytest.raises(psycopg.errors.CheckViolation):  # active 索引的向量不可刪改
        conn.execute("DELETE FROM chunk_embeddings WHERE index_version_id = "
                     "(SELECT id FROM index_versions WHERE label = 'idx2')")


def test_record_hits_keeps_blocked_with_reason(conn, corpus, users):
    _, emb = _ready_index(conn)
    hits = retrieval.search(conn, corpus["public"], "admin", emb, k=10)
    conn.execute("UPDATE documents SET visibility = 'admin' WHERE doc_key = 'PUBLIC'")
    passed, blocked = retrieval.revalidate(conn, hits, "user")
    conv = conn.execute("INSERT INTO conversations (user_id) VALUES (%s) RETURNING id", (users["alice"],)).fetchone()["id"]
    rid = uuid.uuid4()
    conn.execute("INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s, %s, %s, 'q')",
                 (rid, users["alice"], conv))
    retrieval.record_hits(conn, rid, hits, blocked)
    rows = conn.execute("SELECT passed_filter, filter_reason, detail FROM retrieval_hits ORDER BY rank").fetchall()
    assert len(rows) == 2 and all(not r["passed_filter"] for r in rows)
    assert {r["filter_reason"] for r in rows} == {"not_visible_to_role"}
    assert rows[0]["detail"]["index_label"] == "idx1"


def test_bench_reports_blockers_instead_of_running(conn, corpus, monkeypatch, db, capsys):
    monkeypatch.setenv("DATABASE_URL", db)
    with pytest.raises(SystemExit) as exc:
        retrieval_bench.main(["--role", "user"])
    message = str(exc.value)
    assert "DEC-11" in message and "沒有 active 索引" in message and "S4Q1" in message


def test_known_evidence_file_is_well_formed():
    spec = json.loads(retrieval_bench.QUERIES.read_text(encoding="utf-8"))
    assert len(spec["queries"]) == 5
    assert sum(q["lang"] == "zh→en" for q in spec["queries"]) >= 4
    assert all(q["expected"] and q["answer_limits"] for q in spec["queries"])


def test_units_by_id_is_fixed_mapping_of_returned_units(conn, corpus):
    retrieval.build_index(conn, "x768", Mock768())
    retrieval.activate_index(conn, "x768")
    store = retrieval.CandidateStore(conn, "user", Mock768(side="query"))
    rows = store.vector_search(Mock768(side="query").embed([corpus["public"]])[0], 10)
    mapping = store.units_by_id([r["unit_id"] for r in rows] + ["ADMIN_ONLY", "NOT_A_UNIT"])
    assert list(mapping) == ["PUBLIC"]                         # 只含本 store 交出的單位
    u = mapping["PUBLIC"]
    assert u["text_sha256"] == hashlib.sha256(corpus["public"].encode("utf-8")).hexdigest() == rows[0]["meta"]["text_sha256"]
    assert (u["version_label"], u["source_id"]) == ("v1", "PUBLIC")


def test_build_index_cli_cache_only(conn, corpus, db, monkeypatch, tmp_path, capsys):
    from app import model_adapters as ma
    sha = "a" * 64
    monkeypatch.setenv("DATABASE_URL", db)
    for k, v in {"EMBEDDING_PROVIDER": "llamacpp", "EMBEDDING_BASE_URL": "http://127.0.0.1:9",
                 "EMBEDDING_MODEL": "MOCK-xvita", "EMBEDDING_DIM": "768", "EMBEDDING_MODEL_SHA256": sha,
                 "EMBEDDING_DOC_PREFIX": "title: none | text: ", "EMBEDDING_QUERY_PREFIX": "q: "}.items():
        monkeypatch.setenv(k, v)
    texts = [r["search_text"] for r in conn.execute("SELECT search_text FROM usable_chunks")]
    cache = ma.EmbeddingCache()
    cache.put(sha, "title: none | text: ", texts[0], [0.1] * 768)
    path = tmp_path / "cache.jsonl"
    cache.save(path)
    with pytest.raises(SystemExit, match="快取未命中 1 筆"):     # 缺一筆就整批失敗；服務埠 9 不存在也不會被呼叫
        retrieval.main(["build-index", "--label", "c768", "--cache", str(path), "--cache-only"])
    assert conn.execute("SELECT count(*) AS n FROM index_versions").fetchone()["n"] == 0   # 交易整批回滾
    for t in texts[1:]:
        cache.put(sha, "title: none | text: ", t, [0.2] * 768)
    cache.save(path)
    retrieval.main(["build-index", "--label", "c768", "--cache", str(path), "--cache-only", "--activate"])
    out = json.loads(capsys.readouterr().out)
    assert out["cache"] == {"path": str(path), "hits": 2, "misses": 0} and out["activate"]["missing"] == 0


def test_candidate_store_repeated_search_reuses_first_hit(conn, corpus):
    """multi-need-v1（2026-11-07）：同一個 store 搜兩次、結果重疊時，units() 仍可用（回傳第一次交出的同一份命中）。"""
    retrieval.build_index(conn, "x768", Mock768())
    retrieval.activate_index(conn, "x768")
    store = retrieval.CandidateStore(conn, "user")
    first = store.search(corpus["public"], 10, Mock768(side="query"))
    second = store.search(corpus["public"], 10, Mock768(side="query"))
    assert [h.chunk_key for h in second] == [h.chunk_key for h in first] and second[0] is first[0]
    assert [u["chunk_key"] for u in store.units(first + second)] == ["PUBLIC"]   # 同一段只取一次
    other = retrieval.search(conn, corpus["admin_only"], "admin", Mock768(side="query"), k=10)
    with pytest.raises(RetrievalError, match="本次搜尋"):
        store.units(other)
