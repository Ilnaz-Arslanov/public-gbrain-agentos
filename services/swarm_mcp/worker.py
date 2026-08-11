"""swarm-mcp worker — polls delivery_outbox and POSTs to agent gateways.

Transport: HTTP POST to URL from AGENT_GATEWAYS env (JSON map {agent: url}).
HTTP 200/2xx → mark_acked. 5xx/timeout/network → mark_retry with backoff.
4xx (except 429) → mark as failed (permanent client error).
Missing gateway URL for an agent → mark_retry (operator can configure later).

Per-agent outbound auth (extension 2026-05-17, Hermes integration):
- ``AGENT_GATEWAYS`` remains the existing JSON map ``{agent: url}``.
- Optional ``AGENT_GATEWAY_AUTH`` JSON map selects auth mode per agent:
  ``{"tyrande": "hmac:env:TYRANDE_WEBHOOK_HMAC",
     "daisy":   "hmac_github:env:DAISY_WEBHOOK_HMAC",
     "claude":  "bearer:env:GATEWAY_WEBHOOK_TOKEN"}``.
  Two HMAC schemes exist because targets disagree on the wire format:
  ``hmac`` is the Hermes/Stripe timestamped scheme (``X-Hermes-Signature`` +
  ``X-Hermes-Timestamp``), while ``hmac_github`` signs the raw body only and
  sends ``X-Hub-Signature-256`` — the format the Hermes gateway's own
  ``hermes webhook`` routes verify. Sending the wrong one yields HTTP 401.
  Spec ``<mode>:env:<ENV_VAR_NAME>`` resolves the secret from the named env var
  at load time. Raw secrets must never be embedded in ``AGENT_GATEWAY_AUTH``
  literally.
- Agents without an explicit ``AGENT_GATEWAY_AUTH`` entry keep the legacy
  behavior: use ``GATEWAY_WEBHOOK_TOKEN`` as a Bearer token if set, otherwise
  send no auth header. Bearer is therefore the default for backward
  compatibility.
- ``GBRAIN_HMAC_OUTBOUND_ENABLED=0`` disables HMAC signing globally; targets
  configured as HMAC are then returned as ``retry`` so they re-deliver after
  the operator re-enables outbound HMAC.
"""
import asyncio
import dataclasses
import json
import logging
import os
import signal
import sys
from typing import Literal

import httpx

os.environ.setdefault("MCP_PORT", "0")  # worker doesn't need MCP port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.config import Config
from services.shared.db import close_pool, get_pool
from services.shared.hmac_sign import sign_request, sign_request_github

from . import outbox

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 10
BATCH_SIZE = 20
# A target with no gateway URL never had a delivery attempted, so it must not
# consume attempts -- re-check on a slow timer instead. See outbox.mark_deferred.
DEFER_NO_GATEWAY_SEC = 300
NO_GATEWAY_PREFIX = "no gateway URL"

# Bridge to a Telegram/HTTP gateway which expects POST {agentId, message, chatId}.
# Configure with env: OWNER_CHAT_ID (Telegram chat for forwarded prompts),
# COORDINATOR_AGENT (agent name used as the loop-prevention sink),
# GATEWAY_WEBHOOK_TOKEN (Bearer token if your gateway enforces auth).
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))
GATEWAY_TOKEN = os.environ.get("GATEWAY_WEBHOOK_TOKEN", "")
COORDINATOR_AGENT = os.environ.get("COORDINATOR_AGENT", "coordinator-agent")


@dataclasses.dataclass(frozen=True)
class GatewayAuth:
    """Resolved per-agent gateway auth.

    Attributes:
        mode: Auth scheme. ``"bearer"`` adds ``Authorization: Bearer <value>``,
            ``"hmac"`` signs the body with the value as the raw secret bytes
            (Hermes/Stripe timestamped scheme), ``"hmac_github"`` signs the raw
            body only and sends ``X-Hub-Signature-256``, ``"none"`` sends no
            auth header.
        value: Raw token (bearer) or raw secret (hmac). Empty string for
            ``none``. Treat as sensitive — never log or include in errors.
            ``repr=False`` so the secret does not leak via stray repr/log.
    """

    mode: Literal["bearer", "hmac", "hmac_github", "none"]
    value: str = dataclasses.field(repr=False)


