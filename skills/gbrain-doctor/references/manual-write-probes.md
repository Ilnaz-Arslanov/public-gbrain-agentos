# Manual write probes (operator-approved, NOT run by the doctor)

These probes **mutate gbrain state**. The doctor never runs them: the default doctor run is
read-only, and `--fix` only does local chmod/symlink/`gh repo create`. Run these by hand only when
an operator explicitly accepts a side effect (a memory note, an outbox delivery row).

They model the verified streamable-http JSON-RPC pattern from
`scripts/task-board-gbrain.sh::_call_mcp`: a single stateless POST to the `/<service>/mcp` URL with
`Content-Type: application/json`, `Accept: application/json, text/event-stream`, and
`Authorization: Bearer <token>`. No `initialize` handshake. The response may be plain JSON or an
SSE `data:` frame.

## Prerequisites

```bash
# Raw Bearer for this agent. Never echo it into a shared chat.
export GBRAIN_TOKEN="$(cat ~/.secrets/gbrain-token)"

# Endpoints — take the exact URLs from this agent's .mcp.json.
export MEMORY_URL="https://mcp.orgrimmar.xyz/memory/mcp"
export SWARM_URL="https://mcp.orgrimmar.xyz/swarm/mcp"

# The agent you are checking (used as to_agent for the self-notify roundtrip).
export AGENT="silvana"
```

Shared helper — POST a `tools/call` and print the parsed result. Redacts nothing on its own, so
do not paste raw output into a chat the prince records; summarize instead.

```bash
mcp_call() {
  # $1 = endpoint URL, $2 = tool name, $3 = arguments JSON
  local url="$1" tool="$2" args="$3"
  curl -s --max-time 30 "$url" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer ${GBRAIN_TOKEN}" \
    -d "$(python3 -c '
import json, sys
print(json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])},
}))' "$tool" "$args")" \
  | python3 -c '
import json, sys
body = sys.stdin.read()
# SSE first, then plain JSON.
env = None
for line in body.splitlines():
    if line.startswith("data: "):
        env = json.loads(line[6:]); break
if env is None and body.strip():
    env = json.loads(body)
res = (env or {}).get("result", {})
if res.get("isError"):
    print("TOOL ERROR:", (res.get("content") or [{}])[0].get("text", "unknown"))
    sys.exit(1)
sc = res.get("structuredContent")
if sc is not None:
    print(json.dumps(sc, ensure_ascii=False, indent=2))
else:
    for c in res.get("content", []):
        print(c.get("text", ""))
'
}
```

## Probe 1 — memory create_* idempotent probe (C021 manual)

Writes one decision note, then writes the **same body again**. gbrain dedups by sha256, so the
second call is a no-op (same note id / "already exists"). That proves the write path and
idempotency without polluting the vault with two rows.

```bash
PROBE_BODY="gbrain-doctor manual write probe $(date -u +%Y-%m-%dT%H:%M:%SZ). Safe to delete. No secrets."

# First write — expect a created note id.
mcp_call "$MEMORY_URL" "create_decision_note" "$(python3 -c '
import json, sys
print(json.dumps({
    "title": "gbrain-doctor write probe",
    "body": sys.argv[1],
    "tags": ["gbrain-doctor", "probe"],
}))' "$PROBE_BODY")"

# Second write, identical body — expect idempotent no-op (same id / already exists).
mcp_call "$MEMORY_URL" "create_decision_note" "$(python3 -c '
import json, sys
print(json.dumps({
    "title": "gbrain-doctor write probe",
    "body": sys.argv[1],
    "tags": ["gbrain-doctor", "probe"],
}))' "$PROBE_BODY")"
```

Pass criteria: first call returns a note id without error; second call returns the same id or an
"already exists" / no-op signal (no duplicate row). NEVER put a token, key, IP, or any secret into
`title`/`body`/`tags`.

## Probe 2 — swarm self-notify roundtrip (C015 manual)

Sends a notify to **this same agent**, then fetches the resulting delivery by id. Proves the
outbox state machine end-to-end (enqueue -> deliver -> readable row).

```bash
# Notify self. Capture the structured result to extract a task/delivery id.
NOTIFY_OUT="$(mcp_call "$SWARM_URL" "notify" "$(python3 -c '
import json, sys
print(json.dumps({
    "to_agent": sys.argv[1],
    "payload": {"context": "gbrain-doctor self-notify probe — ignore", "goal": "roundtrip smoke"},
}))' "$AGENT")")"
echo "$NOTIFY_OUT"

# Extract the id the server returned (field name varies: task_id / delivery_id / id).
TASK_ID="$(printf '%s' "$NOTIFY_OUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
for k in ("task_id", "delivery_id", "id"):
    if isinstance(d, dict) and d.get(k):
        print(d[k]); break
')"
echo "delivery id: ${TASK_ID:-<none>}"

# Fetch it back.
if [ -n "${TASK_ID:-}" ]; then
  mcp_call "$SWARM_URL" "get_delivery" "$(python3 -c '
import json, sys
print(json.dumps({"task_id": sys.argv[1]}))' "$TASK_ID")"
fi
```

Pass criteria: `notify` returns an id; `get_delivery` returns a row whose id matches and whose
`to_agent` is this agent. This leaves a real delivery row in the outbox — acceptable only as an
operator-approved smoke. Ack or let it drain afterwards so it does not inflate `pending_depth`
(G3) on the next doctor run.

## Cleanup

- Memory probe notes are tagged `gbrain-doctor`/`probe`; prune via the normal vault tooling if you
  do not want them lingering.
- The self-notify delivery should be acked/drained so G3 `swarm_pending_depth` stays clean.
