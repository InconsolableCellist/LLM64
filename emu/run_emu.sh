#!/usr/bin/env bash
# Launch x64sc wired up for the LLM64 client, for interactive use.
#
#   ./run_emu.sh direct [host:port]   boots llm64-vice.d64: the ACIA is
#                                     the socket and VICE dials the proxy
#                                     itself. No modem, nothing to answer
#                                     AT commands. This is the emulator
#                                     disk, and the default.
#   ./run_emu.sh hayes  [tcpser-port] boots llm64.d64: the hardware disk,
#                                     which dials ATDT. tcpser plays the
#                                     modem, and this script starts one if
#                                     the port is free.
#
# Which disk boots is the whole difference between the two modes, and
# getting it wrong is not subtle: a hayes client with nothing to answer
# ATZ sits in "Resetting modem..." forever while the proxy shows a
# connected socket, because VICE opens the TCP connection the moment the
# ACIA does.
#
# The critical flags (this is what never worked before): the client drives a
# 6551 ACIA at $DE00, so VICE needs the ACIA cartridge emulation (-acia1),
# NOT the userport RS232 device. -myaciadev 0 routes it to -rsdev1.
#
# x64sc runs through emu/vice-run.sh: a native install if there is one,
# otherwise the net.sf.VICE flatpak.

set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-direct}"
PRG="c64_client/build/llm64.prg"

case "$MODE" in
  direct)
    TARGET="${2:-127.0.0.1:6400}"
    RSDEV=(-rsdev1 "$TARGET" +rsdev1ip232)
    DISK="c64_client/build/llm64-vice.d64"
    BUILD_HINT="make disk-vice (or ./run.sh emu-80)"
    ;;
  hayes)
    TCPSER_PORT="${2:-25232}"
    TARGET="127.0.0.1:$TCPSER_PORT"
    RSDEV=(-rsdev1 "$TARGET" -rsdev1ip232)
    DISK="c64_client/build/llm64.d64"
    BUILD_HINT="make disk"
    ;;
  *)
    echo "usage: $0 [direct|hayes] [target]"; exit 1
    ;;
esac

# The disk decides what boots, and that is deliberate: llm64-vice.d64 is
# always a CONNECT=direct build and llm64.d64 always a CONNECT=hayes one,
# so mounting the right image for the mode cannot pick the wrong client.
# A loose build/llm64.prg carries no such guarantee - it is whatever was
# compiled last - and booting a hayes PRG in direct mode is precisely the
# "Resetting modem..." loop this script exists to avoid. So we do not
# fall back to it silently.
#
# BOOT_PRG=1 asks for the loose PRG anyway. That is the 40-column path
# (./run.sh emu), which has no modules and therefore no disk.
DISK8=()
if [ "${BOOT_PRG:-0}" = 1 ]; then
  BOOT="$PRG"
  [ -f "$BOOT" ] || { echo "No $PRG - build the client first"; exit 1; }
else
  BOOT="$DISK"
  if [ ! -f "$DISK" ]; then
    echo "No $DISK - this is the disk $MODE mode boots."
    echo "Build it with: $BUILD_HINT"
    echo "(BOOT_PRG=1 boots build/llm64.prg instead, for a 40-column build.)"
    exit 1
  fi
  # No staleness check against build/llm64.prg on purpose: an image holds
  # its own client and the modules built with it, so it is consistent
  # whatever was compiled afterwards. `make release` builds this disk and
  # then the other one, which would make every post-release run look
  # stale for no reason.
  DISK8=(-8 "$DISK")
fi

# tcpser is the modem in hayes mode. Start one only if nothing already
# holds the port (an existing tcpser, or a real WiFi modem forwarded
# there, is left alone), and take it down with us.
if [ "$MODE" = hayes ] && ! (exec 3<>/dev/tcp/127.0.0.1/"$TCPSER_PORT") 2>/dev/null; then
  command -v tcpser >/dev/null || {
    echo "hayes mode needs tcpser on :$TCPSER_PORT (install it, or use direct mode)"
    exit 1; }
  tcpser -v "$TCPSER_PORT" -s 9600 -p $((TCPSER_PORT + 1)) -tSs >/dev/null 2>&1 &
  TCPSER_PID=$!
  trap 'kill "$TCPSER_PID" 2>/dev/null' EXIT
  echo "tcpser on :$TCPSER_PORT (pid $TCPSER_PID)"
fi

# Printer on IEC device 4, so /print works here too (docs/14). The ascii
# driver appends the text as it is printed; -pr4drv mps803 -pr4output
# graphics renders dot-matrix page images instead.
PRINTER=(-devicebackend4 1 -busdevice4
         -pr4drv ascii -pr4output text
         -pr4txtdev 0 -prtxtdev1 c64_client/build/printer4.txt)

echo "booting $BOOT ($MODE) -> $TARGET"
./emu/vice-run.sh x64sc \
  -acia1 -acia1mode 0 -acia1base 0xDE00 -acia1irq 2 -myaciadev 0 \
  "${RSDEV[@]}" -rsdev1baud 9600 \
  "${DISK8[@]}" \
  "${PRINTER[@]}" \
  -autostartprgmode 1 -autostart "$BOOT"
