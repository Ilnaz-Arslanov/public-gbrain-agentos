"""G6 hooks-parity and freshness checks for gbrain-doctor.

This module implements checks C022-C034: it loads a configurable expected-hook
manifest, parses every active Claude Code settings layer (global, plugin,
workspace, workspace-local), merges their ``hooks`` blocks with provenance,
resolves placeholder-laden command paths, and asserts that the lifecycle hooks
the agent depends on (SessionStart / Stop / PreCompact by default) are wired,
present on disk, executable, non-async, and firing recently.

A missing or broken lifecycle hook is a *silent* feature breakage (no session
bootstrap, no turn logging, no pre-compact snapshot), so these checks are the
critical group of the doctor.

Public entry point:

    def run_checks(ctx: DoctorContext) -> list[CheckResult]

Only C027 (``hook_scripts_executable``) carries an ``auto_fix`` callback, and it
performs a confirmed ``chmod +x`` on the resolved hook script paths *only* —
never on anything outside the set of paths the manifest/settings point at.

Settings layer precedence (low -> high), per Claude Code docs:

    ~/.claude/settings.json
      < <plugin>/hooks/hooks.json   (only if plugin in enabledPlugins)
      < <workspace>/.claude/settings.json
      < <workspace>/.claude/settings.local.json

``hooks`` arrays are concatenated across layers. ``disableAllHooks: true`` at a
layer kills that layer and everything below it.

stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import redact
from result import CheckResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gbrain_doctor import DoctorContext


# Stop-hook freshness marker emitted by ``agent-template/hooks/stop-hook.sh``:
#   ### 2026-05-28 14:30 [stop-hook]
_STOP_MARKER_RE = re.compile(
    r"^###\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::(\d{2}))?\s+\[stop-hook\]\s*$"
)

# Placeholder -> resolver mapping is built per-context (HOME / project dir /
# plugin root differ between agents), so kept as plain regex tokens here.
# Matches both ``${VAR}`` and bare ``$VAR`` (real hook commands use bare $HOME).
_PLACEHOLDER_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# Shell metacharacters that terminate the leading command token in a shell-form
# ``command`` string (we only need the first real argument/script path).
_SHELL_SPLIT_RE = re.compile(r"\s+(?:&&|\|\||;|\||>|<)\s+")

# Interpreters/wrappers that may precede the real script path in a hook command.
_INTERPRETERS = frozenset(
    {"bash", "sh", "zsh", "python", "python3", "node", "env"}
)

# RED / secret-bearing path patterns that must NEVER be chmod'd by the autofix.
_RED_BASENAMES = frozenset({"CLAUDE.md", "rules.md", "USER.md", ".env"})
_SECRET_SUFFIXES = (".key", ".pem")
_SECRET_DIR_PARTS = frozenset({"secrets", ".secrets"})


# ---------------------------------------------------------------------------
# Settings layer model
# ---------------------------------------------------------------------------


@dataclass
class SettingsLayer:
    """One parsed Claude Code settings source.

    Attributes:
        label: Human-readable provenance label, e.g. ``"workspace/.local"``.
        path: The file the layer was read from.
        data: Parsed JSON object (empty dict when the file was absent).
        present: Whether the file existed on disk.
        parse_error: Redacted ``JSONDecodeError`` message when parsing failed.
        is_local_only: ``True`` for ``settings.local.json`` (non-portable).
        kind: ``"global"`` | ``"plugin"`` | ``"workspace"`` | ``"local"``.
    """

    label: str
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    present: bool = False
    parse_error: str | None = None
    is_local_only: bool = False
    kind: str = "workspace"


@dataclass
class HookEntry:
    """A single normalized hook command with provenance.

    Attributes:
        event: The lifecycle event the hook is registered under.
        command: Raw ``command`` string from settings.
        timeout: Optional declared timeout (seconds).
        is_async: Whether the entry is marked ``async: true``.
        layer_label: Provenance label of the originating settings layer.
        layer_kind: Originating layer kind (global/plugin/workspace/local).
    """

    event: str
    command: str
    timeout: int | None
    is_async: bool
    layer_label: str
    layer_kind: str


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


@dataclass
class Manifest:
    """Parsed expected-hooks manifest."""

    expected: list[dict[str, Any]]
    freshness: dict[str, Any]
    error: str | None = None

    @property
    def expected_events(self) -> list[str]:
        return [str(e.get("event", "")) for e in self.expected if e.get("event")]


def _load_manifest(path: Path) -> Manifest:
    """Load and validate the expected-hooks manifest.

    Args:
        path: Path to the manifest JSON (``ctx.expected_hooks_path``).

    Returns:
        A :class:`Manifest`. On any error ``error`` is set (redacted) and the
        lists are empty, which downstream checks treat as a hard fail for C022.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Manifest([], {}, error=redact.redact(f"cannot read manifest {path}: {exc}"))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Manifest([], {}, error=redact.redact(f"manifest JSON error in {path}: {exc}"))
    if not isinstance(parsed, dict):
        return Manifest([], {}, error=redact.redact(f"manifest {path} is not a JSON object"))
    expected = parsed.get("expected")
    if not isinstance(expected, list) or not expected:
        return Manifest(
            [], {}, error=redact.redact(f"manifest {path} has no non-empty 'expected' list")
        )
    clean: list[dict[str, Any]] = []
    for item in expected:
        if isinstance(item, dict) and item.get("event") and item.get("script"):
            clean.append(item)
    if not clean:
        return Manifest(
            [], {}, error=redact.redact(f"manifest {path} 'expected' entries lack event/script")
        )
    freshness = parsed.get("freshness")
    if not isinstance(freshness, dict):
        freshness = {}
    return Manifest(clean, freshness)


