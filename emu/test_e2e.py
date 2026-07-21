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
import shutil
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
PROXY_PORT = 6400  # default; override with --proxy-port
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


def parse_labels(path):
    """ld65 -Ln label file -> {symbol: address} (true link addresses)."""
    labels = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == 'al':
            labels[parts[2].lstrip('.')] = int(parts[1], 16)
    return labels


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



def wait_ready(monitor, timeout, artifacts, label):
    """Wait for the client's Ready status. The client shows a
    '[data loss ov=.. hw=.. cr=..]' banner when bytes were lost; the
    harness's own monitor pauses can cause 1-3 byte hw overruns in the
    emulator (no monitor exists on real hardware), so tolerate tiny
    hw/cr counts but fail on ring drops or anything larger."""
    screen = wait_for_screen(monitor, r'ready\.', timeout, artifacts, label)
    m = re.search(r'\[data loss ov=(..) hw=(..) cr=(..)\]', screen, re.I)
    if m:
        ov, hw, cr = (int(x, 16) for x in m.groups())
        if ov > 0 or hw + cr > 4:
            raise AssertionError(
                f'data loss beyond monitor-pause tolerance: ov={ov} '
                f'hw={hw} cr={cr} ({label})')
        print(f'  PASS: ready with tolerable monitor-artifact loss '
              f'(hw={hw} cr={cr}) ({label})')
    return screen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['direct', 'hayes'], default='direct')
    parser.add_argument('--prg', default=str(REPO / 'c64_client/build/c64llm.prg'))
    parser.add_argument('--timeout', type=float, default=90.0)
    parser.add_argument('--baud', type=int, default=9600)
    parser.add_argument('--acia-mode', type=int, default=0,
                        help='VICE -acia1mode: 0=plain 6551 (control $1E = '
                             '9600), 1=SwiftLink (clock doubled: $1E = '
                             '19200, matching real C64U hardware; pair '
                             'with --baud 19200)')
    parser.add_argument('--proxy-port', type=int, default=PROXY_PORT,
                        help='TCP port for the test proxy (use a free one if a live proxy runs on 6400)')
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
    parser.add_argument('--cols80', action='store_true',
                        help='Client built with MODE80=1: read the ASCII '
                             'shadow at $C000 instead of screen RAM')
    parser.add_argument('--tui', action='store_true',
                        help='Drive the interactive TUI via keyboard injection '
                             'instead of asserting the scripted debug session')
    args = parser.parse_args()

    artifacts = REPO / 'emu' / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)

    if not Path(args.prg).exists():
        sys.exit(f"PRG not found: {args.prg} (build it first)")
    proxy_port = args.proxy_port
    if port_in_use(proxy_port):
        sys.exit(f"Port {proxy_port} already in use - is another proxy "
                 f"running? (use --proxy-port to pick a free one)")

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
                # deterministic card set regardless of the user's cards/
                'C64LLM_CARDS_DIR': str(REPO / 'emu' / 'fixtures'),
                # keep test conversations out of the user's data dir
                'C64LLM_DATA_DIR': str(artifacts / 'data'),
            })
            # Deterministic 2-tune music library (urgent -> Pac-Man,
            # festive -> Astro Chase)
            shutil.copytree(REPO / 'emu' / 'fixtures' / 'sids',
                            artifacts / 'data' / 'sids',
                            dirs_exist_ok=True)
            # Image generation without the real API: every "generation"
            # returns this fixture
            env['C64LLM_IMG_FIXTURE'] = str(
                REPO / 'emu' / 'fixtures' / 'scene.png')
            # Claude Code mode drives a mock CLI (no real agent/API)
            env['C64LLM_CLAUDE_CMD'] = (
                f"{sys.executable} {REPO / 'emu' / 'mock_claude.py'}")
            print(f"mock LLM on :{mock_port}")

        # 2. Proxy
        stack.start('proxy', [
            proxy_python(), '-m', 'src.main',
            '--host', '127.0.0.1', '--port', str(proxy_port), '-v'],
            cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(proxy_port):
            raise AssertionError('proxy did not start (see artifacts/proxy.log)')
        print(f"proxy on :{proxy_port}")

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
            rsdev = ['-rsdev1', f'127.0.0.1:{proxy_port}', '+rsdev1ip232']

        # 3.5 Overlay-module disk: direct mode mounts a d64 on unit 8
        # carrying the config-editor module (c64llm.1), so the F1->E
        # module test exercises the real disk-load path. Built fresh in
        # artifacts per run - the editor's save writes into it.
        d64_path = None
        mod1 = Path(args.prg + '.1')
        mod2 = Path(args.prg + '.2')
        if mod1.exists() and shutil.which('c1541'):
            d64_path = artifacts / 'modules.d64'
            cmd = ['c1541', '-format', 'c64llm,01', 'd64', str(d64_path),
                   '-write', str(mod1), 'c64llm.1']
            if mod2.exists():
                cmd += ['-write', str(mod2), 'c64llm.2']
            if args.mode == 'hayes':
                # hayes boots from the DISK config (the real-hardware
                # path): blob = PRG header, magic, version, host[32],
                # port[6] - digits/dots are PETSCII-identical
                cfg = artifacts / 'test.cfg'
                cfg.write_bytes(b'\x00\x10\xc6\x01'
                                + b'127.0.0.1'.ljust(32, b'\x00')
                                + str(proxy_port).encode().ljust(6, b'\x00'))
                cmd += ['-write', str(cfg), 'c64llm.cfg']
            subprocess.run(cmd, check=True, capture_output=True)
            print(f"module disk: {d64_path.name}")

        # 4. VICE. No warp in hayes mode: the client's AT-response timeouts
        # are cycle-based, but tcpser answers in wall-clock time, so a warped
        # C64 gives up long before the modem replies.
        mon_port = find_free_port()
        speed = [] if (args.mode == 'hayes' or args.no_warp) else ['-warp']
        disk8 = ['-8', str(d64_path)] if d64_path else []
        stack.start('vice', [
            'x64sc', '-default', *speed, '-sounddev', 'dummy',
            '+confirmonexit',
            '-acia1', '-acia1mode', str(args.acia_mode),
            '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            *rsdev, '-rsdev1baud', str(args.baud),
            *disk8,
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
        monitor = ViceMonitor(port=mon_port, cols80=args.cols80)

        # 5. Assertions
        tag = f'{args.mode}-tui' if args.tui else args.mode
        if args.tui:
            # Interactive TUI: wait for the ready prompt, type a message,
            # watch it echo, stream, and return to ready.
            screen = wait_for_screen(monitor, r'ready\. type your message',
                                     args.timeout, artifacts, f'{tag}-ready')
            # Title bar shows the build hash (buildhash.h, generated by
            # the client Makefile) so a stale deploy is visible at a glance
            hashfile = REPO / 'c64_client' / 'include' / 'buildhash.h'
            m = re.search(r'"([0-9a-f]+\+?)"', hashfile.read_text())
            if m and m.group(1) not in screen:
                raise AssertionError(
                    f'build hash {m.group(1)!r} missing from title bar')
            print(f'  PASS: title bar build hash ({m.group(1)})')
            monitor.keyboard_feed('hello computer\r')
            wait_for_screen(monitor, r'> hello computer', 30,
                            artifacts, f'{tag}-echo')
            if not args.live:
                wait_for_screen(monitor, re.escape(args.expect), 60,
                                artifacts, f'{tag}-content')
            final = wait_ready(monitor, args.timeout, artifacts,
                               f'{tag}-done')
            # F7 = server-side /help streamed into the scrollback
            monitor.keyboard_feed_petscii(b'\x88')
            # phrase must survive 40-col wrapping: keep it short
            wait_for_screen(monitor, r'/findall <text>', 30,
                            artifacts, f'{tag}-help')
            final = wait_ready(monitor, 30, artifacts, f'{tag}-restore')
            if not args.live:
                # Conversation browser: F5, load newest (= this session).
                # The header draws before the list frames arrive (the
                # manager module requests the list only after its disk
                # load), and Return on an empty list is ignored - give
                # the frames time to land before pressing it.
                monitor.keyboard_feed_petscii(b'\x87')
                wait_for_screen(monitor, r'conversations \(return=load',
                                15, artifacts, f'{tag}-browser')
                time.sleep(2)
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
                wait_ready(monitor, 30, artifacts, f'{tag}-adventure-ready')

                if args.cols80:
                    # Streamed SID: /music picks Pac-Man (only urgent tune
                    # in the fixture library) and the client starts playing
                    # it: music_state == $FF, play vector = Pac-Man's
                    labels = parse_labels(
                        Path(args.prg).parent / 'labels.txt')
                    def music_word(name):
                        a = labels[name]
                        lo, hi = monitor.read_memory(a, a + 1)  # end incl.
                        return lo | (hi << 8)
                    monitor.keyboard_feed('/music urgent\r')
                    wait_for_screen(monitor, r'playing: pac-man', 30,
                                    artifacts, f'{tag}-music-cmd')
                    time.sleep(2)  # transfer is ~1s of wire time
                    state = monitor.read_memory(
                        labels['_music_state'], labels['_music_state'])[0]
                    if state != 0xFF:
                        raise AssertionError(
                            f'music_state={state:#x}, expected 0xff (ext)')
                    if music_word('_music_ext_play_addr') != 0xB07A:
                        raise AssertionError('play vector is not Pac-Man')
                    print('  PASS: /music transfer + play')

                    # LLM music directive: mock replies with
                    # [[MUSIC: festive]] which must be stripped from the
                    # visible text and switch the tune to Astro Chase
                    monitor.keyboard_feed('musictest\r')
                    screen = wait_for_screen(monitor, r'carnival begins', 60,
                                             artifacts,
                                             f'{tag}-music-directive')
                    if re.search(r'\[\[\s*music', screen, re.IGNORECASE):
                        raise AssertionError(
                            f'music directive leaked to screen\n{screen}')
                    # Wait for PLAYBACK, not just the vector: the client
                    # stores addresses at SID_BEGIN but only re-enters ext
                    # mode (state $FF) at SID_END, when it is idle again
                    deadline = time.time() + 30
                    while not (music_word('_music_ext_play_addr') == 0xB051
                               and monitor.read_memory(
                                   labels['_music_state'],
                                   labels['_music_state'])[0] == 0xFF):
                        if time.time() > deadline:
                            raise AssertionError(
                                'tune never switched to Astro Chase')
                        time.sleep(1)
                    time.sleep(1)  # let the status line settle
                    print('  PASS: [[MUSIC]] directive switched tune')

                    # Scene illustration: [[IMAGE:]] directive (ask mode)
                    # parks a suggestion; /pic streams the converted
                    # fixture into the bitmap at $E000/$CC00
                    expected = subprocess.run(
                        [proxy_python(), '-c',
                         'import sys; sys.path.insert(0, ".");'
                         'from src.imaging import convert_to_c64_mc;'
                         'from PIL import Image;'
                         f'i = Image.open("{REPO}/emu/fixtures/scene.png");'
                         'b, s, c, bg = convert_to_c64_mc(i, caption='
                         '"The crystal deep hums with cold light.");'
                         'sys.stdout.write(b[:16].hex() + s[-16:].hex()'
                         ' + c[-16:].hex())'],
                        cwd=str(PROXY_DIR), capture_output=True, text=True
                    ).stdout.strip()
                    assert len(expected) == 96, f'converter probe: {expected}'
                    want_colram = [int(expected[64 + i*2:66 + i*2], 16) & 0x0F
                                   for i in range(16)]
                    monitor.keyboard_feed('pictest\r')
                    wait_for_screen(monitor, r'cavern opens', 60,
                                    artifacts, f'{tag}-img-suggest')
                    # Persistent '!P' indicator appears with the suggestion
                    wait_for_screen(monitor, r'!p', 30,
                                    artifacts, f'{tag}-img-hint')
                    # ...plus the rainbow attention line in the chat
                    wait_for_screen(monitor, r'scene is ready', 15,
                                    artifacts, f'{tag}-img-announce')
                    print('  PASS: pic-pending indicator + announcement')
                    monitor.keyboard_feed('/pic\r')
                    def img_shown():
                        return monitor.read_memory(
                            labels['_img_shown'], labels['_img_shown'])[0]
                    deadline = time.time() + 60
                    while True:
                        got = (bytes(monitor.read_memory(0xE000, 0xE00F,
                                                         bank=1)).hex()
                               + bytes(monitor.read_memory(0xCFD8, 0xCFE7,
                                                           bank=1)).hex())
                        colram = [b & 0x0F for b in monitor.read_memory(
                            0xDBD8, 0xDBE7, bank=0)]
                        # img_shown flips only when IMG_END verified the
                        # byte count: the one non-racy completion signal
                        if (got == expected[:64] and colram == want_colram
                                and img_shown()):
                            break
                        if time.time() > deadline:
                            raise AssertionError(
                                f'image never completed (shown='
                                f'{img_shown()})\nwant {expected[:64]}\n'
                                f'got  {got} colram {colram}')
                        time.sleep(2)
                    print('  PASS: image streamed to bitmap+matrix+colram')
                    # Music no longer pauses for transfers (the ACIA-
                    # blinding theory was disproven): the tune plays
                    # through transfer and display alike
                    if monitor.read_memory(
                            labels['_music_state'],
                            labels['_music_state'])[0] != 0xFF:
                        raise AssertionError(
                            'ext music stopped during image')
                    print('  PASS: music plays through the image')
                    monitor.keyboard_feed(' ')   # dismiss
                    screen = wait_for_screen(monitor, r'> pictest', 15,
                                             artifacts, f'{tag}-img-dismiss')
                    if re.search(r'!p', screen):
                        raise AssertionError(
                            f'pic indicator not cleared after /pic\n{screen}')
                    wait_for_screen(monitor, r'the crystal deep hums', 10,
                                    artifacts, f'{tag}-caption')
                    # ...and restart when the picture closes
                    deadline = time.time() + 15
                    while monitor.read_memory(
                            labels['_music_state'],
                            labels['_music_state'])[0] != 0xFF:
                        if time.time() > deadline:
                            raise AssertionError(
                                'ext music not restarted after image')
                        time.sleep(1)
                    print('  PASS: image dismissed, chat restored, '
                          'caption shown, music paused + resumed')

                    # Picture browser: list + re-show from cache
                    monitor.keyboard_feed('/pics\r')
                    wait_for_screen(monitor, r'1\. a vast crystal cavern',
                                    30, artifacts, f'{tag}-pics-list')
                    monitor.keyboard_feed('/pic 1\r')
                    wait_for_screen(monitor, r'showing: the crystal deep',
                                    30, artifacts, f'{tag}-pic-reshow')
                    deadline = time.time() + 60
                    while True:
                        got = bytes(monitor.read_memory(
                            0xE000, 0xE00F, bank=1)).hex()
                        if got == expected[:32] and img_shown():
                            break
                        if time.time() > deadline:
                            raise AssertionError('cached re-show never '
                                                 f'landed (got {got})')
                        time.sleep(2)
                    monitor.keyboard_feed(' ')
                    wait_for_screen(monitor, r'> /pics', 15,
                                    artifacts, f'{tag}-pic-reshow-dismiss')
                    print('  PASS: /pics browser + cached re-show')

                    # Bare /pic with nothing pending: the proxy asks the
                    # model to describe the scene, then generates
                    monitor.keyboard_feed('/pic\r')
                    wait_for_screen(monitor, r'illustrating:', 60,
                                    artifacts, f'{tag}-pic-derive')
                    deadline = time.time() + 60
                    while not img_shown():
                        if time.time() > deadline:
                            raise AssertionError(
                                'derived-prompt image never completed')
                        time.sleep(2)
                    monitor.keyboard_feed(' ')
                    wait_for_screen(monitor, r'> /pic', 15,
                                    artifacts, f'{tag}-pic-derive-dismiss')
                    print('  PASS: bare /pic derived a scene prompt')

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

                # Directives are filtered in roleplay too: the musictest
                # reply carries [[MUSIC: festive]] which must not leak
                # (the tune change itself may be rate-limited here -
                # stripping is what's asserted)
                monitor.keyboard_feed('musictest\r')
                rp = wait_for_screen(monitor, r'carnival begins', 60,
                                     artifacts, f'{tag}-rp-music')
                if re.search(r'\[\[\s*music', rp, re.IGNORECASE):
                    raise AssertionError(
                        f'directive leaked in roleplay\n{rp}')
                print('  PASS: roleplay music directive stripped')

                # Page up/down (F4/F6) through a long response
                monitor.keyboard_feed('longtest\r')
                wait_for_screen(monitor, r'number\s+60', 120,
                                artifacts, f'{tag}-longdone')
                wait_ready(monitor, 60, artifacts, f'{tag}-longready')
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
                wait_for_screen(monitor, r'number\s+60', 15,
                                artifacts, f'{tag}-pagedown')

                # /find + /history: proxy-side search and paging over the
                # full stored conversation (scrollback is just the view)
                monitor.keyboard_feed('/find sentence\r')
                wait_for_screen(monitor, r'match\(es\) for "sentence"', 30,
                                artifacts, f'{tag}-find')
                wait_for_screen(monitor, r'\[\d+\] p\d+:', 10,
                                artifacts, f'{tag}-find-hit')
                wait_ready(monitor, 15, artifacts, f'{tag}-find-done')
                monitor.keyboard_feed('/history 1\r')
                wait_for_screen(monitor, r'page 1/\d+ \(msgs 1-', 30,
                                artifacts, f'{tag}-history')
                wait_for_screen(monitor, r'ahoy', 10,
                                artifacts, f'{tag}-history-content')
                print('  PASS: /find + /history paging')

                # /findall searches every saved conversation (wait out
                # the /history stream first: Return while receiving is
                # swallowed as 'Busy')
                wait_ready(monitor, 15, artifacts, f'{tag}-history-done')
                monitor.keyboard_feed('/findall sentence\r')
                wait_for_screen(monitor,
                                r'conversations mentioning "sentence"', 30,
                                artifacts, f'{tag}-findall')
                wait_for_screen(monitor, r'\d+ hits?\)', 10,
                                artifacts, f'{tag}-findall-hit')
                print('  PASS: /findall cross-conversation search')

                # Editor must be VISIBLE: glyph colors live in the matrix,
                # which the ASCII shadow can't see - check color RAM/matrix
                # for the editor rows directly (yellow = 7)
                monitor.keyboard_feed('q')
                time.sleep(1)
                if args.cols80:
                    mrow = monitor.read_memory(0xCC00 + 21 * 40,
                                               0xCC00 + 21 * 40 + 4)
                    ok = all((b >> 4) == 0x07 for b in mrow)
                else:
                    mrow = monitor.read_memory(0xD800 + 21 * 40,
                                               0xD800 + 21 * 40 + 4)
                    ok = all((b & 0x0F) == 0x07 for b in mrow)
                if not ok:
                    raise AssertionError(
                        f'editor color wrong (not yellow): {bytes(mrow).hex()}')
                print('  PASS: editor color is yellow (visible)')
                monitor.keyboard_feed_petscii(b'\x14')  # DEL the q

                # Long input: 260 chars scrolls the editor viewport and
                # sends as a single frame (would have been cut at 120)
                monitor.keyboard_feed('say ok: ' + 'x' * 252 + '\r')
                wait_for_screen(monitor, r'x{30}', 60,
                                artifacts, f'{tag}-longinput')
                wait_ready(monitor, args.timeout, artifacts,
                           f'{tag}-longinput-done')

                # Music toggle. In cols80 a streamed SID is still playing
                # from the music tests: S first stops it, then cycles the
                # pattern tunes off->tune1->tune2->off as always
                monitor.keyboard_feed_petscii(b'\x85')
                if args.cols80:
                    wait_for_screen(monitor, r'S  music: streamed', 15,
                                    artifacts, f'{tag}-menu-music')
                    monitor.keyboard_feed('s')
                    wait_for_screen(monitor, r'music off', 15,
                                    artifacts, f'{tag}-music-ext-stop')
                    monitor.keyboard_feed_petscii(b'\x85')
                    wait_for_screen(monitor, r'S  music \(off\)', 15,
                                    artifacts, f'{tag}-menu-music-off')
                else:
                    wait_for_screen(monitor, r'S  music \(off\)', 15,
                                    artifacts, f'{tag}-menu-music')
                monitor.keyboard_feed('s')
                wait_for_screen(monitor, r'music: dungeon depths', 15,
                                artifacts, f'{tag}-music-on')
                monitor.keyboard_feed_petscii(b'\x85')
                monitor.keyboard_feed('s')
                wait_for_screen(monitor, r'music: northward road', 15,
                                artifacts, f'{tag}-music-2')
                monitor.keyboard_feed_petscii(b'\x85')
                monitor.keyboard_feed('s')
                wait_for_screen(monitor, r'music off', 15,
                                artifacts, f'{tag}-music-off')

                # F1 menu -> M lists models (numbered); /model 2 picks
                monitor.keyboard_feed_petscii(b'\x85')  # F1
                wait_for_screen(monitor, r'models \(/model', 15,
                                artifacts, f'{tag}-menu')
                monitor.keyboard_feed('m')
                wait_for_screen(monitor, r'2\. mock-large', 30,
                                artifacts, f'{tag}-models')
                wait_ready(monitor, 15, artifacts, f'{tag}-models-done')
                monitor.keyboard_feed('/model 2\r')
                final = wait_for_screen(monitor, r'now using: mock-large',
                                        15, artifacts, f'{tag}-modelset')

                # Overlay module system (SOFT80): F1 -> E pulls the
                # config editor from the d64 on unit 8. Segment OVERLAY1
                # is NOT in the resident PRG, so the editor appearing at
                # all proves the disk load; the save is verified against
                # the d64 after VICE exits.
                if d64_path and args.cols80:
                    monitor.keyboard_feed_petscii(b'\x85')  # F1
                    wait_for_screen(monitor, r'server config', 15,
                                    artifacts, f'{tag}-menu-mod')
                    monitor.keyboard_feed('e')
                    wait_for_screen(monitor, r'host:', 20,
                                    artifacts, f'{tag}-mod-editor')
                    print('  PASS: config module loaded from drive 8')
                    monitor.keyboard_feed_petscii(b'\x14' * 24)  # clear host
                    monitor.keyboard_feed('10.0.0.7')
                    wait_for_screen(monitor, r'host: 10\.0\.0\.7', 15,
                                    artifacts, f'{tag}-mod-host')
                    monitor.keyboard_feed('\r')                  # -> port
                    monitor.keyboard_feed_petscii(b'\x14' * 8)   # clear port
                    monitor.keyboard_feed('6502\r')              # save
                    wait_for_screen(monitor, r'config saved', 15,
                                    artifacts, f'{tag}-mod-saved')
                    print('  PASS: config editor saved to drive 8')
                    # Reopen: module reloads, fields show the live values
                    monitor.keyboard_feed_petscii(b'\x85')
                    time.sleep(1)
                    monitor.keyboard_feed('e')
                    wait_for_screen(monitor, r'port: 6502', 20,
                                    artifacts, f'{tag}-mod-reopen')
                    monitor.keyboard_feed('\r\r')                # save again
                    wait_for_screen(monitor, r'config saved', 15,
                                    artifacts, f'{tag}-mod-resave')
                    print('  PASS: module reload shows live config')

                # Conversation manager (module #2): F5 loads it from
                # disk; star the newest conversation (refresh shows the
                # '*' prefix), unstar it, then delete an OLDER one (the
                # newest is needed by the roleplay-restore test below).
                if d64_path and args.cols80:
                    monitor.keyboard_feed_petscii(b'\x87')  # F5
                    wait_for_screen(monitor,
                                    r'conversations \(return=load, d=del',
                                    20, artifacts, f'{tag}-mgr-open')
                    print('  PASS: conversation manager loaded from disk')
                    time.sleep(2)  # keys during 'loading...' are ignored
                    monitor.keyboard_feed('s')
                    wait_for_screen(monitor, r'star toggled', 15,
                                    artifacts, f'{tag}-mgr-star')
                    wait_for_screen(monitor, r'\*', 15,
                                    artifacts, f'{tag}-mgr-starred')
                    print('  PASS: star toggle + starred prefix')
                    monitor.keyboard_feed('s')  # unstar (row 0 again)
                    wait_for_screen(monitor, r'star toggled', 15,
                                    artifacts, f'{tag}-mgr-unstar')
                    time.sleep(2)  # list refresh settles
                    monitor.keyboard_feed_petscii(b'\x11')  # crsr down
                    monitor.keyboard_feed('d')
                    wait_for_screen(monitor, r'delete selected', 15,
                                    artifacts, f'{tag}-mgr-delconfirm')
                    monitor.keyboard_feed('y')
                    wait_for_screen(monitor, r'deleted\.', 15,
                                    artifacts, f'{tag}-mgr-deleted')
                    print('  PASS: delete with confirm')
                    time.sleep(2)  # post-delete refresh settles
                    monitor.keyboard_feed_petscii(b'\x87')  # F5 closes
                    wait_ready(monitor, 15, artifacts, f'{tag}-mgr-close')

                # Roleplay restore: /chat leaves the card, reloading the
                # conversation (newest saved - the empty chat one is
                # in-memory only) must bring the character back via the
                # 'char' meta
                monitor.keyboard_feed('/chat\r')
                wait_for_screen(monitor, r'chat mode', 15,
                                artifacts, f'{tag}-tochat')
                # Loads must silence a playing tune: its SEI windows
                # corrupt the incoming frames on real hardware (big
                # load stalled at 'Loading... 19' in the field)
                if args.cols80:
                    monitor.keyboard_feed('/music urgent\r')
                    wait_for_screen(monitor, r'playing: pac-man', 30,
                                    artifacts, f'{tag}-preload-music')
                    deadline = time.time() + 30
                    while monitor.read_memory(
                            labels['_music_state'],
                            labels['_music_state'])[0] != 0xFF:
                        if time.time() > deadline:
                            raise AssertionError(
                                'preload tune never started')
                        time.sleep(1)
                monitor.keyboard_feed_petscii(b'\x87')  # F5 browser
                wait_for_screen(monitor, r'conversations \(return=load',
                                15, artifacts, f'{tag}-rp-browser')
                time.sleep(2)  # list frames land after the header draws
                monitor.keyboard_feed('\r')
                wait_for_screen(monitor, r'conversation loaded', 30,
                                artifacts, f'{tag}-rp-loadstatus')
                # The newest message is the ~3KB longtest reply: its
                # 1000-char clip arrives SPLIT across frames (client
                # buffer is 512) and ends with the truncation marker.
                # This is the assert that was missing when oversized
                # single frames broke loads in the field.
                wait_for_screen(monitor, r'/history shows the rest', 10,
                                artifacts, f'{tag}-rp-longmsg')
                print('  PASS: long message split across frames + marker')
                if args.cols80:
                    if monitor.read_memory(
                            labels['_music_state'],
                            labels['_music_state'])[0] != 0:
                        raise AssertionError(
                            'music not silenced by conversation load')
                    print('  PASS: load silences a playing tune')
                monitor.keyboard_feed('/mode\r')
                final = wait_for_screen(monitor,
                                        r'mode: roleplay: captain', 15,
                                        artifacts, f'{tag}-rp-restored')
                print('  PASS: roleplay mode restored on load')

                # Claude Code mode: /code starts the (mock) CLI session,
                # an instruction produces a tool call + y/n permission
                # prompt, and 'y' lets it finish
                monitor.keyboard_feed('/code\r')
                wait_for_screen(monitor, r'claude code ready', 30,
                                artifacts, f'{tag}-code-start')
                monitor.keyboard_feed('make a file\r')
                wait_for_screen(monitor, r'allow write hello.txt', 30,
                                artifacts, f'{tag}-code-perm')
                print('  PASS: Claude Code tool permission prompt')
                monitor.keyboard_feed('y\r')
                final = wait_for_screen(monitor, r'done, wrote hello',
                                        30, artifacts, f'{tag}-code-done')
                print('  PASS: Claude Code approval completes the turn')
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

        # The config editor's save must have landed on the d64 itself:
        # read c64llm.cfg back out and check the blob (magic C6 01,
        # host at +2, port at +34, NUL-padded PETSCII).
        if d64_path and args.tui and args.cols80:
            saved = artifacts / 'saved.cfg'
            subprocess.run(
                ['c1541', str(d64_path), '-read', 'c64llm.cfg', str(saved)],
                check=True, capture_output=True)
            blob = saved.read_bytes()[2:]  # skip the PRG load-address header
            host = blob[2:34].rstrip(b'\0').decode('ascii', 'replace')
            port = blob[34:40].rstrip(b'\0').decode('ascii', 'replace')
            if blob[:2] != b'\xc6\x01' or host != '10.0.0.7' or port != '6502':
                raise AssertionError(
                    f'cfg on disk wrong: magic={blob[:2].hex()} '
                    f'host={host!r} port={port!r}')
            print('  PASS: c64llm.cfg on the d64 holds the edited config')

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
