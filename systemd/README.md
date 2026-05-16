# systemd unit templates

Four service unit templates that `scripts/install.sh` renders and installs to `/etc/systemd/system/`:

| Template | Installed name | Purpose |
|---|---|---|
| `memory-mcp.service.template` | `gbrain-memory-mcp.service` | Write-side MCP (port 8767) |
| `recall-mcp.service.template` | `gbrain-recall-mcp.service` | Read-side MCP (port 8768) |
| `swarm-mcp.service.template` | `gbrain-swarm-mcp.service` | Inter-agent event bus (port 8766) |
| `ingest-worker.service.template` | `gbrain-ingest-worker.service` | Vault file indexer |

## Placeholders

Templates use Jinja-style `{{NAME}}` placeholders that `install.sh` replaces via `sed`:

| Placeholder | Default | Description |
|---|---|---|
| `{{INSTALL_DIR}}` | `/opt/gbrain` | Where the repo + venv live |
| `{{SERVICE_USER}}` | `gbrain` | System user that runs the services |
| `{{ETC_DIR}}` | `/etc/gbrain` | Where `secrets.env` lives (mode 0600, owned by SERVICE_USER) |
| `{{LOG_DIR}}` | `/var/log/gbrain` | Log directory (read-writable by service) |
| `{{STATE_DIR}}` | `/var/lib/gbrain` | Mutable state (read-writable by service) |

All defaults come from `.env`. Override any of them before running `install.sh`.

## What `install.sh` does

```bash
for tpl in systemd/*.service.template; do
  out_name="$(basename "$tpl" .service.template)"
  rendered="/etc/systemd/system/gbrain-${out_name}.service"
  sed \
    -e "s|{{INSTALL_DIR}}|${INSTALL_DIR}|g" \
    -e "s|{{SERVICE_USER}}|${SERVICE_USER}|g" \
    -e "s|{{ETC_DIR}}|${ETC_DIR}|g" \
    -e "s|{{LOG_DIR}}|${LOG_DIR}|g" \
    -e "s|{{STATE_DIR}}|${STATE_DIR}|g" \
    "$tpl" | sudo tee "$rendered" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp gbrain-ingest-worker
```

## Hardening notes

All four units run with a tight sandbox:

- `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=yes` — minimum filesystem reach
- `ReadWritePaths` allow-list only the vault + log + state dirs
- `RestrictAddressFamilies` blocks raw sockets
- `MemoryDenyWriteExecute` for MCP services (workers may JIT, so it's relaxed there)
- `SystemCallFilter=@system-service` deny dangerous syscalls
- `MemoryMax` caps memory: 256M for MCP services, 1G for the ingest worker (embeddings are heavier)

If a service refuses to start with a syscall error, check `journalctl -u gbrain-<name>-mcp` for the offending syscall and consider whether to relax the filter or fix the upstream call.

## Verification

After install:

```bash
systemctl status gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp gbrain-ingest-worker
journalctl -u gbrain-memory-mcp -n 50 --no-pager
ss -tlnp | grep -E '876[678]'
```

You should see three sockets listening on 8766 / 8767 / 8768, all owned by `python` processes under the `gbrain` user.

## Troubleshooting

- **Service flaps on start:** check `EnvironmentFile=` path exists and is readable by the service user.
- **Permission denied on vault:** `chown -R gbrain:gbrain {{INSTALL_DIR}}/vault` and confirm `ReadWritePaths=` lists it.
- **Port in use:** another process is bound; `ss -tlnp | grep <port>` to find it.
- **Memory killed:** raise `MemoryMax=` if your dataset grew.
