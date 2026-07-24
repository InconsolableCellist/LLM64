#!/usr/bin/env python3
"""Stage 2 of the SID pipeline: relocate + verify candidates with sidreloc.

Takes the candidate list from sid_scan.py, runs each file through sidreloc
targeting the client's window ($A800, ZP $FB-$FE), and records the outcome.
The reloc range is pinned to the tune's actual load pages: sidreloc's
default pads up to 64 pages of scratch RAM, which the client cannot offer —
a tune that writes outside its own image would corrupt the program. Exit
code 0 alone counts as success (verification clean AND no OOB writes).
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WINDOW_PAGES = 16  # 4 KB window
ZP_RANGE = "fb-fe"


def unique_name(path: str) -> str:
    """HVSC-path-derived output filename: bare stems collide (391 dupes
    across the tree), so keep the directory path in the name."""
    p = str(path)
    for marker in ("C64Music/", "C64Music\\"):
        if marker in p:
            p = p.split(marker, 1)[1]
            break
    return p.lstrip("/").replace("/", "__")

# sidreloc exit bits (from sidreloc.c)
EXIT_BITS = {
    0x20: "oob-write",
    0x40: "tolerance",
}


def classify(code: int, stderr: str) -> str:
    if code == 0:
        return "ok"
    reasons = [name for bit, name in EXIT_BITS.items() if code & bit]
    if reasons:
        return "+".join(reasons)
    if "zero-page" in stderr or "zp" in stderr.lower():
        return "zp-unsolvable"
    return f"verify-fail({code})"


def run_one(sidreloc: str, rec: dict, root: Path, outdir: Path | None,
            page: str, play_cycles: int | None):
    first_page = rec["load"] >> 8
    last_page = (rec["load"] + rec["size"] - 1) >> 8
    span = last_page - first_page + 1
    if span > WINDOW_PAGES:
        return rec, "page-span-too-big", None

    src = root / rec["path"] if not Path(rec["path"]).is_absolute() else Path(rec["path"])
    if outdir:
        out = outdir / unique_name(rec["path"])
    else:
        out = Path("/dev/null")
    cmd = [
        sidreloc, "-p", page, "-z", ZP_RANGE,
        "-r", f"{first_page:02x}-{last_page:02x}",
        str(src), str(out),
    ]
    if play_cycles:
        cmd[1:1] = ["--play-cycles", str(play_cycles)]
    try:
        # sidreloc echoes tune names in latin-1; don't assume utf-8
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return rec, "timeout", None
    return rec, classify(proc.returncode, proc.stderr), proc.stderr.strip() or None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("candidates", type=Path, help="JSON list from sid_scan.py")
    ap.add_argument("--sidreloc", default="sidreloc", help="path to sidreloc binary")
    ap.add_argument("--root", type=Path, default=Path("."),
                    help="base dir for relative SID paths")
    ap.add_argument("--outdir", type=Path,
                    help="write relocated .sid files here (default: discard)")
    ap.add_argument("--page", default="a8",
                    help="destination page in hex (client window: b0)")
    ap.add_argument("--play-cycles", type=int,
                    help="reject tunes whose play routine exceeds this "
                         "(client IRQ budget: 1600)")
    # Modest default: a full-core sweep trips the UPS power budget here
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("-o", "--output", type=Path, help="write per-file results JSON")
    args = ap.parse_args()

    records = json.loads(args.candidates.read_text())
    if args.outdir:
        args.outdir.mkdir(parents=True, exist_ok=True)

    results = []
    counts = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = [pool.submit(run_one, args.sidreloc, r, args.root,
                            args.outdir, args.page, args.play_cycles)
                for r in records]
        for i, fut in enumerate(futs):
            rec, outcome, stderr = fut.result()
            counts[outcome] = counts.get(outcome, 0) + 1
            results.append({"path": rec["path"], "outcome": outcome, "stderr": stderr})
            if (i + 1) % 100 == 0:
                print(f"{i + 1}/{len(records)}...", file=sys.stderr)

    summary = {"total": len(records), "counts": counts,
               "yield_pct": round(100 * counts.get("ok", 0) / max(1, len(records)), 1)}
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
