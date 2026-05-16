#!/usr/bin/env bash
# shellcheck disable=SC2012
set -euo pipefail

# ============================================================
# install.sh -- One-click agent workspace setup
#
# Creates a complete Claude Code agent workspace at
#   ~/.claude-lab/<agent-id>/.claude/
# wired to a gbrain MCP server (memory / recall / swarm).
#
# Assumes you already deployed the gbrain server (see ../README.md and
# ../scripts/install-vps.sh) and issued an agent token with
# ../scripts/issue-agent-token.py.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="${SCRIPT_DIR}/templates"
SCRIPTS_DIR="${SCRIPT_DIR}/scripts"
HOOKS_DIR="${SCRIPT_DIR}/hooks"
LAB_DIR="${HOME}/.claude-lab"
GLOBAL_DIR="${HOME}/.claude"
DISTRO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARED_SKILLS_SRC="${DISTRO_ROOT}/skills"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[x]${NC} $1"; }
ask()   { echo -en "${CYAN}[?]${NC} $1: "; }

# Cross-platform sed in-place (macOS BSD vs GNU)
sed_i() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

# ============================================================
# Step 1: Prerequisites
# ============================================================

echo ""
echo "============================================"
echo "  Claude Code Agent -- Workspace Installer"
echo "============================================"
echo ""

if ! command -v claude &>/dev/null; then
    warn "Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code"
    warn "Continuing anyway (workspace will be ready when you install the CLI)."
fi

for cmd in jq python3 curl; do
    if ! command -v "$cmd" &>/dev/null; then
        warn "${cmd} not installed; some features (gbrain recall, JSON parsing) won't work until you install it."
    fi
done

if [ ! -d "$TEMPLATES_DIR" ]; then
    err "Templates directory not found: $TEMPLATES_DIR"
    err "Run this script from the cloned agent-template/ root."
    exit 1
fi

# ============================================================
# Step 2: Gather parameters
# ============================================================

echo "Answer a few questions to set up your agent."
echo ""

