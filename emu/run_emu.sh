#!/usr/bin/env bash
# Launch x64sc wired up for the LLM64 client, for interactive use.
#
#   ./run_emu.sh direct [host:port]   ACIA pipe straight to the proxy
#                                     (build client with CONNECT=direct)
#   ./run_emu.sh hayes  [tcpser-port] via tcpser modem emulation
#                                     (build with CONNECT=hayes SERVER_IP=127.0.0.1;
#                                      start tcpser separately: tcpser -v 25232 -s 9600 -p 25233)
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

[ -f "$PRG" ] || { echo "Build the client first: make client-direct (or client-hayes-local)"; exit 1; }

case "$MODE" in
  direct)
    TARGET="${2:-127.0.0.1:6400}"
    RSDEV=(-rsdev1 "$TARGET" +rsdev1ip232)
    ;;
  hayes)
    TARGET="127.0.0.1:${2:-25232}"
    RSDEV=(-rsdev1 "$TARGET" -rsdev1ip232)
    ;;
  *)
    echo "usage: $0 [direct|hayes] [target]"; exit 1
    ;;
esac

# Overlay-module disk (make disk): mount on unit 8 if built
DISK8=()
[ -f c64_client/build/llm64.d64 ] && DISK8=(-8 c64_client/build/llm64.d64)

# Printer on IEC device 4, so /print works here too (docs/14). The ascii
# driver appends the text as it is printed; -pr4drv mps803 -pr4output
# graphics renders dot-matrix page images instead.
PRINTER=(-devicebackend4 1 -busdevice4
         -pr4drv ascii -pr4output text
         -pr4txtdev 0 -prtxtdev1 c64_client/build/printer4.txt)

exec ./emu/vice-run.sh x64sc \
  -acia1 -acia1mode 0 -acia1base 0xDE00 -acia1irq 2 -myaciadev 0 \
  "${RSDEV[@]}" -rsdev1baud 9600 \
  "${DISK8[@]}" \
  "${PRINTER[@]}" \
  -autostartprgmode 1 -autostart "$PRG"
