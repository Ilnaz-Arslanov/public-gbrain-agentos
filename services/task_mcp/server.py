"""FastMCP server for task-mcp (task board CRUD for agent swarm), port 8769."""
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import asyncpg
from fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.asgi_auth import HermesAwareAuthMiddleware
from services.shared.auth import (
    AgentContext,
    AuthValue,
    authenticate_captured,
    check_write_scope,
    resolve_request_identity,
)
from services.shared.config import Config
from services.shared.db import close_pool, get_pool
from services.shared.audit import log_audit
from services.shared.tool_gating import parse_tool_set, should_register_tool

from . import store
from . import agents_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 8769

# Per-request auth captured by ASGI middleware. Holds a Bearer string,
# a HmacAuthValue, or None. Workaround for FastMCP stateless HTTP not
# surfacing request headers to tool handlers via ctx.request_context in
# some transport configs.
_REQUEST_AUTH: ContextVar[AuthValue] = ContextVar("task_request_auth", default=None)


class AuthCaptureMiddleware(HermesAwareAuthMiddleware):
    """ASGI middleware: capture Bearer or Hermes HMAC auth into ContextVar.

    Thin compatibility subclass over :class:`HermesAwareAuthMiddleware`
    that binds the task-mcp ContextVar.
    """

    def __init__(self, app):
        super().__init__(app, _REQUEST_AUTH)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, object]]:
    """Initialize DB pool on startup, close on shutdown."""
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    pool = await get_pool(config)
    logger.info("task-mcp started: port=%d", config.mcp_port)
    try:
        yield {"pool": pool, "config": config}
    finally:
        await close_pool()
        logger.info("task-mcp shutdown complete")


mcp = FastMCP("task-mcp", lifespan=lifespan)

# Tool gating: parse GBRAIN_TOOLS once at import time.
_TOOL_SET = parse_tool_set(os.environ.get("GBRAIN_TOOLS"))


def _gated_tool(tool_name: str, **kwargs):
    """Decorator that registers a tool only when permitted by GBRAIN_TOOLS.

    Returns either `mcp.tool(...)` or an identity decorator so the underlying
    coroutine remains importable and callable from Python regardless of mode.
    """
    if should_register_tool("task_mcp", tool_name, _TOOL_SET):
        return mcp.tool(**kwargs)

    def _identity(fn):
        return fn

    return _identity


async def _get_pool() -> asyncpg.Pool:
    """Return the shared connection pool."""
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    return await get_pool(config)


TASKS_WRITE_SCOPE = "10-tasks"


async def _resolve_caller(ctx: Any, pool: asyncpg.Pool) -> AgentContext:
    """Authenticate the calling agent via Bearer token or Hermes HMAC.

    Returns the full :class:`AgentContext` so callers can check write scopes.

    ``ctx`` is accepted for backward-compat but no longer consulted
    -- the ContextVar is the source of truth once the ASGI middleware
    has run.
    """
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    return await resolve_request_identity(
        _REQUEST_AUTH,
        pool,
        hmac_auth_enabled=config.hmac_auth_enabled,
        tolerance_seconds=config.hmac_timestamp_tolerance_seconds,
    )


