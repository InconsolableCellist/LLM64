#!/usr/bin/env python3
"""Stage 4 of the MIDI pipeline: assemble the runtime music library.

Joins midi_scan.py's facts with midi_mood.py's tags into midi.json, the
MIDI twin of moods.json. Same shape wherever the shape can be the same,
because MidiLibrary and MusicLibrary answer the same questions.

One field is new, and it exists to fill a real hole. The SID library
weights selection by `rank`, a percentile built by sid_rank.py from the
C64 scene's own published opinion - compo placings, CSDb ratings,
DeepSID's composer register. There is no DeepSID for MIDI. Nobody
publishes a ranking of game-music sequences.

So `quality` is computed from the file itself, on the theory that the
difference between a sequence somebody cared about and a mechanical dump
is visible in the data:

  velocity spread   a human performs dynamics; a converter emits 100,
                    100, 100. This is the strongest single signal.
  parts             a 6-part arrangement took longer than a 2-part one.
  length            a 12-second fragment is not a score.
  drum balance      some percussion is arrangement; 80% is a drum loop.

It is a weaker signal than "an audience voted for this at a demoparty",
and it is honest about being a proxy. But it is not nothing, and it is
computed rather than crawled.
"""

import argparse
import json
import statistics
from pathlib import Path


def raw_quality(t: dict) -> float:
    """Unnormalized 'somebody sequenced this carefully' score."""
    # Velocity spread. ~0 for a mechanical dump; a well-sequenced piece
    # sits around 12-20. Saturate at 18 so an erratic file cannot buy
    # its way to the top on noise alone.
    dynamics = min(t.get("vel_sd", 0.0) / 18.0, 1.0)

    # Parts actually playing, saturating at 8 - beyond that it is an
    # orchestration choice, not additional evidence of effort.
    parts = min(t.get("channels", 1) / 8.0, 1.0)

    # Length: a background tune wants to be a minute or more. Full marks
    # from 90 s up, scaled below that.
    length = min(t.get("seconds", 0) / 90.0, 1.0)

    # Percussion is fine; a drum loop with a melody bolted on is not.
    drums = t.get("drum_fraction", 0.0)
    balance = 1.0 - max(0.0, (drums - 0.45) / 0.55)

    return (0.45 * dynamics + 0.25 * parts + 0.20 * length
            + 0.10 * balance)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scan", type=Path, help="scan.json from midi_scan.py")
    ap.add_argument("tags", type=Path, help="tags JSON from midi_mood.py")
    ap.add_argument("--corpus", type=Path, default=None,
                    help="corpus root (default: <scan dir>/vgmusic)")
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    corpus = args.corpus or (args.scan.parent / "vgmusic")
    scan = {t["path"]: t for t in json.loads(args.scan.read_text())}
    tags = [t for t in json.loads(args.tags.read_text()) if "error" not in t]

    # Percentile, exactly like sid_ranking's: "better than this fraction
    # of your own library". Absolute scores are meaningless across
    # corpora; a percentile is not.
    raws = sorted(raw_quality(t) for t in scan.values())

    def percentile(x: float) -> float:
        lo, hi = 0, len(raws)
        while lo < hi:
            mid = (lo + hi) // 2
            if raws[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        return round(lo / max(len(raws) - 1, 1), 3)

    tunes, skipped = [], 0
    for t in tags:
        s = scan.get(t["path"])
        if not s or not t.get("moods"):
            skipped += 1
            continue
        f = corpus / t["path"]
        tunes.append({
            "id": t["path"].replace("/", "__").rsplit(".", 1)[0],
            "title": t["title"],
            "game": t.get("game", ""),
            "author": t.get("author", ""),
            "moods": t["moods"],
            "settings": t.get("settings", {}),
            "arcadey": t.get("arcadey"),
            "iconic": t.get("iconic"),
            "confidence": t.get("confidence"),
            "file": str(f.resolve().relative_to(
                args.output.resolve().parent)),
            "secs": s["seconds"],
            "bytes": s.get("bytes", 0),
            "quality": percentile(raw_quality(s)),
            "gm_reset": s.get("gm_reset", False),
            "platform": s.get("platform", ""),
        })

    db = {"tunes": tunes,
          "moods": sorted({m for t in tunes for m in t["moods"]})}
    args.output.write_text(json.dumps(db, indent=1))

    print(f"{len(tunes)} tunes -> {args.output}  ({skipped} skipped)")
    per_mood = {}
    for t in tunes:
        for m in t["moods"]:
            per_mood[m] = per_mood.get(m, 0) + 1
    print("\nmood coverage:")
    for m, n in sorted(per_mood.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<14} {n:5d}")
    thin = [m for m, n in per_mood.items() if n < 5]
    if thin:
        print(f"\n  THIN (<5 tunes): {', '.join(sorted(thin))}")
    if tunes:
        print(f"\nmedian length {statistics.median(t['secs'] for t in tunes) / 60:.1f} min, "
              f"{sum(t['bytes'] for t in tunes) / 1e6:.1f} MB total")


if __name__ == "__main__":
    main()