# ---------------------------------------------------------------------------
# Settings layer collection
# ---------------------------------------------------------------------------


def _read_layer(path: Path, label: str, kind: str, *, is_local_only: bool = False) -> SettingsLayer:
    """Read a single settings file into a :class:`SettingsLayer` (never raises)."""
    layer = SettingsLayer(label=label, path=path, kind=kind, is_local_only=is_local_only)
    if not path.is_file():
        return layer
    layer.present = True
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        layer.parse_error = redact.redact(f"cannot read {path}: {exc}")
        return layer
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        layer.parse_error = redact.redact(f"JSON error in {path}: {exc}")
        return layer
    if isinstance(data, dict):
        layer.data = data
    else:
        layer.parse_error = redact.redact(f"{path} is not a JSON object")
    return layer


def _enabled_plugins(layers: list[SettingsLayer]) -> set[str]:
    """Collect ``enabledPlugins`` ids across all parsed layers.

    Claude Code stores enabled plugins as a mapping or list under
    ``enabledPlugins``; we tolerate both shapes and also a nested
    ``{"plugins": {...}}`` form.
    """
    enabled: set[str] = set()
    for layer in layers:
        raw = layer.data.get("enabledPlugins")
        if isinstance(raw, dict):
            for pid, on in raw.items():
                if on:
                    enabled.add(str(pid))
        elif isinstance(raw, list):
            for pid in raw:
                if isinstance(pid, str):
                    enabled.add(pid)
    return enabled


def _plugin_layers(enabled: set[str]) -> list[SettingsLayer]:
    """Build settings layers for each enabled plugin's ``hooks/hooks.json``.

    Plugin id may be ``owner/name@version`` or a bare directory id; we look for
    ``~/.claude/plugins/<sanitized-id>/hooks/hooks.json``. Absent plugin dirs
    yield a non-present layer so C028 can flag a missing plugin for an active
    hook.
    """
    layers: list[SettingsLayer] = []
    plugins_root = Path.home() / ".claude" / "plugins"
    for pid in sorted(enabled):
        # A plugin id can carry an ``@version`` suffix or a path; take the last
        # path segment and strip the version for the directory lookup.
        base = pid.split("/")[-1].split("@", 1)[0]
        hooks_json = plugins_root / base / "hooks" / "hooks.json"
        layers.append(
            _read_layer(
                hooks_json,
                label=f"plugin:{base}",
                kind="plugin",
            )
        )
    return layers


def _collect_layers(ctx: "DoctorContext") -> list[SettingsLayer]:
    """Collect every active settings layer low->high.

    Order: global < plugin(s) < workspace < workspace-local. Plugin layers are
    only inserted for plugins that appear in ``enabledPlugins`` of the
    global/workspace layers.
    """
    home = Path.home()
    global_layer = _read_layer(home / ".claude" / "settings.json", "~/.claude", "global")

    # ``ctx.workspace_root`` ALREADY points at the ``<agent>/.claude`` directory
    # (e.g. ``~/.claude-lab/silvana/.claude``), so settings files live directly
    # under it — NOT under a nested ``.claude``. (C033/C034 use ``ws/"core/..."``
    # for the same reason.)
    ws = ctx.workspace_root
    workspace_layer: SettingsLayer
    local_layer: SettingsLayer
    if ws is not None:
        workspace_layer = _read_layer(ws / "settings.json", "workspace", "workspace")
        local_layer = _read_layer(
            ws / "settings.local.json",
            "workspace/.local",
            "local",
            is_local_only=True,
        )
    else:
        # No resolved workspace: still honor a CWD-local ``.claude`` if present.
        # Here CWD is a project root, so the nested ``.claude`` is correct.
        cwd = Path.cwd()
        workspace_layer = _read_layer(cwd / ".claude" / "settings.json", "cwd", "workspace")
        local_layer = _read_layer(
            cwd / ".claude" / "settings.local.json",
            "cwd/.local",
            "local",
            is_local_only=True,
        )

    # enabledPlugins is read from global + workspace + local (any may declare).
    plugin_decl = [global_layer, workspace_layer, local_layer]
    plugins = _plugin_layers(_enabled_plugins(plugin_decl))

    return [global_layer, *plugins, workspace_layer, local_layer]


def _active_layers(layers: list[SettingsLayer]) -> tuple[list[SettingsLayer], SettingsLayer | None]:
    """Return the layers whose hooks remain active after ``disableAllHooks``.

    ``disableAllHooks: true`` kills *that* layer and every layer below it (lower
    precedence). Since ``layers`` is ordered low->high, we find the highest
    layer that sets the flag and drop it plus everything before it.

    Returns:
        ``(active_layers, disabling_layer)`` where ``disabling_layer`` is the
        topmost layer asserting ``disableAllHooks`` (or ``None``).
    """
    disabling_index = -1
    disabling_layer: SettingsLayer | None = None
    for idx, layer in enumerate(layers):
        if layer.data.get("disableAllHooks") is True:
            disabling_index = idx
            disabling_layer = layer
    if disabling_index < 0:
        return layers, None
    return layers[disabling_index + 1 :], disabling_layer


# ---------------------------------------------------------------------------
# Hook entry extraction + placeholder resolution
# ---------------------------------------------------------------------------


