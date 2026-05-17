"""Agent registry database operations -- heartbeat + status.

Table: agents (agent_name PK, status, last_seen, host, version, metadata JSONB)
"""
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset({
    "online", "offline", "busy", "error", "maintenance",
})


def _json_dumps(data: dict[str, Any]) -> str:
    """Serialize dict to JSON string for JSONB columns."""
    import json
    return json.dumps(data, ensure_ascii=False, default=str)


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg Record from agents table to a plain dict."""
    import json as _json
    result: dict[str, Any] = {
        "agent_name": row["agent_name"],
        "status": row["status"],
        "last_seen": row["last_seen"].isoformat(),
        "host": row["host"],
        "version": row["version"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
    metadata_raw = row.get("metadata_json") or row.get("metadata")
    if metadata_raw is not None and isinstance(metadata_raw, str):
        result["metadata"] = _json.loads(metadata_raw)
    elif metadata_raw is not None:
        result["metadata"] = metadata_raw
    else:
        result["metadata"] = {}
    return result


async def heartbeat(
    pool: asyncpg.Pool,
    agent_name: str,
    status: str = "online",
    host: str | None = None,
    version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert agent status and last_seen timestamp.

    Creates the agent row if it doesn't exist (INSERT ON CONFLICT UPDATE).

    Args:
        pool: Asyncpg connection pool.
        agent_name: Agent identifier (e.g. 'my-agent').
        status: Agent status.
        host: Agent host/server.
        version: Agent version string.
        metadata: Arbitrary JSONB metadata.

    Returns:
        Agent record as dict.

    Raises:
        ValueError: If status is invalid.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}, must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )

    meta_json = _json_dumps(metadata or {})
    row = await pool.fetchrow(
        """
        INSERT INTO agents (agent_name, status, last_seen, host, version, metadata)
        VALUES ($1, $2, now(), $3, $4, $5::jsonb)
        ON CONFLICT (agent_name) DO UPDATE SET
            status = EXCLUDED.status,
            last_seen = now(),
            host = COALESCE(EXCLUDED.host, agents.host),
            version = COALESCE(EXCLUDED.version, agents.version),
            metadata = CASE
                WHEN EXCLUDED.metadata::text = '{}'::text THEN agents.metadata
                ELSE EXCLUDED.metadata
            END,
            updated_at = now()
        RETURNING agent_name, status, last_seen, host, version,
                  metadata::text AS metadata_json, created_at, updated_at
        """,
        agent_name,
        status,
        host,
        version,
        meta_json,
    )
    logger.info("agent.heartbeat %s status=%s host=%s", agent_name, status, host)
    return _row_to_dict(row)


async def get_agent(pool: asyncpg.Pool, agent_name: str) -> dict[str, Any] | None:
    """Fetch a single agent by name.

    Args:
        pool: Asyncpg connection pool.
        agent_name: Agent identifier.

    Returns:
        Agent dict or None if not found.
    """
    row = await pool.fetchrow(
        """
        SELECT agent_name, status, last_seen, host, version,
               metadata::text AS metadata_json, created_at, updated_at
        FROM agents
        WHERE agent_name = $1
        """,
        agent_name,
    )
    if row is None:
        return None
    return _row_to_dict(row)


async def list_agents(
    pool: asyncpg.Pool,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List all agents, optionally filtered by status.

    Args:
        pool: Asyncpg connection pool.
        status: Filter by agent status.

    Returns:
        List of agent dicts, ordered by agent_name.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status filter {status!r}, must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )

    if status is not None:
        rows = await pool.fetch(
            """
            SELECT agent_name, status, last_seen, host, version,
                   metadata::text AS metadata_json, created_at, updated_at
            FROM agents
            WHERE status = $1
            ORDER BY agent_name
            """,
            status,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT agent_name, status, last_seen, host, version,
                   metadata::text AS metadata_json, created_at, updated_at
            FROM agents
            ORDER BY agent_name
            """
        )
    return [_row_to_dict(r) for r in rows]
