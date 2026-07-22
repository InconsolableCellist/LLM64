#!/usr/bin/env python3
"""F1 server-fed menu: a minimal boot-and-press-F1 harness.

Written to chase a dead menu on real hardware that turned out NOT to be
a client bug at all - the failing disks came from a tree another session
was rebuilding underneath them (HANDOFF.md, RETRACTED). The harness is
kept because it is the cheapest way to exercise the module-load path in
isolation: boot, press F1, report whether the panel populated.

    python3 emu/repro_menu.py --mode direct
    python3 emu/repro_menu.py --mode hayes     # via tcpser

It did find one thing worth knowing. At --baud 19200 --acia-mode 1 (the
SwiftLink clock-doubled setup that matches the C64U) the menu stays on
"fetching from server..." in BOTH transports - and it does so on
b9a71b0 too, a build proven good on real hardware, so treat it as a VICE
ACIA artifact rather than a client fault. The e2e only ever runs 9600,
which is why nothing else shows it.

Build the matching client first (MODE80=1 is required - the server-fed
menu module only exists in the soft-80 build; the 40-column build falls
back to a resident text menu):

    make -C c64_client clean && make -C c64_client CONNECT=direct MODE80=1
    make -C c64_client clean && \
        make -C c64_client CONNECT=hayes SERVER_IP=127.0.0.1 \
                           SERVER_PORT=6464 MODE80=1

The overlay-module d64 is attached to unit 8 in both modes: the F1 menu
is module c64llm.4, LOADED FROM DISK. Without the disk the client falls
back to the resident menu and nothing is being tested.

Exit 0 = the menu rendered.  Exit 1 = it did not.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vice_monitor import ViceMonitor, ViceMonitorError
# Reuse the e2e machinery verbatim - importing is safe, test_e2e only
# calls main() under __main__.
from test_e2e import (
    REPO, PROXY_DIR, TCPSER_PORT, Stack, find_free_port, have_vice_tool,
    port_in_use, proxy_python, vice_tool, wait_for_port, wait_for_screen,
    wait_ready,
)

# The proxy port the repo's tests use. 6400 belongs to the live proxy.
TESTPORT = 6464

# What the menu module puts on screen (mod_menu.c)
TITLE = 'c64 llm menu'                 # panel title, drawn immediately
FETCHING = 'fetching from server'      # placeholder until MENU_LIST lands
ENTRY = 'copy client disk'             # a menu entry present in every mode


def connect_monitor(port, tries=10):
    """VICE sometimes refuses/drops the binary-monitor socket; retry."""
    for attempt in range(tries):
        try:
            return ViceMonitor(port=port, cols80=True)
        except OSError as e:
            if attempt == tries - 1:
                raise AssertionError(f'monitor connect failed: {e}')
            time.sleep(1)


def poll_menu(monitor, mon_port, timeout, artifacts):
    """Watch the screen for the menu panel. Returns (verdict, screen)."""
    deadline = time.time() + timeout
    saw_panel = False
    saw_fetching = False
    drops = 0
    screen = ''
    while time.time() < deadline:
        try:
            screen = monitor.screen_text()
        except (ViceMonitorError, OSError) as e:
            # VICE drops the monitor socket in some failure modes; the
            # emulator itself is usually still running, so reconnect and
            # keep watching rather than losing the run.
            drops += 1
            print(f'  monitor hiccup ({drops}): {e} - reconnecting')
            monitor.close()
            time.sleep(1)
            try:
                monitor = connect_monitor(mon_port, tries=3)
            except AssertionError:
                print('  monitor gone for good (VICE dead?)')
                return ('MONITOR LOST - VICE/monitor died mid-poll', screen)
            continue
        low = screen.lower()
        if TITLE in low and not saw_panel:
            saw_panel = True
            print(f'  [{time.time() - (deadline - timeout):5.1f}s] '
                  f'panel title drew')
        if FETCHING in low and not saw_fetching:
            saw_fetching = True
            print(f'  [{time.time() - (deadline - timeout):5.1f}s] '
                  f'"fetching from server..." shown')
        if ENTRY in low:
            print(f'  [{time.time() - (deadline - timeout):5.1f}s] '
                  f'menu entries present')
            return ('MENU RENDERED', screen)
        time.sleep(0.5)

    suffix = f' (monitor dropped {drops}x)' if drops else ''
    if saw_panel or saw_fetching:
        return ('STUCK FETCHING' + suffix, screen)
    return ('PANEL NEVER DREW' + suffix, screen)


def tail(path, n=40):
    p = Path(path)
    if not p.exists():
        return f'(no {p.name})'
    return '\n'.join(p.read_text(errors='replace').splitlines()[-n:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['direct', 'hayes'], default='direct')
    ap.add_argument('--prg', default=str(REPO / 'c64_client/build/c64llm.prg'))
    ap.add_argument('--proxy-port', type=int, default=TESTPORT)
    ap.add_argument('--baud', type=int, default=9600)
    ap.add_argument('--acia-mode', type=int, default=0)
    ap.add_argument('--ready-timeout', type=float, default=180.0,
                    help='seconds to wait for the client ready prompt')
    ap.add_argument('--menu-timeout', type=float, default=25.0,
                    help='seconds to wait for the menu after F1')
    ap.add_argument('--no-warp', action='store_true',
                    help='never warp (hayes never warps regardless)')
    args = ap.parse_args()

    artifacts = REPO / 'emu' / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)

    if not Path(args.prg).exists():
        sys.exit(f'PRG not found: {args.prg} (build it first)')
    mod4 = Path(args.prg + '.4')
    if not mod4.exists():
        sys.exit(f'menu module not found: {mod4} - build with MODE80=1')
    if not have_vice_tool('c1541'):
        sys.exit('c1541 not available; the module disk cannot be built')
    proxy_port = args.proxy_port
    if port_in_use(proxy_port):
        sys.exit(f'port {proxy_port} already in use - pick another with '
                 f'--proxy-port (never 6400: the live proxy owns it)')

    stack = Stack(artifacts)
    verdict = 'PANEL NEVER DREW'
    screen = ''
    try:
        # 1. Mock LLM - the menu never touches it, but the proxy expects
        #    an API to exist and the rest of the boot path uses it.
        env = dict(os.environ)
        mock_port = find_free_port()
        stack.start('mock_llm', [sys.executable, str(REPO / 'emu/mock_llm.py'),
                                 '--port', str(mock_port)])
        if not wait_for_port(mock_port):
            raise AssertionError('mock LLM did not start')
        env.update({
            'OPENAI_API_KEY': 'mock-key',
            'OPENAI_API_BASE': f'http://127.0.0.1:{mock_port}/v1',
            'OPENAI_MODEL': 'mock',
            'C64LLM_CARDS_DIR': str(REPO / 'emu' / 'fixtures'),
            'C64LLM_DATA_DIR': str(artifacts / 'repro-data'),
            'C64LLM_IMG_FIXTURE': str(REPO / 'emu' / 'fixtures' / 'scene.png'),
            'C64LLM_CLAUDE_CMD':
                f"{sys.executable} {REPO / 'emu' / 'mock_claude.py'}",
        })
        # Music library: 'Jukebox' is a menu entry only when music is
        # available, and the entry count changes the MENU_LIST size.
        shutil.copytree(REPO / 'emu' / 'fixtures' / 'sids',
                        artifacts / 'repro-data' / 'sids', dirs_exist_ok=True)
        print(f'mock LLM on :{mock_port}')

        # 2. Proxy
        stack.start('proxy', [proxy_python(), '-m', 'src.main',
                              '--host', '127.0.0.1', '--port', str(proxy_port),
                              '-v'],
                    cwd=str(PROXY_DIR), env=env)
        if not wait_for_port(proxy_port):
            raise AssertionError('proxy did not start (artifacts/proxy.log)')
        print(f'proxy on :{proxy_port}')

        # 3. Transport
        if args.mode == 'hayes':
            stack.start('tcpser', ['tcpser', '-v', str(TCPSER_PORT),
                                   '-s', str(args.baud),
                                   '-p', str(find_free_port()), '-tSs'])
            if not wait_for_port(TCPSER_PORT):
                raise AssertionError('tcpser did not start')
            print(f'tcpser on :{TCPSER_PORT}')
            rsdev = ['-rsdev1', f'127.0.0.1:{TCPSER_PORT}', '-rsdev1ip232']
        else:
            rsdev = ['-rsdev1', f'127.0.0.1:{proxy_port}', '+rsdev1ip232']

        # 4. Overlay-module disk on unit 8: the F1 menu is c64llm.4 and
        #    is loaded from here. Hayes additionally boots from the disk
        #    config so it dials 127.0.0.1:<proxy_port>.
        d64_path = artifacts / 'repro-modules.d64'
        cmd = [*vice_tool('c1541'), '-format', 'c64llm,01', 'd64',
               str(d64_path)]
        for ext in ('1', '2', '3', '4', '5'):
            mod = Path(f'{args.prg}.{ext}')
            if mod.exists():
                cmd += ['-write', str(mod), f'c64llm.{ext}']
        if args.mode == 'hayes':
            cfg = artifacts / 'repro.cfg'
            cfg.write_bytes(b'\x00\x10\xc6\x01'
                            + b'127.0.0.1'.ljust(32, b'\x00')
                            + str(proxy_port).encode().ljust(6, b'\x00'))
            cmd += ['-write', str(cfg), 'c64llm.cfg']
        subprocess.run(cmd, check=True, capture_output=True)
        print(f'module disk: {d64_path.name}')

        # 5. VICE. Hayes never warps: the client's AT timeouts are
        #    cycle-based while tcpser answers in wall-clock time.
        mon_port = find_free_port()
        speed = [] if (args.mode == 'hayes' or args.no_warp) else ['-warp']
        stack.start('vice', [
            *vice_tool('x64sc'), '-default', *speed, '-sounddev', 'dummy',
            '+confirmonexit',
            '-acia1', '-acia1mode', str(args.acia_mode),
            '-acia1base', '0xDE00',
            '-acia1irq', '2', '-myaciadev', '0',
            *rsdev, '-rsdev1baud', str(args.baud),
            '-8', str(d64_path),
            '-binarymonitor',
            '-binarymonitoraddress', f'ip4://127.0.0.1:{mon_port}',
            '-exitscreenshot', str(artifacts / f'repro-{args.mode}.png'),
            '-autostartprgmode', '1',
            '-autostart', args.prg])
        if not wait_for_port(mon_port, timeout=30):
            raise AssertionError('VICE binary monitor did not come up')
        print(f'VICE up, binary monitor on :{mon_port}')

        time.sleep(4)  # let autostart finish before pausing for reads
        monitor = connect_monitor(mon_port)

        # 6. Ready, then F1.
        wait_ready(monitor, args.ready_timeout, artifacts,
                   f'repro-{args.mode}-ready')
        print('client ready; pressing F1')
        monitor.keyboard_feed_petscii(b'\x85')   # F1
        verdict, screen = poll_menu(monitor, mon_port, args.menu_timeout,
                                    artifacts)

    except (AssertionError, ViceMonitorError, OSError) as e:
        verdict = f'SETUP FAILED: {e}'
    finally:
        try:
            (artifacts / f'repro-{args.mode}-screen.txt').write_text(screen)
        except OSError:
            pass
        stack.teardown()

    ok = verdict == 'MENU RENDERED'
    print(f'\n=== VERDICT [{args.mode}]: {verdict} ===')
    print(f'--- screen ({args.mode}) ---')
    print(screen if screen else '(no screen captured)')
    print('-' * 60)
    if not ok:
        for name in ('proxy', 'tcpser', 'vice'):
            log = artifacts / f'{name}.log'
            if log.exists():
                print(f'\n--- {name}.log (tail) ---')
                print(tail(log, 40))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