def _require_write(agent_ctx: AgentContext) -> None:
    """Raise PermissionError if agent lacks task write scope."""
    if not check_write_scope(agent_ctx, TASKS_WRITE_SCOPE):
        raise PermissionError(
            f"Agent {agent_ctx.agent!r} lacks write scope {TASKS_WRITE_SCOPE!r}"
        )


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@_gated_tool("task_create")
async def task_create(
    title: str,
    description: str = "",
    assignee: str | None = None,
    priority: str = "medium",
    metadata: dict[str, Any] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Create a new task on the board.

    Returns the created task with all fields including generated id.
    """
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.create_task(
        pool, agent_ctx.agent, title, description, assignee, priority, metadata,
    )
    await log_audit(
        pool, agent_ctx.agent, "task_create",
        {"title": title, "assignee": assignee, "id": result["id"]},
        "ok", 0,
    )
    return result


@_gated_tool("task_update")
async def task_update(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
    metadata: dict[str, Any] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Update specific fields of a task. Only provided fields are changed."""
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.update_task(
        pool, task_id, agent_ctx.agent,
        title=title,
        description=description,
        assignee=assignee,
        priority=priority,
        metadata=metadata,
    )
    await log_audit(
        pool, agent_ctx.agent, "task_update",
        {"task_id": task_id, "fields": [
            k for k, v in {
                "title": title, "description": description,
                "assignee": assignee, "priority": priority,
                "metadata": metadata,
            }.items() if v is not None
        ]},
        "ok", 0,
    )
    return result


@_gated_tool("task_get", annotations={"readOnlyHint": True})
async def task_get(
    task_id: int,
    ctx: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single task by ID. Returns null if not found."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await store.get_task(pool, task_id)


@_gated_tool("task_list", annotations={"readOnlyHint": True})
async def task_list(
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 50,
    ctx: Any = None,
) -> list[dict[str, Any]]:
    """List tasks with optional filters by assignee and/or status."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    clamped_limit = max(1, min(limit, 500))
    return await store.list_tasks(pool, assignee, status, clamped_limit)


@_gated_tool("task_start")
async def task_start(
    task_id: int,
    note: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Transition task to 'progress' status. Valid from: new, blocked."""
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.transition_status(pool, task_id, agent_ctx.agent, "progress", note)
    await log_audit(
        pool, agent_ctx.agent, "task_start",
        {"task_id": task_id, "note": note},
        "ok", 0,
    )
    return result


@_gated_tool("task_review")
async def task_review(
    task_id: int,
    note: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Transition task to 'review' status. Valid from: progress."""
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.transition_status(pool, task_id, agent_ctx.agent, "review", note)
    await log_audit(
        pool, agent_ctx.agent, "task_review",
        {"task_id": task_id, "note": note},
        "ok", 0,
    )
    return result


@_gated_tool("task_done")
async def task_done(
    task_id: int,
    note: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Transition task to 'done' status (terminal). Valid from: review."""
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.transition_status(pool, task_id, agent_ctx.agent, "done", note)
    await log_audit(
        pool, agent_ctx.agent, "task_done",
        {"task_id": task_id, "note": note},
        "ok", 0,
    )
    return result


@_gated_tool("task_block")
async def task_block(
    task_id: int,
    reason: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Transition task to 'blocked' status. Valid from: new, progress."""
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await store.transition_status(pool, task_id, agent_ctx.agent, "blocked", reason)
    await log_audit(
        pool, agent_ctx.agent, "task_block",
        {"task_id": task_id, "reason": reason},
        "ok", 0,
    )
    return result


@_gated_tool("task_reopen")
async def task_reopen(
    task_id: int,
    note: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Reopen a task: blocked->new or review->progress.

    Inspects the current status to determine the correct target:
    - blocked -> new
    - review -> progress (rejected back to work)
    """
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)

    # Read current status to determine transition target
    current = await store.get_task(pool, task_id)
    if current is None:
        raise ValueError(f"task {task_id} not found")

    target_map = {"blocked": "new", "review": "progress"}
    current_status = current["status"]
    target = target_map.get(current_status)
    if target is None:
        raise ValueError(
            f"task_reopen not allowed from status {current_status!r}. "
            f"Valid source statuses: blocked, review"
        )

    result = await store.transition_status(pool, task_id, agent_ctx.agent, target, note)
    await log_audit(
        pool, agent_ctx.agent, "task_reopen",
        {"task_id": task_id, "from": current_status, "to": target, "note": note},
        "ok", 0,
    )
    return result


@_gated_tool("task_history", annotations={"readOnlyHint": True})
async def task_history(
    task_id: int,
    ctx: Any = None,
) -> list[dict[str, Any]]:
    """Return status transition history for a task. Read-only."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await store.list_task_history(pool, task_id)


# ---------------------------------------------------------------------------
# Agent Heartbeat Tools
# ---------------------------------------------------------------------------


@_gated_tool("agent_heartbeat")
async def agent_heartbeat(
    status: str = "online",
    host: str | None = None,
    version: str | None = None,
    metadata: dict[str, Any] | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Send a heartbeat: upsert agent status and last_seen timestamp.

    Automatically uses the authenticated agent name as the agent_name.
    Call this periodically (e.g. every 5 min) to signal the agent is alive.
    """
    pool = await _get_pool()
    agent_ctx = await _resolve_caller(ctx, pool)
    _require_write(agent_ctx)
    result = await agents_store.heartbeat(pool, agent_ctx.agent, status, host, version, metadata)
    await log_audit(
        pool, agent_ctx.agent, "agent_heartbeat",
        {"status": status, "host": host},
        "ok", 0,
    )
    return result


@_gated_tool("agent_status", annotations={"readOnlyHint": True})
async def agent_status(
    agent_name: str,
    ctx: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single agent's status by name. Returns null if not found."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await agents_store.get_agent(pool, agent_name)


@_gated_tool("agent_list", annotations={"readOnlyHint": True})
async def agent_list(
    status: str | None = None,
    ctx: Any = None,
) -> list[dict[str, Any]]:
    """List all registered agents, optionally filtered by status."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await agents_store.list_agents(pool, status)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    logger.info("Starting task-mcp on %s:%d (with auth middleware)", host, port)
    app = mcp.http_app(transport="streamable-http")
    app = AuthCaptureMiddleware(app)
    uvicorn.run(app, host=host, port=port, log_level="info")
