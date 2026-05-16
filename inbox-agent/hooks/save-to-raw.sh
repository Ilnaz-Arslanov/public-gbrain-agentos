#!/usr/bin/env bash
set -euo pipefail

# inbox-agent dual-write hook
# Writes incoming forwards to:
#   1) Local MD: ${INBOX_AGENT_HOME}/raw/YYYY-MM/<type>/<slug>.md
#   2) Shared gbrain via memory-mcp.create_external_note
# Called by gateway. Args: $1=user_text $2=agent_response $3=source_tag
# Never blocks: exits 0 on partial failure.

USER_TEXT="${1:-}"
AGENT_RESPONSE="${2:-}"
SOURCE_TAG="${3:-forward}"

[ -z "$USER_TEXT" ] && exit 0

: "${INBOX_AGENT_HOME:?INBOX_AGENT_HOME must be set, e.g. ~/.claude-lab/inbox-agent}"
: "${MCP_URL:=https://mcp.example.com/memory/mcp}"

RAW_BASE="${INBOX_AGENT_HOME}/raw"
LOG="${INBOX_AGENT_HOME}/logs/save-to-raw.log"
MCP_JSON="${INBOX_AGENT_HOME}/.claude/.mcp.json"

mkdir -p "$(dirname "$LOG")"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

TS=$(date +%s)
DATE_UTC=$(date -u +%Y-%m-%d)
MONTH=$(date -u +%Y-%m)

# Extract first URL
URL=$(echo "$USER_TEXT" | grep -oE 'https?://[^[:space:]]+' | head -1 || true)

# Classify
TYPE="text"
CATEGORY="reference"
TAGS_CSV="research"

if [[ "$URL" == *"youtube.com"* ]] || [[ "$URL" == *"youtu.be"* ]]; then
    TYPE="youtube"; CATEGORY="article"; TAGS_CSV="content,youtube"
elif [[ "$URL" == *"instagram.com"* ]]; then
    TYPE="instagram"; CATEGORY="article"; TAGS_CSV="instagram,content"
elif [[ "$URL" == *"twitter.com"* ]] || [[ "$URL" == *"x.com"* ]]; then
    TYPE="twitter"; CATEGORY="article"; TAGS_CSV="twitter,content"
elif [[ "$URL" == *"reddit.com"* ]]; then
    TYPE="web"; CATEGORY="reference"; TAGS_CSV="reddit,research"
elif [ -n "$URL" ]; then
    TYPE="web"; CATEGORY="reference"; TAGS_CSV="web,research"
else
    TYPE="text"; CATEGORY="learning"; TAGS_CSV="text,research"
fi

# Slug
if [ -n "$URL" ]; then
    SLUG=$(echo "$URL" | sed 's|https\?://||; s|www\.||; s|[^a-zA-Z0-9]|-|g; s|-\+|-|g; s|^-||; s|-$||' | cut -c1-60)
    DOMAIN=$(echo "$URL" | sed -E 's|https?://([^/]+).*|\1|' | sed 's|^www\.||')
else
    SLUG="text-${TS}"
    DOMAIN=""
fi

# Title heuristic
case "$TYPE" in
    youtube|instagram|twitter)
        TITLE="Forward from ${DOMAIN}: ${SLUG}"
        ;;
    web)
        TITLE="${SLUG}"
        ;;
    text)
        TITLE=$(echo "$USER_TEXT" | tr '\n' ' ' | cut -c1-80)
        [ -z "$TITLE" ] && TITLE="text-${TS}"
        ;;
    *)
        TITLE="${SLUG}"
        ;;
esac