def _extract_hooks(layers: list[SettingsLayer]) -> list[HookEntry]:
    """Flatten ``hooks`` blocks from active layers into :class:`HookEntry` rows.

    Schema per layer::

        {"hooks": {"<Event>": [{"matcher": "...",
                                "hooks": [{"type": "command",
                                           "command": "...",
                                           "timeout": N,
                                           "async": false}]}]}}
    """
    entries: list[HookEntry] = []
    for layer in layers:
        hooks = layer.data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                inner = group.get("hooks")
                if not isinstance(inner, list):
                    continue
                for entry in inner:
                    if not isinstance(entry, dict):
                        continue
                    command = entry.get("command")
                    if not isinstance(command, str):
                        command = ""
                    timeout = entry.get("timeout")
                    timeout = timeout if isinstance(timeout, int) else None
                    entries.append(
                        HookEntry(
                            event=str(event),
                            command=command,
                            timeout=timeout,
                            is_async=bool(entry.get("async") is True),
                            layer_label=layer.label,
                            layer_kind=layer.kind,
                        )
                    )
    return entries


def _build_placeholder_map(ctx: "DoctorContext") -> dict[str, str]:
    """Build the ``${VAR}`` -> value map used before ``Path.exists()``.

    ``${CLAUDE_PROJECT_DIR}`` resolves to the agent workspace root when known,
    else the repo root. ``${CLAUDE_PLUGIN_ROOT}`` resolves to the Claude plugins
    directory; an exact plugin id is not known per-command, so this is a best
    effort base used only to turn the placeholder into a real existing prefix.
    """
    home = str(Path.home())
    project_dir = str(ctx.workspace_root) if ctx.workspace_root else str(ctx.repo_root)
    plugin_root = str(Path.home() / ".claude" / "plugins")
    mapping = {
        "HOME": home,
        "CLAUDE_PROJECT_DIR": project_dir,
        "CLAUDE_PLUGIN_ROOT": plugin_root,
    }
    # Fall back to the live environment for any other ${VAR} we encounter.
    return mapping


def _resolve_command_path(command: str, placeholders: dict[str, str]) -> Path | None:
    """Resolve the script path a shell-form ``command`` ultimately runs.

    Steps:
        1. Substitute both ``${VAR}`` and bare ``$VAR`` placeholders (context map
           first, then live environment; unknown vars left as literal text).
        2. Split with ``shlex`` so quoting is honored (quotes are stripped).
        3. Skip a leading interpreter wrapper (``bash``/``/usr/bin/env`` ...),
           its flags, and ``VAR=val`` assignments; take the first real path token.

    Inline-code commands (``bash -c '...code...'``) carry no resolvable script
    path; these return ``None`` and the caller emits a WARN (wrapper-obscured),
    never a FAIL.

    Returns:
        The resolved :class:`Path` (not guaranteed to exist), or ``None`` when
        no script-like token can be isolated.
    """
    if not command.strip():
        return None

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in placeholders:
            return placeholders[name]
        return os.environ.get(name, match.group(0))

    expanded = _PLACEHOLDER_RE.sub(_sub, command).strip()

    # Cut at the first shell operator so trailing ``&& foo`` does not pollute
    # the path token.
    head = _SHELL_SPLIT_RE.split(expanded, maxsplit=1)[0].strip()
    if not head:
        return None

    # shlex honors quotes ("/path with space/x.sh") and drops the quote chars.
    # On malformed quoting fall back to a naive split so we never raise.
    try:
        tokens = shlex.split(head)
    except ValueError:
        tokens = head.split()
    if not tokens:
        return None

    idx = 0
    # Skip a leading interpreter wrapper (``bash``, ``/usr/bin/env`` ...).
    if Path(tokens[0]).name in _INTERPRETERS:
        idx = 1
        # ``env`` may stack a second interpreter (``/usr/bin/env bash ...``).
        if (
            idx < len(tokens)
            and Path(tokens[0]).name == "env"
            and Path(tokens[idx]).name in _INTERPRETERS
        ):
            idx += 1
        # Skip ``VAR=val`` assignments (``env FOO=bar script``).
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("/"):
            idx += 1
        # Skip flags. ``-c``/``--command`` (and combined ``-lc``) consume inline
        # code, not a path -> wrapper-obscured, return None for a WARN upstream.
        while idx < len(tokens) and tokens[idx].startswith("-"):
            flag = tokens[idx]
            if flag in ("-c", "--command") or ("c" in flag.lstrip("-") and flag != "--"):
                return None
            idx += 1

    if idx >= len(tokens):
        return None
    candidate = tokens[idx]
    # A surviving ``VAR=val`` (or another flag) means no script path is present.
    if candidate.startswith("-"):
        return None
    if "=" in candidate and not candidate.startswith("/"):
        return None
    return Path(candidate).expanduser()


def _make_chmod_fix(paths: list[Path]) -> Callable[[], bool]:
    """Build a confirmed-``chmod +x`` autofix bound to exactly ``paths``.

    The returned callable adds the user/group/other execute bits to each given
    path and returns ``True`` only if every target ended up executable. It
    never touches any path outside the captured list.
    """
    targets = list(paths)

    def _fix() -> bool:
        ok = True
        for p in targets:
            try:
                if not p.is_file():
                    ok = False
                    continue
                mode = p.stat().st_mode
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                if not os.access(p, os.X_OK):
                    ok = False
            except OSError:
                ok = False
        return ok

    return _fix


