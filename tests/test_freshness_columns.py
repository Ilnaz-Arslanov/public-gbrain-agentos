"""Freshness projection (migration 007): columns, mirror, recall filter.

Covers what the D3 code change actually promises:
  * every note is written with ``valid_from``/``is_latest`` in the row and
    the same two fields in its frontmatter,
  * closing a decision (auto or explicit) moves the columns, not only the
    frontmatter, so recall can filter on them,
  * ``verify_note`` is the single writer of the semantic layer and refuses
    to stamp a fact nobody can re-check,
  * recall hides superseded notes unless asked for them.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.memory_mcp.tools import (
    _REQUEST_AUTH,
    _parse_iso_ts,
    _with_freshness,
    register_tools,
)
from services.shared.auth import AgentContext
from tests.test_create_decision_note_supersession import (
    _make_pool,
    _make_recorder,
    _call_create,
    _row,
)


# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------
class TestParseIsoTs:
    def test_strict_utc_form(self) -> None:
        got = _parse_iso_ts("2026-08-31T10:25:10Z")
        assert got == datetime(2026, 8, 31, 10, 25, 10, tzinfo=timezone.utc)

    def test_bare_date_form_used_by_daily_logs(self) -> None:
        got = _parse_iso_ts("2026-08-31")
        assert got == datetime(2026, 8, 31, tzinfo=timezone.utc)

    def test_garbage_returns_none_so_column_default_applies(self) -> None:
        assert _parse_iso_ts("yesterday") is None
        assert _parse_iso_ts("") is None
        assert _parse_iso_ts(None) is None
        assert _parse_iso_ts(12345) is None

    def test_naive_datetime_is_assumed_utc(self) -> None:
        got = _parse_iso_ts(datetime(2026, 8, 31, 10, 0, 0))
        assert got is not None and got.tzinfo is timezone.utc


class TestWithFreshness:
    def test_valid_from_reuses_created_not_wall_clock(self) -> None:
        fm = _with_freshness({"type": "decision", "created": "2026-06-01T00:00:00Z"})
        assert fm["valid_from"] == "2026-06-01T00:00:00Z"
        assert fm["is_latest"] is True

    def test_daily_note_falls_back_to_date(self) -> None:
        fm = _with_freshness({"type": "daily", "date": "2026-08-31"})
        assert fm["valid_from"] == "2026-08-31"

    def test_never_stamps_the_semantic_layer(self) -> None:
        """Writing a note is not re-verifying it -- verify_note owns those."""
        fm = _with_freshness({"type": "decision", "created": "2026-06-01T00:00:00Z"})
        assert "verified_at" not in fm
        assert "verified_by" not in fm
        assert "basis" not in fm

    def test_existing_values_win(self) -> None:
        fm = _with_freshness({"created": "2026-06-01T00:00:00Z", "is_latest": False})
        assert fm["is_latest"] is False


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------
class TestWritePathMirrorsFreshness:
    @pytest.mark.asyncio
    async def test_new_decision_file_carries_both_fields(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(
            recorder, agent_ctx, "Fresh Note", "alpha beta gamma"
        )
        rel = result.split("created: ", 1)[1].strip()
        written = (tmp_path / rel).read_text(encoding="utf-8")
        assert "is_latest: true" in written
        assert "valid_from:" in written
        # The mirror must agree with the note's own creation stamp.
        created_line = next(
            l for l in written.splitlines() if l.startswith("created:")
        )
        valid_line = next(
            l for l in written.splitlines() if l.startswith("valid_from:")
        )
        assert created_line.split(":", 1)[1] == valid_line.split(":", 1)[1]

    @pytest.mark.asyncio
    async def test_insert_passes_valid_from_to_the_column(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The row must not silently fall back to now() when the file says otherwise."""
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        await _call_create(recorder, agent_ctx, "Column Note", "alpha beta gamma")
        inserts = [
            (q, a) for q, a in pool.fetchrow_calls if "INSERT INTO documents" in q
        ]
        assert inserts, "expected an INSERT via _upsert_document"
        query, args = inserts[-1]
        assert "valid_from" in query
        assert isinstance(args[-1], datetime)


