#!/usr/bin/env bash
# One-stop launcher for the LLM64 stack.
#
# SETUP
#   ./run.sh config      create run.conf and print the current settings.
#                        run.conf contains the proxy address, and 
#                        the real hardware address, if applicable.
#
# EMULATOR
#   ./run.sh emu         40-column build in VICE (deprecated)
#   ./run.sh emu-80      80-column build, plus the boot disk
#   ./run.sh emu-80 HOST:PORT
#                        connect somewhere else for this run only
#
#   The emulator build has NO MODEM (CONNECT=direct): VICE itself opens
#   the TCP connection (the address in the client's own config editor
#   is not used). The proxy in run.conf - or the argument
#   above - is used to make the connection.
#
# PROXY
#   ./run.sh proxy       foreground (Ctrl-C stops)
#   ./run.sh proxy-bg    background (log: llm64_proxy/proxy-live.log)
#   ./run.sh stop        stop a background proxy
#
#   Needs llm64_proxy/config.toml: copy config.toml.example and set your
#   model endpoint and API key
#
# REAL HARDWARE - C64 Ultimate on the network (FTP turned on)
#   ./run.sh c64         build 40-col client, upload it to /Temp, run it (deprecated)
#   ./run.sh c64-80      same, 80-column build
#   ./run.sh install     put the 80-col client in /Flash
#                        (use /Temp builds to avoid rw wear)
#
# ./run.sh status

set -euo pipefail
cd "$(dirname "$0")"

usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; }

# --- settings: defaults, then run.conf, then the environment ---------
CONF=run.conf
PROXY_HOST=127.0.0.1
PROXY_PORT=6400
C64U_HOST=
PROXY_SSH=
# shellcheck source=/dev/null
[ -f "$CONF" ] && . "./$CONF"
PROXY_HOST="${LLM64_PROXY_HOST:-$PROXY_HOST}"
PROXY_PORT="${LLM64_PROXY_PORT:-$PROXY_PORT}"
C64U_HOST="${LLM64_C64U_HOST:-$C64U_HOST}"
PROXY_SSH="${LLM64_PROXY_SSH:-$PROXY_SSH}"

PY=llm64_proxy/.venv/bin/python
[ -x "$PY" ] || PY=python3

# Is the configured proxy this very machine (so `emu` may start one)?
proxy_is_local() {
  case "$PROXY_HOST" in 127.0.0.1|localhost|::1) return 0;; *) return 1;; esac
}
# Something listening on our port here? (What `stop` and `proxy-bg` mean
# by "running" - it cannot tell whose proxy it is, only that the port is
# taken.)
local_proxy_listening() { ss -tln 2>/dev/null | grep -q ":$PROXY_PORT "; }
# host port -> 0 if a TCP connect succeeds. No IPv6 literals: they would
# need bracket parsing here and in the host:port splitting below.
port_open() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

need_c64u() {
  [ -n "$C64U_HOST" ] || {
    echo "No C64U_HOST set - put your C64 Ultimate's address in $CONF"
    echo "(./run.sh config creates it). Emulator commands need no hardware."
    exit 1
  }
}

