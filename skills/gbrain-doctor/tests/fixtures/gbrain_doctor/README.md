# gbrain-doctor test fixtures

Static fixtures for `tests/test_gbrain_doctor_skill.py`. No network, no real `~/.claude`.

| Fixture | Scenario it exercises |
|---|---|
| `mcp-valid.json` | Well-formed `.mcp.json` with memory/recall/swarm (no tasks). |
| `mcp-malformed.json` | Truncated JSON — explicit `--mcp-json` must exit 2. |
| `settings-complete.json` | All three expected events registered, portable, non-async. Green G6 baseline. |
| `settings-missing-hook.json` | Only SessionStart registered — Stop/PreCompact missing (C025). |
| `settings-typo-event.json` | `StopHook` / `PreCompactt` typo event names (C029). |
| `settings.local.json` | Expected hooks present only in a `.local` layer (C030 local-only). |
| `settings-async-blocking.json` | Stop hook has `async: true` on a blocking lifecycle hook (C032). |
| `settings-disable-all-hooks.json` | `disableAllHooks: true` active layer (C024). |
| `workspace/hooks/session-start-hook.sh` | Executable hook (baseline). |
| `workspace/hooks/stop-hook.sh` | Non-executable hook for C027 (tests chmod 644 defensively). |
| `workspace/hooks/precompact-hook.sh` | Executable PreCompact hook. |
| `workspace/core/hot/recent.md` | Stale `[stop-hook]` marker dated 2026-01-01 (C033 freshness warn). |
| `workspace/core/hot/pre-compact/recent-*.md` | A PreCompact snapshot so C034 can pass. |

The executable bit on `stop-hook.sh` is set to 644 at creation, but git does not reliably preserve
non-exec bits across clones, so the hooks tests `chmod 0o644` it at setup to guarantee the
non-executable scenario regardless of checkout state.
