#!/usr/bin/env bash
# CC-TG launcher — uses dedicated venv (httpx + telegramify-markdown)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"

# Fallback to system python if venv missing
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

command -v "$PYTHON" >/dev/null 2>&1 || { echo "Python not found: $PYTHON" >&2; exit 1; }
"$PYTHON" -c "import httpx, telegramify_markdown" 2>/dev/null || {
    echo "Deps missing. Run: uv pip install --python $DIR/.venv/bin/python httpx telegramify-markdown" >&2
    exit 1
}
[ -f "$DIR/config.json" ] || { echo "config.json not found in $DIR" >&2; exit 1; }
[ -f "$DIR/cc_tg.py" ] || { echo "cc_tg.py not found in $DIR" >&2; exit 1; }

echo "🤖 Starting CC-TG..."
echo "   Config : $DIR/config.json"
echo "   Python : $PYTHON"
echo "   Logs   : $DIR/logs/bot.log"
echo ""

exec "$PYTHON" "$DIR/cc_tg.py" "$@"
