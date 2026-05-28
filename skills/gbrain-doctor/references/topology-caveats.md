# Topology caveats — gbrain MCP URL security (G8)

On-demand note for the `gbrain-doctor` G8 checks. It explains how the doctor
classifies an MCP endpoint's topology and what "secure" means for each. The
doctor is **topology-aware**: it asserts that whatever topology is in use is
configured correctly, rather than mandating one transport.

## What "secure" means per topology

| Topology | Detection signal | Secure when | Plain HTTP ok? |
|---|---|---|---|
| Local-only | host is `127.0.0.1` / `localhost` / `::1` | Bearer still enforced; same-host testing only | yes (loopback) |
| Tailscale | host/IP in `100.64.0.0/10` (CGNAT) | Bearer enforced; bound to the tailnet interface | yes (tailnet) |
| Caddy + TLS public domain | hostname on 443 with valid TLS | valid cert, no-Bearer returns 401/403, raw MCP ports 8766-8769 closed on the origin IP | no — must be HTTPS |
| Cloudflare-fronted / Tunnel | `server: cloudflare`, `cf-ray: <hex>-<IATA>`, or DNS in CF ranges | Bearer enforced **and** real streamable-http MCP calls succeed | no — must be HTTPS |
| Raw public IP (insecure) | host is a raw, non-private public IP literal | never — this is a failure | n/a |

Insecure verdicts the doctor flags as `fail`:

- MCP URL is a raw public backend IP on 443 or on the MCP ports.
- Raw backend MCP ports (8766-8769) reachable from the agent machine on the
  resolved public/origin IP.
- Any public endpoint returns HTTP 200 without a Bearer header.

## Cloudflare Tunnel vs proxied DNS — the SSE buffering caveat

The prince has asked to wrap cross-host MCP traffic via Cloudflare. There are
two very different Cloudflare mechanisms, and they behave differently for MCP
`streamable-http`:

- **Cloudflare Tunnel (`cloudflared`)** — an outbound tunnel from the origin
  to Cloudflare's edge. This can work with streaming if the edge does not
  buffer the response.
- **Cloudflare proxied DNS (orange cloud)** — the hostname's DNS record is
  proxied through Cloudflare. The repo `docs/security.md` warns that the
  Cloudflare proxy **buffers SSE / streaming responses**, which breaks
  `streamable-http` MCP transport. Use **DNS-only** (`proxied=false`) for MCP
  hostnames.

Because the SSE-buffering risk is real and the repo already standardized on
Caddy+TLS and Tailscale, the doctor does **not** mandate Cloudflare. When it
detects a Cloudflare-fronted endpoint:

- If real `tools/list` MCP calls succeed -> `warn` with the SSE caveat (it
  works now, but proxied DNS may start buffering and break streaming later).
- If MCP calls fail -> `fail` (the proxy is likely buffering SSE).

Escalate the canonical-topology choice to the prince when docs and preference
diverge; do not silently force Cloudflare.

## Origin verification limitation

A Cloudflare-fronted hostname hides the raw origin IP. The
`backend_ports_closed` check can only *prove* origin closure when the operator
supplies `GBRAIN_BACKEND_IP` (or the URL is already a raw IP). Without it the
doctor `warn`s instead of false-passing — it must not claim an origin is closed
when it cannot see the origin.

## Viewpoint limitation of socket scans

The backend port scan runs **from the agent machine**. A port that is "closed
from here" is **not** proof it is closed from the whole internet (different
network paths, firewalls, source-IP allowlists). The inverse is solid: an
**open** backend MCP port from the agent machine is definitely actionable
exposure. Messages say so explicitly.

## Cloudflare IP ranges note

`checks_local.py` hard-codes an IPv4 snapshot of Cloudflare ranges (as of
2026-05-28) used only as a soft DNS signal alongside `server`/`cf-ray`
headers. The authoritative live list is <https://www.cloudflare.com/ips-v4>
and the snapshot **will drift** — refresh it if CF detection misfires.
