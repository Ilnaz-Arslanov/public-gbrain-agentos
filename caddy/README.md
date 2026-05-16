# Caddy gateway

`Caddyfile.template` is a path-based reverse proxy that fronts the three MCP services with TLS:

```
mcp.<your-domain>/memory/*  → 127.0.0.1:8767
mcp.<your-domain>/recall/*  → 127.0.0.1:8768
mcp.<your-domain>/swarm/*   → 127.0.0.1:8766
```

## Prerequisites

1. **DNS A record.** `mcp.<your-domain>` must resolve to your VPS public IP.
2. **Port 80 reachable.** Let's Encrypt uses HTTP-01 challenge by default. Open port 80 in your firewall (`ufw allow 80/tcp`) and any cloud provider security group.
3. **Port 443 reachable.** Open for HTTPS traffic.
4. **No CDN proxy in front.** If you use Cloudflare, set the DNS record to **DNS-only** (grey cloud). CDN proxies buffer SSE, which breaks MCP streamable-http transport. If you want DDoS protection, do it at the firewall layer or use a CDN with confirmed unbuffered streaming support.

## Installation

`scripts/install.sh` handles rendering and installation automatically when you set `DOMAIN` and `ACME_EMAIL` in `.env`. If you prefer to do it manually:

```bash
export DOMAIN=example.com
export ACME_EMAIL=you@example.com
envsubst < caddy/Caddyfile.template | sudo tee /etc/caddy/Caddyfile.d/gbrain.caddy
sudo systemctl reload caddy
```

Verify the cert was issued:

```bash
sudo journalctl -u caddy -n 50 --no-pager | grep -i 'certificate obtained'
curl -fsS https://mcp.$DOMAIN/  # should return "gbrain MCP gateway: ..."
```

## Alternative: Tailscale-only setup

If you do not want to expose MCP services to the public internet, skip Caddy entirely and bind the services to a Tailscale interface:

1. Install Tailscale on the VPS and your local agents.
2. Edit each `*.service.template` to set `MCP_HOST=<vps-tailscale-ip>` before install.
3. Point each agent's `.mcp.json` at `http://<vps-tailscale-ip>:876X/mcp` over the Tailnet.

Trade-off: no TLS (Bearer auth still applies), no public access for third-party agents, but zero attack surface.

## Logging

Access logs land in `/var/log/caddy/gbrain.access.log` as JSON, with 50 MB rotation and 7-file retention. Tail them with:

```bash
sudo tail -f /var/log/caddy/gbrain.access.log | jq .
```

## SSE / streamable-http note

The `flush_interval -1` directive tells Caddy to flush every byte as it arrives from the upstream. Without this, MCP `tools/list` calls over `Accept: text/event-stream` will hang until the response is large enough to flush a buffer. The placeholder Caddy config sets this on all three routes.