ask "Agent name (e.g. Homer, Jarvis, Friday)"
read -r AGENT_NAME
AGENT_NAME="${AGENT_NAME:-MyAgent}"
AGENT_ID=$(echo "$AGENT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')

if [[ ! "${AGENT_ID}" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; then
    err "Agent name must contain only letters, numbers, and hyphens (max 31 chars)"
    exit 1
fi

ask "Agent role (e.g. Coder, Coordinator, Research assistant)"
read -r AGENT_ROLE
AGENT_ROLE="${AGENT_ROLE:-Coder}"

ask "One-sentence role description"
read -r AGENT_ROLE_DESCRIPTION
AGENT_ROLE_DESCRIPTION="${AGENT_ROLE_DESCRIPTION:-Autonomous coding assistant. Writes code, reviews architecture, runs tests.}"

ask "Character traits (e.g. Pragmatic, calm, precise)"
read -r CHARACTER_TRAITS
CHARACTER_TRAITS="${CHARACTER_TRAITS:-Efficient, precise, proactive. Reports results, not process.}"

echo ""
echo "  Models: opus (code+review), sonnet (subagents+research)"
ask "Primary model [opus]"
read -r PRIMARY_MODEL
PRIMARY_MODEL="${PRIMARY_MODEL:-opus}"

ask "Research model [Perplexity Sonar]"
read -r RESEARCH_MODEL
RESEARCH_MODEL="${RESEARCH_MODEL:-Perplexity Sonar (web search only, no code)}"

ask "Max simultaneous subagents [5]"
read -r MAX_SUBAGENTS
MAX_SUBAGENTS="${MAX_SUBAGENTS:-5}"

echo ""
echo "--- Operator (you) ---"
ask "Your name"
read -r OPERATOR_NAME
OPERATOR_NAME="${OPERATOR_NAME:-Operator}"

ask "How agent should address you (e.g. Boss, Chief)"
read -r OPERATOR_ADDRESS
OPERATOR_ADDRESS="${OPERATOR_ADDRESS:-Boss}"

ask "Your timezone (e.g. UTC+3, America/New_York)"
read -r TIMEZONE
TIMEZONE="${TIMEZONE:-UTC}"

ask "Response language (e.g. English, Russian)"
read -r LANGUAGE
LANGUAGE="${LANGUAGE:-English}"

ask "Commit language (e.g. English, Russian)"
read -r COMMIT_LANGUAGE
COMMIT_LANGUAGE="${COMMIT_LANGUAGE:-English}"

ask "Red zone budget limit in USD [50]"
read -r BUDGET_LIMIT
BUDGET_LIMIT="${BUDGET_LIMIT:-50}"

ask "GitHub username (or skip)"
read -r GITHUB_USERNAME
GITHUB_USERNAME="${GITHUB_USERNAME:-your-username}"

echo ""
echo "--- gbrain MCP server ---"
ask "MCP host URL (e.g. https://mcp.example.com or http://<vps-ip>:8767)"
read -r MCP_HOST
MCP_HOST="${MCP_HOST:-https://mcp.example.com}"
MCP_HOST="${MCP_HOST%/}"  # strip trailing slash

ask "Agent bearer token (issued by scripts/issue-agent-token.py on the gbrain VPS)"
read -r AGENT_BEARER
AGENT_BEARER="${AGENT_BEARER:-CHANGE_ME}"

ask "Agent scopes [30-decisions,50-external,70-runbooks,90-inbox]"
read -r AGENT_SCOPES
AGENT_SCOPES="${AGENT_SCOPES:-30-decisions,50-external,70-runbooks,90-inbox}"

# ============================================================
# Step 3: Confirm
# ============================================================

echo ""
echo "============================================"
echo "  Setup Summary"
echo "============================================"
echo ""
echo "  Agent:       ${AGENT_NAME} (${AGENT_ID})"
echo "  Role:        ${AGENT_ROLE}"
echo "  Model:       ${PRIMARY_MODEL}"
echo "  Operator:    ${OPERATOR_NAME}"
echo "  Language:    ${LANGUAGE}"
echo "  Workspace:   ${LAB_DIR}/${AGENT_ID}/.claude/"
echo "  gbrain host: ${MCP_HOST}"
echo "  Scopes:      ${AGENT_SCOPES}"
echo ""
ask "Proceed? [Y/n]"
read -r CONFIRM
CONFIRM_LOWER=$(echo "$CONFIRM" | tr '[:upper:]' '[:lower:]')
if [[ "$CONFIRM_LOWER" == "n" ]]; then
    echo "Cancelled."
    exit 0
fi

# ============================================================
# Step 4: Directory structure
# ============================================================

WORKSPACE="${LAB_DIR}/${AGENT_ID}/.claude"
SHARED="${LAB_DIR}/shared"

log "Creating directory structure..."

mkdir -p "${WORKSPACE}/core/warm"
mkdir -p "${WORKSPACE}/core/hot/archive"
mkdir -p "${WORKSPACE}/core/hot/pre-compact"
mkdir -p "${WORKSPACE}/core/archive"
mkdir -p "${WORKSPACE}/tools"
mkdir -p "${WORKSPACE}/agents"
mkdir -p "${WORKSPACE}/scripts"
mkdir -p "${WORKSPACE}/hooks"
mkdir -p "${WORKSPACE}/logs"
mkdir -p "${SHARED}/secrets"
mkdir -p "${SHARED}/skills"
mkdir -p "${SHARED}/scripts"
mkdir -p "${GLOBAL_DIR}/rules"

echo "# Hot context -- last 10 entries" > "${WORKSPACE}/core/hot/handoff.md"

# ============================================================
# Step 5: Render templates (envsubst-style {{VAR}} and ${VAR})
# ============================================================

fill_template() {
    local src="$1"
    local dst="$2"

    if [ -f "$dst" ]; then
        warn "Skipping (exists): $dst"
        return
    fi

    cp "$src" "$dst"

    # {{VAR}} placeholders
    sed_i "s|{{AGENT_NAME}}|${AGENT_NAME}|g" "$dst"
    sed_i "s|{{AGENT_ID}}|${AGENT_ID}|g" "$dst"
    sed_i "s|{{AGENT_ROLE}}|${AGENT_ROLE}|g" "$dst"
    sed_i "s|{{AGENT_ROLE_DESCRIPTION}}|${AGENT_ROLE_DESCRIPTION}|g" "$dst"
    sed_i "s|{{CHARACTER_TRAITS}}|${CHARACTER_TRAITS}|g" "$dst"
    sed_i "s|{{PRIMARY_MODEL}}|${PRIMARY_MODEL}|g" "$dst"
    sed_i "s|{{RESEARCH_MODEL}}|${RESEARCH_MODEL}|g" "$dst"
    sed_i "s|{{MAX_SUBAGENTS}}|${MAX_SUBAGENTS}|g" "$dst"
    sed_i "s|{{OPERATOR_NAME}}|${OPERATOR_NAME}|g" "$dst"
    sed_i "s|{{OPERATOR_ADDRESS}}|${OPERATOR_ADDRESS}|g" "$dst"
    sed_i "s|{{TIMEZONE}}|${TIMEZONE}|g" "$dst"
    sed_i "s|{{LANGUAGE}}|${LANGUAGE}|g" "$dst"
    sed_i "s|{{COMMIT_LANGUAGE}}|${COMMIT_LANGUAGE}|g" "$dst"
    sed_i "s|{{BUDGET_LIMIT}}|${BUDGET_LIMIT}|g" "$dst"
    sed_i "s|{{GITHUB_USERNAME}}|${GITHUB_USERNAME}|g" "$dst"
    sed_i "s|{{INSTALL_DATE}}|$(date -u +%Y-%m-%d)|g" "$dst"

    # ${VAR} placeholders (for .mcp.json template and tools.md, settings.json)
    sed_i "s|\${MCP_HOST}|${MCP_HOST}|g" "$dst"
    sed_i "s|\${AGENT_BEARER}|${AGENT_BEARER}|g" "$dst"

    # Sweep any unsubstituted {{TODO}} placeholders
    sed_i 's|{{[A-Z_0-9]*}}|TODO: fill in|g' "$dst"

    log "Created: $dst"
}

log "Filling templates..."

fill_template "${TEMPLATES_DIR}/CLAUDE.md.template"    "${WORKSPACE}/CLAUDE.md"
fill_template "${TEMPLATES_DIR}/agents.md.template"    "${WORKSPACE}/core/AGENTS.md"
fill_template "${TEMPLATES_DIR}/USER.md.template"      "${WORKSPACE}/core/USER.md"
fill_template "${TEMPLATES_DIR}/rules.md.template"     "${WORKSPACE}/core/rules.md"
fill_template "${TEMPLATES_DIR}/tools.md.template"     "${WORKSPACE}/tools/TOOLS.md"
fill_template "${TEMPLATES_DIR}/decisions.md.template" "${WORKSPACE}/core/warm/decisions.md"
fill_template "${TEMPLATES_DIR}/recent.md.template"    "${WORKSPACE}/core/hot/recent.md"
fill_template "${TEMPLATES_DIR}/MEMORY.md.template"    "${WORKSPACE}/core/MEMORY.md"
fill_template "${TEMPLATES_DIR}/LEARNINGS.md.template" "${WORKSPACE}/core/LEARNINGS.md"
fill_template "${TEMPLATES_DIR}/mcp.json.template"     "${WORKSPACE}/.mcp.json"
fill_template "${TEMPLATES_DIR}/settings.json.template" "${WORKSPACE}/settings.json"

# Global ~/.claude/CLAUDE.md only if missing
fill_template "${TEMPLATES_DIR}/global-CLAUDE.md.template" "${GLOBAL_DIR}/CLAUDE.md"

# ============================================================
# Step 6: Copy scripts and hooks (not symlinked, so each agent owns them)
# ============================================================

log "Copying memory-management scripts..."
for script in trim-hot.sh compress-warm.sh rotate-warm.sh memory-rotate.sh gbrain-recall-on-start.sh; do
    if [ -f "${SCRIPTS_DIR}/${script}" ]; then
        if [ ! -f "${WORKSPACE}/scripts/${script}" ]; then
            cp "${SCRIPTS_DIR}/${script}" "${WORKSPACE}/scripts/${script}"
            chmod +x "${WORKSPACE}/scripts/${script}"
            log "Copied: scripts/${script}"
        else
            warn "Skipping (exists): scripts/${script}"
        fi
    else
        warn "Script not found in distro: ${script}"
    fi
done

log "Copying hooks..."
for hook in session-start-hook.sh stop-hook.sh precompact-hook.sh; do
    if [ -f "${HOOKS_DIR}/${hook}" ]; then
        if [ ! -f "${WORKSPACE}/hooks/${hook}" ]; then
            cp "${HOOKS_DIR}/${hook}" "${WORKSPACE}/hooks/${hook}"
            chmod +x "${WORKSPACE}/hooks/${hook}"
            log "Copied: hooks/${hook}"
        else
            warn "Skipping (exists): hooks/${hook}"
        fi
    else
        warn "Hook not found in distro: ${hook}"
    fi
done

# ============================================================
# Step 7: Per-agent rc file with gbrain env (sourced before `claude`)
# ============================================================

AGENT_RC="${WORKSPACE}/agent.env"
if [ ! -f "$AGENT_RC" ]; then
    cat > "$AGENT_RC" <<RC
# ${AGENT_NAME} runtime env
# Source this before launching Claude Code:  source ${AGENT_RC}
export AGENT_ID="${AGENT_ID}"
export AGENT_WORKSPACE="${WORKSPACE}"
export MCP_HOST="${MCP_HOST}"
export AGENT_BEARER="${AGENT_BEARER}"
export AGENT_SCOPES="${AGENT_SCOPES}"
export SUMMARY_LANGUAGE="${LANGUAGE}"
RC
    chmod 600 "$AGENT_RC"
    log "Created: ${AGENT_RC} (chmod 600)"
fi

# ============================================================
# Step 8: Shared skills (optional, interactive)
# ============================================================

if [ -d "$SHARED_SKILLS_SRC" ]; then
    echo ""
    echo "--- Shared skills ---"
    echo "Available in ${SHARED_SKILLS_SRC}:"
    ls -1 "$SHARED_SKILLS_SRC" | sed 's/^/  - /'
    ask "Symlink all shared skills into the workspace? [Y/n]"
    read -r ENABLE_ALL
    ENABLE_ALL_LOWER=$(echo "$ENABLE_ALL" | tr '[:upper:]' '[:lower:]')
    if [[ "$ENABLE_ALL_LOWER" != "n" ]]; then
        if [ ! -L "${WORKSPACE}/skills" ] && [ ! -d "${WORKSPACE}/skills" ]; then
            ln -s "$SHARED_SKILLS_SRC" "${WORKSPACE}/skills"
            log "Symlinked: skills/ -> ${SHARED_SKILLS_SRC}"
        else
            warn "Skipping: skills/ already present"
        fi
    else
        warn "Skills not linked. Symlink later: ln -s ${SHARED_SKILLS_SRC} ${WORKSPACE}/skills"
    fi
else
    warn "Shared skills dir not found at ${SHARED_SKILLS_SRC}; skipping."
fi

# ============================================================
# Step 9: Language rules in ~/.claude/rules/
# ============================================================

log "Setting up language rules..."

for rule_file in bash.md python.md typescript.md; do
    if [ ! -f "${GLOBAL_DIR}/rules/${rule_file}" ]; then
        case "$rule_file" in
            bash.md)
                cat > "${GLOBAL_DIR}/rules/${rule_file}" << 'RULE'
# Bash rules
- set -euo pipefail at the start
- Quote variables: "$VAR"
- Check file existence before operations
- Log actions with echo
RULE
                ;;
            python.md)
                cat > "${GLOBAL_DIR}/rules/${rule_file}" << 'RULE'
# Python rules
- Type hints required for all functions
- Docstrings in Google style
- pathlib instead of os.path
- f-strings instead of .format()
- dataclasses or pydantic instead of dict
- Imports: stdlib, blank line, third-party, blank line, local
- Logging via logging module, not print
RULE
                ;;
            typescript.md)
                cat > "${GLOBAL_DIR}/rules/${rule_file}" << 'RULE'
