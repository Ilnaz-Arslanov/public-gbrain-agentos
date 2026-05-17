#!/usr/bin/env bash
# smoke-test.sh — verify the four MCP services answer `tools/list` over HTTP.
#
# Usage:
#   bash scripts/smoke-test.sh                    # uses localhost ports
#   MCP_BASE=https://mcp.example.com bash ...     # uses domain (Caddy path routing)
#
# Reads admin token from $INSTALL_DIR/secrets/admin.token if present,
# else $ADMIN_TOKEN env var. Token is masked in all output.

set -euo pipefail

log()  { printf '[smoke %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { printf '[smoke FAIL] %s\n' "$*" >&2; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

: "${INSTALL_DIR:=/opt/gbrain}"
: "${MCP_BASE:=}"
: "${MCP_MEMORY_PORT:=8767}"
: "${MCP_RECALL_PORT:=8768}"
: "${MCP_SWARM_PORT:=8766}"
: "${MCP_TASK_PORT:=8769}"
: "${ADMIN_TOKEN:=}"

# Load admin token from file if not set
if [ -z "$ADMIN_TOKEN" ] && [ -r "$INSTALL_DIR/secrets/admin.token" ]; then
  ADMIN_TOKEN="$(tr -d '\n' < "$INSTALL_DIR/secrets/admin.token")"
fi

if [ -z "$ADMIN_TOKEN" ]; then
  log "no admin token available — running unauthenticated (expect 401 from services)"
  AUTH_HDR=()
else
  AUTH_HDR=(-H "Authorization: Bearer ${ADMIN_TOKEN}")
fi

# Build endpoint list: domain mode or localhost ports
if [ -n "$MCP_BASE" ]; then
  ENDPOINTS=(
    "memory:${MCP_BASE}/memory/mcp"
    "recall:${MCP_BASE}/recall/mcp"
    "swarm:${MCP_BASE}/swarm/mcp"
    "task:${MCP_BASE}/task/mcp"
  )
else
  ENDPOINTS=(
    "memory:http://127.0.0.1:${MCP_MEMORY_PORT}/mcp"
    "recall:http://127.0.0.1:${MCP_RECALL_PORT}/mcp"
    "swarm:http://127.0.0.1:${MCP_SWARM_PORT}/mcp"
    "task:http://127.0.0.1:${MCP_TASK_PORT}/mcp"
  )
fi

failures=0
for ep in "${ENDPOINTS[@]}"; do
  name="${ep%%:*}"
  url="${ep#*:}"

  body='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

  resp=$(curl -sS \
    --max-time 8 \
    -w '\n__HTTP__:%{http_code}' \
    -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    "${AUTH_HDR[@]}" \
    -d "$body" 2>&1 || true)

  http_code="${resp##*__HTTP__:}"
  payload="${resp%__HTTP__:*}"

  # Mask any accidental token echo
  masked_payload="$(printf '%s' "$payload" | sed 's/Bearer [A-Za-z0-9_-]\{20,\}/Bearer ***/g')"

  if [ "$http_code" != "200" ]; then
    fail "$name $url http=$http_code"
    printf '  response: %s\n' "$(printf '%s' "$masked_payload" | head -c 200)"
    failures=$((failures + 1))
    continue
  fi

  # Look for non-empty tools array — works for both SSE and plain JSON responses
  if printf '%s' "$payload" | grep -qE '"tools"\s*:\s*\['; then
    if printf '%s' "$payload" | grep -qE '"name"\s*:\s*"[^"]+"'; then
      log "$name OK ($url)"
    else
      fail "$name returned empty tools list ($url)"
      failures=$((failures + 1))
    fi
  else
    fail "$name response missing 'tools' field ($url)"
    printf '  response: %s\n' "$(printf '%s' "$masked_payload" | head -c 200)"
    failures=$((failures + 1))
  fi
done

if [ "$failures" -eq 0 ]; then
  log "all 4 services healthy"
  exit 0
fi

fail "$failures service(s) failed"
exit 1
