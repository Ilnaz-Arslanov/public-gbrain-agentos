# public-gbrain-agentos

> A Second Brain for your Claude Code agents, plus an optional generator for the agents themselves. Self-hosted on a single VPS. Markdown vault, hybrid recall, dual-write resilience, Telegram inbox, layered per-agent memory.

## TL;DR

Clone this repo, hand `AGENT.md` to a fresh Claude Code agent, pick **Path A** or **Path B**, answer ~8–12 questions, and 30–90 minutes later you have either (A) a working long-term memory layer fed by a Telegram bot, or (B) the same plus N personal Claude Code agent workspaces — each with layered memory and recall into the shared brain.

## What is this?

**A Second Brain is a long-term, structured memory layer for AI agents.** Your agents stop forgetting decisions, runbooks, error patterns, and external sources between sessions. Instead of dumping context into ever-longer prompts, agents write to and recall from a shared markdown vault.

**Plus, optionally, the agents themselves.** The `agent-template/` directory in this repo is a complete generator for Claude Code agent workspaces with layered memory (hot/warm/cold), Stop/SessionStart/PreCompact hooks, and `.mcp.json` wired to your brain. One install command per agent. Run it as many times as you need agents.

**Who it is for.** Solo builders and small teams running multiple Claude Code agents (a coordinator, a coder, a reviewer, a researcher) who want them to share institutional memory. If you have one agent and a 200k context window covers your work, you probably want Path A (brain only). If you have three agents and they keep stepping on each other's decisions, you want Path B (brain + workspaces).

## Two paths

| | Path A — Minimal | Path B — Full stack |
|---|---|---|
| What you ship | Shared brain (VPS) + Telegram inbox-agent | Brain + inbox-agent + N personal agent workspaces |
| Install time | ~30 min | ~60–90 min (10 min per extra agent) |
| Best for | Archiving forwards, daily digests, recall API to wire up later by hand | Multi-agent teams that share one brain, layered per-agent memory |
| Uses `agent-template/`? | No | Yes |
| You get | A searchable markdown vault fed by Telegram forwards, hybrid recall over MCP, daily digest | Same as A, plus 1+ Claude Code workspaces at `~/.claude-lab/<agent-id>/.claude/` with their own SOUL, rules, decisions log, hot handoff, hooks, memory-rotation crons, and MCP recall into the shared brain |

If unsure → start with Path A. Adding Path B later is one command per agent (`bash agent-template/install.sh`).

## What you get after install

| Component | Path A | Path B |
|---|---|---|
| Postgres 16 + pgvector on VPS | yes | yes |
| 3 MCP services (memory write, recall read, swarm event bus) | yes | yes |
| Ingest worker (embeds new vault files) | yes | yes |
| Markdown vault (12 numbered folders) | yes | yes |
| Telegram inbox-agent (dual-write to local raw/ + brain) | yes | yes |
| Daily digest cron (09:00) + compile cron (every 15 min) | yes | yes |
| Optional ingestion skills (YouTube, IG, X, voice, web) | yes | yes |
| One Bearer token per agent identity in `agent_tokens` | 2 by default (`coordinator-agent`, `inbox-agent`) | 2 + 1 per personal agent |
| Personal Claude Code workspace(s) at `~/.claude-lab/<agent-id>/.claude/` | no | yes (N of them) |
| Layered memory per workspace (CLAUDE.md / rules.md / decisions.md / handoff.md → MEMORY.md / LEARNINGS.md / TOOLS.md on demand) | no | yes |
| Stop / SessionStart / PreCompact hooks per workspace | no | yes |
| Memory-rotation crons (hot → warm, warm → cold, compress old warm) | no | yes (one set per workspace) |
| `.mcp.json` per workspace, pre-wired to the brain | no | yes |

## Architecture

