#!/usr/bin/env python3
"""Stage 1 of the MIDI pipeline: fetch a corpus from VGMusic.

The SID pipeline starts by unpacking one HVSC archive. VGMusic has no
archive, so this stands in for that stage - but it buys something HVSC
never gave us. HVSC's context is the *path* (`GAMES/S-Z/Zak_McKracken`),
which sid_mood.py mines because it is all there is. A VGMusic index page
is a table:

    <tr class="gameheader"><td ...>Ultima VII</td></tr>
    <tr><td><a href="u7_stones.mid">Stones</a>
        <td align="right">21033 bytes
        <td align="center">Sequenced by whoever

so every file arrives with the GAME, a HUMAN-WRITTEN SONG TITLE and the
sequencer's name. That is strictly more than a filename, and it is what
makes the mood tagger worth trusting (see midi_mood.py).

The files themselves are not redistributable - each is copyrighted by
whoever sequenced it, exactly like every tune in HVSC - so this
downloads to your machine and nothing here ever ends up in git. See
README's music section for the same position stated about SIDs.

Politeness: one connection, sequential, a delay between requests, and a
User-Agent that says who is calling. VGMusic asks people not to link
directly to the files from a web page; archiving a copy locally is a
different thing, but there is no reason to be rude about it.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.vgmusic.com"
UA = ("llm64-midi-fetch/1.0 (personal music library for a hobby "
      "text-adventure client; contact via github.com/InconsolableCellist)")

# Platforms worth having for a text adventure's soundtrack, in the order
# a listener would miss them. The PC section is first on purpose: the
# client this music is for IS a 1993 PC, so its own games' scores are the
# most honest thing it could be playing.
DEFAULT_PLATFORMS = [
    "computer/microsoft/windows",    # DOS & Windows - Sierra, LucasArts
    "computer/commodore/amiga",      # the C64's successor, same composers
    "console/sega/genesis",          # Phantasy Star, Shining Force
    "console/nintendo/snes",         # the RPG goldmine
]

# One <tr> per row; the table is machine-generated and has been stable
# for two decades, which is why regex is honest here and bs4 is a
# dependency this repo does not otherwise need.
ROW_RE = re.compile(r"<tr\b(.*?)(?=<tr\b|</table)", re.I | re.S)
GAME_RE = re.compile(r'class="header"[^>]*>(.*?)</td>', re.I | re.S)
FILE_RE = re.compile(r'<a\s+href="([^"]+\.mid)"[^>]*>(.*?)</a>', re.I | re.S)
SIZE_RE = re.compile(r'align="right"[^>]*>\s*(\d+)\s*bytes', re.I)
# The sequencer cell is <td align="center">Name; the Comments cell also
# carries align="center" but always has a class= first, so requiring
# align to come immediately after the tag separates them.
SEQ_RE = re.compile(r'<td\s+align="center"[^>]*>([^<]*)', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def text(raw: str) -> str:
    """Strip tags and decode the handful of entities this table uses."""
    s = TAG_RE.sub("", raw)
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return " ".join(s.split())


def get(url: str, tries: int = 3, timeout: int = 30) -> bytes:
    """One GET with backoff. Returns b'' when the file is simply gone -
    a dead link in a 30-year-old index is not a reason to stop."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return b""
            if attempt == tries - 1:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt)
    return b""


def parse_index(html: str):
    """Index page -> [{file, game, title, seq, size}], in page order."""
    out, game = [], ""
    for m in ROW_RE.finditer(html):
        row = m.group(0)
        if 'class="gameheader"' in row.lower():
            g = GAME_RE.search(row)
            if g:
                game = text(g.group(1))
            continue
        f = FILE_RE.search(row)
        if not f:
            continue
        size = SIZE_RE.search(row)
        seq = SEQ_RE.search(row)
        out.append({
            "file": f.group(1),
            "game": game,
            "title": text(f.group(2)),
            "seq": text(seq.group(1)) if seq else "",
            "size": int(size.group(1)) if size else 0,
        })
    return out


def fetch_platform(platform: str, root: Path, delay: float,
                   limit: int = 0) -> list:
    """Download one platform's directory. Resumable: an existing file of
    the right size is left alone, so an interrupted run costs only the
    index page."""
    slug = platform.replace("/", "__")
    outdir = root / slug
    outdir.mkdir(parents=True, exist_ok=True)

    url = f"{BASE}/music/{platform}/"
    print(f"  index {url}", flush=True)
    entries = parse_index(get(url).decode("utf-8", "replace"))
    if limit:
        entries = entries[:limit]
    print(f"  {len(entries)} files listed", flush=True)

    records, got, skipped, failed = [], 0, 0, 0
    for i, e in enumerate(entries, 1):
        # Flatten any stray subdirectory in the href; the corpus is one
        # directory per platform and a '/' in a name would escape it.
        name = e["file"].replace("/", "_")
        dest = outdir / name
        rec = dict(e, platform=platform,
                   path=str(dest.relative_to(root)))
        if dest.exists() and dest.stat().st_size > 0:
            rec["bytes"] = dest.stat().st_size
            records.append(rec)
            skipped += 1
            continue
        try:
            data = get(f"{BASE}/music/{platform}/{e['file']}")
        except Exception as exc:                       # noqa: BLE001
            print(f"    ! {name}: {exc}", flush=True)
            failed += 1
            continue
        if not data:
            failed += 1
            continue
        dest.write_bytes(data)
        rec["bytes"] = len(data)
        records.append(rec)
        got += 1
        if i % 100 == 0:
            print(f"    {i}/{len(entries)}  new={got} have={skipped} "
                  f"gone={failed}", flush=True)
        time.sleep(delay)
    print(f"  done: new={got} had={skipped} gone={failed}", flush=True)
    return records


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "data" / "midi" / "vgmusic")
    ap.add_argument("--platforms", nargs="*", default=DEFAULT_PLATFORMS,
                    help="VGMusic paths, e.g. console/nintendo/snes")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="seconds between file requests")
    ap.add_argument("--limit", type=int, default=0,
                    help="first N files per platform (for a smoke test)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    index_path = args.out / "index.json"

    # Merge with anything a previous run recorded, so adding a platform
    # later does not lose the ones already fetched.
    all_recs = {}
    if index_path.exists():
        for r in json.loads(index_path.read_text()):
            all_recs[r["path"]] = r

    for p in args.platforms:
        print(f"[{p}]", flush=True)
        try:
            for r in fetch_platform(p, args.out, args.delay, args.limit):
                all_recs[r["path"]] = r
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! platform failed: {exc}", file=sys.stderr)
        # Write after every platform: a long crawl should never be all
        # or nothing.
        index_path.write_text(json.dumps(sorted(all_recs.values(),
                                                key=lambda r: r["path"]),
                                         indent=1))
        print(f"  index -> {index_path} ({len(all_recs)} total)", flush=True)


if __name__ == "__main__":
    main()
