"""Unit tests for memory-mcp helpers + shared auth/audit and adjacent modules."""
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ingest_worker.chunker import chunk_text
from services.memory_mcp.path_guard import ALLOWED_SCOPES, validate_path
from services.memory_mcp.tools import (
    _build_frontmatter,
    _extract_frontmatter_block,
    _extract_token,
    _parse_frontmatter,
    _sha256,
    _slugify,
)
from services.recall_mcp.cache import RecallCache
from services.recall_mcp.source_weights import temporal_decay
from services.shared.audit import log_audit
from services.shared.auth import AgentContext, authenticate, check_write_scope


# -----------------------------------------------------------------------
# PathGuard
# -----------------------------------------------------------------------
class TestPathGuard:
    """Tests for services.memory_mcp.path_guard.validate_path."""

    def test_valid_path(self, tmp_path: Path) -> None:
        result = validate_path("30-decisions/my-note.md", str(tmp_path))
        assert result == (tmp_path / "30-decisions" / "my-note.md").resolve()

    def test_all_scopes_accepted(self, tmp_path: Path) -> None:
        for scope in ALLOWED_SCOPES:
            result = validate_path(f"{scope}/test.md", str(tmp_path))
            assert str(result).startswith(str(tmp_path.resolve()))

    def test_scope_count(self) -> None:
        assert len(ALLOWED_SCOPES) == 13

    def test_empty_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            validate_path("", str(tmp_path))

    def test_traversal_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_path("30-decisions/../etc/passwd", str(tmp_path))

    def test_tilde_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Home expansion"):
            validate_path("30-decisions/~root", str(tmp_path))

    def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Absolute paths"):
            validate_path("/etc/passwd", str(tmp_path))

    def test_unknown_scope_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown scope"):
            validate_path("99-secret/note.md", str(tmp_path))

    def test_nested_path(self, tmp_path: Path) -> None:
        result = validate_path("50-external/twitter/2026-01-01-post.md", str(tmp_path))
        expected = (tmp_path / "50-external" / "twitter" / "2026-01-01-post.md").resolve()
        assert result == expected

    def test_scope_only_no_file(self, tmp_path: Path) -> None:
        result = validate_path("30-decisions", str(tmp_path))
        assert result == (tmp_path / "30-decisions").resolve()


# -----------------------------------------------------------------------
# Slugify
# -----------------------------------------------------------------------
class TestSlugify:
    """Tests for _slugify helper."""

    def test_basic(self) -> None:
        assert _slugify("My Great Decision") == "my-great-decision"

    def test_special_chars(self) -> None:
        assert _slugify("Fix: #123 -- urgent!") == "fix-123-urgent"

    def test_unicode(self) -> None:
        slug = _slugify("Deploy strategy")
        assert slug == "deploy-strategy"

    def test_max_length(self) -> None:
        long_title = "a" * 200
        assert len(_slugify(long_title)) <= 60

    def test_leading_trailing_dashes_stripped(self) -> None:
        assert _slugify("---hello---") == "hello"

    def test_empty_string(self) -> None:
        assert _slugify("") == ""

    def test_only_special_chars(self) -> None:
        assert _slugify("!@#$%^&*()") == ""


# -----------------------------------------------------------------------
# SHA256
# -----------------------------------------------------------------------
class TestSha256:
    """Tests for _sha256 helper."""

    def test_known_hash(self) -> None:
        expected = hashlib.sha256(b"hello").hexdigest()
        assert _sha256("hello") == expected

    def test_empty_string(self) -> None:
        expected = hashlib.sha256(b"").hexdigest()
        assert _sha256("") == expected

    def test_unicode_content(self) -> None:
        content = "Cyrillic text"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert _sha256(content) == expected

    def test_deterministic(self) -> None:
        assert _sha256("test") == _sha256("test")


