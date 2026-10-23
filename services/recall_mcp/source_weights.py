"""Source type weights and temporal decay for recall scoring."""

# Ordering rationale: a runbook answers "how do we do this" and is the source of
# truth for repeated work, so it outranks a one-off decision. An error-pattern
# still wins: not repeating a mistake beats knowing the procedure.
# Every source_type written by memory-mcp must appear here -- a missing key
# silently falls back to 1.0 in search.py, which is how handoff used to outrank
# runbook. See tests/test_source_weights.py.
SOURCE_WEIGHTS: dict[str, float] = {
    "error-pattern": 3.0,
    "runbook": 2.5,
    "decision": 2.0,
    "protocol": 1.5,
    "project": 1.2,
    "external": 1.0,
    "knowledge": 1.0,
    "handoff": 0.8,  # addressed mail, not knowledge -- keep it out of the way
    "daily": 0.0,  # excluded from semantic search
    "unknown": 1.0,  # note without a type in frontmatter: neutral, as the fallback was
}

# Hours thresholds for temporal decay buckets
_HOURS_1_DAY = 24
_HOURS_7_DAYS = 168
_HOURS_30_DAYS = 720


def temporal_decay(hours_ago: float) -> float:
    """Compute temporal decay multiplier based on document age.

    Args:
        hours_ago: Hours since the document was last updated.

    Returns:
        Decay multiplier (1.5 for fresh, down to 0.9 for old).
    """
    if hours_ago < _HOURS_1_DAY:
        return 1.5
    if hours_ago < _HOURS_7_DAYS:
        return 1.2
    if hours_ago < _HOURS_30_DAYS:
        return 1.0
    return 0.9
