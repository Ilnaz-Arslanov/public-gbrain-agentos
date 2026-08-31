-- Migration 007: freshness columns on documents (node D2, variant 1).
-- Markdown frontmatter stays canonical; these columns are the queryable
-- projection of it. Applied to prod 2026-08-31 10:24 UTC.
BEGIN;

ALTER TABLE documents
    ADD COLUMN valid_from    timestamptz,
    ADD COLUMN valid_until   timestamptz,
    ADD COLUMN is_latest     boolean NOT NULL DEFAULT true,
    ADD COLUMN superseded_by text,
    ADD COLUMN verified_at   timestamptz,
    ADD COLUMN verified_by   text,
    ADD COLUMN basis         text;

-- Mechanical backfill: every one of the 203 rows has a known creation date.
--   181 rows: frontmatter->>'created' -- strict ISO 'YYYY-MM-DDTHH:MM:SSZ' (len 20)
--    22 rows: frontmatter->>'date'    -- '20-daily', 'YYYY-MM-DD' (len 10)
--     0 rows fall through to created_at, but the COALESCE keeps the migration
--       total for any row written between measuring and applying.
UPDATE documents
SET valid_from = COALESCE(
        CASE WHEN frontmatter->>'created' ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'
             THEN (frontmatter->>'created')::timestamptz END,
        CASE WHEN frontmatter->>'date' ~ '^\d{4}-\d{2}-\d{2}$'
             THEN (frontmatter->>'date')::timestamptz END,
        created_at
    );

-- Carry over any supersession already recorded in frontmatter (0 rows as of
-- 2026-08-31 -- the Jaccard 0.85 branch never fired -- but the migration must
-- not silently drop a chain written between measuring and applying).
UPDATE documents
SET is_latest     = false,
    superseded_by = NULLIF(frontmatter->>'superseded_by', '')
WHERE frontmatter->>'is_latest' = 'false';

ALTER TABLE documents
    ALTER COLUMN valid_from SET NOT NULL,
    ALTER COLUMN valid_from SET DEFAULT now();

-- verified_at / verified_by / basis stay NULL on purpose: nobody re-verified
-- these facts, and a backdated stamp would make stale rows look confirmed.
-- NULL reads honestly as "never re-checked since it was written".

ALTER TABLE documents
    ADD CONSTRAINT documents_superseded_by_fkey
        FOREIGN KEY (superseded_by) REFERENCES documents(path)
        ON UPDATE CASCADE ON DELETE SET NULL,
    ADD CONSTRAINT documents_closed_not_latest
        CHECK (valid_until IS NULL OR is_latest = false),
    ADD CONSTRAINT documents_superseded_not_latest
        CHECK (superseded_by IS NULL OR is_latest = false),
    ADD CONSTRAINT documents_not_self_superseded
        CHECK (superseded_by IS NULL OR superseded_by <> path),
    ADD CONSTRAINT documents_verified_pair
        CHECK ((verified_at IS NULL) = (verified_by IS NULL));

-- Hot path: recall filters "current only" inside a scope.
CREATE INDEX idx_documents_latest_scope_updated
    ON documents (scope, updated_at DESC)
    WHERE is_latest;

-- Cold path: the superseded pile, for audits and chain walks.
CREATE INDEX idx_documents_superseded_by
    ON documents (superseded_by)
    WHERE superseded_by IS NOT NULL;

COMMIT;
