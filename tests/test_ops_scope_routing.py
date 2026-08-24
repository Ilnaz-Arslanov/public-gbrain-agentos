"""Tests for `_resolve_write_target` -- the 50-ops write fallback.

Every memory write tool hardcodes its scope (`create_decision_note` always
targets `30-decisions`, `append_daily_log` always `20-daily`, ...). Taking
those scopes off an operator-run agent's token would therefore leave it with
nothing to write. `_resolve_write_target` closes that: an agent that lacks the
canonical scope but holds `50-ops` writes into `50-ops/<agent>/<scope>/`.

`50-ops` is deliberately one flat scope -- operator agents see each other's
notes, our scopes stay out of reach.
"""
from __future__ import annotations

import pytest

from services.memory_mcp.tools import OPS_SCOPE, _resolve_write_target
from services.shared.auth import AgentContext


def _ctx(agent: str, write_scopes: list[str]) -> AgentContext:
    return AgentContext(agent=agent, write_scopes=write_scopes, read_scopes=[])


class TestResolveWriteTarget:
    """Scope resolution driven purely by the token's write permissions."""

    def test_wildcard_agent_writes_canonical_scope(self) -> None:
        """Our agents hold '*' and keep writing exactly where they did."""
        scope, prefix = _resolve_write_target(_ctx("cody", ["*"]), "30-decisions")
        assert (scope, prefix) == ("30-decisions", "30-decisions")

    def test_explicit_scope_holder_writes_canonical_scope(self) -> None:
        """An explicit grant is honored without touching the fallback."""
        ctx = _ctx("archie", ["20-daily", "90-inbox"])
        assert _resolve_write_target(ctx, "20-daily") == ("20-daily", "20-daily")

    def test_ops_agent_falls_back_to_own_subfolder(self) -> None:
        """No canonical scope + 50-ops -> per-agent subfolder inside 50-ops."""
        ctx = _ctx("ivi", [OPS_SCOPE, "90-inbox"])
        scope, prefix = _resolve_write_target(ctx, "30-decisions")
        assert scope == OPS_SCOPE
        assert prefix == "50-ops/ivi/30-decisions"

    def test_fallback_keeps_agents_separated_by_path(self) -> None:
        """Two ops agents never land on the same path for the same day."""
        _, ivi = _resolve_write_target(_ctx("ivi", [OPS_SCOPE]), "20-daily")
        _, ilnar = _resolve_write_target(_ctx("ilnar", [OPS_SCOPE]), "20-daily")
        assert ivi != ilnar

    def test_agent_name_is_slugified_into_the_path(self) -> None:
        """A hostile agent name cannot escape its subfolder."""
        ctx = _ctx("../../etc", [OPS_SCOPE])
        _, prefix = _resolve_write_target(ctx, "20-daily")
        assert ".." not in prefix
        assert prefix.startswith(f"{OPS_SCOPE}/")

    def test_no_scope_and_no_fallback_denies(self) -> None:
        """Holding neither scope is still a denial, not a silent redirect."""
        ctx = _ctx("legal", ["90-inbox"])
        with pytest.raises(PermissionError, match="cannot write to 30-decisions"):
            _resolve_write_target(ctx, "30-decisions")

    @pytest.mark.parametrize(
        "canonical",
        ["20-daily", "30-decisions", "50-external", "70-runbooks", "80-error-patterns"],
    )
    def test_every_routed_scope_has_a_fallback(self, canonical: str) -> None:
        """All routed tools resolve for an ops agent, none raise."""
        scope, prefix = _resolve_write_target(_ctx("irek", [OPS_SCOPE]), canonical)
        assert scope == OPS_SCOPE
        assert prefix == f"{OPS_SCOPE}/irek/{canonical}"
