"""Stage 2 ACK protocol: transport delivery and agent acknowledgement are
separate states.

Before Stage 2 the worker wrote 'acked' on any 2xx from the gateway, so a
letter delivered to a frozen agent looked exactly like one that was handled.
These tests pin the split: 2xx → 'sent', agent's own swarm.ack → 'acked', and
'sent' rows that nobody acks are flagged 'ack_missing' rather than re-delivered.

The lifecycle test needs a live Postgres and is marked `integration`
(GBRAIN_TEST_INTEGRATION=1); the rest are pure unit guards.
"""
from __future__ import annotations

import asyncio
import inspect
import os

import pytest

os.environ.setdefault("MCP_PORT", "0")

from services.swarm_mcp import outbox  # noqa: E402
from services.swarm_mcp.worker import _format_virtual_prompt  # noqa: E402


# --- Unit guards -------------------------------------------------------------


def _sql_of(fn) -> str:
    """Return a function's source with its docstring stripped.

    The guards below assert on the SQL these functions run, and every one of
    them discusses the rejected alternative in prose right above it.
    """
    src = inspect.getsource(fn)
    doc = inspect.getdoc(fn)
    if doc:
        for line in doc.splitlines():
            src = src.replace(line, "")
    return src


def test_bootstrap_recovery_is_gone() -> None:
    """The startup reset of 'sent'/'ack_missing' → 'pending' must not come back.

    With Stage 2 live, 'sent' means the agent already received the letter.
    Resetting those rows on startup would re-deliver every unacked letter on
    each worker restart — re-running tasks agents may have already performed.
    """
    assert not hasattr(outbox, "bootstrap_recovery")


def test_mark_sent_only_advances_pending_rows() -> None:
    """mark_sent is scoped to 'pending' so it can never overwrite a real ack.

    A delivery that the agent acked before the worker's UPDATE landed must stay
    'acked'; widening this predicate would silently downgrade it back to 'sent'.
    """
    src = _sql_of(outbox.mark_sent)
    assert "SET status = 'sent'" in src
    assert "status = 'pending'" in src


def test_sweep_never_returns_rows_to_pending() -> None:
    """ack_missing is an observability signal, not an automatic re-delivery.

    The letter did arrive. Re-queueing it would re-run tasks for the agents that
    do the work but forget to ack — the common case, not the rare one.
    """
    src = _sql_of(outbox.sweep_ack_missing)
    assert "SET status = 'ack_missing'" in src
    assert "'pending'" not in src


def test_pull_inbox_excludes_ack_missing() -> None:
    """list_my_pending must not re-serve already-delivered letters."""
    src = _sql_of(outbox.list_pending_for)
    assert "status = 'pending'" in src
    assert "ack_missing" not in src


# --- Integration: full lifecycle on a live Postgres --------------------------


