"""Secret redaction for gbrain-doctor output.

Single source of truth for masking Bearer tokens, HMAC secrets/digests,
``sk-*`` API keys, and ``password=...`` values before anything reaches
stdout, stderr, JSON serialization, or autofix audit logs.

Every ``CheckResult.message`` and every exception message surfaced by the
doctor MUST pass through :func:`redact` first. Raw tokens are kept only in
memory and shown via :func:`mask_token` / :func:`mask_hmac`.
"""

from __future__ import annotations

import re
from typing import Iterable

# Toxic regex (case-insensitive). Matches the most common secret shapes and
# collapses each to the literal ``<REDACTED>`` marker. Ordering inside the
# alternation matters only where one alternative is a prefix of another — the
# more specific key=value / env-style forms are listed before the bare-value
# forms so the readable ``KEY=<REDACTED>`` shape wins where it applies.
# ``re.sub`` walks left-to-right and replaces every non-overlapping match.
#
# Groups:
#   1. GitHub OAuth/app/user/server tokens: ``gho_...`` / ``ghp_...`` etc.
#   2. GitHub fine-grained PATs: ``github_pat_...``.
#   3. env-style assignments of known secret-bearing variable names. The
#      variable name is captured (group ``envkey``) so we can re-emit it as
#      ``KEY=<REDACTED>`` and keep the output diagnosable.
#   4. ``authorization: bearer <x>`` / ``authorization: token <x>`` headers.
#   5. key/value secret forms: ``token: "x"`` / ``api_key=x`` / ``secret=x``
#      (the value is captured as ``kvval`` and stripped).
#   6. legacy bare-value shapes: ``Bearer <x>``, ``sk-...``, ``hmac_...``,
#      ``password=...`` (kept verbatim for backward compatibility).
_TOXIC_RE = re.compile(
    r"""
    (?:gh[opsu]_[A-Za-z0-9]{20,})
    | (?:github_pat_[A-Za-z0-9_]{20,})
    | (?P<envkey>GH_TOKEN|GITHUB_TOKEN|WEBHOOK_HMAC_SECRET|WEBHOOK_BEARER
        |[A-Z0-9_]*SECRET|[A-Z0-9_]*TOKEN|[A-Z0-9_]*API_KEY|[A-Z0-9_]*PASSWORD)
        =\S+
    | (?:authorization:\s*(?:bearer|token)\s+\S+)
    | (?:(?:password|passwd|token|secret|api[_-]?key|bearer)
        ["']?\s*[:=]\s*["']?(?P<kvval>[^\s"',]+))
    | (?:Bearer\s+\S+)
    | (?:sk-[A-Za-z0-9_-]+)
    | (?:hmac_[A-Za-z0-9]+)
    | (?:password=\S+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Registry of known secret literals (e.g. loaded Bearer tokens that have no
# recognizable prefix). Populated at runtime via :func:`register_secrets` so
# even opaque, prefix-less tokens echoed back by a server never leak.
_REGISTERED: set[str] = set()


def register_secrets(secrets: Iterable[str]) -> None:
    """Register known secret literals to be masked verbatim by :func:`redact`.

    Args:
        secrets: An iterable of raw secret strings (e.g. loaded MCP Bearer
            tokens). Values shorter than 8 chars are ignored to avoid masking
            innocuous short substrings. Falsy values are skipped.

    Note:
        Never register an already-masked form (``tok[:8]...tok[-4:]``); doing
        so would re-mask diagnostic output. Only raw secrets belong here.
    """
    for s in secrets:
        if s and len(s) >= 8:
            _REGISTERED.add(s)


def _envkey_repl(match: re.Match[str]) -> str:
    """Build the replacement for a single toxic match.

    Keeps the output readable where a variable name is known:
    ``KEY=<REDACTED>`` for env-style assignments, ``<REDACTED>`` otherwise.
    """
    envkey = match.groupdict().get("envkey")
    if envkey:
        return f"{envkey}=<REDACTED>"
    return "<REDACTED>"


def redact(text: str) -> str:
    """Replace any toxic-secret pattern in ``text`` with ``<REDACTED>``.

    Two passes:
        1. Regex masking of well-known secret shapes (GitHub tokens, env-style
           assignments, ``authorization:`` headers, key/value secret forms,
           and the legacy ``Bearer``/``sk-``/``hmac_``/``password=`` shapes).
        2. Literal masking of any secret previously registered via
           :func:`register_secrets`, longest-first to avoid partial overlaps.

    Args:
        text: Arbitrary text that may embed a secret.

    Returns:
        The input with every matched/registered secret replaced by
        ``<REDACTED>`` (or ``KEY=<REDACTED>`` for env-style assignments).
        Non-string input is coerced to ``str`` first so callers can safely
        pass exception objects.
    """
    if not isinstance(text, str):
        text = str(text)
    out = _TOXIC_RE.sub(_envkey_repl, text)
    if _REGISTERED:
        # Longest-first so a longer secret is masked before a shorter one that
        # may be a substring of it. The marker is never itself registered.
        for secret in sorted(_REGISTERED, key=len, reverse=True):
            if secret in out:
                out = out.replace(secret, "<REDACTED>")
    return out


def mask_token(tok: str) -> str:
    """Mask a Bearer token, revealing only a short prefix and suffix.

    Args:
        tok: The raw token. Never logged in full anywhere.

    Returns:
        ``tok[:8] + "..." + tok[-4:]`` when ``len(tok) >= 13``; otherwise
        ``"***"`` (too short to reveal any portion safely).
    """
    if not isinstance(tok, str) or len(tok) < 13:
        return "***"
    return f"{tok[:8]}...{tok[-4:]}"


def mask_hmac(h: str) -> str:
    """Mask an HMAC secret or digest, revealing only a short prefix.

    Args:
        h: The raw HMAC secret or hex digest.

    Returns:
        ``h[:12] + "..."`` when ``len(h) >= 12``; otherwise ``"***"``.
    """
    if not isinstance(h, str) or len(h) < 12:
        return "***"
    return f"{h[:12]}..."
