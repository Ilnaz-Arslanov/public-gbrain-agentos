# Setup — manual install

If you want to deploy this without handing `AGENT.md` to a Claude Code agent, follow these steps yourself. They mirror what the agent does, written for a human who already knows SSH and Postgres.

The steps assume:

- A fresh Ubuntu 22.04 LTS VPS with SSH access and sudo.
- A local Mac / Linux / WSL workstation with Python 3.11+, `crontab`, and the `claude` CLI installed.
- This repo cloned locally at `~/public-gbrain-agentos/` (or wherever).

Estimated time: 30–45 minutes including waiting for `apt`.

---

## Step 0: pre-flight on your local machine

```bash
cd ~/public-gbrain-agentos
bash scripts/sanitize-check.sh
```

If sanitize-check exits 0, the repo is clean. If not, re-clone and try again. Do not edit by hand to make it pass.

Confirm you can SSH to the VPS:

```bash
ssh <USER>@<VPS_IP> "uname -a && lsb_release -a"
```

You should see `Ubuntu 22.04`. Anything else — stop. Provision a fresh 22.04 VPS first.

---

## Step 1: push the repo to the VPS

From your local machine:

```bash
rsync -avz --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  ~/public-gbrain-agentos/ <USER>@<VPS_IP>:/opt/gbrain/
```

Verify:

```bash
ssh <USER>@<VPS_IP> "ls /opt/gbrain/"
```

You should see `AGENT.md`, `README.md`, `services/`, `migrations/`, `scripts/`, etc.

---

## Step 2: run the installer

SSH to the VPS and run the install script:

```bash
ssh <USER>@<VPS_IP>
cd /opt/gbrain
sudo bash scripts/install.sh
```

The script is idempotent and takes 10–15 minutes. Watch the output. It will:

1. `apt update` and install `python3.11` (from the deadsnakes PPA), `postgresql-16`, `postgresql-16-pgvector`, `caddy`.
2. Create system user `gbrain` and group `gbrain`.
3. Create Postgres role `gbrain` with a generated password.
4. Create Postgres database `gbrain` owned by `gbrain`.
5. Enable the `vector` extension.
6. Run all SQL files in `migrations/` in order.
7. Create `${VAULT_ROOT}` (default `/opt/gbrain/vault/`) and seed from `vault-template/`.
8. Create a Python virtualenv at `/opt/gbrain/.venv/` and `pip install -r requirements.txt`.
9. Install systemd unit files from `systemd/*.service.template` into `/etc/systemd/system/`, substituting `${INSTALL_DIR}` etc.
10. `systemctl daemon-reload && systemctl enable --now gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp gbrain-task-mcp gbrain-ingest-worker`.
11. If you passed a domain via `--domain mcp.example.com`, install `caddy/Caddyfile.template` to `/etc/caddy/Caddyfile`, substitute the domain, `systemctl reload caddy`.
12. Generate an initial **admin agent token**. **This is printed once.** Capture it now.

Expected last lines of output:

```
==== INSTALL COMPLETE ====
Admin agent token (save this, you will not see it again):
  <64-char-hex>

Services running:
  gbrain-memory-mcp     active
  gbrain-recall-mcp     active
  gbrain-swarm-mcp      active
  gbrain-task-mcp       active
  gbrain-ingest-worker  active

Next: run scripts/smoke-test.sh to verify auth and tools.
```

If the script exits with anything other than that — stop and read `docs/troubleshooting.md`.

---

## Step 3: verify services are healthy

```bash
systemctl status gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp gbrain-task-mcp gbrain-ingest-worker --no-pager
```

All five must say `active (running)`. If any says `failed`:

```bash
journalctl -u <service-name> -n 100 --no-pager
```

The most common causes:

- **Postgres not ready when service started** — `systemctl restart <service>` once Postgres is up.
- **Port already in use** — another service grabbed 8766/7/8/9. Find it: `ss -tlnp | grep 876`. Kill or reconfigure.
- **Permission denied on vault** — the `gbrain` user must own `${VAULT_ROOT}`. `chown -R gbrain:gbrain /opt/gbrain/vault`.

