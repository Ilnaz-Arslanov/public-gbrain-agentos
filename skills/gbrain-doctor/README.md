# gbrain Doctor

Agent-facing diagnostic skill at `skills/gbrain-doctor/`. Runs from an agent's own machine and
checks that the agent's gbrain wiring is healthy: MCP connectivity, identity, recall, swarm,
hooks parity, webhooks, MCP URL security topology, the per-agent GitHub repo, and the skill's
own install state.

This is distinct from the server-side `scripts/gbrain_doctor.py` in the repo root, which inspects
the gbrain install on the VPS host. The skill doctor looks at gbrain from the outside, the way an
agent experiences it.

## Why it exists

Agents need a safe, deterministic self-check for MCP, hooks, webhooks, identity, and shared
memory from their own machine — without leaking secrets and without mutating state. When an agent
cannot recall, notify, or receive tasks, the doctor answers "is it me or the server?" before
anyone escalates to the host.

## How it works

- Reads `.mcp.json` (resolution order: `--mcp-json` > `~/.claude-lab/<agent>/.claude/.mcp.json`
  when `--agent` is set > `./.mcp.json` > `~/.mcp.json`).
- Calls each gbrain MCP endpoint over streamable-http JSON-RPC with stdlib `urllib` — a single
  stateless `tools/list` / `tools/call` POST, no `initialize` handshake (gbrain runs
  `FASTMCP_STATELESS_HTTP=1`). Modeled exactly on `scripts/task-board-gbrain.sh`.
- Parses both plain JSON and SSE `data: <json>` frames; `result.isError` is treated as a failed call.
- Runs local checks through files, sockets, platform service managers (`launchctl` / `systemctl`),
  and `gh`.
- Every message, exception, subprocess output, and autofix log passes through the redactor before
  it reaches stdout/stderr/JSON. Python 3.12+, stdlib only.

## How to run

```bash
# Full read-only run for an agent
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent silvana

# Hooks parity only, no color
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent silvana --group G6 --no-color

# Machine-readable JSON, exit code only
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent silvana --json --quiet

# Explicit .mcp.json path (outside the agent workspace)
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py \
  --agent silvana \
  --mcp-json /Users/you/.claude-lab/silvana/.claude/.mcp.json \
  --no-color
```

### Flags

| Flag | Behavior |
|---|---|
| `--json` | Emit a JSON array of check results (`name`, `status`, `message`, `remediation`). No raw secrets; `auto_fix` always omitted. |
| `--fix` | Offer only whitelisted safe fixes attached to failing checks. Logs to stderr, redacted. Mutates nothing without confirmation unless `--yes`. |
| `--quiet` | Suppress normal stdout; rely on exit code. Autofix audit logs still go to stderr. |
| `--agent AGENT` | Canonical agent id for identity, workspace path, webhook, GitHub repo, and task-registry checks. |
| `--mcp-json PATH` | Override MCP config path (see resolution order above). |
| `--expected-hooks PATH` | Override the hook manifest. Default `references/expected-hooks.json`. |
| `--group G1[,G6]` | Run only selected groups. Repeatable and comma-separated. Valid `G1`..`G10`. |
| `--no-color` | Disable ANSI color (also off when `NO_COLOR` is set or stdout is not a TTY). |
| `--yes`, `--noninteractive` | Skip confirmation prompts for safe `--fix` actions only. Never expands the fix allowlist. |

## How to read results

Status semantics:

- `pass` — green.
- `warn` — non-blocking; empty data, stale freshness, drift, weak inference.
- `fail` — broken, actionable. Any `fail` sets exit code `1`.
- `skip` — not applicable (optional `tasks` server absent, write probe held by read-only policy).

Exit codes: `0` no fail (warn/skip allowed), `1` one or more fail, `2` invocation/config error
(invalid `--group`, unreadable explicit manifest, malformed explicit `.mcp.json`, Python <3.12).

