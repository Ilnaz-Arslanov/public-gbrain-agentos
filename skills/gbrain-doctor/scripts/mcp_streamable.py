"""Minimal stdlib JSON-RPC client for gbrain MCP streamable-http endpoints.

Verified pattern (mirrors ``scripts/task-board-gbrain.sh::_call_mcp``):

- Single JSON-RPC POST to the ``/<service>/mcp`` URL. NO ``initialize``
  handshake — the gbrain servers run ``FASTMCP_STATELESS_HTTP=1``, so a lone
  ``tools/list`` or ``tools/call`` request works.
- Headers: ``Content-Type: application/json``,
  ``Accept: application/json, text/event-stream``,
  ``Authorization: Bearer <token>``.
- Body: ``{"jsonrpc":"2.0","id":1,"method":<method>,"params":<params>}``.
- Response may be plain JSON OR Server-Sent Events. We scan for the first
  ``data: <json>`` frame; otherwise parse the whole body as JSON.
- ``result.isError`` truthy -> treated as a failed call; the first
  ``content[].text`` is surfaced as the (redacted) error message.
- On success we prefer ``result.structuredContent``; else we parse / collect
  the ``result.content[].text`` payloads.

Security: the token is never logged. Every exception message is passed
through :func:`redact.redact` so a token echoed in an error body never leaks.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import redact

logger = logging.getLogger("gbrain_doctor.mcp")


class McpError(Exception):
    """An MCP call failed at the HTTP, URL, protocol, or tool level.

    Attributes:
        message: Redacted, human-readable error detail.
        status_code: HTTP status when the failure was an HTTP error, else
            ``None`` (URL/socket/protocol/tool-level failures).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        # The message is redacted defensively so even a caller passing a raw
        # server error body cannot leak a token through ``str(McpError)``.
        safe = redact.redact(message)
        super().__init__(safe)
        self.message: str = safe
        self.status_code: int | None = status_code


def _parse_body(body: str) -> dict[str, Any]:
    """Parse an MCP HTTP body that may be plain JSON or SSE.

    Args:
        body: The decoded response body text.

    Returns:
        The decoded JSON-RPC envelope as a dict.

    Raises:
        McpError: When no parseable JSON / ``data:`` frame is found.
    """
    # SSE path: find the first ``data: <json>`` frame.
    for line in body.splitlines():
        if line.startswith("data: "):
            frame = line[6:].strip()
            if not frame:
                continue
            try:
                return json.loads(frame)
            except json.JSONDecodeError as exc:
                raise McpError(f"malformed SSE data frame: {exc}") from exc

    # Plain JSON path.
    text = body.strip()
    if not text:
        raise McpError("empty response body")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"non-JSON, non-SSE response: {exc}") from exc


def _extract_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-RPC envelope and return its ``result`` object.

    Args:
        envelope: The decoded JSON-RPC response.

    Returns:
        The ``result`` object (a dict).

    Raises:
        McpError: On JSON-RPC ``error``, ``result.isError``, or a missing /
            malformed ``result``.
    """
    if "error" in envelope and envelope["error"]:
        err = envelope["error"]
        if isinstance(err, dict):
            detail = err.get("message") or json.dumps(err, default=str)
        else:
            detail = str(err)
        raise McpError(f"JSON-RPC error: {detail}")

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise McpError("response missing 'result' object")

    if result.get("isError"):
        content = result.get("content") or []
        msg = "unknown tool error"
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                msg = first.get("text") or msg
        raise McpError(f"tool error: {msg}")

    return result


def mcp_call(
    server_url: str,
    token: str,
    method: str,
    params: dict,
    timeout: float = 15.0,
) -> tuple[dict, float]:
    """Issue a single stateless JSON-RPC call to an MCP streamable-http URL.

    Args:
        server_url: The full ``/<service>/mcp`` endpoint URL.
        token: Raw Bearer token (never logged).
        method: JSON-RPC method, e.g. ``"tools/list"`` or ``"tools/call"``.
        params: JSON-RPC params object.
        timeout: Socket timeout in seconds.

    Returns:
        A tuple of ``(result_obj, latency_ms)`` where ``result_obj`` is the
        validated JSON-RPC ``result`` dict.

    Raises:
        McpError: On HTTP error, URL/socket error, timeout, protocol error,
            JSON-RPC error, or ``result.isError``. The message is redacted.
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")

    req = urllib.request.Request(
        server_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Read a bounded slice of the error body for context, then redact.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 — error-body read is best-effort
            detail = ""
        raise McpError(
            f"HTTP {exc.code}: {detail}", status_code=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise McpError(f"URL error: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise McpError(f"timeout after {timeout}s: {exc}") from exc
    except ssl.SSLError as exc:
        raise McpError(f"TLS error: {exc}") from exc

    latency_ms = (time.monotonic() - start) * 1000.0
    envelope = _parse_body(body)
    result = _extract_result(envelope)
    return result, latency_ms


def tools_list(
    server_url: str, token: str, timeout: float = 15.0
) -> tuple[list[dict], float]:
    """List the tools exposed by an MCP endpoint.

    Args:
        server_url: The ``/<service>/mcp`` endpoint URL.
        token: Raw Bearer token.
        timeout: Socket timeout in seconds.

    Returns:
        A tuple of ``(tools, latency_ms)`` where ``tools`` is the list of
        tool descriptor dicts (possibly empty).

    Raises:
        McpError: As per :func:`mcp_call`.
    """
    result, latency_ms = mcp_call(server_url, token, "tools/list", {}, timeout)
    tools = result.get("tools")
    if tools is None:
        # Some stateless servers return tools nested in structuredContent.
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            tools = structured.get("tools")
    if not isinstance(tools, list):
        tools = []
    return tools, latency_ms


def tool_call(
    server_url: str,
    token: str,
    name: str,
    arguments: dict,
    timeout: float = 15.0,
) -> tuple[dict, float]:
    """Invoke a single MCP tool and return its decoded payload.

    Args:
        server_url: The ``/<service>/mcp`` endpoint URL.
        token: Raw Bearer token.
        name: Tool name, e.g. ``"stats"``.
        arguments: Tool arguments object.
        timeout: Socket timeout in seconds.

    Returns:
        A tuple of ``(payload, latency_ms)``. ``payload`` is
        ``result.structuredContent`` when present; otherwise a dict
        ``{"content": [<parsed-or-raw texts>]}`` built from
        ``result.content[].text``.

    Raises:
        McpError: As per :func:`mcp_call`.
    """
    result, latency_ms = mcp_call(
        server_url,
        token,
        "tools/call",
        {"name": name, "arguments": arguments},
        timeout,
    )

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, latency_ms

    # Fall back to assembling content[].text, parsing JSON where possible.
    collected: list[Any] = []
    for chunk in result.get("content", []) or []:
        if not isinstance(chunk, dict):
            continue
        text = chunk.get("text")
        if text is None:
            continue
        try:
            collected.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            collected.append(text)

    # Single decoded JSON object is the common case — return it directly so
    # callers get the natural shape rather than a wrapper list.
    if len(collected) == 1 and isinstance(collected[0], dict):
        return collected[0], latency_ms
    return {"content": collected}, latency_ms
