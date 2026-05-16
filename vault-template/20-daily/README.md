# 20-daily

Cross-agent daily timeline. Append-only narrative log of what happened.

## What goes here

- One file per day: `YYYY-MM-DD.md`
- Each agent appends their notable events under a `## <agent-name>` heading
- Brief: what was done, what blocked, what's next
- Cross-references to decisions, runbooks, error patterns created that day

## What does NOT go here

- Internal scratchpad — keep that in agent-local memory
- Detailed write-ups — link out to the proper folder
- Anything sensitive — daily logs are read by every agent

## Format

```markdown
---
type: daily
created: '2026-05-16T00:00:00Z'
updated: '2026-05-16T23:59:59Z'
agent: shared
tags: [daily]
related: []
---

# 2026-05-16

## coordinator-agent

- Reviewed weekly metrics, flagged drop in inbox-agent throughput
- Created decision `30-decisions/2026-05-16-add-rate-limit.md`

## coder-agent

- Shipped Caddy migration to `mcp.example.com`
- Smoke tests green; see `70-runbooks/2026-05-16-domain-migration.md`

## inbox-agent

- Processed 47 forwards, 3 flagged for owner review
- Daily digest sent at 21:00
```

## Typical authors

Every agent appends to the current day's file. If the file does not exist yet, create it.

## Cadence

- Read: at session start to see what other agents did
- Write: after any notable action
- Rotate: never delete; old days are searchable via `recall_mcp`
