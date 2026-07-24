#!/usr/bin/env python3
"""End-to-end test for the shareware intro on the free disk.

Boots build/llm64-free.d64 in VICE exactly as a user's Run Disk would,
and asserts the whole chain: the intro paints its panel, the nag really
cannot be skipped, and a key afterwards chain-loads the stock client.

Deliberately NOT part of `make test-all` (docs/11 section 8.3): it runs
without warp, because the nag assertion is about wall-clock seconds, so
it costs ~40s. Run it after touching c64_client/intro/.

    python3 emu/test_intro.py

The intro's text panel lives at $0C00 (the picture owns $0400 as bitmap
color data), so screen assertions read that region directly rather than
going through ViceMonitor.screen_rows().
"""

import argparse
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'emu'))

from vice_monitor import ViceMonitor, ViceMonitorError, screen_code_to_char

PANEL = 0x0F20          # text screen $0C00 + 20 rows: the five visible ones
PANEL_ROWS = 5
SOFT80_SHADOW = 0xC000  # the chained client's 80-column ASCII shadow


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_port(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(('127.0.0.1', port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def panel_text(monitor):
    data = monitor.read_memory(PANEL, PANEL + PANEL_ROWS * 40 - 1)
    return '\n'.join(
        ''.join(screen_code_to_char(b) for b in data[r * 40:(r + 1) * 40])
        for r in range(PANEL_ROWS))


def client_text(monitor):
    data = monitor.read_memory(SOFT80_SHADOW, SOFT80_SHADOW + 80 * 25 - 1)
    return '\n'.join(
        ''.join(chr(b) if 0x20 <= b < 0x7F else ' '
                for b in data[r * 80:(r + 1) * 80])
        for r in range(25))


def wait_for(monitor, reader, pattern, timeout, label):
    regex = re.compile(pattern, re.IGNORECASE)
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        try:
            last = reader(monitor)
        except ViceMonitorError as e:
            print(f'  monitor hiccup: {e}')
            time.sleep(0.5)
            continue
        if regex.search(last):
            print(f'  PASS: {label}')
            return last
        time.sleep(0.5)
    raise AssertionError(f'timeout ({timeout}s) waiting for /{pattern}/ '
                         f'({label})\n--- last ---\n{last}\n------------')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--d64', default=str(REPO / 'c64_client/build/llm64-free.d64'))
    ap.add_argument('--timeout', type=float, default=60.0)
    args = ap.parse_args()

    d64 = Path(args.d64)
    if not d64.exists():
        raise SystemExit(f'{d64} not found - build it with '
                         '`make -C c64_client MODE80=1 && '
                         'make -C c64_client disk-free`')

    port = free_port()
    # -console keeps VICE off the desktop (no X needed); no -warp, because
    # the nag countdown is the thing under test.
    vice = subprocess.Popen([
        str(REPO / 'emu/vice-run.sh'), 'x64sc', '-default', '-console',
        '-sounddev', 'dummy', '+confirmonexit',
        '-8', str(d64),
        '-binarymonitor', '-binarymonitoraddress', f'ip4://127.0.0.1:{port}',
        '-autostart', str(d64),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for_port(port, 30):
            raise AssertionError('VICE binary monitor did not come up')
        monitor = ViceMonitor(port=port)

        # 1. The intro paints its panel once the data blob is in.
        wait_for(monitor, panel_text, r'patreon\.com/c/foxipso',
                 args.timeout, 'intro panel with support links')
        wait_for(monitor, panel_text, r'please wait',
                 5, 'nag countdown running')

        # 2. Keys during the countdown are drained, not honored.
        monitor.keyboard_feed(' ')
        time.sleep(3)
        panel = panel_text(monitor)
        if 'please wait' not in panel.lower():
            raise AssertionError('nag was skippable - a key during the '
                                 f'countdown dismissed it\n{panel}')
        print('  PASS: early keypress ignored')

        # 3. After the countdown the prompt appears and a key chains out.
        wait_for(monitor, panel_text, r'press any key', 20, 'nag expired')
        monitor.keyboard_feed(' ')

        # 4. The stock client boots. Assert on its title bar, not on the
        # config editor: that only opens in non-CONNECT_DIRECT builds
        # (main.c gates config_load() on it), so a `direct` client goes
        # straight to "Contacting server..." on a fresh disk.
        wait_for(monitor, client_text, r'f1=menu', args.timeout,
                 'client chain-loaded and running')
        print('\nintro test PASSED')
    finally:
        try:
            monitor.quit()
        except Exception:
            pass
        time.sleep(1)
        vice.terminate()
        try:
            vice.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vice.kill()


if __name__ == '__main__':
    main()
