#!/usr/bin/env bash
# sanitize-check.sh — fail if any private-team identifier leaked into the public distro.
#
# Run before `git commit -a` and as a pre-publish gate.
# Exits non-zero on any match.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Forbidden terms (regex, case-insensitive). Add new ones at the end with a comment.
FORBIDDEN='(silvana|arthas|thrall|kaelthas|garrosh|illidan|orgrimmar|dashi-gbrain|dashibrain|orgrimmar_brain|mrqwwiwi|@dashieshiev|@fridayhumanbot|@kaelthasproducerbot|@garroshsalebot|@Illidandevopsbot|65\.109\.137\.239|100\.65\.239\.12|100\.104\.191\.127|213\.171\.6\.132|mcp\.orgrimmar\.xyz|task\.orgrimmar\.xyz|edgelab\.su|/opt/dashi-gbrain|/home/openclaw|MCP_FALLBACK_TOKEN|FALLBACK_AGENT)'

# Excluded paths (we don't scan these)
EXCLUDES=(
  --exclude-dir=.git
  --exclude-dir=.venv
  --exclude-dir=__pycache__
  --exclude-dir=node_modules
  --exclude-dir=sources
  --exclude=*.pyc
  --exclude=*.bak
  --exclude=sanitize-check.sh
)

if ! command -v grep >/dev/null 2>&1; then
  echo "[sanitize] grep not installed" >&2
  exit 2
fi

matches="$(grep -REinI "${EXCLUDES[@]}" "$FORBIDDEN" . 2>/dev/null || true)"

if [ -n "$matches" ]; then
  printf 'sanitize-check FAILED — forbidden terms present:\n\n%s\n\n' "$matches" >&2
  printf 'Patterns checked: %s\n' "$FORBIDDEN" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Legacy env-var name check — catches re-introduction of pre-canonical names.
# Canonical names: PG_DATABASE, PG_PASSWORD, MCP_MEMORY_PORT, MCP_RECALL_PORT,
# MCP_SWARM_PORT, VAULT_ROOT.
# -----------------------------------------------------------------------------

# The rule definition itself contains the names it forbids; build it from
# components so the file is allowed to mention them.
_LEG1="PG_DB"
_LEG2="MCP_PORT_MEMORY"
_LEG3="MCP_PORT_RECALL"
_LEG4="MCP_PORT_SWARM"
_LEG5="VAULT_DIR"
LEGACY_ENV="\\b(${_LEG1}|${_LEG2}|${_LEG3}|${_LEG4}|${_LEG5})\\b"

legacy_matches="$(grep -REnI "${EXCLUDES[@]}" \
  --include='*.py' --include='*.sh' --include='*.template' \
  --include='*.example' --include='*.toml' --include='*.md' \
  "$LEGACY_ENV" . 2>/dev/null || true)"

# Strip self-references (the rule definition lives in this script).
legacy_matches="$(printf '%s' "$legacy_matches" | grep -v '^\./scripts/sanitize-check\.sh' || true)"
# Allow a single documented alias line in .env.example (none currently).
legacy_matches="$(printf '%s' "$legacy_matches" | grep -v '^\./\.env\.example.*# legacy alias' || true)"

if [ -n "$legacy_matches" ]; then
  printf 'sanitize-check FAILED — legacy env-var names present:\n\n%s\n\n' "$legacy_matches" >&2
  printf 'Canonical names: PG_DATABASE, MCP_MEMORY_PORT, MCP_RECALL_PORT, MCP_SWARM_PORT, VAULT_ROOT\n' >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Generic secret detection — catch hardcoded API keys / tokens by shape.
# Patterns intentionally use shape, not vendor name, so this catches future
# providers too. Build from parts so the file itself doesn't trigger.
# -----------------------------------------------------------------------------
_S_GROQ='gsk_[A-Za-z0-9]{40,}'
_S_OPENAI='sk-[A-Za-z0-9]{40,}'
_S_STRIPE='sk_live_[A-Za-z0-9]{20,}'
_S_SLACK='xoxb-[0-9A-Za-z-]{40,}'
_S_GITHUB='ghp_[A-Za-z0-9]{30,}'
_S_GOOGLE='AIza[A-Za-z0-9_-]{30,}'
_S_AWS='AKIA[A-Z0-9]{16}'
_S_TELEGRAM_BOT='[0-9]{8,12}:[A-Za-z0-9_-]{30,}'
SECRET_PATTERNS="(${_S_GROQ}|${_S_OPENAI}|${_S_STRIPE}|${_S_SLACK}|${_S_GITHUB}|${_S_GOOGLE}|${_S_AWS}|${_S_TELEGRAM_BOT})"

secret_matches="$(grep -REnI "${EXCLUDES[@]}" \
  --include='*.py' --include='*.sh' --include='*.template' \
  --include='*.example' --include='*.toml' --include='*.md' \
  --include='*.json' --include='*.yaml' --include='*.yml' \
  "$SECRET_PATTERNS" . 2>/dev/null || true)"

# Strip self-references (this script defines the patterns).
secret_matches="$(printf '%s' "$secret_matches" | grep -v '^\./scripts/sanitize-check\.sh' || true)"

if [ -n "$secret_matches" ]; then
  printf 'sanitize-check FAILED — hardcoded secret(s) detected:\n\n%s\n\n' "$secret_matches" >&2
  printf 'Replace with env var read or "${HOME}/.claude-lab/<agent>/secrets/<name>" file load.\n' >&2
  exit 1
fi

echo "sanitize-check OK — no forbidden terms found"
