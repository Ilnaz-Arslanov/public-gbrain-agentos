# 80-error-patterns

Incidents + fixes. **Weighted 3.0× in recall** — this folder is the team's institutional muscle memory.

## What goes here

- Bugs you hit, with the symptom you observed, the root cause, and the fix
- Production incidents, even minor ones
- «I spent two hours on this, no one else should» moments
- Provider quirks that bit you (rate limits, undocumented behavior, breaking changes)
- Recurring failure modes (write the pattern, not just the instance)

## What does NOT go here

- Step-by-step procedures — those go to `70-runbooks/`
- Decisions made in response — `30-decisions/`
- General knowledge — `50-knowledge/`

## Why weight 3.0×

When an agent searches the vault, results from this folder are ranked higher than the same text in other folders. The reasoning: if you have ever solved this exact problem, you want to surface that fact **before** you start fresh debugging. The 3.0× multiplier is tuned in `services/recall_mcp/source_weights.py` — adjust if your team's error-pattern signal is too noisy or too weak.

## What a good error-pattern entry looks like

1. **Symptom** — exactly what you observed (logs, error messages, user-facing behavior)
2. **Root cause** — the actual bug, with file / line references when applicable
3. **Fix** — what you changed, with a commit hash if possible
4. **Detection** — how do you know if this happens again? (alert, log line, metric)
5. **Prevention** — is there a test / lint / hook that would catch this next time?

The format is intentionally similar to a postmortem, but lighter — a one-page write-up is fine.

## Frontmatter

```yaml
---
type: error-pattern
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coder-agent
tags: [mcp, auth, asgi]
related: ['30-decisions/2026-05-16-drop-fallback-token.md']
weight: 3.0
severity: high
---
```

## Naming

```
2026-05-16-mcp-stateless-headers-lost.md
2026-05-16-pgvector-hnsw-build-oom.md
2026-05-16-caddy-sse-buffering-broke-streaming.md
```

## Template

See `_templates/error-pattern.md`.

## Typical authors

Every agent. Whoever found and fixed the bug writes the entry.

## Cadence

- Add: immediately after a fix lands, while context is fresh — do not defer
- Read: at the start of any debugging session, recall returns these first
- Update: only to add `related:` links when a similar incident appears
