#!/usr/bin/env bash
# Run a VICE tool (x64sc, c1541, petcat, ...) wherever it lives on this host:
# a native install if there is one, otherwise the net.sf.VICE flatpak.
#
#   ./emu/vice-run.sh x64sc -default -acia1 ...
#   ./emu/vice-run.sh c1541 -format "c64llm,01" d64 build/c64llm.d64 ...
#
# The flatpak needs two things its manifest does not grant:
#
#   --share=network  the sandbox otherwise gets a private network namespace
#                    with nothing but an isolated `lo`, so -rsdev1 could not
#                    reach the proxy on 127.0.0.1 and the host could not
#                    reach VICE's binary monitor. Sharing the host netns
#                    makes both sides agree on what 127.0.0.1 means.
#   --cwd            so relative paths on the command line still resolve.
#                    (The repo lives under $HOME, which the manifest already
#                    shares; a checkout elsewhere needs --filesystem too -
#                    see VICE_FLATPAK_ARGS below.)
#
# Overrides: VICE_FLATPAK=app.id, VICE_FLATPAK_ARGS="--filesystem=/srv/..."

set -euo pipefail

TOOL="${1:-}"
[ -n "$TOOL" ] || { echo "usage: $0 <x64sc|c1541|petcat|...> [args...]" >&2; exit 2; }
shift

if command -v "$TOOL" >/dev/null 2>&1; then
  exec "$TOOL" "$@"
fi

APP="${VICE_FLATPAK:-net.sf.VICE}"
if command -v flatpak >/dev/null 2>&1 && flatpak info "$APP" >/dev/null 2>&1; then
  # shellcheck disable=SC2086  # VICE_FLATPAK_ARGS is intentionally word-split
  exec flatpak run --command="$TOOL" --share=network --die-with-parent \
       --cwd="$PWD" ${VICE_FLATPAK_ARGS:-} "$APP" "$@"
fi

echo "vice-run: no '$TOOL' on PATH and no $APP flatpak installed." >&2
echo "  install VICE from your distro, or: flatpak install flathub $APP" >&2
exit 127
