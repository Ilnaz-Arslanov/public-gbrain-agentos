---
name: gbrain-doctor
description: "Proactively diagnose an myfleet agent's gbrain MCP setup end-to-end: connectivity, identity, recall, swarm, hooks parity, webhooks, GitHub repo, MCP URL security, and skill install. Triggers: «gbrain-doctor», «gbrain health», «check gbrain», «gbrain MCP», «проверь gbrain», «диагностика gbrain», «не работает gbrain»."
---

# gbrain Doctor

Agent-facing safe diagnostic for an myfleet agent's gbrain MCP setup. Запускается с
машины самого агента, читает `.mcp.json`, бьёт по gbrain MCP endpoints через streamable-http
JSON-RPC и проверяет локальные hooks/webhooks/repo/skill install. Output редактируется и
безопасен для вставки в чат.

Это НЕ серверный VPS-доктор (`scripts/gbrain_doctor.py` в корне репо инспектит установку на
хосте). Этот скилл смотрит со стороны агента наружу.

## When to Use

- Когда просят проверить gbrain, MCP, hooks, webhooks, agent repo или cross-agent память.
- Перед тем как винить сервер: если агент не может recall, notify или получать задачи —
  сначала прогнать доктор, потом эскалировать.
- При жалобах «не работает gbrain», «recall молчит», «задачи не приходят», «hooks не сработали».

## Safety

- Output редактируется (Bearer / HMAC / `sk-*` / `password=` маскируются) — безопасно вставлять.
- Default run полностью read-only. Мутирующие MCP-tools (`memory.create_*`, `swarm.notify`,
  `tasks.agent_heartbeat`) НЕ вызываются.
- `--fix` allowlist только: `chmod +x` на hook/skill скрипты, symlink скилла в skills dir,
  `gh repo create --private` для per-agent repo. Каждый fix — с подтверждением на stderr.
- Никогда не пишет токены, не правит RED-файлы (CLAUDE.md / rules.md), не рестартит сервисы,
  не мутирует gbrain память по умолчанию.

## Quick Run

```bash
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent <agent-id>
```

- `--mcp-json PATH` — когда запускаешь вне workspace агента.
- `--group G6` — только hooks parity (или `--group G1,G2` и т.д.).
- `--json --quiet` — машинный вывод + код возврата.

```bash
# hooks only, без цвета
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent nova --group G6 --no-color

# машинный JSON
python3 skills/gbrain-doctor/scripts/gbrain_doctor.py --agent nova --json --quiet
```

## Reading Results

- `pass` — проверка зелёная.
- `warn` — не критично, но стоит глянуть (пустые данные, stale freshness, drift).
- `fail` — сломано, требует действия. Любой `fail` → exit code `1`.
- `skip` — проверка неприменима (опциональный `tasks` server отсутствует, write probe по
  политике read-only). `skip` не делает run красным.

Exit codes: `0` нет fail (warn/skip ок), `1` есть хотя бы один fail, `2` ошибка вызова/конфига
(битый `--group`, нечитаемый `--mcp-json`/`--expected-hooks`, Python <3.12).

Hooks parity (G6) критична: пропавший hook = тихая поломка фичи (память не пишется, snapshot
не делается). Не игнорировать G6 warn/fail.

## Groups

- `G1` — MCP config + connectivity (servers present, specs well-formed, `tools/list`, recall/swarm stats).
- `G2` — identity + token (no-Bearer denied 401/403, agent resolved, bearer consistency, redaction selftest).
- `G3` — swarm (recent acked ratio, pending depth, delivery lookup).
- `G4` — recall (recall probe, recent, reindex_check drift).
- `G5` — memory (write tools registered, schema readable, write probe = skip by policy).
- `G6` — hooks parity (manifest, settings layers, expected events, paths exist, executable, async, freshness).
- `G7` — webhooks (listener healthz, auth modes, service loaded, token file safe, delivery errors).
- `G8` — topology/security (URL topology, public auth enforced, backend ports closed, TLS, Cloudflare caveat).
- `G9` — GitHub (`gh` present+authed, per-agent repo exists private, repo status).
- `G10` — skill self-install (dir present, frontmatter valid, scripts executable, symlinked, references present).

## Fix Workflow

1. Прогнать один раз read-only: `gbrain_doctor.py --agent <id>`.
2. Если у fail есть safe autofix (C027/C046/C050/C051) — перезапустить с `--fix`,
   подтвердить каждый fix на stderr.
3. Перезапустить read-only для верификации. `--fix` не перепрогоняет проверки сам.

## Topology Note

Caddy+TLS, Tailscale (100.64/10) и Cloudflare Tunnel/proxy детектятся отдельно. Cloudflare
НЕ обязателен — proxied DNS может буферить SSE и ломать streamable-http. Если docs и
предпочтение принца расходятся — escalate выбор топологии, не навязывать Cloudflare.
Детали: `references/topology-caveats.md`.

## Manual Side-Effect Probes

Для опциональных мутирующих smoke-тестов (memory write idempotent probe, swarm self-notify
roundtrip) см. `references/manual-write-probes.md`. Эти пробы МУТИРУЮТ состояние и НЕ
запускаются доктором — только вручную под контролем оператора.

Подробности по группам, allowlist, troubleshooting — в `README.md`.
