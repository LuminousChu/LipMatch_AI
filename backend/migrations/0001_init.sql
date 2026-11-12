-- 0001_init：S2 初始資料結構（PostgreSQL 15+、pgvector 0.6+）
-- 由 app/migrate.py 依檔名順序在單一交易內執行，並把檔案 SHA-256 記在 schema_migrations。
-- 已套用的遷移檔不得修改；要改結構請新增 0002_xxx.sql。
-- 欄位說明見 docs/mvp/schema.md。

CREATE EXTENSION IF NOT EXISTS vector;

-- ===========================================================================
-- 共用：updated_at 自動更新
-- ===========================================================================
CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- ===========================================================================
-- 帳號與登入（只由後端 CLI 預建，無註冊／忘記密碼）
-- ===========================================================================
CREATE TABLE users (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9_.-]{3,64}$'),
    password_hash  TEXT NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    role           TEXT NOT NULL CHECK (role IN ('user', 'admin')),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);
CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 登入工作階段：只存權杖的 SHA-256，資料庫外洩也拿不到可用權杖
CREATE TABLE auth_sessions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_sha256  TEXT NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);
CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id);

-- ===========================================================================
-- 來源文件與版本
-- ===========================================================================
CREATE TABLE documents (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_key               TEXT NOT NULL UNIQUE CHECK (doc_key ~ '^[A-Za-z0-9_-]+$'),  -- = config/documents.json 的 doc_id
    title                 TEXT NOT NULL,
    doc_type              TEXT NOT NULL CHECK (doc_type IN ('academic_paper', 'web_article', 'synthetic', 'other')),
    visibility            TEXT NOT NULL DEFAULT 'admin' CHECK (visibility IN ('all', 'admin')),
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    rag_eligible          BOOLEAN NOT NULL DEFAULT FALSE,
    rag_exclusion_reason  TEXT,
    bibliography          JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(bibliography) = 'object'),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 合成資料（如 sample_makeup_article）永遠不能當證據
    CHECK (doc_type <> 'synthetic' OR NOT rag_eligible),
    CHECK (rag_eligible OR rag_exclusion_reason IS NOT NULL)
);
CREATE TRIGGER documents_updated_at BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 發布後不可覆寫：只允許 draft → published → retired；每份文件同時最多一個 published
CREATE TABLE document_versions (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    version_label    TEXT NOT NULL,
    source_sha256    TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),  -- 原始 PDF/HTML 指紋
    source_filename  TEXT,
    etl_version      TEXT NOT NULL,                                          -- 產生片段的 ETL 程式版本（git SHA）
    status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'retired')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at     TIMESTAMPTZ,
    retired_at       TIMESTAMPTZ,
    UNIQUE (document_id, version_label),
    CHECK ((status = 'draft') = (published_at IS NULL)),
    CHECK ((status = 'retired') = (retired_at IS NOT NULL))
);
CREATE UNIQUE INDEX document_versions_one_published
    ON document_versions (document_id) WHERE status = 'published';

CREATE FUNCTION document_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'document_version % is %, cannot delete', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'draft' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'published' AND NEW.status = 'retired'
       AND (NEW.document_id, NEW.version_label, NEW.source_sha256, NEW.source_filename,
            NEW.etl_version, NEW.created_at, NEW.published_at)
           IS NOT DISTINCT FROM
           (OLD.document_id, OLD.version_label, OLD.source_sha256, OLD.source_filename,
            OLD.etl_version, OLD.created_at, OLD.published_at) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'document_version % is %, content is immutable', OLD.id, OLD.status
        USING ERRCODE = 'check_violation';
END $$;
CREATE TRIGGER document_versions_guard BEFORE UPDATE OR DELETE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION document_versions_guard();