# TypeScript rules
- strict: true always
- Never any, use unknown + type guard
- interface over type for objects
- Zod for runtime validation
- Barrel exports (index.ts) for modules
RULE
                ;;
        esac
        log "Created: ${GLOBAL_DIR}/rules/${rule_file}"
    else
        warn "Skipping (exists): ${GLOBAL_DIR}/rules/${rule_file}"
    fi
done

# ============================================================
# Step 10: Permissions on secrets dir
# ============================================================

chmod 700 "${SHARED}/secrets" 2>/dev/null || true
chmod 600 "${WORKSPACE}/.mcp.json" 2>/dev/null || true

# ============================================================
# Step 11: Summary
# ============================================================

echo ""
echo "============================================"
echo "  Setup Complete"
echo "============================================"
echo ""
echo "  Workspace:    ${WORKSPACE}/"
echo "  Shared:       ${SHARED}/"
echo "  Global:       ${GLOBAL_DIR}/"
echo ""

FILE_COUNT=$(find "${WORKSPACE}" -type f | wc -l | tr -d ' ')
echo "  ${FILE_COUNT} files in workspace"
echo ""
echo "  Next steps:"
echo ""
echo "    1. Review and customize identity files:"
echo "       - ${WORKSPACE}/CLAUDE.md (SOUL)"
echo "       - ${WORKSPACE}/core/AGENTS.md (models, team)"
echo "       - ${WORKSPACE}/core/USER.md (your profile)"
echo "       - ${WORKSPACE}/tools/TOOLS.md (servers, services)"
echo ""
echo "    2. Verify gbrain connectivity:"
echo "       source ${AGENT_RC}"
echo "       curl -sS -H \"Authorization: Bearer \${AGENT_BEARER}\" \\"
echo "            -H \"Accept: application/json, text/event-stream\" \\"
echo "            -H \"Content-Type: application/json\" \\"
echo "            -X POST \"\${MCP_HOST}/recall/mcp\" \\"
echo "            --data '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'"
echo ""
echo "    3. Launch agent (settings.json wires Stop/SessionStart/PreCompact hooks):"
echo "       source ${AGENT_RC} && claude --project ${WORKSPACE}"
echo ""
echo "    4. (Optional) Cron for memory rotation:"
echo "       crontab -e"
echo "       30 4 * * * AGENT_WORKSPACE=${WORKSPACE} bash ${WORKSPACE}/scripts/rotate-warm.sh"
echo "       0  5 * * * AGENT_WORKSPACE=${WORKSPACE} bash ${WORKSPACE}/scripts/trim-hot.sh"
echo "       0  6 * * * AGENT_WORKSPACE=${WORKSPACE} bash ${WORKSPACE}/scripts/compress-warm.sh"
echo "       0 21 * * * AGENT_WORKSPACE=${WORKSPACE} bash ${WORKSPACE}/scripts/memory-rotate.sh"
echo ""
echo "    5. (Optional) Add more agents:"
echo "       bash install.sh   # run again with a different agent name"
echo ""
echo "============================================"
echo "  Done."
echo "============================================"