class TestDefaultThresholds:
    @pytest.mark.asyncio
    async def test_identical_text_hints_instead_of_auto_merging(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Shipped defaults: auto off, hint 0.35 (node D measured max 0.568).

        The previous default of 0.85 could never fire on this corpus, so
        the branch that silently rewrote another agent's note was dead
        code pretending to be a feature. With auto off, an identical body
        surfaces as a suggestion and nothing is mutated.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        body = "alpha beta gamma delta epsilon"
        pool = _make_pool()
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "same", body)

        parsed = json.loads(result)
        assert parsed["suggested_supersedes"][0]["path"] == (
            "30-decisions/2026-01-01-old.md"
        )
        assert pool.in_tx_updates == []
        assert pool.audit_supersede_calls == []

    @pytest.mark.asyncio
    async def test_unrelated_note_stays_below_hint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        pool.decision_rows = [
            _row("30-decisions/2026-01-01-old.md", "wholly different content here")
        ]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(
            recorder, agent_ctx, "Brand New Topic", "alpha beta gamma delta"
        )
        assert result.startswith("created:")


class TestAutoSupersedeWritesColumns:
    @pytest.mark.asyncio
    async def test_columns_move_with_the_frontmatter(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("GBRAIN_SUPERSEDE_AUTO", "0.85")
        monkeypatch.setenv("GBRAIN_SUPERSEDE_HINT", "0.70")
        body = "alpha beta gamma delta epsilon"
        pool = _make_pool()
        pool.decision_rows = [
            _row("30-decisions/2026-01-01-old.md", body, title="same")
        ]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        await _call_create(recorder, agent_ctx, "same", body)

        updates = [
            (q, a)
            for q, a in pool.execute_calls
            if "UPDATE documents" in q
        ]
        assert updates, "expected the old decision to be closed"
        query = updates[0][0]
        assert "is_latest = false" in query
        assert "valid_until = now()" in query
        assert "superseded_by = $2" in query
        # Frontmatter mirror is still written in the same statement.
        assert "'{is_latest}'" in query

    @pytest.mark.asyncio
    async def test_superseded_file_records_when_it_was_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("GBRAIN_SUPERSEDE_AUTO", "0.85")
        monkeypatch.setenv("GBRAIN_SUPERSEDE_HINT", "0.70")
        body = "alpha beta gamma delta epsilon"
        old_rel = "30-decisions/2026-01-01-old.md"
        old_abs = tmp_path / old_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text(
            "---\ntype: decision\ncreated: 2026-01-01T00:00:00Z\n---\n\n# Old\n\n"
            + body
            + "\n",
            encoding="utf-8",
        )
        pool = _make_pool()
        pool.decision_rows = [_row(old_rel, body, title="same")]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        await _call_create(recorder, agent_ctx, "same", body)

        rewritten = old_abs.read_text(encoding="utf-8")
        assert "is_latest: false" in rewritten
        assert "valid_until:" in rewritten
        assert "superseded_by:" in rewritten


class TestSupersedeDecisionClosesTheOldOne:
    @pytest.mark.asyncio
    async def test_old_row_and_file_are_closed(self, tmp_path: Path) -> None:
        """Before 007 this tool left the old decision is_latest and recall kept serving it."""
        old_rel = "30-decisions/2026-01-01-old.md"
        old_abs = tmp_path / old_rel
        old_abs.parent.mkdir(parents=True, exist_ok=True)
        old_abs.write_text(
            "---\ntype: decision\ncreated: 2026-01-01T00:00:00Z\nis_latest: true\n"
            "---\n\n# Old\n\nthe old call\n",
            encoding="utf-8",
        )
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            tok = _REQUEST_AUTH.set("Bearer token")
            try:
                result = await recorder.tools["supersede_decision"](
                    old_path=old_rel,
                    new_title="New Call",
                    new_body="the new call",
                    reason="measured again",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(tok)

        assert result.startswith("created:")
        closing = [
            (q, a) for q, a in pool.execute_calls if "UPDATE documents" in q
        ]
        assert closing, "the superseded decision must be closed in the same flow"
        query, args = closing[0]
        assert "is_latest = false" in query
        assert "valid_until = now()" in query
        assert args[0] == old_rel

        rewritten = old_abs.read_text(encoding="utf-8")
        assert "is_latest: false" in rewritten
        assert "valid_until:" in rewritten
        # The old text itself is preserved -- closing is not deleting.
        assert "the old call" in rewritten


class TestVerifyNote:
    def _ctx(self) -> dict[str, Any]:
        return {"headers": {"authorization": "Bearer token"}}

    async def _call(self, recorder, agent_ctx, **kwargs):
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            tok = _REQUEST_AUTH.set("Bearer token")
            try:
                return await recorder.tools["verify_note"](
                    ctx=self._ctx(), **kwargs
                )
            finally:
                _REQUEST_AUTH.reset(tok)

    @pytest.mark.asyncio
    async def test_stamps_row_and_file(self, tmp_path: Path) -> None:
        rel = "30-decisions/2026-01-01-old.md"
        abs_path = tmp_path / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            "---\ntype: decision\ncreated: 2026-01-01T00:00:00Z\n---\n\n# Old\n\nbody\n",
            encoding="utf-8",
        )
        pool = _make_pool()
        pool.fetchrow_results = [{"id": 7, "is_latest": True}]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))

        result = await self._call(
            recorder, agent_ctx, path=rel, basis="psql -c 'select count(*)' -> 204"
        )
        assert result == f"verified: {rel}"

        updates = [
            (q, a) for q, a in pool.execute_calls if "UPDATE documents" in q
        ]
        assert updates
        query, args = updates[0]
        assert "verified_at = now()" in query
        assert args[1] == "coder-agent"
        assert args[2].startswith("psql -c")

        rewritten = abs_path.read_text(encoding="utf-8")
        assert "verified_by: coder-agent" in rewritten
        assert "basis:" in rewritten

    @pytest.mark.asyncio
    async def test_rejects_a_basis_nobody_can_repeat(self, tmp_path: Path) -> None:
        pool = _make_pool()
        pool.fetchrow_results = [{"id": 7, "is_latest": True}]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        with pytest.raises(ValueError, match="basis must say how"):
            await self._call(
                recorder, agent_ctx, path="30-decisions/x.md", basis="ok"
            )
        assert not [q for q, _ in pool.execute_calls if "UPDATE documents" in q]

    @pytest.mark.asyncio
    async def test_refuses_a_superseded_note(self, tmp_path: Path) -> None:
        pool = _make_pool()
        pool.fetchrow_results = [{"id": 7, "is_latest": False}]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        with pytest.raises(ValueError, match="superseded"):
            await self._call(
                recorder,
                agent_ctx,
                path="30-decisions/x.md",
                basis="checked the log at 10:24 UTC",
            )

    @pytest.mark.asyncio
    async def test_missing_document_is_an_error(self, tmp_path: Path) -> None:
        pool = _make_pool()
        pool.fetchrow_results = [None]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        with pytest.raises(ValueError, match="Document not found"):
            await self._call(
                recorder,
                agent_ctx,
                path="30-decisions/missing.md",
                basis="checked the log at 10:24 UTC",
            )

    @pytest.mark.asyncio
    async def test_scope_outside_write_rights_is_refused(self, tmp_path: Path) -> None:
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        with pytest.raises(PermissionError):
            await self._call(
                recorder,
                agent_ctx,
                path="90-inbox/someone-else.md",
                basis="checked the log at 10:24 UTC",
            )


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------
class TestRecallHidesSupersededByDefault:
    """The filter has to live in SQL: a closed note must never be ranked."""

    @staticmethod
    def _stub_auth(monkeypatch) -> None:
        from services.shared.auth import AgentContext as _AC

        async def _fake_resolve(_var, _pool, **_kwargs):
            return _AC(agent="nova", write_scopes=[], read_scopes=["*"])

        monkeypatch.setattr(
            "services.recall_mcp.search.resolve_request_identity", _fake_resolve
        )

    def test_default_query_filters_on_is_latest(self, monkeypatch) -> None:
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH

        self._stub_auth(monkeypatch)
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            recall_fn, _, pool = _capture_recall_tool()
            asyncio.run(recall_fn("anything", limit=5, scopes=["*"]))
        finally:
            _RECALL_AUTH.reset(tok)

        assert pool.calls, "expected the search queries to be issued"
        for query, _params in pool.calls:
            assert "d.is_latest" in query

    def test_include_superseded_drops_the_filter(self, monkeypatch) -> None:
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH

        self._stub_auth(monkeypatch)
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            recall_fn, _, pool = _capture_recall_tool()
            asyncio.run(
                recall_fn(
                    "anything", limit=5, scopes=["*"], include_superseded=True
                )
            )
        finally:
            _RECALL_AUTH.reset(tok)

        assert pool.calls
        for query, _params in pool.calls:
            assert "d.is_latest" not in query

    def test_the_flag_is_part_of_the_cache_key(self, monkeypatch) -> None:
        """Otherwise an audit query would be served the filtered result."""
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH

        self._stub_auth(monkeypatch)
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            recall_fn, cache, _ = _capture_recall_tool()
            asyncio.run(recall_fn("same query", limit=5, scopes=["*"]))
            asyncio.run(
                recall_fn(
                    "same query", limit=5, scopes=["*"], include_superseded=True
                )
            )
        finally:
            _RECALL_AUTH.reset(tok)

        assert len(cache.get_calls) >= 2
        assert cache.get_calls[0] != cache.get_calls[1]
        assert cache.get_calls[0][-1] is False
        assert cache.get_calls[1][-1] is True

    def test_search_selects_the_freshness_columns(self, monkeypatch) -> None:
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH

        self._stub_auth(monkeypatch)
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            recall_fn, _, pool = _capture_recall_tool()
            asyncio.run(recall_fn("anything", limit=5, scopes=["*"]))
        finally:
            _RECALL_AUTH.reset(tok)

        for query, _params in pool.calls:
            assert "d.valid_from" in query
            assert "d.verified_at" in query


