# 90-inbox

Handoffs between agents. State-machine messages.

## What goes here

- One-shot work handoffs from one agent to another («coder-agent: please rebuild X»)
- Async notifications that should be persisted (not just runtime)
- Cross-agent context drops at session boundary
- Pending review items («reviewer-agent: please look at PR #42»)

## What does NOT go here

- Real-time messaging — use the swarm-mcp `notify` tool for live delivery
- Long-term knowledge — `50-knowledge/`
- Decisions — `30-decisions/`

## How it interacts with swarm-mcp

`90-inbox/` is the **durable** layer. `swarm_mcp` is the **delivery** layer. When you call `swarm.notify(to_agent='X', payload=...)`:

1. Worker writes the payload to `90-inbox/<recipient>/YYYY-MM-DD-<task-id>.md`
2. Worker fires a webhook to the recipient's runtime (Telegram, Discord, etc.)
3. Recipient agent reads the inbox file when it boots or polls

The webhook is the «doorbell»; the inbox file is the «letter on the desk». If delivery fails (webhook timeout, recipient offline), the letter is still there for the next session.

## Folder convention

```
90-inbox/
├── coordinator-agent/
├── coder-agent/
├── inbox-agent/
├── marketer-agent/
├── reviewer-agent/
└── sales-agent/
```

Each subfolder is the destination agent's queue. Other agents write here, the owner agent reads + acks.

## Ack workflow

When the recipient processes the message:

1. Read the file
2. Take the action it requests
3. Either:
   - **Done:** rename the file to `done-YYYY-MM-DD-<task-id>.md` (keep audit trail)
   - **Escalate:** add a reply note in the same file, change `status: escalated` in frontmatter
   - **Drop:** rename to `dropped-YYYY-MM-DD-<task-id>.md` with reason in frontmatter

Never delete inbox entries; the audit trail is what makes the system trustworthy.

## Frontmatter

```yaml
---
type: inbox
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coordinator-agent
to_agent: coder-agent
tags: [handoff]
related: []
task_id: 'agent::coder-agent::abc123def456'
status: pending | done | escalated | dropped
---
```

## Naming

```
2026-05-16-abc123def456.md
done-2026-05-16-abc123def456.md
```

The task ID is opaque; use a short hex or UUID. Keep filename grep-able.

## Typical authors

Every agent writes. Each agent reads only its own subfolder.

## Cadence

- Add: any time you want to hand off work asynchronously
- Read: at session start, after every wake-up trigger
- Archive: monthly, move processed entries to `90-inbox/_archive/YYYY-MM/` to keep current folder lean
