#!/usr/bin/env python3
"""Render a C64 picture for a paper printer (docs/14 13.11).

The third thing /print can put on paper. printdoc.py composes text and
printcups.py delivers; this turns the C64's own picture into something a
1-bit printer can lay down.

What gets printed is the BLOB, not the source image: 8000 bytes of
multicolor bitmap plus screen, colour RAM and background - exactly the
bytes the C64 displays, caption band and all - decoded back through
imaging.render_preview_mc. So the page carries the machine's 16-colour,
160x200 rendering, not the 1024x1024 painting the model produced. That
is the whole point of printing it.

Colour has to become dots, and HOW decides whether it still looks like a
C64 picture. Three orders of operation were printed and compared on a
203 dpi thermal (docs/14 13.11):

  dither at 160x200, then enlarge   - error diffusion at C64 resolution
                                      turns every flat colour area into
                                      noise; the scene stops reading
  enlarge, then Floyd-Steinberg     - good tone, but still scatters
                                      error inside areas the C64 drew
                                      as one flat colour
  enlarge, then ORDERED halftone    - what this module does

An ordered matrix gives every pixel of the same colour the identical dot
pattern, so flat stays flat and edges stay hard, and at scale 4 the 4x4
matrix is exactly one halftone cell per C64 pixel - the pixel grid and
the dot grid line up instead of beating against each other. That is why
the scale is an integer and why 4 is the default.

Pure functions over bytes, no event loop and no printer - see
tests/test_printpic.py.
"""

import io

from PIL import Image, ImageChops

from . import imaging

# Printer dot pitch the page is built for. The PNG carries it as its DPI
# and lp is passed a matching `ppi`, so CUPS places the image 1:1 rather
# than fitting it to the page - a rescale would resample the halftone
# and moire it against the printer's own dot grid.
DPI = 203

# One C64 pixel becomes SCALE x SCALE printer dots. At 203 dpi, 4 puts a
# 320-wide picture at 6.3in - most of an A4's printable width - and
# keeps the halftone cell aligned to the pixel grid.
SCALE = 4

# Bayer 4x4. Ordered rather than error-diffusing on purpose (see above).
BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))

# A multicolor blob is bitmap + screen + colram + the background byte;
# the older hires format is bitmap + matrix with no background.
MC_LEN = 10001
HIRES_LEN = 9000


def decode(blob: bytes) -> Image.Image:
    """Blob bytes -> the RGB image the C64 shows. Accepts both formats
    for the same reason /pic <n> does: conversations still hold blobs
    from the hires era."""
    n = len(blob)
    if n == MC_LEN:
        return imaging.render_preview_mc(blob[:8000], blob[8000:9000],
                                         blob[9000:10000], blob[10000])
    if n == HIRES_LEN:
        return imaging.render_preview(blob[:8000], blob[8000:9000])
    raise ValueError(f"not a picture blob: {n} bytes")


def _threshold(size, scale: int) -> Image.Image:
    """A tiled Bayer field the size of the enlarged picture. Built by
    pasting rather than computed per pixel - a page is over a million
    dots and this runs on the proxy, not a workstation."""
    w, h = size
    tile = Image.new('L', (4, 4))
    tile.putdata([v * 16 + 8 for row in BAYER for v in row])
    row = Image.new('L', (w, 4))
    for x in range(0, w, 4):
        row.paste(tile, (x, 0))
    field = Image.new('L', (w, h))
    for y in range(0, h, 4):
        field.paste(row, (0, y))
    return field


def halftone(img: Image.Image, scale: int = SCALE) -> Image.Image:
    """The C64 picture as 1-bit dots: enlarge with NEAREST so each pixel
    stays a hard square, then threshold against the ordered field."""
    scale = max(1, int(scale))
    big = img.convert('L').resize(
        (img.width * scale, img.height * scale), Image.NEAREST)
    lit = ImageChops.subtract(big, _threshold(big.size, scale))
    return lit.point(lambda v: 255 if v > 0 else 0).convert('1')


def render(blob: bytes, scale: int = SCALE, dpi: int = DPI) -> bytes:
    """Blob -> PNG bytes ready for lp. 1-bit, DPI-stamped, no scaling
    left for CUPS to do."""
    page = halftone(decode(blob), scale)
    buf = io.BytesIO()
    page.save(buf, format='PNG', dpi=(dpi, dpi))
    return buf.getvalue()
