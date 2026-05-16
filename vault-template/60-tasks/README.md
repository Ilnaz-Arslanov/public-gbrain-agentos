# 60-tasks

Task-board snapshots and archived task histories.

## What goes here

- Daily / weekly task-board exports
- Per-agent task history (in / done / blocked)
- Sprint or milestone retrospectives
- Pointer back to live task-board state in your DB or Firebase

## What does NOT go here

- Live task state — that lives in your operational store (Postgres, Firebase, Linear, etc.)
- This folder is the **searchable archive**, not the source of truth for active tasks

## Why archive at all

The live store optimizes for «what should I work on right now?». The vault optimizes for «what did we ship in May?» Agents that ask «have we tried fixing this before?» benefit from indexed task history far more than from a live board.

## Typical authors

- `coordinator-agent`, owns the snapshot cadence
- A cron / scheduled job, if you automate it

## Cadence

- Daily: optional brief snapshot
- Weekly: structured rollup with completed / dropped / carried tasks
- Per milestone: full retrospective document

## Frontmatter

```yaml
---
type: tasks
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coordinator-agent
tags: [tasks, weekly]
related: []
period: 2026-W20
---
```

## Naming

```
weekly-2026-W20.md
milestone-q3-2026-retro.md
agent-coder-2026-05.md
```
