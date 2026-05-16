# 40-projects

Active initiatives. One folder per project, each containing a `PROJECT.md` and supporting artifacts.

## What goes here

```
40-projects/<project-slug>/
├── PROJECT.md              ← goals, milestones, status, links
├── decisions/              ← project-local decisions (optional)
├── notes/                  ← scratch, research, designs
└── artifacts/              ← exported docs, slides, screenshots
```

## What does NOT go here

- Personal scratch — keep agent-local
- Cross-project decisions — those go to root-level `30-decisions/`
- Stable knowledge — promote to `50-knowledge/` when the project ships

## PROJECT.md shape

```yaml
---
type: project
status: active | paused | done | abandoned
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coordinator-agent
tags: [edition-2, q3]
related: []
---

# Project name

## Goal
One sentence.

## Why now
One paragraph.

## Milestones
- [ ] M1 — ...
- [ ] M2 — ...

## Status
Current state, blockers, next action.

## Links
- Repo: ...
- Decisions: ...
- Owner: ...
```

## Typical authors

- `coordinator-agent` owns the index and status
- `coder-agent` updates technical notes and links commits
- Any agent can add to `notes/`

## Cadence

- Add: when a project starts
- Update: weekly minimum, ideally after every notable move
- Archive: rename status to `done` or `abandoned` (do not delete) — past projects are gold for recall

## Naming

Use a lowercase-hyphen slug that's stable for the life of the project:

```
40-projects/agentos-platform/
40-projects/inbox-agent-v2/
40-projects/migrate-to-pgvector/
```
