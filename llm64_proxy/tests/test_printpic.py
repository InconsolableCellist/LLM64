#!/usr/bin/env python3
"""printpic: a C64 picture on a 1-bit printer (docs/14 13.11).

The picture that reaches paper has to be the C64's own - decoded from
the blob, not the source painting - and the dots have to be ORDERED, so
that an area the C64 drew in one flat color prints as one flat texture
instead of error-diffusion noise. Both are asserted here against
synthetic blobs; what a page looks like in the hand is a hardware
question (13.11 records that comparison).

Run: .venv/bin/python tests/test_printpic.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import printpic

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def blob_mc(bitmap_byte=0x00, screen=0x00, colram=0x00, bg=0x00):
    """A multicolor blob of 1000 identical cells. bitmap 0x00 = every
    pixel is code 00 = the background color, so the whole picture is
    one flat color - which is the case ordered dithering has to render
    as one repeating pattern."""
    return (bytes([bitmap_byte]) * 8000 + bytes([screen]) * 1000
            + bytes([colram]) * 1000 + bytes([bg]))


# --- decode: the C64's picture, both blob eras ------------------------

img = printpic.decode(blob_mc())
check('multicolor decodes to the C64 frame', img.size, (320, 200))
# /pic <n> still re-streams hires-era blobs, so printing must take them
hires = printpic.decode(bytes(8000) + bytes(1000))
check('hires blob still decodes', hires.size, (320, 200))

for bad in (b'', bytes(10), bytes(10000), bytes(10002)):
    try:
        printpic.decode(bad)
        failures.append(f'decode({len(bad)} bytes) should have raised')
    except ValueError:
        pass

# --- the halftone -----------------------------------------------------

# White background (color 1) at scale 4: one C64 pixel = one 4x4 cell.
white = printpic.halftone(printpic.decode(blob_mc(bg=0x01)), scale=4)
check('halftone size is scale x the frame', white.size, (1280, 800))
check('1-bit, which is all the printer has', white.mode, '1')

# The point of ORDERED: a flat area must come out as ONE repeating tile,
# identical everywhere. Floyd-Steinberg would vary it cell by cell.
px = white.load()
tile = [[px[x, y] for x in range(4)] for y in range(4)]
same = all(
    [px[x0 + x, y0 + y] for x in range(4) for y in range(4)]
    == [tile[y][x] for y in range(4) for x in range(4)]
    for y0 in range(0, 800, 4) for x0 in range(0, 1280, 4))
if not same:
    failures.append('a flat C64 color must halftone to one repeating '
                    'tile - that is what ordered dithering buys')

# Black stays fully black and white fully white: the ends of the range
# must not dither, or every picture prints through a gray veil.
black = printpic.halftone(printpic.decode(blob_mc(bg=0x00)), scale=2)
check('black is solid', black.convert('L').getextrema(), (0, 0))
check('white is solid', white.convert('L').getextrema(), (255, 255))

# Scale is printer dots per C64 pixel, and it must stay integer or the
# pixel grid beats against the dot grid.
check('scale 1', printpic.halftone(printpic.decode(blob_mc()), 1).size,
      (320, 200))
check('scale 8', printpic.halftone(printpic.decode(blob_mc()), 8).size,
      (2560, 1600))
check('a nonsense scale still renders',
      printpic.halftone(printpic.decode(blob_mc()), 0).size, (320, 200))

# --- render(): what actually goes to lp -------------------------------

png = printpic.render(blob_mc(bg=0x06), scale=4, dpi=203)
check('render returns a PNG', png[:8], b'\x89PNG\r\n\x1a\n')

try:
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    check('rendered page geometry', im.size, (1280, 800))
    check('rendered page is 1-bit', im.mode, '1')
    # The DPI is not decoration: lp is passed a matching ppi so CUPS
    # places the image 1:1 instead of resampling the halftone. PNG
    # stores it as pixels per METRE, so 203 comes back as 202.9968 -
    # round-tripping to the same integer is the assertion.
    check('DPI is stamped for lp',
          tuple(round(v) for v in im.info.get('dpi', (0, 0))), (203, 203))
except ImportError:                                      # pragma: no cover
    print('printpic: PNG re-read skipped - no PIL')

# --- report ------------------------------------------------------------

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print('  ' + f)
    sys.exit(1)
print("printpic: all checks passed")
