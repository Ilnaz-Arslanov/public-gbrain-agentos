"""Invariants for recall source-type weights.

A source_type missing from SOURCE_WEIGHTS does not fail loudly: search.py falls
back to 1.0, so the type quietly scores above runbook and below decision. That
is exactly how handoff behaved until 2026-09-04. These tests pin both the
ordering and the coverage.
"""
import re
from pathlib import Path

from services.recall_mcp.source_weights import SOURCE_WEIGHTS

_TOOLS_PY = Path(__file__).resolve().parents[1] / "services" / "memory_mcp" / "tools.py"


class TestSourceWeights:
    def test_error_pattern_outranks_runbook_outranks_decision(self) -> None:
        assert SOURCE_WEIGHTS["error-pattern"] > SOURCE_WEIGHTS["runbook"]
        assert SOURCE_WEIGHTS["runbook"] > SOURCE_WEIGHTS["decision"]

    def test_handoff_is_explicit_and_below_decision(self) -> None:
        # Mail is addressed and delivered by swarm; it should not crowd out
        # notes in semantic search. Explicit beats the silent 1.0 fallback.
        assert "handoff" in SOURCE_WEIGHTS
        assert SOURCE_WEIGHTS["handoff"] < SOURCE_WEIGHTS["decision"]

    def test_daily_stays_excluded(self) -> None:
        assert SOURCE_WEIGHTS["daily"] == 0.0

    def test_every_written_source_type_has_a_weight(self) -> None:
        """No source_type that memory-mcp writes may rely on the 1.0 fallback."""
        source = _TOOLS_PY.read_text(encoding="utf-8")
        written = {
            t
            for t in re.findall(r'["\'](decision|error-pattern|runbook|handoff|external|daily)["\']', source)
        }
        assert written, "no source types found in memory_mcp/tools.py -- check the pattern"
        missing = sorted(written - SOURCE_WEIGHTS.keys())
        assert not missing, f"source types written but not weighted: {missing}"

    def test_weights_are_non_negative(self) -> None:
        assert all(w >= 0.0 for w in SOURCE_WEIGHTS.values())
