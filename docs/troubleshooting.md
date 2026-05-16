# Troubleshooting

FAQ for the most common failures. Find your symptom, follow the fix. If your problem is not listed, the diagnostic order is always:

1. `journalctl -u gbrain-<service>-mcp -n 200 --no-pager` on the VPS
2. `tail -200 ${INBOX_AGENT_HOME}/logs/*.log` locally
3. Check Postgres: `psql -U gbrain -d gbrain -c "SELECT count(*) FROM documents;"` and `psql -U gbrain -d gbrain -c "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL;"`

---

## Q: `smoke-test.sh` fails with 401 Unauthorized

**Cause:** the Bearer token sent does not match any row in `agent_tokens`, or the row is `revoked_at IS NOT NULL`.

**Fix:**

1. Confirm the token's sha256 is in the table:

   ```bash
   TOKEN="<your-token>"
   HASH=$(echo -n "$TOKEN" | sha256sum | cut -d' ' -f1)
   psql -U gbrain -d gbrain -c "SELECT agent, revoked_at FROM agent_tokens WHERE token_sha256 = '$HASH';"
   ```

2. If no row → re-issue the token: `python scripts/issue-agent-token.py --agent <name> --scopes '...'`.
3. If revoked_at is set → the token was revoked. Issue a new one.
4. If row exists and not revoked but you still get 401, the AuthCaptureMiddleware likely is not loaded. See "Q: AuthCaptureMiddleware missing" below.

---

## Q: `ingest-worker` is not embedding new files

**Symptoms:** you write a markdown file under `${VAULT_ROOT}/50-external/test.md`, wait a minute, and `recall.recent` does not return it.

**Diagnosis:**

```bash
sudo journalctl -u gbrain-ingest-worker -n 100 --no-pager
```

Common causes:

- **FastEmbed model not downloaded.** First start downloads ~1GB. If the network was flaky, it may have failed silently. Look for `huggingface_hub` errors in the log. Fix: `sudo -u gbrain /opt/gbrain/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('intfloat/multilingual-e5-large')"` to force the download with visible output.

- **You wrote the file directly to the filesystem.** The ingest worker is **not** a filesystem watcher — it polls the `embedding_jobs` table, and only `memory_mcp` writes jobs to that table. A file dropped into `${VAULT_ROOT}/50-external/test.md` by hand is invisible to the worker. Fix: either call `memory_mcp.update_document(path=...)` from an authenticated agent, or insert the job manually: `psql -U gbrain -d gbrain -c "INSERT INTO documents(path, body, source_type, agent, scope) VALUES ('<path>', '<body>', 'external', 'admin', '50-external') RETURNING id;"` followed by `INSERT INTO embedding_jobs(doc_id, status) VALUES (<id>, 'pending') ON CONFLICT DO NOTHING;`.

- **Permissions on the new file.** The `gbrain` user must be able to read the file. Fix: `sudo chown -R gbrain:gbrain ${VAULT_ROOT}`.

- **Worker not running.** `sudo systemctl status gbrain-ingest-worker`. If `failed`, look at the last 50 log lines for the actual error.

---

## Q: Caddy can't get a TLS certificate

**Symptoms:** Caddy logs show `acme: error: 403` or `challenge failed`.

**Fix in this order:**

1. **DNS:** does `dig +short mcp.example.com` return the VPS IP? If not, the A record hasn't propagated yet, or it points elsewhere. Wait 5–15 minutes after creating the record.
2. **Port 80:** Let's Encrypt's HTTP-01 challenge requires port 80 open. `sudo ufw status` should show `80/tcp ALLOW`. Add: `sudo ufw allow 80/tcp`.
3. **Existing service on port 80:** `sudo ss -tlnp | grep ':80 '`. If something else is on 80 (apache, nginx, another caddy), stop it.
4. **Cloudflare or proxy in front?** If the domain is on Cloudflare with the orange cloud (proxied), Let's Encrypt may fail. Set the record to "DNS only" (grey cloud) for the MCP subdomain. MCP also needs DNS-only because the proxy buffers SSE.
5. **Rate limits:** Let's Encrypt has rate limits per registered domain. If you retried many times with the same domain, you may be temporarily blocked. Wait an hour.

