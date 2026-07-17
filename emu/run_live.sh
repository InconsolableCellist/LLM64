#!/usr/bin/env bash
# Interactive live session: proxy (real API from config.toml) + VICE TUI.
# Build first: make client-tui-direct

set -euo pipefail
cd "$(dirname "$0")/.."

# Absolute path: the proxy is launched from inside c64llm_proxy/, so a
# relative venv path would resolve to c64llm_proxy/c64llm_proxy/...
PY="$PWD/c64llm_proxy/.venv/bin/python"
if ! "$PY" -c 'import httpx' 2>/dev/null; then
  PY=python3
  if ! "$PY" -c 'import httpx' 2>/dev/null; then
    echo "No Python with httpx found. Rebuild the proxy venv:" >&2
    echo "  cd c64llm_proxy && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
fi

(cd c64llm_proxy && exec "$PY" -m src.main --host 127.0.0.1 --port 6400) &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT

# Wait for the proxy to listen
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/6400) 2>/dev/null; then exec 3>&- 3<&-; break; fi
  sleep 0.2
done

exec_emu() { ./emu/run_emu.sh direct 127.0.0.1:6400; }
exec_emu