---

## Step 4: run smoke test

Still on the VPS:

```bash
cd /opt/gbrain
bash scripts/smoke-test.sh
```

This should print:

```
[1/5] GET / on memory_mcp        ... 200 ok
[2/5] GET / on recall_mcp        ... 200 ok
[3/5] GET / on swarm_mcp         ... 200 ok
[4/5] tools/list with admin token... 9 tools
[5/5] auth roundtrip             ... ok
SMOKE TEST PASSED
```

If step 4 returns 401, the admin token in `ADMIN_AGENT_TOKEN` (or wherever the script reads it) does not match the sha256 stored. Re-run the issue-token flow.

If step 5 fails, the AuthCaptureMiddleware is not loaded. Verify `services/memory_mcp/server.py` and `services/recall_mcp/server.py` both wrap `mcp.http_app()` in `AuthCaptureMiddleware` and use `uvicorn.run(...)`.

---

## Step 5: issue agent tokens

Decide which agents will use the brain. At minimum:

- **coordinator-agent** — your main Claude Code agent. Full scope set.
- **inbox-agent** — the local Telegram bot. Restricted scopes.

On the VPS:

```bash
cd /opt/gbrain
sudo -u gbrain python scripts/issue-agent-token.py \
  --agent coordinator-agent \
  --scopes '20-daily,30-decisions,50-external,50-knowledge,70-runbooks,80-error-patterns,90-inbox'

sudo -u gbrain python scripts/issue-agent-token.py \
  --agent inbox-agent \
  --scopes '30-decisions,50-external,50-knowledge,90-inbox'
```

Each command prints the token once. Copy both to your password manager immediately.

To add more agents later (e.g. `coder-agent`, `reviewer-agent`, a research bot), re-run with the appropriate scopes.

---

## Step 6: TLS via Caddy (optional but recommended)

If you have a domain (e.g. `mcp.example.com`) with an A record pointing at `<VPS_IP>`, Caddy will issue a Let's Encrypt cert automatically.

Edit `/etc/caddy/Caddyfile` if `install.sh` did not substitute correctly:

```
mcp.example.com {
    handle_path /memory/* {
        reverse_proxy 127.0.0.1:8767 {
            flush_interval -1
            header_up Host {upstream_hostport}
        }
    }
    handle_path /recall/* {
        reverse_proxy 127.0.0.1:8768 {
            flush_interval -1
            header_up Host {upstream_hostport}
        }
    }
    handle_path /swarm/* {
        reverse_proxy 127.0.0.1:8766 {
            flush_interval -1
            header_up Host {upstream_hostport}
        }
    }
    handle_path /task/* {
        reverse_proxy 127.0.0.1:8769 {
            flush_interval -1
            header_up Host {upstream_hostport}
        }
    }
    log {
        output file /var/log/caddy/mcp.access.log {
            roll_size 50mb
            roll_keep 7
        }
        format json
    }
}
```

`flush_interval -1` disables buffering — required for SSE / streamable-http transport.

Reload:

```bash
sudo systemctl reload caddy
journalctl -u caddy -n 50 --no-pager
```

You should see Caddy obtain a cert (look for `certificate obtained successfully`). If it fails:

- DNS A record propagated? `dig mcp.example.com`.
- Port 80 open? `sudo ufw status`. Caddy needs 80 for HTTP-01 ACME challenge, plus 443 for HTTPS.
- See `docs/troubleshooting.md` Q: "Caddy can't get TLS cert".

Test from outside:

```bash
curl -sS https://mcp.example.com/recall/
```

Should return the recall service banner.

---

## Step 7: install inbox-agent locally

On your local workstation (not the VPS):

```bash
cd ~/public-gbrain-agentos
bash scripts/install-local.sh
```

The script:

- creates `${INBOX_AGENT_HOME}` (default `~/.claude-lab/inbox-agent`)
- copies `inbox-agent/` skeleton in
- renders `.mcp.json` from `inbox-agent/config/.mcp.json.template`, substituting your VPS URL and the inbox-agent token
- writes `.env` with the values you provided, `chmod 600`
- pings the recall endpoint with the token to verify reachability

