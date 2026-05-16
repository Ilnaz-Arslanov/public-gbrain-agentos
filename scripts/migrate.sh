#!/usr/bin/env bash
# migrate.sh — apply SQL migrations in lexicographic order to the gbrain database.
#
# Idempotent: tracks applied migrations in a table `schema_migrations(filename, applied_at)`.
# Each migration runs in a single transaction. A migration is applied at most once.
#
# Usage:
#   bash scripts/migrate.sh          # apply pending
#   bash scripts/migrate.sh --list   # list pending

set -euo pipefail

log() { printf '[migrate %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '[migrate ERROR] %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi

: "${PG_DATABASE:=gbrain}"
: "${MIGRATIONS_DIR:=$REPO_ROOT/migrations}"

if [ ! -d "$MIGRATIONS_DIR" ]; then
  die "migrations dir not found: $MIGRATIONS_DIR"
fi

LIST_ONLY=false
if [ "${1:-}" = "--list" ]; then
  LIST_ONLY=true
fi

# Bootstrap the tracking table.
sudo -u postgres psql -d "$PG_DATABASE" -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
SQL

mapfile -t files < <(find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name '*.sql' | sort)

if [ "${#files[@]}" -eq 0 ]; then
  log "no migration files found in $MIGRATIONS_DIR"
  exit 0
fi

pending=()
for f in "${files[@]}"; do
  bn="$(basename "$f")"
  already=$(sudo -u postgres psql -d "$PG_DATABASE" -tAc "SELECT 1 FROM schema_migrations WHERE filename = '$bn';")
  if [ "$already" != "1" ]; then
    pending+=("$f")
  fi
done

if [ "${#pending[@]}" -eq 0 ]; then
  log "no pending migrations"
  exit 0
fi

if $LIST_ONLY; then
  log "pending migrations:"
  for f in "${pending[@]}"; do
    printf '  %s\n' "$(basename "$f")"
  done
  exit 0
fi

for f in "${pending[@]}"; do
  bn="$(basename "$f")"
  log "applying $bn"
  sudo -u postgres psql -d "$PG_DATABASE" -v ON_ERROR_STOP=1 --single-transaction \
    -f "$f" \
    -c "INSERT INTO schema_migrations (filename) VALUES ('$bn');"
  log "applied $bn"
done

log "all pending migrations applied"