def _resolve_auth_spec(spec: str) -> GatewayAuth:
    """Resolve a single ``AGENT_GATEWAY_AUTH`` value.

    Supported forms:
        ``bearer:env:VAR_NAME``       -> Bearer with value from env var
        ``hmac:env:VAR_NAME``         -> HMAC (Hermes/Stripe) from env var
        ``hmac_github:env:VAR_NAME``  -> HMAC (X-Hub-Signature-256) from env var
        ``none``                      -> no auth

    Unknown / empty / unresolvable specs degrade to ``GatewayAuth("none","")``.
    The literal raw token form is intentionally NOT supported here to keep raw
    secrets out of process arg lists / docker inspect output.
    """
    if not spec or not isinstance(spec, str):
        return GatewayAuth("none", "")
    spec = spec.strip()
    if spec == "none":
        return GatewayAuth("none", "")
    parts = spec.split(":", 2)
    if len(parts) != 3:
        return GatewayAuth("none", "")
    mode, source, name = parts[0].lower(), parts[1].lower(), parts[2]
    if mode not in ("bearer", "hmac", "hmac_github"):
        return GatewayAuth("none", "")
    if source != "env":
        return GatewayAuth("none", "")
    value = os.environ.get(name, "")
    if not value:
        return GatewayAuth("none", "")
    return GatewayAuth(mode, value)  # type: ignore[arg-type]


