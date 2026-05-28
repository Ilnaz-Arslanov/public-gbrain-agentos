"""Result model and renderers for gbrain-doctor.

Defines the shared ``CheckResult`` dataclass (mirroring the server-side
doctor's shape), the status ranking used for verdicts, ANSI color tags, and
the table / JSON renderers plus summary/verdict helpers.

All ``CheckResult.message`` strings are expected to be pre-redacted by the
caller (see ``redact.py``). The renderers here do NOT re-redact — they trust
the contract that every message was constructed through ``redact()``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Optional

Status = Literal["pass", "warn", "fail", "skip"]


@dataclass
class CheckResult:
    """Outcome of a single doctor check.

    Attributes:
        name: Check identifier, group-prefixed snake_case, e.g.
            ``"G6.hook_scripts_executable"``.
        status: One of ``pass`` / ``warn`` / ``fail`` / ``skip``.
        message: Human-readable detail. MUST already be redacted by the
            caller before construction.
        remediation: Optional operator hint shown for warn/fail rows.
        auto_fix: Optional zero-arg callback returning ``True`` on success.
            NEVER serialized; only invoked under ``--fix`` with confirmation.
    """

    name: str
    status: Status
    message: str
    remediation: Optional[str] = None
    auto_fix: Optional[Callable[[], bool]] = None

    def to_serializable(self) -> dict:
        """Return a JSON-safe dict dropping the ``auto_fix`` callable.

        Returns:
            A dict with keys ``name``, ``status``, ``message``,
            ``remediation`` only.
        """
        data = asdict(self)
        data.pop("auto_fix", None)
        return data


# Severity ranking. Higher number = more severe. Used by ``verdict`` to pick
# the overall headline status (fail > warn > skip > pass for the verdict word,
# though skip never dominates pass in the headline — see ``verdict``).
STATUS_RANK: dict[str, int] = {
    "pass": 0,
    "skip": 1,
    "warn": 2,
    "fail": 3,
}

# ANSI color codes (SGR). Kept as a small mapping so renderers stay terse.
_GREEN = 92
_YELLOW = 93
_RED = 91
_DIM = 2

_STATUS_COLOR: dict[str, int] = {
    "pass": _GREEN,
    "warn": _YELLOW,
    "fail": _RED,
    "skip": _DIM,
}

_STATUS_LABEL: dict[str, str] = {
    "pass": "[PASS]",
    "warn": "[WARN]",
    "fail": "[FAIL]",
    "skip": "[SKIP]",
}


def _color(text: str, code: int, *, no_color: bool) -> str:
    """Wrap ``text`` in an ANSI escape, or return it plain when disabled."""
    if no_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def _status_tag(status: Status, *, no_color: bool) -> str:
    """Format the bracketed colored status tag for a table row."""
    label = _STATUS_LABEL.get(status, f"[{status.upper()}]")
    code = _STATUS_COLOR.get(status, _DIM)
    return _color(label, code, no_color=no_color)


def render_table(results: list[CheckResult], no_color: bool) -> str:
    """Render results as a left-aligned plain-text table.

    Args:
        results: Ordered check results.
        no_color: When ``True``, omit all ANSI escapes.

    Returns:
        The full table including the trailing verdict line, as a single
        string (no trailing newline).
    """
    lines: list[str] = []
    name_width = max((len(r.name) for r in results), default=10)
    # Stable severity ordering: most severe first (fail > warn > skip > pass)
    # via STATUS_RANK, preserving original order within each rank. Python's
    # sort is stable, so equal-rank rows keep their relative input order.
    ordered = sorted(
        results,
        key=lambda r: STATUS_RANK.get(r.status, 0),
        reverse=True,
    )
    for r in ordered:
        tag = _status_tag(r.status, no_color=no_color)
        lines.append(f"{tag} {r.name.ljust(name_width)}  {r.message}")
        if r.status in ("fail", "warn") and r.remediation:
            hint = _color(f"        -> {r.remediation}", _DIM, no_color=no_color)
            lines.append(hint)

    verdict_word, _ = verdict(results)
    counts = summary_counts(results)
    summary = (
        f"{counts['pass']} pass, {counts['warn']} warn, "
        f"{counts['fail']} fail, {counts['skip']} skip"
    )
    lines.append("")
    lines.append(
        f"Verdict: {verdict_word} ({summary}) -- output redacted and safe to paste"
    )
    return "\n".join(lines)


def render_json(results: list[CheckResult]) -> str:
    """Render results as a JSON array of serializable check dicts.

    Args:
        results: Ordered check results.

    Returns:
        A pretty-printed JSON array string. ``auto_fix`` is always omitted.
    """
    return json.dumps(
        [r.to_serializable() for r in results],
        indent=2,
        ensure_ascii=False,
    )


def summary_counts(results: list[CheckResult]) -> dict:
    """Tally results by status.

    Args:
        results: Ordered check results.

    Returns:
        A dict with integer counts for keys ``pass``, ``warn``, ``fail``,
        ``skip``.
    """
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        if r.status in counts:
            counts[r.status] += 1
    return counts


def verdict(results: list[CheckResult]) -> tuple[str, int]:
    """Compute the overall verdict word and process exit code.

    Args:
        results: Ordered check results.

    Returns:
        ``("FAIL", 1)`` if any result failed, ``("WARN", 0)`` if any warned
        (but none failed), otherwise ``("PASS", 0)``. Exit code is ``1`` only
        when at least one ``fail`` is present; warn/skip do not fail the run.
    """
    has_fail = any(r.status == "fail" for r in results)
    has_warn = any(r.status == "warn" for r in results)
    if has_fail:
        return ("FAIL", 1)
    if has_warn:
        return ("WARN", 0)
    return ("PASS", 0)
