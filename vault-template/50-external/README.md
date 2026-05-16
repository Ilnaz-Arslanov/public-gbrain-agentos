# 50-external

Scout sources. Inbox-agent writes here.

## What goes here

- Telegram channel forwards (text, with link back to original)
- Articles you save while reading
- Tweets / X threads worth keeping
- YouTube transcripts of relevant talks
- Newsletters you want indexed
- Anything from outside your stack that should be searchable later

## What does NOT go here

- Internal team chat — never index private conversations without explicit consent
- Documents you authored — they go to `50-knowledge/` or the appropriate project folder
- Secrets, credentials, paywalled content you don't have rights to

## Compiled flag

Frontmatter carries `compiled: false` when the inbox-agent first writes a raw forward. After the agent (or you) extract structured insights, set `compiled: true` and move the insights to `50-knowledge/` with a back-link. The raw forward stays here as evidence.

```yaml
---
type: external
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: inbox-agent
tags: [forward, telegram]
related: []
source_url: 'https://t.me/some-channel/12345'
source_kind: telegram-channel
compiled: false
---
```

## Typical authors

- `inbox-agent` — automated, writes every forward it processes
- You — manually, when you paste an article worth keeping

## Cadence

- Add: continuously, as scout sources flow in
- Compile: weekly batch, when inbox-agent runs its digest
- Prune: never delete; raw sources are evidence

## Naming

```
2026-05-16-forward-telegram-some-channel-12345.md
2026-05-16-article-some-blog-claude-skills-overview.md
2026-05-16-tweet-someone-on-mcp-spec.md
```

The file name should encode date + source kind + brief topic so it's grep-able without opening.

## Template

See `_templates/external.md`.
