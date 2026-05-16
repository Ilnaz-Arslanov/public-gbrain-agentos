"""swarm-mcp worker — polls delivery_outbox and POSTs to agent gateways.

Transport: HTTP POST to URL from AGENT_GATEWAYS env (JSON map {agent: url}).
HTTP 200/2xx → mark_acked. 5xx/timeout/network → mark_retry with backoff.
4xx (except 429) → mark as failed (permanent client error).
Missing gateway URL for an agent → mark_retry (operator can configure later).
"""
import asyncio
import json
import logging
import os
import signal
import sys

import httpx

os.environ.setdefault("MCP_PORT", "0")  # worker doesn't need MCP port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.config import Config
from services.shared.db import close_pool, get_pool

from . import outbox

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 10
BATCH_SIZE = 20

# Bridge to a Telegram/HTTP gateway which expects POST {agentId, message, chatId}.
# Configure with env: OWNER_CHAT_ID (Telegram chat for forwarded prompts),
# COORDINATOR_AGENT (agent name used as the loop-prevention sink),
# GATEWAY_WEBHOOK_TOKEN (Bearer token if your gateway enforces auth).
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))
GATEWAY_TOKEN = os.environ.get("GATEWAY_WEBHOOK_TOKEN", "")
COORDINATOR_AGENT = os.environ.get("COORDINATOR_AGENT", "coordinator-agent")


def _format_virtual_prompt(from_agent: str, to_agent: str, task_id: str, payload: dict) -> str:
    """Pack inter-agent payload into a chat-style prompt the agent will see.

    The receiving agent sees this as if it came from the owner via the
    chat gateway (synthetic update). Agent must call swarm.ack(task_id) when done.

    Loop-prevention gates:
    - Ack-only fast path for: (a) reports back to the coordinator (COORDINATOR_AGENT),
      detected by title prefix "Report from " or `_origin_task` field; (b) explicit
      smoke pings via `_smoke=true`. These skip the full report + dual-notify flow
      which would otherwise cause infinite recursion (coordinator → coordinator).
    """
    title = payload.get("title") or "(no title)"
    body = payload.get("body") or ""
    urgency = payload.get("urgency") or payload.get("_priority") or "normal"
    reason = payload.get("_escalation_reason") or ""
    extra = ""
    if reason:
        extra = f"\nEscalation reason: {reason}"

    # Hard loop gate: COORDINATOR_AGENT is the coordinator; it never needs a
    # dual-report back to itself. Any swarm.notify(coordinator, ...) → ack-only.
    # Plus explicit smoke pings (`_smoke=true`) for any target.
    is_to_coordinator = to_agent == COORDINATOR_AGENT
    is_smoke = bool(payload.get("_smoke"))

    if is_to_coordinator or is_smoke:
        if is_to_coordinator:
            kind_hint = (
                "retro-summary"
                if str(title).startswith("Report from") or payload.get("_origin_task")
                else "request to coordinator"
            )
        else:
            kind_hint = "smoke ping"
        return (
            f"[Inter-agent from {from_agent} -> {to_agent}] urgency={urgency} ({kind_hint})\n"
            f"Task: {title}\n"
            f"{body}{extra}\n"
            f"---\n"
            f"ACTIONS (ack-only fast path, no dual-report):\n"
            f"1. Inspect payload and decide if action is needed. Coordinator targets "
            f"and smoke pings do not require a full chat report.\n"
            f"2. If meaningful, send the owner a 1-3 line note via the chat gateway. "
            f"Otherwise skip.\n"
            f"3. DO NOT swarm.notify back (loop risk). Go straight to swarm.ack.\n"
            f"4. swarm.ack(task_id=\"{task_id}\")."
        )

    return (
        f"[Inter-agent from {from_agent} -> {to_agent}] urgency={urgency}\n"
        f"Task: {title}\n"
        f"{body}{extra}\n"
        f"---\n"
        f"ACTIONS:\n"
        f"1. Execute the task.\n"
        f"2. Send the owner a detailed chat report. Format:\n"
        f"\n"
        f"   Task from {from_agent}: <short name>\n"
        f"\n"
        f"   What I did:\n"
        f"   - concrete step 1 (paths/commands/numbers)\n"
        f"   - concrete step 2\n"
        f"   - ...\n"
        f"\n"
        f"   Result:\n"
        f"   - what worked, what failed, gaps found\n"
        f"   - links to files/commits/PRs if applicable\n"
        f"\n"
        f"   Time spent: <minutes or mm:ss>\n"
        f"\n"
        f"   Avoid one-liner 'done, acked' reports. The owner wants substance, "
        f"at least 5-10 lines.\n"
        f"3. ALSO SEND A SHORT SUMMARY TO THE COORDINATOR via swarm.notify:\n"
        f"   swarm.notify(to_agent=\"{COORDINATOR_AGENT}\", payload={{"
        f"\"title\": \"Report from {to_agent}: <task name>\", "
        f"\"body\": \"<2-4 bullets: what done + commit/path + gaps>\", "
        f"\"_origin_task\": \"{task_id}\"}})\n"
        f"   Without this step the coordinator cannot see your work or schedule "
        f"follow-ups. Chat report = owner, swarm.notify = coordinator. Two recipients.\n"
        f"4. Call swarm.ack(task_id=\"{task_id}\") at the very end."
    )


