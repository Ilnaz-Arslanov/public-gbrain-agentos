"""MCP tools for recall read-side: recall, recent, related, get, stats, reindex_check."""
import asyncio
import hashlib
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
from fastmcp import Context

from .cache import CacheKey, RecallCache
from .cross_link import expand_links, find_wikilinks
from .source_weights import SOURCE_WEIGHTS, temporal_decay

logger = logging.getLogger(__name__)

# Per-request Authorization header captured by ASGI middleware in server.py.
# Workaround for FastMCP stateless HTTP not surfacing request headers to tool
# handlers via ctx.request_context.request in some transport configs.
# Currently read-only tools do not require authentication, but the ContextVar
# is published so a future iteration can enforce token-scoped reads without
# server.py changes.
_REQUEST_AUTH: ContextVar[str | None] = ContextVar("recall_request_auth", default=None)

# RRF constant (standard value from original paper)
_RRF_K = 60

# Snippet budget: max lines across all results
_MAX_SNIPPET_LINES = 8


def _truncate_snippets(
    results: list[dict[str, Any]],
    max_lines: int = _MAX_SNIPPET_LINES,
) -> list[dict[str, Any]]:
    """Truncate snippets so total lines across all results fit the budget.

    Args:
        results: List of result dicts with 'snippet' key.
        max_lines: Maximum total lines allowed.

    Returns:
        Same list with snippets truncated in-place.
    """
    used = 0
    for item in results:
        snippet = item.get("snippet", "")
        lines = snippet.split("\n")
        remaining = max_lines - used
        if remaining <= 0:
            item["snippet"] = "..."
            continue
        if len(lines) > remaining:
            lines = lines[:remaining]
            lines.append("...")
        item["snippet"] = "\n".join(lines)
        used += len(lines)
    return results


def _build_scope_filter(
    scopes: list[str],
    agent_filter: str | None,
    source_types: list[str] | None,
    param_offset: int,
) -> tuple[str, list[Any]]:
    """Build WHERE clause fragments for scope/agent/source_type filtering.

    Args:
        scopes: List of scopes or ["*"] for all.
        agent_filter: Optional agent name filter.
        source_types: Optional list of source_type values.
        param_offset: Starting $N parameter index.

    Returns:
        (sql_fragment, params) tuple.
    """
    clauses: list[str] = []
    params: list[Any] = []
    idx = param_offset

    if scopes != ["*"]:
        clauses.append(f"d.scope = ANY(${idx}::text[])")
        params.append(scopes)
        idx += 1

    if agent_filter is not None:
        clauses.append(f"d.agent = ${idx}")
        params.append(agent_filter)
        idx += 1

    if source_types is not None:
        clauses.append(f"d.source_type = ANY(${idx}::text[])")
        params.append(source_types)
        idx += 1

    sql = ""
    if clauses:
        sql = " AND " + " AND ".join(clauses)
    return sql, params