After fixing, `sudo systemctl reload caddy` and watch logs.

---

## Q: inbox-agent bot doesn't respond to messages

**Symptoms:** you forward a message to the bot, nothing happens.

**Diagnosis chain:**

1. Is the bot process running locally? Check whatever supervises it (launchd / systemd / manual).
2. Check bot log: `tail -50 ${INBOX_AGENT_HOME}/logs/bot.log`.
3. Common: **Privacy ON without the bot being addressed properly.** A privacy-ON bot only sees messages that (a) start with `/command`, (b) are direct DMs, (c) are replies to its own messages, or (d) `@mention` it. If you are forwarding to a group chat where the bot lives, it will not see plain forwards. Either DM the bot directly, or disable privacy with `@BotFather → /setprivacy → Disable` (the bot will see all group messages).
4. **User not allowlisted.** Check `${INBOX_AGENT_HOME}/config/classifier.yaml` — your `user_id` must be in `allowed_user_ids`. The bot silently ignores everyone else.
5. **Token wrong.** A bad `TELEGRAM_BOT_TOKEN` makes the bot fail to connect on startup. Bot log will show `Unauthorized: bad token`.
6. **Network.** The bot polls `api.telegram.org` over HTTPS. If your network blocks it (corporate proxy, etc.), polling fails.

---

## Q: bot doesn't reply to my forwards (no "Got it" ack)

**Symptoms:** you send a message to `bot.py` and nothing comes back, but the bot was working before.

**Diagnosis chain:**

1. **Is `bot.py` actually running?** `pgrep -af 'inbox-agent/bot.py'` should return a single PID. If empty, restart it (see Step 8 of AGENT.md / setup.md). If multiple, kill the duplicates — competing pollers race on `getUpdates`.
2. **Is the bot token valid?** `curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq .`. Expect `ok: true` and the bot's username. `401 Unauthorized` means the token in `${INBOX_AGENT_HOME}/.env` is wrong or BotFather revoked it.
3. **Is your user_id allowlisted?** `grep PRINCE_CHAT_ID ${INBOX_AGENT_HOME}/.env` must match the user_id `@userinfobot` reports for you. The bot silently ignores everyone else — no log entry, no ack.
4. **Bot log:** `tail -50 ${INBOX_AGENT_HOME}/logs/bot.log`. Look for `Application started`, `Handler invoked`, or any tracebacks. Common: `RuntimeError: This Application is already running` (duplicate poller) or `Conflict: terminated by other getUpdates` (another instance somewhere — see point 1).
5. **Privacy mode + group chats.** If you're forwarding from a group chat where the bot lives with privacy ON, the bot only sees `/commands`, DMs, replies, and @mentions. Either DM the bot directly, or `@BotFather → /setprivacy → Disable`.

---

## Q: bot replies "Got it" but `recall.recent` returns empty

**Symptoms:** the bot acks normally, the raw file appears in `${INBOX_AGENT_HOME}/raw/...`, but `recall.recent(scope='50-external')` from the coordinator agent does not show the URL.

**Diagnosis chain:**

1. **Did the dual-write attempt succeed?** `tail -200 ${INBOX_AGENT_HOME}/logs/save-to-raw.log`. The hook logs the HTTP status returned by `memory_mcp.create_external_note`. A `200` means brain accepted it. A `401`/`403`/`5xx` means the brain rejected it — read the body to see why.
2. **Is the `.mcp.json` Bearer correct?** `cat ${INBOX_AGENT_HOME}/.claude/.mcp.json | jq '.mcpServers["gbrain-memory"]'`. Confirm the URL matches your VPS (`https://mcp.<your-domain>/memory/mcp` or `http://<tailscale-ip>:8767/mcp`) and the bearer header is non-empty. If you see literal `${MCP_HOST}` / `${AGENT_TOKEN}` placeholders, re-run `bash scripts/install-local.sh` — `envsubst` did not substitute.
3. **VPS reachability:** `curl -sS -H "Authorization: Bearer $(jq -r '.mcpServers["gbrain-memory"].headers.Authorization' ${INBOX_AGENT_HOME}/.claude/.mcp.json | cut -d' ' -f2)" https://mcp.<your-domain>/recall/mcp/` (or the Tailscale equivalent). Expect 406 with an MCP error body — that proves the upstream is alive and your token works. 401 → wrong token. Connection refused → firewall blocks 443 (or 8767 on Tailscale).
4. **Did the embedding job run?** On the VPS: `psql -U gbrain -d gbrain -c "SELECT id, status, created_at FROM embedding_jobs ORDER BY id DESC LIMIT 5;"`. A `pending` job that hasn't moved in minutes means the ingest worker is stuck — see "ingest-worker is not embedding new files" above.
5. **Scope check.** `recall.recent` filters by scope. The hook writes scope `50-external` by default — if your `classifier.yaml` rerouted the URL to `50-knowledge` or `90-inbox`, change the recall call accordingly.

