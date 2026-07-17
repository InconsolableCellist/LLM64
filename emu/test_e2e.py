#!/usr/bin/env python3
"""End-to-end automated test: mock LLM -> proxy -> VICE-emulated C64 client.

Boots the whole stack, autostarts the C64 client PRG in x64sc, and asserts
progress by reading the emulated screen RAM through the VICE binary monitor.
Produces a screen dump and PNG screenshot under emu/artifacts/.

Modes:
  direct  ACIA pipe wired straight to the proxy (client built CONNECT=direct)
  hayes   tcpser emulates a Hayes modem (client built CONNECT=hayes,
          SERVER_IP=127.0.0.1) — mirrors the real C64 Ultimate flow.

Exit code 0 = pass.
"""

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vice_monitor import ViceMonitor, ViceMonitorError

REPO = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO / 'c64llm_proxy'
PROXY_PORT = 6400
TCPSER_PORT = 25232


def find_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_for_port(port, timeout=15.0, host='127.0.0.1'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def port_in_use(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port))
            return False
        except OSError:
            return True


class Stack:
    """Manages the subprocess stack and tears it down in reverse order."""

    def __init__(self, artifacts):
        self.procs = []
        self.artifacts = artifacts
        self.logs = {}

    def start(self, name, cmd, **kwargs):
        log = open(self.artifacts / f'{name}.log', 'wb')
        self.logs[name] = log
        proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, **kwargs)
        self.procs.append((name, proc))
        return proc

    def teardown(self):
        for name, proc in reversed(self.procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
        for log in self.logs.values():
            log.close()


def proxy_python():
    venv = PROXY_DIR / '.venv' / 'bin' / 'python'
    return str(venv) if venv.exists() else sys.executable


POLL_INTERVAL = float(os.environ.get('E2E_POLL_INTERVAL', '0.5'))


def wait_for_screen(monitor, pattern, timeout, artifacts, label):
    """Poll screen RAM until regex `pattern` matches (case-insensitive)."""
    regex = re.compile(pattern, re.IGNORECASE)
    deadline = time.time() + timeout
    last_screen = ''
    while time.time() < deadline:
        try:
            last_screen = monitor.screen_text()
        except ViceMonitorError as e:
            print(f"  monitor hiccup: {e}")
            time.sleep(POLL_INTERVAL)
            continue
        if regex.search(last_screen):
            print(f"  PASS: found /{pattern}/ ({label})")
            return last_screen
        time.sleep(POLL_INTERVAL)
    (artifacts / f'fail-{label}.txt').write_text(last_screen)
    raise AssertionError(
        f"timeout ({timeout}s) waiting for /{pattern}/ ({label}).\n"
        f"--- last screen ---\n{last_screen}\n-------------------")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['direct', 'hayes'], default='direct')
    parser.add_argument('--prg', default=str(REPO / 'c64_client/build/c64llm.prg'))
    parser.add_argument('--timeout', type=float, default=90.0)
    parser.add_argument('--baud', type=int, default=9600)
    parser.add_argument('--live', action='store_true',
                        help='Use the real API from config.toml instead of the mock')
    parser.add_argument('--expect', default='mock llm',
                        help='Text that must appear on the final screen')
    parser.add_argument('--assert-all-chunks', action='store_true',
                        help='Assert every chunk of the mock LONGTEST '
                             'response arrived intact')
    parser.add_argument('--no-warp', action='store_true',
                        help='Run the emulator at real C64 speed (exposes '
                             'wall-clock timing issues that warp hides)')
    parser.add_argument('--tui', action='store_true',
                        help='Drive the interactive TUI via keyboard injection '
                             'instead of asserting the scripted debug session')
    args = parser.parse_args()

    artifacts = REPO / 'emu' / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)

    if not Path(args.prg).exists():
        sys.exit(f"PRG not found: {args.prg} (build it first)")
    if port_in_use(PROXY_PORT):
        sys.exit(f"Port {PROXY_PORT} already in use - is another proxy running?")

    stack = Stack(artifacts)
    status = 1
    try:
        # 1. Mock LLM (unless testing live)
        env = dict(os.environ)
        if not args.live:
            mock_port = find_free_port()
            stack.start('mock_llm', [
                sys.executable, str(REPO / 'emu/mock_llm.py'),
                '--port', str(mock_port)])
            if not wait_for_port(mock_port):
                raise AssertionError('mock LLM did not start')
            env.update({
                'OPENAI_API_KEY': 'mock-key',
                'OPENAI_API_BASE': f'http://127.0.0.1:{mock_port}/v1',
                'OPENAI_MODEL': 'mock',
            })
            print(f"mock LLM on :{mock_port}")

        # 2. Proxy
        stack.start('proxy', [
            proxy_python(), '-m', 'src.main',
            '--host', '127.0.0.1', '--port', str(PROXY_PORT), '-v'],
            cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(PROXY_PORT):
            raise AssertionError('proxy did not start (see artifacts/proxy.log)')
        print(f"proxy on :{PROXY_PORT}")

        # 3. tcpser (hayes mode only): VICE connects to it via ip232, and it
        #    answers AT commands / dials the proxy like the Ultimate's modem.
        if args.mode == 'hayes':
            stack.start('tcpser', [
                'tcpser', '-v', str(TCPSER_PORT), '-s', str(args.baud),
                '-p', str(find_free_port()),  # inbound-call port; default 6400 collides
                '-tSs'])
            if not wait_for_port(TCPSER_PORT):
                raise AssertionError('tcpser did not start')
            print(f"tcpser on :{TCPSER_PORT}")
            rsdev = [f'-rsdev1', f'127.0.0.1:{TCPSER_PORT}', '-rsdev1ip232']
        else:
            rsdev = ['-rsdev1', f'127.0.0.1:{PROXY_PORT}', '+rsdev1ip232']

        # 4. VICE. No warp in hayes mode: the client's AT-response timeouts
        # are cycle-based, but tcpser answers in wall-clock time, so a warped
        # C64 gives up long before the modem replies.
        mon_port = find_free_port()
        speed = [] if (args.mode == 'hayes' or args.no_warp) else ['-warp']
        stack.start('vice', [
            'x64sc', '-default', *speed, '-sounddev', 'dummy',
            '+confirmonexit',
            '-acia1', '-acia1mode', '0', '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            *rsdev, '-rsdev1baud', str(args.baud),
            '-binarymonitor',
            '-binarymonitoraddress', f'ip4://127.0.0.1:{mon_port}',
            '-exitscreenshot', str(artifacts / f'{args.mode}-final.png'),
            '-autostartprgmode', '1',  # inject PRG into RAM: deterministic
            '-autostart', args.prg])
        if not wait_for_port(mon_port, timeout=30):
            raise AssertionError('VICE binary monitor did not come up')
        print(f"VICE up, binary monitor on :{mon_port}")

        # Let autostart finish before pausing the machine with monitor reads
        time.sleep(4)
        monitor = ViceMonitor(port=mon_port)

        # 5. Assertions
        tag = f'{args.mode}-tui' if args.tui else args.mode
        if args.tui:
            # Interactive TUI: wait for the ready prompt, type a message,
            # watch it echo, stream, and return to ready.
            wait_for_screen(monitor, r'ready\. type your message',
                            args.timeout, artifacts, f'{tag}-ready')
            monitor.keyboard_feed('hello computer\r')
            wait_for_screen(monitor, r'> hello computer', 30,
                            artifacts, f'{tag}-echo')
            if not args.live:
                wait_for_screen(monitor, re.escape(args.expect), 60,
                                artifacts, f'{tag}-content')
            final = wait_for_screen(monitor, r'ready\. type your message',
                                    args.timeout, artifacts, f'{tag}-done')
            # Help overlay round-trip (F7, then any key to close)
            monitor.keyboard_feed_petscii(b'\x88')
            wait_for_screen(monitor, r'press any key to close', 15,
                            artifacts, f'{tag}-help')
            monitor.keyboard_feed(' ')
            final = wait_for_screen(monitor, r'> hello computer', 15,
                                    artifacts, f'{tag}-restore')
            if not args.live:
                # Conversation browser: F5, load newest (= this session)
                monitor.keyboard_feed_petscii(b'\x87')
                wait_for_screen(monitor, r'conversations \(return=load',
                                15, artifacts, f'{tag}-browser')
                monitor.keyboard_feed('\r')
                wait_for_screen(monitor, r'conversation loaded', 30,
                                artifacts, f'{tag}-loadstatus')
                wait_for_screen(monitor, r'> hello computer', 15,
                                artifacts, f'{tag}-loaded')

                # Adventure mode: kickoff goes through the API (mock
                # answers the hidden 'Begin the adventure' message)
                monitor.keyboard_feed('/adventure\r')
                wait_for_screen(monitor, r'dark room', 60,
                                artifacts, f'{tag}-adventure')
                wait_for_screen(monitor, r'ready\. type your message', 30,
                                artifacts, f'{tag}-adventure-ready')

                # Character cards: list, then load the example card
                monitor.keyboard_feed('/chars\r')
                wait_for_screen(monitor, r'captain byte', 30,
                                artifacts, f'{tag}-chars')
                monitor.keyboard_feed('/char captain\r')
                wait_for_screen(monitor, r'ahoy', 30,
                                artifacts, f'{tag}-greeting')

                # Roleplay sampling params reach the API (mock echoes them)
                monitor.keyboard_feed('paramtest\r')
                wait_for_screen(monitor, r'temp 1\.0 topk 64', 60,
                                artifacts, f'{tag}-sampling')

                # Page up/down (F4/F6) through a long response
                monitor.keyboard_feed('longtest\r')
                wait_for_screen(monitor, r'number\s+60', 120,
                                artifacts, f'{tag}-longdone')
                wait_for_screen(monitor, r'ready\. type your message', 30,
                                artifacts, f'{tag}-longready')
                monitor.keyboard_feed_petscii(b'\x8a')  # F4: page up
                time.sleep(1)
                paged = monitor.screen_text()
                if re.search(r'number\s+60', paged, re.IGNORECASE):
                    raise AssertionError('F4 did not page up (tail still '
                                         f'visible)\n{paged}')
                if not re.search(r'sentence number', paged, re.IGNORECASE):
                    raise AssertionError(f'F4 lost the content\n{paged}')
                print('  PASS: F4 paged up')
                monitor.keyboard_feed_petscii(b'\x8b')  # F6: page down
                final = wait_for_screen(monitor, r'number\s+60', 15,
                                        artifacts, f'{tag}-pagedown')
        else:
            # Scripted debug session runs in warp faster than we can poll,
            # so assert on the durable end state: the client parks on
            # "Test complete!" with the streamed response still on screen.
            final = wait_for_screen(monitor, r'test complete', args.timeout,
                                    artifacts, f'{tag}-complete')
            if not args.live:
                final = wait_for_screen(monitor, re.escape(args.expect), 10,
                                        artifacts, f'{tag}-content')
                wait_for_screen(monitor, r'crc fails: 00', 10,
                                artifacts, f'{tag}-crc')
                if args.assert_all_chunks:
                    import mock_llm
                    n = ((len(mock_llm.LONG_RESPONSE)
                          + mock_llm.CHUNK_SIZE - 1)
                         // mock_llm.CHUNK_SIZE)
                    pat = f'chunks: {n >> 8:02x} {n & 0xff:02x}'
                    wait_for_screen(monitor, re.escape(pat), 10,
                                    artifacts, f'{tag}-allchunks')

        (artifacts / f'{args.mode}-final.txt').write_text(final)
        print(f"\n--- final screen ---\n{final}\n--------------------")

        monitor.quit()
        time.sleep(2)  # let -exitscreenshot write
        print(f"\nE2E {args.mode} mode: PASS")
        status = 0

    except AssertionError as e:
        print(f"\nE2E {args.mode} mode: FAIL\n{e}", file=sys.stderr)
        for name in ('proxy', 'vice', 'tcpser'):
            log = artifacts / f'{name}.log'
            if log.exists():
                tail = log.read_text(errors='replace').splitlines()[-15:]
                print(f"\n--- {name}.log (tail) ---", file=sys.stderr)
                print('\n'.join(tail), file=sys.stderr)
    finally:
        stack.teardown()

    sys.exit(status)


if __name__ == '__main__':
    main()
