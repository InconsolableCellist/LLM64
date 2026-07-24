#!/usr/bin/env python3
"""Convert an image to a C64 hires bitmap blob.

Runs src/imaging.py on one file and writes bitmap (8000 bytes) followed by
the color matrix (1000 bytes) as a single 9000-byte blob. Optionally emits
a PNG preview rendered back from the blob, plus per-cell color stats so
degenerate conversions (everything one color pair) are easy to spot.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from src.imaging import convert_to_c64, render_preview


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="source image (any PIL format)")
    ap.add_argument("output", type=Path, help="output blob (bitmap + matrix, 9000 bytes)")
    ap.add_argument("--preview", type=Path, help="also write a PNG preview here")
    args = ap.parse_args()

    img = Image.open(args.input)
    bitmap, matrix = convert_to_c64(img)
    blob = bitmap + matrix
    args.output.write_bytes(blob)
    print(f"{args.output}: {len(blob)} bytes ({len(bitmap)} bitmap + {len(matrix)} matrix)")

    pair_counts = Counter(matrix)
    colors_used = sorted({b >> 4 for b in matrix} | {b & 0x0F for b in matrix})
    print(f"distinct fg/bg pairs: {len(pair_counts)}, palette colors used: {colors_used}")
    print("top pairs (fg,bg,cells):",
          ", ".join(f"({v >> 4},{v & 0x0F})x{n}" for v, n in pair_counts.most_common(5)))

    if args.preview:
        render_preview(bitmap, matrix).save(args.preview)
        print(f"preview: {args.preview}")


if __name__ == "__main__":
    main()
