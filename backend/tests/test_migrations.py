"""遷移與資料庫層規則：在全新測試資料庫上驗證結構、版本不可覆寫、審核、嵌入維度與所有權外鍵。"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid

import psycopg
import pytest

from app.cli import create_user
from app.migrate import MIGRATIONS_DIR, MigrationError, migrate

from .conftest import PASSWORD

ENTITIES = {
    "users", "documents", "document_versions", "chunks", "chunk_embeddings",
    "recommendation_options", "conversations", "requests", "retrieval_hits", "feedback",
}
SUPPORT = {
    "schema_migrations", "auth_sessions", "index_versions", "index_version_documents",
    "recommendation_option_evidence", "request_recommendations", "request_citations",
    "auth_login_attempts",
    "chunk_review_events",
}
SHA = hashlib.sha256(b"x").hexdigest()


def _vec(value: float = 0.1, dim: int = 1024) -> str:
    return "[" + ",".join([str(value)] * dim) + "]"


def test_fresh_database_migrates(empty_db):
    assert migrate(empty_db) == [p.name for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    with psycopg.connect(empty_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert ENTITIES | SUPPORT <= tables
        assert conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        (dim,) = conn.execute(
            """SELECT atttypmod FROM pg_attribute
                WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding'""").fetchone()
        assert dim == 1024
    # 再跑一次不重複套用
    assert migrate(empty_db) == []


def test_modified_migration_is_rejected(empty_db, tmp_path):
    migrate(empty_db)
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    target = copy / "0001_init.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="checksum"):
        migrate(empty_db, copy)


def test_failed_migration_rolls_back(empty_db, tmp_path):
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    (copy / "0099_broken.sql").write_text("CREATE TABLE half_done (id int);\nSELECT 1/0;", encoding="utf-8")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(empty_db, copy)
    with psycopg.connect(empty_db) as conn:
        assert conn.execute("SELECT to_regclass('half_done')").fetchone()[0] is None
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    # 正式遷移全部套用，壞掉的 0099 完全沒有留下
    assert sorted(versions) == [p.name[:4] for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


# ---------------------------------------------------------------------------
# 資料庫層規則
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(db):
    with psycopg.connect(db, autocommit=True) as c:
        yield c


@pytest.fixture
def admin_id(db):
    return create_user(db, "reviewer", "admin", PASSWORD)


def _doc(conn, key="TH013", eligible=True):
    return conn.execute(
        """INSERT INTO documents (doc_key, title, doc_type, rag_eligible, rag_exclusion_reason)
           VALUES (%s, 't', 'academic_paper', %s, %s) RETURNING id""",
        (key, eligible, None if eligible else "test"),
    ).fetchone()[0]


def _version(conn, doc_id, label="v1"):
    return conn.execute(
        """INSERT INTO document_versions (document_id, version_label, source_sha256, etl_version)
           VALUES (%s, %s, %s, 'abc123') RETURNING id""",
        (doc_id, label, SHA),
    ).fetchone()[0]


def _chunk(conn, version_id, key="p1:b1"):
    return conn.execute(
        """INSERT INTO chunks (document_version_id, chunk_key, ordinal, page_start, quote_text,
                               search_text, content_sha256)
           VALUES (%s, %s, 0, 1, 'quote', 'search', %s) RETURNING id""",
        (version_id, key, SHA),
    ).fetchone()[0]


def _publish(conn, version_id):
    conn.execute("UPDATE document_versions SET status = 'published', published_at = now() WHERE id = %s",
                 (version_id,))


def test_published_version_is_immutable(conn, admin_id):
    doc = _doc(conn)
    v1 = _version(conn, doc)
    chunk = _chunk(conn, v1)
    _publish(conn, v1)

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE document_versions SET etl_version = 'changed' WHERE id = %s", (v1,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE chunks SET quote_text = 'changed' WHERE id = %s", (chunk,))
    with pytest.raises(psycopg.errors.CheckViolation):
        _chunk(conn, v1, key="p2:b1")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM document_versions WHERE id = %s", (v1,))
    # 停用片段可以
    conn.execute("UPDATE chunks SET is_active = FALSE WHERE id = %s", (chunk,))


