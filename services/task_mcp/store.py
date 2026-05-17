"""Task board database operations -- CRUD + status state machine.

Tables:
    tasks: id, title, description, status, assignee, priority, created_by,
           created_at, updated_at, metadata (jsonb)
    task_history: id, task_id (FK), old_status, new_status, changed_by,
                  note, changed_at

Status state machine:
    new -> progress, blocked
    progress -> review, blocked
    review -> done, progress
    blocked -> new, progress
    done is terminal (no transitions out)
"""
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

VALID_STATUSES: frozenset[str] = frozenset({
    "new", "progress", "review", "done", "blocked",
})

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"progress", "blocked"}),
    "progress": frozenset({"review", "blocked"}),
    "review": frozenset({"done", "progress"}),
    "blocked": frozenset({"new", "progress"}),
    "done": frozenset(),
}

VALID_PRIORITIES: frozenset[str] = frozenset({
    "low", "medium", "high", "critical",
})


def _json_dumps(data: dict[str, Any]) -> str:
    """Serialize dict to JSON string for JSONB columns."""
    import json
    return json.dumps(data, ensure_ascii=False, default=str)


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg Record from tasks table to a plain dict.

    Serializes datetime fields to ISO format and parses metadata JSONB.
    """
    import json as _json
    result: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "assignee": row["assignee"],
        "priority": row["priority"],
        "created_by": row["created_by"],
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


def _history_row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert a task_history Record to a plain dict."""
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "old_status": row["old_status"],
        "new_status": row["new_status"],
        "changed_by": row["changed_by"],
        "note": row["note"],
        "changed_at": row["changed_at"].isoformat(),
    }


