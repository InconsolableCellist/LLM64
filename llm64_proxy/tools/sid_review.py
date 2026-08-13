#!/usr/bin/env python3
"""Listen to the music library and fix what the tagger got wrong.

sid_mood.py tags ~10k tunes from filenames and STIL notes, which means it
is confidently wrong a lot: it hears "horror game" in a path and tags a
bouncy chiptune 'eerie', so the narrator asks for eerie music and the
scene gets carnival organ. And some tunes are just bad, or relocate
badly, and should never play at all.

This is the ears in that loop. It deals you a tune, plays it through
VICE's vsid, and lets you retag it, block it, or confirm the automatic
tags were right. Verdicts go to src/sid_overrides.json - NOT to
moods.json, which is generated - so they survive the next tagger run and
deploy with the proxy. See src/sid_overrides.py.

Nobody is going to sit through 10k tunes, so tunes are dealt
best-regarded first when tools/sid_rank.py has built a ranking: your
listening time goes to the music the game is most likely to play, and
each tune shows what the scene thinks of it. --order random for the
old behavior.

Usage:
  tools/sid_review.py                       # best-regarded unreviewed tunes
  tools/sid_review.py --mood eerie          # audit one mood bucket
  tools/sid_review.py --mood eerie --as-selected
                                            # ...weighted the way the game
                                            #    actually picks, so you hear
                                            #    what players hear first
  tools/sid_review.py --status blocked      # revisit your own rejects
  tools/sid_review.py --author Hubbard      # audit one composer

Keys are listed on screen; '?' for the full set.
"""

import argparse
import curses
import json
import random
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import sid_overrides                     # noqa: E402
from src import sid_ranking                       # noqa: E402
from sid_mood import MOODS, SETTINGS              # noqa: E402

# The three 0..1 dials the selector weighs alongside mood fit. Same names
# as in the tune record; kept in one list so the UI can treat them like
# any other editable row.
SCORES = ('arcadey', 'iconic', 'confidence')

# A human said so, so believe it: the selector skips tunes under 0.5
# confidence, and everything reviewed here was actually heard.
REVIEWED_CONFIDENCE = 1.0

# Weight a bare space-bar toggle turns a tag on at. Most hand tags are
# "yes this fits" rather than a fine gradation; nudge from there.
TOGGLE_WEIGHT = 0.7

BLOCK_PRESETS = {
    '1': 'broken or glitchy playback',
    '2': 'bad music',
    '3': 'wrong for the game (too arcadey / too iconic)',
}


# --- pure logic (unit-tested in tests/test_sid_overrides.py) ----------

def working_values(tune: dict, entry) -> dict:
    """The editable state of a tune: the human's values where they exist,
    the tagger's where they do not. Blocked tunes keep their tags so
    unblocking restores something sensible."""
    vals = {'moods': dict(tune.get('moods') or {}),
            'settings': dict(tune.get('settings') or {})}
    for s in SCORES:
        vals[s] = float(tune.get(s) or 0.0)
    if entry:
        for f in ('moods', 'settings'):
            if f in entry:
                vals[f] = dict(entry[f])
        for s in SCORES:
            if s in entry:
                vals[s] = float(entry[s])
    return vals


def make_entry(tune: dict, vals: dict, blocked=False, reason='', note='',
               today=None) -> dict:
    """Build the verdict written to sid_overrides.json.

    Always records the tagger's original values under 'auto' when they
    differ: knowing what the model said and what the ear said is what
    lets us improve the tagging prompt later.
    """
    entry = {'verdict': 'blocked' if blocked else 'keep'}
    if reason:
        entry['reason'] = reason
    if not blocked:
        entry['moods'] = sid_overrides.clean_weights(vals['moods'])
        entry['settings'] = sid_overrides.clean_weights(vals['settings'])
        for s in SCORES:
            entry[s] = round(float(vals[s]), 2)
        entry['confidence'] = REVIEWED_CONFIDENCE
    if note:
        entry['note'] = note
    entry['reviewed'] = str(today or date.today())
    auto = {'moods': sid_overrides.clean_weights(tune.get('moods')),
            'settings': sid_overrides.clean_weights(tune.get('settings'))}
    for s in SCORES:
        auto[s] = tune.get(s)
    if blocked or any(entry.get(k) != auto.get(k)
                      for k in ('moods', 'settings') + SCORES):
        entry['auto'] = auto
    return entry


