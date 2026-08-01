#!/usr/bin/env bash
# One-time setup for building llm64-proxy.exe on this Linux box.
#
# PyInstaller does not cross-compile, so the Windows binary has to be
# built by a Windows Python. Wine provides one: this installs CPython
# into a dedicated Wine prefix and puts PyInstaller and the proxy's
# dependencies in it. After this runs once, tools/win-build.sh (and
# `make proxy-bin-win` at the top level) produce dist/llm64-proxy.exe.
#
#   ./tools/win-build-setup.sh          # ~15 min, ~600 MB in the prefix
#
# The prefix is separate from ~/.wine and from the client's
# ~/.wine-llm64 on purpose: a Python install with a broken prefix is
# cheap to throw away (rm -rf ~/.wine-llm64proxy) and takes nothing
# else with it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
proxydir="$(dirname "$here")"

PYVER="${PYVER:-3.12.10}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-llm64proxy}"
export WINEARCH=win64
# No Mono/Gecko: the installer does not need them and the download
# prompts block a non-interactive run.
export WINEDLLOVERRIDES="mscoree,mshtml="
export WINEDEBUG="${WINEDEBUG:--all}"

PYDIR='C:\Python312'
cache="$proxydir/build/win-setup"
installer="$cache/python-$PYVER-amd64.exe"

command -v wine >/dev/null || { echo "wine not found - install it first"; exit 1; }

# Let the installer's background processes finish, but never block on
# them: `wineserver -w` returns only when EVERY process in the prefix
# is gone, so one stuck service would hang the setup for good.
wait_wine() { timeout 120 wineserver -w || true; }

mkdir -p "$cache"
if [ ! -s "$installer" ]; then
    echo "==> downloading CPython $PYVER (Windows amd64)"
    curl -fL --retry 3 -o "$installer" \
        "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"
fi

echo "==> initialising Wine prefix $WINEPREFIX"
wineboot -u >/dev/null 2>&1 || true
wait_wine

echo "==> installing CPython into $PYDIR (quiet; several minutes)"
# Include_tcltk=1 is not optional: the launcher UI is Tkinter, and
# PyInstaller can only bundle what the build Python can import.
wine "$installer" /quiet \
    TargetDir="$PYDIR" \
    InstallAllUsers=0 \
    Include_tcltk=1 \
    Include_pip=1 \
    Include_test=0 \
    Include_launcher=0 \
    PrependPath=0 \
    AssociateFiles=0 \
    Shortcuts=0
wait_wine

winpy="$PYDIR\\python.exe"
echo "==> $(wine "$winpy" -c 'import sys; print(sys.version)' 2>/dev/null)"
wine "$winpy" -c 'import tkinter' || {
    echo "tkinter missing in the Wine Python - rerun with Include_tcltk=1"; exit 1; }

echo "==> installing the proxy dependencies and PyInstaller"
wine "$winpy" -m pip install --upgrade pip
wine "$winpy" -m pip install -r "$(winepath -w "$proxydir/requirements.txt")" pyinstaller
wait_wine

echo
echo "Setup complete. Build the exe with:"
echo "    make proxy-bin-win        (from the repository root)"
