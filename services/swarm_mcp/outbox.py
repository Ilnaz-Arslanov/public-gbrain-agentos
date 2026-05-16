"""delivery_outbox state machine + DB operations.

States: pending → sent → ack_missing → acked | failed
MVP simplification: pending → (HTTP 200) acked | (HTTP fail) pending+retry | (max attempts) failed.
sent/ack_missing reserved for Stage 2 ACK protocol.
"""
import logging
import secrets
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

BACKOFF_BASE_SEC = 10
BACKOFF_CAP_SEC = 300


def compute_backoff_seconds(attempts: int) -> int:
    """Exponential backoff: 10s × 2^(n-1), capped at 300s."""
    if attempts <= 0:
        return 0
    delay = BACKOFF_BASE_SEC * (2 ** (attempts - 1))
    return min(delay, BACKOFF_CAP_SEC)


def make_task_id(from_agent: str, to_agent: str) -> str:
    """Generate idempotency-friendly task_id."""
    nonce = secrets.token_hex(8)
    return f"{from_agent}::{to_agent}::{nonce}"


async def enqueue(
    pool: asyncpg.Pool,
    from_agent: str,
    to_agent: str,
    payload: dict[str, Any],
    task_id: str | None = None,
    max_attempts: int = 5,
) -> str:
    """Insert a pending delivery row. Returns task_id (idempotent on conflict)."""
    if task_id is None:
        task_id = make_task_id(from_agent, to_agent)

    row = await pool.fetchrow(
        """
        INSERT INTO delivery_outbox
          (task_id, from_agent, to_agent, payload, max_attempts, next_retry_at)
        VALUES ($1, $2, $3, $4::jsonb, $5, now())
        ON CONFLICT (task_id) DO UPDATE
          SET updated_at = now()
        RETURNING task_id, status, attempts
        """,
        task_id,
        from_agent,
        to_agent,
        _json_dumps(payload),
        max_attempts,
    )
    logger.info(
        "outbox.enqueue task_id=%s to=%s status=%s attempts=%d",
        row["task_id"],
        to_agent,
        row["status"],
        row["attempts"],
    )
    return row["task_id"]


async def fetch_due(
    pool: asyncpg.Pool,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Fetch up to N pending rows whose next_retry_at <= now()."""
    return await pool.fetch(
        """
        SELECT id, task_id, from_agent, to_agent, payload::text AS payload_json,
               attempts, max_attempts
        FROM delivery_outbox
        WHERE status = 'pending'
          AND next_retry_at <= now()
        ORDER BY created_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
        """,
        limit,
    )


async def mark_acked(conn: asyncpg.Connection, task_id: str) -> bool:
    """Mark row as acked. Returns True if a row was updated."""
    result = await conn.execute(
        """
        UPDATE delivery_outbox
        SET status = 'acked', updated_at = now()
        WHERE task_id = $1 AND status IN ('pending', 'sent', 'ack_missing')
        """,
        task_id,
    )
    rowcount = int(result.split()[-1]) if result.startswith("UPDATE ") else 0
    if rowcount > 0:
        logger.info("outbox.mark_acked task_id=%s", task_id)
    return rowcount > 0


async def mark_retry(
    conn: asyncpg.Connection,
    row_id: int,
    attempts: int,
    max_attempts: int,
    last_error: str,
) -> str:
    """Schedule a retry or move to failed if attempts exhausted. Returns new status."""
    if attempts >= max_attempts:
        await conn.execute(
            """
            UPDATE delivery_outbox
            SET status = 'failed',
                attempts = $2,
                updated_at = now()
            WHERE id = $1
            """,
            row_id,
            attempts,
        )
        logger.warning("outbox.failed id=%d attempts=%d last_error=%s", row_id, attempts, last_error[:200])
        return "failed"

    delay = compute_backoff_seconds(attempts)
    await conn.execute(
        """
        UPDATE delivery_outbox
        SET attempts = $2,
            next_retry_at = now() + ($3 || ' seconds')::interval,
            updated_at = now()
        WHERE id = $1
        """,
        row_id,
        attempts,
        str(delay),
    )
    logger.info(
        "outbox.retry id=%d attempts=%d/%d delay=%ds last_error=%s",
        row_id, attempts, max_attempts, delay, last_error[:120],
    )
    return "pending"


async def bootstrap_recovery(pool: asyncpg.Pool) -> int:
    """Reset rows stuck in 'sent' or 'ack_missing' back to 'pending' on startup.

    Why: if worker died mid-flight, those rows would never be retried.
    """
    result = await pool.execute(
        """
        UPDATE delivery_outbox
        SET status = 'pending',
            next_retry_at = now(),
            updated_at = now()
        WHERE status IN ('sent', 'ack_missing')
        """
    )
    n = int(result.split()[-1]) if result.startswith("UPDATE ") else 0
    if n > 0:
        logger.warning("outbox.bootstrap reset %d non-terminal rows to pending", n)
    return n


async def get_stats(pool: asyncpg.Pool) -> dict[str, int]:
    """Return counts per status for observability."""
    rows = await pool.fetch(
        "SELECT status, count(*)::int AS c FROM delivery_outbox GROUP BY status"
    )
    return {row["status"]: row["c"] for row in rows}


async def list_recent(
    pool: asyncpg.Pool,
    limit: int = 50,
    status_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return most recent deliveries across all agents (for Kanban dashboard).

    Read-only — does not mutate any rows. Sorted by created_at DESC.
    status_filter: None → all statuses; 'pending' / 'acked' / 'failed' to scope.
    """
    import json as _json
    if status_filter:
        rows = await pool.fetch(
            """
            SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
                   payload::text AS payload_json, created_at, updated_at
            FROM delivery_outbox
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status_filter, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
                   payload::text AS payload_json, created_at, updated_at
            FROM delivery_outbox
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "task_id": r["task_id"],
            "from_agent": r["from_agent"],
            "to_agent": r["to_agent"],
            "status": r["status"],
            "attempts": r["attempts"],
            "max_attempts": r["max_attempts"],
            "payload": _json.loads(r["payload_json"]),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


async def list_pending_for(pool: asyncpg.Pool, to_agent: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return pending deliveries addressed to a specific agent (pull-based delivery).

    Agent calls this on session start to fetch its inbox.
    Status remains 'pending' until agent acks via mark_acked.
    """
    import json as _json
    rows = await pool.fetch(
        """
        SELECT task_id, from_agent, payload::text AS payload_json,
               created_at, attempts
        FROM delivery_outbox
        WHERE to_agent = $1 AND status = 'pending'
        ORDER BY created_at
        LIMIT $2
        """,
        to_agent, limit,
    )
    return [
        {
            "task_id": r["task_id"],
            "from_agent": r["from_agent"],
            "payload": _json.loads(r["payload_json"]),
            "created_at": r["created_at"].isoformat(),
            "attempts": r["attempts"],
        }
        for r in rows
    ]


async def get_row(pool: asyncpg.Pool, task_id: str) -> dict[str, Any] | None:
    """Fetch a single delivery row by task_id."""
    row = await pool.fetchrow(
        """
        SELECT task_id, from_agent, to_agent, status, attempts, max_attempts,
               payload::text AS payload_json, created_at, updated_at,
               next_retry_at
        FROM delivery_outbox
        WHERE task_id = $1
        """,
        task_id,
    )
    if row is None:
        return None
    import json as _json
    return {
        "task_id": row["task_id"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "status": row["status"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "payload": _json.loads(row["payload_json"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "next_retry_at": row["next_retry_at"].isoformat(),
    }


def _json_dumps(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, default=str)
