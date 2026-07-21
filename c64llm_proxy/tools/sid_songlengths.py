#!/usr/bin/env python3
"""Stage 3b of the SID pipeline: HVSC song durations.

Parses DOCUMENTS/Songlengths.md5 into {unique_name: [seconds per subtune]}
so sid_makedb.py can stamp a duration on each tune - the sound-window
module needs it to draw a progress bar, and there is no way to derive it
from the SID file itself.

Keyed off the path comment rather than the MD5. The hash in that file is
of the ORIGINAL HVSC .sid, which our relocated copies no longer match,
and the exact hashing rule has changed across HVSC releases; the path is
stable and maps onto the rest of the pipeline through unique_name().

  ; /MUSICIANS/Z/Zabutom/One_Little_Wish_tune_2.sid
  c7c299ce06ec5ccffb2261fb11b42a73=4:33.108 1:02

Emits JSON on stdout or to --output.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sid_reloc_batch import unique_name  # noqa: E402

# "4:33", "4:33.108", "0:56" - fractional part is milliseconds
DURATION_RE = re.compile(r"^(\d+):(\d{2})(?:\.(\d+))?$")


def parse_duration(tok: str):
    m = DURATION_RE.match(tok)
    if not m:
        return None
    mins, secs, frac = m.groups()
    total = int(mins) * 60 + int(secs)
    if frac:
        total += int(frac.ljust(3, "0")[:3]) / 1000.0
    return round(total, 3)


def parse(path: Path) -> dict:
    """{unique_name: [seconds, ...]} - one entry per subtune, in order."""
    out = {}
    pending = None
    for line in path.read_text(encoding="latin-1").splitlines():
        line = line.strip()
        if line.startswith(";"):
            pending = line[1:].strip()
            continue
        if "=" not in line or pending is None:
            continue
        # Durations follow '='; ignore any trailing annotation tokens
        secs = [d for d in (parse_duration(t)
                            for t in line.split("=", 1)[1].split())
                if d is not None]
        if secs:
            out[unique_name(pending)] = secs
        pending = None
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("songlengths", type=Path,
                    help="HVSC DOCUMENTS/Songlengths.md5")
    ap.add_argument("-o", "--output", type=Path)
    args = ap.parse_args()

    table = parse(args.songlengths)
    if not table:
        sys.exit("no entries parsed - wrong file?")
    total = sum(len(v) for v in table.values())
    print(f"{len(table)} tunes, {total} subtune durations", file=sys.stderr)

    text = json.dumps(table)
    if args.output:
        args.output.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
