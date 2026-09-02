"""Wiki-link expansion (1-hop) for recall results."""
import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_RELATED_RE = re.compile(r"^related:\s*(.+)$", re.MULTILINE)

# Characters a vault path never contains. Note bodies quote shell, and
# `[[ -z "$TOKEN" ]]` or `[[:space:]]` match the wiki-link pattern just
# as well as a real link does.
_NOT_IN_A_PATH = set(" \t\"'$=")


def _looks_like_path(link: str) -> bool:
    """Reject wiki-link candidates that cannot be a document path.

    Args:
        link: Raw text captured between the brackets.

    Returns:
        True when the candidate is worth looking up.
    """
    if not link or link.startswith(":"):
        return False
    return not any(ch in link for ch in _NOT_IN_A_PATH)


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
        if path and path not in seen and _looks_like_path(path):
            links.append(path)
            seen.add(path)

    # related: path1, path2 from frontmatter
    for match in _RELATED_RE.finditer(text):
        for part in match.group(1).split(","):
            path = part.strip().strip('"').strip("'")
            if path and path not in seen and _looks_like_path(path):
                links.append(path)
                seen.add(path)

    return links


def link_forms(path: str) -> list[str]:
    """Return the ways a note may be referenced in a wiki-link.

    Writers link both by full vault path and by bare slug, so a
    backlink search that only looks for the full path misses most of
    the links that actually point at the note.

    Args:
        path: Vault-relative document path.

    Returns:
        Path, file name, and file name without the .md suffix.
    """
    forms = [path]
    tail = path.rsplit("/", 1)[-1]
    if tail and tail not in forms:
        forms.append(tail)
    if tail.endswith(".md"):
        stem = tail[:-3]
        if stem and stem not in forms:
            forms.append(stem)
    return forms


async def resolve_link_paths(
    pool: asyncpg.Pool,
    links: list[str],
) -> list[str]:
    """Resolve wiki-link targets onto real document paths.

    A link is written the way a person refers to a note --
    ``[[some-slug]]`` -- while the vault stores it as
    ``30-decisions/some-slug.md``. Matching on the full path alone
    leaves those links dangling: on the production corpus that is why
    157 of 163 links resolved to nothing, and the link graph the
    expansion walks was mostly empty.

    A bare slug therefore also matches the tail of a path, but only
    when exactly one document ends with it. An ambiguous slug resolves
    to nothing: pulling in the wrong neighbour is worse than pulling
    in none.

    Args:
        pool: Asyncpg connection pool.
        links: Raw link targets as written in the note.

    Returns:
        Deduplicated real document paths. No filtering by freshness or
        scope happens here -- that is the caller's job, and skipping it
        would be a way around both.
    """
    if not links:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for link in links:
        candidate = link if link.endswith(".md") else f"{link}.md"
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    rows = await pool.fetch(
        """
        SELECT c.cand AS cand, d.path AS path
        FROM unnest($1::text[]) AS c(cand)
        JOIN documents d
          ON d.path = c.cand
          OR right(d.path, length(c.cand) + 1) = '/' || c.cand
        """,
        candidates,
    )

    matches: dict[str, set[str]] = {}
    for row in rows:
        matches.setdefault(row["cand"], set()).add(row["path"])

    resolved: list[str] = []
    taken: set[str] = set()
    for candidate in candidates:
        found = matches.get(candidate, set())
        if len(found) != 1:
            if len(found) > 1:
                logger.debug(
                    "ambiguous wiki-link %s matches %d documents",
                    candidate, len(found),
                )
            continue
        path = next(iter(found))
        if path not in taken:
            resolved.append(path)
            taken.add(path)

    return resolved


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
        paths: Real document paths, as returned by ``resolve_link_paths``.
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