def test_only_one_published_version_and_switch_in_one_transaction(conn):
    doc = _doc(conn)
    v1, v2 = _version(conn, doc, "v1"), _version(conn, doc, "v2")
    _publish(conn, v1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _publish(conn, v2)
    with conn.transaction():
        conn.execute("UPDATE document_versions SET status = 'retired', retired_at = now() WHERE id = %s", (v1,))
        _publish(conn, v2)
    with pytest.raises(psycopg.errors.CheckViolation):  # 退役後不可再發布
        conn.execute("UPDATE document_versions SET status = 'published', retired_at = NULL WHERE id = %s", (v1,))


def test_chunk_approval_requires_active_admin(conn, db, admin_id):
    plain = create_user(db, "plainuser", "user", PASSWORD)
    chunk = _chunk(conn, _version(conn, _doc(conn)))
    with pytest.raises(psycopg.errors.CheckViolation):  # 沒有審核人
        conn.execute("UPDATE chunks SET review_status = 'approved' WHERE id = %s", (chunk,))
    with pytest.raises(psycopg.errors.CheckViolation):  # 一般使用者不能核准
        conn.execute("UPDATE chunks SET review_status = 'approved', reviewed_by = %s, reviewed_at = now() "
                     "WHERE id = %s", (plain, chunk))
    conn.execute("UPDATE chunks SET review_status = 'approved', reviewed_by = %s, reviewed_at = now() "
                 "WHERE id = %s", (admin_id, chunk))


def test_synthetic_document_can_never_be_rag_eligible(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("""INSERT INTO documents (doc_key, title, doc_type, rag_eligible)
                        VALUES ('sample_makeup_article', 't', 'synthetic', TRUE)""")


def test_embedding_rules(conn, admin_id):
    chunk = _chunk(conn, _version(conn, _doc(conn)))
    idx = conn.execute("""INSERT INTO index_versions (label, embedding_model, embedding_dim)
                          VALUES ('idx1', 'bge-m3', 1024) RETURNING id""").fetchone()[0]
    insert = "INSERT INTO chunk_embeddings (index_version_id, chunk_id, embedding, content_sha256) VALUES (%s,%s,%s,%s)"

    with pytest.raises(psycopg.errors.CheckViolation):  # 未核准片段不能被索引
        conn.execute(insert, (idx, chunk, _vec(), SHA))
    conn.execute("UPDATE chunks SET review_status = 'approved', reviewed_by = %s, reviewed_at = now() "
                 "WHERE id = %s", (admin_id, chunk))
    with pytest.raises(psycopg.errors.DataException):  # 維度與模型設定不符
        conn.execute(insert, (idx, chunk, _vec(dim=768), SHA))
    with pytest.raises(psycopg.errors.CheckViolation):  # 片段內容指紋不符
        conn.execute(insert, (idx, chunk, _vec(), "0" * 64))
    conn.execute(insert, (idx, chunk, _vec(), SHA))

    conn.execute("UPDATE index_versions SET status = 'active', activated_at = now() WHERE id = %s", (idx,))
    with pytest.raises(psycopg.errors.CheckViolation):  # 啟用後不可再寫入
        conn.execute("UPDATE chunk_embeddings SET embedding = %s WHERE index_version_id = %s", (_vec(0.2), idx))
    # 0006 起允許 768 維，但必須帶完整嵌入指紋；其他維度一律拒絕
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("""INSERT INTO index_versions (label, embedding_model, embedding_dim)
                        VALUES ('idx768', 'other', 768)""")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("""INSERT INTO index_versions (label, embedding_model, embedding_dim)
                        VALUES ('idx512', 'other', 512)""")


# ---------------------------------------------------------------------------
# 0006：768 維索引分表（修復 D）
# ---------------------------------------------------------------------------
FP = {"model": "MOCK-xvita", "model_sha256": "a" * 64, "dim": 768, "doc_prefix": "title: none | text: ",
      "query_prefix": "task: search result | query: "}


def _input_sha(text: str, prefix: str = FP["doc_prefix"]) -> str:
    return hashlib.sha256((prefix + text).encode("utf-8")).hexdigest()


def test_vector768_rules(conn, admin_id):
    chunk = _chunk(conn, _version(conn, _doc(conn)))
    conn.execute("UPDATE chunks SET review_status = 'approved', reviewed_by = %s, reviewed_at = now() "
                 "WHERE id = %s", (admin_id, chunk))
    idx768 = conn.execute("""INSERT INTO index_versions (label, embedding_model, embedding_dim, embedding_fingerprint)
                             VALUES ('x768', 'MOCK-xvita', 768, %s) RETURNING id""",
                          (json.dumps(FP),)).fetchone()[0]
    idx1024 = conn.execute("""INSERT INTO index_versions (label, embedding_model, embedding_dim)
                              VALUES ('b1024', 'bge-m3', 1024) RETURNING id""").fetchone()[0]
    ins768 = """INSERT INTO chunk_embeddings_v768 (index_version_id, chunk_id, embedding, content_sha256, input_sha256)
                VALUES (%s,%s,%s,%s,%s)"""
    ins1024 = "INSERT INTO chunk_embeddings (index_version_id, chunk_id, embedding, content_sha256) VALUES (%s,%s,%s,%s)"

    with pytest.raises(psycopg.errors.DataException):       # 768 表不收 1024 維（不補零、不截斷）
        conn.execute(ins768, (idx768, chunk, _vec(dim=1024), SHA, _input_sha("search")))
    with pytest.raises(psycopg.errors.CheckViolation):      # 768 維向量寫進 1024 維索引版本
        conn.execute(ins768, (idx1024, chunk, _vec(dim=768), SHA, _input_sha("search")))
    with pytest.raises(psycopg.errors.CheckViolation):      # 1024 表不收 768 維索引版本的資料
        conn.execute(ins1024, (idx768, chunk, _vec(), SHA))
    with pytest.raises(psycopg.errors.CheckViolation):      # 嵌入輸入不是「文件前綴＋search_text」
        conn.execute(ins768, (idx768, chunk, _vec(dim=768), SHA, _input_sha("search", FP["query_prefix"])))
    with pytest.raises(psycopg.errors.CheckViolation):      # 內容指紋不符
        conn.execute(ins768, (idx768, chunk, _vec(dim=768), "0" * 64, _input_sha("search")))
    conn.execute(ins768, (idx768, chunk, _vec(dim=768), SHA, _input_sha("search")))
    conn.execute(ins1024, (idx1024, chunk, _vec(), SHA))      # 既有 1024 路徑照舊可用

    conn.execute("UPDATE index_versions SET status = 'active', activated_at = now() WHERE id = %s", (idx768,))
    with pytest.raises(psycopg.errors.CheckViolation):      # 啟用後指紋不可改
        conn.execute("UPDATE index_versions SET embedding_fingerprint = '{}' WHERE id = %s", (idx768,))
    with pytest.raises(psycopg.errors.CheckViolation):      # 啟用後不可再寫入／刪除
        conn.execute("DELETE FROM chunk_embeddings_v768 WHERE index_version_id = %s", (idx768,))
    hnsw = conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'chunk_embeddings_v768_hnsw'").fetchone()
    assert "hnsw" in hnsw[0] and "vector_cosine_ops" in hnsw[0]


# 結構快照：表欄位與型別、限制、索引、觸發器、函式本文、view 定義（還原前後比對用）
def _catalog(conn) -> dict:
    q = {
        "columns": """SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v') AND a.attnum > 0
                         AND NOT a.attisdropped ORDER BY 1, 2""",
        "constraints": """SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
                           WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2""",
        "indexes": "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1",
        "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2",
        "functions": """SELECT proname, md5(prosrc) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                         ORDER BY 1""",
        "views": """SELECT viewname, md5(definition) FROM pg_views WHERE schemaname = 'public' ORDER BY 1""",
    }
    return {k: conn.execute(v).fetchall() for k, v in q.items()}


# 0006 的還原 SQL（隔離庫遷移失敗或要撤回時用；正式還原另以遷移前的 pg_dump 為準）。
ROLLBACK_0006 = """
DROP VIEW searchable_chunks_v768;
DROP TABLE chunk_embeddings_v768;
DROP FUNCTION chunk_embeddings_v768_guard();
DROP TRIGGER chunk_embeddings_dim_guard ON chunk_embeddings;
DROP FUNCTION chunk_embeddings_dim_guard();
CREATE OR REPLACE FUNCTION index_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'building' THEN
        IF (NEW.label, NEW.embedding_model, NEW.embedding_dim, NEW.created_at, NEW.activated_at)
           IS DISTINCT FROM (OLD.label, OLD.embedding_model, OLD.embedding_dim, OLD.created_at, OLD.activated_at)
           OR NOT (NEW.status = OLD.status OR (OLD.status = 'active' AND NEW.status = 'retired')) THEN
            RAISE EXCEPTION 'index_version % is %, cannot modify', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
ALTER TABLE index_versions DROP CONSTRAINT index_versions_768_fingerprint;
ALTER TABLE index_versions DROP COLUMN embedding_fingerprint;
ALTER TABLE index_versions DROP CONSTRAINT index_versions_embedding_dim_check;
ALTER TABLE index_versions ADD CONSTRAINT index_versions_embedding_dim_check CHECK (embedding_dim = 1024);
DELETE FROM schema_migrations WHERE version = '0006';
"""


def test_0006_keeps_1024_structure_and_rolls_back(empty_db, tmp_path):
    # 0007（2026-11-07）之後：只驗 0006 本身，所以「套用後」用只到 0006 的資料夾，不連後面的遷移一起套
    before_dir, upto6 = tmp_path / "upto0005", tmp_path / "upto0006"
    before_dir.mkdir()
    upto6.mkdir()
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if p.name < "0006":
            shutil.copy(p, before_dir / p.name)
        if p.name < "0007":
            shutil.copy(p, upto6 / p.name)
    migrate(empty_db, before_dir)
    with psycopg.connect(empty_db, autocommit=True) as c:
        before = _catalog(c)
    assert migrate(empty_db, upto6) == ["0006_vector768_index.sql"]
    with psycopg.connect(empty_db, autocommit=True) as c:
        after = _catalog(c)
        # 既有 1024 維結構完全不變：chunk_embeddings 欄位、HNSW 索引、searchable_chunks view 定義
        for key, pick in (("columns", lambda r: r[0] in ("chunk_embeddings", "searchable_chunks")),
                          ("indexes", lambda r: r[0].startswith("chunk_embeddings_") and "v768" not in r[0]),
                          ("views", lambda r: r[0] in ("searchable_chunks", "usable_chunks"))):
            assert [r for r in before[key] if pick(r)] == [r for r in after[key] if pick(r)]
        assert ("chunk_embeddings_v768", "embedding", "vector(768)", True) in after["columns"]
        with c.transaction():
            c.execute(ROLLBACK_0006)
        assert _catalog(c) == before
    assert migrate(empty_db, upto6) == ["0006_vector768_index.sql"]   # 還原後可重新套用
    assert migrate(empty_db) == ["0007_conversation_hidden.sql"]       # 之後的遷移照常接上


def test_request_and_feedback_ownership_enforced_by_foreign_keys(conn, db):
    alice = create_user(db, "alice", "user", PASSWORD)
    bob = create_user(db, "bob", "user", PASSWORD)
    conv = conn.execute("INSERT INTO conversations (user_id) VALUES (%s) RETURNING id", (alice,)).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # bob 的請求掛在 alice 的對話
        conn.execute("INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s,%s,%s,'q')",
                     (uuid.uuid4(), bob, conv))
    rid = uuid.uuid4()
    conn.execute("INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s,%s,%s,'q')",
                 (rid, alice, conv))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # bob 對 alice 的請求回饋
        conn.execute("INSERT INTO feedback (request_id, user_id, rating) VALUES (%s,%s,1)", (rid, bob))
    # token 未取得時是 NULL（未知），不是 0
    tokens = conn.execute("SELECT input_tokens, output_tokens FROM requests WHERE id = %s", (rid,)).fetchone()
    assert tokens == (None, None)


def test_citation_must_be_a_filtered_retrieval_hit(conn, admin_id, db):
    alice = create_user(db, "alice", "user", PASSWORD)
    chunk = _chunk(conn, _version(conn, _doc(conn)))
    opt = conn.execute("""INSERT INTO recommendation_options (option_key, name, description)
                          VALUES ('soft-rose', '柔霧玫瑰', 'd') RETURNING id""").fetchone()[0]
    conv = conn.execute("INSERT INTO conversations (user_id) VALUES (%s) RETURNING id", (alice,)).fetchone()[0]
    rid = uuid.uuid4()
    conn.execute("INSERT INTO requests (id, user_id, conversation_id, question) VALUES (%s,%s,%s,'q')",
                 (rid, alice, conv))
    conn.execute("INSERT INTO request_recommendations (request_id, rank, option_id, reason) VALUES (%s,1,%s,'r')",
                 (rid, opt))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO request_citations VALUES (%s, 1, %s)", (rid, chunk))
    conn.execute("""INSERT INTO retrieval_hits (request_id, retriever, rank, chunk_id, passed_filter, filter_reason)
                    VALUES (%s, 'hybrid', 1, %s, FALSE, 'not_approved')""", (rid, chunk))
    with pytest.raises(psycopg.errors.CheckViolation):  # 被過濾掉的片段也不能引用
        conn.execute("INSERT INTO request_citations VALUES (%s, 1, %s)", (rid, chunk))
