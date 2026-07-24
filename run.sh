#!/usr/bin/env bash
# One-stop launcher for the LLM64 stack.
#
#   ./run.sh proxy       start the proxy in the foreground (Ctrl-C stops)
#   ./run.sh proxy-bg    start the proxy in the background (log: proxy-live.log)
#   ./run.sh c64         build 40-col client, deploy to the C64U and run it
#   ./run.sh c64-80      same, 80-column build
#   ./run.sh install     build 80-col client and store it in the C64U's
#                        persistent /Flash (survives power-off; day-to-day
#                        deploys use the /Temp RAM disk to spare flash wear)
#   ./run.sh emu         local VICE session against the running proxy
#   ./run.sh stop        stop a background proxy
#   ./run.sh status      show proxy + C64 connection state

set -euo pipefail
cd "$(dirname "$0")"

PY=llm64_proxy/.venv/bin/python
[ -x "$PY" ] || PY=python3

proxy_running() { ss -tln 2>/dev/null | grep -q ":6400 "; }
mlboy_proxy_up() { timeout 3 bash -c "exec 3<>/dev/tcp/192.168.1.21/6400" 2>/dev/null; }

case "${1:-help}" in
  proxy)
    cd llm64_proxy && exec "${PY#llm64_proxy/}" -m src.main --host 0.0.0.0 --port 6400
    ;;
  proxy-bg)
    proxy_running && { echo "proxy already running on :6400"; exit 0; }
    (cd llm64_proxy && setsid nohup "${PY#llm64_proxy/}" -m src.main \
        --host 0.0.0.0 --port 6400 > proxy-live.log 2>&1 < /dev/null &)
    sleep 2
    proxy_running && echo "proxy started on :6400 (log: llm64_proxy/proxy-live.log)" \
                  || { echo "proxy failed to start - see llm64_proxy/proxy-live.log"; exit 1; }
    ;;
  c64)
    mlboy_proxy_up || echo "warning: mlboy proxy (192.168.1.21:6400) unreachable"
    make deploy-c64u
    ;;
  c64-80)
    mlboy_proxy_up || echo "warning: mlboy proxy (192.168.1.21:6400) unreachable"
    make deploy-c64u-80
    ;;
  install)
    make -C c64_client clean
    make -C c64_client CONNECT=hayes SERVER_IP=192.168.1.21 MODE80=1
    curl -sS --max-time 20 -T c64_client/build/llm64.prg \
        "ftp://192.168.1.64/Flash/llm64.prg" --user anonymous:
    echo "installed to /Flash/llm64.prg (run it from the Ultimate menu)"
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
    mlboy_proxy_up && echo "mlboy proxy: up (192.168.1.21:6400)" \
        || echo "mlboy proxy: DOWN"
    proxy_running && echo "local dev proxy: running on :6400" \
        || echo "local dev proxy: not running"
    ssh -o ConnectTimeout=4 mlboy "ss -tn state established 2>/dev/null | grep -q \"192.168.1.64\"" \
        && echo "c64u: connected to mlboy" || echo "c64u: not connected"
    ;;
  *)
    grep '^#   ' "$0" | sed 's/^#   //'
    ;;
esac
