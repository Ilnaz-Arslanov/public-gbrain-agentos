# inbox-agent

A small local agent — its own Telegram bot + a dual-write hook — that turns
"forward a link to a chat" into "the link is in my local raw store AND in my
shared knowledge base, searchable via recall-mcp".

## Architecture

```
Telegram client
     |
     v
[ bot.py ] ----- /start, /status (handles commands)
     |
     | every message (text or caption)
     v
[ hooks/save-to-raw.sh ]
     |                              \
     | (1) local write              | (2) memory-mcp.create_external_note
     v                              v
~/.claude-lab/inbox-agent/      gbrain VPS — scope=50-external
    raw/YYYY-MM/<type>/<slug>.md
```

The bot REPLIES on every message so you always know the system is alive:

1. `Got it, processing...` (immediate ack)
2. `Saved to knowledge base. Type: youtube, slug: ...` (after hook returns)
   or `Error (rc=N): <last stderr line>` (on failure)

Then two cron jobs run later:

| Script | When | What |
|---|---|---|
| `scripts/compile.sh` | every 15 min | finds raw entries with `compiled: false`, runs Sonnet to produce TL;DR + takeaways + tags, writes to `compiled/`, flips flag |
| `scripts/daily-digest.sh` | 09:00 UTC | aggregates last 26h of compiled entries, asks Sonnet for an HTML digest, sends it to your Telegram |

## Components

| File | Role |
|---|---|
| `bot.py` | Long-polling Telegram bot daemon. Acks every forward, calls the hook. |
| `requirements.txt` | `python-telegram-bot>=20.0` for the bot. |
| `inbox-bot.service.template` | Optional systemd user unit to run `bot.py`. |
| `hooks/save-to-raw.sh` | Dual-write logic (local + memory-mcp). |
| `scripts/compile.sh` | Sonnet summariser cron. |
| `scripts/daily-digest.sh` | Telegram digest cron. |
| `scripts/crontab.example` | Sample cron lines. |
| `config/.mcp.json.template` | Wire the bearer + MCP host into `.claude/.mcp.json`. |
| `config/classifier.yaml` | URL → type mapping (informational; rules baked into hook). |
| `config/digest-template.html` | Minimal HTML wrapper for `daily-digest.sh`. |
| `prompts/compile.prompt.md` | Sonnet prompt for raw → compiled. |
| `prompts/digest.prompt.md` | Sonnet prompt for daily digest. |

## Required environment

```sh
export INBOX_AGENT_HOME="$HOME/.claude-lab/inbox-agent"
export MCP_HOST="https://mcp.example.com"
export PRINCE_CHAT_ID="<your_telegram_user_id>"      # 0 disables allowlist
export TG_BOT_TOKEN_FILE="${INBOX_AGENT_HOME}/secrets/telegram-bot-token"
export CLAUDE_BIN="$HOME/.local/bin/claude"
```

## Canonical paths

These three paths are wired through `bot.py`, `save-to-raw.sh`, and
`daily-digest.sh`. Don't drift.

| Path | Used by |
|---|---|
| `${INBOX_AGENT_HOME}/.claude/.mcp.json` | `save-to-raw.sh` reads bearer from this; `install-local.sh` renders here. |
| `${INBOX_AGENT_HOME}/secrets/telegram-bot-token` | `bot.py` + `daily-digest.sh` read; `install-local.sh` writes. |
| `${INBOX_AGENT_HOME}/raw/YYYY-MM/<type>/<slug>.md` | hook writes; compile.sh reads. |
| `${INBOX_AGENT_HOME}/compiled/<type>/<slug>.md` | compile.sh writes; digest reads. |

## Install

```sh
# 1. Run the helper from the repo root — it scaffolds the workspace,
#    prompts for MCP_HOST + INBOX_BEARER + Telegram bot token, renders
#    .claude/.mcp.json, optionally installs cron lines.
bash scripts/install-local.sh

# 2. Install bot deps in the workspace's venv (or your global Python).
pip install -r inbox-agent/requirements.txt

# 3. Start the bot:
#    - quick test:
python3 "$INBOX_AGENT_HOME/bot.py"
#    - or as a service (Linux systemd user unit):
cp "$INBOX_AGENT_HOME/inbox-bot.service.template" \
   ~/.config/systemd/user/inbox-bot.service
# edit {{INBOX_AGENT_HOME}}, {{PRINCE_CHAT_ID}}, {{PYTHON_BIN}} in that file,
# then:
systemctl --user daemon-reload
systemctl --user enable --now inbox-bot.service
```

## How the bearer is used

`config/.mcp.json.template` uses two placeholders: `${MCP_HOST}` and
`${INBOX_BEARER}`. We use ONE bearer for all three MCP services
(`memory`, `recall`, `swarm`) because in the recommended deployment the same
agent token has the right scopes for all three. `install-local.sh` runs
`envsubst '${MCP_HOST} ${INBOX_BEARER}'` to render the file — only these two
vars are substituted, so any other `$VAR` in the template is left intact.

If you need different bearers per service, edit `.claude/.mcp.json` by hand
after the render.

## Allowlist

`bot.py` reads `PRINCE_CHAT_ID`. If it's a non-zero integer, only that
Telegram user id may forward messages and receive replies. Set to `0` to
disable the allowlist (not recommended — your bot URL is effectively public).

## Categories

The default classifier sorts forwards into 6 types: `youtube`, `instagram`,
`twitter`, `web` (reddit + generic URL), `text` (no URL). The mapping lives
in `hooks/save-to-raw.sh`. `config/classifier.yaml` is reference-only — edit
the bash if you add a source.

## Non-goals

- This agent does not run web scrapers itself — `skills/` does that. The
  compile step can shell out to skills if you wire them in.
- It does not own credentials for your main coordinator agent.
