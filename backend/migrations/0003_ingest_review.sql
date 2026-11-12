-- 0003_ingest_review：S3 資料接入與人工審核
-- 已套用的遷移檔不得修改；要改結構請新增下一號。欄位說明見 docs/mvp/schema.md。
-- 編號說明：0002 由認證（DEC-10）使用；本檔與 0002 無共同物件，兩者套用順序互不影響。

-- ===========================================================================
-- 1. 匯入指紋：同內容重複匯入不建新版本
-- ===========================================================================
ALTER TABLE document_versions
    ADD COLUMN import_sha256 TEXT CHECK (import_sha256 ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX document_versions_import_sha256_key
    ON document_versions (document_id, import_sha256) WHERE import_sha256 IS NOT NULL;

-- 發布後不可覆寫：比對欄位加入 import_sha256（其餘規則同 0001）
CREATE OR REPLACE FUNCTION document_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
            NEW.etl_version, NEW.created_at, NEW.published_at, NEW.import_sha256)
           IS NOT DISTINCT FROM
           (OLD.document_id, OLD.version_label, OLD.source_sha256, OLD.source_filename,
            OLD.etl_version, OLD.created_at, OLD.published_at, OLD.import_sha256) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'document_version % is %, content is immutable', OLD.id, OLD.status
        USING ERRCODE = 'check_violation';
END $$;

-- ===========================================================================
-- 2. 片段守門：已發布版本允許「繼續審核」，內容仍不可改
--    draft     ：任何審核狀態轉換
--    published ：pending → approved／rejected、approved → rejected（不可把 rejected 再放行）
--    retired   ：審核狀態不可再變（只能停用）
--    原因：人工審核是分批進行；發布版的「內容」不可覆寫，審核結果是附加的稽核紀錄
--    （每次變更都寫入 chunk_review_events）。
-- ===========================================================================
CREATE OR REPLACE FUNCTION chunks_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM document_versions
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.document_version_id ELSE NEW.document_version_id END;

    IF TG_OP = 'DELETE' THEN
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
            IF NEW.review_status <> OLD.review_status
               AND (v_status = 'retired'
                    OR NOT ((OLD.review_status = 'pending' AND NEW.review_status IN ('approved', 'rejected'))
                            OR (OLD.review_status = 'approved' AND NEW.review_status = 'rejected'))) THEN
                RAISE EXCEPTION 'chunk % in a % version cannot go from % to %',
                    OLD.id, v_status, OLD.review_status, NEW.review_status
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

-- ===========================================================================
-- 3. 審核歷程：只能新增，不能改或刪
-- ===========================================================================
CREATE TABLE chunk_review_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chunk_id        BIGINT NOT NULL REFERENCES chunks(id) ON DELETE RESTRICT,
    reviewer_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    from_status     TEXT NOT NULL CHECK (from_status IN ('pending', 'approved', 'rejected')),
    to_status       TEXT NOT NULL CHECK (to_status IN ('approved', 'rejected')),
    reason          TEXT NOT NULL CHECK (length(btrim(reason)) >= 2 AND length(reason) <= 2000),
    content_sha256  TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),  -- 審核當下看到的片段內容
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_status <> to_status)
);
CREATE INDEX chunk_review_events_chunk_idx ON chunk_review_events (chunk_id, created_at);

CREATE FUNCTION chunk_review_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'chunk_review_events is append-only' USING ERRCODE = 'check_violation';
END $$;
CREATE TRIGGER chunk_review_events_append_only BEFORE UPDATE OR DELETE ON chunk_review_events
    FOR EACH ROW EXECUTE FUNCTION chunk_review_events_append_only();

-- 審核唯一入口：同一交易內更新片段並寫歷程。
-- 理由必填；p_expected_sha 必須等於片段目前內容（避免核准到審核清單匯出後才變動的內容）。
-- 審核人必須是啟用中的管理者（由 chunks_guard 檢查）。回傳新狀態，或 'unchanged'。
CREATE FUNCTION review_chunk(p_chunk_id BIGINT, p_reviewer_id BIGINT, p_decision TEXT,
                             p_reason TEXT, p_expected_sha TEXT)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    v_old TEXT;
    v_sha TEXT;
    v_new TEXT;
BEGIN
    IF p_decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'decision must be approve or reject, got %', p_decision USING ERRCODE = 'check_violation';
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) < 2 THEN
        RAISE EXCEPTION 'review reason is required' USING ERRCODE = 'check_violation';
    END IF;
    SELECT review_status, content_sha256 INTO v_old, v_sha FROM chunks WHERE id = p_chunk_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'chunk % not found', p_chunk_id USING ERRCODE = 'no_data_found';
    END IF;
    IF v_sha IS DISTINCT FROM p_expected_sha THEN
        RAISE EXCEPTION 'chunk % content changed since it was listed for review', p_chunk_id
            USING ERRCODE = 'check_violation';
    END IF;
    v_new := CASE p_decision WHEN 'approve' THEN 'approved' ELSE 'rejected' END;
    IF v_old = v_new THEN
        RETURN 'unchanged';
    END IF;
    UPDATE chunks
       SET review_status = v_new, reviewed_by = p_reviewer_id, reviewed_at = now(), review_note = p_reason
     WHERE id = p_chunk_id;
    INSERT INTO chunk_review_events (chunk_id, reviewer_id, from_status, to_status, reason, content_sha256)
    VALUES (p_chunk_id, p_reviewer_id, v_old, v_new, p_reason, v_sha);
    RETURN v_new;
END $$;

-- ===========================================================================
-- 4. 可用片段：檢索唯一可用的來源（S4 起使用；還要再依角色過濾 visibility）
-- ===========================================================================
CREATE VIEW usable_chunks AS
SELECT c.id AS chunk_id, c.chunk_key, c.ordinal, c.section, c.page_start, c.page_end, c.html_locator,
       c.quote_text, c.search_text, c.content_sha256, c.attributes,
       dv.id AS document_version_id, dv.version_label,
       d.id AS document_id, d.doc_key, d.title, d.visibility, d.bibliography
  FROM chunks c
  JOIN document_versions dv ON dv.id = c.document_version_id
  JOIN documents d ON d.id = dv.document_id
 WHERE c.review_status = 'approved'
   AND c.is_active
   AND dv.status = 'published'
   AND d.is_active
   AND d.rag_eligible;
