#!/usr/bin/env python3
"""Pixel-diff the spike's caption against the 3.11 reference.

The reference is a 1024x768 guest capture with 3 columns trimmed off the
RIGHT (proved: its sysmenu button is a full 18 px starting at x=0). So
reference x maps 1:1 onto true screen x for every region, and the only
offset anywhere is the caption's y: the reference band starts at y=2, the
spike's at y=0.

Prints, per region, an ASCII colour map of both so the structure can be
read directly, then the numeric diff.
"""

import sys
from PIL import Image

REF = "/home/offipso/storage/git/c64_llm/screenshots/win311_client.png"
SPK = ("/tmp/claude-1000/-home-offipso-storage-git-c64-llm/"
       "7445cc61-c39a-4c66-925d-624fd68250e3/scratchpad/shots/spike-max.png")

REF_Y, SPK_Y, BAND = 2, 0, 19       # 18 caption rows + the black rule

# name, x0, width, draw_map
REGIONS = [
    ("left",  0,   26, True),
    ("right", 983, 38, True),
    ("title", 478, 50, True),
]

SYM = {
    (192, 192, 192): ".",   # C0C0C0 face
    (255, 255, 255): "H",   # highlight / white
    (128, 128, 128): "s",   # shadow
    (0, 0, 0):       "K",   # black
    (0, 0, 128):     "N",   # navy
}


def grab(img, y0, x0, w):
    px = img.convert("RGB").load()
    return [[px[x0 + x, y0 + y] for x in range(w)] for y in range(BAND)]


def show(rows, x0, label):
    print(f"  {label}")
    print("        " + "".join(str((x0 + x) % 10) for x in range(len(rows[0]))))
    for y, row in enumerate(rows):
        print(f"    y{y:<2} " + "".join(SYM.get(c, "?") for c in row))


def extent(rows, colour):
    xs = [x for row in rows for x, c in enumerate(row) if c == colour]
    ys = [y for y, row in enumerate(rows) if colour in row]
    if not xs:
        return None
    return (min(xs), max(xs), min(ys), max(ys))


def main():
    ref = Image.open(REF)
    spk = Image.open(sys.argv[1] if len(sys.argv) > 1 else SPK)
    total = bad = 0

    for name, x0, w, draw in REGIONS:
        a = grab(ref, REF_Y, x0, w)
        b = grab(spk, SPK_Y, x0, w)
        miss = [(x, y) for y in range(BAND) for x in range(w)
                if a[y][x] != b[y][x]]
        total += BAND * w
        bad += len(miss)
        print(f"\n=== {name}  x={x0}..{x0+w-1}: "
              f"{len(miss)}/{BAND*w} differ "
              f"({100.0*len(miss)/(BAND*w):.1f}%) ===")
        if draw and miss:
            show(a, x0, "REFERENCE")
            show(b, x0, "SPIKE")
        if name == "title":
            ea, eb = extent(a, (255, 255, 255)), extent(b, (255, 255, 255))
            print(f"  ref   text bbox x={ea[0]+x0}..{ea[1]+x0} y={ea[2]}..{ea[3]}")
            print(f"  spike text bbox x={eb[0]+x0}..{eb[1]+x0} y={eb[2]}..{eb[3]}")
            print(f"  -> dx={eb[0]-ea[0]}  dy={eb[2]-ea[2]}  "
                  f"ref w={ea[1]-ea[0]+1} spike w={eb[1]-eb[0]+1}")
        if not miss:
            print("  IDENTICAL")

    print(f"\nTOTAL: {bad}/{total} differ ({100.0*bad/total:.2f}%)  -> "
          f"{'PIXEL PERFECT' if not bad else 'work to do'}")
    return 1 if bad else 0


sys.exit(main())
