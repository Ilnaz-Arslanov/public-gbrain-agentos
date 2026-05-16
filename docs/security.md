# Security

What this system protects, what it does not, and how to keep an unintentional mistake from becoming a breach.

---

## Threat model

**What is protected:**

- All traffic to MCP services is Bearer-authenticated. A request without a valid `Authorization: Bearer <token>` header returns 401. A token whose scopes do not include the target write folder returns 403.
- Caddy + Let's Encrypt provides TLS for all public-facing traffic. Bearer tokens never traverse plain HTTP in normal operation.
- Postgres is reachable only on localhost (`pg_hba.conf` default in Ubuntu's `postgresql-16` package). Services authenticate to it with a password.
- The vault is filesystem-owned by the `gbrain` system user, mode 750. Only `gbrain` and root can read.

**What is NOT protected:**

- **The admin agent token has full power.** It can issue new tokens for any agent with any scopes, read every vault entry, and write into any folder. Treat it like a root SSH key.
- **The Postgres password has full power.** Anyone with Postgres-superuser-equivalent access can `DROP TABLE`, alter the schema, or read tokens (the sha256 column does not protect against a chosen-prefix attack if the password is leaked).
- **A compromised local workstation owns its agent tokens.** The inbox-agent token lives in `${INBOX_AGENT_HOME}/.env` — if your laptop is stolen and unlocked, the attacker can write into `50-external` and `90-inbox`.
- **The brain VPS is a single point of failure.** No HA, no read replicas, no geo-redundancy. If the VPS is compromised, the attacker has everything.
- **The vault contains whatever you put into it.** If you forward private messages to the bot, those messages are in `vault/50-external/` in plain text. Anyone with read access to the vault filesystem can read them. Encrypt the volume if the threat model demands it.

---

## Token issuance and rotation

**Issue:**

```bash
python scripts/issue-agent-token.py \
  --agent <name> \
  --scopes 'comma,separated,list'
```

Prints the raw token once to stdout. Store it in a password manager. Never paste it into a chat, never commit it, never echo it back to logs.

The scopes argument restricts which vault folders the agent can write into. A `recall-only` agent gets no scopes (`--scopes ''`). A scoped writer gets only the folders it needs:

| Agent role | Suggested scopes |
|---|---|
| Coordinator (full agent) | `20-daily, 30-decisions, 50-external, 50-knowledge, 70-runbooks, 80-error-patterns, 90-inbox` |
| Inbox-agent (Telegram bot) | `30-decisions, 50-external, 50-knowledge, 90-inbox` |
| Coder agent (writes runbooks, error-patterns) | `30-decisions, 70-runbooks, 80-error-patterns, 90-inbox` |
| Reviewer agent | `30-decisions, 80-error-patterns, 90-inbox` |
| Read-only research agent | (empty — recall works without write scopes) |

**Revoke:**

```bash
python scripts/issue-agent-token.py --agent <name> --revoke
```

Sets `revoked_at = now()`. The next auth check excludes the row.

**Rotate** (replace without downtime):

1. Issue a new token: `python scripts/issue-agent-token.py --agent <name> --scopes '...'` (this leaves the old one valid).
2. Update the agent's `.mcp.json` with the new token.
3. Restart the agent.
4. Wait long enough to confirm the new token works (1 minute is plenty).
5. Revoke the old token: `python scripts/issue-agent-token.py --agent <name> --revoke`. This revokes ALL active tokens for that agent by default — pass `--revoke-id <id>` if you want only the old one.

---

## AuthCaptureMiddleware — why it matters

Earlier MCP server versions read the `Authorization` header inside the tool handler via `ctx.request_context.request.headers`. Under FastMCP's `streamable-http` transport with `FASTMCP_STATELESS_HTTP=1`, that mapping is empty by the time the tool handler runs — the HTTP layer has already discarded request context.

The fallback path in those versions was an `MCP-FALLBACK-TOKEN` env variable. If no header arrived, the call was attributed to a single fallback agent. Result: every other agent looked like the fallback agent in the audit log, in the `agent` column of `documents`, in the `from_agent` column of `delivery_outbox`. Six agents could all be writing, but `audit_log` would show them all as one.

This distribution ships with `AuthCaptureMiddleware` in `memory_mcp/server.py`, `recall_mcp/server.py`, and `swarm_mcp/server.py`. The middleware:

1. Runs at the ASGI layer (before FastMCP's HTTP app).
2. Reads `Authorization` from `scope['headers']`.
3. Stores it in a module-level `_REQUEST_AUTH: ContextVar[str | None]`.
4. Calls the inner app.

Tool handlers' `_extract_token()` reads the ContextVar. If empty, they raise `PermissionError` → 401. **No silent fallback. No env fallback.** If your stack is missing the middleware, your audit trail is unreliable. Upgrade before exposing the brain to multiple agents.

To verify the middleware is in place:

```bash
grep -l 'AuthCaptureMiddleware' services/*/server.py
# Should list: memory_mcp/server.py, recall_mcp/server.py, swarm_mcp/server.py

grep -l 'MCP-FALLBACK-TOKEN\|FALLBACK-AGENT' services/
# Should be empty.
```

---

## Network exposure

There are two supported topologies. Pick one.

### Option A: public domain with Caddy + TLS

```
internet ---> :443 Caddy ---> :8767/8/6 (localhost) MCP services
```

- UFW rules: `ufw allow 80/tcp` (ACME challenge), `ufw allow 443/tcp`, `ufw default deny incoming`.
- MCP services bind `127.0.0.1` only (`MCP_HOST=127.0.0.1`).
- Postgres binds `127.0.0.1` only (default).
- SSH on 22 is the only other open port. Restrict to your IP or a jumphost if possible.

This is the recommended default. TLS + Bearer is layered defense — neither alone is sufficient; both together raise the cost of attack significantly.

### Option B: Tailscale-only, no public exposure

```
your devices on tailnet ---> 100.x.y.z:8767/8/6 MCP services (Tailscale IP only)
```

- No port 80/443 exposed publicly.
- MCP services bind the Tailscale interface IP (e.g. `MCP_HOST=100.x.y.z`), not `0.0.0.0`.
- UFW default deny incoming, allow only from `100.0.0.0/8`.
- Bearer auth still required — Tailscale auth ≠ MCP auth, they layer.

Tailscale-only is appropriate when (a) you do not have a domain or do not want public TLS, (b) you accept that any agent must be on the tailnet to reach the brain, (c) you trust Tailscale's coordination server in your threat model.

### What NOT to do

- **Do NOT bind MCP services to `0.0.0.0` without a firewall.** Even with Bearer auth, exposing 8767/8/6 to the internet means anyone can hammer the auth endpoint indefinitely. UFW + firewall rules are not optional.
- **Do NOT put the brain behind Cloudflare proxied DNS** for the MCP endpoints. Cloudflare's proxy buffers SSE / streaming, breaking the `streamable-http` transport. Use DNS-only (`proxied=false`) for MCP subdomains.
- **Do NOT expose Postgres on the public network.** Even with a password, this is a `DROP TABLE` risk if the password leaks. Postgres → localhost only.

---

## Vault privacy

The vault is plain markdown on disk owned by the `gbrain` system user. Anyone who can read the filesystem as that user (or root) can read every note.

**Implications:**

- **Backups must be encrypted.** If you `rsync` the vault to a remote backup target, use `restic` (built-in encryption) or `borg`, not plain `rsync`. The vault contains potentially private content the bot has captured.
- **Do not commit the vault to a public git remote.** The `vault-template/` folder in this repo is the starter skeleton (READMEs, templates, no actual content). The live vault at `${VAULT_ROOT}` is a separate thing and must not be pushed. Ensure your `.gitignore` excludes it.
- **Encrypt the volume** if the VPS provider's snapshot system is part of your threat model. Hetzner/DO/Linode snapshots are stored unencrypted by default. LUKS at the partition level mitigates this.

---

## Telegram and BotFather

The bot token from BotFather is a shared secret. Anyone with the token can impersonate the bot:

- Read incoming messages directed at the bot (in chats where it's added).
- Send messages as the bot.

**Treat the bot token like a password:**

- Stored only in `${INBOX_AGENT_HOME}/.env`, mode 600.
- Never logged. The compile and digest scripts do not echo it.
- Never committed.

The `TELEGRAM_USER_ID` (your numeric ID) is not secret — anyone who has ever DM'd a public bot has exposed theirs. But the allowlist in `classifier.yaml` is what enforces "only my forwards are processed". If you accidentally remove the allowlist, anyone who knows the bot username can spam the inbox-agent and have their messages stored in your vault. Verify the allowlist after any config change.

If you suspect the bot token leaked:

1. Revoke it: `/revoke` in `@BotFather`, then `/newbot` or `/token` for a new one.
2. Update `${INBOX_AGENT_HOME}/.env` with the new token.
3. Restart the bot (whatever process / launchd / systemd unit you use).

---

## Audit log

Every MCP write logs a row in `audit_log` with `agent`, `tool`, `args_summary`, `result_status`, `latency_ms`. This is your forensic trail. Inspect it:

```sql
SELECT ts, agent, tool, result_status
FROM audit_log
WHERE ts > now() - interval '24 hours'
ORDER BY ts DESC
LIMIT 50;
```

If you see writes from an agent you do not expect, or scope violations (`result_status = 403`), investigate immediately — either rotate the token or revoke it.

Recall calls are NOT in `audit_log` by default (read-only, high volume). If you need read auditing, modify `services/recall_mcp/` to write a separate `recall_log` table.

---

## Updating after a security advisory

If a CVE lands in one of the dependencies (FastMCP, uvicorn, asyncpg, FastEmbed, pgvector, Caddy, Postgres):

1. Read the advisory. Determine which service is affected.
2. Pin the patched version in `requirements.txt`.
3. Rebuild the venv on the VPS: `sudo -u gbrain /opt/gbrain/.venv/bin/pip install -r /opt/gbrain/requirements.txt --upgrade`.
4. Restart the affected service: `sudo systemctl restart gbrain-<service>-mcp`.
5. Verify health: `journalctl -u gbrain-<service>-mcp -n 50 --no-pager`.

Do not skip this. Long-running services that are publicly reachable accumulate exposure.
