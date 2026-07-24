#!/usr/bin/env python3
"""The two legs of one /print job (docs/14 13.10).

printdoc.py decides what the page says and printcups.py how it is
delivered; what is left - and what this pins - is the wiring between
them in protocol.py: which backends run, in which order, and with which
page width. The width is the reason this file exists. `width` is the
MPS-803's 78 columns and a till roll holds about 34, so the document is
laid out once per backend; a job that composed once and wrapped once
sent the roll lines its driver would crop.

ProtocolHandler is built without __init__ here (it wants a live socket,
an api_client and a conversation store), and both delivery legs are
replaced by recorders. Nothing on the wire, no lp, no event loop beyond
asyncio.run.

Run: .venv/bin/python tests/test_printjob.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.protocol import ProtocolHandler

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


class FakeConfig:
    printer_width = 78
    printer_cups_width = 34
    printer_backend = 'both'
    printer_cups_queue = 'n80'
    printer_cups_server = ''
    printer_cups_options = ''
    printer_cups_feed = 5


BODY = ('Brown the salted pork in a heavy pot, then add the onions and '
        'garlic until soft, stir in the paprika, and simmer for two hours.')


def run_job(backend):
    """Drive _print_job with both legs recorded. Returns the list of
    (leg, document) in delivery order."""
    h = ProtocolHandler.__new__(ProtocolHandler)
    h.config = FakeConfig()
    h.config.printer_backend = backend
    h._print_busy = False
    delivered = []

    async def fake_cups(doc):
        delivered.append(('cups', doc))

    async def fake_iec(doc):
        delivered.append(('c64', doc))

    h._print_cups = fake_cups
    h._send_print = fake_iec
    asyncio.run(h._print_job('Fire Stew', BODY))
    return delivered, h


# --- one document, two layouts ----------------------------------------

delivered, handler = run_job('both')
check('both legs ran', [leg for leg, _ in delivered], ['cups', 'c64'])
docs = dict(delivered)

# The whole point: neither leg gets the other's wrap.
for leg, width in (('cups', 34), ('c64', 78)):
    over = [ln for ln in docs[leg].split('\n') if len(ln) > width]
    check(f'{leg} leg wrapped to {width}', over, [])
    # The rule under the header is exactly the page width, so it is the
    # cheapest proof that this leg was laid out for THIS paper
    check(f'{leg} leg rule is {width} wide',
          docs[leg].split('\n')[2], '-' * width)

# Same document, not two compositions: the words must match even though
# the line breaks do not.
words = {leg: doc.replace('-' * 78, '').replace('-' * 34, '').split()
         for leg, doc in docs.items()}
check('both legs printed the same text', words['cups'], words['c64'])

# --- routing ----------------------------------------------------------

check('c64 only', [leg for leg, _ in run_job('c64')[0]], ['c64'])
check('cups only', [leg for leg, _ in run_job('cups')[0]], ['cups'])

# The re-entry guard has to be clear again afterwards, or the next
# /print answers "A print job is already running." forever.
check('busy flag released', handler._print_busy, False)


# A leg that raises must not strand the guard - the finally clause is
# the only thing between a failed delivery and a dead /print command.
def run_failing():
    h = ProtocolHandler.__new__(ProtocolHandler)
    h.config = FakeConfig()
    h.config.printer_backend = 'both'
    h._print_busy = False

    async def boom(doc):
        raise RuntimeError('lp went missing')

    async def ok(doc):
        pass

    h._print_cups = boom
    h._send_print = ok
    try:
        asyncio.run(h._print_job('T', BODY))
    except RuntimeError:
        pass
    return h._print_busy


check('busy flag released after a failure', run_failing(), False)

# --- report ------------------------------------------------------------

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print('  ' + f)
    sys.exit(1)
print("printjob: all checks passed")