def _approved_chmod_roots(ctx: "DoctorContext", enabled: set[str]) -> list[Path]:
    """Directories under which the chmod autofix is allowed to operate.

    Approved: the workspace ``hooks/`` dir, the per-agent ``~/.claude-lab/<agent>/
    hooks`` dir, ``~/.claude/hooks``, and each enabled plugin's ``hooks`` dir.
    Any hook path outside ALL of these is never chmod'd.
    """
    roots: list[Path] = []
    home = Path.home()
    ws = ctx.workspace_root
    if ws is not None:
        # ``ws`` is ``<agent>/.claude``; hooks may live under it or one level up
        # (agents wire hooks at ``~/.claude-lab/<agent>/hooks`` next to .claude).
        roots.append(ws / "hooks")
        roots.append(ws.parent / "hooks")
    roots.append(home / ".claude" / "hooks")
    plugins_root = home / ".claude" / "plugins"
    for pid in enabled:
        base = pid.split("/")[-1].split("@", 1)[0]
        roots.append(plugins_root / base / "hooks")
    return roots


def _is_secret_or_red_path(path: Path) -> bool:
    """True if ``path`` matches a RED/secret pattern that must never be chmod'd."""
    if path.name in _RED_BASENAMES:
        return True
    if path.suffix in _SECRET_SUFFIXES:
        return True
    # Any ``secrets/`` or ``.secrets`` component in the path.
    if _SECRET_DIR_PARTS & set(path.parts):
        return True
    return False


def _chmod_allowed(path: Path, approved_roots: list[Path], want_basename: str | None) -> bool:
    """Gate a single hook path for the chmod autofix.

    A path is eligible ONLY when it (a) is not a RED/secret path, (b) matches the
    manifest basename for its event, and (c) resolves under an approved root.
    Symlinks/relative parts are normalized before the prefix check.
    """
    if _is_secret_or_red_path(path):
        return False
    if not want_basename or path.name != want_basename:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in approved_roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Freshness helpers
# ---------------------------------------------------------------------------


def _parse_stop_marker_age_hours(recent_md: Path) -> tuple[float | None, str | None]:
    """Find the newest ``[stop-hook]`` marker in recent.md and return its age.

    The marker timestamp is written by ``stop-hook.sh`` with ``date -u``, so it
    is UTC. We interpret it as UTC only and compute the age against the current
    UTC time. (The previous dual-TZ "pick the smaller non-negative age" logic
    biased toward a false PASS by silently choosing whichever interpretation
    looked freshest -- dropped here.)

    Returns:
        ``(age_hours, raw_marker)``. ``age_hours`` is ``None`` when no marker is
        found; ``raw_marker`` is the matched timestamp text for the message.
    """
    try:
        text = recent_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    newest: datetime | None = None
    newest_raw: str | None = None
    now_utc = datetime.now(timezone.utc)

    for line in text.splitlines():
        m = _STOP_MARKER_RE.match(line.strip())
        if not m:
            continue
        date_s, time_s, sec_s = m.group(1), m.group(2), m.group(3)
        sec = int(sec_s) if sec_s else 0
        try:
            y, mo, d = (int(x) for x in date_s.split("-"))
            hh, mm = (int(x) for x in time_s.split(":"))
            # Marker is written with ``date -u`` -> always UTC.
            stamp = datetime(y, mo, d, hh, mm, sec, tzinfo=timezone.utc)
        except ValueError:
            continue

        if newest is None or stamp > newest:
            newest = stamp
            newest_raw = f"{date_s} {time_s}"

    if newest is None:
        return None, None
    age_hours = max((now_utc - newest).total_seconds(), 0.0) / 3600.0
    return age_hours, newest_raw


# ---------------------------------------------------------------------------
# Individual checks (C022-C034)
# ---------------------------------------------------------------------------

_G = "G6"


def _r(check: str, status: str, message: str, remediation: str | None = None,
       auto_fix: Callable[[], bool] | None = None) -> CheckResult:
    """Build a CheckResult with a redacted message and the G6 name prefix."""
    return CheckResult(
        name=f"{_G}.{check}",
        status=status,  # type: ignore[arg-type]
        message=redact.redact(message),
        remediation=remediation,
        auto_fix=auto_fix,
    )


