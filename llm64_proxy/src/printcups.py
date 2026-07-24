#!/usr/bin/env python3
"""Hand a composed /print document to a CUPS queue (docs/14 13).

The other half of /print. printdoc.py decides what the page SAYS; this
decides where paper comes out when it isn't the C64's own IEC bus - a
printer on the proxy host's USB, or one shared over IPP by a Raspberry
Pi hidden behind the C64 (the N80 setup, docs/14 2.4).

Delivery is stock CUPS: `lp` takes the document on stdin and CUPS's own
text->raster chain renders it, so the proxy host needs cups-client and
no driver at all. There are no PRINT frames on this path - the client is
not involved and does not know this backend exists.

Nothing here touches the wire protocol or the event loop except to spawn
lp, so tests/test_printcups.py stubs `lp` on PATH and asserts both the
command line and what the stub received.
"""

import asyncio
import contextlib
import logging
from collections import namedtuple

logger = logging.getLogger(__name__)

# lp returns as soon as the job is spooled - well under a second. This
# only has to outlast a slow cupsd or an unreachable `-h` host, and it
# must stay well inside the client's ~43s watchdog so a wedged queue can
# never look like a wedged C64.
TIMEOUT = 20.0

# Passed to lp as -o. printer_width is 78 columns (the MPS-803's width,
# shared with the IEC path) and at CUPS's 10 cpi default an A4 text page
# holds only ~72 of them, so the document would wrap a second time and
# ragged-right becomes ragged-both. 12 cpi holds 87 columns; 8 lpi keeps
# a full page of lines on one sheet. Empty = the queue's own defaults.
OPTIONS = 'cpi=12 lpi=8'

# What shows up in `lpstat -o` (the runbook's debugging step). Without
# it every job is called "(stdin)".
TITLE = 'llm64'

# lp's own words, mapped to something that fits the C64's status row.
# Anything unmatched falls back to the exit code and the log keeps the
# full text - these are the three that actually happen.
REASONS = (
    ('does not exist', 'no such queue'),
    ('unable to connect', 'no cups server'),
    ('not accepting', 'queue not accepting'),
)

Result = namedtuple('Result', 'ok reason detail')


def argv(queue: str, server: str = '', options: str = OPTIONS,
         title: str = TITLE):
    """The lp command line. `server` empty means the local cupsd; set it
    to host[:port] for a print bridge (an mDNS name beats an IP)."""
    cmd = ['lp']
    if server:
        cmd += ['-h', server]
    cmd += ['-d', queue]
    if title:
        cmd += ['-t', title]
    for opt in (options or '').split():
        cmd += ['-o', opt]
    return cmd + ['-']


def _reason(text: str, returncode: int) -> str:
    low = (text or '').lower()
    for needle, short in REASONS:
        if needle in low:
            return short
    return f"lp exit {returncode}"


async def send(doc: str, queue: str, server: str = '',
               options: str = OPTIONS, title: str = TITLE,
               timeout: float = TIMEOUT) -> Result:
    """Spool `doc` to `queue`. Never raises and never blocks the reader
    task: every failure comes back as a Result whose `reason` is short
    enough for the C64's status row and whose `detail` carries lp's own
    words for the log.

    ok=True means CUPS ACCEPTED the job, which is as far as this can
    see - a printer that is asleep, out of paper or unplugged still
    spools cleanly (docs/14 13.6 lists where to look when a page never
    appears)."""
    if not queue:
        return Result(False, 'no queue configured',
                      'printer.cups_queue is empty')
    cmd = argv(queue, server, options, title)
    logger.debug("CUPS print: %s", ' '.join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return Result(False, 'lp not installed',
                      'lp is not on PATH - install cups-client')
    except OSError as exc:
        return Result(False, 'lp failed to start', str(exc))

    data = doc.encode('ascii', 'replace')
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return Result(False, 'timed out',
                      f"lp did not finish within {timeout:g}s")

    text = (out or b'').decode('utf-8', 'replace').strip()
    if proc.returncode != 0:
        return Result(False, _reason(text, proc.returncode),
                      text or f"lp exited {proc.returncode}")
    return Result(True, '', text)
