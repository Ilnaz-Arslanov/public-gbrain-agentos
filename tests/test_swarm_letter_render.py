"""Payload rendering contract for swarm-worker inter-agent letters.

Regression guard for the "empty letter" class of bug: a sender that does not
use the historical ``{title, body}`` field names must still deliver readable
content, not a letter whose whole body is blank.
"""
import os

os.environ.setdefault("MCP_PORT", "0")

from services.swarm_mcp.worker import (  # noqa: E402
    _RENDER_BODY_LIMIT_DEFAULT,
    _RENDER_BODY_LIMIT_ENV,
    _format_virtual_prompt,
    _render_payload_body,
    measure_payload_body,
    render_body_limit,
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
    assert len(rendered) <= _RENDER_BODY_LIMIT_DEFAULT + 40
    assert rendered.endswith("[truncated by swarm-worker]")


def test_measure_reports_size_before_truncation() -> None:
    """The sender learns the untruncated size, not the size it was cut to.

    Without this the author of an oversized letter has no signal at all: the
    ``[truncated]`` marker lands in the receiver's copy only.
    """
    rendered, full_length = measure_payload_body({"body": "y" * 10_000})
    assert full_length == 10_000
    assert len(rendered) < full_length
    assert rendered.endswith("[truncated by swarm-worker]")


def test_measure_reports_exact_size_when_it_fits() -> None:
    """A letter under the cap is reported at its real length and left intact."""
    rendered, full_length = measure_payload_body({"body": "short letter"})
    assert rendered == "short letter"
    assert full_length == len("short letter")


def test_limit_is_overridable_by_env(monkeypatch) -> None:
    """Operators can raise or lower the cap without editing the code."""
    monkeypatch.setenv(_RENDER_BODY_LIMIT_ENV, "50")
    assert render_body_limit() == 50
    rendered, full_length = measure_payload_body({"body": "z" * 200})
    assert full_length == 200
    assert rendered.startswith("z" * 50)
    assert rendered.endswith("[truncated by swarm-worker]")


def test_bad_limit_falls_back_to_default(monkeypatch) -> None:
    """A typo in the env var must not cut every letter to nothing."""
    for bad in ("", "   ", "abc", "0", "-100"):
        monkeypatch.setenv(_RENDER_BODY_LIMIT_ENV, bad)
        assert render_body_limit() == _RENDER_BODY_LIMIT_DEFAULT


def test_nested_structures_degrade_to_json() -> None:
    """Dict/list-of-dict values are serialized instead of being dropped."""
    rendered = _render_payload_body({"ctx": {"run": 7}, "items": [{"a": 1}]})
    assert '"run": 7' in rendered or '"run":7' in rendered
    assert '"a": 1' in rendered or '"a":1' in rendered


# ---------------------------------------------------------------------------
# The letter must not ask the receiver to report to a coordinator.
# That instruction produced 21 undeliverable letters to a non-existent
# `coordinator-agent` (cleaned up 2026-08-10) — coordination goes through the
# owner, so the second report had no reader.
# ---------------------------------------------------------------------------


def test_letter_does_not_ask_for_coordinator_copy() -> None:
    prompt = _format_virtual_prompt("cody", "daisy", "t-1", {"title": "x", "body": "y"})
    assert "swarm.notify" not in prompt
    assert "coordinator" not in prompt.lower()


def test_letter_actions_are_task_report_ack() -> None:
    """Exactly three numbered steps remain, ack last."""
    prompt = _format_virtual_prompt("cody", "daisy", "t-1", {"title": "x", "body": "y"})
    assert "1. Execute the task." in prompt
    assert "2. Send the owner a detailed chat report" in prompt
    assert '3. Call swarm.ack(task_id="t-1")' in prompt
    assert "4." not in prompt.split("ACTIONS:")[1]


def test_coordinator_target_still_takes_ack_only_path() -> None:
    """The loop gate itself is untouched — only the instruction was removed."""
    from services.swarm_mcp import worker

    prompt = _format_virtual_prompt("cody", worker.COORDINATOR_AGENT, "t-2", {"title": "x"})
    assert "ack-only fast path" in prompt
    assert "DO NOT swarm.notify back" in prompt
