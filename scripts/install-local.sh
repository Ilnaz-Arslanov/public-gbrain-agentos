#!/usr/bin/env bash
# install-local.sh — set up the inbox-agent on a developer machine (Mac or Linux).
#
# Usage:
#   bash scripts/install-local.sh
#
# Steps:
#   1. Create ~/.claude-lab/your-agent/ skeleton
#   2. Copy inbox-agent/ tree
#   3. Render .mcp.json from the local .env
#   4. Prompt for BotFather token (or read $TELEGRAM_BOT_TOKEN env)
#   5. Install crontab entries (optional, with confirmation)

set -euo pipefail

log()  { printf '[install-local %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die()  { printf '[install-local ERROR] %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

: "${INBOX_AGENT_HOME:=$HOME/.claude-lab/inbox-agent}"
: "${MCP_HOST:=}"
: "${INBOX_BEARER:=}"
: "${TELEGRAM_BOT_TOKEN:=}"
: "${PRINCE_CHAT_ID:=}"

log "INBOX_AGENT_HOME=$INBOX_AGENT_HOME"

if [ ! -d "$REPO_ROOT/inbox-agent" ]; then
  die "$REPO_ROOT/inbox-agent does not exist — Batch B not landed yet?"
fi

mkdir -p \
  "$INBOX_AGENT_HOME" \
  "$INBOX_AGENT_HOME/.claude" \
  "$INBOX_AGENT_HOME/hooks" \
  "$INBOX_AGENT_HOME/scripts" \
  "$INBOX_AGENT_HOME/config" \
  "$INBOX_AGENT_HOME/prompts" \
  "$INBOX_AGENT_HOME/raw" \
  "$INBOX_AGENT_HOME/compiled" \
  "$INBOX_AGENT_HOME/logs"

# ---------------------------------------------------------------------------
# Copy skeleton (don't overwrite hand-edited files)
# ---------------------------------------------------------------------------

log "copying inbox-agent skeleton"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --ignore-existing "$REPO_ROOT/inbox-agent/" "$INBOX_AGENT_HOME/"
else
  cp -an "$REPO_ROOT/inbox-agent/." "$INBOX_AGENT_HOME/"
fi

chmod +x "$INBOX_AGENT_HOME"/hooks/*.sh 2>/dev/null || true
chmod +x "$INBOX_AGENT_HOME"/scripts/*.sh 2>/dev/null || true

# ---------------------------------------------------------------------------
# .mcp.json render
# ---------------------------------------------------------------------------

MCP_JSON_TPL="$INBOX_AGENT_HOME/config/.mcp.json.template"
MCP_JSON_OUT="$INBOX_AGENT_HOME/.claude/.mcp.json"

if [ -f "$MCP_JSON_TPL" ]; then
  if [ -z "$MCP_HOST" ]; then
    read -r -p "MCP host base URL (e.g. https://mcp.example.com): " MCP_HOST
  fi
  if [ -z "$INBOX_BEARER" ]; then
    read -r -p "Bearer token for inbox-agent (one token, used for memory/recall/swarm): " INBOX_BEARER
  fi

  mkdir -p "$INBOX_AGENT_HOME/.claude"
  # shellcheck disable=SC2016
  # envsubst whitelist must be a literal string with $VAR placeholders.
  MCP_HOST="$MCP_HOST" INBOX_BEARER="$INBOX_BEARER" \
    envsubst '${MCP_HOST} ${INBOX_BEARER}' < "$MCP_JSON_TPL" > "$MCP_JSON_OUT"
  chmod 600 "$MCP_JSON_OUT"
  log "rendered $MCP_JSON_OUT"
else
  log "WARNING: $MCP_JSON_TPL missing — skip .mcp.json render"
fi

# ---------------------------------------------------------------------------
# Telegram bot token
# ---------------------------------------------------------------------------

SECRETS_DIR="$INBOX_AGENT_HOME/secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo
  echo "You need a Telegram bot for the inbox-agent."
  echo "  1. Open https://t.me/BotFather"
  echo "  2. /newbot, name it, copy the HTTP API token."
  echo
  read -r -p "Paste bot token (or leave empty to skip): " TELEGRAM_BOT_TOKEN
fi

if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  printf '%s\n' "$TELEGRAM_BOT_TOKEN" > "$SECRETS_DIR/telegram-bot-token"
  chmod 600 "$SECRETS_DIR/telegram-bot-token"
  log "telegram bot token saved to $SECRETS_DIR/telegram-bot-token"
fi

if [ -z "$PRINCE_CHAT_ID" ]; then
  read -r -p "Your Telegram chat id (numeric, where digests will arrive): " PRINCE_CHAT_ID
fi

if [ -n "$PRINCE_CHAT_ID" ]; then
  printf 'PRINCE_CHAT_ID=%s\n' "$PRINCE_CHAT_ID" >> "$INBOX_AGENT_HOME/.env"
  log "PRINCE_CHAT_ID saved to $INBOX_AGENT_HOME/.env"
fi

# ---------------------------------------------------------------------------
# Crontab (opt-in)
# ---------------------------------------------------------------------------

CRON_TPL="$INBOX_AGENT_HOME/scripts/crontab.example"
if [ -f "$CRON_TPL" ]; then
  echo
  echo "Crontab template at $CRON_TPL:"
  echo "---"
  cat "$CRON_TPL"
  echo "---"
  read -r -p "Install these cron entries now? [y/N] " ans
  case "$ans" in
    y|Y|yes)
      tmp="$(mktemp)"
      crontab -l 2>/dev/null > "$tmp" || true
      # Idempotent append: only add lines that aren't already there
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        [[ "$line" =~ ^# ]] && continue
        if ! grep -Fqx "$line" "$tmp"; then
          printf '%s\n' "$line" >> "$tmp"
        fi
      done < "$CRON_TPL"
      crontab "$tmp"
      rm -f "$tmp"
      log "crontab installed"
      ;;
    *)
      log "crontab skipped — install manually with: crontab -e"
      ;;
  esac
fi

log "local setup done. Workspace: $INBOX_AGENT_HOME"
