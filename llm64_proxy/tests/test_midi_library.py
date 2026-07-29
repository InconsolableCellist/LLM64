#!/usr/bin/env python3
"""The MIDI music path: parsing, selection, and the vocabulary both
clients have to agree on.

The load-bearing assertion in here is not about MIDI at all. It is that
midi_mood.MOODS IS sid_mood.MOODS - the same list object, not an equal
copy. The narrator is told the mood vocabulary in its system prompt,
that prompt is built upstream of the client profile (docs/16 section 7),
and a C64 and a Windows client can be in the same adventure. Two
vocabularies would mean telling the model about moods only half the
world can play, and the failure would be silent: the tune just never
changes for one of them.

No network, no corpus, no soundfont. The MIDI files are built byte by
byte here so the parser is tested against known input rather than
against whatever happened to be downloaded.

Run: python3 tests/test_midi_library.py
"""

import json
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from src.midi_library import MidiLibrary                       # noqa: E402
from src.music import MusicLibrary                             # noqa: E402
import midi_scan                                               # noqa: E402
import midi_makedb                                             # noqa: E402

failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok  {name}')
    else:
        print(f'  FAIL {name}  {detail}')
        failures.append(name)


# --- building a MIDI file by hand -------------------------------------

def vlq(n):
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, 0x80 | (n & 0x7F))
        n >>= 7
    return bytes(out)


def track(events):
    body = b''.join(events) + b'\x00\xff\x2f\x00'
    return b'MTrk' + struct.pack('>I', len(body)) + body


def smf(events, tpq=480, fmt=0):
    head = b'MThd' + struct.pack('>IHHH', 6, fmt, 1, tpq)
    return head + track(events)


def tempo(usec):
    return b'\x00\xff\x51\x03' + usec.to_bytes(3, 'big')


def note(delta, chan, pitch, vel, dur):
    """One note on/off pair."""
    return (vlq(delta) + bytes([0x90 | chan, pitch, vel])
            + vlq(dur) + bytes([0x80 | chan, pitch, 0]))


def meta(mtype, text):
    b = text.encode('latin-1')
    return b'\x00\xff' + bytes([mtype]) + vlq(len(b)) + b


print('midi_scan: parsing')

# 480 tpq, 500000 usec/quarter = 120bpm -> 1 quarter = 0.5 s.
# Four whole-note-length gaps of 480 ticks each = 4 quarters = 2 s.
ev = [tempo(500000), meta(0x03, 'Lead'), bytes([0x00, 0xC0, 48])]
ev += [note(0, 0, 60 + i, 90, 470) for i in range(4)]
f = midi_scan.parse(smf(ev))
check('duration from tempo map', abs(f['seconds'] - 2.0) < 0.05,
      f"got {f['seconds']}")
check('note count', f['notes'] == 4, f['notes'])
check('program captured', f['programs'] == [48], f['programs'])
check('track name captured', f['names'] == ['Lead'], f['names'])

# A tempo change halfway must be integrated, not averaged.
ev = [tempo(500000)] + [note(0, 0, 60, 90, 470) for _ in range(2)]
ev += [b'\x00\xff\x51\x03' + (250000).to_bytes(3, 'big')]
ev += [note(0, 0, 62, 90, 470) for _ in range(2)]
f = midi_scan.parse(smf(ev))
# 2 quarters at 120bpm (1.0 s) + 2 at 240bpm (0.5 s)
check('tempo change integrated', abs(f['seconds'] - 1.5) < 0.05,
      f"got {f['seconds']}")

# Channel 10 (index 9) is percussion: its program change is a drum kit
# and must not enter the instrument palette.
ev = [tempo(500000), bytes([0x00, 0xC9, 25])]
ev += [note(0, 9, 36, 100, 100) for _ in range(3)]
ev += [note(0, 0, 60, 100, 100) for _ in range(1)]
f = midi_scan.parse(smf(ev))
check('drum program excluded from palette', f['programs'] == [],
      f['programs'])
check('drum fraction measured', abs(f['drum_fraction'] - 0.75) < 0.01,
      f['drum_fraction'])

# Running status: a stream of note-ons with the status byte sent once.
body = [tempo(500000), bytes([0x00, 0x90, 60, 100])]
body += [bytes([0x60, 62, 100]), bytes([0x60, 64, 100])]   # no status byte
f = midi_scan.parse(smf(body))
check('running status decoded', f['notes'] == 3, f['notes'])

# GM reset SysEx.
sysex = b'\x00\xf0\x05\x7e\x7f\x09\x01\xf7'
f = midi_scan.parse(smf([tempo(500000), sysex]
                        + [note(0, 0, 60, 90, 470) for _ in range(4)]))