def _load_gateway_auth() -> dict[str, GatewayAuth]:
    """Parse ``AGENT_GATEWAY_AUTH`` env JSON into a per-agent auth map.

    Returns an empty dict if the env var is unset or malformed. Each value is
    resolved via :func:`_resolve_auth_spec`; the returned map never carries an
    env var NAME, only the resolved raw secret/token.
    """
    raw = os.environ.get("AGENT_GATEWAY_AUTH", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.error("AGENT_GATEWAY_AUTH parse failed: %s", exc)
        return {}
    if not isinstance(parsed, dict):
        logger.error("AGENT_GATEWAY_AUTH is not a JSON object, ignoring")
        return {}
    out: dict[str, GatewayAuth] = {}
    for agent, spec in parsed.items():
        out[str(agent)] = _resolve_auth_spec(str(spec))
    return out


def _gateway_auth_for(agent: str, auth_map: dict[str, GatewayAuth]) -> GatewayAuth:
    """Return the GatewayAuth to use for ``agent``.

    Priority:
        1. Explicit ``AGENT_GATEWAY_AUTH`` entry for the agent.
        2. Legacy fallback: ``GATEWAY_WEBHOOK_TOKEN`` env as Bearer.
        3. ``GatewayAuth("none", "")``.
    """
    if agent in auth_map:
        return auth_map[agent]
    if GATEWAY_TOKEN:
        return GatewayAuth("bearer", GATEWAY_TOKEN)
    return GatewayAuth("none", "")


def _serialize_gateway_body(body: dict) -> bytes:
    """Serialize a gateway webhook body to bytes exactly once.

    The returned bytes are what we sign AND what we POST — using the same
    bytes for both guarantees the verifier sees identical content. ``httpx``
    is invoked with ``content=<bytes>`` (not ``json=``) so it does not
    re-serialize the dict.
    """
    return json.dumps(body, ensure_ascii=False, sort_keys=False, separators=(",", ":")).encode("utf-8")


def _hmac_outbound_enabled() -> bool:
    """Whether outbound HMAC signing is globally enabled.

    Default: enabled. Set ``GBRAIN_HMAC_OUTBOUND_ENABLED=0`` for emergency
    rollback — HMAC targets then defer to retry until re-enabled.
    """
    return os.environ.get("GBRAIN_HMAC_OUTBOUND_ENABLED", "1") != "0"


# Payload keys consumed by the prompt template itself — never repeated in the
# rendered body. Everything else in the payload is content and must survive.
_RENDER_RESERVED_KEYS = frozenset(
    {"title", "body", "message", "urgency", "_priority", "_escalation_reason", "_smoke", "kind", "from"}
)
# Hard cap on the rendered body so one oversized payload cannot blow up the
# gateway request. Truncation is visible, never silent.
_RENDER_BODY_LIMIT = 4000


def _render_section(key: str, value: object) -> str:
    """Render one leftover payload field as a readable prompt section.

    Scalars become ``Key: value``, lists become bullet lists, anything else
    falls back to compact JSON. Never raises — an unserializable value degrades
    to ``repr``.
    """
    label = str(key).replace("_", " ").strip().capitalize()
    if isinstance(value, str):
        return f"{label}: {value.strip()}"
    if isinstance(value, (int, float, bool)):
        return f"{label}: {value}"
    if isinstance(value, list):
        lines = []
        for item in value:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            lines.append(f"- {text}")
        return f"{label}:\n" + "\n".join(lines)
    try:
        return f"{label}: {json.dumps(value, ensure_ascii=False)}"
    except (TypeError, ValueError):
        return f"{label}: {value!r}"


def _render_payload_body(payload: dict) -> str:
    """Build the human-readable body of an inter-agent letter.

    The historical contract was ``{title, body}`` only: any sender using other
    field names (``message``, ``facts``, ``sources``, ...) delivered a letter
    with an empty body, and the receiving agent saw nothing to act on. This
    renderer puts ``body`` (or ``message``) first and then appends every
    remaining non-reserved field as its own section, so no payload content is
    ever silently dropped — not even when ``body`` is present alongside extra
    fields.

    Args:
        payload: Raw inter-agent payload as stored in ``delivery_outbox``.

    Returns:
        Rendered body text (possibly empty if the payload carried no content),
        truncated to ``_RENDER_BODY_LIMIT`` with an explicit marker.
    """
    parts: list[str] = []
    for lead in ("body", "message"):
        value = payload.get(lead)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
            break
    for key, value in payload.items():
        if key in _RENDER_RESERVED_KEYS or str(key).startswith("_"):
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        parts.append(_render_section(key, value))

    rendered = "\n\n".join(parts)
    if len(rendered) > _RENDER_BODY_LIMIT:
        rendered = rendered[:_RENDER_BODY_LIMIT] + "\n... [truncated by swarm-worker]"
    return rendered


def _format_virtual_prompt(from_agent: str, to_agent: str, task_id: str, payload: dict) -> str:
    """Pack inter-agent payload into a chat-style prompt the agent will see.

    The receiving agent sees this as if it came from the owner via the
    chat gateway (synthetic update). Agent must call swarm.ack(task_id) when done.

    The prompt asks for exactly two things: do the task, report to the owner in
    chat. It deliberately does NOT ask for a copy to a coordinator — that
    instruction used to be here and produced 21 undeliverable letters to a
    non-existent ``coordinator-agent`` before it was removed (2026-08-10).
    Coordination happens through the owner, so a second report had no reader.
    Restore it only together with a coordinator that someone actually reads.

    Loop-prevention gates:
    - Ack-only fast path for: (a) anything addressed to COORDINATOR_AGENT;
      (b) explicit smoke pings via `_smoke=true`. These skip the full report
      flow which would otherwise recurse (coordinator → coordinator).

    Envelope kinds:
    - default — a task: execute, report to the owner, ack.
    - ``_kind="report"`` — findings sent for the record, with no work attached.
      The imperative block is what makes an agent act, so handing it to an
      informational letter makes the receiver stage a performance of "executing"
      an already-finished piece of work (observed on the 2026-08-11 bridge
      audit). Senders set this flag; ``kind`` is left alone because it already
      titles the letter.
    """
    title = payload.get("title") or payload.get("kind") or "(no title)"
    body = _render_payload_body(payload)
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

    # Informational letter: no task, so no imperative to execute one.
    if str(payload.get("_kind") or "").lower() == "report":
        return (
            f"[Inter-agent from {from_agent} -> {to_agent}] urgency={urgency} (report)\n"
            f"Report: {title}\n"
            f"{body}{extra}\n"
            f"---\n"
            f"ACTIONS (report — nothing to execute):\n"
            f"1. Read it. No task is attached; do not invent one.\n"
            f"2. If it changes your plans or contradicts what you know, send the "
            f"owner 1-3 lines. Otherwise skip the report.\n"
            f"3. swarm.ack(task_id=\"{task_id}\")."
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
        f"3. Call swarm.ack(task_id=\"{task_id}\") at the very end."
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


async def _deliver_one(
    client: httpx.AsyncClient,
    gateways: dict[str, str],
    row: object,
    auth_map: dict[str, GatewayAuth] | None = None,
) -> tuple[str, str]:
    """Try to deliver one row. Returns (status, last_error).

    ``status`` is a *transport* verdict — "delivered" means the gateway
    answered 2xx, not that the receiving agent did anything with the letter.
    The row is closed only when that agent calls ``swarm.ack``.

    Selects per-agent auth via ``auth_map`` (resolved from ``AGENT_GATEWAY_AUTH``)
    with a legacy ``GATEWAY_WEBHOOK_TOKEN`` Bearer fallback. The request body
    bytes are serialized exactly once and shared between signature computation
    (when HMAC) and the POST itself — this is the integrity invariant.
    """
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

    auth = _gateway_auth_for(to_agent, auth_map or {})
    headers: dict[str, str] = {"Content-Type": "application/json"}
    body_bytes = _serialize_gateway_body(body)

    if auth.mode in ("hmac", "hmac_github"):
        if not _hmac_outbound_enabled():
            return "retry", f"hmac_outbound_disabled for agent={to_agent}"
        signer = sign_request if auth.mode == "hmac" else sign_request_github
        headers.update(signer(auth.value.encode("utf-8"), body_bytes))
    elif auth.mode == "bearer":
        headers["Authorization"] = f"Bearer {auth.value}"
    # mode == "none": no auth header.

    try:
        resp = await client.post(url, content=body_bytes, headers=headers, timeout=HTTP_TIMEOUT_SEC)
    except httpx.TimeoutException as exc:
        return "retry", f"timeout: {exc}"
    except httpx.HTTPError as exc:
        return "retry", f"http_error: {type(exc).__name__}: {exc}"

    if 200 <= resp.status_code < 300:
        return "delivered", ""
    if resp.status_code == 429:
        return "retry", f"http_429"
    if 400 <= resp.status_code < 500:
        return "failed", f"http_{resp.status_code}: {resp.text[:200]}"
    return "retry", f"http_{resp.status_code}: {resp.text[:200]}"


async def run() -> None:
    config = Config(mcp_port=0)
    pool = await get_pool(config)
    gateways = _load_gateways()
    auth_map = _load_gateway_auth()
    logger.info(
        "swarm-worker started: gateways=%s auth_modes=%s ack_timeout=%ds poll=%ds",
        list(gateways.keys()),
        {a: v.mode for a, v in auth_map.items()},
        outbox.ACK_TIMEOUT_SEC,
        POLL_INTERVAL_SEC,
    )

    # Sweep for unacked deliveries roughly once a minute — the check scans a
    # non-indexed status/updated_at predicate, and nothing here is urgent.
    sweep_every = max(1, 60 // POLL_INTERVAL_SEC)
    tick = 0

    async with httpx.AsyncClient() as client:
        while not _shutdown.requested:
            try:
                tick += 1
                if tick % sweep_every == 0:
                    await outbox.sweep_ack_missing(pool)
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
                            status, last_error = await _deliver_one(client, gateways, row, auth_map)
                            if status == "delivered":
                                await outbox.mark_sent(conn, row["task_id"])
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
                            elif last_error.startswith(NO_GATEWAY_PREFIX):
                                await outbox.mark_deferred(
                                    conn, row["id"], last_error, DEFER_NO_GATEWAY_SEC,
                                )
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