async def create_task(
    pool: asyncpg.Pool,
    agent: str,
    title: str,
    description: str = "",
    assignee: str | None = None,
    priority: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new task and return it as a dict.

    Args:
        pool: Asyncpg connection pool.
        agent: Agent creating the task (from auth context).
        title: Task title (required, non-empty).
        description: Task description.
        assignee: Agent assigned to the task.
        priority: One of low, medium, high, critical.
        metadata: Arbitrary JSONB metadata.

    Returns:
        Created task as a dict with all fields.

    Raises:
        ValueError: If title is empty or priority is invalid.
    """
    if not title or not title.strip():
        raise ValueError("title must not be empty")
    if priority not in VALID_PRIORITIES:
        raise ValueError(
            f"invalid priority {priority!r}, must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}"
        )

    meta_json = _json_dumps(metadata or {})
    row = await pool.fetchrow(
        """
        INSERT INTO tasks
          (title, description, status, assignee, priority, created_by, metadata)
        VALUES ($1, $2, 'new', $3, $4, $5, $6::jsonb)
        RETURNING id, title, description, status, assignee, priority,
                  created_by, created_at, updated_at,
                  metadata::text AS metadata_json
        """,
        title.strip(),
        description,
        assignee,
        priority,
        agent,
        meta_json,
    )
    logger.info(
        "task.create id=%d title=%r by=%s assignee=%s",
        row["id"], title, agent, assignee,
    )
    return _row_to_dict(row)


async def update_task(
    pool: asyncpg.Pool,
    task_id: int,
    agent: str,
    **fields: Any,
) -> dict[str, Any]:
    """Update specific fields of a task. Only provided fields are changed.

    Args:
        pool: Asyncpg connection pool.
        task_id: Task ID to update.
        agent: Agent performing the update (for audit trail).
        **fields: Fields to update. Allowed: title, description, assignee,
                  priority, metadata.

    Returns:
        Updated task as a dict.

    Raises:
        ValueError: If no valid fields provided, task not found, or invalid values.
    """
    allowed_fields = {"title", "description", "assignee", "priority", "metadata"}
    updates = {k: v for k, v in fields.items() if k in allowed_fields and v is not None}

    if not updates:
        raise ValueError("no valid fields to update")

    if "title" in updates:
        if not updates["title"] or not updates["title"].strip():
            raise ValueError("title must not be empty")
        updates["title"] = updates["title"].strip()

    if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
        raise ValueError(
            f"invalid priority {updates['priority']!r}, must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}"
        )

    # Build dynamic SET clause
    set_parts: list[str] = []
    params: list[Any] = []
    param_idx = 1

    for field_name, value in updates.items():
        if field_name == "metadata":
            set_parts.append(f"metadata = ${param_idx}::jsonb")
            params.append(_json_dumps(value))
        else:
            set_parts.append(f"{field_name} = ${param_idx}")
            params.append(value)
        param_idx += 1

    set_parts.append("updated_at = now()")
    params.append(task_id)

    query = f"""
        UPDATE tasks
        SET {', '.join(set_parts)}
        WHERE id = ${param_idx}
        RETURNING id, title, description, status, assignee, priority,
                  created_by, created_at, updated_at,
                  metadata::text AS metadata_json
    """

    row = await pool.fetchrow(query, *params)
    if row is None:
        raise ValueError(f"task {task_id} not found")

    logger.info(
        "task.update id=%d fields=%s by=%s",
        task_id, list(updates.keys()), agent,
    )
    return _row_to_dict(row)


async def get_task(pool: asyncpg.Pool, task_id: int) -> dict[str, Any] | None:
    """Fetch a single task by ID.

    Args:
        pool: Asyncpg connection pool.
        task_id: Task ID to fetch.

    Returns:
        Task dict or None if not found.
    """
    row = await pool.fetchrow(
        """
        SELECT id, title, description, status, assignee, priority,
               created_by, created_at, updated_at,
               metadata::text AS metadata_json
        FROM tasks
        WHERE id = $1
        """,
        task_id,
    )
    if row is None:
        return None
    return _row_to_dict(row)


async def list_tasks(
    pool: asyncpg.Pool,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List tasks with optional filters.

    Args:
        pool: Asyncpg connection pool.
        assignee: Filter by assignee agent name.
        status: Filter by task status.
        limit: Maximum number of tasks to return.

    Returns:
        List of task dicts, ordered by created_at DESC.

    Raises:
        ValueError: If status filter is not a valid status.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status filter {status!r}, must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )

    conditions: list[str] = []
    params: list[Any] = []
    param_idx = 1

    if assignee is not None:
        conditions.append(f"assignee = ${param_idx}")
        params.append(assignee)
        param_idx += 1

    if status is not None:
        conditions.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1

    params.append(limit)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT id, title, description, status, assignee, priority,
               created_by, created_at, updated_at,
               metadata::text AS metadata_json
        FROM tasks
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${param_idx}
    """

    rows = await pool.fetch(query, *params)
    return [_row_to_dict(r) for r in rows]


async def transition_status(
    pool: asyncpg.Pool,
    task_id: int,
    agent: str,
    new_status: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Transition a task to a new status following the state machine.

    Validates the transition against VALID_TRANSITIONS, updates the task,
    and inserts a history record.

    Args:
        pool: Asyncpg connection pool.
        task_id: Task ID to transition.
        agent: Agent performing the transition.
        new_status: Target status.
        note: Optional note for the history record.

    Returns:
        Updated task as a dict.

    Raises:
        ValueError: If task not found, status invalid, or transition not allowed.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {new_status!r}, must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the row to prevent concurrent transitions
            row = await conn.fetchrow(
                """
                SELECT id, status
                FROM tasks
                WHERE id = $1
                FOR UPDATE
                """,
                task_id,
            )
            if row is None:
                raise ValueError(f"task {task_id} not found")

            old_status = row["status"]
            allowed = VALID_TRANSITIONS.get(old_status, frozenset())
            if new_status not in allowed:
                raise ValueError(
                    f"transition {old_status!r} -> {new_status!r} not allowed "
                    f"for task {task_id}. Valid targets: "
                    f"{', '.join(sorted(allowed)) or 'none (terminal)'}"
                )

            # Update task status
            updated = await conn.fetchrow(
                """
                UPDATE tasks
                SET status = $1, updated_at = now()
                WHERE id = $2
                RETURNING id, title, description, status, assignee, priority,
                          created_by, created_at, updated_at,
                          metadata::text AS metadata_json
                """,
                new_status,
                task_id,
            )

            # Insert history record
            await conn.execute(
                """
                INSERT INTO task_history
                  (task_id, old_status, new_status, changed_by, note)
                VALUES ($1, $2, $3, $4, $5)
                """,
                task_id,
                old_status,
                new_status,
                agent,
                note,
            )

    logger.info(
        "task.transition id=%d %s->%s by=%s note=%s",
        task_id, old_status, new_status, agent, note,
    )
    return _row_to_dict(updated)


async def list_task_history(
    pool: asyncpg.Pool,
    task_id: int,
) -> list[dict[str, Any]]:
    """List status transition history for a task.

    Args:
        pool: Asyncpg connection pool.
        task_id: Task ID to get history for.

    Returns:
        List of history dicts, ordered by changed_at ASC.
    """
    rows = await pool.fetch(
        """
        SELECT id, task_id, old_status, new_status, changed_by, note, changed_at
        FROM task_history
        WHERE task_id = $1
        ORDER BY changed_at ASC
        """,
        task_id,
    )
    return [_history_row_to_dict(r) for r in rows]
