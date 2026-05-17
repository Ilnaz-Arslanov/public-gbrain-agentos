# Setup Guide -- agent-template

Step-by-step setup for an agent workspace wired to a shared gbrain MCP server.

This is the "client-side" of the public-gbrain-agentos distro. The "server-side"
(memory MCP, recall MCP, swarm MCP, task MCP, Postgres + pgvector, Caddy + TLS) is
documented in [../docs/SERVER-SETUP.md](../docs/SERVER-SETUP.md) and installed
via [../scripts/install-vps.sh](../scripts/install-vps.sh). You **must** have a
running gbrain server (or know its `MCP_HOST` URL and have a Bearer token for
your agent) before running `install.sh` here.

## Architecture in one paragraph

`agent-template/install.sh` creates `~/.claude-lab/<agent-id>/.claude/`. Inside,
a four-layer memory pyramid (IDENTITY -> WARM -> HOT -> COLD) lives as Markdown
files. A `.mcp.json` points Claude Code at four remote MCP servers --
**memory** (write decisions / runbooks / external notes), **recall** (read
shared semantic memory), **swarm** (notify other agents), **task** (kanban board
and agent heartbeat) -- all behind a single
`${MCP_HOST}` Bearer-authenticated endpoint. Three local hooks
(`session-start`, `stop`, `precompact`) keep the local memory fresh; an
optional `gbrain-recall-on-start.sh` pulls top-N relevant items from the
shared brain on each session start.

## Prerequisites

- macOS or Linux with bash, `jq`, `python3`, `curl`, `git`
- Claude Code CLI: `npm install -g @anthropic-ai/claude-code`
- gbrain server already deployed and reachable. You need:
  - `MCP_HOST` URL (e.g. `https://mcp.example.com` or `http://<vps-ip>:8767`)
  - Agent bearer token issued by
    [`../scripts/issue-agent-token.py`](../scripts/issue-agent-token.py) on the
    gbrain VPS
- (Optional) `gh` CLI authorized if you plan to use GitHub workflows

## One-command install

```bash
cd ~/path/to/public-gbrain-agentos/agent-template
bash install.sh
```

The script asks for:

1. Agent identity (name, role, character, language, primary model, max subagents)
2. Operator profile (name, address, timezone, budget cap)
3. **gbrain connection** (MCP host URL, Bearer token, comma-separated scopes)

Default scopes: `30-decisions,50-external,70-runbooks,90-inbox`. Issue the token
on the server with matching scopes:

```bash
# on the gbrain VPS
python3 /opt/gbrain/scripts/issue-agent-token.py \
    --agent <agent-id> \
    --scopes 30-decisions,50-external,70-runbooks,90-inbox
```

Copy the printed token into the installer prompt.

## What gets created

```
~/.claude-lab/<agent-id>/.claude/
|-- CLAUDE.md                  # SOUL: who the agent is
|-- .mcp.json                  # gbrain memory/recall/swarm endpoints (chmod 600)
|-- settings.json              # Claude Code hooks (SessionStart/Stop/PreCompact)
|-- agent.env                  # source this to export MCP_HOST/AGENT_BEARER
|-- core/
|   |-- USER.md                # operator profile
|   |-- rules.md               # operational rules (RED zone, security)
|   |-- AGENTS.md              # team / models / pipelines
|   |-- MEMORY.md              # COLD archive (>14d, on-demand Read)
|   |-- LEARNINGS.md           # structured log of corrections
|   |-- warm/decisions.md      # last 14d decisions (auto-rotated)
|   `-- hot/
|       |-- recent.md          # 24h rolling journal (Stop hook appends)
|       |-- handoff.md         # last-N entries used by SessionStart
|       |-- archive/           # old recent.md slices
|       `-- pre-compact/       # PreCompact snapshots (rotated)
|-- tools/TOOLS.md             # infra map
|-- scripts/                   # memory-rotate, trim-hot, rotate-warm,
|                              # compress-warm, gbrain-recall-on-start
|-- hooks/                     # session-start, stop, precompact
|-- logs/                      # hooks.log, verbose-YYYY-MM-DD.jsonl
`-- skills/                    # symlink to ../skills/ (shared bundle)
```

`~/.claude/CLAUDE.md` and `~/.claude/rules/{bash,python,typescript}.md` are
created globally on first run.

## Verifying gbrain connectivity

```bash
source ~/.claude-lab/<agent-id>/.claude/agent.env