---

## Q: `recall.recall(...)` returns 0 results, but I know the content is there

**Diagnosis:**

1. **Is the file in the vault?** `find ${VAULT_ROOT} -name '*<keyword>*'`.
2. **Is it in `documents`?**

   ```sql
   SELECT d.id, d.path, d.scope, length(d.body) AS body_len,
          (SELECT count(*) FROM chunks c WHERE c.doc_id = d.id AND c.embedding IS NOT NULL) AS embedded_chunks
   FROM documents d
   WHERE d.path LIKE '%<keyword>%';
   ```

3. **If `documents` row exists but `embedded_chunks = 0`** — the embedding step has not happened yet. Check the queue: `SELECT status, count(*) FROM embedding_jobs WHERE doc_id = <id> GROUP BY status;`. If `pending` is stuck, look at `journalctl -u gbrain-ingest-worker -f` for embedding errors (most often FastEmbed cache missing).
4. **If `documents` and `chunks` rows both exist but no recall results** — the query may not be matching anywhere. Try a broader query, or use `recall.recent(scope='...')` to confirm the entry is visible.
5. **You wrote the file by hand instead of via memory-mcp.** Direct filesystem writes are not picked up. See the previous FAQ ("ingest-worker is not embedding new files").
6. **HNSW index missing.** `psql -U gbrain -d gbrain -c "\\d chunks"` and confirm an HNSW index on `chunks.embedding` exists, plus a GIN index on `documents.body_tsv` / `chunks.content_tsv`. If not, re-run `migrations/002_indexes.sql`.

---

## Q: AuthCaptureMiddleware missing — calls attributed to wrong agent

**Symptoms:** `audit_log.agent` shows the same agent for all writes regardless of the Bearer token sent, or 401s appear with valid tokens.

**Cause:** the MCP server is running an older version that lacks `AuthCaptureMiddleware`. Under FastMCP's `streamable-http`, headers do not reach the tool handler — the middleware is the fix.

**Fix:**

1. Pull the latest distro: `git pull` (or `rsync` again from your local clone).
2. Run `sudo bash scripts/install.sh` on the VPS (idempotent — it will reinstall services).
3. Restart: `sudo systemctl restart gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp`.
4. Verify: `grep AuthCaptureMiddleware /opt/gbrain/services/*/server.py` — should match three files.
5. Verify no fallback: `grep MCP-FALLBACK-TOKEN /opt/gbrain/services/` — should be empty.
6. Re-run a smoke test from each agent's host. `audit_log.agent` should now reflect the correct identity.

---

## Q: Postgres won't start after install

**Symptoms:** `sudo systemctl status postgresql` shows `failed` or `inactive`.

**Diagnosis:**

```bash
sudo journalctl -u postgresql -n 200 --no-pager
sudo journalctl -u postgresql@16-main -n 200 --no-pager
```

Common causes:

- **Disk full.** `df -h`. Postgres refuses to start with `<5%` free on the data partition.
- **Port 5432 in use.** Another Postgres install, or a leftover from a previous attempt. `sudo ss -tlnp | grep 5432`.
- **`pg_hba.conf` syntax error.** Look for `FATAL` lines mentioning hba.conf. Restore from `/etc/postgresql/16/main/pg_hba.conf.bak.<ts>` if `install.sh` made one.
- **Locale missing.** Ubuntu minimal images sometimes lack `en_US.UTF-8`. `sudo locale-gen en_US.UTF-8`, then restart Postgres.

