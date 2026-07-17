#!/usr/bin/env bash
# One-stop launcher for the C64 LLM stack.
#
#   ./run.sh proxy       start the proxy in the foreground (Ctrl-C stops)
#   ./run.sh proxy-bg    start the proxy in the background (log: proxy-live.log)
#   ./run.sh c64         build 40-col client, deploy to the C64U and run it
#   ./run.sh c64-80      same, 80-column build
#   ./run.sh emu         local VICE session against the running proxy
#   ./run.sh stop        stop a background proxy
#   ./run.sh status      show proxy + C64 connection state

set -euo pipefail
cd "$(dirname "$0")"

PY=c64llm_proxy/.venv/bin/python
[ -x "$PY" ] || PY=python3

proxy_running() { ss -tln 2>/dev/null | grep -q ":6400 "; }

case "${1:-help}" in
  proxy)
    cd c64llm_proxy && exec "${PY#c64llm_proxy/}" -m src.main --host 0.0.0.0 --port 6400
    ;;
  proxy-bg)
    proxy_running && { echo "proxy already running on :6400"; exit 0; }
    (cd c64llm_proxy && setsid nohup "${PY#c64llm_proxy/}" -m src.main \
        --host 0.0.0.0 --port 6400 > proxy-live.log 2>&1 < /dev/null &)
    sleep 2
    proxy_running && echo "proxy started on :6400 (log: c64llm_proxy/proxy-live.log)" \
                  || { echo "proxy failed to start - see c64llm_proxy/proxy-live.log"; exit 1; }
    ;;
  c64)
    proxy_running || "$0" proxy-bg
    make deploy-c64u
    ;;
  c64-80)
    proxy_running || "$0" proxy-bg
    make deploy-c64u-80
    ;;
  emu)
    proxy_running || "$0" proxy-bg
    make client-tui-direct
    ./emu/run_emu.sh direct 127.0.0.1:6400
    ;;
  stop)
    pkill -f "src.main --host" && echo "proxy stopped" || echo "no proxy running"
    ;;
  status)
    proxy_running && echo "proxy: running on :6400" || echo "proxy: not running"
    ss -tn state established 2>/dev/null | grep ":6400" | grep -q "192.168.1.64" \
        && echo "c64u: connected" || echo "c64u: not connected"
    ;;
  *)
    grep '^#   ' "$0" | sed 's/^#   //'
    ;;
esac
