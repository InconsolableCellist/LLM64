"""Human verdicts on SID tunes, layered over the generated database.

moods.json is machine output: sid_mood.py asks a model to tag ~10k tunes
from filenames and STIL notes, and sid_makedb.py assembles the result.
The model is often wrong in ways only ears catch - it tags a bouncy
chiptune 'eerie' because the game was a horror game - and some tunes are
simply bad, or survive relocation badly. Those judgements come from a
person listening (tools/sid_review.py), and re-running the tagger must
not erase them.

So they live here in src/, keyed by tune id: version-controlled, deployed
with the proxy, and applied in two places -

  - sid_makedb.py, so a freshly built moods.json is already corrected;
  - MusicLibrary, so the moods.json already on disk is corrected at load
    time. Rebuilding the database is an hours-long pipeline and a verdict
    ("that tune is awful") has to take effect long before the next one.

An entry's presence IS the reviewed-by-a-human flag: anything in this
file was heard by someone, anything absent is still the tagger's guess.
That distinction survives into the runtime record as tune['source'].

File format (version 1):

  {"version": 1,
   "tunes": {
     "<tune id>": {
       "verdict":  "keep" | "blocked",   # blocked = never play it
       "reason":   "why it was blocked",
       "moods":    {"eerie": 0.8},       # replaces the auto tags wholesale
       "settings": {"horror": 0.5},
       "arcadey": 0.2, "iconic": 0.0, "confidence": 1.0,
       "note":     "free text for the next person",
       "reviewed": "2026-07-26",
       "auto":     {...}                 # what the tagger had said
     }}}

Every field except 'verdict' is optional: an entry that only says
"keep" means a listener confirmed the automatic tags were right.
"""

import json
import os
from pathlib import Path

from .respath import resource_dir

VERSION = 1
DEFAULT_PATH = resource_dir() / 'sid_overrides.json'

# Fields a human verdict may replace on a tune record. Everything else in
# a tune (load/init/play addresses, size, file, secs...) is measured, not
# judged, and is never overridden by hand.
FIELDS = ('moods', 'settings', 'arcadey', 'iconic', 'confidence')


def load(path=None) -> dict:
    """Return {tune_id: entry}. A missing or broken file means no
    verdicts, never a crash: the proxy must still play music."""
    p = Path(path) if path else DEFAULT_PATH
    try:
        db = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    tunes = db.get('tunes')
    return tunes if isinstance(tunes, dict) else {}


def save(entries: dict, path=None):
    """Write verdicts back. Sorted keys and one-space indent so a review
    session shows up as a readable diff, and written via a temp file so
    an interrupted save cannot truncate the record of past sessions."""
    p = Path(path) if path else DEFAULT_PATH
    body = json.dumps({'version': VERSION,
                       'tunes': {k: entries[k] for k in sorted(entries)}},
                      indent=1, sort_keys=False)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(body + '\n')
    os.replace(tmp, p)


def is_blocked(entry) -> bool:
    return bool(entry) and entry.get('verdict') == 'blocked'


def clean_weights(d) -> dict:
    """Drop zero/absent weights: a mood at 0.0 is not a mood, and the
    selector's `moods.get(mood, 0) > 0` test would happily keep it."""
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items()
            if isinstance(v, (int, float)) and v > 0}


def apply(tune: dict, entry: dict):
    """Merge one verdict into one tune record.

    Returns the merged copy, or None when the tune is blocked (callers
    drop it). Tunes with no entry come back marked 'auto' so downstream
    code - and the review tool's own queue - can tell heard from guessed.
    """
    merged = dict(tune)
    if not entry:
        merged.setdefault('source', 'auto')
        return merged
    if is_blocked(entry):
        return None
    for f in FIELDS:
        if f in entry:
            merged[f] = (clean_weights(entry[f])
                         if f in ('moods', 'settings') else entry[f])
    merged['source'] = 'manual'
    if entry.get('reviewed'):
        merged['reviewed'] = entry['reviewed']
    return merged


def apply_all(tunes, entries: dict) -> list:
    """Whole-library pass: blocked tunes gone, verdicts merged in."""
    out = []
    for t in tunes:
        merged = apply(t, entries.get(t.get('id')))
        if merged is not None:
            out.append(merged)
    return out