-- ===========================================================================
-- 知識片段：保留頁碼／HTML 定位、引用文字、搜尋文字與人工審核
-- ===========================================================================
CREATE TABLE chunks (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_version_id  BIGINT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    chunk_key            TEXT NOT NULL,              -- ETL 的 block_id，對回 rag_ready JSONL
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    section              TEXT,
    page_start           INTEGER CHECK (page_start >= 1),
    page_end             INTEGER,
    html_locator         TEXT,
    quote_text           TEXT NOT NULL CHECK (length(btrim(quote_text)) > 0),  -- 顯示與引用
    search_text          TEXT NOT NULL,                                         -- 檢索
    content_sha256       TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    review_status        TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'approved', 'rejected')),
    reviewed_by          BIGINT REFERENCES users(id) ON DELETE RESTRICT,
    reviewed_at          TIMESTAMPTZ,
    review_note          TEXT,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    attributes           JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_version_id, chunk_key),
    CHECK (page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)),
    CHECK (page_start IS NOT NULL OR html_locator IS NOT NULL),
    CHECK (review_status = 'pending' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE INDEX chunks_usable_idx ON chunks (document_version_id)
    WHERE review_status = 'approved' AND is_active;

-- 片段守門：
--  1. 只能在 draft 版本新增或刪除片段。
--  2. 非 draft 版本的片段內容不可改；只能停用、或把審核改成 rejected。
--  3. 審核人必須是啟用中的管理者（清洗不等於人工核准）。
CREATE FUNCTION chunks_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM document_versions
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.document_version_id ELSE NEW.document_version_id END;

    IF TG_OP = 'DELETE' THEN
        -- v_status 為 NULL 表示 draft 版本本身被刪除時的連帶刪除
        IF v_status IS NOT NULL AND v_status <> 'draft' THEN
            RAISE EXCEPTION 'chunk % belongs to a % version, cannot delete', OLD.id, v_status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'INSERT' AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'cannot add chunks to a % version', v_status USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.document_version_id <> OLD.document_version_id THEN
            RAISE EXCEPTION 'chunk % cannot move between versions', OLD.id USING ERRCODE = 'check_violation';
        END IF;
        IF v_status <> 'draft' THEN
            IF (NEW.chunk_key, NEW.ordinal, NEW.section, NEW.page_start, NEW.page_end, NEW.html_locator,
                NEW.quote_text, NEW.search_text, NEW.content_sha256, NEW.attributes, NEW.created_at)
               IS DISTINCT FROM
               (OLD.chunk_key, OLD.ordinal, OLD.section, OLD.page_start, OLD.page_end, OLD.html_locator,
                OLD.quote_text, OLD.search_text, OLD.content_sha256, OLD.attributes, OLD.created_at) THEN
                RAISE EXCEPTION 'chunk % belongs to a % version, content is immutable', OLD.id, v_status
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.review_status <> OLD.review_status AND NEW.review_status <> 'rejected' THEN
                RAISE EXCEPTION 'chunk % belongs to a % version, can only be rejected', OLD.id, v_status
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END IF;

    IF NEW.review_status <> 'pending'
       AND (TG_OP = 'INSERT' OR NEW.review_status IS DISTINCT FROM OLD.review_status
            OR NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by) THEN
        IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.reviewed_by AND role = 'admin' AND is_active) THEN
            RAISE EXCEPTION 'reviewer % is not an active admin', NEW.reviewed_by
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunks_guard BEFORE INSERT OR UPDATE OR DELETE ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_guard();

-- ===========================================================================
-- 索引版本與嵌入向量
-- 嵌入維度固定 1024（預定模型 bge-m3）。換成不同維度的模型須新增遷移，不能混存。
-- ===========================================================================
CREATE TABLE index_versions (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label            TEXT NOT NULL UNIQUE,
    embedding_model  TEXT NOT NULL,
    embedding_dim    INTEGER NOT NULL CHECK (embedding_dim = 1024),
    status           TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'active', 'retired')),
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at     TIMESTAMPTZ,
    retired_at       TIMESTAMPTZ,
    CHECK ((status = 'building') = (activated_at IS NULL)),
    CHECK ((status = 'retired') = (retired_at IS NOT NULL))
);
-- 同時只有一個 active 索引；新索引建好才切換
CREATE UNIQUE INDEX index_versions_one_active ON index_versions ((TRUE)) WHERE status = 'active';

CREATE FUNCTION index_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER index_versions_guard BEFORE UPDATE ON index_versions
    FOR EACH ROW EXECUTE FUNCTION index_versions_guard();

-- 這個索引版本由哪些文件版本建成（追溯用）
CREATE TABLE index_version_documents (
    index_version_id     BIGINT NOT NULL REFERENCES index_versions(id) ON DELETE CASCADE,
    document_version_id  BIGINT NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    PRIMARY KEY (index_version_id, document_version_id)
);