# -----------------------------------------------------------------------
# Frontmatter
# -----------------------------------------------------------------------
class TestFrontmatter:
    """Tests for _build_frontmatter and _parse_frontmatter."""

    def test_build_roundtrip(self) -> None:
        fields = {"type": "decision", "agent": "coder-agent", "tags": ["deploy"]}
        text = _build_frontmatter(fields)
        assert text.startswith("---\n")
        assert text.endswith("---\n")
        parsed = _parse_frontmatter(text)
        assert parsed is not None
        assert parsed["type"] == "decision"
        assert parsed["agent"] == "coder-agent"
        assert parsed["tags"] == ["deploy"]

    def test_build_empty_dict(self) -> None:
        text = _build_frontmatter({})
        assert text.startswith("---\n")
        assert text.endswith("---\n")

    def test_parse_no_frontmatter(self) -> None:
        assert _parse_frontmatter("# Just a heading") is None

    def test_parse_incomplete_delimiters(self) -> None:
        assert _parse_frontmatter("---\nkey: value\n") is None

    def test_parse_invalid_yaml(self) -> None:
        assert _parse_frontmatter("---\n: : :\n---\n") is None

    def test_parse_empty_frontmatter(self) -> None:
        result = _parse_frontmatter("---\n\n---\nbody")
        assert result is None  # yaml.safe_load("") returns None

    def test_build_unicode(self) -> None:
        text = _build_frontmatter({"title": "Test"})
        assert "title: Test" in text


class TestExtractFrontmatterBlock:
    """Tests for _extract_frontmatter_block."""

    def test_valid_block(self) -> None:
        text = "---\nkey: value\n---\n# Body"
        result = _extract_frontmatter_block(text)
        assert result == "---\nkey: value\n---\n"

    def test_no_frontmatter(self) -> None:
        assert _extract_frontmatter_block("# No frontmatter") is None

    def test_unclosed_frontmatter(self) -> None:
        assert _extract_frontmatter_block("---\nkey: value\nno closing") is None

    def test_preserves_content(self) -> None:
        text = "---\na: 1\nb: 2\n---\nbody text"
        block = _extract_frontmatter_block(text)
        assert block is not None
        assert "a: 1" in block
        assert "body text" not in block


# -----------------------------------------------------------------------
# ExtractToken
# -----------------------------------------------------------------------
class TestExtractToken:
    """Tests for _extract_token (ContextVar primary, ctx fallback, no env)."""

    @pytest.mark.asyncio
    async def test_valid_token(self) -> None:
        ctx = {"headers": {"authorization": "Bearer my-secret-token"}}
        token = await _extract_token(ctx)
        assert token == "my-secret-token"

    @pytest.mark.asyncio
    async def test_capitalized_header(self) -> None:
        ctx = {"headers": {"Authorization": "Bearer capitalized-token"}}
        token = await _extract_token(ctx)
        assert token == "capitalized-token"

    @pytest.mark.asyncio
    async def test_missing_header_raises(self) -> None:
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token({"headers": {}})

    @pytest.mark.asyncio
    async def test_no_headers_key_raises(self) -> None:
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token({})

    @pytest.mark.asyncio
    async def test_basic_auth_rejected(self) -> None:
        ctx = {"headers": {"authorization": "Basic dXNlcjpwYXNz"}}
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token(ctx)

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self) -> None:
        ctx = {"headers": {"authorization": "Token abc"}}
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token(ctx)


