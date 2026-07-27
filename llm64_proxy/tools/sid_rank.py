#!/usr/bin/env python3
"""Stage 6 of the SID pipeline: rank tunes by what the C64 scene thinks.

sid_mood.py says what a tune is FOR. Nothing in the pipeline says whether
it is any GOOD - and at 10k tunes nobody can audition the library by hand
(sid_review.py is ~100 hours of listening at 40 seconds a tune). So the
selector needs an opinion it did not get from listening.

The scene has been publishing one for forty years, and DeepSID's database
has it collected in one place:

  compo     party music-competition placings. An audience voted on these;
            1st in "C64 Music" at a party is the strongest quality signal
            in the whole dataset.
  youtube   tunes somebody thought worth filming and uploading.
  usage     how many CSDb releases re-use the tune. Musicians rip and
            re-use music they rate; a tune in 40 releases is a hit.
  composer  DeepSID's composer register: the pros (Hubbard, Galway...)
            and the documented notables.
  stil      an HVSC STIL entry at all - somebody documented this tune.
  csdb      average user rating of the releases the tune appears in.
            Optional: this one needs the network (see --csdb-ratings).

Plus your own verdicts, which outrank all of it: jukebox favourites are
boosted, and anything blocked in src/sid_overrides.json is dropped.

Absence of evidence is NOT evidence of badness here - most of HVSC is
obscure demo music nobody wrote about. So the score feeds a weighting,
never a filter: MusicLibrary.pick() prefers a well-regarded tune but
still reaches the unknown ones (see sid_ranking.FLOOR).

Source data: DeepSID (Jens-Christian Huus / Chordian), whose database
dump is a single 8MB download - no crawling. https://deepsid.chordian.net

  tools/sid_rank.py --download          # fetch the dump, build ranking.json
  tools/sid_rank.py --explain 40        # ...and show the top 40 with reasons
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import sid_overrides                                   # noqa: E402
from src import sid_ranking                                     # noqa: E402

DUMP_URL = 'https://chordian.net/files/deepsid/DeepSID_Database.zip'
HVSC_PREFIX = '_High Voltage SID Collection/'

# Tables we actually read, out of the 30 in the dump.
TABLES = ('hvsc_files', 'youtube', 'competitions_cache', 'csdb',
          'sid_release_map', 'composers')

# What each signal is worth. Weights are renormalized over the signals
# actually available, so skipping the CSDb pass does not quietly deflate
# every score by 10%.
WEIGHTS = {'compo': 0.30, 'youtube': 0.20, 'usage': 0.20,
           'composer': 0.15, 'stil': 0.05, 'csdb': 0.10}

# A placing in a music compo, by place. Anything past 10th still means a
# musician entered it into a competition, which beats no signal at all.
COMPO_SCORE = {1: 1.0, 2: 0.85, 3: 0.75, 4: 0.6, 5: 0.6}
COMPO_TAIL = 0.45          # 6th-10th
COMPO_ENTERED = 0.25       # placed outside the top 10, or place unrecorded

# Only music competitions count. A tune that appeared in a winning DEMO
# says the demo was good; the audience was not voting on the music.
# DeepSID's compo register is almost entirely music compos already -
# "C64 Music", "Mixed Music", "Mixed", "C64 Sample Music", "C64 2SID" -
# with a few dozen game/intro rows to drop, so exclude rather than
# include and a compo named something new still counts.
NON_MUSIC_COMPO_RE = re.compile(r'game|intro|demo|graphic|gfx|wild',
                                re.IGNORECASE)

# Re-use saturates: 30 releases and 300 both mean "everybody knows it".
USAGE_FULL = 30

FAVOURITE_BONUS = 0.20     # your jukebox 'f' key outranks the scene


# --- MySQL dump reading ----------------------------------------------
# The dump is 30 mysqldump .sql files. Rather than require a MySQL server
# to read six tables, scan the INSERT statements directly.

def rows(sql_path: Path):
    """Yield each INSERT row as a dict of column -> string value."""
    txt = sql_path.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r'INSERT INTO `[^`]+` \(([^)]*)\) VALUES\s*', txt):
        cols = [c.strip(' `') for c in m.group(1).split(',')]
        i = m.end()
        while i < len(txt):
            while i < len(txt) and txt[i] in ' \n\r\t,':
                i += 1
            if i >= len(txt) or txt[i] != '(':
                break                      # ';' - end of this statement
            i += 1
            vals, cur, quoted = [], [], False
            while i < len(txt):
                c = txt[i]
                if quoted:
                    if c == '\\':          # \' \\ \n ... - keep it literal
                        cur.append(txt[i + 1])
                        i += 2
                        continue
                    if c == "'":
                        quoted = False
                        i += 1
                        continue
                    cur.append(c)
                    i += 1
                    continue
                if c in ' \n\r\t' and not cur:
                    i += 1                 # padding before the value
                    continue
                if c == "'":
                    quoted = True
                    i += 1
                    continue
                if c == ',':
                    vals.append(''.join(cur))
                    cur = []
                    i += 1
                    continue
                if c == ')':
                    vals.append(''.join(cur))
                    i += 1
                    break
                cur.append(c)
                i += 1
            yield dict(zip(cols, vals))


def num(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def download_dump(dest: Path):
    print(f'downloading {DUMP_URL}', file=sys.stderr)
    req = urllib.request.Request(DUMP_URL, headers={
        'User-Agent': sid_ranking.USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        dest.write_bytes(r.read())
    print(f'  {dest.stat().st_size // 1024} KB -> {dest}', file=sys.stderr)


def unpack(dump: Path, workdir: Path) -> Path:
    """Return a directory holding the .sql files we need."""
    if dump.is_dir():
        return dump
    workdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dump) as z:
        for t in TABLES:
            name = f'{t}.sql'
            out = workdir / name
            if not out.exists():
                out.write_bytes(z.read(name))
    return workdir


# --- the signals -----------------------------------------------------

def gather(sqldir: Path) -> dict:
    """Read the dump into per-HVSC-path signal records."""
    files, sig = {}, {}
    for r in rows(sqldir / 'hvsc_files.sql'):
        fid = num(r['id'])
        files[fid] = r['fullname']
        sig[fid] = {'stil': bool(r.get('stil'))}
    print(f'  {len(files)} HVSC files', file=sys.stderr)

    for r in rows(sqldir / 'youtube.sql'):
        s = sig.get(num(r['file_id']))
        if s is not None:
            s['youtube'] = s.get('youtube', 0) + 1

    placings = 0
    for r in rows(sqldir / 'competitions_cache.sql'):
        s = sig.get(num(r['file_id']))
        if s is None or NON_MUSIC_COMPO_RE.search(r.get('name', '')):
            continue
        place = num(r['place'])
        best = s.get('compo')
        # place -1 means "entered, no recorded placing"
        if place > 0 and (best is None or best < 0 or place < best):
            s['compo'] = place
        elif best is None:
            s['compo'] = -1
        s['compo_at'] = r.get('name', '')
        placings += 1
    print(f'  {placings} music-compo placings', file=sys.stderr)

    for r in rows(sqldir / 'csdb.sql'):
        s = sig.get(num(r['sid_id']))
        if s is not None:
            s['usage'] = num(r['entries'])

    for r in rows(sqldir / 'sid_release_map.sql'):
        s = sig.get(num(r['sid_id']))
        if s is not None:
            s.setdefault('releases', []).append(num(r['release_id']))

    # Composers are per HVSC folder (MUSICIANS/H/Hubbard_Rob), so the
    # register joins on the tune's directory, not the tune.
    composers = {}
    for r in rows(sqldir / 'composers.sql'):
        composers[r['fullname']] = r
    for fid, path in files.items():
        c = composers.get(path.rsplit('/', 1)[0])
        if not c:
            continue
        if c.get('focus1') == 'PRO':
            sig[fid]['composer'] = ('pro', 1.0)
        elif c.get('notable'):
            sig[fid]['composer'] = ('notable', 0.75)
        else:
            sig[fid]['composer'] = ('known', 0.35)

    return {files[fid].removeprefix(HVSC_PREFIX): s
            for fid, s in sig.items() if files[fid].startswith(HVSC_PREFIX)}


def csdb_ratings(releases, cache_path: Path, delay=1.0) -> dict:
    """User vote averages for CSDb releases, via their webservice.

    One request per release, cached on disk forever - the ratings barely
    move and their server is a hobby project. Releases nobody voted on
    have no <Rating> element and are cached as null so a rerun does not
    ask again.
    """
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except ValueError:
            pass
    todo = [r for r in releases if str(r) not in cache]
    print(f'  csdb: {len(cache)} cached, {len(todo)} to fetch '
          f'(~{len(todo) * delay / 60:.0f} min)', file=sys.stderr)
    for n, rid in enumerate(todo, 1):
        url = f'https://csdb.dk/webservice/?type=release&id={rid}'
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': sid_ranking.USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode('utf-8', 'replace')
            m = re.search(r'<Rating>([\d.]+)</Rating>', body)
            cache[str(rid)] = float(m.group(1)) if m else None
        except Exception as e:                     # noqa: BLE001
            print(f'    {rid}: {e}', file=sys.stderr)
            continue                                # retry on the next run
        if n % 50 == 0 or n == len(todo):
            cache_path.write_text(json.dumps(cache))
            print(f'    {n}/{len(todo)}', file=sys.stderr)
        time.sleep(delay)
    cache_path.write_text(json.dumps(cache))
    return {int(k): v for k, v in cache.items() if v is not None}


def score(sig: dict, ratings: dict, have_csdb: bool):
    """Signals -> (0..1 score, parts, human-readable reason)."""
    parts, why = {}, []

    place = sig.get('compo')
    if place is not None:
        if place < 0:
            parts['compo'] = COMPO_ENTERED
            why.append(f"entered {sig.get('compo_at', 'a music compo')}")
        else:
            parts['compo'] = (COMPO_SCORE.get(place) or (
                COMPO_TAIL if place <= 10 else COMPO_ENTERED))
            ord_ = {1: '1st', 2: '2nd', 3: '3rd'}.get(place, f'{place}th')
            why.append(f"{ord_} in {sig.get('compo_at', 'a music compo')}")
    else:
        parts['compo'] = 0.0

    vids = sig.get('youtube', 0)
    parts['youtube'] = min(1.0, 0.6 + 0.2 * vids) if vids else 0.0
    if vids:
        why.append(f"{vids} video{'s' if vids > 1 else ''}")

    used = sig.get('usage', 0)
    parts['usage'] = min(1.0, math.log1p(used) / math.log1p(USAGE_FULL))
    if used >= 3:
        why.append(f'used in {used} releases')

    kind_val = sig.get('composer')
    parts['composer'] = kind_val[1] if kind_val else 0.0
    if kind_val and kind_val[0] != 'known':
        why.append(f'{kind_val[0]} composer')

    parts['stil'] = 1.0 if sig.get('stil') else 0.0

    if have_csdb:
        rs = [ratings[r] for r in sig.get('releases', []) if r in ratings]
        if rs:
            best = max(rs)
            parts['csdb'] = min(1.0, best / 10.0)
            why.append(f'CSDb {best:.1f}/10')
        else:
            parts['csdb'] = 0.0

    total = sum(WEIGHTS[k] for k in parts)
    value = sum(WEIGHTS[k] * v for k, v in parts.items()) / total
    return value, parts, '; '.join(why)


def percentiles(values):
    """Midrank percentile of each value within the list, 0..1.

    Midrank, not plain rank: thousands of tunes score exactly zero (no
    scene footprint of any kind), and pinning that whole block to 0.0
    would say 'definitely bad' about music nobody has an opinion on.
    """
    order = sorted(values)
    n = len(order)
    if n < 2:
        return [1.0] * n
    import bisect
    return [((bisect.bisect_left(order, v) + bisect.bisect_right(order, v))
             / 2.0) / n for v in values]


def main():
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', type=Path, default=here / 'data/sids/moods.json',
                    help='music database to rank '
                         '(default: data/sids/moods.json)')
    ap.add_argument('--dump', type=Path,
                    help='DeepSID_Database.zip, or an unpacked directory '
                         '(default: <db dir>/deepsid/DeepSID_Database.zip)')
    ap.add_argument('--download', action='store_true',
                    help='fetch the DeepSID dump first (8MB, one time)')
    ap.add_argument('--csdb-ratings', action='store_true',
                    help='also fetch CSDb release ratings: one polite '
                         'request per release, cached, adds ~30 min the '
                         'first time')
    ap.add_argument('--csdb-delay', type=float, default=1.0,
                    help='seconds between CSDb requests (default: 1)')
    ap.add_argument('-o', '--output', type=Path,
                    help='default: <db dir>/ranking.json')
    ap.add_argument('--explain', type=int, metavar='N',
                    help='print the top N tunes and why they scored')
    ap.add_argument('--missing', type=int, metavar='N',
                    help='report the N best-regarded HVSC tunes that are '
                         'NOT in the library (relocation drops most of the '
                         'classics: worth knowing what you are missing)')
    args = ap.parse_args()

    dbdir = args.db.parent
    out = args.output or dbdir / sid_ranking.DEFAULT_NAME
    dump = args.dump or dbdir / 'deepsid' / 'DeepSID_Database.zip'
    if args.download or (not dump.exists() and not args.dump):
        dump.parent.mkdir(parents=True, exist_ok=True)
        download_dump(dump)
    if not dump.exists():
        sys.exit(f'no DeepSID dump at {dump} (use --download)')

    print('reading the DeepSID dump...', file=sys.stderr)
    sqldir = unpack(dump, dump.parent / 'sql')
    sig_by_path = gather(sqldir)

    db = json.loads(args.db.read_text())
    tunes = db['tunes']
    favourites = set()
    fav_path = dbdir / 'favorites.json'
    if fav_path.exists():
        try:
            favourites = set(json.loads(fav_path.read_text()))
        except ValueError:
            pass
    blocked = {k for k, v in sid_overrides.load().items()
               if sid_overrides.is_blocked(v)}

    # Tune ids are HVSC paths with '/' -> '__' (sid_reloc_batch.unique_name)
    sigs = {t['id']: sig_by_path.get(t['id'].replace('__', '/') + '.sid', {})
            for t in tunes}
    matched = sum(1 for s in sigs.values() if s)
    print(f'  matched {matched}/{len(tunes)} tunes to the dump',
          file=sys.stderr)

    ratings = {}
    if args.csdb_ratings:
        rel = sorted({r for s in sigs.values() for r in s.get('releases', [])})
        ratings = csdb_ratings(rel, dbdir / 'csdb_ratings.json',
                               args.csdb_delay)

    raw = {}
    for t in tunes:
        if t['id'] in blocked:
            continue                  # you already said no; do not rank it
        value, parts, why = score(sigs[t['id']], ratings, args.csdb_ratings)
        if t['id'] in favourites:
            value = min(1.0, value + FAVOURITE_BONUS)
            why = '; '.join(filter(None, ['your favourite', why]))
        raw[t['id']] = (value, parts, why)

    # Published rank is the percentile within YOUR library, not the raw
    # weighted score: raw scores bunch up near zero (most of HVSC has no
    # scene footprint at all) and would waste most of the weighting
    # range. As a percentile, 'rank' reads as "better regarded than this
    # fraction of your library" and stays meaningful when the signal mix
    # changes - e.g. when the CSDb pass is added later.
    ranked = {}
    for tid, pct in zip(raw, percentiles([v[0] for v in raw.values()])):
        value, parts, why = raw[tid]
        entry = {'rank': round(pct, 3), 'score': round(value, 3)}
        if why:
            entry['why'] = why
        keep = {k: round(v, 2) for k, v in parts.items() if v}
        if keep:
            entry['parts'] = keep
        ranked[tid] = entry

    out.write_text(json.dumps({
        'version': 1,
        'generated': str(date.today()),
        'source': 'DeepSID database dump (Chordian)'
                  + (' + CSDb release ratings' if args.csdb_ratings else ''),
        'weights': {k: WEIGHTS[k] for k in WEIGHTS
                    if k != 'csdb' or args.csdb_ratings},
        'tunes': ranked}, indent=1))

    vals = sorted(e['rank'] for e in ranked.values())
    def pct(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    print(f'{len(ranked)} tunes ranked -> {out}')
    print(f'  median {pct(0.5):.2f}  top 10% >= {pct(0.9):.2f}  '
          f'top 1% >= {pct(0.99):.2f}  max {vals[-1]:.2f}')

    if args.explain:
        titles = {t['id']: (t['title'], t.get('author', '')) for t in tunes}
        best = sorted(ranked.items(), key=lambda kv: -kv[1]['score'])
        print()
        for tid, e in best[:args.explain]:
            title, author = titles[tid]
            print(f"{e['score']:.2f}  {title[:34]:<34} {author[:22]:<22} "
                  f"{e.get('why', '')}")

    if args.missing:
        # Score the whole of HVSC the same way and see what the library
        # never got. Most of the famous game music does not survive
        # relocation into the $B000 window, and that is invisible from
        # inside the library - every tune in there looks fine.
        have = {t['id'] for t in tunes}
        gap = []
        for path, s in sig_by_path.items():
            tid = path.removesuffix('.sid').replace('/', '__')
            if tid in have:
                continue
            value, _, why = score(s, ratings, args.csdb_ratings)
            gap.append((value, path, why))
        gap.sort(reverse=True)
        local = dbdir / 'C64Music'
        print(f'\nbest-regarded HVSC tunes NOT in the library '
              f'({len(gap)} candidates):')
        for value, path, why in gap[:args.missing]:
            here_ = 'on disk' if (local / path).exists() else 'not fetched'
            print(f'  {value:.2f}  {path[:64]:<64} [{here_}] {why[:44]}')


if __name__ == '__main__':
    main()
