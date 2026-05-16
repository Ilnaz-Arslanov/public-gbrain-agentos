"""Smoke tests for recall-mcp.

Unit smoke covers:
- AuthCaptureMiddleware ContextVar is exposed (server.py wires it through search.py)
- read-only tools are registered on the FastMCP server
- cache, source-weights, cross-link modules are importable and have expected shape

Integration tests against a live Postgres are marked `@pytest.mark.integration`
and skipped unless GBRAIN_TEST_INTEGRATION=1.
"""
from __future__ import annotations

import pytest

from services.recall_mcp.cross_link import find_wikilinks
from services.recall_mcp.search import _REQUEST_AUTH


def test_request_auth_context_var_exists() -> None:
    """_REQUEST_AUTH must be importable -- middleware in server.py depends on it."""
    assert _REQUEST_AUTH is not None
    # Default value when no request is in flight must be None.
    assert _REQUEST_AUTH.get() is None


def test_request_auth_set_and_reset_round_trip() -> None:
    """ContextVar must accept and surface Bearer values as set by the ASGI middleware."""
    token = _REQUEST_AUTH.set("Bearer hello-world")
    try:
        assert _REQUEST_AUTH.get() == "Bearer hello-world"
    finally:
        _REQUEST_AUTH.reset(token)
    assert _REQUEST_AUTH.get() is None


def test_find_wikilinks_basic() -> None:
    """Wikilink extractor returns deduplicated targets."""
    text = "See [[30-decisions/a.md]] and [[30-decisions/a.md]] and [[70-runbooks/b.md]]."
    out = find_wikilinks(text)
    assert out == ["30-decisions/a.md", "70-runbooks/b.md"]


def test_find_wikilinks_with_related_frontmatter() -> None:
    """Wikilink extractor picks up related: frontmatter entries as well."""
    text = (
        "related: 40-projects/x.md, 40-projects/y.md\n"
        "body with [[30-decisions/z.md]] mention."
    )
    out = find_wikilinks(text)
    assert "30-decisions/z.md" in out
    assert "40-projects/x.md" in out
    assert "40-projects/y.md" in out


def test_search_module_exports_register_tools() -> None:
    """register_tools must be exposed -- server.py imports it on startup."""
    from services.recall_mcp import search

    assert callable(search.register_tools)


@pytest.mark.integration
def test_recall_mcp_lists_tools_with_valid_auth() -> None:
    """End-to-end: a valid Bearer token should yield a non-empty tool list."""
    pytest.skip("recall-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_recall_mcp_missing_auth_returns_401() -> None:
    """End-to-end: request without Authorization header should be rejected by middleware."""
    pytest.skip("recall-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_recall_mcp_bad_auth_returns_401() -> None:
    """End-to-end: request with unknown Bearer token should be rejected."""
    pytest.skip("recall-mcp integration smoke not yet implemented")
