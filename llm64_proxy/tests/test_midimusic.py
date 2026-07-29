#!/usr/bin/env python3
"""The MIDI music path: same library shape as the SIDs, different
payload, capability-gated egress. Run: python3 tests/test_midimusic.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.music import MusicLibrary
from src.midi_library import MidiLibrary
from src.profiles import from_hello, CAP_MIDI, CAP_DIB_IMAGES, C64, WIN16

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


MID = (b'MThd' + (6).to_bytes(4, 'big') + b'\x00\x00\x00\x01\x00\x60'
       + b'MTrk' + (4).to_bytes(4, 'big') + b'\x00\xff\x2f\x00')

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / 'tune.mid').write_bytes(MID)
    (d / 'midi.json').write_text(json.dumps({
        'moods': ['festive'],
        'tunes': [{'id': 't1', 'file': 'tune.mid', 'title': 'T',
                   'author': 'A', 'moods': {'festive': 1.0},
                   'confidence': 1.0}],
    }))
    lib = MidiLibrary(d / 'midi.json')
    check("library loads", lib.available, True)
    check("shares the picker", lib.pick('festive')['id'], 't1')
    check("no tune for an unknown mood", lib.pick('dirge'), None)
    # The one difference from the SID library: the file ships verbatim -
    # no PSID header surgery, which WOULD eat the MThd magic.
    check("payload is the file, whole", lib.payload(lib.find('t1')), MID)
    check("mood vocabulary offered to the model",
          'festive' in lib.prompt_snippet(), True)

    empty = MidiLibrary(d / 'absent.json')
    check("absent database means unavailable", empty.available, False)

# The two payloads must never collapse into one: MusicLibrary strips
# PSID headers, and doing that to a .MID would eat the MThd magic.
check("the two classes keep separate payloads",
      MusicLibrary.payload is MidiLibrary.payload, False)

# --- capability plumbing ----------------------------------------------


def hello(caps, name=b'win16'):
    return bytes([1, 80, 0, 8, caps & 0xFF, caps >> 8, len(name)]) + name


check("claiming CAP_MIDI selects the midi format",
      from_hello(hello(CAP_MIDI))[0].music_fmt, 'midi')
check("without the cap a win16 gets no music",
      from_hello(hello(CAP_DIB_IMAGES))[0].music_fmt, None)
check("an unknown machine claiming it gets midi too",
      from_hello(hello(CAP_MIDI, name=b'dos32'))[0].music_fmt, 'midi')
check("an unknown machine without it gets nothing",
      from_hello(hello(0, name=b'dos32'))[0].music_fmt, None)
check("the C64 (no hello at all) keeps its SIDs", C64.music_fmt, 'sid')
check("the win16 table row alone grants nothing", WIN16.music_fmt, None)

if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("-", f)
    sys.exit(1)
print("test_midimusic: all checks passed")
