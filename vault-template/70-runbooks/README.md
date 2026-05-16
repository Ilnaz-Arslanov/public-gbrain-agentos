# 70-runbooks

How-to playbooks: deploy, rollback, incident response, recurring operations.

## What goes here

- Step-by-step procedures that another agent (or future-you) can follow blind
- Deploy runbooks: per-service, per-environment
- Rollback procedures with verification steps
- Incident response checklists (general — specific incidents go to `80-error-patterns/`)
- Recurring chores: rotate credentials, refresh certificates, vacuum DB
- Onboarding runbooks for new agents joining the team

## What does NOT go here

- Decisions about how to deploy — `30-decisions/`
- One-off incidents — `80-error-patterns/`
- Background concepts — `50-knowledge/`

## What a good runbook looks like

1. **Preconditions** — what must be true before you start (credentials available, off-peak, etc.)
2. **Steps** — numbered, with exact commands, copy-pasteable
3. **Verification** — how do you know each step worked? Output to expect
4. **Rollback** — if step N fails, do this
5. **Notes** — non-obvious gotchas the author learned the hard way

Verification is non-negotiable. A runbook without verification is a hope, not a procedure.

## Typical authors

- `coder-agent`, after shipping anything non-trivial
- `reviewer-agent`, when standardizing a pattern across services
- You, when you've done the same thing manually three times

## Cadence

- Add: after the third manual repetition of a procedure
- Update: every time you actually run it and find a step is wrong / missing
- Retire: when the underlying procedure no longer applies (move to `archive/` subdirectory rather than delete)

## Frontmatter

```yaml
---
type: runbook
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coder-agent
tags: [deploy, rollback]
related: []
last_executed: '2026-05-16T11:00:00Z'
---
```

## Naming

```
deploy-memory-mcp.md
rollback-vault-corruption.md
rotate-agent-tokens.md
new-agent-onboarding.md
```

## Template

See `_templates/runbook.md`.
