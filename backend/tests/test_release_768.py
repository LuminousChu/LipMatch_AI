"""S9 發布流程在 768 維索引（0006，xVITA）下的 prepare／switch，以及「只供引用」片段不建向量。

SYNTHETIC／MOCK（不是正式驗收）：文件與片段是合成的；Mock768 以雜湊產生向量、沒有語意，只驗證流程。
"""
from __future__ import annotations

import hashlib

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import release, retrieval
from app.release import ReleaseError
from tests.test_qa import QUESTION, _doc
from tests.test_retrieval import Mock768

OP = "pytest-release-768"


class CountingMock768(Mock768):
    """MOCK：計算 embed 被呼叫幾次（驗證可沿用的向量不會再送嵌入）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    # 每呼叫一次加一，再交給 Mock768 產生雜湊向量。
    def embed(self, texts):
        self.calls += 1
        return super().embed(texts)


@pytest.fixture
def conn(db, users):
    """autocommit＋dict_row 的測試連線。"""
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _copy_with_citation_only(conn, admin: int) -> int:
    """PUBLIC v2：v1 片段逐字複製（同內容指紋）＋一個已核准、只供引用（citation_only）的新片段。"""
    doc = conn.execute("SELECT id FROM documents WHERE doc_key = 'PUBLIC'").fetchone()
    vid = conn.execute("""INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version)
                          VALUES (%s, 'v2', %s, 'test') RETURNING id""", (doc["id"], _sha("v2"))).fetchone()["id"]
    old = conn.execute("""SELECT c.chunk_key, c.page_start, c.quote_text, c.search_text, c.content_sha256 FROM chunks c
                            JOIN document_versions dv ON dv.id = c.document_version_id
                           WHERE dv.document_id = %s AND dv.status = 'published'""", (doc["id"],)).fetchone()
    rows = [(old["chunk_key"], old["quote_text"], old["content_sha256"], {}),
            ("PUBLIC_card", "SYNTHETIC first sentence only", _sha("SYNTHETIC first sentence only"),
             {"citation_only": True, "parent_chunk_key": old["chunk_key"]})]
    for i, (key, text, sha, attrs) in enumerate(rows):
        cid = conn.execute(
            """INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text, search_text,
                                   content_sha256, attributes) VALUES (%s, %s, %s, 4, %s, %s, %s, %s) RETURNING id""",
            (vid, key, i, text, text, sha, Jsonb(attrs))).fetchone()["id"]
        conn.execute("SELECT review_chunk(%s, %s, 'approve', '合成測試核准', %s)", (cid, admin, sha))
    return vid


@pytest.fixture
def indexed768(conn, users):
    """兩份公開合成文件（v1 已發布）＋ active 768 維 MOCK 索引 x768。"""
    admin = users["root"]
    _doc(conn, "PUBLIC", admin, QUESTION)
    _doc(conn, "OTHER", admin, "SYNTHETIC other document text")
    retrieval.build_index(conn, "x768", Mock768())
    retrieval.activate_index(conn, "x768")
    return admin


def test_prepare_768_reuses_vectors_and_skips_citation_only(conn, indexed768):
    _copy_with_citation_only(conn, indexed768)
    emb = CountingMock768()
    prep = release.prepare(conn, "PUBLIC", "v2", emb, actor=OP, index_label="x768-v2")
    assert (prep["reused"], prep["embedded"], emb.calls) == (2, 0, 0)     # 內容相同全部沿用、不送嵌入
    rows = conn.execute("""SELECT c.chunk_key, ce.input_sha256 FROM chunk_embeddings_v768 ce
                             JOIN chunks c ON c.id = ce.chunk_id JOIN index_versions iv ON iv.id = ce.index_version_id
                            WHERE iv.label = 'x768-v2'""").fetchall()
    assert "PUBLIC_card" not in {r["chunk_key"] for r in rows}              # 只供引用的片段不建向量
    assert all(r["input_sha256"] for r in rows)                            # 768 維沿用時一併帶嵌入輸入 SHA
    assert conn.execute("SELECT embedding_fingerprint FROM index_versions WHERE label = 'x768-v2'"
                        ).fetchone()["embedding_fingerprint"] == Mock768().fingerprint

    out = release.switch(conn, "PUBLIC", "v2", actor=OP, index_label="x768-v2")
    assert out["published"] == "v2" and retrieval.active_index(conn)["label"] == "x768-v2"
    usable = {r["chunk_key"] for r in conn.execute("SELECT chunk_key FROM usable_chunks WHERE doc_key = 'PUBLIC'")}
    searchable = {r["chunk_key"] for r in conn.execute(
        "SELECT chunk_key FROM searchable_chunks_v768 WHERE doc_key = 'PUBLIC'")}
    assert "PUBLIC_card" in usable and "PUBLIC_card" not in searchable     # 可當證據，但不進檢索


def test_prepare_768_embeds_only_new_content(conn, indexed768):
    _copy_with_citation_only(conn, indexed768)
    conn.execute("""UPDATE chunks SET attributes = '{}'::jsonb WHERE chunk_key = 'PUBLIC_card'
                       AND document_version_id = (SELECT max(id) FROM document_versions)""")
    emb = CountingMock768()
    prep = release.prepare(conn, "PUBLIC", "v2", emb, actor=OP, index_label="x768-v2b")
    assert (prep["reused"], prep["embedded"], emb.calls) == (2, 1, 1)     # 只有新內容送嵌入


def test_prepare_768_refuses_query_side_embedder(conn, indexed768):
    _copy_with_citation_only(conn, indexed768)
    with pytest.raises(ReleaseError):
        release.prepare(conn, "PUBLIC", "v2", Mock768(side="query"), actor=OP, index_label="x768-q")
    assert conn.execute("SELECT count(*) AS n FROM index_versions WHERE label = 'x768-q'").fetchone()["n"] == 0