A green read-only run can still contain `skip`: the optional `gbrain-tasks` server may be absent,
and the memory write probe (C021) is `skip` by policy because it would mutate state. Those skips do
not make the run fail.

Example table:

```text
[PASS] G1.mcp_tools_list              recall=82ms swarm=77ms memory=91ms tasks=skip
[WARN] G6.stop_hook_recent_fresh      last stop-hook marker is 31h old
        -> Start a session/turn or repair Stop hook.
[FAIL] G8.backend_ports_closed        8767 reachable on public backend IP
        -> Close UFW/security group; bind MCP to localhost or tailnet.

Verdict: FAIL (34 pass, 6 warn, 1 fail, 5 skip) -- output redacted and safe to paste
```

## Groups G1-G10

| Group | Covers | Notes |
|---|---|---|
| G1 | MCP config + connectivity: required servers present (`tasks` optional), specs well-formed, `tools/list`, recall/swarm stats. | Fail on timeout/401/403/5xx/protocol error. |
| G2 | Identity + token: unauth request denied (401/403), agent identity resolved, bearer non-placeholder/consistency, redaction selftest. | A `200` without Bearer is a fail. |
| G3 | Swarm: recent acked ratio, this agent's pending depth, fetch a recent delivery by id. | Warns on empty data. |
| G4 | Recall: `recall` probe, `recent`, `reindex_check` drift. | `reindex_check` exception (e.g. OperationalError) is a fail. |
| G5 | Memory: write tools registered, tool schemas readable, write probe held as `skip` (read-only default). | True write is manual only. |
| G6 | Hooks parity: manifest loaded, settings layers parse, no `disableAllHooks`, expected events registered, command paths exist, scripts executable, plugin refs enabled, event names valid, not local-only, basenames match, blocking hooks not async, Stop freshness, PreCompact snapshot. | Critical — a missing hook silently breaks a feature. |
| G7 | Webhooks: listener `/healthz`, auth modes configured, platform service loaded, token file safe (mode 600, non-empty, no trailing-newline risk), delivery errors. | Missing listener is warn/skip when none is expected. |
| G8 | Topology/security: MCP URL topology, public endpoint enforces auth, backend ports closed, TLS valid, Cloudflare SSE caveat. | See topology section below. |
| G9 | GitHub: `gh` present+authed, per-agent repo exists and private, repo status. | Missing repo can be created by `--fix`. |
| G10 | Skill self-install: dir present, SKILL.md frontmatter valid, scripts executable, symlinked into skills dir, references present. | Symlink/chmod can be `--fix`ed. |

## `--fix` behavior

Only four checks attach an autofix callback, all safe and local:

| Check | Fix |
|---|---|
| C027 `G6.hook_scripts_executable` | `chmod +x` the hook script. |
| C046 `G9.github_repo_exists_private` | `gh repo create --private` the per-agent repo (only when missing). |
| C050 `G10.skill_scripts_executable` | `chmod +x` the CLI entry script. |
| C051 `G10.skill_symlinked` | Symlink the repo skill into the agent/global skills dir (only when target absent). |

- Interactive mode: each fix asks a concrete yes/no prompt on stderr.
- Non-interactive without `--yes`: fixes are skipped and logged.
- `--fix` never re-runs the full check set; it prints a final line telling you to rerun to verify.

Explicit non-goals — `--fix` NEVER: writes tokens, edits RED files (CLAUDE.md / rules.md /
USER.md), restarts services, modifies `.mcp.json` / `settings.json`, or writes to gbrain memory.

## Topology and the Cloudflare caveat

The doctor is topology-aware and does not mandate any single topology.

Secure topologies:

- Caddy+TLS public domain on 443 with valid TLS, no-Bearer returns 401/403, and raw MCP backend
  ports (8766-8769) closed on the resolved public IP.
- Tailscale: URL host is an IP in `100.64.0.0/10`; plain HTTP is acceptable only on tailnet
  addresses, Bearer still enforced.
