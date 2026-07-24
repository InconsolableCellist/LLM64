#!/usr/bin/env python3
"""printcups: the /print CUPS backend (docs/14 13).

No printer and no cupsd in CI, so `lp` is stubbed on PATH - the 12
lesson, which turned a 10-minute emulator round trip into a 2-second
one. What matters and is asserted here: the exact command line CUPS
gets, that the document reaches lp's stdin byte for byte, and that every
failure comes back as a Result instead of an exception (the reader task
must never see one) with a reason short enough for the C64's status row.

Run: .venv/bin/python tests/test_printcups.py  (plain python3 works too,
but skips the config-table checks - those need the toml module).
"""

import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import printcups

failures = []
REAL_PATH = os.environ.get('PATH', '')


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def check_in(name, needle, hay):
    if needle not in hay:
        failures.append(f"{name}: {needle!r} missing from {hay!r:.300}")


DOC = "Fire Stew\n2026-07-24\n" + '-' * 78 + "\nBrown the pork.\n"


def stub_lp(body: str):
    """Put an `lp` on PATH that runs `body` (with @CAP@ replaced by a
    scratch file the stub can write) and return that file's path. PATH is
    rebuilt from the real one each time, so one stub never shadows the
    next."""
    d = Path(tempfile.mkdtemp(prefix='printcups-'))
    cap = d / 'capture'
    lp = d / 'lp'
    lp.write_text("#!/bin/sh\n" + body.replace('@CAP@', str(cap)))
    lp.chmod(lp.stat().st_mode | stat.S_IXUSR)
    os.environ['PATH'] = f"{d}{os.pathsep}{REAL_PATH}"
    return cap


# --- the command line -------------------------------------------------

check('argv, local queue', printcups.argv('n80'),
      ['lp', '-d', 'n80', '-t', 'llm64',
       '-o', 'cpi=12', '-o', 'lpi=8', '-'])

# -h must precede the queue, and a bridge is host[:port], not a URI
check('argv, remote cupsd',
      printcups.argv('n80', 'printpi.local:631', options=''),
      ['lp', '-h', 'printpi.local:631', '-d', 'n80', '-t', 'llm64', '-'])

# Empty options = the queue's own defaults; empty title = "(stdin)"
check('argv, no options no title',
      printcups.argv('n80', options='', title=''), ['lp', '-d', 'n80', '-'])
check('argv, options split into separate -o',
      printcups.argv('q', options='a=1  b=2', title=''),
      ['lp', '-d', 'q', '-o', 'a=1', '-o', 'b=2', '-'])

# --- delivery ----------------------------------------------------------

# Happy path: exit 0, and the document arrives unaltered on lp's stdin
cap = stub_lp('{ echo "ARGS $*"; cat; } > "@CAP@"\n')
res = asyncio.run(printcups.send(DOC, 'n80'))
check('ok', (res.ok, res.reason), (True, ''))
captured = cap.read_text()
check_in('argv reached lp', 'ARGS -d n80 -t llm64 -o cpi=12 -o lpi=8 -',
         captured)
check('document reached lp intact', captured.split('\n', 1)[1], DOC)

# A queue that does not exist: lp says so and exits nonzero. The C64 gets
# three words; the log gets lp's whole sentence.
stub_lp('echo "lp: Error - The printer or class does not exist." >&2\n'
        'exit 1\n')
res = asyncio.run(printcups.send(DOC, 'nope'))
check('missing queue', (res.ok, res.reason), (False, 'no such queue'))
check_in('missing queue detail', 'does not exist', res.detail)

# An unreachable print bridge - the other failure that actually happens
stub_lp('echo "lp: Unable to connect to server: Connection refused" >&2\n'
        'exit 1\n')
res = asyncio.run(printcups.send(DOC, 'n80', server='printpi.local:631'))
check('dead server', (res.ok, res.reason), (False, 'no cups server'))

# Anything else falls back to the exit code rather than guessing
stub_lp('echo "lp: something new" >&2\nexit 3\n')
res = asyncio.run(printcups.send(DOC, 'n80'))
check('unmapped failure', (res.ok, res.reason), (False, 'lp exit 3'))
check_in('unmapped detail kept', 'something new', res.detail)

# A wedged queue must not hold the print job open: kill it and report.
# (Both legs of backend="both" wait on this one.)
stub_lp('sleep 5\n')
res = asyncio.run(printcups.send(DOC, 'n80', timeout=0.4))
check('timeout', (res.ok, res.reason), (False, 'timed out'))

# --- refusals that never spawn anything -------------------------------

# backend="cups" with no cups_queue is caught in config.py, but /print
# must not raise if it ever gets this far
res = asyncio.run(printcups.send(DOC, ''))
check('no queue', (res.ok, res.reason), (False, 'no queue configured'))

# cups-client not installed on the proxy host: the one failure that is
# neither an exit code nor a timeout
os.environ['PATH'] = str(Path(tempfile.mkdtemp(prefix='printcups-empty-')))
res = asyncio.run(printcups.send(DOC, 'n80'))
check('lp missing', (res.ok, res.reason), (False, 'lp not installed'))
os.environ['PATH'] = REAL_PATH

# --- config: the routing table and its fallbacks ----------------------

# `backend` is the one print setting a C64 user cannot fix from the C64,
# so junk has to degrade to the shipped default at startup (loudly, in
# the proxy log) rather than fail at /print time.
try:
    import toml                                        # noqa: F401
except ImportError:
    print("printcups: config checks skipped - no toml module "
          "(run under llm64_proxy/.venv/bin/python)")
else:
    import logging
    from src.config import Config
    # The fallbacks warn by design; don't make the test output look angry
    logging.getLogger('src.config').setLevel(logging.CRITICAL)

    def printer_cfg(table: str):
        d = Path(tempfile.mkdtemp(prefix='printcups-cfg-'))
        cfg = d / 'config.toml'
        cfg.write_text(f'[storage]\ndata_dir = "{d / "data"}"\n\n'
                       f'[printer]\n{table}\n')
        c = Config(str(cfg))
        return (c.printer_backend, c.printer_cups_queue,
                c.printer_cups_server, c.printer_cups_options)

    check('config default is the shipped IEC path', printer_cfg('')[0], 'c64')
    check('config cups', printer_cfg('backend = "cups"\n'
                                     'cups_queue = "n80"')[:2],
          ('cups', 'n80'))
    check('config both', printer_cfg('backend = "BOTH"\n'
                                     'cups_queue = "n80"')[0], 'both')
    check('config bridge and options',
          printer_cfg('backend = "cups"\ncups_queue = "n80"\n'
                      'cups_server = "printpi.local:631"\n'
                      'cups_options = ""')[2:],
          ('printpi.local:631', ''))
    check('config keeps the cpi default', printer_cfg('')[3], 'cpi=12 lpi=8')
    check('config junk backend falls back',
          printer_cfg('backend = "laserjet"')[0], 'c64')
    check('config cups without a queue falls back',
          printer_cfg('backend = "cups"')[0], 'c64')
    check('config both without a queue falls back',
          printer_cfg('backend = "both"')[0], 'c64')

# --- report ------------------------------------------------------------

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print('  ' + f)
    sys.exit(1)
print("printcups: all checks passed")
