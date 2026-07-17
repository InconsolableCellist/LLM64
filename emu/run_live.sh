#!/usr/bin/env bash
# Interactive live session: proxy (real API from config.toml) + VICE TUI.
# Build first: make client-tui-direct

set -euo pipefail
cd "$(dirname "$0")/.."

PY=c64llm_proxy/.venv/bin/python
[ -x "$PY" ] || PY=python3

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