def pick_weight(tune: dict, vals: dict, mood: str) -> float:
    """MusicLibrary.pick's weighting, so --as-selected deals the tunes a
    player is most likely to actually hear. Kept in step with music.py."""
    w = vals['moods'].get(mood, 0.0)
    return w * (1.0 - 0.5 * vals['arcadey']) * (1.0 - 0.6 * vals['iconic'])


def build_queue(tunes, entries, status='unreviewed', mood=None, author=None,
                as_selected=False, rng=None, ranking=None,
                order='best') -> list:
    """Filtered, ordered list of tune ids to review.

    Filters see the CURRENT state - a tune you retagged to 'serene'
    leaves the 'eerie' bucket immediately - because the point of an audit
    pass is to check what the game will do now, not what it did before.

    Order is best-regarded first when a ranking exists (tools/sid_rank.py).
    Nobody is going to hear 10k tunes, so the hours you do spend should
    go to the ones the game is most likely to play.
    """
    rng = rng or random
    pool = []
    for t in tunes:
        entry = entries.get(t.get('id'))
        blocked = sid_overrides.is_blocked(entry)
        if status == 'unreviewed' and entry:
            continue
        if status == 'reviewed' and (not entry or blocked):
            continue
        if status == 'blocked' and not blocked:
            continue
        if status in ('unreviewed', 'reviewed') and blocked:
            continue        # a blocked tune is out of the game; don't deal it
        vals = working_values(t, entry)
        if mood and vals['moods'].get(mood, 0) <= 0:
            continue
        if author and author.lower() not in (t.get('author') or '').lower():
            continue
        pool.append((t, vals))
    ranking = ranking or {}
    if as_selected and mood:
        # Weighted shuffle (Efraimidis-Spirakis): a tune's chance of
        # coming up early is its chance of being picked in-game, but
        # every tune in the bucket still eventually comes up.
        keyed = [(rng.random() ** (1.0 / max(
            pick_weight(t, v, mood) * sid_ranking.weight(
                (ranking.get(t['id']) or {}).get('rank')), 1e-6)),
                  t['id']) for t, v in pool]
        keyed.sort(reverse=True)
        return [i for _, i in keyed]
    ids = [t['id'] for t, _ in pool]
    rng.shuffle(ids)                     # ties broken randomly, not by path
    if order == 'best' and ranking:
        ids.sort(key=lambda i: -((ranking.get(i) or {}).get('rank') or 0))
    return ids


def hvsc_original(tune: dict, hvsc_root: Path):
    """The un-relocated HVSC file, for A/B-ing a suspect relocation.

    Tune ids are HVSC paths with '/' turned into '__' (sid_reloc_batch.
    unique_name), which is ambiguous for the rare name containing '__';
    those simply come back None and the key does nothing.
    """
    if not hvsc_root:
        return None
    p = hvsc_root / (tune['id'].replace('__', '/') + '.sid')
    return p if p.exists() else None


# --- playback --------------------------------------------------------

