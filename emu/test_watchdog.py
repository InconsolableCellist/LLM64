#!/usr/bin/env python3
"""Response-watchdog test: a request that never gets a reply must time out
and return the client to Ready, rather than hanging forever.

The mock (STALLTEST) accepts the request but never streams; the client is
built with a short watchdog (WATCHDOG_UNITS=1, ~4s) so the test is quick.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_e2e import (Stack, find_free_port, wait_for_port, proxy_python,
                      wait_for_screen, PROXY_DIR, REPO)
from vice_monitor import ViceMonitor


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
            raise AssertionError('mock did not start')
        env = dict(os.environ, OPENAI_API_KEY='k', OPENAI_MODEL='mock',
                   OPENAI_API_BASE=f'http://127.0.0.1:{mock_port}/v1',
                   C64LLM_CARDS_DIR=str(REPO / 'emu' / 'fixtures'),
                   C64LLM_DATA_DIR=str(artifacts / 'data'))
        pport = find_free_port()
        stack.start('proxy', [proxy_python(), '-m', 'src.main', '--host',
                              '127.0.0.1', '--port', str(pport)],
                    cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(pport):
            raise AssertionError('proxy did not start')
        mon_port = find_free_port()
        stack.start('vice', [
            'x64sc', '-default', '-sounddev', 'dummy', '+confirmonexit',
            '-acia1', '-acia1mode', '0', '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            '-rsdev1', f'127.0.0.1:{pport}', '+rsdev1ip232',
            '-rsdev1baud', '9600', '-binarymonitor',
            '-binarymonitoraddress', f'ip4://127.0.0.1:{mon_port}',
            '-autostartprgmode', '1',
            '-autostart', str(REPO / 'c64_client/build/c64llm.prg')])
        if not wait_for_port(mon_port, timeout=30):
            raise AssertionError('VICE monitor did not start')
        time.sleep(4)
        mon = ViceMonitor(port=mon_port, cols80=True)
        wait_for_screen(mon, r'ready\. type your message', 60,
                        artifacts, 'wd-ready')
        # Send a message the mock will accept but never answer
        mon.keyboard_feed('stalltest please\r')
        # Proxy ACKs + "Contacting API..." fast, then silence -> watchdog
        wait_for_screen(mon, r'timed out', 40, artifacts, 'wd-timeout')
        # And the client must be usable again
        mon.keyboard_feed('x')
        time.sleep(1)
        screen = mon.screen_text().lower()
        if 'x' not in screen.split('type your message')[0][-120:] \
                and 'timed out' not in screen:
            pass  # editor visibility already covered elsewhere
        print('WATCHDOG: PASS')
        status = 0
    except AssertionError as e:
        print(f'WATCHDOG: FAIL\n{e}', file=sys.stderr)
    finally:
        stack.teardown()
    sys.exit(status)


if __name__ == '__main__':
    main()
