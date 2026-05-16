# Vault template

This directory is a **template** for the shared vault that all your agents read from and write to. On a fresh install, `scripts/install.sh` copies this tree to `${INSTALL_DIR}/vault/` and initializes it as a git repository (so changes are auditable).

## Layout

The vault uses a Johnny-Decimal style numeric prefix to keep folders sortable and stable across agents. Each folder has a single purpose; agents pick a folder based on what they are writing, not who is writing.

| Folder | Purpose | Typical author |
|---|---|---|
| `10-strategy/` | Long-term mission, milestones, north-star artifacts | owner, coordinator-agent |
| `10-system/` | System configs, infra notes, environment topology | coder-agent, reviewer-agent |
| `20-daily/` | Cross-agent daily timeline (append-only log) | every agent |
| `20-metrics/` | Monitoring exports, KPI snapshots, dashboards | inbox-agent, coordinator-agent |
| `30-decisions/` | Immutable decision notes with full context | every agent (most trafficked) |
| `40-projects/` | Active initiatives, PROJECT.md files, status | coordinator-agent, coder-agent |
| `50-external/` | Scout sources: forwards, articles, channels, social posts | inbox-agent |
| `50-knowledge/` | Compiled artifacts, documentation for other agents | inbox-agent, coordinator-agent |
| `60-tasks/` | Task-board snapshots, archived task histories | coordinator-agent |
| `70-runbooks/` | How-to playbooks: deploy, rollback, incident response | coder-agent, reviewer-agent |
| `80-error-patterns/` | Incidents + fixes (recall weight 3.0×) | every agent |
| `90-inbox/` | Handoffs between agents, state-machine messages | every agent |

## How indexing works

The `ingest_worker` service is **not** a filesystem watcher. The canonical write path is `memory_mcp` — when an agent calls a `create_*_note` / `update_document` tool, memory-mcp:

1. Parses the frontmatter (YAML between `---` markers).
2. Writes the markdown file to disk under `${VAULT_ROOT}/<scope>/...`.
3. Upserts the `documents` row (path, body, body_tsv generated, sha256, source_type, agent, scope, frontmatter JSONB).
4. Enqueues an `embedding_jobs` row.

The ingest worker then pops the job, splits the body into chunks (word-window, currently `WINDOW_SIZE_DEFAULT=500` / `OVERLAP_DEFAULT=50`; token-aware chunking is a follow-up), computes a 1024-dim FastEmbed `multilingual-e5-large` vector per chunk, and upserts rows into `chunks` (one per slice) keyed by `(doc_id, position)` with `chunk_hash` for idempotent re-runs.

Read-side queries (`recall_mcp.recall(...)`) combine vector cosine similarity over `chunks.embedding`, full-text rank over `documents.body_tsv`, and a scope weight. `80-error-patterns/` carries weight `3.0` so past incidents bubble to the top — this is the most useful signal for «have we hit this before?».

> **Note:** files dropped directly onto the filesystem (`vim vault/30-decisions/foo.md`) will **not** appear in recall — there is no inotify watcher. Use `memory_mcp.update_document(path=...)` instead, or insert an `embedding_jobs` row by hand. See `docs/troubleshooting.md`.

## File naming convention

```
{folder}/{YYYY-MM-DD}-{slug}.md
```

Examples:

```
30-decisions/2026-05-16-postgres-pgvector-index-strategy.md
70-runbooks/2026-05-16-deploy-staging-rollback.md
80-error-patterns/2026-05-16-mcp-stateless-headers-lost.md
```

Use lowercase, hyphens for spaces, no special characters. Date is the day the file was created (immutable). Slug should be searchable — write it like a git commit subject.

## Frontmatter

Every file should start with YAML frontmatter. See `_templates/*.md` for canonical shapes. Minimum required fields:

```yaml
---
type: decision | runbook | error-pattern | external | note
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: <writing-agent-name>
tags: []
related: []
---
```

## Adding new folders

Do **not** add folders ad hoc. If you genuinely need a new top-level scope:

1. Pick a numeric prefix that fits the conceptual band (10 = strategic, 20 = ops, 30 = decisions, 40 = projects, 50 = external/knowledge, 60 = tasks, 70 = runbooks, 80 = errors, 90 = inbox).
2. Create the folder with `.gitkeep` and `README.md` describing purpose + typical author.
3. Update this README's table.
4. Update `services/recall_mcp/source_weights.py` if the new folder needs a non-default weight.
5. Commit as a decision in `30-decisions/`.

## Git workflow

The vault is a git repository inside the install dir. `scripts/vault-autosync.sh` commits and pushes on a cron schedule (default every 10 min). If you want a self-hosted git remote, set `VAULT_GIT_REMOTE` in `.env`.

## See also

- `_templates/` — copy these when starting a new note
- `migrations/` — schema for the indexed tables
- `docs/architecture.md` — full data flow diagram
