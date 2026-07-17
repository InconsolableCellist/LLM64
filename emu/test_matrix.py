#!/usr/bin/env python3
"""Keyboard matrix scanner test: REAL host keystrokes via xdotool.

The main e2e suite types through VICE's binary monitor, which writes the
KERNAL keyboard buffer directly and BYPASSES the custom matrix scanner in
keyboard.s. This test drives actual X11 key events into the VICE window
(XTEST), exercising host key -> emulated matrix -> keyboard.s -> buffer.

Requires a real X session and steals focus while running - not part of
`make test-all`. Run with: make test-emu-matrix

Covers: typing with shift/digits/punctuation, backspace, send/reply
round-trip, and 3-key simultaneous rollover (the KERNAL scanner would
register only one of three keys pressed in the same frame).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_e2e import (Stack, find_free_port, wait_for_port, proxy_python,
                      wait_for_screen, PROXY_DIR, REPO)
from vice_monitor import ViceMonitor


def xdo(*args, **kw):
    subprocess.run(['xdotool', *args], check=True, **kw)


def hold_key(sym, hold=0.04):
    """Press a key long enough for a 60Hz matrix scan to see it.
    xdotool's synthetic taps last ~10ms, which can fall entirely between
    two 16.7ms scan samples - real keypresses last 80ms+."""
    xdo('keydown', sym)
    time.sleep(hold)
    xdo('keyup', sym)
    time.sleep(0.025)


def type_held(text):
    for ch in text:
        if ch == ' ':
            hold_key('space')
        elif ch.isupper():
            xdo('keydown', 'shift')
            time.sleep(0.01)
            hold_key(ch.lower())
            xdo('keyup', 'shift')
            time.sleep(0.01)
        else:
            hold_key(ch)


def main():
    artifacts = REPO / 'emu' / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    stack = Stack(artifacts)
    status = 1
    try:
        mock_port = find_free_port()
        stack.start('mock_llm', [sys.executable, str(REPO / 'emu/mock_llm.py'),
                                 '--port', str(mock_port)])
        if not wait_for_port(mock_port):
            raise AssertionError('mock LLM did not start')
        env = dict(os.environ, OPENAI_API_KEY='k', OPENAI_MODEL='mock',
                   OPENAI_API_BASE=f'http://127.0.0.1:{mock_port}/v1')
        stack.start('proxy', [proxy_python(), '-m', 'src.main',
                              '--host', '127.0.0.1', '--port', '6400'],
                    cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(6400):
            raise AssertionError('proxy did not start')

        mon = find_free_port()
        stack.start('vice', [
            'x64sc', '-default', '-sounddev', 'dummy', '+confirmonexit',
            '-acia1', '-acia1mode', '0', '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            '-rsdev1', '127.0.0.1:6400', '+rsdev1ip232',
            '-rsdev1baud', '9600',
            '-binarymonitor',
            '-binarymonitoraddress', f'ip4://127.0.0.1:{mon}',
            '-autostartprgmode', '1',
            '-autostart', str(REPO / 'c64_client/build/c64llm.prg')])
        if not wait_for_port(mon, timeout=30):
            raise AssertionError('VICE monitor did not start')
        time.sleep(4)
        monitor = ViceMonitor(port=mon)
        wait_for_screen(monitor, r'ready\. type your message', 60,
                        artifacts, 'mx-ready')

        win = subprocess.check_output(
            ['xdotool', 'search', '--name', 'VICE .C64SC.'],
            text=True).split()[0]
        xdo('windowactivate', '--sync', win)
        time.sleep(1)

        # 1. Real typing: mixed case (explicit shift chord) and digits
        type_held('test of Real keys 123')
        wait_for_screen(monitor, r'test of real keys 123', 15,
                        artifacts, 'mx-typed')

        # 2. Backspace, then send and get a reply
        for _ in range(4):
            hold_key('BackSpace')
        hold_key('Return')
        wait_for_screen(monitor, r'> test of real keys', 30,
                        artifacts, 'mx-sent')
        wait_for_screen(monitor, r'mock llm', 60, artifacts, 'mx-reply')

        # 3. Rollover: three keys held simultaneously must all register
        xdo('keydown', 'j', 'keydown', 'k', 'keydown', 'l')
        time.sleep(0.3)
        xdo('keyup', 'j', 'keyup', 'k', 'keyup', 'l')
        time.sleep(0.5)
        screen = monitor.screen_text().lower()
        got = ''.join(sorted(c for c in 'jkl' if c in screen))
        print(f"rollover registered: {got!r} (want 'jkl')")
        if got != 'jkl':
            raise AssertionError('rollover: not all simultaneous keys arrived')

        print('MATRIX/ROLLOVER: PASS')
        status = 0
    except AssertionError as e:
        print(f'MATRIX/ROLLOVER: FAIL\n{e}', file=sys.stderr)
    finally:
        stack.teardown()
    sys.exit(status)


if __name__ == '__main__':
    main()
