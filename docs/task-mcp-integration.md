# Task MCP Integration Guide

Task MCP provides a kanban-style task board and agent registry over MCP (Streamable HTTP transport). It runs as the 4th MCP service alongside memory, recall, and swarm (plus the non-MCP ingest worker).

## Quick start

```bash
# 1. Run migration (requires 001-004 applied first)
psql -U gbrain -d gbrain -f migrations/005_tasks.sql
psql -U gbrain -d gbrain -f migrations/006_agents.sql

# 2. Install systemd unit
scripts/install-vps.sh   # or manually:
cp systemd/task-mcp.service.template /etc/systemd/system/gbrain-task-mcp.service
# Replace {{SERVICE_USER}}, {{INSTALL_DIR}}, {{ETC_DIR}} in the unit file
systemctl daemon-reload
systemctl enable --now gbrain-task-mcp

# 3. Verify
curl -s -X POST http://127.0.0.1:8769/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer <your-agent-token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Port

Default: **8769**. Set via `MCP_PORT=8769` in the systemd unit Environment directive (not in `.env`, since `MCP_PORT` is per-service).

## Auth

Same dual Bearer + HMAC model as other gbrain services:

- **Bearer token**: issued via `scripts/issue-agent-token.py`. Stored as SHA-256 hash in `agent_tokens` table.
- **HMAC**: Hermes-style signed requests via `shared/asgi_auth.py`.

### Write scope

Task write operations (create, update, start, review, done, block, reopen) require the `10-tasks` scope in `can_write_scopes`:

```bash
# Add task write scope to an existing agent token
psql -U gbrain -d gbrain -c "
  UPDATE agent_tokens
  SET can_write_scopes = array_append(can_write_scopes, '10-tasks')
  WHERE agent = 'my-agent'
    AND NOT ('10-tasks' = ANY(can_write_scopes));
"
```

Read operations (task_get, task_list, task_history, agent_status, agent_list) work for any authenticated agent.

## MCP client config

Add to your agent's `.mcp.json`:

```json
{
  "gbrain-tasks": {
    "type": "http",
    "url": "http://<gbrain-host>:8769/mcp",
    "headers": {
      "Authorization": "Bearer <your-agent-token>"
    }
  }
}
```

## Tools (13 total)

### Task CRUD (10 tools)

| Tool | Description | Write? |
|------|-------------|--------|
| `task_create` | Create a new task | yes |
| `task_update` | Update task fields (title, description, assignee, priority, metadata) | yes |
| `task_get` | Fetch single task by ID | no |
| `task_list` | List tasks with optional filters (assignee, status) | no |
| `task_start` | Transition: new/blocked -> progress | yes |
| `task_review` | Transition: progress -> review | yes |
| `task_done` | Transition: review -> done (terminal) | yes |
| `task_block` | Transition: new/progress -> blocked | yes |
| `task_reopen` | Reopen: blocked -> new, review -> progress | yes |
| `task_history` | Status transition history for a task | no |

### Agent Registry (3 tools)

| Tool | Description | Write? |
|------|-------------|--------|
| `agent_heartbeat` | Upsert agent status + last_seen timestamp | yes |
| `agent_status` | Fetch single agent's status | no |
| `agent_list` | List all agents, optionally filtered by status | no |

### Status state machine

```
new --> progress --> review --> done (terminal)
 |         |          |
 v         v          v
blocked   blocked   progress (reopen/reject)
 |
 v
new (reopen)
```

## CLI wrapper

`scripts/task-board-gbrain.sh` provides a shell interface for task operations:

```bash
export GBRAIN_TOKEN="<your-agent-token>"
export TASK_MCP_HOST="<gbrain-host>"

# List tasks
./scripts/task-board-gbrain.sh my-tasks --agent my-agent
./scripts/task-board-gbrain.sh list --status progress

# CRUD
./scripts/task-board-gbrain.sh create --title "Fix auth bug" --assignee my-agent --priority high
./scripts/task-board-gbrain.sh get 1
./scripts/task-board-gbrain.sh update 1 --next-action "deploy to staging" --last-result "tests pass"

# State transitions
./scripts/task-board-gbrain.sh start 1 --note "beginning work"
./scripts/task-board-gbrain.sh review 1 --note "ready for review"
./scripts/task-board-gbrain.sh done 1 --note "shipped"
./scripts/task-board-gbrain.sh block 1 --reason "waiting for API key"
./scripts/task-board-gbrain.sh reopen 1 --note "rejected, needs fix"

# Agent heartbeat
./scripts/task-board-gbrain.sh heartbeat --status online
```

## Database schema

### tasks

| Column | Type | Notes |
|--------|------|-------|
| id | bigserial | PK |
| title | text | required |
| description | text | default '' |
| status | text | CHECK: new/progress/review/done/blocked |
| assignee | text | nullable |
| priority | text | CHECK: low/medium/high/critical |
| created_by | text | auto from auth |
| metadata | jsonb | arbitrary key-value |
| created_at | timestamptz | auto |
| updated_at | timestamptz | auto (trigger) |

### task_history

Append-only audit trail. One row per status transition.

### agents

| Column | Type | Notes |
|--------|------|-------|
| agent_name | text | PK |
| status | text | CHECK: online/offline/busy/error/maintenance |
| last_seen | timestamptz | updated by heartbeat |
| host | text | nullable |
| version | text | nullable |
| metadata | jsonb | arbitrary |

## Tool gating

All 13 tools are in the `core` tool set (GBRAIN_TOOLS=core, the default). No special configuration needed.
