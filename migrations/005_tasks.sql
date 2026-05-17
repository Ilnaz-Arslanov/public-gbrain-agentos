-- gbrain task-board: tasks + task_history tables.
-- Idempotent: safe to re-run.

-- tasks -------------------------------------------------------------------
-- Kanban board for agent swarm. Status state machine:
-- new -> progress -> review -> done (terminal), any -> blocked, blocked -> new/progress.

CREATE TABLE IF NOT EXISTS tasks (
    id bigserial PRIMARY KEY,
    title text NOT NULL,
    description text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'new',
    assignee text,
    priority text NOT NULL DEFAULT 'medium',
    created_by text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tasks_status_check CHECK (
        status IN ('new', 'progress', 'review', 'done', 'blocked')
    ),
    CONSTRAINT tasks_priority_check CHECK (
        priority IN ('low', 'medium', 'high', 'critical')
    )
);

-- task_history ------------------------------------------------------------
-- Append-only audit trail for status transitions.

CREATE TABLE IF NOT EXISTS task_history (
    id bigserial PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    old_status text,
    new_status text NOT NULL,
    changed_by text NOT NULL,
    note text,
    changed_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes -----------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks (status);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status
    ON tasks (assignee, status);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at_desc
    ON tasks (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tasks_updated_at_desc
    ON tasks (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_history_task_id
    ON task_history (task_id, changed_at DESC);

-- updated_at trigger (reuse touch_updated_at from 001 if exists) ---------

CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tasks_touch ON tasks;
CREATE TRIGGER tasks_touch
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Grants ------------------------------------------------------------------

GRANT ALL PRIVILEGES ON TABLE tasks TO gbrain;
GRANT ALL PRIVILEGES ON SEQUENCE tasks_id_seq TO gbrain;
GRANT ALL PRIVILEGES ON TABLE task_history TO gbrain;
GRANT ALL PRIVILEGES ON SEQUENCE task_history_id_seq TO gbrain;