If reachability fails — fix that before continuing. The local box must be able to reach the brain over HTTPS (or Tailscale, or SSH tunnel).

---

## Step 8: create the Telegram bot

If you do not already have a bot:

1. Open Telegram, search for `@BotFather`.
2. `/newbot`.
3. Pick a display name (e.g. `My Inbox`).
4. Pick a username (must end in `bot`, e.g. `my_inbox_bot`).
5. Copy the token BotFather returns. Format: `123456:ABCdef...`.
6. `/setprivacy` → choose your bot → `Enable`. Privacy ON means the bot only sees DMs and replies — recommended.

Write the token into `${INBOX_AGENT_HOME}/.env` AND the digest token file expected by `daily-digest.sh`:

```bash
echo 'TELEGRAM_BOT_TOKEN=123456:ABCdef...' >> ${INBOX_AGENT_HOME}/.env
chmod 600 ${INBOX_AGENT_HOME}/.env

mkdir -p ${INBOX_AGENT_HOME}/secrets/telegram
echo '123456:ABCdef...' > ${INBOX_AGENT_HOME}/secrets/telegram-bot-token
chmod 600 ${INBOX_AGENT_HOME}/secrets/telegram-bot-token
```

Get your own Telegram user_id (the bot ignores everyone else). Easiest way: message `@userinfobot`, it replies with your id. Write it to `.env`:

```bash
echo 'PRINCE_CHAT_ID=<your-user-id>' >> ${INBOX_AGENT_HOME}/.env
```

`bot.py` reads `PRINCE_CHAT_ID` for the allowlist — only messages from that user reach the hook.

Now start the bot daemon:

```bash
${INBOX_AGENT_HOME}/.venv/bin/pip install -r ${INBOX_AGENT_HOME}/requirements.txt
nohup ${INBOX_AGENT_HOME}/.venv/bin/python ${INBOX_AGENT_HOME}/bot.py \
  >> ${INBOX_AGENT_HOME}/logs/bot.log 2>&1 &
```

For long-running setups, wire `bot.py` into systemd or launchd instead of `nohup`. Verify:

```bash
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq .
tail -20 ${INBOX_AGENT_HOME}/logs/bot.log   # expect "Application started"
```

---

## Step 9: install crontab

```bash
(crontab -l 2>/dev/null; cat ${INBOX_AGENT_HOME}/scripts/crontab.example) | crontab -
crontab -l
```

You should see two entries:

```
*/15 * * * * /bin/bash -lc "$INBOX_AGENT_HOME/scripts/compile.sh >> $INBOX_AGENT_HOME/logs/compile.log 2>&1"
0 9 * * *    /bin/bash -lc "$INBOX_AGENT_HOME/scripts/daily-digest.sh >> $INBOX_AGENT_HOME/logs/digest.log 2>&1"
```

The compile job processes new raw forwards. The digest job sends yesterday's recap to Telegram.

---

## Step 10: end-to-end smoke

On your phone or desktop Telegram:

1. Send `/start` to your bot. It should reply with a brief help message within 2 seconds.
2. Send a URL (YouTube, article, anything). It should ack within ~2 seconds: `Got it` (the exact text lives in `inbox-agent/bot.py`).
3. From your Claude Code agent (configured with the coordinator-agent token in `.claude/.mcp.json`), call:

   ```
   recall.recent(scope="50-external", limit=5)
   ```

4. The URL must appear with `agent: inbox-agent` and a recent `created_at`.

If yes — done. The brain is live.

If no — read `docs/troubleshooting.md` in this order: bot not responding → ingest worker not embedding → recall returns empty.

---

## Step 11 (Path B only): generate a personal agent workspace

This step is **optional**. Skip it if you only want the brain + inbox-agent (that is Path A — you are already done at step 10).

If you want one or more personal Claude Code agents that share this brain (a coordinator, a coder, a marketer, a researcher), use the `agent-template/` generator. Run it once per agent.

