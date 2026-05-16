#!/bin/bash
set -euo pipefail

# Groq Whisper API voice transcription
# Usage: transcribe.sh /path/to/file.ogg

FILE_PATH="${1:-}"

if [ -z "$FILE_PATH" ]; then
  echo "ERROR: No file path provided"
  echo "Usage: transcribe.sh /path/to/file.ogg"
  exit 1
fi

if [ ! -f "$FILE_PATH" ]; then
  echo "ERROR: File not found: $FILE_PATH"
  exit 1
fi

# Read Groq API key from env or ~/.claude-lab/<your-agent>/secrets/groq-api-key
if [ -n "${GROQ_API_KEY:-}" ]; then
  GROQ_KEY="$GROQ_API_KEY"
elif [ -f "${SHARED_SECRETS:-$HOME/.claude-lab/shared/secrets}/groq-api-key" ]; then
  GROQ_KEY="$(cat "${SHARED_SECRETS:-$HOME/.claude-lab/shared/secrets}/groq-api-key")"
else
  echo "ERROR: Groq API key not found. Set GROQ_API_KEY env or write key to ~/.claude-lab/shared/secrets/groq-api-key" >&2
  exit 1
fi

RESPONSE=$(curl -s -w "\n%{http_code}" \
  --max-time 30 \
  "https://api.groq.com/openai/v1/audio/transcriptions" \
  -H "Authorization: Bearer $GROQ_KEY" \
  -F "file=@$FILE_PATH" \
  -F "model=whisper-large-v3-turbo" \
  -F "response_format=text")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
  echo "Transcript: $BODY"
else
  echo "ERROR: Groq API returned HTTP $HTTP_CODE"
  echo "$BODY"
  exit 1
fi
