#!/usr/bin/env bash
# Steam Dashboard launcher (Git Bash / Linux / macOS).
# Usage: ./start-dashboard.sh [--no-browser]
set -uo pipefail

cd "$(dirname "$0")"

PY=""
for c in py python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] Python was not found on PATH. Install Python 3 and try again." >&2
    exit 1
fi

# Read the configured port from the settings DB; fall back to 8081.
PORT=8081
if [ -f steam_dashboard.db ]; then
    detected=$("$PY" -c "import sqlite3,json;r=sqlite3.connect('steam_dashboard.db').execute('SELECT value FROM settings WHERE key=?',('dashboard',)).fetchone();print((json.loads(r[0]) if r else {}).get('port',8081))" 2>/dev/null)
    case "$detected" in
        ''|*[!0-9]*) ;;
        *) PORT="$detected" ;;
    esac
fi

URL="http://localhost:$PORT"

open_browser() {
    sleep 3
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    elif command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$URL"
    fi
}

if [ "${1:-}" = "--no-browser" ]; then
    echo "[INFO] Browser auto-open disabled."
else
    open_browser >/dev/null 2>&1 &
fi

echo "[INFO] Starting Steam Dashboard on $URL ... (Ctrl+C to stop)"
exec "$PY" dashboard.py