```bash
cd ~/public-gbrain-agentos
bash agent-template/install.sh
```

The script is interactive. It will ask:

- **agent id** — slug, lowercase, hyphenated (e.g. `coordinator-agent`, `coder-agent`). Becomes the workspace directory name and the `agent` row in `agent_tokens`.
- **role description** — 1 line. E.g. "main coordinator and brainstorm partner".
- **owner name** — your name. Goes into `core/USER.md` of the new workspace.
- **MCP host** — your brain URL. `https://mcp.example.com` if Caddy is up, otherwise `http://<VPS_IP>:8767` for memory plus the matching ports for recall/swarm/task (the script renders all four).
- **agent bearer token** — leave blank for now. You will fill it in step 13.
- **model** — `claude-sonnet-4.6` for most roles, `claude-opus-4.7` for a coordinator.
- **install dir** — default `~/.claude-lab/<agent-id>`. Confirm.

The script creates `~/.claude-lab/<agent-id>/.claude/` with the full skeleton: `CLAUDE.md`, `core/USER.md`, `core/rules.md`, `core/MEMORY.md`, `core/LEARNINGS.md`, `core/AGENTS.md`, `core/warm/decisions.md`, `core/hot/handoff.md`, `core/hot/recent.md`, `tools/TOOLS.md`, `settings.json`, plus `hooks/` (stop / session-start / pre-compact) and `scripts/` (memory rotation). All template files are rendered with your answers using a `sed_i` helper that works on both macOS and Linux.

Run it again for each additional agent you want.

Verify the skeleton:

```bash
WORKSPACE=~/.claude-lab/<agent-id>/.claude
ls -la "$WORKSPACE"
test -f "$WORKSPACE/CLAUDE.md"
test -f "$WORKSPACE/core/rules.md"
test -f "$WORKSPACE/.mcp.json"
test -x "$WORKSPACE/hooks/stop-hook.sh"
test -x "$WORKSPACE/scripts/trim-hot.sh"
```

All checks must pass. If any fails, read the install.sh output for which template did not render and re-run.

---

## Step 12 (Path B only): issue a per-agent Bearer token

For each personal agent, on the VPS:

```bash
sudo -u gbrain python /opt/gbrain/scripts/issue-agent-token.py \
  --agent <agent-id> \
  --scopes '<comma-separated-scopes>'
```

Default scope sets per role:

- `coordinator-agent`: `20-daily,30-decisions,50-external,50-knowledge,70-runbooks,80-error-patterns,90-inbox`
- `coder-agent`: `30-decisions,50-knowledge,70-runbooks,80-error-patterns,90-inbox`
- `marketer-agent`: `20-daily,50-knowledge,90-inbox`
- `researcher-agent` (recall-only): `--scopes ''` (empty — recall always works, but no writes)

The script prints the token **once**. Save it to your password manager. **Do not reuse tokens across agents** — each one has its own identity row for audit purposes.

---

## Step 13 (Path B only): wire the Bearer into `.mcp.json`

Open `~/.claude-lab/<agent-id>/.claude/.mcp.json` and replace the `<AGENT_BEARER>` placeholder (left in by step 11) with the token from step 12. The file should end up looking like:

```json
{
  "mcpServers": {
    "gbrain-memory": {
      "url": "https://mcp.example.com/memory/mcp",
      "headers": { "Authorization": "Bearer <actual-token>" }
    },
    "gbrain-recall": {
      "url": "https://mcp.example.com/recall/mcp",
      "headers": { "Authorization": "Bearer <actual-token>" }
    },
    "gbrain-swarm": {
      "url": "https://mcp.example.com/swarm/mcp",
      "headers": { "Authorization": "Bearer <actual-token>" }
    },
    "gbrain-task": {
      "url": "https://mcp.example.com/task/mcp",
      "headers": { "Authorization": "Bearer <actual-token>" }
    }
  }
}
```

`chmod 600` the file.

Add per-workspace memory-rotation crons (one set per agent — keep them on separate lines so a failure in one workspace does not bleed into another):