```
   You forward content                         Your agents recall
   to a Telegram bot              <-->         and write decisions
           |                                            |
           v                                            v
  +----------------+                          +-------------------+
  |  inbox-agent   |---------HTTPS / TS-------|       VPS         |
  |  (local)       |                          |                   |
  |  dual-writes   |                          |  Caddy (TLS)      |
  |  to local raw/ |                          |  memory_mcp 8767  |
  |  AND remote    |                          |  recall_mcp 8768  |
  +----------------+                          |  swarm_mcp  8766  |
          ^                                   |  ingest-worker    |
          |                                   |                   |
  +-------+-------------+ (Path B)            |  Postgres 16      |
  |  Personal agents    |--------HTTPS/TS---->|  + pgvector       |
  |  ~/.claude-lab/     |                     |  + FTS            |
  |  <agent-id>/.claude |                     |                   |
  |  + hot/warm/cold    |                     |  vault/ (12 dirs) |
  |  + Stop/Session/    |                     +-------------------+
  |    PreCompact hooks |
  |  + .mcp.json        |
  +---------------------+
```

Every agent has a Bearer token. Every write is scoped (an `inbox-agent` cannot write decisions). Every retrieval combines semantic (1024-dim FastEmbed multilingual embeddings) and lexical (Postgres FTS) search, fused with Reciprocal Rank Fusion, re-weighted by source kind and recency.

The vault is plain markdown on a filesystem. Postgres is an index over it, not the source. If you lose the database, you re-embed from markdown in 5 minutes. If you lose the vault, you have a problem — back it up.

In Path B, each personal agent's workspace is **also** plain markdown (its SOUL, rules, decisions, handoff). Each workspace is self-contained — you can `tar` it up, move it to another machine, point it at the same brain, and resume.

## Quick start

```bash
git clone https://github.com/<your-fork>/public-gbrain-agentos.git
cd public-gbrain-agentos
# Open Claude Code in this directory, then:
# "Read AGENT.md. I want Path A." (or "Path B with a coordinator and a coder")
```

That is it. The agent reads `AGENT.md`, asks you for VPS access + a few config inputs, runs the install scripts, issues tokens, configures the local inbox bot, and (Path B) runs `agent-template/install.sh` once per personal agent. It runs an end-to-end smoke test before declaring done.

If you would rather install by hand, read `docs/setup.md` — it is the same steps written for humans.

## What is in this repo

```
public-gbrain-agentos/
  AGENT.md                main entry — fed into Claude Code agent
  README.md               this file
  LICENSE                 Apache 2.0
  .env.example            all env vars commented by section
  .gitignore
  pyproject.toml          package metadata + deps
  requirements.txt        pinned versions

  docs/
    architecture.md       how the system works (deep dive, incl. agent workspace layer)
    setup.md              manual install, step-by-step (Path A + Path B)
    security.md           threat model, token rotation, exposure rules
    troubleshooting.md    FAQ for common errors (incl. agent-template entries)

  services/
    shared/               auth, db, audit, config
    memory_mcp/           write API (AuthCaptureMiddleware)
    recall_mcp/           read API (hybrid search)
    swarm_mcp/            event bus for inter-agent messages
    ingest_worker/        watches embedding_jobs, embeds new chunks

  migrations/             schema (SQL)
  tests/                  pytest, smoke + unit

  vault-template/         12 folders + READMEs + note templates
                          (10-strategy, 20-daily, 30-decisions, ...)

  inbox-agent/            local bot + dual-write hook + cron scripts

  agent-template/         (Path B only) generator for personal agent workspaces
    install.sh            interactive: prompts for agent id, role, owner,
                          MCP host, model — produces a full workspace
    templates/            *.template files for CLAUDE.md, rules.md,
                          mcp.json, USER.md, decisions.md, etc.
    scripts/              memory-rotate.sh, trim-hot.sh, rotate-warm.sh,
                          compress-warm.sh, gbrain-recall-on-start.sh
    hooks/                stop-hook.sh, session-start-hook.sh,
                          precompact-hook.sh
    docs/                 ARCHITECTURE.md, MEMORY.md, HOOKS.md,
                          MULTI-AGENT.md, TOKEN-OPTIMIZATION.md,
                          SETUP-GUIDE.md

  skills/                 optional ingestion skills (YouTube, IG, X, ...)

  caddy/                  Caddyfile template (TLS, optional)
  systemd/                unit file templates
  scripts/                install, smoke-test, sanitize-check,
                          issue-agent-token, migrate
```