# -----------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------
class TestAuth:
    """Tests for authenticate and check_write_scope."""

    @pytest.mark.asyncio
    async def test_authenticate_valid_token(self) -> None:
        pool = MagicMock()
        token = "valid-token-123"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        pool.fetchrow = AsyncMock(return_value={
            "agent": "coder-agent",
            "can_write_scopes": ["30-decisions", "70-runbooks"],
            "can_read_scopes": ["*"],
        })

        ctx = await authenticate(token, pool)
        assert ctx.agent == "coder-agent"
        assert "30-decisions" in ctx.write_scopes
        assert "*" in ctx.read_scopes

        call_args = pool.fetchrow.call_args
        assert call_args[0][1] == token_hash

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self) -> None:
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)

        with pytest.raises(PermissionError, match="Invalid or unknown"):
            await authenticate("bad-token", pool)

    @pytest.mark.asyncio
    async def test_authenticate_null_scopes(self) -> None:
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={
            "agent": "agent-1",
            "can_write_scopes": None,
            "can_read_scopes": None,
        })

        ctx = await authenticate("token", pool)
        assert ctx.write_scopes == []
        assert ctx.read_scopes == []

    def test_check_write_scope_wildcard(self) -> None:
        ctx = AgentContext(agent="admin", write_scopes=["*"], read_scopes=[])
        assert check_write_scope(ctx, "30-decisions") is True
        assert check_write_scope(ctx, "anything") is True

    def test_check_write_scope_specific(self) -> None:
        ctx = AgentContext(
            agent="limited",
            write_scopes=["30-decisions"],
            read_scopes=[],
        )
        assert check_write_scope(ctx, "30-decisions") is True
        assert check_write_scope(ctx, "70-runbooks") is False

    def test_check_write_scope_empty(self) -> None:
        ctx = AgentContext(agent="readonly", write_scopes=[], read_scopes=["*"])
        assert check_write_scope(ctx, "30-decisions") is False


