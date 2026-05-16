#!/usr/bin/env bash
set -euo pipefail

# inbox-agent compile cron: every 6h, summarize uncompiled raw/ items with Sonnet.
# Walks ${INBOX_AGENT_HOME}/raw/, finds files with `compiled: false`,
# writes compiled version to ${INBOX_AGENT_HOME}/compiled/<type>/<slug>.md,
# flips raw frontmatter to `compiled: true`.

: "${INBOX_AGENT_HOME:?INBOX_AGENT_HOME must be set, e.g. ~/.claude-lab/inbox-agent}"
: "${CLAUDE_BIN:=$HOME/.local/bin/claude}"

RAW_DIR="${INBOX_AGENT_HOME}/raw"
COMPILED_DIR="${INBOX_AGENT_HOME}/compiled"
LOG="${INBOX_AGENT_HOME}/logs/compile.log"
PROMPT_FILE="${INBOX_AGENT_HOME}/prompts/compile.prompt.md"
LOCK_FILE="${INBOX_AGENT_HOME}/logs/compile.lock"

mkdir -p "$COMPILED_DIR" "$(dirname "$LOG")"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# Single-instance guard: cron may overlap if Sonnet calls are slow.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "compile already running, skipping"
    exit 0
fi

[ ! -d "$RAW_DIR" ] && { log "no raw dir, nothing to do"; exit 0; }
[ ! -x "$CLAUDE_BIN" ] && { log "claude CLI not found at $CLAUDE_BIN"; exit 1; }

# Default inline prompt if file is missing.
DEFAULT_PROMPT='You are processing an inbox item forwarded to an AI agent.
Below is the raw material. Produce:

1. A 1-2 sentence TL;DR
2. 3-5 key takeaways (bullet points)
3. Suggested category (one of: tutorial, opinion, news, tool, reference, case_study)
4. 3-5 relevant tags (lowercase, hyphenated)

Format as markdown. No preamble.'

if [ -f "$PROMPT_FILE" ]; then
    BASE_PROMPT=$(cat "$PROMPT_FILE")
else
    BASE_PROMPT="$DEFAULT_PROMPT"
fi

PROCESSED=0
FAILED=0

while IFS= read -r FILE; do
    [ -z "$FILE" ] && continue
    [ ! -f "$FILE" ] && continue

    SLUG=$(basename "$FILE" .md)
    TYPE=$(grep "^type:" "$FILE" | head -1 | awk '{print $2}')
    URL=$(grep "^source:" "$FILE" | head -1 | sed 's/^source: //')
    TAGS_LINE=$(grep "^tags:" "$FILE" | head -1)

    [ -z "$TYPE" ] && TYPE="text"

    PROMPT="${BASE_PROMPT}

--- RAW MATERIAL ---
$(cat "$FILE")"

    RESULT=$(echo "$PROMPT" | timeout 90 "$CLAUDE_BIN" --model sonnet --print --output-format text 2>>"$LOG") || {
        log "compile failed for $FILE"
        FAILED=$((FAILED + 1))
        continue
    }

    if [ -z "$RESULT" ]; then
        log "empty result for $FILE"
        FAILED=$((FAILED + 1))
        continue
    fi

    OUTDIR="$COMPILED_DIR/$TYPE"
    mkdir -p "$OUTDIR"
    OUTFILE="$OUTDIR/${SLUG}.md"

    {
        echo "---"
        echo "source: $URL"
        echo "type: $TYPE"
        echo "compiled: true"
        echo "compiled_at: $(date -u +%FT%TZ)"
        [ -n "$TAGS_LINE" ] && echo "$TAGS_LINE"
        echo "---"
        echo ""
        echo "$RESULT"
        echo ""
        echo "---"
        echo ""
        echo "## Raw material"
        echo ""
        # strip ONLY the opening frontmatter block. State: count first 2 ^---$
        # to identify frontmatter boundary; once past, print everything
        # (including any subsequent --- in body).
        awk 'BEGIN{n=0; past=0} {
            if (past) { print; next }
            if ($0 == "---") { n++; if (n>=2) past=1; next }
        }' "$FILE"
    } > "$OUTFILE"

    # Flip flag in raw file. sed -i differs on BSD vs GNU; use portable form.
    if sed --version >/dev/null 2>&1; then
        sed -i 's/^compiled: false/compiled: true/' "$FILE"
    else
        sed -i '' 's/^compiled: false/compiled: true/' "$FILE"
    fi

    log "compiled $FILE -> $OUTFILE"
    PROCESSED=$((PROCESSED + 1))

    # Optional: write compiled note to shared gbrain under 50-knowledge/<type>/.
    # Disabled by default — your inbox-agent has scope 50-external; widen its
    # token if you want this. Leaving the hook here for reference.
    # See inbox-agent/README.md.

done < <(grep -lR "^compiled: false" "$RAW_DIR" 2>/dev/null || true)

log "run done: processed=$PROCESSED failed=$FAILED"
exit 0
