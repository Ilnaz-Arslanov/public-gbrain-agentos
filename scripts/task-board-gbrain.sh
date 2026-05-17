#!/usr/bin/env bash
set -euo pipefail

# task-board-gbrain.sh -- CLI wrapper for gbrain task-mcp (port 8769).
# Drop-in replacement for orgbus-based task-board.sh.
#
# Usage:
#   task-board-gbrain.sh my-tasks --agent my-agent
#   task-board-gbrain.sh start TASK_ID --note "plan: ..."
#   task-board-gbrain.sh update TASK_ID --next-action "..." --last-result "..."
#   task-board-gbrain.sh review TASK_ID --note "done, PR #42"
#   task-board-gbrain.sh block TASK_ID --reason "waiting for X"
#   task-board-gbrain.sh create --title "..." [--assignee sa-X] [--priority high]
#   task-board-gbrain.sh get TASK_ID
#   task-board-gbrain.sh done TASK_ID --note "shipped"
#   task-board-gbrain.sh reopen TASK_ID --note "rejected"
#   task-board-gbrain.sh history TASK_ID
#   task-board-gbrain.sh heartbeat [--status online]

TASK_MCP_HOST="${TASK_MCP_HOST:-127.0.0.1}"
TASK_MCP_PORT="${TASK_MCP_PORT:-8769}"
TASK_MCP_URL="http://${TASK_MCP_HOST}:${TASK_MCP_PORT}/mcp"

if [[ -z "${GBRAIN_TOKEN:-}" ]]; then
    TOKEN_FILE="${HOME}/.secrets/gbrain-token"
    if [[ -f "$TOKEN_FILE" ]]; then
        GBRAIN_TOKEN="$(cat "$TOKEN_FILE")"
    else
        echo "ERROR: GBRAIN_TOKEN not set and $TOKEN_FILE not found" >&2
        exit 1
    fi
fi

