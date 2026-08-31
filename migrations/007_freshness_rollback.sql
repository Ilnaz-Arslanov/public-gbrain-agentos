-- Rollback for 007. Drops the projection only -- frontmatter in the vault
-- files is canonical and untouched, so nothing is lost that cannot be rebuilt.
BEGIN;
DROP INDEX IF EXISTS idx_documents_superseded_by;
DROP INDEX IF EXISTS idx_documents_latest_scope_updated;
ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_verified_pair,
    DROP CONSTRAINT IF EXISTS documents_not_self_superseded,
    DROP CONSTRAINT IF EXISTS documents_superseded_not_latest,
    DROP CONSTRAINT IF EXISTS documents_closed_not_latest,
    DROP CONSTRAINT IF EXISTS documents_superseded_by_fkey,
    DROP COLUMN IF EXISTS basis,
    DROP COLUMN IF EXISTS verified_by,
    DROP COLUMN IF EXISTS verified_at,
    DROP COLUMN IF EXISTS superseded_by,
    DROP COLUMN IF EXISTS is_latest,
    DROP COLUMN IF EXISTS valid_until,
    DROP COLUMN IF EXISTS valid_from;
COMMIT;
