#!/usr/bin/env bash
# Drive the client under Wine and photograph it: connect, ping, send a
# line, capture the reply. The Windows equivalent of emu/test_e2e.py,
# minus the emulator.
#
#   ./tools/devproxy.sh 6410 &      # in one shell
#   ./tools/wine_smoke.sh 6410      # in another
#
# Screenshots land in build/shot-*.png.
#
# It drives the keyboard with XTEST, which types into whatever has focus
# on the display it is pointed at - so by default it brings up its own
# Xvfb rather than typing into the session you are sitting in front of.
# DISPLAY=:0 ./tools/wine_smoke.sh to watch it happen instead, and then
# keep your hands off the keyboard while it runs.

set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here/.."
port="${1:-6410}"
msg="${2:-hello from windows 3.11}"

xvfb_pid=""
wm_pid=""
if [ -z "${DISPLAY:-}" ]; then
    for n in 99 98 97 96; do
        if [ ! -e "/tmp/.X11-unix/X$n" ]; then
            Xvfb ":$n" -screen 0 1024x768x24 >/dev/null 2>&1 &
            xvfb_pid=$!
            export DISPLAY=":$n"
            sleep 1
            break
        fi
    done
    [ -n "$xvfb_pid" ] || { echo "no free display for Xvfb"; exit 1; }
    # A window manager is not decoration here: with no WM to confirm the
    # resize, Wine never turns an X ConfigureNotify into WM_SIZE, so the
    # client goes on painting at its old width and the re-flow shots
    # below silently prove nothing.
    if command -v openbox >/dev/null; then
        openbox >/dev/null 2>&1 &
        wm_pid=$!
        sleep 1
    else
        echo "warning: no window manager - the resize shots will not re-flow"
    fi
    echo "headless on $DISPLAY"
fi
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
trap 'kill $wine_pid 2>/dev/null
      [ -n "$wm_pid" ] && kill $wm_pid 2>/dev/null
      [ -n "$xvfb_pid" ] && kill $xvfb_pid 2>/dev/null' EXIT

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

# A bare Xvfb has no window manager, so there is nothing to honour
# _NET_ACTIVE_WINDOW; windowfocus is the one that works there.
xdotool windowactivate --sync "$wid" 2>/dev/null || xdotool windowfocus "$wid"
sleep 1
import -window "$wid" build/shot-1-connected.png

# Type through XTEST, into whatever is focused, rather than with
# --window: a targeted xdotool type is XSendEvent, and the 16-bit VDM
# drops most of those synthetic keystrokes - the symptom is a message
# that arrives at the proxy as its first word only.
xdotool type --delay 80 "$msg"
sleep 1
import -window "$wid" build/shot-2-typed.png

xdotool key Return
sleep 12
import -window "$wid" build/shot-3-reply.png

# Re-flow. The transcript stores logical lines unwrapped and wraps them
# at paint time, so these two shots are the same text laid out twice -
# which is the thing the Phase 0 fixed array could not do at all.
xdotool windowsize --sync "$wid" 420 440; sleep 2
import -window "$wid" build/shot-4-narrow.png
xdotool windowsize --sync "$wid" 900 440; sleep 2
import -window "$wid" build/shot-5-wide.png

echo "captured:"
ls -la build/shot-*.png