class Player:
    """vsid in console mode: no window, real reSID emulation, loops until
    killed. One tune at a time; starting another kills the first."""

    def __init__(self, normalize=True):
        self.proc = None
        self.normalize = normalize
        self.path = None
        self.subtune = 1

    def volume(self, tune) -> int:
        """Match what the C64 will do. The client writes vol_byte to
        $D418 after every play call, so a tune the pipeline decided to
        attenuate must be auditioned attenuated - half the 'this tune is
        too much' verdicts are really loudness."""
        vb = tune.get('vol_byte')
        if not self.normalize or vb is None:
            return 100
        return max(5, round(100 * (vb & 0x0F) / 15))

    def play(self, path: Path, tune: dict, subtune=None):
        self.stop()
        self.path = path
        self.subtune = subtune or max(1, tune.get('start_song', 1))
        self.proc = subprocess.Popen(
            ['vsid', '-console', '+saveres', '-silent',
             '-tune', str(self.subtune),
             '-soundvolume', str(self.volume(tune)), str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    @property
    def playing(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None


# --- curses UI -------------------------------------------------------

HELP = [
    ("up/down", "move cursor"),
    ("tab", "moods <-> settings/scores column"),
    ("left/right", "nudge weight -/+ 0.1"),
    ("0-9", "set weight (0 clears the tag)"),
    ("space", "toggle tag off / on at %.1f" % TOGGLE_WEIGHT),
    ("enter", "save verdict, next tune"),
    ("n", "next tune, discarding edits"),
    ("p", "previous tune"),
    ("b", "block this tune (asks why)"),
    ("u", "undo my verdict (back to auto tags)"),
    ("r", "replay   o: original HVSC file   s: stop"),
    (", .", "previous / next subtune"),
    ("v", "volume: as the C64 hears it / raw"),
    ("t", "type a note"),
    ("q", "quit"),
]

C_HEAD, C_KEY, C_CHANGED, C_BLOCK, C_OK, C_DIM = 1, 2, 3, 4, 5, 6


class Reviewer:
    def __init__(self, stdscr, tunes, entries, queue, args, ranking=None):
        self.scr = stdscr
        self.tunes = {t['id']: t for t in tunes}
        self.entries = entries
        self.queue = queue
        self.args = args
        self.ranking = ranking or {}
        self.ordered = bool(self.ranking) and args.order == 'best'
        self.hvsc = args.hvsc if args.hvsc and args.hvsc.is_dir() else None
        self.pos = 0
        self.history = []
        self.player = Player(normalize=not args.raw_volume)
        self.msg = ''
        self.col = 0          # 0 = moods, 1 = settings+scores
        self.row = 0
        self.dirty = False
        self.rows = [MOODS, list(SETTINGS) + list(SCORES)]
        self.load_current()

    # -- current tune state --

    @property
    def tune(self):
        return self.tunes[self.queue[self.pos]]

    @property
    def entry(self):
        return self.entries.get(self.tune['id'])

    def load_current(self):
        self.vals = working_values(self.tune, self.entry)
        self.note = (self.entry or {}).get('note', '')
        self.dirty = False
        self.play_relocated()

    def sid_path(self, original=False) -> Path:
        if original:
            return hvsc_original(self.tune, self.hvsc)
        p = Path(self.tune['file'])
        return p if p.is_absolute() else self.args.db.parent / p

    def play_relocated(self, subtune=None):
        p = self.sid_path()
        if p.exists():
            self.player.play(p, self.tune, subtune)
        else:
            self.msg = f'missing file: {p}'

    # -- editing --

    def current_key(self):
        return self.rows[self.col][self.row]

    def get_val(self, key):
        if key in SCORES:
            return self.vals[key]
        field = 'moods' if key in MOODS else 'settings'
        return self.vals[field].get(key, 0.0)

    def set_val(self, key, v):
        v = round(min(1.0, max(0.0, v)), 2)
        if key in SCORES:
            self.vals[key] = v
        else:
            field = 'moods' if key in MOODS else 'settings'
            if v > 0:
                self.vals[field][key] = v
            else:
                self.vals[field].pop(key, None)
        self.dirty = True

    def auto_val(self, key):
        """What the tagger said, for the changed-from-auto marker."""
        t = self.tune
        if key in SCORES:
            return float(t.get(key) or 0.0)
        field = 'moods' if key in MOODS else 'settings'
        return float((t.get(field) or {}).get(key, 0.0))

    # -- verdicts --

    def commit(self, blocked=False, reason=''):
        entry = make_entry(self.tune, self.vals, blocked=blocked,
                           reason=reason, note=self.note)
        self.entries[self.tune['id']] = entry
        self.save()
        self.msg = 'blocked: ' + reason if blocked else 'saved'
        self.advance()

    def revert(self):
        if self.entries.pop(self.tune['id'], None) is None:
            self.msg = 'no verdict on this tune'
            return
        self.save()
        self.vals = working_values(self.tune, None)
        self.note = ''
        self.dirty = False
        self.msg = 'reverted to the automatic tags'

    def save(self):
        try:
            sid_overrides.save(self.entries, self.args.overrides)
        except OSError as e:
            self.msg = f'SAVE FAILED: {e}'

    def advance(self, step=1):
        if step > 0:
            self.history.append(self.pos)
            self.pos += 1
            if self.pos >= len(self.queue):
                return False
        else:
            if not self.history:
                self.msg = 'no previous tune'
                return True
            self.pos = self.history.pop()
        self.load_current()
        return True

    # -- drawing --

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        t, e = self.tune, self.entry
        blocked = sid_overrides.is_blocked(e)

        done = len(self.entries)
        nblock = sum(1 for x in self.entries.values()
                     if sid_overrides.is_blocked(x))
        head = (f' SID review  {self.pos + 1}/{len(self.queue)} in queue'
                f'{" (best first)" if self.ordered else ""}  '
                f'| library: {done} reviewed, {nblock} blocked, '
                f'{len(self.tunes)} total ')
        scr.addstr(0, 0, head[:w - 1].ljust(w - 1), curses.color_pair(C_HEAD))

        y = 2
        scr.addstr(y, 1, t['title'][:w - 2], curses.A_BOLD)
        y += 1
        scr.addstr(y, 1, f"{t.get('author', '?')}"[:w - 2],
                   curses.color_pair(C_DIM))
        y += 1
        bits = [t['id']]
        if t.get('secs'):
            bits.append(f"{t['secs']:.0f}s")
        if t.get('songs', 1) > 1:
            bits.append(f"subtune {self.player.subtune}/{t['songs']}")
        if t.get('rms_db'):
            bits.append(f"{t['rms_db']:.0f} dB")
        if t.get('vol_byte') is not None:
            bits.append(f"vol {self.player.volume(t)}%")
        scr.addstr(y, 1, ' | '.join(bits)[:w - 2], curses.color_pair(C_DIM))
        y += 1
        # What the scene thinks of it, so a verdict is not formed in a
        # vacuum: "1st in C64 Music" is worth knowing before you judge.
        rk = self.ranking.get(t['id'])
        if rk:
            regard = (f"regard {rk.get('rank', 0):.0%}"
                      + (f" - {rk['why']}" if rk.get('why') else ''))
            scr.addstr(y, 1, regard[:w - 2], curses.color_pair(C_OK)
                       if rk.get('rank', 0) > 0.9 else curses.A_NORMAL)
            y += 1
        if blocked:
            scr.addstr(y, 1, f"BLOCKED: {e.get('reason', '')} "
                             f"({e.get('reviewed', '')})"[:w - 2],
                       curses.color_pair(C_BLOCK) | curses.A_BOLD)
        elif e:
            scr.addstr(y, 1, f"reviewed by hand {e.get('reviewed', '')}"
                             f"{'  *edited' if self.dirty else ''}"[:w - 2],
                       curses.color_pair(C_OK))
        else:
            scr.addstr(y, 1, ('auto-tagged, unheard'
                              + ('  *edited' if self.dirty else ''))[:w - 2],
                       curses.color_pair(C_DIM))
        y += 2

        top = y
        colx = (2, max(35, w // 2))
        scr.addstr(top - 1, colx[0], 'MOODS', curses.A_UNDERLINE)
        scr.addstr(top - 1, colx[1], 'SETTINGS / SCORES', curses.A_UNDERLINE)
        for ci, keys in enumerate(self.rows):
            yy = top
            for ri, key in enumerate(keys):
                if key in SCORES and ri and keys[ri - 1] not in SCORES:
                    yy += 1      # blank line between settings and scores
                if yy >= h - 2:
                    break
                v = self.get_val(key)
                changed = abs(v - self.auto_val(key)) > 0.001
                bar = '#' * int(round(v * 10))
                line = f'{key:<17}{v:4.1f} {bar:<10}'
                if changed:
                    attr = curses.color_pair(C_CHANGED)
                elif v <= 0:
                    attr = curses.color_pair(C_DIM)
                else:
                    attr = curses.A_NORMAL
                if ci == self.col and ri == self.row:
                    attr |= curses.A_REVERSE
                scr.addstr(yy, colx[ci], line[:max(0, w - colx[ci] - 1)], attr)
                yy += 1

        foot = h - 1
        if self.note:
            scr.addstr(foot - 2, 1, f'note: {self.note}'[:w - 2],
                       curses.color_pair(C_DIM))
        keys = ('enter save  n next  p prev  b block  u undo  r replay  '
                'o orig  space toggle  0-9 set  ? help  q quit')
        scr.addstr(foot - 1, 1, keys[:w - 2], curses.color_pair(C_KEY))
        state = 'playing' if self.player.playing else 'stopped'
        scr.addstr(foot, 1, f'[{state}] {self.msg}'[:w - 2])
        scr.refresh()

    def prompt(self, text) -> str:
        h, w = self.scr.getmaxyx()
        self.scr.addstr(h - 1, 0, ' ' * (w - 1))
        self.scr.addstr(h - 1, 1, text[:w - 2], curses.A_BOLD)
        self.scr.refresh()
        curses.echo()
        curses.curs_set(1)
        self.scr.timeout(-1)
        try:
            raw = self.scr.getstr(h - 1, min(len(text) + 2, w - 3), 120)
            return raw.decode('utf-8', 'replace').strip()
        except (curses.error, KeyboardInterrupt):
            return ''
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.scr.timeout(500)

    def show_help(self):
        self.scr.erase()
        self.scr.addstr(0, 1, 'SID review - keys', curses.A_BOLD)
        for i, (k, d) in enumerate(HELP):
            if i + 2 >= self.scr.getmaxyx()[0] - 2:
                break
            self.scr.addstr(i + 2, 2, f'{k:<12}', curses.color_pair(C_KEY))
            self.scr.addstr(i + 2, 15, d)
        self.scr.addstr(self.scr.getmaxyx()[0] - 1, 1, 'any key to return',
                        curses.color_pair(C_DIM))
        self.scr.refresh()
        self.scr.timeout(-1)
        self.scr.getch()
        self.scr.timeout(500)

    # -- main loop --

    def run(self):
        self.scr.timeout(500)          # tick so 'playing' stays honest
        while True:
            self.draw()
            try:
                c = self.scr.getch()
            except KeyboardInterrupt:
                break
            if c == -1:
                continue
            self.msg = ''
            # 'q' only: ESC is the first byte of every arrow key, and a
            # stray one must not end a review session.
            if c == ord('q'):
                if self.dirty and not self.prompt(
                        'edits on this tune are unsaved - quit anyway? [y/N] '
                ).lower().startswith('y'):
                    continue
                break
            if not self.handle(c):
                break
        self.player.stop()

    def handle(self, c) -> bool:
        """False ends the session (queue exhausted)."""
        keys = self.rows[self.col]
        if c in (curses.KEY_DOWN, ord('j')):
            self.row = (self.row + 1) % len(keys)
        elif c in (curses.KEY_UP, ord('k')):
            self.row = (self.row - 1) % len(keys)
        elif c == ord('\t'):
            self.col = 1 - self.col
            self.row = min(self.row, len(self.rows[self.col]) - 1)
        elif c == curses.KEY_RIGHT:
            self.set_val(self.current_key(), self.get_val(self.current_key())
                         + 0.1)
        elif c == curses.KEY_LEFT:
            self.set_val(self.current_key(), self.get_val(self.current_key())
                         - 0.1)
        elif ord('0') <= c <= ord('9'):
            self.set_val(self.current_key(), (c - ord('0')) / 10.0)
        elif c == ord(' '):
            key = self.current_key()
            self.set_val(key, 0.0 if self.get_val(key) > 0 else TOGGLE_WEIGHT)
        elif c in (curses.KEY_ENTER, 10, 13):
            if not self.advance_after(lambda: self.commit()):
                return False
        elif c == ord('n'):
            if not self.advance():
                return False
        elif c == ord('p'):
            self.advance(-1)
        elif c == ord('b'):
            reason = self.prompt('block, why? [1 broken  2 bad music  '
                                 '3 wrong for game  or type]: ')
            if reason:
                why = BLOCK_PRESETS.get(reason, reason)
                if not self.advance_after(lambda: self.commit(True, why)):
                    return False
            else:
                self.msg = 'block canceled (a reason is required)'
        elif c == ord('u'):
            self.revert()
        elif c == ord('r'):
            self.play_relocated(self.player.subtune)
        elif c == ord('o'):
            p = self.sid_path(original=True)
            if p:
                self.player.play(p, self.tune, self.player.subtune)
                self.msg = 'playing the original HVSC file (r: back to ours)'
            else:
                self.msg = 'no original found under --hvsc'
        elif c == ord('s'):
            self.player.stop()
        elif c in (ord('.'), ord(',')):
            n = self.tune.get('songs', 1)
            step = 1 if c == ord('.') else -1
            self.play_relocated(min(n, max(1, self.player.subtune + step)))
        elif c == ord('v'):
            self.player.normalize = not self.player.normalize
            self.msg = ('volume: as the C64 hears it' if self.player.normalize
                        else 'volume: raw')
            self.play_relocated(self.player.subtune)
        elif c == ord('t'):
            self.note = self.prompt('note: ') or self.note
            self.dirty = True
        elif c == ord('?'):
            self.show_help()
        return True

    def advance_after(self, action) -> bool:
        """Run a verdict, report exhaustion of the queue rather than
        crashing on the next draw."""
        before = self.pos
        action()
        if self.pos >= len(self.queue):
            self.pos = before
            self.msg = 'queue finished'
            return False
        return True


def main():
    default_db = (Path(__file__).resolve().parent.parent
                  / 'data' / 'sids' / 'moods.json')
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', type=Path, default=default_db,
                    help='music database (default: data/sids/moods.json)')
    ap.add_argument('--overrides', type=Path,
                    default=sid_overrides.DEFAULT_PATH,
                    help='verdict file (default: src/sid_overrides.json)')
    ap.add_argument('--hvsc', type=Path, default=None,
                    help="HVSC C64Music root, for 'o' (play the original, "
                         'un-relocated file); default: alongside the db')
    ap.add_argument('--status', choices=('unreviewed', 'reviewed', 'blocked',
                                         'all'), default='unreviewed',
                    help='which tunes to deal (default: unreviewed)')
    ap.add_argument('--mood',
                    help='only tunes currently tagged with this mood')
    ap.add_argument('--author', help='only tunes whose author matches')
    ap.add_argument('--order', choices=('best', 'random'), default='best',
                    help='best: best-regarded first, so limited listening '
                         'time goes where it matters (needs ranking.json '
                         'from tools/sid_rank.py). Default: best')
    ap.add_argument('--ranking', type=Path,
                    help='ranking file (default: <db dir>/ranking.json)')
    ap.add_argument('--as-selected', action='store_true',
                    help='with --mood, deal in the order the game would '
                         'pick, so the most-likely-to-be-heard come first')
    ap.add_argument('--raw-volume', action='store_true',
                    help='do not audition at the C64 playback volume')
    ap.add_argument('--seed', type=int, help='reproducible shuffle')
    args = ap.parse_args()

    if args.hvsc is None:
        cand = args.db.parent / 'C64Music'
        args.hvsc = cand if cand.is_dir() else None

    try:
        db = json.loads(args.db.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f'cannot read {args.db}: {e}')
    tunes = db['tunes']
    entries = sid_overrides.load(args.overrides)
    ranking = sid_ranking.load(args.ranking
                               or args.db.parent / sid_ranking.DEFAULT_NAME)
    rng = random.Random(args.seed) if args.seed is not None else random
    queue = build_queue(tunes, entries, status=args.status, mood=args.mood,
                        author=args.author, as_selected=args.as_selected,
                        rng=rng, ranking=ranking, order=args.order)
    if not queue:
        sys.exit('nothing to review with those filters')

    if not shutil.which('vsid'):
        sys.exit("vsid (VICE's SID player) is not on PATH")

    reviewer = []

    def go(stdscr):
        curses.curs_set(0)
        # The screen redraws on a 500ms tick (to keep 'playing' honest),
        # and ncurses' default 1s wait for the rest of an escape sequence
        # is longer than that: an arrow key would time out half-read and
        # eat the next keystrokes with it. Resolve escapes well inside
        # the tick instead.
        if hasattr(curses, 'set_escdelay'):
            curses.set_escdelay(25)
        curses.use_default_colors()
        for pair, fg in ((C_HEAD, curses.COLOR_CYAN),
                         (C_KEY, curses.COLOR_CYAN),
                         (C_CHANGED, curses.COLOR_YELLOW),
                         (C_BLOCK, curses.COLOR_RED),
                         (C_OK, curses.COLOR_GREEN),
                         (C_DIM, curses.COLOR_BLUE)):
            curses.init_pair(pair, fg, -1)
        reviewer.append(Reviewer(stdscr, tunes, entries, queue, args,
                                 ranking))
        reviewer[0].run()

    try:
        curses.wrapper(go)
    finally:
        # A crash must not leave a tune playing into an empty terminal.
        if reviewer:
            reviewer[0].player.stop()
    done = len(entries)
    print(f'{done} verdicts on file '
          f'({sum(1 for e in entries.values() if sid_overrides.is_blocked(e))}'
          f' blocked) in {args.overrides}')


if __name__ == '__main__':
    main()
