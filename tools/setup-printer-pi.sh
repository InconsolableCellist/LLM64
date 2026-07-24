#!/usr/bin/env bash
# Set up the CUPS queue that LLM64's /print cups backend spools to
# (docs/14-printer-hardcopy.md 13.6). Run it ON THE MACHINE THE PRINTER
# IS PLUGGED INTO - a Raspberry Pi hidden behind the C64, or the proxy
# host itself. Never goes on the C64 disk; the C64 knows nothing about
# this backend.
#
# It automates the middle of the runbook: packages, vendor driver, queue,
# sharing. What it cannot do for you is plug the printer in and switch it
# on (a sleeping battery printer enumerates as nothing).
#
#   ./setup-printer-pi.sh --driver ~/Downloads/n80/Linux_ZHJY-N80_driver_v1.0.5
#   ./setup-printer-pi.sh --queue n80 --uri 'usb://ZHJY/N80?serial=...' --test
#   ./setup-printer-pi.sh --dry-run          # show every command, run none
#
# Then in llm64_proxy/config.toml:
#   [printer]
#   backend = "both"                 # or "cups" for paper only
#   cups_queue = "n80"
#   cups_server = ""                 # "" when the proxy IS this machine,
#                                    # else "thishost.local:631"

set -euo pipefail

QUEUE=n80
DRIVER=""
URI=""
PPD=""
SHARE=1
TEST=0
DRY=0

usage() { sed -n '2,22p' "$0" | sed 's/^# \?//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --queue)   QUEUE=$2; shift 2 ;;
        --driver)  DRIVER=$2; shift 2 ;;
        --uri)     URI=$2; shift 2 ;;
        --ppd)     PPD=$2; shift 2 ;;
        --no-share) SHARE=0; shift ;;
        --test)    TEST=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

say()  { printf '\n== %s\n' "$*"; }
run()  {
    printf '   $ %s\n' "$*"
    [ "$DRY" = 1 ] || "$@"
}
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. packages ------------------------------------------------------
# cupsd serves the queue; cups-client is what provides `lp` (the proxy
# host needs only that half, and no driver at all).
say "CUPS packages"
if command -v apt-get >/dev/null; then
    run sudo apt-get install -y cups cups-client
elif command -v pacman >/dev/null; then
    run sudo pacman -S --needed --noconfirm cups
    run sudo systemctl enable --now cups
elif command -v dnf >/dev/null; then
    run sudo dnf install -y cups
    run sudo systemctl enable --now cups
else
    echo "   no known package manager - install cups + cups-client yourself"
fi

# The user must be able to talk to cupsd for lpadmin; on Debian that is
# the lpadmin group. Harmless if already a member.
if getent group lpadmin >/dev/null 2>&1; then
    run sudo usermod -aG lpadmin "$USER"
fi

# --- 2. vendor driver -------------------------------------------------
# The N80 speaks a proprietary raster command set, so CUPS needs the
# vendor PPD + rastertoN80 filter (docs/14 2.4). Its own `install` script
# picks the right filter architecture, including a 32-bit userland on a
# 64-bit Pi kernel. A driverless queue (an ordinary laser/inkjet, or
# anything with an IPP Everywhere profile) needs none of this: skip
# --driver and pass --ppd, or let lpadmin auto-detect.
if [ -n "$DRIVER" ]; then
    say "Vendor driver from $DRIVER"
    [ -d "$DRIVER" ] || die "no such directory: $DRIVER"
    [ -x "$DRIVER/install" ] || [ -f "$DRIVER/install" ] \
        || die "$DRIVER has no install script (extract the vendor tarball first)"
    run sudo sh -c "cd '$DRIVER' && ./install"
    [ -n "$PPD" ] || PPD=$(ls "$DRIVER"/ppd/*.ppd 2>/dev/null | head -1 || true)
    [ -n "$PPD" ] && echo "   PPD: $PPD"
fi

# --- 3. find the printer ----------------------------------------------
# lpinfo -v lists what CUPS can see. A printer that is off, asleep or on
# a charge-only USB-C cable shows up as nothing at all - that is the
# usual cause of an empty list, not a driver problem.
if [ -z "$URI" ]; then
    say "Looking for a USB printer"
    if [ "$DRY" = 1 ]; then
        echo '   $ lpinfo -v   (dry run: assuming usb://EXAMPLE/N80)'
        URI='usb://EXAMPLE/N80'
    else
        echo '   $ lpinfo -v'
        ALL=$(sudo lpinfo -v || true)
        printf '%s\n' "$ALL" | sed 's/^/     /'
        URI=$(printf '%s\n' "$ALL" | awk '/^direct +usb:/ {print $2}' | head -1)
        [ -n "$URI" ] || die "no usb:// device listed. Is the printer ON and
       connected with a DATA (not charge-only) USB-C cable? Check
       'dmesg | tail' for the enumeration, then pass --uri yourself."
        echo "   using: $URI"
    fi
fi

# --- 4. the queue -----------------------------------------------------
# -E enables it and makes it accept jobs. Re-running this reconfigures
# the existing queue rather than making a second one.
say "Queue '$QUEUE'"
if [ -n "$PPD" ]; then
    run sudo lpadmin -p "$QUEUE" -E -v "$URI" -P "$PPD"
else
    echo "   (no PPD given - letting CUPS pick a driverless/everywhere profile)"
    run sudo lpadmin -p "$QUEUE" -E -v "$URI" -m everywhere
fi
run sudo cupsenable "$QUEUE"
run sudo cupsaccept "$QUEUE"

# --- 5. share it ------------------------------------------------------
# The network hop is CUPS/IPP from the proxy to THIS machine - never to
# the printer, which has no network of its own. Skip on the proxy host
# itself (--no-share), where cups_server stays "".
if [ "$SHARE" = 1 ]; then
    say "Sharing over IPP"
    run sudo cupsctl --share-printers
    run sudo lpadmin -p "$QUEUE" -o printer-is-shared=true
    echo "   different subnets? also: sudo cupsctl --remote-any"
    echo "   .local name not resolving from the proxy? install avahi-daemon"
fi

# --- 6. smoke test ----------------------------------------------------
# Opt-in because it uses real paper. This is exactly what the proxy does
# on /print, minus the composed document.
if [ "$TEST" = 1 ]; then
    say "Test page"
    if [ "$DRY" = 1 ]; then
        echo "   \$ echo ... | lp -d $QUEUE -t llm64 -o cpi=12 -o lpi=8 -"
    else
        printf 'LLM64 hardcopy test\n%s\n' "$(printf -- '-%.0s' $(seq 1 78))" \
            | lp -d "$QUEUE" -t llm64 -o cpi=12 -o lpi=8 -
        lpstat -o || true
        echo "   nothing came out? 'journalctl -u cups' here, and check the"
        echo "   printer is awake - a spooled job can complete into a"
        echo "   sleeping printer with no page and no error."
    fi
fi

say "Done. In llm64_proxy/config.toml set:"
cat <<EOF
     [printer]
     backend = "both"        # "cups" for paper only
     cups_queue = "$QUEUE"
     cups_server = ""        # "" if the proxy runs HERE, else $(hostname).local:631
EOF
echo "   then restart the proxy and try /print from the C64."
