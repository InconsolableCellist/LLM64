#!/usr/bin/env python3
"""Human SID verdicts: the layer that survives a re-tag.

The point of src/sid_overrides.json is that a person's ears outrank the
tagger permanently - so these assertions pin the merge rules, the
blocking, and the auto/manual bookkeeping that tells the two apart.
No curses, no audio.

Run: python3 tests/test_sid_overrides.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from src import sid_overrides                                  # noqa: E402
from sid_review import (build_queue, make_entry, pick_weight,   # noqa: E402
                        working_values)

AUTO = {'id': 'GAMES__C__Creepshow', 'title': 'Creepshow',
        'author': 'A. Tagger', 'file': 'b000/Creepshow.sid',
        'moods': {'eerie': 0.8, 'tense': 0.4}, 'settings': {'horror': 0.6},
        'arcadey': 0.2, 'iconic': 0.1, 'confidence': 0.4,
        'load': 45056, 'init': 45056, 'play': 45059, 'songs': 1,
        'start_song': 1, 'size': 900}
BOUNCY = dict(AUTO, id='GAMES__P__Party', title='Party',
              moods={'playful': 0.9}, settings={}, arcadey=0.8, iconic=0.0,
              confidence=0.9)

failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok  {name}')
    else:
        failures.append(name)
        print(f'  FAIL {name} {detail}')


print('merge')
merged = sid_overrides.apply(AUTO, None)
check('untouched tune is marked auto', merged['source'] == 'auto')
check('untouched tags survive', merged['moods'] == AUTO['moods'])

entry = {'verdict': 'keep', 'moods': {'playful': 0.9, 'festive': 0.0},
         'confidence': 1.0, 'reviewed': '2026-07-26'}
merged = sid_overrides.apply(AUTO, entry)
check('hand tags replace auto tags wholesale',
      merged['moods'] == {'playful': 0.9}, merged['moods'])
check('zero weights are dropped, not kept as a mood',
      'festive' not in merged['moods'])
check('untouched fields keep the auto value',
      merged['settings'] == {'horror': 0.6} and merged['arcadey'] == 0.2)
check('measured fields are never touched',
      merged['play'] == 45059 and merged['file'] == AUTO['file'])
check('reviewed tune is marked manual', merged['source'] == 'manual')
check('review date carries into the runtime record',
      merged['reviewed'] == '2026-07-26')

print('blocking')
check('blocked tune merges to nothing',
      sid_overrides.apply(AUTO, {'verdict': 'blocked'}) is None)
kept = sid_overrides.apply_all([AUTO, BOUNCY],
                               {'GAMES__P__Party': {'verdict': 'blocked'}})
check('apply_all drops blocked tunes',
      [t['id'] for t in kept] == ['GAMES__C__Creepshow'])

print('round trip')
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'ov.json'
    sid_overrides.save({'b': {'verdict': 'blocked'},
                        'a': {'verdict': 'keep'}}, p)
    check('saved sorted for readable diffs',
          list(json.loads(p.read_text())['tunes']) == ['a', 'b'])
    check('reload gives back the entries',
          set(sid_overrides.load(p)) == {'a', 'b'})
    check('missing file is not an error',
          sid_overrides.load(Path(d) / 'nope.json') == {})
    (Path(d) / 'bad.json').write_text('{ not json')
    check('a corrupt file never stops the music',
          sid_overrides.load(Path(d) / 'bad.json') == {})

print('verdicts built by the review tool')
vals = working_values(AUTO, None)
check('editing starts from the auto tags', vals['moods'] == AUTO['moods'])
vals['moods'] = {'playful': 0.8}
e = make_entry(AUTO, vals, today='2026-07-26')
check('a keep verdict records the new tags',
      e['verdict'] == 'keep' and e['moods'] == {'playful': 0.8})
check('a heard tune is fully confident',
      e['confidence'] == 1.0)
check('what the tagger had said is kept for later',
      e['auto']['moods'] == AUTO['moods'] and e['auto']['confidence'] == 0.4)
check('the verdict is dated', e['reviewed'] == '2026-07-26')

e = make_entry(AUTO, working_values(AUTO, None), blocked=True,
               reason='bad music', today='2026-07-26')
check('a block records why', e['verdict'] == 'blocked'
      and e['reason'] == 'bad music')
check('a block carries no tags to argue about', 'moods' not in e)

confirm = make_entry(AUTO, working_values(AUTO, None), today='2026-07-26')
check('confirming auto tags still raises confidence',
      confirm['moods'] == AUTO['moods'] and confirm['confidence'] == 1.0)

print('queue')
entries = {'GAMES__P__Party': {'verdict': 'keep', 'moods': {'eerie': 0.9}}}
q = build_queue([AUTO, BOUNCY], entries, status='unreviewed')
check('unreviewed queue skips tunes with a verdict',
      q == ['GAMES__C__Creepshow'])
q = build_queue([AUTO, BOUNCY], entries, status='reviewed')
check('reviewed queue holds only the heard ones', q == ['GAMES__P__Party'])
q = build_queue([AUTO, BOUNCY], entries, status='all', mood='eerie')
check('mood filter sees the CURRENT tags, not the auto ones',
      sorted(q) == ['GAMES__C__Creepshow', 'GAMES__P__Party'], q)
q = build_queue([AUTO, BOUNCY], {'GAMES__P__Party': {'verdict': 'blocked'}},
                status='blocked')
check('blocked queue lets you revisit your own rejects',
      q == ['GAMES__P__Party'])
q = build_queue([AUTO, BOUNCY], {}, status='all', author='tagger')
check('author filter matches case-insensitively', len(q) == 2)

check('selection weighting damps arcadey and iconic',
      pick_weight(BOUNCY, working_values(BOUNCY, None), 'playful')
      < working_values(BOUNCY, None)['moods']['playful'])
check('a mood the tune lacks weighs nothing',
      pick_weight(AUTO, working_values(AUTO, None), 'playful') == 0)

print()
if failures:
    print(f'{len(failures)} FAILED: {", ".join(failures)}')
    sys.exit(1)
print('all sid override tests passed')
