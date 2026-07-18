#!/usr/bin/env python3
"""Stage 4 of the SID pipeline: assemble the runtime music library.

Takes a directory of sidreloc-relocated .sid files plus the mood tags from
sid_mood.py and emits moods.json — the proxy's music database. The PSID
header is parsed for the relocated init/play/load addresses; the C64 data
payload (what actually gets streamed to the client) stays in the .sid file
and is extracted at send time.
"""

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sid_reloc_batch import unique_name  # noqa: E402


def sid_header(path: Path) -> dict:
    data = path.read_bytes()
    version, data_offset, load, init, play, songs, start_song = \
        struct.unpack(">HHHHHHH", data[4:0x12])
    payload = data[data_offset:]
    if load == 0:
        load = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    return {"load": load, "init": init or load, "play": play,
            "songs": songs, "start_song": start_song, "size": len(payload)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("siddir", type=Path, help="directory of relocated .sid files")
    ap.add_argument("tags", type=Path, help="mood tags JSON from sid_mood.py")
    ap.add_argument("--loudness", type=Path,
                    help="loudness JSON from sid_loudness.py: adds a "
                         "per-tune $D418 override byte normalizing to the "
                         "corpus median RMS")
    ap.add_argument("--d418", type=Path,
                    help="d418 trace JSON (sid_loudness.py --d418-only): "
                         "tunes whose play writes a single constant $D418 "
                         "value are safe to override despite being 'live'")
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    loud = {}
    if args.loudness:
        loud = {r["file"]: r for r in json.loads(args.loudness.read_text())
                if "rms_db" in r}
        med = sorted(r["rms_db"] for r in loud.values())[len(loud) // 2]
    d418 = {}
    if args.d418:
        d418 = {r["file"]: r.get("d418_values")
                for r in json.loads(args.d418.read_text())
                if "d418_values" in r}

    # Keyed by path-derived unique filename: bare stems collide in HVSC
    tags = {unique_name(t["path"]): t
            for t in json.loads(args.tags.read_text()) if "error" not in t}

    tunes = []
    for sid in sorted(args.siddir.glob("*.sid")):
        t = tags.get(sid.name)
        if not t:
            print(f"warning: no tags for {sid.name}, skipping", file=sys.stderr)
            continue
        hdr = sid_header(sid)
        tunes.append({
            "id": sid.stem,
            "title": t["title"],
            "author": t["author"],
            "moods": t["moods"],
            "settings": t["settings"],
            "arcadey": t.get("arcadey"),
            "iconic": t.get("iconic"),
            "confidence": t.get("confidence"),
            # relative to the output db's directory (library is rsynced)
            "file": str(sid.resolve().relative_to(
                args.output.resolve().parent)),
            **hdr,
        })
        lr = loud.get(sid.name)
        if lr:
            # Near-silent output = broken under our player; drop it
            if lr["rms_db"] < 40:
                tunes.pop()
                continue
            # Attenuate louder-than-median tunes: volume nibble scaled by
            # the dB excess (SID volume is ~linear amplitude), filter bits
            # preserved. The client stores this byte to $D418 after each
            # play call. Overridable if the play routine never writes
            # $D418, or always writes one constant value (then keep its
            # filter bits); truly varying writers (tremolo) are left alone.
            vals = d418.get(sid.name)
            if not lr["d418_live"]:
                filt = lr["d418_init"] & 0xF0
            elif vals is not None and len(vals) == 1:
                filt = vals[0] & 0xF0
            else:
                filt = None
            if filt is not None:
                excess = max(0.0, lr["rms_db"] - med)
                vol = max(4, round(15 * 10 ** (-excess / 20)))
                tunes[-1]["vol_byte"] = filt | vol
            tunes[-1]["rms_db"] = lr["rms_db"]

    moods = sorted({m for t in tunes for m in t["moods"]})
    db = {"version": 1, "moods": moods, "tunes": tunes}
    args.output.write_text(json.dumps(db, indent=1))
    print(f"{len(tunes)} tunes, moods: {', '.join(moods)}")


if __name__ == "__main__":
    main()
