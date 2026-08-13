#!/usr/bin/env python3
"""Pixel-diff our hand-drawn 3.1 chrome against a real 3.11 capture.

    tools/pixdiff.py <capture.png> [reference.png]

Every metric in spike/caption.c was found with this, and every one of them
was wrong *confidently* by eye first. If you change the chrome, run it.

The default reference is screenshots/win311_client.png: a 1024x768 guest
capture with 3 columns trimmed off the RIGHT (proved - its sysmenu box is
a full 18 px starting at x=0). So reference x maps 1:1 onto true screen x
in every region, and the only offset anywhere is y: the reference's
caption starts at row 2, ours at row 0.

That capture is MAXIMISED, which is why it can verify the caption and the
menu bar but says nothing at all about a sizing border - a maximised 3.1
window has none.

Prints an ASCII color map of both sides per region, so the structure can
be read rather than guessed at, then the numeric diff.
"""

import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
REF = REPO / "screenshots" / "win311_client.png"

REF_Y, SPK_Y, BAND = 2, 0, 38       # caption + rule + menu bar + rule

# name, x0, width, draw the map?
REGIONS = [
    ("left",    0,   26,  True),    # sysmenu box
    ("right",   983, 38,  True),    # the two arrow buttons
    ("title",   478, 50,  False),   # centered caption text
    ("menubar", 0,   340, False),   # File Link Settings Window Help
]

SYM = {
    (192, 192, 192): ".",   # C0C0C0 face
    (255, 255, 255): "H",   # highlight / white
    (128, 128, 128): "s",   # 808080 shadow
    (0, 0, 0):       "K",   # black
    (0, 0, 128):     "N",   # navy caption
}


def grab(img, y0, x0, w):
    px = img.convert("RGB").load()
    return [[px[x0 + x, y0 + y] for x in range(w)] for y in range(BAND)]


def show(rows, x0, label):
    print(f"  {label}")
    print("        " + "".join(str((x0 + x) % 10) for x in range(len(rows[0]))))
    for y, row in enumerate(rows):
        print(f"    y{y:<2} " + "".join(SYM.get(c, "?") for c in row))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    spk = Image.open(argv[1])
    ref = Image.open(argv[2] if len(argv) > 2 else REF)
    total = bad = 0

    for name, x0, w, draw in REGIONS:
        a = grab(ref, REF_Y, x0, w)
        b = grab(spk, SPK_Y, x0, w)
        miss = [(x, y) for y in range(BAND) for x in range(w)
                if a[y][x] != b[y][x]]
        total += BAND * w
        bad += len(miss)
        print(f"\n=== {name}  x={x0}..{x0 + w - 1}: {len(miss)}/{BAND * w} "
              f"differ ({100.0 * len(miss) / (BAND * w):.1f}%) ===")
        if not miss:
            print("  IDENTICAL")
            continue
        if draw:
            show(a, x0, "REFERENCE")
            show(b, x0, "OURS")
        # Structural rows vs glyph ink: the distinction that matters. A
        # difference in the bar's fill or in a rule is ours to fix; one
        # inside a letterform is the host's System font, and only shipping
        # 3.1's own vgasys.fon closes that.
        ink = [p for p in miss if 4 <= p[1] <= 13 or 22 <= p[1] <= 33]
        print(f"  inside glyph ink: {len(ink)}   structural: "
              f"{len(miss) - len(ink)}")

    print(f"\nTOTAL: {bad}/{total} differ ({100.0 * bad / total:.3f}%)  -> "
          f"{'PIXEL PERFECT' if not bad else 'work to do'}")
    return 1 if bad else 0


sys.exit(main(sys.argv))
