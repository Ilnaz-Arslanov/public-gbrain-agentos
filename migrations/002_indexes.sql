-- Indexes for gbrain hybrid search and audit/outbox hot paths.
--
-- Apply AFTER 001_initial_schema.sql. Idempotent: every index uses
-- CREATE INDEX IF NOT EXISTS so this file can be replayed safely.
--
-- HNSW on chunks.embedding ---------------------------------------------------
-- pgvector HNSW for cosine similarity. `m` and `ef_construction` are pgvector
-- defaults; bump `ef_construction` if you have ample build-time budget.

CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- GIN on tsvectors -----------------------------------------------------------
-- Full-text search backed by generated body_tsv / content_tsv columns.

CREATE INDEX IF NOT EXISTS idx_documents_body_tsv
    ON documents
    USING gin (body_tsv);

CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
    ON chunks
    USING gin (content_tsv);

-- B-tree composites for recall + recent filters -----------------------------

CREATE INDEX IF NOT EXISTS idx_documents_agent_scope_updated
    ON documents (agent, scope, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_scope_updated
    ON documents (scope, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_source_type
    ON documents (source_type);

-- audit_log read paths ------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_audit_agent_ts
    ON audit_log (agent, ts DESC);

CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON audit_log (ts DESC);

-- embedding_jobs queue scan -------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_embedding_jobs_status_created
    ON embedding_jobs (status, created_at);

-- delivery_outbox worker poll ----------------------------------------------

CREATE INDEX IF NOT EXISTS idx_outbox_status_next_retry
    ON delivery_outbox (status, next_retry_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_outbox_to_agent_status
    ON delivery_outbox (to_agent, status, created_at);

CREATE INDEX IF NOT EXISTS idx_outbox_created_desc
    ON delivery_outbox (created_at DESC);