async def _vector_search(
    pool: asyncpg.Pool,
    embedding: list[float],
    extra_where: str,
    extra_params: list[Any],
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Run pgvector HNSW cosine similarity search.

    Args:
        pool: Asyncpg connection pool.
        embedding: Query embedding vector.
        extra_where: Additional WHERE clause fragments.
        extra_params: Parameters for extra_where.
        limit: Max rows to return.

    Returns:
        List of asyncpg Records.
    """
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    params: list[Any] = [vec_str]
    params.extend(extra_params)

    query = f"""
        SELECT c.id, c.doc_id, c.content, d.path, d.source_type,
               d.scope, d.updated_at,
               1 - (c.embedding <=> $1::vector) AS vec_score
        FROM chunks c
        JOIN documents d ON c.doc_id = d.id
        WHERE d.source_type != 'daily'{extra_where}
        ORDER BY c.embedding <=> $1::vector
        LIMIT {limit}
    """
    return await pool.fetch(query, *params)


async def _fts_search(
    pool: asyncpg.Pool,
    query_text: str,
    extra_where: str,
    extra_params: list[Any],
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Run full-text search using tsvector.

    Args:
        pool: Asyncpg connection pool.
        query_text: Raw search query string.
        extra_where: Additional WHERE clause fragments.
        extra_params: Parameters for extra_where.
        limit: Max rows to return.

    Returns:
        List of asyncpg Records.
    """
    params: list[Any] = [query_text]
    params.extend(extra_params)

    query = f"""
        SELECT c.id, c.doc_id, c.content, d.path, d.source_type,
               d.scope, d.updated_at,
               ts_rank(c.content_tsv, plainto_tsquery('russian', $1)) AS fts_score
        FROM chunks c
        JOIN documents d ON c.doc_id = d.id
        WHERE c.content_tsv @@ plainto_tsquery('russian', $1)
          AND d.source_type != 'daily'{extra_where}
        LIMIT {limit}
    """
    return await pool.fetch(query, *params)


def _rrf_fuse(
    vec_rows: list[asyncpg.Record],
    fts_rows: list[asyncpg.Record],
) -> dict[int, dict[str, Any]]:
    """Fuse vector and FTS results using Reciprocal Rank Fusion.

    Args:
        vec_rows: Results from vector search (ranked by similarity).
        fts_rows: Results from full-text search (ranked by ts_rank).

    Returns:
        Dict mapping chunk_id to merged record with rrf score.
    """
    merged: dict[int, dict[str, Any]] = {}

    for rank, row in enumerate(vec_rows):
        cid = row["id"]
        merged[cid] = {
            "id": cid,
            "doc_id": row["doc_id"],
            "content": row["content"],
            "path": row["path"],
            "source_type": row["source_type"],
            "scope": row["scope"],
            "updated_at": row["updated_at"],
            "rrf": 1.0 / (_RRF_K + rank),
        }

    for rank, row in enumerate(fts_rows):
        cid = row["id"]
        if cid in merged:
            merged[cid]["rrf"] += 1.0 / (_RRF_K + rank)
        else:
            merged[cid] = {
                "id": cid,
                "doc_id": row["doc_id"],
                "content": row["content"],
                "path": row["path"],
                "source_type": row["source_type"],
                "scope": row["scope"],
                "updated_at": row["updated_at"],
                "rrf": 1.0 / (_RRF_K + rank),
            }

    return merged


def register_tools(
    mcp: Any,
    get_pool_fn: Any,
    get_embed_fn: Any,
    get_cache_fn: Any,
    get_vault_root_fn: Any,
) -> None:
    """Register all gbrain MCP tools on the server.

    Args:
        mcp: FastMCP server instance.
        get_pool_fn: Callable returning asyncpg.Pool.
        get_embed_fn: Callable returning TextEmbedding model.
        get_cache_fn: Callable returning RecallCache.
        get_vault_root_fn: Callable returning vault root Path.
    """

    @mcp.tool(annotations={"readOnlyHint": True})
    async def recall(
        query: str,
        limit: int = 5,
        scopes: list[str] | None = None,
        agent_filter: str | None = None,
        source_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search: vector + full-text with RRF fusion, source weights, temporal decay.

        Args:
            query: Search query text.
            limit: Max results to return.
            scopes: Scope filter list, or None / ["*"] for all scopes.
            agent_filter: Optional agent name to filter by.
            source_types: Optional list of source_type values to filter.

        Returns:
            Ranked list of {path, source_type, score, snippet, scope}.
        """
        if scopes is None:
            scopes = ["*"]

        cache = get_cache_fn()
        cache_key: CacheKey = (query, limit, tuple(sorted(scopes)))
        cached = cache.get(cache_key)
        if cached is not None:
            logger.debug("recall cache hit: query=%s", query[:50])
            return cached

        pool = get_pool_fn()
        embed_model = get_embed_fn()

        # Embed query
        embeddings = list(embed_model.embed([query]))
        vec = embeddings[0].tolist()

        # Build filters (param offset 2 because $1 is vec/query)
        extra_where, extra_params = _build_scope_filter(
            scopes, agent_filter, source_types, param_offset=2
        )

        # Parallel vector + FTS search
        vec_rows, fts_rows = await asyncio.gather(
            _vector_search(pool, vec, extra_where, extra_params),
            _fts_search(pool, query, extra_where, extra_params),
        )

        # RRF fusion
        merged = _rrf_fuse(vec_rows, fts_rows)

        # Apply source weight + temporal decay
        now = datetime.now(timezone.utc)
        scored: list[dict[str, Any]] = []
        for item in merged.values():
            sw = SOURCE_WEIGHTS.get(item["source_type"], 1.0)
            if sw == 0.0:
                continue

            updated_at = item["updated_at"]
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            hours_ago = (now - updated_at).total_seconds() / 3600
            td = temporal_decay(hours_ago)

            final_score = item["rrf"] * sw * td
            scored.append({
                "path": item["path"],
                "source_type": item["source_type"],
                "score": round(final_score, 6),
                "snippet": item["content"] or "",
                "scope": item["scope"],
                "_doc_id": item["doc_id"],
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:limit]

        # Cross-link expansion (1-hop)
        existing_ids: set[int] = {r["_doc_id"] for r in results}
        all_links: list[str] = []
        for r in results:
            all_links.extend(find_wikilinks(r["snippet"]))

        if all_links:
            adjacent = await expand_links(pool, all_links, existing_ids)
            results.extend(adjacent)

        # Clean internal fields
        for r in results:
            r.pop("_doc_id", None)

        # Truncate snippets
        results = _truncate_snippets(results)

        # Cache
        cache.put(cache_key, results)

        logger.info(
            "recall: query=%s results=%d (vec=%d fts=%d merged=%d)",
            query[:50], len(results), len(vec_rows), len(fts_rows), len(merged),
        )
        return results

    @mcp.tool(annotations={"readOnlyHint": True})
    async def recent(
        scope: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return last N documents from a given scope, ordered by updated_at.

        Args:
            scope: Scope to filter by.
            limit: Max results to return.

        Returns:
            List of recent documents with path, source_type, agent, timestamps, snippet.
        """
        pool = get_pool_fn()
        rows = await pool.fetch(
            """
            SELECT path, source_type, agent, created_at, updated_at,
                   substring(body, 1, 200) AS snippet
            FROM documents
            WHERE scope = $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            scope,
            limit,
        )
        return [
            {
                "path": r["path"],
                "source_type": r["source_type"],
                "agent": r["agent"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "snippet": r["snippet"] or "",
            }
            for r in rows
        ]

    @mcp.tool(annotations={"readOnlyHint": True})
    async def related(
        path: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Find documents related to a given path via wikilinks and backlinks.

        Args:
            path: Document path to find relations for.
            limit: Max results to return.

        Returns:
            List of related documents sorted by updated_at.
        """
        pool = get_pool_fn()

        # Get source document
        doc = await pool.fetchrow(
            "SELECT id, body, frontmatter FROM documents WHERE path = $1",
            path,
        )
        if doc is None:
            return []

        # Forward links: wikilinks from body + related from frontmatter
        fm = doc["frontmatter"] or {}
        forward_paths = find_wikilinks(doc["body"] or "")
        if isinstance(fm, dict):
            forward_paths.extend(
                p for p in fm.get("related", [])
                if isinstance(p, str) and p not in forward_paths
            )

        # Backlinks: docs that reference this path
        backlinks = await pool.fetch(
            """
            SELECT id, path, source_type, scope, updated_at,
                   substring(body, 1, 200) AS snippet
            FROM documents
            WHERE (body LIKE '%[[' || $1 || ']]%'
                   OR frontmatter::text LIKE '%' || $1 || '%')
              AND id != $2
            ORDER BY updated_at DESC
            """,
            path,
            doc["id"],
        )

        # Forward link lookups
        forward_docs: list[asyncpg.Record] = []
        if forward_paths:
            forward_docs = await pool.fetch(
                """
                SELECT id, path, source_type, scope, updated_at,
                       substring(body, 1, 200) AS snippet
                FROM documents
                WHERE path = ANY($1::text[]) AND id != $2
                """,
                forward_paths,
                doc["id"],
            )

        # Deduplicate and merge
        seen_ids: set[int] = set()
        results: list[dict[str, Any]] = []

        for row in list(forward_docs) + list(backlinks):
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            results.append({
                "path": row["path"],
                "source_type": row["source_type"],
                "scope": row["scope"],
                "updated_at": (
                    row["updated_at"].isoformat() if row["updated_at"] else None
                ),
                "snippet": row["snippet"] or "",
            })

        # Sort by updated_at descending
        results.sort(
            key=lambda x: x["updated_at"] or "",
            reverse=True,
        )
        return results[:limit]

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get(path: str) -> dict[str, Any] | None:
        """Return full document content by path.

        Args:
            path: Document path.

        Returns:
            Document dict or None if not found.
        """
        pool = get_pool_fn()
        row = await pool.fetchrow(
            """
            SELECT path, frontmatter, body, source_type, agent, scope,
                   created_at, updated_at
            FROM documents
            WHERE path = $1
            """,
            path,
        )
        if row is None:
            return None

        return {
            "path": row["path"],
            "frontmatter": row["frontmatter"],
            "body": row["body"],
            "source_type": row["source_type"],
            "agent": row["agent"],
            "scope": row["scope"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def stats() -> dict[str, Any]:
        """Return aggregate counters for the brain database.

        Returns:
            Dict with docs_per_scope, last_update, pending_jobs, total_chunks.
        """
        pool = get_pool_fn()

        scope_rows, last_update_row, pending_row, chunks_row = await asyncio.gather(
            pool.fetch("SELECT scope, count(*) AS cnt FROM documents GROUP BY scope"),
            pool.fetchrow("SELECT max(updated_at) AS last_update FROM documents"),
            pool.fetchrow(
                "SELECT count(*) AS cnt FROM embedding_jobs WHERE status = 'pending'"
            ),
            pool.fetchrow("SELECT count(*) AS cnt FROM chunks"),
        )

        docs_per_scope = {r["scope"]: r["cnt"] for r in scope_rows}
        last_update = last_update_row["last_update"] if last_update_row else None

        return {
            "docs_per_scope": docs_per_scope,
            "last_update": last_update.isoformat() if last_update else None,
            "pending_jobs": pending_row["cnt"] if pending_row else 0,
            "total_chunks": chunks_row["cnt"] if chunks_row else 0,
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def reindex_check() -> list[dict[str, str]]:
        """Find stale documents where DB sha256 doesn't match file on disk.

        Returns:
            List of {path, db_sha256, file_sha256} for mismatched documents.
        """
        pool = get_pool_fn()
        vault_root = get_vault_root_fn()

        rows = await pool.fetch("SELECT path, sha256 FROM documents")
        mismatched: list[dict[str, str]] = []

        for row in rows:
            file_path = vault_root / row["path"]
            if not file_path.is_file():
                mismatched.append({
                    "path": row["path"],
                    "db_sha256": row["sha256"] or "",
                    "file_sha256": "FILE_NOT_FOUND",
                })
                continue

            file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if file_hash != (row["sha256"] or ""):
                mismatched.append({
                    "path": row["path"],
                    "db_sha256": row["sha256"] or "",
                    "file_sha256": file_hash,
                })

        logger.info(
            "reindex_check: %d docs checked, %d mismatched",
            len(rows), len(mismatched),
        )
        return mismatched