---

## Q: How do I add a new agent?

```bash
sudo -u gbrain python /opt/gbrain/scripts/issue-agent-token.py \
  --agent <new-agent-name> \
  --scopes 'comma,separated,scope,list'
```

Capture the printed token, add it to that agent's `.mcp.json` under each MCP server config:

```json
{
  "mcpServers": {
    "gbrain-memory": {
      "url": "https://mcp.example.com/memory/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    },
    "gbrain-recall": {
      "url": "https://mcp.example.com/recall/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    },
    "gbrain-swarm": {
      "url": "https://mcp.example.com/swarm/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Restart the agent. Confirm with one call: `recall.stats()` should succeed.

---

## Q: How do I revoke or rotate a token?

**Revoke:**

```bash
sudo -u gbrain python /opt/gbrain/scripts/issue-agent-token.py --agent <name> --revoke
```

**Rotate without downtime:** issue a new token, update the agent's `.mcp.json`, restart the agent, then revoke the old one. See `docs/security.md` "Token issuance and rotation" for the full procedure.

---

## Q: `compile.sh` fills the log with the same file repeatedly

**Cause:** the compile step is failing to write the `compiled_at` marker into the raw file's frontmatter, so the next cron run re-processes it.

**Fix:**

1. Check the actual error in the log: `tail -100 ${INBOX_AGENT_HOME}/logs/compile.log`.
2. Common causes:
   - The memory MCP call returned 403 (scope mismatch). Inbox-agent token needs `50-external` and `90-inbox` scopes.
   - The raw file is read-only (permission). `chmod u+w` it.
   - JSON-RPC parse error from a malformed response. Check the memory MCP service logs on the VPS.

3. Manual reset for one stuck file: edit it, add `compiled_at: skip` to frontmatter, save. The compile loop will skip it.

---

## Q: Daily digest is empty

**Symptoms:** the 07:00 digest arrives but contains no entries.

**Diagnosis:**

1. Was yesterday actually empty? `recall.recent(scope='50-external', limit=20)` from your coordinator agent should show yesterday's entries.
2. Time zone mismatch: `daily-digest.sh` uses the local box's TZ to compute "yesterday". If your box is in UTC but you live in UTC+8, "yesterday" starts and ends at unexpected hours. Set `TZ=` in the digest crontab line.
3. Recall token wrong: `tail ${INBOX_AGENT_HOME}/logs/digest.log` for 401.

---

## Q: Vault grew to 50GB and I want to prune old entries

**Recommended: archive, do not delete.**

1. Create a `vault/archive/<YYYY>/` folder.
2. Move old daily entries (`20-daily/`) and external entries (`50-external/`) more than 1 year old into it.
3. Drop them from the search index but keep markdown on disk:

   ```sql
   DELETE FROM chunks WHERE doc_id IN (SELECT id FROM documents WHERE path LIKE '${VAULT_ROOT}/archive/%');
   DELETE FROM documents WHERE path LIKE '${VAULT_ROOT}/archive/%';
   ```
4. Back up the archive folder elsewhere if you want.

Hard-deleting markdown is supported but irreversible. Always back up before pruning.

---

## Q: Services restart every few minutes (auto-restart loop)

**Diagnosis:** `systemctl status gbrain-<svc>-mcp` shows `activating (auto-restart)` repeatedly.

**Cause:** the service's main process is crashing on startup.

**Fix:**

1. `journalctl -u gbrain-<svc>-mcp -n 200 --no-pager` to find the actual crash.
2. The most common crashes:
   - Postgres connection refused — Postgres not up yet (`systemctl status postgresql`) or password wrong.
   - Module import error — venv corrupted or `requirements.txt` not installed. Reinstall: `sudo -u gbrain /opt/gbrain/.venv/bin/pip install -r /opt/gbrain/requirements.txt`.
   - Port already in use — another process grabbed 8767/8/6. `sudo ss -tlnp | grep 876` and kill it.
3. To stop the auto-restart while debugging: `sudo systemctl stop gbrain-<svc>-mcp`, then run the service manually: `sudo -u gbrain /opt/gbrain/.venv/bin/python -m services.<svc>.server` — you will see errors in your terminal directly.

---

## Q: `agent-template/install.sh` fails on macOS with a `sed` error

**Symptoms:** the installer aborts mid-template-rendering with `sed: -i: ...: invalid command code` or `sed: 1: ...: extra characters at the end`.

**Cause:** GNU `sed` (Linux) and BSD `sed` (macOS) differ in how `-i` (in-place edit) takes its backup argument. GNU accepts `sed -i 's/...'`, BSD requires `sed -i '' 's/...'`. A naive script that hardcodes one form breaks on the other.

**Fix:**

1. The shipped `agent-template/install.sh` uses a `sed_i` helper that picks the right form per platform. If you see a sed error, you may be running an older copy.
2. `head -50 agent-template/install.sh` and confirm a `sed_i()` function (or equivalent) is defined. If not, pull the latest distro.
3. As a one-off workaround, run the install on Linux (e.g. inside a fresh Ubuntu container) and then `rsync` the produced workspace to your Mac. The workspace itself is portable — only the install step is sed-sensitive.

---

## Q: I created an agent workspace but `recall` returns 0 results

**Symptoms:** `agent-template/install.sh` finished, the workspace at `~/.claude-lab/<agent-id>/.claude/` opens, but every `gbrain-recall.*` call returns an empty list — even though the brain has data (you can see entries from the inbox-agent).

**Diagnosis chain:**

1. **Bearer wrong.** Open `~/.claude-lab/<agent-id>/.claude/.mcp.json` and confirm the `Authorization: Bearer <token>` header is the actual token from `scripts/issue-agent-token.py --agent <agent-id> ...` — not the literal `<AGENT_BEARER>` placeholder, not the inbox-agent's token, not another agent's. Each agent has its own.
2. **Bearer not in DB.** On the VPS: `psql -U gbrain -d gbrain -c "SELECT agent, can_write_scopes, can_read_scopes, revoked_at FROM agent_tokens WHERE agent='<agent-id>';"`. `revoked_at` should be `NULL`. Read scope should include `*` (default) or the scope you are querying.
3. **Brain unreachable.** From the local workstation: `curl -sS -H "Authorization: Bearer <token>" https://<MCP_HOST>/recall/mcp/`. Expect HTTP 406 with an MCP error body — that proves both upstream and token are working. 401 → wrong token (or revoked). Connection refused → firewall, DNS, or Tailscale down.
4. **Scope mismatch.** A `recall.recent(scope='30-decisions')` call returns nothing if no agent has written to `30-decisions` yet. Try `recall.recent(scope='50-external')` to see at minimum the inbox-agent's forwards.
5. **Token issued but workspace cached the old config.** Restart the Claude Code session — `.mcp.json` is read on launch.

