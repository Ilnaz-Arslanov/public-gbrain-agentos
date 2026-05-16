#!/usr/bin/env bash
set -euo pipefail

# inbox-agent daily digest: collect compiled items from last 24-26h, ask Sonnet
# for an HTML digest, send to your Telegram via Bot API sendMessage.

: "${INBOX_AGENT_HOME:?INBOX_AGENT_HOME must be set, e.g. ~/.claude-lab/inbox-agent}"
: "${PRINCE_CHAT_ID:?PRINCE_CHAT_ID must be set (your Telegram user id)}"
: "${CLAUDE_BIN:=$HOME/.local/bin/claude}"
: "${TG_BOT_TOKEN_FILE:=${INBOX_AGENT_HOME}/secrets/telegram-bot-token}"

COMPILED_DIR="${INBOX_AGENT_HOME}/compiled"
LOG="${INBOX_AGENT_HOME}/logs/digest.log"
PROMPT_FILE="${INBOX_AGENT_HOME}/prompts/digest.prompt.md"

mkdir -p "$(dirname "$LOG")"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

[ ! -f "$TG_BOT_TOKEN_FILE" ] && { log "no telegram token at $TG_BOT_TOKEN_FILE"; exit 1; }
[ ! -x "$CLAUDE_BIN" ] && { log "claude CLI not found"; exit 1; }
[ ! -d "$COMPILED_DIR" ] && { log "no compiled dir, nothing to digest"; exit 0; }

TG_TOKEN=$(cat "$TG_BOT_TOKEN_FILE")

# Files modified in last 26h
RECENT=$(find "$COMPILED_DIR" -type f -name "*.md" -newermt "26 hours ago" 2>/dev/null || true)
if [ -z "$RECENT" ]; then
    log "no new items — sending heartbeat digest"
    HEARTBEAT="No items in last 26h. Inbox-agent is alive."
    curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${PRINCE_CHAT_ID}" \
        --data-urlencode "text=${HEARTBEAT}" >> "$LOG" 2>&1
    exit 0
fi

COUNT=$(echo "$RECENT" | wc -l | tr -d ' ')

# Aggregate
AGGREGATE="## Compiled items from last 24h"$'\n\n'
while IFS= read -r F; do
    [ -z "$F" ] && continue
    AGGREGATE="$AGGREGATE"$'\n'"--- $(basename "$F") ---"$'\n'"$(cat "$F")"$'\n'
done <<< "$RECENT"

DEFAULT_PROMPT='You are an inbox digest writer.
The materials below were forwarded to your inbox in the last 24 hours and
auto-processed.
Write a daily digest in the user'\''s native language (default English):

- Opening line: how many items, what types
- For each notable item: 1-line summary + link (use <a href="...">...</a>)
- Bottom: 2-3 key themes across items

Format: Telegram HTML only (<b>, <i>, <a>, <pre>). No markdown tables. No emoji.
Max 3500 chars.'

if [ -f "$PROMPT_FILE" ]; then
    BASE_PROMPT=$(cat "$PROMPT_FILE")
else
    BASE_PROMPT="$DEFAULT_PROMPT"
fi

PROMPT="${BASE_PROMPT}

--- MATERIALS ---
$AGGREGATE"

DIGEST=$(echo "$PROMPT" | timeout 120 "$CLAUDE_BIN" --model sonnet --print --output-format text 2>>"$LOG") || {
    log "digest gen failed"
    exit 1
}

if [ -z "$DIGEST" ]; then
    log "empty digest"
    exit 1
fi

# Telegram has 4096 char limit; trim defensively. Use python to slice by
# characters, not bytes — avoids cutting UTF-8 mid-codepoint.
DIGEST_TRIMMED=$(printf '%s' "$DIGEST" | python3 -c "import sys; print(sys.stdin.read()[:3900])")

curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${PRINCE_CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${DIGEST_TRIMMED}" >> "$LOG" 2>&1

log "digest sent (items=$COUNT)"
exit 0
