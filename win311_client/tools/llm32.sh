#!/usr/bin/env bash
# Launch LLM32.EXE under Wine the quiet way. Exists because launching
# `wine build/LLM32.EXE` by hand floods the terminal with
# err:msg:process_hardware_message - Wine's input plumbing, not the
# client - and because a resident wineserver started WITHOUT
# WINEDEBUG keeps its channels on no matter what a later launch sets.
# So: same-version reset first, then a silenced launch.
#
#   ./tools/llm32.sh                     # INI's server
#   ./tools/llm32.sh 127.0.0.1 6400      # explicit host/port
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here/.."

export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-llm64}"
export WINEDEBUG=-all

[ -f build/LLM32.EXE ] || { echo "build/LLM32.EXE missing - run make both"; exit 1; }

# Retire any resident wineserver and its services: they were started
# with someone else's environment and keep logging with it. Cheap when
# nothing is running, and it also clears a stale server left over from
# a Wine upgrade - the other source of hardware-message chatter.
wineserver -k 2>/dev/null
wineserver -w 2>/dev/null

exec wine build/LLM32.EXE "$@"
