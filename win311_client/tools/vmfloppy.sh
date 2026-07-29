#!/usr/bin/env bash
# Insert, eject and inspect the client's floppy image in a *running*
# QEMU guest, through its monitor socket. No reboot, no restart.
#
#   ./tools/vmfloppy.sh in [image]     insert (rebuild-safe: ejects first)
#   ./tools/vmfloppy.sh out            eject
#   ./tools/vmfloppy.sh status         what the guest has in drive A:
#
# The VM has to have been started with a floppy *device* for any of this
# to work. `-M pc` gives you one for free - `info block` lists it as
# `floppy0: [not inserted]` - so no `-fda` is needed at boot; the drive
# is there whether or not media ever was.
#
# WHY IT ALWAYS EJECTS FIRST: QEMU opens the image file once, at insert.
# Rebuilding the image at the same path afterwards changes bytes the
# guest will never see - and `change` on already-inserted media is a
# no-op for the host file handle. Eject, then change, or you spend an
# afternoon testing the binary you built an hour ago.
#
# Windows caches the FAT too. After a swap, A: in File Manager wants an
# F5, and a DOS box wants any command that touches the drive again; the
# emulated drive does raise the disk-change line, but 3.11 only looks at
# it when something asks it to.

set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MONSOCK="${MONSOCK:-$HOME/VMs/win311/qemu-monitor.sock}"
IMG_DEFAULT="$here/../build/llm64.img"
DEV="${DEV:-floppy0}"

[ -S "$MONSOCK" ] || {
    echo "vmfloppy: no monitor socket at $MONSOCK" >&2
    echo "vmfloppy: is the VM running? (set MONSOCK= to point elsewhere)" >&2
    exit 1
}
command -v socat >/dev/null || { echo "vmfloppy: needs socat" >&2; exit 1; }

# The monitor is a readline prompt: it echoes every character back with
# cursor-motion escapes. Send the command, give it a moment, then strip
# the echo so the caller sees only the answer.
mon() {
    printf '%s\n' "$1" | socat -T1 - UNIX-CONNECT:"$MONSOCK" 2>/dev/null \
        | sed -e 's/\x1b\[[0-9;]*[A-Za-z]//g' \
              -e 's/\r$//' -e 's/^.*\r//' \
              -e '/^(qemu)/d' -e '/^QEMU [0-9]/d' -e '/^[[:space:]]*$/d' \
        | grep -vxF "$1" || true
}

case "${1:-status}" in
in|insert)
    img="${2:-$IMG_DEFAULT}"
    [ -f "$img" ] || { echo "vmfloppy: no image at $img (make floppy?)" >&2; exit 1; }
    img="$(cd "$(dirname "$img")" && pwd)/$(basename "$img")"
    mon "eject -f $DEV" >/dev/null
    out="$(mon "change $DEV $img raw")"
    [ -n "$out" ] && { echo "$out" >&2; exit 1; }
    echo "vmfloppy: A: = $img"
    mon "info block" | grep -A1 "^$DEV"
    ;;
out|eject)
    mon "eject -f $DEV" >/dev/null
    echo "vmfloppy: A: ejected"
    ;;
status)
    mon "info block" | grep -A2 "^$DEV" || echo "vmfloppy: no $DEV device"
    ;;
*)
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac
