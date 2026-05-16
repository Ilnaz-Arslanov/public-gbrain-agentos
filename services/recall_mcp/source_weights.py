"""Source type weights and temporal decay for recall scoring."""

SOURCE_WEIGHTS: dict[str, float] = {
    "error-pattern": 3.0,
    "decision": 2.0,
    "runbook": 1.5,
    "protocol": 1.5,
    "project": 1.2,
    "external": 1.0,
    "knowledge": 1.0,
    "daily": 0.0,  # excluded from semantic search
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
