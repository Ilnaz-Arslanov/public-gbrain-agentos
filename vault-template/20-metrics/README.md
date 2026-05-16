# 20-metrics

Monitoring exports, KPI snapshots, dashboard captures.

## What goes here

- Weekly KPI snapshots (one file per week)
- Service health summaries from `health-watchdog.sh`
- Recall quality reports (precision / recall on a known query set)
- Inbox-agent throughput stats (forwards per day, classification accuracy)
- Database growth, vault size, embedding cache hit rate
- Cost reports (API spend per provider per week)

## What does NOT go here

- Raw logs — they live on disk under `/var/log/gbrain/`
- Live dashboards — these are point-in-time snapshots, not live data
- Alerts — those go through the watchdog to your operator channel

## Typical authors

- `inbox-agent`, when compiling daily digests
- `coordinator-agent`, weekly rollups

## Cadence

- Daily: lightweight snapshot from watchdog (optional, can be ephemeral)
- Weekly: structured rollup, append to `weekly-YYYY-Www.md`
- Monthly: KPI review with commentary

## Frontmatter

```yaml
---
type: metrics
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coordinator-agent
tags: [metrics, weekly]
related: []
period: 2026-W20
---
```

## Naming

```
weekly-2026-W20.md
monthly-2026-05.md
recall-quality-2026-05-16.md
costs-2026-05.md
```
