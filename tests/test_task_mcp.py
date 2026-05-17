"""Smoke tests for task-mcp.

Unit smoke covers:
- AuthCaptureMiddleware ContextVar is exposed (server.py imports it)
- Tool gating: core mode registers all 13 task tools
- Tool gating: all mode registers all 13 task tools
- _REQUEST_AUTH ContextVar round-trip

Integration tests against a live Postgres are marked `@pytest.mark.integration`
and skipped unless GBRAIN_TEST_INTEGRATION=1.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from services.task_mcp.server import _REQUEST_AUTH


# --- Constants ---------------------------------------------------------------

_ALL_TASK_TOOLS = {
    "task_create",
    "task_update",
    "task_get",
    "task_list",
    "task_start",
    "task_review",
    "task_done",
    "task_block",
    "task_reopen",
    "task_history",
    "agent_heartbeat",
    "agent_status",
    "agent_list",
}


# --- Helpers -----------------------------------------------------------------

def _registered_tool_names(mcp_instance) -> set[str]:
    """Return the set of FastMCP tool names registered on an mcp instance."""
    tools = asyncio.run(mcp_instance._list_tools())
    return {t.name for t in tools}


def _reload_task_server_with_tool_set(
    monkeypatch: pytest.MonkeyPatch, tool_set: str,
):
    """Reload services.task_mcp.server with GBRAIN_TOOLS=<tool_set>."""
    monkeypatch.setenv("GBRAIN_TOOLS", tool_set)
    import services.task_mcp.server as server_mod
    return importlib.reload(server_mod)


# --- ContextVar tests --------------------------------------------------------

def test_request_auth_context_var_exists() -> None:
    """_REQUEST_AUTH must be importable -- middleware depends on it."""
    assert _REQUEST_AUTH is not None
    assert _REQUEST_AUTH.get() is None


def test_request_auth_round_trip() -> None:
    """Setting and resetting the ContextVar mirrors the ASGI middleware."""
    token = _REQUEST_AUTH.set("Bearer abc")
    try:
        assert _REQUEST_AUTH.get() == "Bearer abc"
    finally:
        _REQUEST_AUTH.reset(token)
    assert _REQUEST_AUTH.get() is None


# --- Tool gating tests -------------------------------------------------------

def test_task_tools_core_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core mode registers all 13 task tools (they ARE the core set)."""
    mod = _reload_task_server_with_tool_set(monkeypatch, "core")
    registered = _registered_tool_names(mod.mcp)
    assert registered == _ALL_TASK_TOOLS


def test_task_tools_all_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """All mode also registers all 13 task tools."""
    mod = _reload_task_server_with_tool_set(monkeypatch, "all")
    registered = _registered_tool_names(mod.mcp)
    assert registered == _ALL_TASK_TOOLS


def test_task_tool_annotations_readonly() -> None:
    """Read-only tools must have readOnlyHint=True annotations."""
    readonly_tools = {"task_get", "task_list", "task_history", "agent_status", "agent_list"}
    import services.task_mcp.server as server_mod
    mod = importlib.reload(server_mod)
    tools = asyncio.run(mod.mcp._list_tools())
    tool_map = {t.name: t for t in tools}
    for name in readonly_tools:
        assert name in tool_map, f"{name} not registered"
        annotations = getattr(tool_map[name], "annotations", None)
        if annotations:
            assert annotations.readOnlyHint is True, f"{name} missing readOnlyHint"


def test_write_scope_constant() -> None:
    """TASKS_WRITE_SCOPE must be '10-tasks'."""
    from services.task_mcp.server import TASKS_WRITE_SCOPE
    assert TASKS_WRITE_SCOPE == "10-tasks"