- Local-only: `127.0.0.1` / `localhost` / `::1`; fine for same-host testing if Bearer is enforced.
- Cloudflare-fronted / Tunnel: secure when Bearer is enforced and actual G1 streamable-http calls
  succeed.

Insecure (fail): raw public backend IP on 443 or MCP ports, reachable public backend MCP ports,
public HTTP outside tailnet/local, or any MCP endpoint returning `200` without Bearer.

Cloudflare caveat: Cloudflare proxied DNS (orange cloud) can buffer SSE and break streamable-http.
The doctor surfaces this as a warn when Cloudflare is detected but does not force a topology
choice. Repo `docs/security.md` warns against proxied DNS for MCP; the prince's preference and the
docs can diverge, so the doctor escalates the doctrine decision rather than deciding it.

Cloudflare origin caveat: a Cloudflare-fronted hostname hides the raw origin IP, so
`backend_ports_closed` can only prove origin closure when `GBRAIN_BACKEND_IP` (or equivalent) is
provided. Otherwise it warns rather than false-passing.

## Troubleshooting

| Symptom | Likely group | Action |
|---|---|---|
| `tools_list` fail with HTTP 401/403 | G1/G2 | Bearer token wrong/placeholder in `.mcp.json`; reissue and re-render outside chat. |
| `tools_list` fail with timeout/URLError | G1/G8 | Network path or service down; check URL host reachability and host service health. |
| `auth_without_bearer_denied` fail (200) | G2/G8 | MCP endpoint exposed without auth; fix AuthCaptureMiddleware/proxy before exposing. |
| `recall_reindex_check` fail | G4 | Re-run ingest/reindex on the gbrain host; inspect recall dependencies (sqlite/aiosqlite). |
| `stop_hook_recent_fresh` warn | G6 | A quiet agent; start a session/turn, or repair the Stop hook if it never fires. |
| `hook_scripts_executable` fail | G6 | `--fix` to `chmod +x`, or do it manually. |
| `webhook_listener_healthz` fail | G7 | Start Hermes/jarvis listener or fix the port. |
| `backend_ports_closed` fail | G8 | Close UFW/security group; bind MCP to localhost or tailnet. |
| `github_repo_exists_private` fail (missing) | G9 | `--fix` to `gh repo create --private`, or create manually. |
| `skill_symlinked` fail | G10 | `--fix` to symlink, or `ln -s "$PWD/skills/gbrain-doctor" ~/.claude/skills/gbrain-doctor`. |

Rerun a single group with `--group G<n>`. Manual side-effect probes (memory write, swarm
self-notify) live in `references/manual-write-probes.md` and are never run by the doctor.

---

## Appendix: Proposed CLAUDE.md addition (NOT auto-applied)

The block below is a **proposed motivation patch only**. The doctor never edits any CLAUDE.md.
It is quoted here for the prince to review. Existing agent `CLAUDE.md` files are in the RED zone;
applying this to a live agent requires the prince's/operator's approval. Target template:
`agent-template/templates/CLAUDE.md.template`, inserted after `**Data hierarchy:**` and before
`## Memory Layers`.

```markdown
**Shared gbrain memory:**
- gbrain is the shared long-term memory for Orgrimmar agents.
- Use it when continuity matters across sessions, agents, or machines.
- Before non-trivial work, consider recall for prior decisions, runbooks, failures, and handoffs.
- When you discover durable facts, decisions, fixes, or reusable procedures, consider saving a concise note.
- Prefer high-signal summaries over transcript dumps.
- Include source and confidence when saving knowledge.
- Use swarm when another agent should wake up or own a handoff.
- Use tasks when work needs status, assignee, review, or a durable next action.
- Local files are private working memory; gbrain is the cross-agent layer.
- If gbrain is unavailable, continue the task and note what should be synced later.
- Never put secrets, tokens, private keys, or raw credentials into gbrain.
```
