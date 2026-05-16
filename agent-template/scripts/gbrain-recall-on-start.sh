#!/usr/bin/env bash
set -euo pipefail

# gbrain-recall-on-start.sh -- session-start helper
# Pulls top-N recalls from shared gbrain MCP that match the last handoff topic,
# prepends a summary block to core/hot/recent.md.
#
# Replaces the upstream session-sync script from public-architecture-claude-code.
#
# Usage:   bash scripts/gbrain-recall-on-start.sh
# Env:
#   AGENT_WORKSPACE  -- absolute path to .claude workspace; default: derive from script path
#   AGENT_ID         -- short id; default: derived from workspace parent dir
#   MCP_HOST         -- e.g. https://mcp.example.com or http://127.0.0.1:8767
#   AGENT_BEARER     -- bearer token for gbrain MCPs
#   RECALL_LIMIT     -- top-N matches to retrieve (default 5)
#
# Non-blocking: prints warnings, exits 0 on any non-fatal failure.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="${AGENT_WORKSPACE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AGENT_ID="${AGENT_ID:-$(basename "$(dirname "$WS")")}"
LOGDIR="${HOME}/.claude-lab/${AGENT_ID}/logs"
LOG="$LOGDIR/gbrain-recall.log"
HANDOFF="$WS/core/hot/handoff.md"
RECENT="$WS/core/hot/recent.md"
LIMIT="${RECALL_LIMIT:-5}"

mkdir -p "$LOGDIR" "$(dirname "$RECENT")"
touch "$RECENT"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >> "$LOG"; }

log "=== gbrain-recall-on-start.sh START ==="

if [ -z "${MCP_HOST:-}" ] || [ -z "${AGENT_BEARER:-}" ]; then
    log "MCP_HOST or AGENT_BEARER unset; skipping recall"
    exit 0
fi

# 1) Build query from last handoff or last 200 chars of recent.md
QUERY=""
if [ -f "$HANDOFF" ] && [ -s "$HANDOFF" ]; then
    # Take last 3 non-empty lines
    QUERY=$(grep -vE '^[[:space:]]*$' "$HANDOFF" | tail -n 3 | tr '\n' ' ' | head -c 300)
fi
if [ -z "$QUERY" ] && [ -f "$RECENT" ] && [ -s "$RECENT" ]; then
    QUERY=$(tail -n 5 "$RECENT" | tr '\n' ' ' | head -c 300)
fi
if [ -z "$QUERY" ]; then
    log "No handoff/recent content; nothing to query"
    exit 0
fi

# Strip markdown / shell-special chars for safer JSON embedding
QUERY=$(echo "$QUERY" | tr -d '\r' | sed 's/[`"\\]/ /g' | tr -s ' ' | head -c 250)
log "Query: $QUERY"

# 2) Build JSON-RPC payload via python (safe escaping)
PAYLOAD=$(QUERY_E="$QUERY" LIMIT_E="$LIMIT" python3 - <<'PY'
import json, os
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "recall",
        "arguments": {
            "query": os.environ["QUERY_E"],
            "limit": int(os.environ["LIMIT_E"]),
        },
    },
}
print(json.dumps(payload))
PY
)

# 3) POST to gbrain recall MCP
RESPONSE=$(curl -sS -m 15 -X POST "${MCP_HOST%/}/recall/mcp" \
    -H "Authorization: Bearer ${AGENT_BEARER}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    --data "$PAYLOAD" 2>>"$LOG") || {
        log "WARN: recall curl failed"
        exit 0
    }

if echo "$RESPONSE" | grep -qE '"error"[[:space:]]*:[[:space:]]*\{'; then
    SNIPPET=$(echo "$RESPONSE" | tr '\n' ' ' | cut -c1-400)
    log "WARN: recall returned error: $SNIPPET"
    exit 0
fi

# 4) Parse: extract `content[].text` from MCP result. Streamable-http may return
#    SSE-style `data: {...}` lines; pull the JSON payload either way.
HITS=$(RESPONSE_E="$RESPONSE" python3 - <<'PY'
import json, os, re
raw = os.environ["RESPONSE_E"]
# If SSE, isolate last data: line
data_lines = [m.group(1) for m in re.finditer(r'^data:\s*(\{.*\})\s*$', raw, re.M)]
candidate = data_lines[-1] if data_lines else raw
try:
    obj = json.loads(candidate)
except Exception:
    raise SystemExit(0)
result = obj.get("result") or {}
items = []
# Different shapes: result.content[].text (MCP standard) or result.items[]
if isinstance(result.get("content"), list):
    for c in result["content"]:
        t = c.get("text") if isinstance(c, dict) else None
        if t:
            items.append(t.strip())
elif isinstance(result.get("items"), list):
    for it in result["items"]:
        if isinstance(it, dict):
            t = it.get("text") or it.get("body") or it.get("title")
            if t:
                items.append(str(t).strip())
for it in items:
    # Truncate each to 200 chars
    print("- " + it.replace("\n", " ")[:200])
PY
)

if [ -z "$HITS" ]; then
    log "No hits"
    exit 0
fi

COUNT=$(echo "$HITS" | grep -c '^- ' || echo 0)
TS=$(date -u +%Y-%m-%d\ %H:%M)

# 5) Prepend block to recent.md
TMP=$(mktemp)
{
    echo "### ${TS} [gbrain-recall]"
    echo ""
    echo "Session start -- relevant gbrain recalls (${COUNT}):"
    echo ""
    echo "$HITS"
    echo ""
    if [ -s "$RECENT" ]; then
        cat "$RECENT"
    fi
} > "$TMP"
mv "$TMP" "$RECENT"

log "Prepended ${COUNT} recall hits to $RECENT"
log "=== gbrain-recall-on-start.sh DONE ==="