---

## Q: Multiple agents share `~/.claude/CLAUDE.md` global — is that a problem?

**Short answer:** no. That is by design.

**Long answer:** the file at `~/.claude/CLAUDE.md` is global Claude Code conventions (workflow, git, security rules, language preferences) that apply to **every** agent the user runs. It is read-only conventions, not per-agent state. All Path B workspaces include it via `@include` at the top of their own `CLAUDE.md`.

Per-agent specifics — the agent's role, owner, scope, hooks, memory layers — live inside the workspace at `~/.claude-lab/<agent-id>/.claude/`. None of that touches the global file.

If you want different conventions for different agents (e.g. one agent always answers in English, another in Russian), put the override in the workspace's own `CLAUDE.md`, not in the global one. The workspace SOUL wins over the global when they conflict — the agent reads its workspace `CLAUDE.md` after the global, so later instructions take precedence on a per-rule basis.

What you should not do: edit `~/.claude/CLAUDE.md` to add agent-specific rules ("when running as `coordinator-agent` do X"). Conditionals like that belong in the workspace, not in the global.

---

## Q: Memory rotation cron didn't run for my agent

**Symptoms:** `core/hot/recent.md` keeps growing forever and never rolls into `core/hot/archive/`. `core/warm/decisions.md` has entries older than 14 days that should have moved to `core/MEMORY.md`. `logs/trim-hot.log` is empty or missing.

