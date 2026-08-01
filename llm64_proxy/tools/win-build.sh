#!/usr/bin/env bash
# Build dist/llm64-proxy.exe with the Wine-hosted Windows Python that
# tools/win-build-setup.sh installed. Run the setup script once first.
#
# The Windows and Linux binaries share dist/ (different names) but not
# the PyInstaller work directory: --workpath build/win keeps the two
# analyses from overwriting each other's caches.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
proxydir="$(dirname "$here")"

export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-llm64proxy}"
export WINEDLLOVERRIDES="mscoree,mshtml="
export WINEDEBUG="${WINEDEBUG:--all}"

winpy='C:\Python312\python.exe'

if [ ! -x "$WINEPREFIX/drive_c/Python312/python.exe" ]; then
    echo "No Windows Python in $WINEPREFIX."
    echo "Run llm64_proxy/tools/win-build-setup.sh once (it downloads and"
    echo "installs CPython under Wine), then try again."
    exit 1
fi

cd "$proxydir"
# No `wineserver -w` after this: PyInstaller is synchronous, and waiting
# on the server means waiting for every other process in the prefix
# (a launcher window left open, a wineserver that outlived a killed
# app), which hangs the build forever.
wine "$winpy" -m PyInstaller llm64.spec --noconfirm \
    --workpath build/win --distpath dist

ls -lh dist/llm64-proxy.exe