_call_mcp() {
    local tool_name="$1"
    local args_json="$2"

    local response
    response=$(GBRAIN_TOKEN_ENV="$GBRAIN_TOKEN" python3 -c "
import json, os, sys, urllib.request, urllib.error

tool = sys.argv[1]
url = sys.argv[2]
token = os.environ['GBRAIN_TOKEN_ENV']
args = json.loads(sys.stdin.read())

payload = json.dumps({
    'jsonrpc': '2.0', 'id': 1,
    'method': 'tools/call',
    'params': {'name': tool, 'arguments': args}
}).encode()

req = urllib.request.Request(url, data=payload, headers={
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
    'Authorization': f'Bearer {token}',
})

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
except urllib.error.HTTPError as e:
    print(f'ERROR: HTTP {e.code}: {e.read().decode()[:200]}', file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as e:
    print(f'ERROR: {e.reason}', file=sys.stderr)
    sys.exit(1)

data_line = ''
for line in body.splitlines():
    if line.startswith('data: '):
        data_line = line[6:]
        break

if not data_line:
    print('ERROR: empty response', file=sys.stderr)
    sys.exit(1)

d = json.loads(data_line)
result = d.get('result', {})

if result.get('isError'):
    msg = result.get('content', [{}])[0].get('text', 'unknown error')
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)

sc = result.get('structuredContent')
if sc:
    json.dump(sc, sys.stdout, indent=2, ensure_ascii=False, default=str)
else:
    for c in result.get('content', []):
        txt = c.get('text', '')
        try:
            parsed = json.loads(txt)
            json.dump(parsed, sys.stdout, indent=2, ensure_ascii=False, default=str)
        except (json.JSONDecodeError, TypeError):
            sys.stdout.write(txt)
print()
" "$tool_name" "$TASK_MCP_URL" <<< "$args_json") || exit 1

    echo "$response"
}

_format_task_table() {
    python3 -c "
import sys, json

raw = sys.stdin.read().strip()
if not raw:
    print('No tasks found.')
    sys.exit(0)

data = json.loads(raw)
if isinstance(data, dict) and 'result' in data:
    data = data['result']
if not isinstance(data, list):
    data = [data]
if not data:
    print('No tasks found.')
    sys.exit(0)

fmt = '{:<5} {:<12} {:<14} {:<10} {:<40}'
print(fmt.format('ID', 'Status', 'Assignee', 'Priority', 'Title'))
print('-' * 85)
for t in data:
    title = (t.get('title','') or '')[:38]
    print(fmt.format(
        t.get('id','?'),
        t.get('status','?'),
        (t.get('assignee','') or '-')[:12],
        t.get('priority','?'),
        title,
    ))
print(f'\nTotal: {len(data)} tasks')
"
}

_json() {
    # Safe JSON builder. Pass key=value pairs as arguments.
    # Prefix value with i: for int, j: for nested JSON.
    python3 -c "
import json, sys
d = {}
for arg in sys.argv[1:]:
    k, _, v = arg.partition('=')
    if not k: continue
    if v.startswith('i:'):
        d[k] = int(v[2:])
    elif v.startswith('j:'):
        d[k] = json.loads(v[2:])
    else:
        d[k] = v
print(json.dumps(d))
" "$@"
}

_usage() {
    cat <<'EOF'
task-board-gbrain.sh -- gbrain task board CLI

Commands:
  my-tasks --agent AGENT        List tasks assigned to agent
  list [--status STATUS]        List all tasks
  get TASK_ID                   Get single task
  create --title "..." [opts]   Create new task
  start TASK_ID [--note "..."]  Start task (new->progress)
  review TASK_ID [--note "..."] Send to review (progress->review)
  done TASK_ID [--note "..."]   Mark done (review->done)
  block TASK_ID [--reason "..."] Block task
  reopen TASK_ID [--note "..."] Reopen task
  update TASK_ID [--next-action "..." --last-result "..."]
  history TASK_ID               Show status history
  heartbeat [--status online]   Send agent heartbeat

Environment:
  GBRAIN_TOKEN        Bearer token (or ~/.secrets/gbrain-token file)
  TASK_MCP_HOST       MCP host (default: 127.0.0.1)
  TASK_MCP_PORT       MCP port (default: 8769)
EOF
    exit 0
}

CMD="${1:-help}"
shift || true

case "$CMD" in
    my-tasks)
        AGENT="" STATUS=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --agent) AGENT="$2"; shift 2 ;;
                --status) STATUS="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        AGENT="${AGENT#sa-}"
        kv=()
        [[ -n "$AGENT" ]] && kv+=("assignee=sa-${AGENT}")
        [[ -n "$STATUS" ]] && kv+=("status=${STATUS}")
        _call_mcp "task_list" "$(_json "${kv[@]+"${kv[@]}"}")" | _format_task_table
        ;;

    list)
        STATUS="" ASSIGNEE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --status) STATUS="$2"; shift 2 ;;
                --assignee) ASSIGNEE="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        kv=()
        [[ -n "$STATUS" ]] && kv+=("status=${STATUS}")
        [[ -n "$ASSIGNEE" ]] && kv+=("assignee=${ASSIGNEE}")
        _call_mcp "task_list" "$(_json "${kv[@]+"${kv[@]}"}")" | _format_task_table
        ;;

    get)
        TASK_ID="${1:?task_id required}"
        _call_mcp "task_get" "$(_json "task_id=i:${TASK_ID}")"
        ;;

    create)
        TITLE="" DESC="" ASSIGNEE="" PRIORITY="medium"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --title) TITLE="$2"; shift 2 ;;
                --description) DESC="$2"; shift 2 ;;
                --assignee) ASSIGNEE="$2"; shift 2 ;;
                --priority) PRIORITY="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        [[ -z "$TITLE" ]] && { echo "ERROR: --title required" >&2; exit 1; }
        kv=("title=${TITLE}" "priority=${PRIORITY}")
        [[ -n "$DESC" ]] && kv+=("description=${DESC}")
        [[ -n "$ASSIGNEE" ]] && kv+=("assignee=${ASSIGNEE}")
        _call_mcp "task_create" "$(_json "${kv[@]}")"
        echo "Task created."
        ;;

    start)
        TASK_ID="${1:?task_id required}"; shift
        NOTE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --note) NOTE="$2"; shift 2 ;; *) shift ;; esac
        done
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$NOTE" ]] && kv+=("note=${NOTE}")
        _call_mcp "task_start" "$(_json "${kv[@]}")"
        ;;

    review)
        TASK_ID="${1:?task_id required}"; shift
        NOTE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --note) NOTE="$2"; shift 2 ;; *) shift ;; esac
        done
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$NOTE" ]] && kv+=("note=${NOTE}")
        _call_mcp "task_review" "$(_json "${kv[@]}")"
        ;;

    done)
        TASK_ID="${1:?task_id required}"; shift
        NOTE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --note) NOTE="$2"; shift 2 ;; *) shift ;; esac
        done
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$NOTE" ]] && kv+=("note=${NOTE}")
        _call_mcp "task_done" "$(_json "${kv[@]}")"
        ;;

    block)
        TASK_ID="${1:?task_id required}"; shift
        REASON=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --reason) REASON="$2"; shift 2 ;; *) shift ;; esac
        done
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$REASON" ]] && kv+=("reason=${REASON}")
        _call_mcp "task_block" "$(_json "${kv[@]}")"
        ;;

    reopen)
        TASK_ID="${1:?task_id required}"; shift
        NOTE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in --note) NOTE="$2"; shift 2 ;; *) shift ;; esac
        done
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$NOTE" ]] && kv+=("note=${NOTE}")
        _call_mcp "task_reopen" "$(_json "${kv[@]}")"
        ;;

    update)
        TASK_ID="${1:?task_id required}"; shift
        NEXT_ACTION="" LAST_RESULT="" TITLE="" DESC=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --next-action) NEXT_ACTION="$2"; shift 2 ;;
                --last-result) LAST_RESULT="$2"; shift 2 ;;
                --title) TITLE="$2"; shift 2 ;;
                --description) DESC="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        META_JSON=$(python3 -c "
import json, sys
meta = {}
if sys.argv[1]: meta['next_action'] = sys.argv[1]
if sys.argv[2]: meta['last_result'] = sys.argv[2]
print(json.dumps(meta) if meta else '')
" "$NEXT_ACTION" "$LAST_RESULT")
        kv=("task_id=i:${TASK_ID}")
        [[ -n "$TITLE" ]] && kv+=("title=${TITLE}")
        [[ -n "$DESC" ]] && kv+=("description=${DESC}")
        [[ -n "$META_JSON" ]] && kv+=("metadata=j:${META_JSON}")
        _call_mcp "task_update" "$(_json "${kv[@]}")"
        ;;

    history)
        TASK_ID="${1:?task_id required}"
        _call_mcp "task_history" "$(_json "task_id=i:${TASK_ID}")"
        ;;

    heartbeat)
        STATUS="online"
        while [[ $# -gt 0 ]]; do
            case "$1" in --status) STATUS="$2"; shift 2 ;; *) shift ;; esac
        done
        _call_mcp "agent_heartbeat" "$(_json "status=${STATUS}")"
        ;;

    help|--help|-h)
        _usage
        ;;

    *)
        echo "Unknown command: $CMD" >&2
        _usage
        ;;
esac