class TestScopeFilterFragment:
    def test_freshness_gate_is_on_by_default(self) -> None:
        from services.recall_mcp.search import _build_scope_filter

        sql, params = _build_scope_filter(["*"], None, None, param_offset=2)
        assert "d.is_latest" in sql
        assert params == []

    def test_gate_lifts_when_asked(self) -> None:
        from services.recall_mcp.search import _build_scope_filter

        sql, _ = _build_scope_filter(
            ["*"], None, None, param_offset=2, include_superseded=True
        )
        assert "d.is_latest" not in sql

    def test_param_numbering_is_unaffected_by_the_gate(self) -> None:
        """The gate takes no bind parameter, so $N indexes must not shift."""
        from services.recall_mcp.search import _build_scope_filter

        sql, params = _build_scope_filter(
            ["30-decisions"], "cody", ["decision"], param_offset=2
        )
        assert "$2::text[]" in sql and "$3" in sql and "$4::text[]" in sql
        assert params == [["30-decisions"], "cody", ["decision"]]


class TestDuplicateCandidatesTool:
    def test_registered_in_the_default_surface(self) -> None:
        """Gated out of core it would exist for nobody -- every deployment runs core."""
        from services.shared.tool_gating import should_register_tool

        assert should_register_tool("recall_mcp", "duplicate_candidates", "core")
        assert should_register_tool("recall_mcp", "duplicate_candidates", "all")

    def test_centers_vectors_and_excludes_closed_notes(self, monkeypatch) -> None:
        """Raw cosine puts 16104 of 16110 pairs above 0.80 -- centering is the point."""
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH
        from services.shared.auth import AgentContext as _AC

        async def _fake_resolve(_var, _pool, **_kwargs):
            return _AC(agent="nova", write_scopes=[], read_scopes=["*"])

        monkeypatch.setattr(
            "services.recall_mcp.search.resolve_request_identity", _fake_resolve
        )
        from services.recall_mcp.search import register_tools as _register

        class _Rec:
            def __init__(self) -> None:
                self.tools: dict[str, Any] = {}

            def tool(self, **kwargs: Any):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        class _Pool:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[Any, ...]]] = []

            async def fetch(self, query: str, *params: Any) -> list[Any]:
                self.calls.append((query, params))
                return []

        rec, pool = _Rec(), _Pool()
        _register(
            rec, lambda: pool, lambda: None, lambda: None, lambda: None,
            tool_set="core",
        )
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            asyncio.run(rec.tools["duplicate_candidates"](threshold=0.6, limit=5))
        finally:
            _RECALL_AUTH.reset(tok)

        assert pool.calls
        query, params = pool.calls[0]
        assert "avg(c.embedding)" in query          # document centroid
        assert "(v - (SELECT m FROM gm))" in query  # corpus mean subtracted
        assert "d.is_latest" in query               # closed notes already dealt with
        assert params[0] == 0.6 and params[1] == 5

    def test_threshold_and_limit_are_clamped(self, monkeypatch) -> None:
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH
        from services.recall_mcp.search import register_tools as _register
        from services.shared.auth import AgentContext as _AC

        async def _fake_resolve(_var, _pool, **_kwargs):
            return _AC(agent="nova", write_scopes=[], read_scopes=["*"])

        monkeypatch.setattr(
            "services.recall_mcp.search.resolve_request_identity", _fake_resolve
        )

        class _Rec:
            def __init__(self) -> None:
                self.tools: dict[str, Any] = {}

            def tool(self, **kwargs: Any):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        class _Pool:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[Any, ...]]] = []

            async def fetch(self, query: str, *params: Any) -> list[Any]:
                self.calls.append((query, params))
                return []

        rec, pool = _Rec(), _Pool()
        _register(
            rec, lambda: pool, lambda: None, lambda: None, lambda: None,
            tool_set="core",
        )
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            asyncio.run(rec.tools["duplicate_candidates"](threshold=9.0, limit=10_000))
        finally:
            _RECALL_AUTH.reset(tok)

        _query, params = pool.calls[0]
        assert params[0] == 1.0
        assert params[1] == 200