def _safe(fn: Callable[..., CheckResult], *args: Any) -> CheckResult:
    """Run a per-check function, converting any escape into a redacted fail."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - no exception escapes run_checks
        name = getattr(fn, "_check_id", fn.__name__)
        return CheckResult(
            name=f"{_G}.{name}",
            status="fail",
            message=redact.redact(f"check raised: {exc!r}"),
            remediation="Inspect checks_hooks.py; this is a doctor bug.",
        )


def run_checks(ctx: "DoctorContext") -> list[CheckResult]:
    """Run G6 hooks-parity + freshness checks (C022-C034).

    Args:
        ctx: The shared :class:`DoctorContext`.

    Returns:
        An ordered list of :class:`CheckResult`. Empty when ``G6`` is filtered
        out. No exception escapes this function.
    """
    if not ctx.want("G6"):
        return []

    results: list[CheckResult] = []

    # --- shared state, gathered defensively --------------------------------
    manifest = _load_manifest(ctx.expected_hooks_path)
    try:
        layers = _collect_layers(ctx)
    except Exception as exc:  # noqa: BLE001
        layers = []
        results.append(
            _r(
                "settings_layers_parse",
                "fail",
                f"could not collect settings layers: {exc!r}",
                "Inspect checks_hooks.py settings collection.",
            )
        )

    active_layers, disabling_layer = _active_layers(layers)
    entries = _extract_hooks(active_layers)
    placeholders = _build_placeholder_map(ctx)

    found_layers = [ly for ly in layers if ly.present]

    # --- C022 hooks_manifest_loaded ----------------------------------------
    if manifest.error:
        results.append(
            _r(
                "hooks_manifest_loaded",
                "fail",
                f"expected-hooks manifest unusable: {manifest.error}",
                "Fix manifest JSON or pass a valid --expected-hooks path.",
            )
        )
    else:
        results.append(
            _r(
                "hooks_manifest_loaded",
                "pass",
                f"manifest loaded: {len(manifest.expected)} expected hook(s) "
                f"({', '.join(manifest.expected_events)}) from "
                f"{ctx.expected_hooks_path.name}",
            )
        )

    # --- C023 settings_layers_parse ----------------------------------------
    broken = [ly for ly in found_layers if ly.parse_error]
    if not found_layers:
        results.append(
            _r(
                "settings_layers_parse",
                "skip",
                "no Claude Code settings layers found "
                "(no ~/.claude/settings.json or workspace settings)",
                "Render settings from agent-template/templates/settings.json.template.",
            )
        )
    elif broken:
        msg = "; ".join(f"{ly.label}: {ly.parse_error}" for ly in broken)
        results.append(
            _r(
                "settings_layers_parse",
                "fail",
                f"invalid JSON in {len(broken)} settings layer(s): {msg}",
                "Fix JSON syntax in the named settings file.",
            )
        )
    else:
        results.append(
            _r(
                "settings_layers_parse",
                "pass",
                f"{len(found_layers)} settings layer(s) parsed: "
                f"{', '.join(ly.label for ly in found_layers)}",
            )
        )

    # --- C024 disable_all_hooks_absent -------------------------------------
    if not found_layers:
        results.append(
            _r(
                "disable_all_hooks_absent",
                "skip",
                "no settings layers found; disableAllHooks not applicable",
            )
        )
    elif disabling_layer is not None:
        results.append(
            _r(
                "disable_all_hooks_absent",
                "fail",
                f"layer {disabling_layer.label} sets disableAllHooks:true "
                "(kills that layer and all below it)",
                "Remove/override disableAllHooks after operator approval.",
            )
        )
    else:
        results.append(
            _r(
                "disable_all_hooks_absent",
                "pass",
                "no active settings layer disables hooks",
            )
        )

    # Map expected event -> entries for reuse below.
    expected_events = manifest.expected_events if not manifest.error else []
    entries_by_event: dict[str, list[HookEntry]] = {}
    for e in entries:
        entries_by_event.setdefault(e.event, []).append(e)

    # --- C025 expected_events_registered -----------------------------------
    if manifest.error:
        results.append(
            _r(
                "expected_events_registered",
                "skip",
                "manifest unusable; cannot evaluate expected events",
            )
        )
    elif not found_layers:
        results.append(
            _r(
                "expected_events_registered",
                "fail",
                f"no settings layers found but manifest requires "
                f"{', '.join(expected_events)}",
                "Render settings.json.template into the agent workspace.",
            )
        )
    else:
        missing = [ev for ev in expected_events if not entries_by_event.get(ev)]
        registered = [ev for ev in expected_events if entries_by_event.get(ev)]
        if missing and not registered:
            # M3: settings parsed but ZERO expected lifecycle hooks wired. Many
            # agents wire memory via cron/gateway/plugin instead of settings.json,
            # so a total absence is a WARN (configuration variance), not a defect.
            results.append(
                _r(
                    "expected_events_registered",
                    "warn",
                    f"no expected lifecycle hook event registered in settings "
                    f"({', '.join(missing)}); hooks may be wired via "
                    "cron/gateway/plugin instead of settings.json",
                    "If this agent uses a different hook mechanism, set a per-agent "
                    "--expected-hooks manifest; otherwise render settings.json.template.",
                )
            )
        elif missing:
            # Partial wiring: SOME expected hooks present, others missing -> real
            # defect (a half-installed hook set).
            results.append(
                _r(
                    "expected_events_registered",
                    "fail",
                    f"expected hook event(s) not registered in any active layer: "
                    f"{', '.join(missing)}",
                    "Reinstall/render settings.json.template or adjust the manifest.",
                )
            )
        else:
            results.append(
                _r(
                    "expected_events_registered",
                    "pass",
                    f"all expected events registered: {', '.join(expected_events)}",
                )
            )

    # Resolve, per expected event, the command paths in play (reused by
    # C026/C027/C031). Build once.
    @dataclass
    class _Resolved:
        event: str
        entry: HookEntry
        path: Path | None

    resolved: list[_Resolved] = []
    for ev in expected_events:
        for entry in entries_by_event.get(ev, []):
            resolved.append(_Resolved(ev, entry, _resolve_command_path(entry.command, placeholders)))

    # --- C026 hook_command_paths_exist -------------------------------------
    if manifest.error or not found_layers:
        results.append(
            _r("hook_command_paths_exist", "skip",
               "no manifest/settings to resolve expected hook paths")
        )
    elif not resolved:
        results.append(
            _r("hook_command_paths_exist", "skip",
               "expected events not registered; path existence covered by C025")
        )
    else:
        missing_paths: list[str] = []
        unresolved: list[str] = []
        for item in resolved:
            if item.path is None:
                unresolved.append(f"{item.event} (cannot resolve command)")
            elif not item.path.exists():
                missing_paths.append(f"{item.event}: {item.path}")
        if missing_paths:
            results.append(
                _r(
                    "hook_command_paths_exist",
                    "fail",
                    f"registered hook command path(s) missing: {'; '.join(missing_paths)}",
                    "Copy hooks from agent-template/hooks/ or fix the settings path.",
                )
            )
        elif unresolved:
            results.append(
                _r(
                    "hook_command_paths_exist",
                    "warn",
                    f"could not resolve script path for: {'; '.join(unresolved)}",
                    "Use an explicit script path in the hook command.",
                )
            )
        else:
            results.append(
                _r(
                    "hook_command_paths_exist",
                    "pass",
                    f"all {len(resolved)} expected hook command path(s) exist",
                )
            )

    # --- C027 hook_scripts_executable (auto_fix: chmod +x ONLY) ------------
    # Manifest basename per event + approved roots gate the autofix (M1): chmod
    # only attaches to a path that matches the manifest basename for its event
    # AND lives under an approved hooks root AND is not a RED/secret path.
    manifest_basename = {
        str(e.get("event")): Path(str(e.get("script", ""))).name
        for e in manifest.expected
    } if not manifest.error else {}
    approved_roots = _approved_chmod_roots(ctx, _enabled_plugins(layers))
    existing_paths = [item.path for item in resolved if item.path and item.path.is_file()]
    non_exec_items = [
        item
        for item in resolved
        if item.path and item.path.is_file() and not os.access(item.path, os.X_OK)
    ]
    non_exec = [item.path for item in non_exec_items if item.path]
    # Partition non-executable paths into chmod-eligible (gated) vs rejected.
    fixable: list[Path] = []
    rejected: list[Path] = []
    for item in non_exec_items:
        if item.path is None:
            continue
        if _chmod_allowed(item.path, approved_roots, manifest_basename.get(item.event)):
            fixable.append(item.path)
        else:
            rejected.append(item.path)
    if manifest.error or not found_layers or not resolved:
        results.append(
            _r("hook_scripts_executable", "skip",
               "no resolvable expected hook scripts to test for the +x bit")
        )
    elif not existing_paths:
        results.append(
            _r("hook_scripts_executable", "skip",
               "expected hook script paths missing; covered by C026")
        )
    elif non_exec:
        # auto_fix bound to ONLY the gated, manifest-matched paths under approved
        # roots; rejected paths (outside roots / RED / secret / name-mismatch)
        # are reported but never chmod'd automatically.
        msg = f"hook script(s) not executable: {'; '.join(str(p) for p in non_exec)}"
        if rejected:
            msg += (
                f" [{len(rejected)} path(s) NOT auto-fixable: outside approved hooks "
                f"root / name mismatch / RED-or-secret path -- verify before chmod]"
            )
        remediation = (
            "chmod +x the listed hook script(s) "
            "(auto-fix covers only manifest-matched scripts under approved roots; "
            "chmod the rest manually after verifying the path is the intended hook)."
        )
        results.append(
            _r(
                "hook_scripts_executable",
                "fail",
                msg,
                remediation,
                auto_fix=_make_chmod_fix(fixable) if fixable else None,
            )
        )
    else:
        results.append(
            _r(
                "hook_scripts_executable",
                "pass",
                f"all {len(existing_paths)} expected hook script(s) executable",
            )
        )

    # --- C028 plugin_hook_refs_enabled -------------------------------------
    # L1: only WARN when a *settings* hook entry actually references a plugin hook
    # source (``${CLAUDE_PLUGIN_ROOT}`` or a path under ~/.claude/plugins/.../hooks)
    # that is missing/disabled. An enabled plugin that merely lacks
    # hooks/hooks.json and is referenced by NObody is normal -> pass/skip.
    plugin_entries = [e for e in entries if e.layer_kind == "plugin"]
    plugin_layers_present = [ly for ly in layers if ly.kind == "plugin" and ly.present]
    plugins_hooks_dir = str(Path.home() / ".claude" / "plugins")

    def _refs_plugin_hook(cmd: str) -> bool:
        """True if a non-plugin-layer command points at a plugin hook source."""
        if "${CLAUDE_PLUGIN_ROOT}" in cmd or "$CLAUDE_PLUGIN_ROOT" in cmd:
            return True
        resolved = _resolve_command_path(cmd, placeholders)
        if resolved is None:
            # Cannot resolve; fall back to a literal plugins-path substring check.
            return plugins_hooks_dir in cmd
        try:
            return plugins_hooks_dir in str(resolved.resolve())
        except OSError:
            return plugins_hooks_dir in str(resolved)

    # Settings entries (workspace/global/local layers) that reference a plugin.
    referencing_entries = [
        e for e in entries if e.layer_kind != "plugin" and _refs_plugin_hook(e.command)
    ]
    # Of the missing plugin layers, only those actually referenced are a problem.
    referenced_paths = {
        str(_resolve_command_path(e.command, placeholders))
        for e in referencing_entries
    }
    missing_referenced = []
    for ly in layers:
        if ly.kind != "plugin" or ly.present:
            continue
        # ``ly.path`` is ``.../plugins/<base>/hooks/hooks.json``; treat the plugin
        # as referenced if any referencing entry resolved under its hooks dir, or
        # if any referencing entry used the generic ${CLAUDE_PLUGIN_ROOT} token.
        plugin_hooks_dir = str(ly.path.parent)
        if any(
            (rp != "None" and plugin_hooks_dir in rp) for rp in referenced_paths
        ) or any(
            ("CLAUDE_PLUGIN_ROOT" in e.command) for e in referencing_entries
        ):
            missing_referenced.append(ly)

    if not referencing_entries:
        # No settings hook references a plugin source -> nothing to validate.
        results.append(
            _r("plugin_hook_refs_enabled", "pass",
               "no settings hook references a plugin hook source")
        )
    elif missing_referenced:
        names = ", ".join(ly.label for ly in missing_referenced)
        results.append(
            _r(
                "plugin_hook_refs_enabled",
                "warn",
                f"settings hook references plugin hook source(s) that are "
                f"missing/disabled: {names}",
                "Install/repair the plugin or remove the stale hook reference.",
            )
        )
    else:
        results.append(
            _r(
                "plugin_hook_refs_enabled",
                "pass",
                f"{len(referencing_entries)} settings hook reference(s) to "
                f"{len(plugin_layers_present)} present plugin hook source(s) "
                f"({len(plugin_entries)} plugin hook entries)",
            )
        )

    # --- C029 hook_event_names_valid ---------------------------------------
    whitelist = _load_event_whitelist(ctx.skill_root)
    if whitelist is None:
        results.append(
            _r(
                "hook_event_names_valid",
                "warn",
                "could not load references/claude-hook-events.json; skipping typo check",
                "Restore the event whitelist reference file.",
            )
        )
    else:
        all_events = {e.event for e in entries}
        unknown = sorted(ev for ev in all_events if ev not in whitelist)
        if unknown:
            results.append(
                _r(
                    "hook_event_names_valid",
                    "fail",
                    f"unknown/typo hook event name(s): {', '.join(unknown)}",
                    "Rename to a canonical Claude Code hook event.",
                )
            )
        else:
            results.append(
                _r(
                    "hook_event_names_valid",
                    "pass",
                    f"all {len(all_events)} registered event name(s) are canonical",
                )
            )

    # --- C030 expected_hooks_not_local_only --------------------------------
    if manifest.error or not found_layers or not expected_events:
        results.append(
            _r("expected_hooks_not_local_only", "skip",
               "no manifest/settings to evaluate hook provenance")
        )
    else:
        portable_required = {
            str(e.get("event"))
            for e in manifest.expected
            if e.get("portable_required", True)
        }
        local_only_events: list[str] = []
        for ev in expected_events:
            evs = entries_by_event.get(ev, [])
            if not evs:
                continue  # absence handled by C025
            if ev in portable_required and all(e.layer_kind == "local" for e in evs):
                local_only_events.append(ev)
        if local_only_events:
            results.append(
                _r(
                    "expected_hooks_not_local_only",
                    "warn",
                    f"portable hook(s) registered only in settings.local.json: "
                    f"{', '.join(local_only_events)}",
                    "Move portable hook wiring into committed/rendered settings.",
                )
            )
        else:
            results.append(
                _r(
                    "expected_hooks_not_local_only",
                    "pass",
                    "all registered portable hooks live outside local-only settings",
                )
            )

    # --- C031 hook_script_basename_matches_manifest ------------------------
    expected_basename = {
        str(e.get("event")): Path(str(e.get("script", ""))).name
        for e in manifest.expected
    } if not manifest.error else {}
    if manifest.error or not resolved:
        results.append(
            _r("hook_script_basename_matches_manifest", "skip",
               "no resolvable expected hooks to compare against the manifest")
        )
    else:
        mismatches: list[str] = []
        obscured: list[str] = []
        for item in resolved:
            want = expected_basename.get(item.event)
            if not want:
                continue
            if item.path is None:
                obscured.append(f"{item.event} (shell wrapper obscures path)")
                continue
            if item.path.name != want:
                mismatches.append(f"{item.event}: got {item.path.name}, want {want}")
        # A basename mismatch against the DEFAULT (shipped) manifest is advisory:
        # the agent simply uses its own hook naming, and the scripts already exist
        # (C026) and are executable (C027). Only when the operator supplies an
        # explicit --expected-hooks manifest does a mismatch break a declared
        # contract and warrant FAIL.
        _default_manifest = (
            Path(__file__).resolve().parent.parent
            / "references" / "expected-hooks.json"
        )
        try:
            _is_default = (
                ctx.expected_hooks_path.resolve() == _default_manifest.resolve()
            )
        except Exception:
            _is_default = True
        if mismatches and not _is_default:
            results.append(
                _r(
                    "hook_script_basename_matches_manifest",
                    "fail",
                    f"hook script basename mismatch: {'; '.join(mismatches)}",
                    "Update the manifest or settings to the intended script.",
                )
            )
        elif mismatches:
            results.append(
                _r(
                    "hook_script_basename_matches_manifest",
                    "warn",
                    "agent uses custom hook script names vs the default manifest "
                    f"(scripts exist and are executable): {'; '.join(mismatches)}",
                    "Expected for agents with custom hook naming. Provide a "
                    "per-agent --expected-hooks manifest to silence this.",
                )
            )
        elif obscured:
            results.append(
                _r(
                    "hook_script_basename_matches_manifest",
                    "warn",
                    f"shell wrapper hides script path for: {'; '.join(obscured)}",
                    "Reference the script directly so it can be verified.",
                )
            )
        else:
            results.append(
                _r(
                    "hook_script_basename_matches_manifest",
                    "pass",
                    "expected hook script basenames match the manifest",
                )
            )

    # --- C032 blocking_hooks_not_async -------------------------------------
    blocking_events = {
        str(e.get("event")) for e in manifest.expected if e.get("blocking", True)
    } if not manifest.error else set()
    if manifest.error or not entries:
        results.append(
            _r("blocking_hooks_not_async", "skip",
               "no manifest/registered hooks to check for async flag")
        )
    else:
        async_blocking: list[str] = []
        for ev in blocking_events:
            for e in entries_by_event.get(ev, []):
                if e.is_async:
                    async_blocking.append(f"{ev} ({e.layer_label})")
        if async_blocking:
            results.append(
                _r(
                    "blocking_hooks_not_async",
                    "fail",
                    f"blocking lifecycle hook(s) marked async:true: "
                    f"{', '.join(async_blocking)}",
                    "Remove async:true for blocking lifecycle hooks.",
                )
            )
        else:
            results.append(
                _r(
                    "blocking_hooks_not_async",
                    "pass",
                    "no blocking lifecycle hook is async:true",
                )
            )

    # --- C033 stop_hook_recent_fresh ---------------------------------------
    results.append(_check_stop_fresh(ctx, manifest, entries_by_event))

    # --- C034 precompact_snapshot_exists -----------------------------------
    results.append(_check_precompact_snapshot(ctx, manifest))

    return results


def _load_event_whitelist(skill_root: Path) -> set[str] | None:
    """Load the canonical hook event whitelist; ``None`` on any failure."""
    path = skill_root / "references" / "claude-hook-events.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, list):
        return {str(x) for x in data}
    return None


def _check_stop_fresh(
    ctx: "DoctorContext",
    manifest: Manifest,
    entries_by_event: dict[str, list[HookEntry]],
) -> CheckResult:
    """C033: assert the Stop hook fired recently via recent.md marker age."""
    ws = ctx.workspace_root
    if ws is None:
        return _r(
            "stop_hook_recent_fresh",
            "skip",
            "no resolved workspace; cannot locate core/hot/recent.md "
            "(pass --agent from the agent workspace)",
        )
    recent_md = ws / "core" / "hot" / "recent.md"
    max_age = 24.0
    if isinstance(manifest.freshness.get("recent_stop_hook_max_age_hours"), (int, float)):
        max_age = float(manifest.freshness["recent_stop_hook_max_age_hours"])

    stop_registered = bool(entries_by_event.get("Stop"))

    if not recent_md.is_file():
        if stop_registered:
            return _r(
                "stop_hook_recent_fresh",
                "fail",
                f"Stop hook registered but {recent_md} is missing "
                "(stop-hook never wrote a turn snippet)",
                "Start a session/turn or repair the Stop hook.",
            )
        return _r(
            "stop_hook_recent_fresh",
            "warn",
            f"{recent_md} missing and no Stop hook registered",
            "Wire the Stop hook so turn activity is recorded.",
        )

    age_hours, raw = _parse_stop_marker_age_hours(recent_md)
    if age_hours is None:
        return _r(
            "stop_hook_recent_fresh",
            "warn",
            "recent.md present but has no [stop-hook] marker "
            "(no turn recorded yet, or markers trimmed)",
            "Run a turn so the Stop hook appends a marker.",
        )
    if age_hours <= max_age:
        return _r(
            "stop_hook_recent_fresh",
            "pass",
            f"last stop-hook marker {raw} is {age_hours:.1f}h old (<= {max_age:.0f}h)",
        )
    return _r(
        "stop_hook_recent_fresh",
        "warn",
        f"last stop-hook marker {raw} is {age_hours:.1f}h old (> {max_age:.0f}h)",
        "Start a session/turn or repair the Stop hook.",
    )


def _check_precompact_snapshot(ctx: "DoctorContext", manifest: Manifest) -> CheckResult:
    """C034: assert PreCompact produced at least one snapshot."""
    ws = ctx.workspace_root
    if ws is None:
        return _r(
            "precompact_snapshot_exists",
            "skip",
            "no resolved workspace; cannot locate pre-compact snapshots "
            "(pass --agent from the agent workspace)",
        )
    glob = manifest.freshness.get("precompact_snapshot_glob") or "core/hot/pre-compact/recent-*.md"
    if not isinstance(glob, str):
        glob = "core/hot/pre-compact/recent-*.md"

    # Snapshot directory is the parent of the glob's last path segment.
    snap_dir = ws / Path(glob).parent
    try:
        if not snap_dir.exists():
            return _r(
                "precompact_snapshot_exists",
                "warn",
                f"no pre-compact snapshot directory yet ({snap_dir})",
                "Trigger a manual compact or repair the PreCompact hook.",
            )
        matches = list(ws.glob(glob))
    except OSError as exc:
        return _r(
            "precompact_snapshot_exists",
            "fail",
            f"pre-compact snapshot dir inaccessible: {exc}",
            "Fix permissions on the pre-compact snapshot directory.",
        )
    if matches:
        return _r(
            "precompact_snapshot_exists",
            "pass",
            f"{len(matches)} pre-compact snapshot(s) present under {snap_dir.name}/",
        )
    return _r(
        "precompact_snapshot_exists",
        "warn",
        f"pre-compact snapshot dir exists but is empty ({snap_dir})",
        "Trigger a manual compact or repair the PreCompact hook.",
    )
