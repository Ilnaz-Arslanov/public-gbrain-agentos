# 30-decisions

Immutable decision notes with full context. This is the **most-trafficked** folder.

## What goes here

- Any architectural, product, or operational choice that future-you (or another agent) might wonder «why did we do it this way?»
- The decision itself, the alternatives considered, the reasoning, and the consequences
- One file per decision — do NOT bundle multiple decisions into one file

## What does NOT go here

- Tactical implementation details — those go into runbooks
- Bug fixes — `80-error-patterns/`
- Pending proposals (only ship a decision file when the call is made)

## Why this folder exists

Memory fades. Context is the most expensive thing to reconstruct. When the team revisits «should we switch to Provider X?» six months later, the decision file says what was true at the time, what was tried, and why the current state was chosen. Reversing a decision is fine — but the new decision must reference and supersede the old one.

## Mutability rule

**Decisions are append-only.** If you change your mind:

1. Do NOT edit the old decision (except to add a `superseded_by: <new-path>` line at top).
2. Create a new decision file that references the old one in `related:` frontmatter and explicitly states «this supersedes …».
3. Commit both files in the same git commit.

## Frontmatter

```yaml
---
type: decision
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coordinator-agent
tags: [postgres, indexing]
related: ['30-decisions/2026-04-10-hnsw-default.md']
superseded_by: null
---
```

## Naming

```
2026-05-16-postgres-pgvector-index-strategy.md
2026-05-16-drop-fallback-token.md
2026-05-16-vault-folder-numbering.md
```

## Template

See `_templates/decision.md`.

## Typical authors

Every agent. Decisions of consequence should be written by whoever made the call, even if delegated.

## Cadence

- Add: any time a non-trivial choice is made
- Read: before reopening any settled question
- Update: only to add `superseded_by` pointer or correct a factual error (date, link)