## Requirements

**On the VPS (both paths):**

- Ubuntu 22.04 LTS (other Ubuntu/Debian versions are untested)
- 4 vCPU, 8 GB RAM minimum (recall on 50k+ vault files needs the RAM)
- 20 GB disk (vault + Postgres + embeddings)
- SSH access (key-based auth)
- Optional: a domain with an A record pointing at the VPS, for TLS via Caddy. Without a domain, the system is reachable over Tailscale or SSH tunnels.

**On your local workstation (both paths):**

- macOS, Linux, or WSL on Windows
- Claude Code installed (`claude` CLI), authenticated via Anthropic Max or a comparable plan that allows agent runs
- Python 3.11+
- `crontab` available (default on Mac/Linux)
- Tailscale (optional, if not using a public domain)

**Additionally for Path B:**

- One directory per agent under `~/.claude-lab/<agent-id>/` (the install script creates them; just make sure the parent is writable and you have ~15 MB free per agent before any logs accumulate).
- Ability to launch Claude Code with a project flag (e.g. `claude --project ~/.claude-lab/<agent-id>/.claude`) — most current CLI versions support this.
- Per-workspace crontab entries (the install adds three lines per agent for memory rotation; if you already run a crowded crontab, plan accordingly).

**Accounts (both paths):**

- A Telegram account (to talk to your inbox bot).
- A Telegram bot from `@BotFather` (free, 60 seconds to set up). One bot per agent that needs Telegram reachability — never share tokens between bots.

**Optional API keys** (only needed if you enable the corresponding ingestion skill):

- Groq — Whisper transcription of voice notes
- HikerAPI — Instagram caption and metadata extraction
- TranscriptAPI — YouTube transcript fetching
- SocialData — X/Twitter thread reading
- Perplexity — web research

You do not need any of these to start. Enable them later by editing `${INBOX_AGENT_HOME}/.env` and restarting the bot.

## What this is NOT

- Not a multi-tenant SaaS. One vault per VPS, one user (or small team) per vault.
- Not a chat UI. There is no web frontend. The vault is markdown, recall is an MCP API, the bot is for inbox capture.
- Not a replacement for your agent's session context. It is long-term memory; short-term still belongs in the prompt.
- Not a vector database product. The Postgres + pgvector + FTS combination is intentional — markdown stays canonical, the index is recomputable.
- Not battle-tested at scale. Designed for solo/small-team usage with vaults up to ~100k notes and up to ~10 personal agents per brain.
- Not a replacement for your IDE. Path B workspaces are agent home directories, not project repos. You still write your code wherever you write code.

## License

Apache License 2.0. See [LICENSE](LICENSE) for the full text.

## Acknowledgements

- **FastMCP** — the Python MCP framework that powers the three service skeletons.
- **FastEmbed** — embedding library, used for the multilingual-e5-large model (1024 dims, runs on CPU).
- **pgvector** — Postgres extension for vector storage and HNSW indexing.
- **Caddy** — TLS reverse proxy.
- The `agent-template/` directory is a merged port of [`qwwiwi/public-architecture-claude-code`](https://github.com/qwwiwi/public-architecture-claude-code) — the universal Claude Code workspace generator with layered memory and hooks. The brain-side MCP wiring is added on top so each generated workspace plugs directly into the gbrain shipped by this repo.
- The vault folder convention (12 numbered scopes for daily, decisions, runbooks, error-patterns, etc.) is inspired by PARA, Zettelkasten, and the Cognee project's note structure.

Contributions, bug reports, and forks welcome. Open an issue or PR on the upstream repo.
