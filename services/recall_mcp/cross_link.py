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
) -> list[dict[str, Any]]:
    """Expand wiki-link paths into document records with boosted score.

    Args:
        pool: Asyncpg connection pool.
        paths: List of document paths to look up.
        existing_ids: Set of doc IDs already in results (to avoid duplicates).
        adj_boost: Score assigned to adjacent documents.

    Returns:
        List of dicts with path, source_type, score, snippet, scope.
    """
    if not paths:
        return []

    rows = await pool.fetch(
        """
        SELECT id, path, source_type, scope,
               substring(body, 1, 200) AS snippet
        FROM documents
        WHERE path = ANY($1::text[])
        """,
        paths,
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
        })

    return results
