-- gbrain agent registry: agents table for heartbeat + status.
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS agents (
    agent_name text PRIMARY KEY,
    status text NOT NULL DEFAULT 'offline',
    last_seen timestamptz NOT NULL DEFAULT now(),
    host text,
    version text,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agents_status_check CHECK (
        status IN ('online', 'offline', 'busy', 'error', 'maintenance')
    )
);

-- updated_at trigger ------------------------------------------------------

DROP TRIGGER IF EXISTS agents_touch ON agents;
CREATE TRIGGER agents_touch
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Indexes -----------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_agents_status
    ON agents (status);

CREATE INDEX IF NOT EXISTS idx_agents_last_seen
    ON agents (last_seen DESC);

-- Grants ------------------------------------------------------------------

GRANT ALL PRIVILEGES ON TABLE agents TO gbrain;
