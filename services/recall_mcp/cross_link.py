"""Wiki-link expansion (1-hop) for recall results."""
import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_RELATED_RE = re.compile(r"^related:\s*(.+)$", re.MULTILINE)


def find_wikilinks(text: str) -> list[str]:
    """Extract wiki-link targets and related: frontmatter paths from text.

    Args:
        text: Document body (may include frontmatter).

    Returns:
        Deduplicated list of referenced paths.
    """
    links: list[str] = []
    seen: set[str] = set()

    # [[path]] wiki-links
    for match in _WIKILINK_RE.finditer(text):
        path = match.group(1).strip()
        if path and path not in seen:
            links.append(path)
            seen.add(path)

    # related: path1, path2 from frontmatter
    for match in _RELATED_RE.finditer(text):
        for part in match.group(1).split(","):
            path = part.strip().strip('"').strip("'")
            if path and path not in seen:
                links.append(path)
                seen.add(path)

    return links


async def expand_links(
    pool: asyncpg.Pool,
    paths: list[str],
    existing_ids: set[int],
    adj_boost: float = 0.5,
    scopes: list[str] | None = None,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Expand wiki-link paths into document records with boosted score.

    The neighbour pulled in by a link obeys the same two rules as the
    hit that named it: a closed note stays closed, and a scope the
    caller may not read stays unread. Without them a link in a readable
    note is a way around both filters.

    Args:
        pool: Asyncpg connection pool.
        paths: List of document paths to look up.
        existing_ids: Set of doc IDs already in results (to avoid duplicates).
        adj_boost: Score assigned to adjacent documents.
        scopes: Scopes the caller may read, or None / ["*"] for all.
        include_superseded: When False (the default) closed notes are
            not pulled in as neighbours.

    Returns:
        List of dicts with path, source_type, score, snippet, scope,
        valid_from, verified_at.
    """
    if not paths:
        return []

    clauses = ["path = ANY($1::text[])"]
    params: list[Any] = [paths]
    if not include_superseded:
        clauses.append("is_latest")
    if scopes is not None and scopes != ["*"]:
        clauses.append(f"scope = ANY(${len(params) + 1}::text[])")
        params.append(scopes)

    rows = await pool.fetch(
        f"""
        SELECT id, path, source_type, scope, valid_from, verified_at,
               substring(body, 1, 200) AS snippet
        FROM documents
        WHERE {" AND ".join(clauses)}
        """,
        *params,
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] in existing_ids:
            continue
        results.append({
            "path": row["path"],
            "source_type": row["source_type"],
            "score": adj_boost,
            "snippet": row["snippet"] or "",
            "scope": row["scope"],
            "valid_from": row["valid_from"],
            "verified_at": row["verified_at"],
        })

    return results
