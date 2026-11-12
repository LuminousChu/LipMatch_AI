-- 0004_retrieval_ready：S4 可用性查詢（有效期間、索引就緒）
-- 已套用的遷移檔不得修改；要改結構請新增下一號。欄位說明見 docs/mvp/schema.md。

-- ===========================================================================
-- 1. 文件有效期間：NULL 表示不限；期間外的文件不可被檢索
-- ===========================================================================
ALTER TABLE documents
    ADD COLUMN valid_from  TIMESTAMPTZ,
    ADD COLUMN valid_until TIMESTAMPTZ,
    ADD CONSTRAINT documents_valid_period CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from);

-- usable_chunks 加上有效期間條件（欄位不變）
CREATE OR REPLACE VIEW usable_chunks AS
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
   AND d.rag_eligible
   AND (d.valid_from IS NULL OR d.valid_from <= now())
   AND (d.valid_until IS NULL OR d.valid_until > now());

-- ===========================================================================
-- 2. 可檢索片段 = 可用片段 ∩ 目前 active 索引中「內容指紋相符」的向量
--    片段在嵌入後若被改成 rejected／停用／文件下架，會因不在 usable_chunks 而消失；
--    片段不在 active 索引（新核准但尚未重建索引）也不會出現。
--    角色過濾（visibility）仍由應用層依登入身分加上，不信任前端。
-- ===========================================================================
CREATE VIEW searchable_chunks AS
SELECT u.*, ce.index_version_id, iv.label AS index_label, iv.embedding_model, iv.embedding_dim, ce.embedding
  FROM usable_chunks u
  JOIN chunk_embeddings ce ON ce.chunk_id = u.chunk_id AND ce.content_sha256 = u.content_sha256
  JOIN index_versions iv ON iv.id = ce.index_version_id AND iv.status = 'active';