@pytest.mark.integration
def test_delivery_lifecycle_pending_sent_ack_missing_acked() -> None:
    """pending → sent → ack_missing → acked, with no re-delivery in between."""
    from services.shared.config import Config
    from services.shared.db import close_pool, get_pool

    task_id = f"test::ack-protocol::{os.getpid()}"

    async def scenario() -> dict[str, object]:
        pool = await get_pool(Config(mcp_port=0))
        seen: dict[str, object] = {}
        try:
            await pool.execute("DELETE FROM delivery_outbox WHERE task_id = $1", task_id)
            await outbox.enqueue(pool, "test-sender", "test-receiver", {"title": "t"}, task_id)

            row = await outbox.get_row(pool, task_id)
            seen["after_enqueue"] = row["status"]
            seen["in_pull_inbox_when_pending"] = any(
                d["task_id"] == task_id
                for d in await outbox.list_pending_for(pool, "test-receiver", limit=50)
            )

            async with pool.acquire() as conn:
                seen["mark_sent_ok"] = await outbox.mark_sent(conn, task_id)
            seen["after_sent"] = (await outbox.get_row(pool, task_id))["status"]

            # A fresh 'sent' row is not yet overdue: the receiver is still working.
            await outbox.sweep_ack_missing(pool, timeout_sec=3600)
            seen["after_sweep_not_due"] = (await outbox.get_row(pool, task_id))["status"]

            # Past the ack deadline it is flagged — but stays out of the inbox.
            await outbox.sweep_ack_missing(pool, timeout_sec=0)
            seen["after_sweep_due"] = (await outbox.get_row(pool, task_id))["status"]
            seen["in_pull_inbox_when_ack_missing"] = any(
                d["task_id"] == task_id
                for d in await outbox.list_pending_for(pool, "test-receiver", limit=50)
            )

            # A late ack from the agent still closes the row.
            async with pool.acquire() as conn:
                seen["late_ack_ok"] = await outbox.mark_acked(conn, task_id)
                seen["repeat_ack_ok"] = await outbox.mark_acked(conn, task_id)
            seen["after_ack"] = (await outbox.get_row(pool, task_id))["status"]
        finally:
            await pool.execute("DELETE FROM delivery_outbox WHERE task_id = $1", task_id)
            await close_pool()
        return seen

    seen = asyncio.run(scenario())

    assert seen["after_enqueue"] == "pending"
    assert seen["in_pull_inbox_when_pending"] is True
    assert seen["mark_sent_ok"] is True
    assert seen["after_sent"] == "sent", "a 2xx from the gateway must not mean 'acked'"
    assert seen["after_sweep_not_due"] == "sent"
    assert seen["after_sweep_due"] == "ack_missing"
    assert seen["in_pull_inbox_when_ack_missing"] is False, "must not re-serve a delivered letter"
    assert seen["late_ack_ok"] is True
    assert seen["after_ack"] == "acked"
    assert seen["repeat_ack_ok"] is False, "ack is idempotent: the second call changes nothing"


@pytest.mark.integration
def test_ack_reports_status_when_it_changed_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op ack tells the caller why: 'acked' already, or 'not_found'.

    Request identity and audit logging are stubbed — this pins the ack tool's
    return contract, not the auth middleware that fills the ContextVar.
    """
    from services.swarm_mcp import server

    async def _fake_caller(ctx: object, pool: object) -> str:
        return "test-caller"

    async def _fake_audit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(server, "_resolve_caller", _fake_caller)
    monkeypatch.setattr(server, "log_audit", _fake_audit)

    async def scenario() -> dict[str, object]:
        from services.shared.db import close_pool
        try:
            return await server.ack.fn(task_id="test::ack-protocol::nonexistent")
        finally:
            await close_pool()

    result = asyncio.run(scenario())
    assert result["acked"] is False
    assert result["status"] == "not_found"


# --- Letter envelope ---------------------------------------------------------


def test_report_letter_asks_to_read_not_to_execute() -> None:
    """``_kind="report"`` drops the execute-and-report imperative, keeps the ack."""
    prompt = _format_virtual_prompt(
        "daisy", "cody", "t-2", {"title": "Audit results", "body": "B", "_kind": "report"}
    )
    assert "(report)" in prompt
    assert "Report: Audit results" in prompt
    assert "Execute the task." not in prompt
    assert "What I did:" not in prompt
    assert 'swarm.ack(task_id="t-2")' in prompt


def test_report_kind_does_not_collide_with_letter_title() -> None:
    """``kind`` still titles a normal letter; only ``_kind`` switches the envelope.

    Senders in the wild use ``kind`` as a subject line (daisy, 2026-08-09), so
    reading the envelope switch off it would silently mute real tasks.
    """
    prompt = _format_virtual_prompt(
        "daisy", "cody", "t-3", {"kind": "report", "facts": ["f1"]}
    )
    assert "Task: report" in prompt
    assert "Execute the task." in prompt


def test_task_letter_still_demands_execution_and_ack() -> None:
    """The default envelope keeps its imperative block.

    Removing it was proposed during the 2026-08-11 bridge audit; it is the only
    thing that makes a receiving agent do the work, report to the owner and
    close the row with swarm.ack.
    """
    prompt = _format_virtual_prompt("daisy", "cody", "t-1", {"title": "T", "body": "B"})
    assert "Execute the task." in prompt
    assert 'swarm.ack(task_id="t-1")' in prompt
