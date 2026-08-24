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

from services.memory_mcp.tools import (
    OPS_SCOPE,
    _agent_path_component,
    _assert_ops_subtree_owner,
    _canonical_kind_from_path,
    _resolve_write_target,
    _slugify,
)
from services.memory_mcp.path_guard import normalize_rel_path
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


class TestAgentPathComponent:
    """Agent -> path component must be injective (Codex review, 2026-08-24)."""

    def test_plain_name_stays_itself(self) -> None:
        """Ordinary agent names keep readable, unsuffixed folders."""
        assert _agent_path_component("cody") == "cody"
        assert _agent_path_component("ivi2") == "ivi2"

    def test_colliding_names_get_distinct_components(self) -> None:
        """Two names that slugify alike must not share one subtree."""
        assert _agent_path_component("ivi.2") != _agent_path_component("ivi-2")

    def test_digest_component_cannot_be_impersonated(self) -> None:
        """A name spelled like someone else's digest component still differs.

        Second Codex finding: appending a digest to a lossy slug was not
        injective -- an agent literally named like the encoded form of
        another agent mapped onto it.
        """
        encoded = _agent_path_component("иви 2")
        assert encoded.startswith("_")
        assert _agent_path_component(encoded) != encoded

    def test_mapping_is_injective_across_awkward_names(self) -> None:
        names = ["ivi.2", "ivi-2", "IVI-2", "ivi_2", "ivi 2", "иви-2", "_ivi-2"]
        components = {_agent_path_component(n) for n in names}
        assert len(components) == len(names)

    def test_component_is_stable(self) -> None:
        """Same name, same folder -- across calls and restarts."""
        assert _agent_path_component("Ильназ") == _agent_path_component("Ильназ")

    def test_component_is_path_safe(self) -> None:
        """Nothing that could climb out of the subtree survives."""
        for hostile in ["../../etc", "a/b", "~root", ""]:
            component = _agent_path_component(hostile)
            assert "/" not in component
            assert ".." not in component
            assert "~" not in component
            assert component


class TestCanonicalKind:
    """Nested 50-ops notes keep the guarantees of their canonical scope."""

    def test_top_level_path_is_its_own_kind(self) -> None:
        assert _canonical_kind_from_path("30-decisions/note.md") == "30-decisions"

    def test_nested_decision_is_still_a_decision(self) -> None:
        """Immutability must not be escapable by nesting under 50-ops."""
        path = "50-ops/ivi/30-decisions/2026-08-24-note.md"
        assert _canonical_kind_from_path(path) == "30-decisions"

    def test_bare_ops_path_falls_back_to_scope(self) -> None:
        assert _canonical_kind_from_path("50-ops/ivi") == OPS_SCOPE


class TestOpsSubtreeOwner:
    """Write permission on 50-ops is not permission to touch another agent."""

    def test_own_subtree_passes(self) -> None:
        ctx = _ctx("ivi", [OPS_SCOPE])
        _assert_ops_subtree_owner(ctx, "50-ops/ivi/20-daily/2026-08-24.md")

    def test_foreign_subtree_denied(self) -> None:
        ctx = _ctx("ivi", [OPS_SCOPE])
        with pytest.raises(PermissionError, match="may only touch"):
            _assert_ops_subtree_owner(ctx, "50-ops/ilnar/20-daily/2026-08-24.md")

    def test_wildcard_token_is_not_a_bypass(self) -> None:
        """'*' grants the scope, not other agents' subtrees inside it."""
        with pytest.raises(PermissionError):
            _assert_ops_subtree_owner(_ctx("cody", ["*"]), "50-ops/ivi/x.md")

    def test_scope_root_denied(self) -> None:
        """A bare '50-ops' target belongs to nobody in particular."""
        with pytest.raises(PermissionError):
            _assert_ops_subtree_owner(_ctx("ivi", [OPS_SCOPE]), "50-ops")

    def test_other_scopes_untouched(self) -> None:
        """Outside 50-ops the guard is a no-op -- ordinary rights decide."""
        _assert_ops_subtree_owner(_ctx("cody", ["*"]), "30-decisions/note.md")


class TestPathSpellingGuard:
    """Non-canonical spellings must not slip past segment-based guards."""

    def test_dot_segment_rejected(self) -> None:
        """Second Codex finding: './' hid the canonical kind from the guard."""
        with pytest.raises(ValueError, match="Redundant"):
            normalize_rel_path("50-ops/ivi/./30-decisions/note.md")

    def test_empty_segment_rejected(self) -> None:
        with pytest.raises(ValueError, match="Empty path segment"):
            normalize_rel_path("50-ops/ivi//30-decisions/note.md")

    def test_traversal_and_home_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            normalize_rel_path("50-ops/ivi/../ilnar/note.md")
        with pytest.raises(ValueError, match="Home expansion"):
            normalize_rel_path("~/note.md")

    def test_absolute_rejected(self) -> None:
        with pytest.raises(ValueError, match="Absolute"):
            normalize_rel_path("/etc/passwd")

    def test_plain_path_passes_through(self) -> None:
        path = "50-ops/ivi/30-decisions/2026-08-24-note.md"
        assert normalize_rel_path(path) == path

    def test_dot_spelling_would_have_hidden_the_kind(self) -> None:
        """Proves the bite: without the guard, classification was bypassable."""
        assert _canonical_kind_from_path("50-ops/ivi/./30-decisions/n.md") != "30-decisions"


class TestSourceComponent:
    """External-note source must always yield a non-empty path component.

    Third Codex round: the stricter path validation turned a previously
    silent '//' into a hard failure for sources with no ASCII alphanumerics.
    """

    def test_cyrillic_source_transliterates(self) -> None:
        assert _slugify("телеграм") == "telegram"

    def test_symbol_only_source_is_not_empty(self) -> None:
        for source in ["!!!", "—", "   ", "***"]:
            assert _slugify(source)

    def test_distinct_empty_sources_stay_distinct(self) -> None:
        assert _slugify("!!!") != _slugify("***")