# -----------------------------------------------------------------------
# Audit
# -----------------------------------------------------------------------
class TestAudit:
    """Tests for log_audit -- must never raise."""

    @pytest.mark.asyncio
    async def test_successful_log(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()

        await log_audit(
            pool, "coder-agent", "create_decision_note",
            {"title": "test"}, "ok", 42,
        )

        pool.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_db_exception(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=RuntimeError("DB down"))

        await log_audit(
            pool, "coder-agent", "test_tool",
            {"key": "val"}, "error", 100, error="some error",
        )

    @pytest.mark.asyncio
    async def test_swallows_any_exception(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=TypeError("bad type"))

        await log_audit(pool, "agent", "tool", {}, "ok", 0)

    @pytest.mark.asyncio
    async def test_with_error_field(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()

        await log_audit(
            pool, "coder-agent", "failing_tool",
            {"path": "/x"}, "error", 500, error="Something broke",
        )

        call_args = pool.execute.call_args[0]
        assert call_args[6] == "Something broke"


# -----------------------------------------------------------------------
# Chunker
# -----------------------------------------------------------------------
class TestChunker:
    """Tests for chunk_text sliding window."""

    def test_empty_text(self) -> None:
        assert chunk_text("") == []

    def test_whitespace_only(self) -> None:
        assert chunk_text("   \n\t  ") == []

    def test_short_text_single_chunk(self) -> None:
        text = "one two three four five"
        result = chunk_text(text, window_size=10, overlap=2)
        assert len(result) == 1
        assert result[0] == "one two three four five"

    def test_exact_window_size(self) -> None:
        words = ["w"] * 10
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=2)
        assert len(result) == 1

    def test_overlap_works(self) -> None:
        words = [f"w{i}" for i in range(15)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=3)
        assert len(result) >= 2
        chunk0_words = result[0].split()
        chunk1_words = result[1].split()
        assert chunk0_words[-3:] == chunk1_words[:3]

    def test_no_overlap(self) -> None:
        words = [f"w{i}" for i in range(20)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=0)
        assert len(result) == 2
        assert len(result[0].split()) == 10
        assert len(result[1].split()) == 10

    def test_default_params(self) -> None:
        words = ["word"] * 1000
        text = " ".join(words)
        result = chunk_text(text)
        assert len(result) >= 2
        assert len(result[0].split()) == 500

    def test_single_word(self) -> None:
        assert chunk_text("hello") == ["hello"]

    def test_covers_all_content(self) -> None:
        words = [f"w{i}" for i in range(25)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=2)
        all_chunked_words: set[str] = set()
        for c in result:
            all_chunked_words.update(c.split())
        assert all_chunked_words == set(words)


# -----------------------------------------------------------------------
# TemporalDecay
# -----------------------------------------------------------------------
class TestTemporalDecay:
    """Tests for temporal_decay multiplier buckets."""

    def test_fresh_under_24h(self) -> None:
        assert temporal_decay(0) == 1.5
        assert temporal_decay(12) == 1.5
        assert temporal_decay(23.9) == 1.5

    def test_boundary_24h(self) -> None:
        assert temporal_decay(24) == 1.2

    def test_one_week(self) -> None:
        assert temporal_decay(100) == 1.2
        assert temporal_decay(167.9) == 1.2

    def test_boundary_7_days(self) -> None:
        assert temporal_decay(168) == 1.0

    def test_one_month(self) -> None:
        assert temporal_decay(500) == 1.0
        assert temporal_decay(719.9) == 1.0

    def test_boundary_30_days(self) -> None:
        assert temporal_decay(720) == 0.9

    def test_old_document(self) -> None:
        assert temporal_decay(10000) == 0.9

    def test_negative_hours(self) -> None:
        assert temporal_decay(-1) == 1.5


# -----------------------------------------------------------------------
# RecallCache
# -----------------------------------------------------------------------
class TestRecallCache:
    """Tests for RecallCache LRU with TTL."""

    def test_put_and_get(self) -> None:
        cache = RecallCache(ttl_sec=60, max_entries=10)
        key = ("query", 5, ("scope-a",))
        value = [{"id": 1, "text": "result"}]
        cache.put(key, value)
        assert cache.get(key) == value

    def test_miss_returns_none(self) -> None:
        cache = RecallCache()
        assert cache.get(("unknown", 5, ())) is None

    @patch("services.recall_mcp.cache.time")
    def test_ttl_expiry(self, mock_time: MagicMock) -> None:
        mock_time.monotonic.return_value = 1000.0
        cache = RecallCache(ttl_sec=30, max_entries=10)

        key = ("q", 5, ())
        cache.put(key, [{"x": 1}])

        mock_time.monotonic.return_value = 1029.0
        assert cache.get(key) is not None

        mock_time.monotonic.return_value = 1031.0
        assert cache.get(key) is None

    def test_lru_eviction(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=3)

        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])
        cache.put(("c", 1, ()), [{"c": 1}])

        cache.put(("d", 1, ()), [{"d": 1}])

        assert cache.get(("a", 1, ())) is None
        assert cache.get(("b", 1, ())) is not None
        assert cache.get(("d", 1, ())) is not None

    def test_get_promotes_lru(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=3)

        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])
        cache.put(("c", 1, ()), [{"c": 1}])

        cache.get(("a", 1, ()))

        cache.put(("d", 1, ()), [{"d": 1}])

        assert cache.get(("a", 1, ())) is not None
        assert cache.get(("b", 1, ())) is None

    def test_invalidate_all(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=10)
        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])

        cache.invalidate_all()

        assert cache.get(("a", 1, ())) is None
        assert cache.get(("b", 1, ())) is None

    def test_overwrite_existing_key(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=10)
        key = ("q", 5, ())

        cache.put(key, [{"old": True}])
        cache.put(key, [{"new": True}])

        result = cache.get(key)
        assert result is not None
        assert result[0]["new"] is True

    @patch("services.recall_mcp.cache.time")
    def test_expired_entry_removed_on_get(self, mock_time: MagicMock) -> None:
        mock_time.monotonic.return_value = 0.0
        cache = RecallCache(ttl_sec=10, max_entries=10)

        key = ("q", 1, ())
        cache.put(key, [{"x": 1}])

        mock_time.monotonic.return_value = 100.0
        assert cache.get(key) is None

        assert key not in cache._store
