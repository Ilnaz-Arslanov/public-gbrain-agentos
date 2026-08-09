"""Payload rendering contract for swarm-worker inter-agent letters.

Regression guard for the "empty letter" class of bug: a sender that does not
use the historical ``{title, body}`` field names must still deliver readable
content, not a letter whose whole body is blank.
"""
import os

os.environ.setdefault("MCP_PORT", "0")

from services.swarm_mcp.worker import (  # noqa: E402
    _RENDER_BODY_LIMIT,
    _format_virtual_prompt,
    _render_payload_body,
)

# Real payload shape observed from a Hermes-side sender (daisy, 2026-08-09):
# no title, no body — facts/sources only.
FACTS_PAYLOAD = {
    "from": "daisy",
    "kind": "daisy-link-contract-second-pass",
    "facts": ["HMAC header names differ", "Hermes answers 202 before the run finishes"],
    "sources": ["gateway/platforms/webhook.py:584-934"],
}


def test_facts_only_payload_is_not_empty() -> None:
    """A facts/sources payload renders its content instead of an empty body."""
    rendered = _render_payload_body(FACTS_PAYLOAD)
    assert "HMAC header names differ" in rendered
    assert "Hermes answers 202 before the run finishes" in rendered
    assert "gateway/platforms/webhook.py:584-934" in rendered


def test_facts_only_payload_titles_from_kind() -> None:
    """Without an explicit title the letter is titled by ``kind``, not "(no title)"."""
    prompt = _format_virtual_prompt("daisy", "cody", "t-1", FACTS_PAYLOAD)
    assert "Task: daisy-link-contract-second-pass" in prompt
    assert "(no title)" not in prompt
    assert "HMAC header names differ" in prompt


def test_body_leads_and_extra_fields_still_render() -> None:
    """``body`` leads the letter, but extra fields are appended, not dropped.

    Observed live on the 2026-08-09 smoke: a payload carrying ``body`` plus
    ``subject``/``details`` delivered only the body — the same silent-drop class
    of bug this renderer exists to kill.
    """
    rendered = _render_payload_body(
        {"title": "T", "body": "canonical body", "message": "synonym", "details": ["extra"]}
    )
    assert rendered.startswith("canonical body")
    assert "Details:\n- extra" in rendered
    # ``message`` is a synonym of ``body``; it must not be duplicated below it.
    assert "synonym" not in rendered


def test_message_only_payload() -> None:
    """A top-level ``message`` is used when ``body`` is absent."""
    assert _render_payload_body({"message": "  hello  "}) == "hello"


def test_control_fields_are_not_rendered() -> None:
    """Underscore-prefixed control keys and template-consumed keys stay out of the body."""
    rendered = _render_payload_body(
        {
            "kind": "k",
            "from": "daisy",
            "urgency": "high",
            "_origin_task": "task-42",
            "_smoke": True,
            "_escalation_reason": "because",
            "facts": ["visible"],
        }
    )
    assert rendered == "Facts:\n- visible"


def test_empty_payload_renders_empty() -> None:
    """A payload with no content renders an empty body rather than raising."""
    assert _render_payload_body({}) == ""
    assert _render_payload_body({"title": "only a title"}) == ""


def test_oversized_body_is_truncated_visibly() -> None:
    """Oversized content is capped and the cut is announced, never silent."""
    rendered = _render_payload_body({"facts": ["x" * 10_000]})
    assert len(rendered) <= _RENDER_BODY_LIMIT + 40
    assert rendered.endswith("[truncated by swarm-worker]")


def test_nested_structures_degrade_to_json() -> None:
    """Dict/list-of-dict values are serialized instead of being dropped."""
    rendered = _render_payload_body({"ctx": {"run": 7}, "items": [{"a": 1}]})
    assert '"run": 7' in rendered or '"run":7' in rendered
    assert '"a": 1' in rendered or '"a":1' in rendered
