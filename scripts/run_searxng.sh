#!/bin/sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/vendor/searxng"
VENV="$ROOT/.searxng-venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -r requirements.txt
fi
export SEARXNG_SETTINGS_PATH="$ROOT/mcp/searxng/settings.yml"
exec "$VENV/bin/python" -m searx.webapp