**Diagnosis chain:**

1. **Crontab missing the entry.** `crontab -l | grep <agent-id>`. You should see three lines per agent (one each for `trim-hot.sh`, `rotate-warm.sh`, `compress-warm.sh`). If empty, the install never added them — re-add per the setup.md step 13 snippet.
2. **Wrong crontab user.** Memory-rotation crons must be installed in **your user's** crontab, not `root`'s. `whoami` and `crontab -l` should both reflect the user who owns `~/.claude-lab/<agent-id>/`. If the entries are in `sudo crontab -l` (root), move them: `sudo crontab -l | grep <agent-id>` then add to your own `crontab -e`.
3. **Wrong path in cron.** Cron runs with a minimal environment. If the entry references `$INBOX_AGENT_HOME` or `~` without expansion, it silently fails. Use absolute paths: `/Users/<you>/.claude-lab/<agent-id>/.claude/scripts/trim-hot.sh`, not `~/.claude-lab/.../scripts/trim-hot.sh`.
4. **Script not executable.** `ls -l ~/.claude-lab/<agent-id>/.claude/scripts/*.sh` — every script should have the `x` bit. If not, `chmod +x` them.
5. **No log entries at all.** Check `tail /var/log/syslog | grep CRON` (Linux) or `tail /var/log/system.log | grep cron` (macOS). If cron is running but the entry never fires, the schedule expression is malformed.
6. **Per-agent isolation.** Each workspace has its own cron entries. If you have 5 personal agents, you should see 15 lines in `crontab -l`. Do not consolidate them — keep them independent so one agent's failing rotation does not silently take down another's.

---

## Q: `agent-template/install.sh` re-run overwrites my customizations

**Symptoms:** you edited `~/.claude-lab/<agent-id>/.claude/CLAUDE.md` to refine the agent's SOUL, then re-ran `agent-template/install.sh` for that same `<agent-id>` to "update" something — and your edits are gone.

**Cause:** `install.sh` is **idempotent for first-time install only**. For an existing workspace, it does not merge — it re-renders templates over your live files.

**Fix:**

1. Restore from your most recent backup of the workspace (you backed up before re-running, right?).
2. If no backup, check `~/.claude-lab/<agent-id>/.claude/core/hot/recent.md` and `core/warm/decisions.md` — these are append-only and survive template re-renders.
3. For future updates: do **not** re-run `install.sh` against an existing workspace. To update the template files (e.g. pick up a new hook), copy only the specific file you want from `agent-template/templates/` after rendering it manually, or do a `diff` first.
4. To pick up a new memory-rotation script, just copy that one file into `~/.claude-lab/<agent-id>/.claude/scripts/` — no install needed.

---

## Q: Recall is slow (multiple seconds per query)

**Expected latency:** 100–500ms for vaults under 50k files on the recommended 4-vCPU/8-GB VPS. Multi-second latency means something is off.

**Diagnosis:**

1. `psql -U gbrain -d gbrain -c "\d chunks"` and `\d documents` — confirm an HNSW index on `chunks.embedding` and GIN indexes on `documents.body_tsv` / `chunks.content_tsv`.
2. `EXPLAIN ANALYZE` your recall query (look at `services/recall_mcp/search.py` for the actual SQL). If a sequential scan appears, indexes are missing.
3. Vault size: how many rows? `SELECT count(*) FROM documents;` and `SELECT count(*) FROM chunks;`. Over 100k documents (≈500k+ chunks) starts to push the 8 GB box.
4. Other processes on the VPS eating RAM: `htop`, look for whatever is hogging memory.

**Fix:**

- Rebuild indexes if missing: re-run `migrations/002_indexes.sql`.
- Tune HNSW: `m=16, ef_construction=64` is the default; for >50k rows consider `m=32`.
- If the vault is genuinely large, scale the VPS.
