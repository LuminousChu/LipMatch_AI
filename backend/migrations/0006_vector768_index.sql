-- 0006_vector768_index：768 維索引版本（xVITA-Embed-300M-zhTW 等），與既有 1024 維結構分表並存
-- 已套用的遷移檔不得修改；要改結構請新增下一號。欄位說明見 docs/mvp/schema.md。
-- 遷移號 0006 由協調者（E）於 2026-10-23 分配給修復 D（decisions.json E-002）。
--
-- 原則：
-- - 不改 0001～0005，也不改既有 1024 維的 chunk_embeddings 欄位、HNSW 索引與 searchable_chunks；
--   768 維向量另存 chunk_embeddings_v768，不補零、不和 1024 維混存。
-- - 768 維索引版本必須帶「嵌入指紋」（模型檔 SHA-256、文件前綴、查詢前綴）：同模型名但模型檔或前綴不同的向量，
--   在程式（retrieval.check_compatible）與資料庫（不可變）兩層都視為不同索引，不混用。
-- - 每筆 768 維向量記下實際嵌入輸入（文件前綴＋search_text）的 SHA-256，可追溯、可判斷快取能否重用。

-- ===========================================================================
-- 1. 索引版本：允許 768 維；768 維必須有完整嵌入指紋
-- ===========================================================================
ALTER TABLE index_versions DROP CONSTRAINT index_versions_embedding_dim_check;
ALTER TABLE index_versions
    ADD CONSTRAINT index_versions_embedding_dim_check CHECK (embedding_dim IN (768, 1024)),
    ADD COLUMN embedding_fingerprint JSONB
        CHECK (embedding_fingerprint IS NULL OR jsonb_typeof(embedding_fingerprint) = 'object'),
    ADD CONSTRAINT index_versions_768_fingerprint CHECK (
        embedding_dim <> 768 OR (
            embedding_fingerprint IS NOT NULL
            AND length(coalesce(embedding_fingerprint ->> 'model_sha256', '')) = 64
            AND embedding_fingerprint ? 'doc_prefix'
            AND embedding_fingerprint ? 'query_prefix'));

-- 守門：非 building 的索引版本，指紋與其他內容欄位一樣不可改（其餘規則同 0001）
CREATE OR REPLACE FUNCTION index_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'building' THEN
        IF (NEW.label, NEW.embedding_model, NEW.embedding_dim, NEW.embedding_fingerprint,
            NEW.created_at, NEW.activated_at)
           IS DISTINCT FROM (OLD.label, OLD.embedding_model, OLD.embedding_dim, OLD.embedding_fingerprint,
                             OLD.created_at, OLD.activated_at)
           OR NOT (NEW.status = OLD.status OR (OLD.status = 'active' AND NEW.status = 'retired')) THEN
            RAISE EXCEPTION 'index_version % is %, cannot modify', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

-- ===========================================================================
-- 2. 既有 1024 維表：只接受 1024 維索引版本的向量（新觸發器；表、欄位與 HNSW 索引不動）
--    欄位型別 vector(1024) 已擋 768 維向量；這裡再擋「768 維索引版本卻寫進 1024 表」。
-- ===========================================================================
CREATE FUNCTION chunk_embeddings_dim_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT embedding_dim FROM index_versions WHERE id = NEW.index_version_id) <> 1024 THEN
        RAISE EXCEPTION 'index_version % is not a 1024-dim index', NEW.index_version_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_embeddings_dim_guard BEFORE INSERT OR UPDATE ON chunk_embeddings
    FOR EACH ROW EXECUTE FUNCTION chunk_embeddings_dim_guard();

-- ===========================================================================
-- 3. 768 維向量表
-- ===========================================================================
CREATE TABLE chunk_embeddings_v768 (
    index_version_id  BIGINT NOT NULL REFERENCES index_versions(id) ON DELETE CASCADE,
    chunk_id          BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedding         vector(768) NOT NULL,
    content_sha256    TEXT NOT NULL,   -- 嵌入當下的片段指紋，須等於 chunks.content_sha256
    input_sha256      TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),  -- 實際嵌入輸入（文件前綴＋search_text）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_version_id, chunk_id)
);
CREATE INDEX chunk_embeddings_v768_chunk_idx ON chunk_embeddings_v768 (chunk_id);
CREATE INDEX chunk_embeddings_v768_hnsw ON chunk_embeddings_v768 USING hnsw (embedding vector_cosine_ops);

-- 規則同 0005 的 chunk_embeddings_guard，另加：索引版本必須是 768 維；
-- input_sha256 必須等於「索引指紋的文件前綴＋片段 search_text」的 SHA-256（擋住用別的文字或前綴算出的向量）。
CREATE FUNCTION chunk_embeddings_v768_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_index index_versions%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (SELECT status FROM index_versions WHERE id = OLD.index_version_id) = 'active' THEN
            RAISE EXCEPTION 'cannot delete embeddings of the active index' USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    SELECT * INTO v_index FROM index_versions WHERE id = NEW.index_version_id;
    IF v_index.status <> 'building' THEN
        RAISE EXCEPTION 'index_version % is not building', NEW.index_version_id USING ERRCODE = 'check_violation';
    END IF;
    IF v_index.embedding_dim <> 768 THEN
        RAISE EXCEPTION 'index_version % is not a 768-dim index', NEW.index_version_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM chunks c
          JOIN document_versions dv ON dv.id = c.document_version_id
          JOIN documents d ON d.id = dv.document_id
         WHERE c.id = NEW.chunk_id
           AND c.review_status = 'approved' AND c.is_active
           AND c.content_sha256 = NEW.content_sha256
           AND dv.status IN ('draft', 'published', 'retired')
           AND d.is_active AND d.rag_eligible
           AND NEW.input_sha256 = encode(sha256(convert_to(
                   (v_index.embedding_fingerprint ->> 'doc_prefix') || c.search_text, 'UTF8')), 'hex')
    ) THEN
        RAISE EXCEPTION 'chunk % is not approved/active/eligible, content changed, or input hash mismatch',
            NEW.chunk_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_embeddings_v768_guard BEFORE INSERT OR UPDATE OR DELETE ON chunk_embeddings_v768
    FOR EACH ROW EXECUTE FUNCTION chunk_embeddings_v768_guard();

-- ===========================================================================
-- 4. 768 維可檢索片段：同 searchable_chunks 的條件（可用片段 ∩ active 索引中內容指紋相符的向量）
--    角色過濾（visibility）仍由應用層依登入身分加上。
-- ===========================================================================
CREATE VIEW searchable_chunks_v768 AS
SELECT u.*, ce.index_version_id, iv.label AS index_label, iv.embedding_model, iv.embedding_dim,
       iv.embedding_fingerprint, ce.input_sha256, ce.embedding
  FROM usable_chunks u
  JOIN chunk_embeddings_v768 ce ON ce.chunk_id = u.chunk_id AND ce.content_sha256 = u.content_sha256
  JOIN index_versions iv ON iv.id = ce.index_version_id AND iv.status = 'active' AND iv.embedding_dim = 768;
