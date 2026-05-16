# 10-system

System configs, infra notes, environment topology.

## What goes here

- Server inventory: hostnames, IPs, what runs where
- Network topology: VPN, Tailscale, public endpoints
- DNS records that matter for the team's stack
- Service catalog: which systemd units exist on which host
- Credentials INDEX (never the secrets themselves — only pointers to where they live)
- Backup strategy: what is backed up, where, retention
- Disaster recovery procedures (high level — detailed steps in `70-runbooks/`)

## What does NOT go here

- Actual secrets, tokens, passwords — those go in `${INSTALL_DIR}/secrets/` (mode 0600) and a password manager
- Incident write-ups — `80-error-patterns/`
- One-off operational decisions — `30-decisions/`

## Typical authors

- `coder-agent`, when adding new infrastructure
- `reviewer-agent`, when auditing the stack

## Cadence

- Add: when new infrastructure is introduced
- Update: when topology changes (new server, retired service, DNS change)
- Read: before any cross-host operation

## Frontmatter

```yaml
---
type: system
created: '2026-05-16T10:00:00Z'
updated: '2026-05-16T10:00:00Z'
agent: coder-agent
tags: [infra, topology]
related: []
---
```

## Naming

```
servers.md
network-topology.md
dns.md
backups.md
2026-05-16-added-monitoring-host.md
```