case "${1:-help}" in
  config)
    if [ -f "$CONF" ]; then
      echo "$CONF exists - edit it to change these."
    else
      cp run.conf.example "$CONF"
      echo "created $CONF from run.conf.example - edit it to match your setup."
    fi
    echo
    echo "  proxy         $PROXY_HOST:$PROXY_PORT"
    proxy_is_local && echo "                (this machine - ./run.sh proxy starts it)"
    echo "  c64 ultimate  ${C64U_HOST:-<unset - emulator only>}"
    echo "  status ssh    ${PROXY_SSH:-<unset - skipped>}"
    echo
    if proxy_is_local && [ ! -f llm64_proxy/config.toml ]; then
      echo "  NOTE: no llm64_proxy/config.toml yet. Copy config.toml.example"
      echo "        and set your model endpoint and API key, or the proxy"
      echo "        will answer every request with an API error."
    fi
    ;;
  proxy|proxy-bg)
    [ -f llm64_proxy/config.toml ] || \
      echo "warning: no llm64_proxy/config.toml - replies will fail with an API error"
    if [ "$1" = proxy ]; then
      cd llm64_proxy && exec "${PY#llm64_proxy/}" -m src.main \
          --host 0.0.0.0 --port "$PROXY_PORT"
    fi
    local_proxy_listening && { echo "something is already listening on :$PROXY_PORT"; exit 0; }
    (cd llm64_proxy && setsid nohup "${PY#llm64_proxy/}" -m src.main \
        --host 0.0.0.0 --port "$PROXY_PORT" > proxy-live.log 2>&1 < /dev/null &)
    sleep 2
    local_proxy_listening && echo "proxy started on :$PROXY_PORT (log: llm64_proxy/proxy-live.log)" \
                          || { echo "proxy failed to start - see llm64_proxy/proxy-live.log"; exit 1; }
    ;;
  c64|c64-80)
    need_c64u
    # Advisory only: this tests OUR route to the proxy, while what
    # actually matters is the C64's. They differ often enough (VPN here,
    # plain LAN there) that this is a hint, not a gate.
    port_open "$PROXY_HOST" "$PROXY_PORT" || \
      echo "warning: proxy $PROXY_HOST:$PROXY_PORT unreachable from here"
    [ "$1" = c64-80 ] && TGT=deploy-c64u-80 || TGT=deploy-c64u
    make "$TGT" C64_PROXY_IP="$PROXY_HOST" C64_PROXY_PORT="$PROXY_PORT" \
                C64U_IP="$C64U_HOST"
    ;;
  install)
    need_c64u
    make -C c64_client clean
    make -C c64_client CONNECT=hayes SERVER_IP="$PROXY_HOST" \
         SERVER_PORT="$PROXY_PORT" MODE80=1
    curl -sS --max-time 20 -T c64_client/build/llm64.prg \
        "ftp://$C64U_HOST/Flash/llm64.prg" --user anonymous:
    echo "installed to /Flash/llm64.prg (run it from the Ultimate menu)"
    ;;
  emu|emu-80)
    # An explicit argument is for one run and overrides everything. With
    # no argument we use the configured proxy, and start one ourselves
    # only if that proxy is meant to be this machine.
    TARGET="${2:-$PROXY_HOST:$PROXY_PORT}"
    if [ -z "${2:-}" ] && proxy_is_local; then
      local_proxy_listening || "$0" proxy-bg
    fi
    if [ "$1" = emu-80 ]; then
      # ...and the boot disk: run_emu.sh mounts build/llm64.d64 on unit 8
      # if it exists, and every overlay module (config, convmgr, diskcopy,
      # jukebox) loads from there. Without it F1 falls back to the compact
      # built-in menu and e)config reports "Module load failed - boot disk?".
      # client-tui-direct-80 starts with a clean, which deletes the image.
      make client-tui-direct-80
      make -C c64_client disk
    else
      make client-tui-direct
    fi
    port_open "${TARGET%:*}" "${TARGET##*:}" || \
      echo "warning: nothing answering at $TARGET - the client will sit at 'Contacting server'"
    echo "emulated ACIA -> $TARGET"
    ./emu/run_emu.sh direct "$TARGET"
    ;;
  stop)
    pkill -f "src.main --host" && echo "proxy stopped" || echo "no proxy running"
    ;;
  status)
    if port_open "$PROXY_HOST" "$PROXY_PORT"; then
      echo "proxy $PROXY_HOST:$PROXY_PORT: up"
    else
      echo "proxy $PROXY_HOST:$PROXY_PORT: DOWN"
    fi
    # A listener here when the proxy is meant to live elsewhere is worth
    # saying out loud: it is exactly what silently answers `emu` runs
    # aimed at localhost.
    if ! proxy_is_local && local_proxy_listening; then
      echo "local :$PROXY_PORT: something else is listening here"
    fi
    if [ -n "$C64U_HOST" ]; then
      if port_open "$C64U_HOST" 80; then
        echo "c64u $C64U_HOST: up"
      else
        echo "c64u $C64U_HOST: not answering"
      fi
    fi
    if [ -n "$PROXY_SSH" ] && [ -n "$C64U_HOST" ]; then
      if ssh -o ConnectTimeout=4 "$PROXY_SSH" \
             "ss -tn state established 2>/dev/null | grep -q \"$C64U_HOST\""; then
        echo "c64u: connected to the proxy"
      else
        echo "c64u: not connected"
      fi
    fi
    ;;
  *)
    usage
    ;;
esac