class _ShutdownFlag:
    def __init__(self) -> None:
        self.requested = False

    def set(self) -> None:
        self.requested = True


_shutdown = _ShutdownFlag()


def _handle_signal(sig: int, _frame: object) -> None:
    logger.info("Received signal %d, requesting shutdown", sig)
    _shutdown.set()


def _load_gateways() -> dict[str, str]:
    """Parse AGENT_GATEWAYS env JSON: {"agent_name": "http://...", ...}."""
    raw = os.environ.get("AGENT_GATEWAYS", "{}")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.error("AGENT_GATEWAYS is not a JSON object, ignoring")
            return {}
        return {str(k): str(v) for k, v in parsed.items()}
    except Exception as exc:
        logger.error("AGENT_GATEWAYS parse failed: %s", exc)
        return {}


async def _deliver_one(client: httpx.AsyncClient, gateways: dict[str, str], row: object) -> tuple[str, str]:
    """Try to deliver one row. Returns (status, last_error)."""
    to_agent = row["to_agent"]
    url = gateways.get(to_agent)
    if not url:
        return "retry", f"no gateway URL for agent={to_agent}"

    payload = json.loads(row["payload_json"])
    # Repackage into gateway webhook schema.
    body = {
        "agentId": to_agent,
        "message": _format_virtual_prompt(row["from_agent"], to_agent, row["task_id"], payload),
        "chatId": OWNER_CHAT_ID,
    }
    headers = {}
    if GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    except httpx.TimeoutException as exc:
        return "retry", f"timeout: {exc}"
    except httpx.HTTPError as exc:
        return "retry", f"http_error: {type(exc).__name__}: {exc}"

    if 200 <= resp.status_code < 300:
        return "acked", ""
    if resp.status_code == 429:
        return "retry", f"http_429"
    if 400 <= resp.status_code < 500:
        return "failed", f"http_{resp.status_code}: {resp.text[:200]}"
    return "retry", f"http_{resp.status_code}: {resp.text[:200]}"


async def run() -> None:
    config = Config(mcp_port=0)
    pool = await get_pool(config)
    n_recovered = await outbox.bootstrap_recovery(pool)
    gateways = _load_gateways()
    logger.info(
        "swarm-worker started: gateways=%s recovered=%d poll=%ds",
        list(gateways.keys()), n_recovered, POLL_INTERVAL_SEC,
    )

    async with httpx.AsyncClient() as client:
        while not _shutdown.requested:
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        rows = await conn.fetch(
                            """
                            SELECT id, task_id, from_agent, to_agent, payload::text AS payload_json,
                                   attempts, max_attempts
                            FROM delivery_outbox
                            WHERE status = 'pending' AND next_retry_at <= now()
                            ORDER BY created_at
                            LIMIT $1
                            FOR UPDATE SKIP LOCKED
                            """,
                            BATCH_SIZE,
                        )
                        if rows:
                            logger.info("processing batch=%d", len(rows))
                        for row in rows:
                            status, last_error = await _deliver_one(client, gateways, row)
                            if status == "acked":
                                await outbox.mark_acked(conn, row["task_id"])
                            elif status == "failed":
                                await conn.execute(
                                    """
                                    UPDATE delivery_outbox
                                    SET status='failed', attempts=$2, updated_at=now()
                                    WHERE id=$1
                                    """,
                                    row["id"], row["attempts"] + 1,
                                )
                                logger.warning("delivery failed permanently id=%d to=%s err=%s",
                                               row["id"], row["to_agent"], last_error[:120])
                            else:  # retry
                                await outbox.mark_retry(
                                    conn, row["id"], row["attempts"] + 1,
                                    row["max_attempts"], last_error,
                                )
            except Exception:
                logger.exception("worker loop error")

            for _ in range(POLL_INTERVAL_SEC):
                if _shutdown.requested:
                    break
                await asyncio.sleep(1)

    await close_pool()
    logger.info("swarm-worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    asyncio.run(run())


if __name__ == "__main__":
    import services.swarm_mcp.worker as _self
    sys.exit(_self.main())