check('GM reset detected', f['gm_reset'] and not f['mt32'])

# Roland MT-32 SysEx (manufacturer 0x41, model 0x16) - the wrong-patches
# case the scanner exists to catch.
mt = b'\x00\xf0\x07\x41\x10\x16\x12\x00\x00\xf7'
f = midi_scan.parse(smf([tempo(500000), mt]
                        + [note(0, 0, 60, 90, 470) for _ in range(4)]))
check('MT-32 detected', f['mt32'] and not f['gm_reset'])

# Roland GS (model 0x42) is GM-compatible, and must NOT be called MT-32.
gs = b'\x00\xf0\x09\x41\x10\x42\x12\x40\x00\x7f\x00\xf7'
f = midi_scan.parse(smf([tempo(500000), gs]
                        + [note(0, 0, 60, 90, 470) for _ in range(4)]))
check('GS is not MT-32', f['gm_reset'] and not f['mt32'])

# RIFF-wrapped (.RMI saved as .mid) still parses.
inner = smf([tempo(500000)] + [note(0, 0, 60, 90, 470) for _ in range(4)])
rmid = (b'RIFF' + struct.pack('<I', len(inner) + 12) + b'RMIDdata'
        + struct.pack('<I', len(inner)) + inner)
f = midi_scan.parse(rmid)
check('RIFF-wrapped file unwrapped', f['notes'] == 4)

for bad, why in [(b'not a midi at all', 'no header'),
                 (smf([tempo(500000)]), 'no notes')]:
    try:
        midi_scan.parse(bad)
        check(f'rejects {why}', False, 'parsed anyway')
    except midi_scan.BadMidi:
        check(f'rejects {why}', True)


print('\nmidi_scan: the usability verdict')

base = {'seconds': 120, 'notes': 500, 'channels': 5, 'drum_fraction': 0.2,
        'notes_per_sec': 4.0, 'mt32': False}
check('a good tune passes', midi_scan.verdict(base) == '')
check('sting rejected', midi_scan.verdict(dict(base, seconds=8))
      == 'too short')
check('MT-32 rejected', midi_scan.verdict(dict(base, mt32=True))
      == 'MT-32 voiced')
check('drum loop rejected',
      midi_scan.verdict(dict(base, drum_fraction=0.95)) == 'drums only')


print('\nmidi_makedb: quality is a proxy, and an honest one')

flat = {'vel_sd': 0.0, 'channels': 1, 'seconds': 30, 'drum_fraction': 0.0}
rich = {'vel_sd': 20.0, 'channels': 8, 'seconds': 200, 'drum_fraction': 0.2}
check('sequenced beats mechanical',
      midi_makedb.raw_quality(rich) > midi_makedb.raw_quality(flat))
check('velocity spread dominates',
      midi_makedb.raw_quality(dict(flat, vel_sd=20.0))
      > midi_makedb.raw_quality(dict(flat, channels=8)))
check('quality stays in range',
      0.0 <= midi_makedb.raw_quality(flat) <= 1.0
      and 0.0 <= midi_makedb.raw_quality(rich) <= 1.0)


print('\nMidiLibrary: the same shape as MusicLibrary')

# Duck typing is only a guarantee if something checks it. protocol.py
# holds ONE reference and calls it .music; whichever library is behind
# that name has to answer every question the other would.
music_api = {n for n in dir(MusicLibrary)
             if not n.startswith('_')} - {'payload'}
midi_api = {n for n in dir(MidiLibrary) if not n.startswith('_')}
check('MidiLibrary covers MusicLibrary\'s interface',
      music_api <= midi_api, f'missing: {sorted(music_api - midi_api)}')

import midi_mood                                               # noqa: E402
import sid_mood                                                # noqa: E402
check('mood vocabulary is SHARED, not copied',
      midi_mood.MOODS is sid_mood.MOODS)
check('setting vocabulary is SHARED, not copied',
      midi_mood.SETTINGS is sid_mood.SETTINGS)


def make_lib(tunes, tmp):
    db = Path(tmp) / 'midi.json'
    db.write_text(json.dumps({
        'tunes': tunes,
        'moods': sorted({m for t in tunes for m in t['moods']})}))
    return MidiLibrary(db)


def tune(tid, moods, **kw):
    d = {'id': tid, 'title': tid, 'game': 'G', 'author': 'A',
         'moods': moods, 'settings': {}, 'arcadey': 0.0, 'iconic': 0.0,
         'confidence': 0.9, 'file': f'{tid}.mid', 'secs': 100,
         'quality': 0.5}
    d.update(kw)
    return d


