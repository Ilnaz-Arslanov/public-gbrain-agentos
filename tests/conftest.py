"""Shared pytest fixtures and marks for gbrain tests.

Integration tests that need a live Postgres / FastEmbed model are marked
with `@pytest.mark.integration` and skipped by default unless the env var
GBRAIN_TEST_INTEGRATION=1 is set.
"""
import os
import sys
from pathlib import Path

import pytest

# Make sibling `services` package importable regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """Register custom marks."""
    config.addinivalue_line(
        "markers",
        "integration: requires a running Postgres + FastEmbed environment",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip integration tests unless GBRAIN_TEST_INTEGRATION=1."""
    if os.environ.get("GBRAIN_TEST_INTEGRATION") == "1":
        return
    skip_integration = pytest.mark.skip(
        reason="integration test (set GBRAIN_TEST_INTEGRATION=1 to enable)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
