#!/usr/bin/env python3
"""Scene regard: the ranking layer that decides which tune actually plays.

10k tunes is more than anyone can audition, so tools/sid_rank.py scores
them from the C64 scene's own record (compo placings, re-use, videos, the
composer register) and the selector weighs that. These assertions pin the
scoring rules, the never-a-filter guarantee, and the ordering the review
tool depends on. No network, no dump, no audio.

Run: python3 tests/test_sid_ranking.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from src import sid_ranking                                    # noqa: E402
from sid_rank import (NON_MUSIC_COMPO_RE, percentiles,          # noqa: E402
                      score)
from sid_review import build_queue                             # noqa: E402

failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok  {name}')
    else:
        failures.append(name)
        print(f'  FAIL {name} {detail}')


print('scoring')
winner = {'compo': 1, 'compo_at': 'C64 Music', 'youtube': 3, 'usage': 20,
          'composer': ('pro', 1.0), 'stil': True}
nobody = {}
w_val, w_parts, w_why = score(winner, {}, False)
n_val, n_parts, n_why = score(nobody, {}, False)
check('a compo winner outscores an unknown tune', w_val > n_val)
check('an unknown tune scores zero, not negative', n_val == 0, n_val)
check('the reason is human-readable',
      '1st in C64 Music' in w_why and '3 videos' in w_why, w_why)
check('no signals means no reason text', n_why == '')

second = dict(winner, compo=2)
check('1st beats 2nd', score(second, {}, False)[0] < w_val)
entered = dict(winner, compo=-1)
check('entering a compo still counts for something',
      0 < score(entered, {}, False)[1]['compo'] < w_parts['compo'])

check('demo and game compos are not music votes',
      all(NON_MUSIC_COMPO_RE.search(n)
          for n in ('C64 Demo', 'C64 1K Game', 'C64 256b Intro')))
check('every shape of music compo counts',
      not any(NON_MUSIC_COMPO_RE.search(n) for n in
              ('C64 Music', 'Mixed Music', 'Mixed', 'C64 Sample Music',
               'C64 2SID')))

heavy = score({'usage': 400}, {}, False)[1]['usage']
some = score({'usage': 10}, {}, False)[1]['usage']
check('re-use counts but saturates', 0 < some < heavy <= 1.0)

check('csdb ratings are absent, not zero, without the pass',
      'csdb' not in n_parts)
with_rating = score({'releases': [7]}, {7: 8.0}, True)
check('a rated release lifts the score',
      with_rating[1]['csdb'] == 0.8 and 'CSDb 8.0' in with_rating[2])
check('weights renormalize so skipping csdb does not deflate scores',
      abs(score(winner, {}, True)[0]
          - score(winner, {}, False)[0]) > 0.01)

print('percentiles')
p = percentiles([0, 0, 0, 5, 10])
check('ties share a percentile', p[0] == p[1] == p[2], p)
check('the best value tops out', p[-1] > p[-2] and p[-1] <= 1.0, p)
check('a block of zeroes is not pinned to the floor', p[0] > 0, p)

print('weighting')
check('the best-regarded tune weighs 1.0', sid_ranking.weight(1.0) == 1.0)
check('the worst-regarded tune still plays',
      sid_ranking.weight(0.0) == sid_ranking.FLOOR > 0)
check('an unranked tune is not punished for being unknown',
      sid_ranking.weight(None) == 1.0)
check('regard is monotonic',
      sid_ranking.weight(0.9) > sid_ranking.weight(0.5)
      > sid_ranking.weight(0.1))
check('out-of-range values are clamped, never negative',
      sid_ranking.weight(5) == 1.0 and sid_ranking.weight(-3)
      == sid_ranking.FLOOR)

print('runtime file')
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'ranking.json'
    p.write_text(json.dumps({'version': 1, 'tunes': {
        'A': {'rank': 0.9, 'why': '1st in C64 Music'}, 'B': {'rank': 0.1}}}))
    r = sid_ranking.load(p)
    check('ranking loads', r['A']['rank'] == 0.9)
    check('a missing ranking is not an error',
          sid_ranking.load(Path(d) / 'nope.json') == {})
    (Path(d) / 'bad.json').write_text('{{{')
    check('a corrupt ranking never stops the music',
          sid_ranking.load(Path(d) / 'bad.json') == {})
    tunes = [{'id': 'A', 'moods': {}}, {'id': 'B', 'moods': {}},
             {'id': 'C', 'moods': {}}]
    sid_ranking.annotate(tunes, r)
    check('annotate stamps rank and reason',
          tunes[0]['rank'] == 0.9
          and tunes[0]['rank_why'] == '1st in C64 Music')
    check('an unranked tune gets no rank key, so weight() sees None',
          'rank' not in tunes[2])

print('review order')
tunes = [{'id': 'A', 'moods': {'eerie': 0.5}, 'settings': {}, 'arcadey': 0,
          'iconic': 0, 'confidence': 1, 'author': 'x'},
         {'id': 'B', 'moods': {'eerie': 0.5}, 'settings': {}, 'arcadey': 0,
          'iconic': 0, 'confidence': 1, 'author': 'x'},
         {'id': 'C', 'moods': {'eerie': 0.5}, 'settings': {}, 'arcadey': 0,
          'iconic': 0, 'confidence': 1, 'author': 'x'}]
ranking = {'A': {'rank': 0.2}, 'B': {'rank': 0.99}, 'C': {'rank': 0.5}}
q = build_queue(tunes, {}, status='all', ranking=ranking, order='best')
check('best-regarded tunes are reviewed first', q == ['B', 'C', 'A'], q)
q = build_queue(tunes, {}, status='all', ranking={}, order='best')
check('no ranking file means the old random order',
      sorted(q) == ['A', 'B', 'C'])

print()
if failures:
    print(f'{len(failures)} FAILED: {", ".join(failures)}')
    sys.exit(1)
print('all sid ranking tests passed')