curl -sS -H "Authorization: Bearer ${AGENT_BEARER}" \
     -H "Accept: application/json, text/event-stream" \
     -H "Content-Type: application/json" \
     -X POST "${MCP_HOST}/recall/mcp" \
     --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Expected: JSON-RPC response listing `recall`, `get`, `related`, `recent`,
`stats` (the recall MCP tools).

## Launching the agent

```bash
source ~/.claude-lab/<agent-id>/.claude/agent.env
claude --project ~/.claude-lab/<agent-id>/.claude
```

On session start, the `SessionStart` hook reads `core/hot/handoff.md`, and if
`MCP_HOST` / `AGENT_BEARER` are set, runs `scripts/gbrain-recall-on-start.sh`
which posts a JSON-RPC `tools/call recall` to `${MCP_HOST}/recall/mcp` and
prepends a `### YYYY-MM-DD HH:MM [gbrain-recall]` block to `core/hot/recent.md`.

On each turn end, `Stop` hook appends a 200-char snippet to `recent.md` and a
full JSON envelope to `logs/verbose-YYYY-MM-DD.jsonl`.

Before Claude Code auto-compacts context, `PreCompact` hook snapshots
`recent.md` to `core/hot/pre-compact/recent-<ts>.md`.

## Memory rotation cron (optional)

```cron
30 4 * * * AGENT_WORKSPACE=$HOME/.claude-lab/<agent-id>/.claude bash $HOME/.claude-lab/<agent-id>/.claude/scripts/rotate-warm.sh
 0 5 * * * AGENT_WORKSPACE=$HOME/.claude-lab/<agent-id>/.claude bash $HOME/.claude-lab/<agent-id>/.claude/scripts/trim-hot.sh
 0 6 * * * AGENT_WORKSPACE=$HOME/.claude-lab/<agent-id>/.claude bash $HOME/.claude-lab/<agent-id>/.claude/scripts/compress-warm.sh
 0 21 * * * AGENT_WORKSPACE=$HOME/.claude-lab/<agent-id>/.claude bash $HOME/.claude-lab/<agent-id>/.claude/scripts/memory-rotate.sh
```

`trim-hot.sh` and `compress-warm.sh` shell out to `claude --model sonnet --print`
for smart summarization; if Sonnet is unreachable they fall back to a bash
extraction so memory still gets pruned.

## Adding more agents to the same shared brain

Re-run `install.sh` with a different agent name. Each agent gets its own
`~/.claude-lab/<agent-id>/.claude/` workspace and its own Bearer token, but they
**share** the gbrain server -- so writes by one agent (`create_decision_note`,
`create_runbook_note`, ...) become recall hits for the others. See
[MULTI-AGENT.md](MULTI-AGENT.md).

## Overlaying onto an existing Claude Code project

`agent-template/` is an **overlay**: you can either

1. **Standalone:** run `install.sh` to create a fresh
   `~/.claude-lab/<agent-id>/.claude/` workspace and point Claude Code at it
   via `claude --project ...`.
2. **Inside an existing repo:** copy `templates/mcp.json.template`,
   `templates/settings.json.template`, `scripts/`, `hooks/` into the repo's
   `.claude/` directory and render placeholders manually. The hooks tolerate
   absent files (handoff, recent.md) and won't break the harness.

Either way the wire protocol to gbrain is identical: HTTP MCP transport, Bearer
in `Authorization` header, JSON-RPC 2.0 in the body.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `gbrain-recall-on-start.sh` logs "MCP_HOST or AGENT_BEARER unset" | shell didn't `source agent.env` | `source ~/.claude-lab/<agent-id>/.claude/agent.env` before `claude` |
| recall returns `403` | token has no `90-inbox` (or relevant) scope, or wrong agent | re-issue with `issue-agent-token.py --scopes ...` |
| recall returns empty results | gbrain DB has no notes yet | use `create_decision_note` first, or backfill from existing decisions.md |
| `Stop` hook never fires | `settings.json` not picked up | confirm `claude --project` points at the workspace dir that contains `settings.json` |
| `trim-hot.sh` skips silently | HOT < 10KB | by design; only compresses once the file grows |

## Where to look next

- [ARCHITECTURE.md](ARCHITECTURE.md) -- end-to-end picture (memory + gbrain + hooks)
- [HOOKS.md](HOOKS.md) -- hook contracts and patterns
- [MEMORY.md](MEMORY.md) -- 4-layer memory rotation rules
- [MULTI-AGENT.md](MULTI-AGENT.md) -- multiple agents over one shared brain
- [FIRST-AGENT.md](FIRST-AGENT.md) -- worked example of first agent setup
