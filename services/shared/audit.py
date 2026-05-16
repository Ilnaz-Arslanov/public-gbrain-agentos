"""Audit logging for gbrain MCP services."""
import json
import logging

import asyncpg

logger = logging.getLogger(__name__)


async def log_audit(
    pool: asyncpg.Pool,
    agent: str,
    tool: str,
    args_summary: dict[str, object],
    result_status: str,
    latency_ms: int,
    error: str | None = None,
) -> None:
    """Insert an audit log entry.

    Args:
        pool: Asyncpg connection pool.
        agent: Agent name (from auth context).
        tool: MCP tool name that was called.
        args_summary: Summary of tool arguments (no secrets).
        result_status: Outcome -- 'ok', 'unchanged', 'error', etc.
        latency_ms: Call duration in milliseconds.
        error: Error message if result_status is 'error'.
    """
    try:
        await pool.execute(
            """
            INSERT INTO audit_log (agent, tool, args_summary, result_status, latency_ms, error)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6)
            """,
            agent,
            tool,
            json.dumps(args_summary, ensure_ascii=False, default=str),
            result_status,
            latency_ms,
            error,
        )
    except Exception:
        # Audit logging must never break the main flow
        logger.exception("Failed to write audit log for tool=%s agent=%s", tool, agent)
