#!/usr/bin/env python3
"""Scan an HVSC tree for SIDs that can run inside the c64llm client.

Stage 1 of the SID pipeline: parse PSID headers and keep tunes that could
fit the client's reserved window (4 KB at $A800) before spending CPU on
sidreloc verification. Emits a JSON candidate list on stdout or to a file.

Filter criteria (each recorded, so yields per-rule can be inspected):
  - PSID magic (RSIDs need a true C64 environment; the client can't host one)
  - all subtunes VBI-driven (speed bitfield == 0): the client calls play
    from its 60 Hz tick, so CIA-timer multispeed tunes would play wrong
  - data size <= 4 KB (the reserved window is $A800-$B7FF)
  - load address sane (not zero page, not under I/O)
"""

import argparse
import json
import struct
import sys
from pathlib import Path

WINDOW_SIZE = 0x1000  # 4 KB at $A800


def parse_sid(path: Path):
    """Return (record, reject_reason). record is None when rejected."""
    data = path.read_bytes()
    if len(data) < 0x76:
        return None, "truncated"
    magic = data[0:4]
    if magic == b"RSID":
        return None, "rsid"
    if magic != b"PSID":
        return None, "bad-magic"

    version, data_offset, load, init, play, songs, start_song, speed = \
        struct.unpack(">HHHHHHHI", data[4:0x16])
    name = data[0x16:0x36].split(b"\0")[0].decode("latin-1")
    author = data[0x36:0x56].split(b"\0")[0].decode("latin-1")
    released = data[0x56:0x76].split(b"\0")[0].decode("latin-1")

    payload = data[data_offset:]
    if load == 0:
        if len(payload) < 2:
            return None, "truncated"
        load = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    size = len(payload)

    rec = {
        "path": str(path),
        "version": version,
        "load": load,
        "init": init or load,
        "play": play,
        "songs": songs,
        "start_song": start_song,
        "size": size,
        "name": name,
        "author": author,
        "released": released,
    }

    # Multispeed check: PSID speed bit set => CIA-driven subtune
    if speed != 0:
        return rec, "multispeed"
    # play == 0 means the init routine installs its own interrupt: the
    # client must own the tick, so these are unusable as-is
    if play == 0:
        return rec, "self-installing-irq"
    if size > WINDOW_SIZE:
        return rec, "too-big"
    end = load + size
    if load < 0x0200 or end > 0x10000:
        return rec, "bad-load-range"
    # Anything overlapping I/O or KERNAL can't be a clean reloc source
    if load < 0x0400:
        return rec, "low-load"
    return rec, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path, help="HVSC C64Music root")
    ap.add_argument("-o", "--output", type=Path, help="write candidates JSON here")
    args = ap.parse_args()

    counts = {}
    candidates = []
    total = 0
    for path in sorted(args.root.rglob("*.sid")):
        total += 1
        try:
            rec, reason = parse_sid(path)
        except Exception as e:  # unreadable/corrupt file: count and move on
            counts[f"error:{type(e).__name__}"] = counts.get(f"error:{type(e).__name__}", 0) + 1
            continue
        key = reason or "candidate"
        counts[key] = counts.get(key, 0) + 1
        if reason is None:
            candidates.append(rec)

    summary = {"total": total, "counts": counts, "candidates": len(candidates)}
    print(json.dumps(summary, indent=2), file=sys.stderr)
    out = json.dumps(candidates, indent=1)
    if args.output:
        args.output.write_text(out)
    else:
        print(out)


if __name__ == "__main__":
    main()
