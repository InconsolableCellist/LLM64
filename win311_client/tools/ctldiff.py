#!/usr/bin/env python3
"""Diff our 3.1 controls against the real 3.11 capture.

    make ctlcheck                       # builds, renders and runs this
    tools/ctldiff.py build/ctl.bmp

The same method as tools/pixdiff.py and the same reference picture. That
capture has a real 3.11 machine's push buttons in it (the Music window's
Play/Pause/Stop/Next) and a real checkbox (under the Picture browser), so
there is no need to guess at what a 3.1 button looks like - and every
time this project guessed, it was wrong.

spike/ctl.c renders the shapes into a BMP with no window involved, which
takes the window manager, the screen grabber and a frame's worth of
offset arithmetic out of the comparison. Only the geometry is left.

The button's INTERIOR is not compared: the reference has the word
"Pause" in it and the spike draws no text. The border ring is the whole
of the drawing anyway - everything inside it is flat face grey.
"""

import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
REF = REPO / "screenshots" / "win311_client.png"

# Reference: the Pause button, measured off the capture.
REF_BTN = (643, 716, 121, 25)
# Spike: BTN_X, BTN_Y and the raised button is the first of three.
SPK_BTN = (10, 10)
# Reference: the "Illustrate every room" box, and the spike's checked one.
REF_CHK = (643, 572, 13, 13)
SPK_CHK = (10, 115)

RING = 4        # how far in the button's drawing goes on each side

SYM = {
    (192, 192, 192): ".",
    (255, 255, 255): "H",
    (128, 128, 128): "s",
    (0, 0, 0): "K",
}


def crop(img, x0, y0, w, h):
    px = img.convert("RGB").load()
    return [[px[x0 + x, y0 + y] for x in range(w)] for y in range(h)]


def show(rows, label):
    print(f"  {label}")
    for row in rows:
        print("    " + "".join(SYM.get(c, "?") for c in row))


def compare(name, a, b, ring):
    w, h = len(a[0]), len(a)
    miss = []
    for y in range(h):
        for x in range(w):
            if ring and not (x < ring or x >= w - ring
                             or y < ring or y >= h - ring):
                continue        # interior: the reference has text there
            if a[y][x] != b[y][x]:
                miss.append((x, y))
    n = w * h if not ring else w * h - max(0, w - 2 * ring) * max(0, h - 2 * ring)
    print(f"\n=== {name}: {len(miss)}/{n} differ ===")
    if miss:
        show(a, "REFERENCE")
        show(b, "OURS")
        print("  first ten:", miss[:10])
    else:
        print("  IDENTICAL")
    return len(miss), n


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    spk = Image.open(argv[1])
    ref = Image.open(REF)
    bad = total = 0

    x, y, w, h = REF_BTN
    d, n = compare("push button (border ring)",
                   crop(ref, x, y, w, h),
                   crop(spk, SPK_BTN[0], SPK_BTN[1], w, h), RING)
    bad += d
    total += n

    x, y, w, h = REF_CHK
    d, n = compare("checkbox, checked",
                   crop(ref, x, y, w, h),
                   crop(spk, SPK_CHK[0], SPK_CHK[1], w, h), 0)
    bad += d
    total += n

    print(f"\nTOTAL: {bad}/{total} differ -> "
          f"{'PIXEL PERFECT' if not bad else 'work to do'}")
    return 1 if bad else 0


sys.exit(main(sys.argv))