# ---------------------------------------------------------------------------
# Link expansion: the neighbour obeys the same filters as the hit
# ---------------------------------------------------------------------------
class _LinkPool:
    """Records the query and params expand_links issues."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return self._rows


def _link_row(path: str, scope: str = "30-decisions") -> dict[str, Any]:
    return {
        "id": 7,
        "path": path,
        "source_type": "decision",
        "scope": scope,
        "valid_from": datetime(2026, 8, 30, tzinfo=timezone.utc),
        "verified_at": None,
        "snippet": "body",
    }


class TestExpandLinksHonorsFreshnessAndScope:
    """A wiki-link must not be a way around the recall filters."""

    def test_closed_neighbour_is_filtered_by_default(self) -> None:
        from services.recall_mcp.cross_link import expand_links

        pool = _LinkPool()
        asyncio.run(expand_links(pool, ["a.md"], set()))

        query, _params = pool.calls[0]
        assert "is_latest" in query

    def test_include_superseded_drops_the_filter(self) -> None:
        from services.recall_mcp.cross_link import expand_links

        pool = _LinkPool()
        asyncio.run(
            expand_links(pool, ["a.md"], set(), include_superseded=True)
        )

        query, _params = pool.calls[0]
        assert "is_latest" not in query

    def test_scope_of_the_caller_is_enforced(self) -> None:
        from services.recall_mcp.cross_link import expand_links

        pool = _LinkPool()
        asyncio.run(expand_links(pool, ["a.md"], set(), scopes=["50-ops"]))

        query, params = pool.calls[0]
        assert "scope = ANY($2::text[])" in query
        assert params[1] == ["50-ops"]

    def test_wildcard_scope_adds_no_filter(self) -> None:
        from services.recall_mcp.cross_link import expand_links

        pool = _LinkPool()
        asyncio.run(expand_links(pool, ["a.md"], set(), scopes=["*"]))

        query, params = pool.calls[0]
        assert "scope = ANY" not in query
        assert len(params) == 1

    def test_neighbour_carries_the_freshness_columns(self) -> None:
        """Same shape as a search hit -- an adjacent row is a result too."""
        from services.recall_mcp.cross_link import expand_links

        pool = _LinkPool([_link_row("a.md")])
        got = asyncio.run(expand_links(pool, ["a.md"], set()))

        assert got and "valid_from" in got[0] and "verified_at" in got[0]


class TestRecallPassesFiltersIntoExpansion:
    """The filters recall applies to hits must reach the expansion too."""

    @staticmethod
    def _stub_auth(monkeypatch) -> None:
        async def _fake_resolve(_var, _pool, **_kwargs):
            return AgentContext(
                agent="nova", write_scopes=[], read_scopes=["50-ops"]
            )

        monkeypatch.setattr(
            "services.recall_mcp.search.resolve_request_identity", _fake_resolve
        )

    @staticmethod
    def _hit() -> dict[str, Any]:
        return {
            "id": 1,
            "doc_id": 11,
            "content": "a note pointing at [[30-decisions/closed.md]]",
            "path": "50-ops/nova/30-decisions/open.md",
            "source_type": "decision",
            "scope": "50-ops",
            "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "valid_from": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "verified_at": None,
            "vec_score": 0.9,
        }

    def _run(self, monkeypatch, **kwargs: Any) -> dict[str, Any]:
        from tests.test_recall_mcp import _capture_recall_tool
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH

        self._stub_auth(monkeypatch)
        seen: dict[str, Any] = {}

        async def _fake_vector(_pool, _vec, _where, _params, limit=50):
            return [self._hit()]

        async def _fake_fts(_pool, _query, _where, _params, limit=50):
            return []

        async def _spy(_pool, paths, _ids, **kw):
            seen["paths"] = paths
            seen.update(kw)
            return []

        monkeypatch.setattr("services.recall_mcp.search._vector_search", _fake_vector)
        monkeypatch.setattr("services.recall_mcp.search._fts_search", _fake_fts)
        monkeypatch.setattr("services.recall_mcp.search.expand_links", _spy)

        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            recall_fn, _, _pool = _capture_recall_tool()
            asyncio.run(recall_fn("anything", limit=5, **kwargs))
        finally:
            _RECALL_AUTH.reset(tok)
        return seen

    def test_readable_scopes_and_default_freshness_reach_expansion(
        self, monkeypatch
    ) -> None:
        seen = self._run(monkeypatch, scopes=["50-ops"])

        assert seen["paths"] == ["30-decisions/closed.md"]
        assert seen["scopes"] == ["50-ops"]
        assert seen["include_superseded"] is False

    def test_audit_flag_is_carried_through(self, monkeypatch) -> None:
        seen = self._run(monkeypatch, scopes=["50-ops"], include_superseded=True)

        assert seen["include_superseded"] is True


class TestRelatedHidesClosedNotes:
    """``related`` promised the same freshness contract as ``recall``."""

    class _Pool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        async def fetchrow(self, _query: str, *_params: Any) -> dict[str, Any]:
            return {
                "id": 1,
                "body": "links to [[b.md]]",
                "frontmatter": {},
                "scope": "30-decisions",
            }

        async def fetch(self, query: str, *params: Any) -> list[Any]:
            self.calls.append((query, params))
            return []

    def _related(self, monkeypatch, **kwargs: Any):
        from services.recall_mcp.search import _REQUEST_AUTH as _RECALL_AUTH
        from services.recall_mcp.search import register_tools as _register

        async def _fake_resolve(_var, _pool, **_kwargs):
            return AgentContext(agent="nova", write_scopes=[], read_scopes=["*"])

        monkeypatch.setattr(
            "services.recall_mcp.search.resolve_request_identity", _fake_resolve
        )

        class _Rec:
            def __init__(self) -> None:
                self.tools: dict[str, Any] = {}

            def tool(self, **_kwargs: Any):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        rec, pool = _Rec(), self._Pool()
        _register(
            rec, lambda: pool, lambda: None, lambda: None, lambda: None,
            tool_set="core",
        )
        tok = _RECALL_AUTH.set("Bearer test-token")
        try:
            asyncio.run(rec.tools["related"](path="a.md", **kwargs))
        finally:
            _RECALL_AUTH.reset(tok)
        return pool

    def test_backlinks_and_forward_links_filter_by_default(self, monkeypatch) -> None:
        pool = self._related(monkeypatch)

        assert len(pool.calls) == 2  # backlinks + forward links
        for query, _params in pool.calls:
            assert "is_latest" in query

    def test_include_superseded_drops_the_filter(self, monkeypatch) -> None:
        pool = self._related(monkeypatch, include_superseded=True)

        assert pool.calls
        for query, _params in pool.calls:
            assert "is_latest" not in query
