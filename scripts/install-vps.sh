#!/usr/bin/env bash
# install-vps.sh — driver that runs install.sh on a remote Ubuntu 22.04 VPS.
#
# Usage:
#   bash scripts/install-vps.sh user@host [--key /path/to/key]
#
# Steps:
#   1. Rsync this repo (minus dev artifacts) to /tmp/gbrain-install on the VPS
#   2. Optionally copy local .env to the remote staging dir
#   3. Run sudo bash /tmp/gbrain-install/scripts/install.sh on the VPS
#   4. Stream output back to the caller
#
# Prerequisites on the local machine: rsync, ssh.
# Prerequisites on the remote: ssh access with sudo, Ubuntu 22.04.

set -euo pipefail

if [ $# -lt 1 ]; then
  cat <<EOF >&2
Usage: $0 user@host [--key /path/to/key]

Examples:
  bash scripts/install-vps.sh root@198.51.100.42
  bash scripts/install-vps.sh ubuntu@host --key ~/.ssh/gbrain.pem
EOF
  exit 2
fi

REMOTE="$1"
shift

SSH_KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --key)
      SSH_KEY="$2"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
RSYNC_SSH=(ssh "${SSH_OPTS[@]}")
if [ -n "$SSH_KEY" ]; then
  SSH_OPTS+=(-i "$SSH_KEY")
  RSYNC_SSH=(ssh "${SSH_OPTS[@]}" -i "$SSH_KEY")
fi

log() { printf '[install-vps %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

log "remote: $REMOTE"
log "repo:   $REPO_ROOT"

# Ensure remote staging dir exists
ssh "${SSH_OPTS[@]}" "$REMOTE" 'mkdir -p /tmp/gbrain-install && sudo -n true' \
  >/dev/null 2>&1 || {
  echo "ERROR: cannot connect or sudo on $REMOTE" >&2
  echo "Make sure ssh works and the remote user can sudo without prompting (or run interactively)." >&2
  exit 1
}

log "rsync repo → $REMOTE:/tmp/gbrain-install"

rsync -az --delete \
  -e "${RSYNC_SSH[*]}" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'secrets/' \
  --exclude '.env' \
  "$REPO_ROOT/" "$REMOTE:/tmp/gbrain-install/"

# Push .env if it exists locally — operator decides whether to keep it
if [ -f "$REPO_ROOT/.env" ]; then
  log "copying .env to remote"
  scp "${SSH_OPTS[@]}" "$REPO_ROOT/.env" "$REMOTE:/tmp/gbrain-install/.env"
fi

log "running install.sh on remote"

ssh "${SSH_OPTS[@]}" -t "$REMOTE" '
  set -euo pipefail
  cd /tmp/gbrain-install
  chmod +x scripts/*.sh
  sudo -E bash scripts/install.sh
'

log "remote install finished"