CREATE TABLE chunk_embeddings (
    index_version_id  BIGINT NOT NULL REFERENCES index_versions(id) ON DELETE CASCADE,
    chunk_id          BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedding         vector(1024) NOT NULL,
    content_sha256    TEXT NOT NULL,   -- 嵌入當下的片段指紋，須等於 chunks.content_sha256
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_version_id, chunk_id)
);
CREATE INDEX chunk_embeddings_chunk_idx ON chunk_embeddings (chunk_id);
CREATE INDEX chunk_embeddings_hnsw ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);

-- 只有建置中的索引可寫入；只有已核准、啟用、可進 RAG 的片段可被索引
CREATE FUNCTION chunk_embeddings_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (SELECT status FROM index_versions WHERE id = OLD.index_version_id) = 'active' THEN
            RAISE EXCEPTION 'cannot delete embeddings of the active index' USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF (SELECT status FROM index_versions WHERE id = NEW.index_version_id) <> 'building' THEN
        RAISE EXCEPTION 'index_version % is not building', NEW.index_version_id USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM chunks c
          JOIN document_versions dv ON dv.id = c.document_version_id
          JOIN documents d ON d.id = dv.document_id
         WHERE c.id = NEW.chunk_id
           AND c.review_status = 'approved' AND c.is_active
           AND c.content_sha256 = NEW.content_sha256
           AND dv.status IN ('draft', 'published')
           AND d.is_active AND d.rag_eligible
    ) THEN
        RAISE EXCEPTION 'chunk % is not approved/active/eligible or content changed', NEW.chunk_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_embeddings_guard BEFORE INSERT OR UPDATE OR DELETE ON chunk_embeddings
    FOR EACH ROW EXECUTE FUNCTION chunk_embeddings_guard();

-- ===========================================================================
-- 候選色系／妝感方向與其證據
-- ===========================================================================
CREATE TABLE recommendation_options (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    option_key   TEXT NOT NULL UNIQUE CHECK (option_key ~ '^[a-z0-9_-]+$'),
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
    attributes   JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),  -- 色相、明度、質地、情境等
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER recommendation_options_updated_at BEFORE UPDATE ON recommendation_options
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE recommendation_option_evidence (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    option_id     BIGINT NOT NULL REFERENCES recommendation_options(id) ON DELETE CASCADE,
    chunk_id      BIGINT NOT NULL REFERENCES chunks(id) ON DELETE RESTRICT,
    support_type  TEXT NOT NULL CHECK (support_type IN
                    ('research_observation', 'styling_advice', 'product_rule', 'personal_preference')),
    claim         TEXT NOT NULL CHECK (length(btrim(claim)) > 0),
    created_by    BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (option_id, chunk_id, claim)
);
CREATE INDEX recommendation_option_evidence_chunk_idx ON recommendation_option_evidence (chunk_id);

