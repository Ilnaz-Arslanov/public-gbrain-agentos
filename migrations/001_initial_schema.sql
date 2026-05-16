-- gbrain initial schema (idempotent).
-- Run as a Postgres superuser against an empty database; afterwards the
-- service user `gbrain` will own / have full access to all objects.
--
-- Prerequisites:
--   - PostgreSQL 15+ with the `vector` extension (pgvector) installed
--   - The DB role `gbrain` already exists (created by install.sh)
--
-- Extensions ----------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- agent_tokens --------------------------------------------------------------
-- One row per agent. Bearer token is stored as sha256 hex; the raw token is
-- only ever printed once by install.sh / issue-agent-token.py.

CREATE TABLE IF NOT EXISTS agent_tokens (
    agent text PRIMARY KEY,
    token_sha256 text NOT NULL UNIQUE,
    can_write_scopes text[] DEFAULT '{}'::text[] NOT NULL,
    can_read_scopes text[] DEFAULT '{*}'::text[] NOT NULL,
    rate_limit_per_min integer DEFAULT 60 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    last_rotated timestamptz DEFAULT now() NOT NULL,
    revoked_at timestamptz
);

-- audit_log -----------------------------------------------------------------
-- Append-only record of every authenticated tool call. The audit writer
-- swallows its own failures so a misbehaving log table never blocks tools.

CREATE TABLE IF NOT EXISTS audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz DEFAULT now() NOT NULL,
    agent text NOT NULL,
    tool text NOT NULL,
    args_summary jsonb,
    result_status text,
    latency_ms integer,
    error text
);

-- documents -----------------------------------------------------------------
-- Source of truth for vault content. `body` is the markdown body (without
-- frontmatter), `frontmatter` is the parsed YAML, `body_tsv` powers FTS,
-- `sha256` powers idempotent upserts.

CREATE TABLE IF NOT EXISTS documents (
    id bigserial PRIMARY KEY,
    path text NOT NULL UNIQUE,
    frontmatter jsonb,
    body text NOT NULL DEFAULT '',
    body_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('russian', coalesce(body, ''))) STORED,
    sha256 text,
    source_type text NOT NULL,
    agent text NOT NULL,
    scope text NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

-- chunks --------------------------------------------------------------------
-- Each chunk row carries one slice of a document plus its embedding. The
-- embedding dimension matches FastEmbed `multilingual-e5-large` (1024).

CREATE TABLE IF NOT EXISTS chunks (
    id bigserial PRIMARY KEY,
    doc_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position integer NOT NULL,
    content text NOT NULL,
    chunk_hash text NOT NULL,
    embedding vector(1024),
    content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('russian', coalesce(content, ''))) STORED,
    embedded_at timestamptz,
    UNIQUE (doc_id, position)
);

-- embedding_jobs ------------------------------------------------------------
-- Queue consumed by services/ingest_worker. Jobs are unique per doc_id (one
-- pending job per document at a time) so memory-mcp can re-enqueue freely.

CREATE TABLE IF NOT EXISTS embedding_jobs (
    id bigserial PRIMARY KEY,
    doc_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    UNIQUE (doc_id, status)
);

-- delivery_outbox -----------------------------------------------------------
-- swarm-mcp inter-agent state machine: pending -> sent -> ack_missing ->
-- acked | failed. `task_id` is the idempotency key.

CREATE TABLE IF NOT EXISTS delivery_outbox (
    id bigserial PRIMARY KEY,
    task_id text NOT NULL UNIQUE,
    from_agent text NOT NULL,
    to_agent text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Grants --------------------------------------------------------------------
-- The `gbrain` service role gets full CRUD on tables + sequences. The role
-- must already exist (install.sh handles creation).

GRANT USAGE ON SCHEMA public TO gbrain;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO gbrain;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO gbrain;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO gbrain;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO gbrain;
