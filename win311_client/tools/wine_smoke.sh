#!/usr/bin/env bash
# Drive the client under Wine and photograph it: connect, ping, send a
# line, capture the reply. The Windows equivalent of emu/test_e2e.py,
# minus the emulator.
#
#   ./tools/devproxy.sh 6410 &      # in one shell
#   ./tools/wine_smoke.sh 6410      # in another
#
# Screenshots land in build/shot-*.png.

set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here/.."
port="${1:-6410}"
msg="${2:-hello from windows 3.11}"

export DISPLAY="${DISPLAY:-:0}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-llm64}"
export WINEDEBUG=-all

[ -f build/LLM64.EXE ] || { echo "build/LLM64.EXE missing - run make"; exit 1; }
rm -f build/shot-*.png

# Remember the windows that already exist: an earlier run whose timeout
# has not expired yet has the same class and the same title, and would
# be photographed instead of this one.
before=" $(xdotool search --classname winevdm.exe 2>/dev/null | tr '\n' ' ')"

timeout 180 wine build/LLM64.EXE 127.0.0.1 "$port" >build/wine.log 2>&1 &
wine_pid=$!
# Kill by PID, never by pattern: a pkill -f on the executable name also
# matches this script's own command line.
trap 'kill $wine_pid 2>/dev/null' EXIT

# The window is the one owned by the 16-bit VDM and titled LLM64 - the
# title alone matches file managers and editors that happen to have the
# project open.
wid=""
for _ in $(seq 40); do
    for w in $(xdotool search --classname winevdm.exe 2>/dev/null); do
        case "$before" in *" $w "*) continue;; esac
        if [ "$(xdotool getwindowname "$w" 2>/dev/null)" = "LLM64" ]; then
            wid="$w"; break 2
        fi
    done
    sleep 0.5
done
[ -n "$wid" ] || { echo "client window never appeared"; cat build/wine.log; exit 1; }
echo "window $wid"

xdotool windowactivate --sync "$wid"; sleep 1
import -window "$wid" build/shot-1-connected.png

xdotool type --window "$wid" --delay 40 "$msg"
sleep 1
import -window "$wid" build/shot-2-typed.png

xdotool key --window "$wid" Return
sleep 12
import -window "$wid" build/shot-3-reply.png

echo "captured:"
ls -la build/shot-*.png