-- ===========================================================================
-- 對話、請求紀錄、檢索命中、推薦輸出與回饋
-- ===========================================================================
CREATE TABLE conversations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            TEXT CHECK (length(title) <= 200),
    scenario         TEXT NOT NULL DEFAULT 'daily' CHECK (scenario IN ('daily', 'office', 'party')),
    scenario_source  TEXT NOT NULL DEFAULT 'default' CHECK (scenario_source IN ('default', 'user')),  -- 預設情境要透明標示
    preferences      JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(preferences) = 'object'),
    allergy_status   TEXT NOT NULL DEFAULT 'unanswered'
                     CHECK (allergy_status IN ('unanswered', 'none_known', 'has_known', 'unsure')),
    allergy_avoid    JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(allergy_avoid) = 'array'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, user_id),
    CHECK (allergy_status IN ('has_known', 'unsure') OR allergy_avoid = '[]'::jsonb)
);
CREATE INDEX conversations_user_idx ON conversations (user_id, updated_at DESC);
CREATE TRIGGER conversations_updated_at BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 一次推薦／問答請求。id 即 API 回傳的 request_id；請求開始就寫入（status = started）。
-- token 欄位 NULL = 未取得（未知），不可用 0 代替。
CREATE TABLE requests (
    id                    UUID PRIMARY KEY,
    user_id               BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id       UUID NOT NULL,
    question              TEXT NOT NULL,
    preferences_snapshot  JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(preferences_snapshot) = 'object'),
    status                TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'succeeded', 'failed')),
    index_version_id      BIGINT REFERENCES index_versions(id) ON DELETE RESTRICT,
    model_provider        TEXT,
    model_name            TEXT,
    prompt_version        TEXT,
    app_version           TEXT,
    model_output          TEXT,
    answer                TEXT,
    input_tokens          INTEGER CHECK (input_tokens >= 0),
    output_tokens         INTEGER CHECK (output_tokens >= 0),
    latency_ms            INTEGER CHECK (latency_ms >= 0),
    retry_count           INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    error_code            TEXT,
    error_detail          JSONB,
    detail                JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ,
    UNIQUE (id, user_id),
    -- 請求只能掛在自己的對話底下
    FOREIGN KEY (conversation_id, user_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE,
    CHECK ((status = 'started') = (completed_at IS NULL)),
    CHECK (status <> 'failed' OR error_code IS NOT NULL),
    CHECK (completed_at IS NULL OR completed_at >= started_at)
);
CREATE INDEX requests_conversation_idx ON requests (conversation_id, started_at);
CREATE INDEX requests_user_idx ON requests (user_id, started_at DESC);
CREATE INDEX requests_started_idx ON requests (started_at DESC);

CREATE TABLE retrieval_hits (
    request_id     UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    retriever      TEXT NOT NULL CHECK (retriever IN ('bm25', 'vector', 'hybrid')),
    rank           INTEGER NOT NULL CHECK (rank >= 1),
    chunk_id       BIGINT NOT NULL REFERENCES chunks(id) ON DELETE RESTRICT,
    score          DOUBLE PRECISION,
    passed_filter  BOOLEAN NOT NULL,      -- 是否通過核准／啟用／可見性檢查
    filter_reason  TEXT,
    detail         JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
    PRIMARY KEY (request_id, retriever, rank),
    UNIQUE (request_id, retriever, chunk_id),
    CHECK (passed_filter OR filter_reason IS NOT NULL)
);
CREATE INDEX retrieval_hits_chunk_idx ON retrieval_hits (chunk_id);

CREATE TABLE request_recommendations (
    request_id  UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 3),
    option_id   BIGINT NOT NULL REFERENCES recommendation_options(id) ON DELETE RESTRICT,
    reason      TEXT NOT NULL,
    PRIMARY KEY (request_id, rank),
    UNIQUE (request_id, option_id)
);

-- 推薦理由引用的片段；片段必須是該請求實際召回並通過檢查的
CREATE TABLE request_citations (
    request_id  UUID NOT NULL,
    rank        INTEGER NOT NULL,
    chunk_id    BIGINT NOT NULL REFERENCES chunks(id) ON DELETE RESTRICT,
    PRIMARY KEY (request_id, rank, chunk_id),
    FOREIGN KEY (request_id, rank) REFERENCES request_recommendations(request_id, rank) ON DELETE CASCADE
);

CREATE FUNCTION request_citations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM retrieval_hits
                    WHERE request_id = NEW.request_id AND chunk_id = NEW.chunk_id AND passed_filter) THEN
        RAISE EXCEPTION 'chunk % was not a filtered retrieval hit of request %', NEW.chunk_id, NEW.request_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER request_citations_guard BEFORE INSERT OR UPDATE ON request_citations
    FOR EACH ROW EXECUTE FUNCTION request_citations_guard();

-- 回饋只能針對自己的請求（複合外鍵保證）
CREATE TABLE feedback (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id    UUID NOT NULL,
    user_id       BIGINT NOT NULL,
    rating        SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    option_id     BIGINT REFERENCES recommendation_options(id) ON DELETE RESTRICT,
    reason_codes  JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(reason_codes) = 'array'),
    comment       TEXT CHECK (length(comment) <= 2000),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (request_id, user_id) REFERENCES requests(id, user_id) ON DELETE CASCADE,
    UNIQUE (request_id, user_id)
);
CREATE INDEX feedback_user_idx ON feedback (user_id);
CREATE TRIGGER feedback_updated_at BEFORE UPDATE ON feedback
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