```bash
WORKSPACE=~/.claude-lab/<agent-id>/.claude
(crontab -l 2>/dev/null; cat <<EOF
# memory rotation for <agent-id>
0 * * * *  /bin/bash -lc "$WORKSPACE/scripts/trim-hot.sh         >> $WORKSPACE/logs/trim-hot.log 2>&1"
30 3 * * * /bin/bash -lc "$WORKSPACE/scripts/rotate-warm.sh      >> $WORKSPACE/logs/rotate-warm.log 2>&1"
0 4 * * 0  /bin/bash -lc "$WORKSPACE/scripts/compress-warm.sh    >> $WORKSPACE/logs/compress-warm.log 2>&1"
EOF
) | crontab -

crontab -l | grep "<agent-id>"
```

---

## Step 14 (Path B only): verify the workspace launches and recalls

```bash
claude --project ~/.claude-lab/<agent-id>/.claude
```

(Adapt to whatever invocation your `claude` CLI version uses for project-scoped runs.)

Expected behaviour:

1. The CLI opens cleanly.
2. The SessionStart hook runs (you can confirm via `tail ~/.claude-lab/<agent-id>/.claude/logs/session-start.log`).
3. Ask the agent: "What is your role?" — it should respond with the role you set in step 11.
4. Ask the agent: "Recall recent entries from scope 50-external." — it should call `gbrain-recall.recent` and return results (at minimum, the URL you forwarded in step 10 of Path A).

If recall returns 0 results despite the brain having data:

- Re-check the Bearer in `.mcp.json` — it must be the token issued in step 12, not the placeholder, not the inbox-agent's, not another agent's.
- Test the brain directly: `curl -sS -H "Authorization: Bearer <token>" https://mcp.example.com/recall/mcp/`. Expect 406 with an MCP error body (live upstream). 401 → wrong token. Connection refused → firewall.
- Check the token is alive: `psql -U gbrain -d gbrain -c "SELECT agent, can_write_scopes, revoked_at FROM agent_tokens WHERE agent='<agent-id>';"`. `revoked_at` should be `NULL`.

Repeat steps 11–14 for every additional agent the user wants. Each one is independent — a failure in one workspace does not affect the others.

---

## Ongoing operations

**Adding an agent.** `python scripts/issue-agent-token.py --agent <name> --scopes '...'`. Add the token to that agent's `.mcp.json`.

**Revoking an agent.** `python scripts/issue-agent-token.py --agent <name> --revoke`.

**Backing up the vault.** The vault is `${VAULT_ROOT}`. `rsync` it to wherever you keep backups, daily. You can ignore Postgres — it is recomputable from markdown.

**Backing up Postgres.** Optional. `pg_dump -U gbrain gbrain > backup.sql` if you want to skip re-embedding on restore.

**Upgrading services.** Pull a newer commit, `rsync` to VPS, run `sudo bash scripts/install.sh` again (idempotent). Services restart automatically.

**Re-embedding the vault** (after model change or vault rebuild). Stop the worker, clear the chunks (and their queue), re-enqueue every document, restart the worker:

```bash
sudo systemctl stop gbrain-ingest-worker
psql -U gbrain -d gbrain <<'SQL'
TRUNCATE chunks;
TRUNCATE embedding_jobs;
INSERT INTO embedding_jobs (doc_id, status) SELECT id, 'pending' FROM documents ON CONFLICT DO NOTHING;
SQL
sudo systemctl start gbrain-ingest-worker
```

The worker re-chunks and re-embeds every document. Takes 1–10 minutes per 10k documents on a 4-vCPU box. `documents.body` is the canonical source — no markdown re-read needed.

**Rotating tokens.** Issue a new token, update the agent's `.mcp.json`, then revoke the old one. Never the reverse — you will lock out the agent.

**Log rotation.** systemd journal handles VPS logs (`journalctl --vacuum-time=30d` to enforce retention). Local inbox-agent logs grow in `${INBOX_AGENT_HOME}/logs/`. Rotate with `logrotate` or a daily `truncate -s 0`.