# Build tag JSON array from CSV
TAGS_JSON=$(echo "$TAGS_CSV" | python3 -c "
import sys, json
csv = sys.stdin.read().strip()
tags = [t.strip() for t in csv.split(',') if t.strip()]
print(json.dumps(tags))
")

# Body (markdown). Note: use *** as section separator, NOT --- (avoids breaking
# frontmatter strippers like compile.sh that match /^---$/).
BODY=$(cat <<BODY_EOF
## User forward
${USER_TEXT}

***

## Agent response
${AGENT_RESPONSE}
BODY_EOF
)

# 1) Local write
TARGET_DIR="$RAW_BASE/$MONTH/$TYPE"
mkdir -p "$TARGET_DIR"
TARGET_FILE="$TARGET_DIR/${SLUG}.md"

# Frontmatter tags as YAML inline list
TAGS_YAML=$(echo "$TAGS_CSV" | python3 -c "
import sys
csv = sys.stdin.read().strip()
tags = [t.strip() for t in csv.split(',') if t.strip()]
print('[' + ', '.join(tags) + ']')
")

{
  echo "---"
  echo "source: ${URL:-text}"
  echo "type: ${TYPE}"
  echo "category: ${CATEGORY}"
  echo "tags: ${TAGS_YAML}"
  echo "date: ${DATE_UTC}"
  echo "ts: ${TS}"
  echo "source_tag: ${SOURCE_TAG}"
  echo "compiled: false"
  echo "---"
  echo ""
  echo "# ${TITLE}"
  echo ""
  echo "${BODY}"
} > "$TARGET_FILE"

log "local write ok: $TARGET_FILE (type=$TYPE)"

# 2) memory-mcp create_external_note (parallel, non-blocking)

if [ ! -f "$MCP_JSON" ]; then
    log "WARN: no .mcp.json at $MCP_JSON; skipping gbrain write"
    exit 0
fi

# Extract bearer token (raw, without "Bearer " prefix). Re-add at curl call.
RAW_TOKEN=$(MCP_JSON_PATH="$MCP_JSON" python3 - <<'PY' 2>/dev/null || true
import json, os
try:
    with open(os.environ["MCP_JSON_PATH"]) as f:
        d = json.load(f)
    # Try common key names; first match wins.
    candidates = ["gbrain-memory", "memory", "memory-mcp"]
    auth = None
    for k in candidates:
        if k in d.get("mcpServers", {}):
            auth = d["mcpServers"][k]["headers"]["Authorization"]
            break
    if auth and auth.startswith("Bearer "):
        auth = auth[7:]
    if auth:
        print(auth)
except Exception:
    pass
PY
)

if [ -z "$RAW_TOKEN" ]; then
    log "WARN: failed to extract memory-mcp bearer; skipping gbrain"
    exit 0
fi

# Build JSON-RPC payload (proper escaping via python with env vars)
PAYLOAD=$(URL_E="$URL" TYPE_E="$TYPE" TITLE_E="$TITLE" BODY_E="$BODY" TAGS_JSON_E="$TAGS_JSON" python3 <<'PY'
import json, os
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "create_external_note",
        "arguments": {
            "source": os.environ["TYPE_E"],
            "url": os.environ["URL_E"],
            "title": os.environ["TITLE_E"],
            "body": os.environ["BODY_E"],
            "tags": json.loads(os.environ["TAGS_JSON_E"]),
        }
    }
}
print(json.dumps(payload))
PY
)

if [ -z "$PAYLOAD" ]; then
    log "WARN: failed to build memory-mcp payload"
    exit 0
fi

# Initialize session, then call tools/call. memory-mcp is streamable-http.
# Capture response (SSE or JSON) and grep result.
RESPONSE=$(curl -sS -m 15 -X POST "$MCP_URL" \
    -H "Authorization: Bearer $RAW_TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    --data "$PAYLOAD" 2>>"$LOG") || {
        log "WARN: memory-mcp curl failed for $SLUG"
        exit 0
    }

# Check for error in response
if echo "$RESPONSE" | grep -qE '"error"[[:space:]]*:[[:space:]]*\{'; then
    # Sanitize: don't log token, but log error body trimmed
    ERR_SNIPPET=$(echo "$RESPONSE" | tr '\n' ' ' | cut -c1-500)
    log "WARN: memory-mcp error for $SLUG: $ERR_SNIPPET"
    exit 0
fi

log "gbrain write ok: $SLUG (type=$TYPE)"
exit 0
