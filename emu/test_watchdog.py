#!/usr/bin/env python3
"""Response-watchdog test: a request that never gets a reply must time out
and return the client to Ready, rather than hanging forever.

Two scenarios:
  1. Chat: the mock (STALLTEST) accepts the request but never streams.
  2. Load: a relay between VICE and the proxy swallows the final (more=0)
     CONVERSATION_DATA frame of a bulk load - what the C64U modem does to
     the tail of a burst - so the client must time out, unfreeze the chat,
     and succeed on a retry.

The client is built with a short watchdog (WATCHDOG_UNITS=1, ~4s) so the
test is quick.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_e2e import (Stack, find_free_port, wait_for_port, proxy_python,
                      wait_for_screen, vice_tool, PROXY_DIR, REPO)
from vice_monitor import ViceMonitor

SYNC_BYTE = 0x42
MSG_CONVERSATION_DATA = 0x54


class FrameDropRelay(threading.Thread):
    """TCP relay VICE <-> proxy that drops the first CONVERSATION_DATA
    frame carrying more=0 (payload byte 1), then forwards everything."""

    def __init__(self, listen_port, proxy_port):
        super().__init__(daemon=True)
        self.listen_port = listen_port
        self.proxy_port = proxy_port
        self.dropped = False

    def run(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', self.listen_port))
        srv.listen(1)
        client, _ = srv.accept()
        proxy = socket.create_connection(('127.0.0.1', self.proxy_port))
        threading.Thread(target=self._pump_raw, args=(client, proxy),
                         daemon=True).start()
        self._pump_frames(proxy, client)

    def _pump_raw(self, src, dst):
        try:
            while data := src.recv(4096):
                dst.sendall(data)
        except OSError:
            pass

    def _pump_frames(self, src, dst):
        """Proxy->client: parse whole frames so one can be swallowed."""
        buf = bytearray()
        try:
            while data := src.recv(4096):
                buf += data
                while True:
                    frame = self._take_frame(buf)
                    if frame is None:
                        break
                    dst.sendall(frame)
        except OSError:
            pass

    def _take_frame(self, buf):
        # sync, type, len_lo+0x20, len_hi+0x20, payload, crc
        if len(buf) < 5:
            return None
        length = ((buf[2] - 0x20) & 0xFF) | (((buf[3] - 0x20) & 0xFF) << 8)
        end = 4 + length + 1
        if len(buf) < end:
            return None
        frame = bytes(buf[:end])
        del buf[:end]
        if (not self.dropped and frame[1] == MSG_CONVERSATION_DATA
                and length >= 2 and frame[4 + 1] == 0):  # more flag
            self.dropped = True
            print('  relay: dropped final CONVERSATION_DATA frame')
            return b''
        return frame


def seed_conversation(data_dir):
    """A conversation long enough that a load spans many frames; its
    updated_at is far in the future so it sorts newest in the browser."""
    (data_dir / 'conversations').mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'message {i:02d} ' + 'lorem ipsum ' * 32}
            for i in range(20)]
    conv = {'id': str(now), 'title': 'Watchdog Load Fixture',
            'auto_titled': False, 'created_at': now,
            'updated_at': now + 100000, 'chat': {'messages': msgs}}
    (data_dir / 'conversations' / f'{now}.json').write_text(json.dumps(conv))


def main():
    artifacts = REPO / 'emu' / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    data_dir = artifacts / 'data-watchdog'
    seed_conversation(data_dir)
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
                   C64LLM_DATA_DIR=str(data_dir))
        pport = find_free_port()
        stack.start('proxy', [proxy_python(), '-m', 'src.main', '--host',
                              '127.0.0.1', '--port', str(pport)],
                    cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(pport):
            raise AssertionError('proxy did not start')
        relay_port = find_free_port()
        relay = FrameDropRelay(relay_port, pport)
        relay.start()
        # F5 loads the conversation-manager module from disk in SOFT80
        # builds - mount a d64 carrying it
        d64 = artifacts / 'modules.d64'
        subprocess.run(
            [*vice_tool('c1541'), '-format', 'c64llm,01', 'd64', str(d64),
             '-write', str(REPO / 'c64_client/build/c64llm.prg.2'),
             'c64llm.2'],
            check=True, capture_output=True)
        mon_port = find_free_port()
        stack.start('vice', [
            *vice_tool('x64sc'), '-default', '-sounddev', 'dummy',
            '+confirmonexit',
            '-acia1', '-acia1mode', '0', '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            '-rsdev1', f'127.0.0.1:{relay_port}', '+rsdev1ip232',
            '-rsdev1baud', '9600', '-binarymonitor',
            '-binarymonitoraddress', f'ip4://127.0.0.1:{mon_port}',
            '-8', str(d64),
            '-autostartprgmode', '1',
            '-autostart', str(REPO / 'c64_client/build/c64llm.prg')])
        if not wait_for_port(mon_port, timeout=30):
            raise AssertionError('VICE monitor did not start')
        time.sleep(4)
        mon = ViceMonitor(port=mon_port, cols80=True)
        wait_for_screen(mon, r'ready\. type your message', 60,
                        artifacts, 'wd-ready')

        # Scenario 1 - chat: the mock accepts the request but never answers
        mon.keyboard_feed('stalltest please\r')
        # Proxy ACKs + "Contacting API..." fast, then silence -> watchdog
        wait_for_screen(mon, r'timed out', 40, artifacts, 'wd-timeout')
        print('WATCHDOG chat: PASS')

        # Scenario 2 - load: relay swallows the final frame; the client
        # must unfreeze and time out instead of hanging at 'Loading...'
        mon.keyboard_feed_petscii(b'\x87')  # F5: conversation browser
        wait_for_screen(mon, r'watchdog load fixture', 20, artifacts,
                        'wd-browser')
        mon.keyboard_feed('\r')
        wait_for_screen(mon, r'load incomplete', 40, artifacts,
                        'wd-load-timeout')
        if not relay.dropped:
            raise AssertionError('relay never dropped a frame - load '
                                 'timeout came from something else')
        print('WATCHDOG load: PASS (timed out on dropped final frame)')

        # And the retry (relay now forwards everything) must complete
        mon.keyboard_feed_petscii(b'\x87')
        wait_for_screen(mon, r'watchdog load fixture', 20, artifacts,
                        'wd-browser2')
        mon.keyboard_feed('\r')
        wait_for_screen(mon, r'conversation loaded', 60, artifacts,
                        'wd-load-retry')
        print('WATCHDOG load retry: PASS')
        status = 0
    except AssertionError as e:
        print(f'WATCHDOG: FAIL\n{e}', file=sys.stderr)
    finally:
        stack.teardown()
    sys.exit(status)


if __name__ == '__main__':
    main()
