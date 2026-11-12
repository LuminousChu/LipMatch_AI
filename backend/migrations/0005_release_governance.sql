-- 0005_release_governance：S9 版本發布、停用與安全切回舊版
-- 已套用的遷移檔不得修改；要改結構請新增下一號。欄位說明見 docs/mvp/schema.md。
-- 遷移號 0005 於 2026-10-16 分配給 S9。

-- ===========================================================================
-- 1. 發布／停用事件：只能新增，不能改或刪（稽核用）
--    每一次發布、切回舊版、準備向量、停用／恢復、可見範圍與有效期間變更，以及被拒絕的嘗試，
--    都寫一筆；被拒絕的嘗試（event_type = refused）也保留，方便追查「為什麼舊版仍在線上」。
-- ===========================================================================
CREATE TABLE document_release_events (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    event_type       TEXT NOT NULL CHECK (event_type IN
                       ('prepare', 'publish', 'rollback', 'refused',
                        'deactivate', 'reactivate', 'visibility', 'validity')),
    from_version_id  BIGINT REFERENCES document_versions(id) ON DELETE RESTRICT,  -- 切換前的 published
    to_version_id    BIGINT REFERENCES document_versions(id) ON DELETE RESTRICT,  -- 切換後的 published
    index_before_id  BIGINT REFERENCES index_versions(id) ON DELETE RESTRICT,     -- 切換前的 active 索引
    index_after_id   BIGINT REFERENCES index_versions(id) ON DELETE RESTRICT,     -- 切換後的 active（或準備好的 building）索引
    actor            TEXT NOT NULL CHECK (length(btrim(actor)) >= 1 AND length(actor) <= 200),
    reason           TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
    detail           JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX document_release_events_doc_idx ON document_release_events (document_id, created_at);

CREATE FUNCTION document_release_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'document_release_events is append-only' USING ERRCODE = 'check_violation';
END $$;
CREATE TRIGGER document_release_events_append_only BEFORE UPDATE OR DELETE ON document_release_events
    FOR EACH ROW EXECUTE FUNCTION document_release_events_append_only();

-- ===========================================================================
-- 2. 版本守門：新增「受控切回舊版」retired → published
--    只有在同一交易內設定 lipmatch.version_rollback = 'on'（app/release.py 的 rollback 才會設定）時允許；
--    內容欄位一律不可變，只更新 published_at（重新上線時間）並清空 retired_at。
--    歷次上下線的時間與操作人保存在 document_release_events，不靠覆寫版本列。
--    注意：這是防止「誤用一般 UPDATE 切回舊版」的守門，不是權限邊界（能連資料庫的人本來就受信任）。
-- ===========================================================================
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
    -- 內容欄位（含 published_at 以外的時間）在發布後一律不可變
    IF (NEW.document_id, NEW.version_label, NEW.source_sha256, NEW.source_filename,
        NEW.etl_version, NEW.created_at, NEW.import_sha256)
       IS DISTINCT FROM
       (OLD.document_id, OLD.version_label, OLD.source_sha256, OLD.source_filename,
        OLD.etl_version, OLD.created_at, OLD.import_sha256) THEN
        RAISE EXCEPTION 'document_version % is %, content is immutable', OLD.id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'published' AND NEW.status = 'retired'
       AND NEW.published_at IS NOT DISTINCT FROM OLD.published_at THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'retired' AND NEW.status = 'published'
       AND current_setting('lipmatch.version_rollback', true) = 'on'
       AND NEW.retired_at IS NULL AND NEW.published_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'document_version % is %, cannot change to %', OLD.id, OLD.status, NEW.status
        USING ERRCODE = 'check_violation';
END $$;

-- ===========================================================================
-- 3. 向量守門：允許為 retired 版本的片段寫入「建置中」索引
--    用途：安全切回舊版時，先在新的 building 索引放好舊版片段的向量（多半直接沿用舊索引的向量），
--    再在同一交易內切換版本與索引。active 索引仍不可寫入；搜尋仍只看 usable_chunks（已發布版本），
--    所以 retired 片段即使有向量也不會被檢索到。其餘條件與 0001 相同。
-- ===========================================================================
CREATE OR REPLACE FUNCTION chunk_embeddings_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
           AND dv.status IN ('draft', 'published', 'retired')
           AND d.is_active AND d.rag_eligible
    ) THEN
        RAISE EXCEPTION 'chunk % is not approved/active/eligible or content changed', NEW.chunk_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
