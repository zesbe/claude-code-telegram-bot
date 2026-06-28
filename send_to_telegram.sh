#!/usr/bin/env bash
# send_to_telegram.sh — send a file to the current Telegram chat.
# Called by Claude Code via bash tool: ~/.cc-tg/send_to_telegram.sh <file> [caption]
#
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env vars
# (set by the bot before calling Claude Code).

set -euo pipefail

FILE="${1:?Usage: send_to_telegram.sh <file_path> [caption]}"
CAPTION="${2:-}"

if [ ! -f "$FILE" ]; then
    echo "❌ File not found: $FILE" >&2
    exit 1
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. Not running from bot?" >&2
    exit 1
fi

SIZE=$(stat -c%s "$FILE" 2>/dev/null || stat -f%z "$FILE" 2>/dev/null)
if [ "$SIZE" -gt 52428800 ]; then
    echo "❌ File too large ($SIZE bytes, max 50MB)" >&2
    exit 1
fi

BASENAME=$(basename "$FILE")

RESULT=$(curl -s -m 60 \
    -F "chat_id=$TELEGRAM_CHAT_ID" \
    -F "document=@$FILE" \
    -F "caption=$CAPTION" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument")

OK=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
if [ "$OK" = "True" ]; then
    echo "✅ Sent to Telegram: $BASENAME ($SIZE bytes)"
else
    echo "❌ Send failed: $RESULT" >&2
    exit 1
fi