with tempfile.TemporaryDirectory() as tmp:
    lib = make_lib([tune('a', {'eerie': 0.8}), tune('b', {'combat': 0.9})],
                   tmp)
    check('library loads', lib.available and len(lib.tunes) == 2)
    check('moods derived', lib.moods == ['combat', 'eerie'], lib.moods)
    check('unknown mood picks nothing', lib.pick('nonexistent') is None)
    check('mood resolves', lib.pick('eerie')['id'] == 'a')

    # prompt_snippet must be word-for-word MusicLibrary's, because both
    # clients' narrators are handed the same instruction.
    sid_db = Path(tmp) / 'moods.json'
    sid_db.write_text(json.dumps({'tunes': [], 'moods': ['combat', 'eerie']}))
    sid_lib = MusicLibrary(sid_db)
    check('prompt wording identical to the SID library',
          lib.prompt_snippet() == sid_lib.prompt_snippet(),
          f'\n    midi: {lib.prompt_snippet()[:70]}'
          f'\n    sid:  {sid_lib.prompt_snippet()[:70]}')

    # A famous theme is excluded outright; a merely well-known one is
    # only damped. Same thresholds as MusicLibrary, so the two clients
    # feel the same.
    lib = make_lib([tune('famous', {'heroic': 1.0}, iconic=0.9),
                    tune('plain', {'heroic': 1.0}, iconic=0.0)], tmp)
    check('iconic tune excluded',
          {lib.pick('heroic')['id'] for _ in range(20)} == {'plain'})

    # Low confidence - a mood guessed from a bare filename - is skipped
    # whenever the bucket has confident alternatives left.
    #
    # Note the filter ORDER, which is MusicLibrary's and is inherited
    # deliberately: recency is applied BEFORE confidence. On a library
    # small enough that the recent-repeat window empties the bucket, the
    # unconfident tune therefore plays anyway. That is the right
    # trade (silence is worse than a doubtful tune) and it is why this
    # case needs more tunes than the RECENT_N window.
    lib = make_lib([tune('guess', {'serene': 1.0}, confidence=0.2)]
                   + [tune(f'known{i}', {'serene': 1.0}, confidence=0.9)
                      for i in range(5)], tmp)
    check('low-confidence tune skipped',
          'guess' not in {lib.pick('serene')['id'] for _ in range(40)})
    # ...but never starves the bucket.
    lib = make_lib([tune('guess', {'serene': 1.0}, confidence=0.2)], tmp)
    check('low confidence still plays when alone',
          lib.pick('serene')['id'] == 'guess')

    # Quality is a weighting, never a filter - the same guarantee
    # sid_ranking makes, and for the same reason.
    lib = make_lib([tune('bad', {'tense': 1.0}, quality=0.0),
                    tune('good', {'tense': 1.0}, quality=1.0)], tmp)
    ids = {lib.pick('tense')['id'] for _ in range(400)}
    check('low quality is damped, not silenced', ids == {'bad', 'good'})
    check('quality weight is bounded',
          MidiLibrary.quality_weight(0.0) > 0
          and MidiLibrary.quality_weight(1.0) == 1.0)
    check('missing quality is neutral', MidiLibrary.quality_weight(None) == 1.0)

    # Recent-repeat window.
    lib = make_lib([tune(c, {'urgent': 1.0}) for c in 'abcd'], tmp)
    picks = [lib.pick('urgent')['id'] for _ in range(4)]
    check('no immediate repeats', len(set(picks)) == 4, picks)

    # payload() is the file verbatim - the whole reason MIDI is cheap.
    raw = smf([tempo(500000)] + [note(0, 0, 60, 90, 470) for _ in range(4)])
    (Path(tmp) / 'a.mid').write_bytes(raw)
    lib = make_lib([tune('a', {'eerie': 1.0})], tmp)
    check('payload is the file, byte for byte',
          lib.payload(lib.find('a')) == raw)

    # Favorites are per-format: liking a SID says nothing about a MIDI.
    check('favorite persists', lib.toggle_favorite('a')
          and MidiLibrary(Path(tmp) / 'midi.json').is_favorite('a'))
    check('favorites file is separate from the SID one',
          (Path(tmp) / 'midi_favorites.json').exists()
          and not (Path(tmp) / 'favorites.json').exists())

    # A missing library must not be an error: music is optional
    # everywhere it appears.
    empty = MidiLibrary(Path(tmp) / 'nope.json')
    check('missing database degrades quietly',
          not empty.available and empty.pick('eerie') is None)


print()
if failures:
    print(f'{len(failures)} FAILED: {", ".join(failures)}')
    sys.exit(1)
print('all passed')
