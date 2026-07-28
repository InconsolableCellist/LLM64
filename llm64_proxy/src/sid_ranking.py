"""How well-regarded a tune is, and what that does to selection.

tools/sid_rank.py cross-references the library against the C64 scene's
own opinion - party music-compo placings, re-use across releases, YouTube
uploads, the composer register - and writes ranking.json beside the music
database. This module is the runtime half: the proxy reads that file and
lets it weight which tune plays.

It is deliberately a weighting and not a filter. Most of HVSC is obscure
demo music nobody ever wrote about, and an unranked tune is unknown, not
bad - so the worst-regarded tune still plays FLOOR as often as the best
one at the same mood fit, and nothing is ever silently unplayable. Tunes
you actually reject go in sid_overrides.json, by ear.

The file is derived data, so it lives in data/ next to moods.json (not in
src/ like the hand verdicts) and can be rebuilt any time. Absent, every
tune weighs the same and the library behaves as it did before.
"""

import json
from pathlib import Path

DEFAULT_NAME = 'ranking.json'

# Weight of the least-regarded tune relative to the best-regarded one at
# equal mood fit, and how sharply the preference grows in between.
#
# Measured over the real 10k library (mean regard percentile of the tunes
# the selector actually serves, against 0.52 with no ranking at all):
#
#   FLOOR .2 CURVE 1   0.63 served, 15% of picks from the bottom third
#   FLOOR .15 CURVE 2  0.69 served, 11%          <- here
#   FLOOR .05 CURVE 4  0.78 served,  6%
#
# Squaring the percentile is what makes the difference felt without
# collapsing the library onto the few hundred tunes the scene documented:
# an unheard tune is unknown, not bad, and one pick in nine still comes
# from the bottom third - which is also what keeps sid_review.py finding
# things worth blocking.
FLOOR = 0.15
CURVE = 2

# Sent when sid_rank.py fetches the DeepSID dump or CSDb ratings. Kept
# here so both halves agree, and so anyone reading their logs can see
# what this is.
USER_AGENT = ('llm64-sid-rank/1.0 (personal HVSC library ranking; '
              'one-time metadata fetch)')


def load(path) -> dict:
    """{tune_id: {'rank': 0..1, 'why': str}}. Missing file = no ranking."""
    try:
        db = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    tunes = db.get('tunes')
    return tunes if isinstance(tunes, dict) else {}


def annotate(tunes, ranking: dict):
    """Stamp rank/rank_why onto tune records, in place."""
    for t in tunes:
        e = ranking.get(t.get('id'))
        if e:
            t['rank'] = e.get('rank')
            if e.get('why'):
                t['rank_why'] = e['why']
    return tunes


def weight(rank) -> float:
    """Selection multiplier for a tune's rank. Unranked tunes weigh 1.0 -
    no ranking data must never mean 'plays less than everything else'."""
    if rank is None:
        return 1.0
    return FLOOR + (1.0 - FLOOR) * max(0.0, min(1.0, float(rank))) ** CURVE
