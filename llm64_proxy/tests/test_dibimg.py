#!/usr/bin/env python3
"""The Windows image path: PNG -> packed 8-bit DIB, and the style/format
choices that hang off the client profile. Run: python3 tests/test_dibimg.py

What matters here is the WIRE CONTRACT with a Win16 client that will
never be recompiled against this code: header layout, RGBQUAD byte
order, bottom-up rows, 4-byte stride. A wrong stride doesn't error -
it shears the picture.
"""

import asyncio
import io
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image

from src.imaging import convert_to_dib8
from src.images import ImageService, DEFAULT_STYLE_PREFIX
from src.profiles import (from_hello, CAP_DIB_IMAGES, CAP_RICH_TEXT,
                          VGA_STYLE, WIN16, C64)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


# --- the DIB itself ---------------------------------------------------

# A generator-shaped image: 1024x640, distinct corner colours so
# orientation is observable after the resample.
src = Image.new("RGB", (1024, 640), (40, 90, 160))
for x in range(120):
    for y in range(120):
        src.putpixel((x, y), (250, 30, 30))            # top-left: red
        src.putpixel((1023 - x, 639 - y), (30, 250, 30))  # bottom-right

dib, w, h = convert_to_dib8(src)
check("1024x640 scales to the exact era frame", (w, h), (640, 400))

hdr = struct.unpack("<IiiHHIIiiII", dib[:40])
check("biSize", hdr[0], 40)
check("biWidth", hdr[1], w)
check("biHeight positive = bottom-up", hdr[2], h)
check("biPlanes", hdr[3], 1)
check("biBitCount", hdr[4], 8)
check("biCompression BI_RGB", hdr[5], 0)
stride = (w + 3) & ~3
check("biSizeImage", hdr[6], stride * h)
check("biClrUsed always full", hdr[9], 256)
check("total length is header+table+rows",
      len(dib), 40 + 1024 + stride * h)

# Bottom-up: the SOURCE's top-left red block must live in the LAST rows
# of the pixel data. Look the stored index up in the colour table.
pix = dib[40 + 1024:]
idx = pix[(h - 1) * stride]              # last stored row = top image row
b, g, r = dib[40 + idx * 4:40 + idx * 4 + 3]
check("top-left pixel is red after the flip (RGBQUAD is BGR)",
      (r > 180, g < 110, b < 110), (True, True, True))
idx0 = pix[w - 1]                        # first stored row, right edge
b, g, r = dib[40 + idx0 * 4:40 + idx0 * 4 + 3]
check("bottom-right green lands in row 0", (g > 180, r < 110), (True, True))

# An odd width must pad its rows, and a small image must not be blown
# up. A gradient, because a flat fixture reads as all border to
# trim_border - which is trim doing its job, not a conversion bug.
small = Image.new("RGB", (37, 20))
for y in range(20):
    for x in range(37):
        small.putpixel((x, y), (x * 6, y * 12, 128))
d2, w2, h2 = convert_to_dib8(small)
check("no upscale", (w2, h2), (37, 20))
check("odd width pads to 4", len(d2), 40 + 1024 + 40 * 20)

# --- profile capability plumbing --------------------------------------


def hello(caps, name=b'win16'):
    return bytes([1, 80, 0, 8, caps & 0xFF, caps >> 8, len(name)]) + name


check("claiming the bit turns DIBs on",
      from_hello(hello(CAP_DIB_IMAGES))[0].dib_images, True)
check("...and rich text does not imply it",
      from_hello(hello(CAP_RICH_TEXT))[0].dib_images, False)
check("the win16 table row carries the VGA art style",
      from_hello(hello(CAP_DIB_IMAGES))[0].image_style, VGA_STYLE)
check("an unknown machine gets no style opinion",
      from_hello(hello(CAP_DIB_IMAGES, name=b'dos32'))[0].image_style, None)
check("the c64 keeps the images.py default", C64.image_style, None)
check("the table itself never grants the capability",
      WIN16.dib_images, False)

# --- style precedence and the stem round-trip -------------------------


class FakeBackend:
    """Records the prompt, returns a real (tiny) PNG."""
    name = 'fake'

    def __init__(self):
        self.prompts = []

    def available(self):
        return True

    def generate(self, prompt, purpose="adventure"):
        self.prompts.append(prompt)
        img = Image.new("RGB", (64, 40))
        for y in range(40):
            for x in range(64):
                img.putpixel((x, y), (x * 4, y * 6, 200))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()


with tempfile.TemporaryDirectory() as td:
    be = FakeBackend()
    svc = ImageService(Path(td), mode="ask", backend=be)
    blob, stem, bg = svc._generate_sync("a tower", "conv1", style=VGA_STYLE)
    check("a profile style replaces the default prefix",
          be.prompts[-1].startswith(VGA_STYLE), True)
    svc._generate_sync("a tower", "conv1")
    check("no style still means the C64 default",
          be.prompts[-1].startswith(DEFAULT_STYLE_PREFIX), True)

    cfg = ImageService(Path(td), mode="ask", backend=be,
                       style_prefix="oil painting of ")
    cfg._generate_sync("a tower", "conv1", style=VGA_STYLE)
    check("an operator's explicit style outranks every profile",
          be.prompts[-1], "oil painting of a tower")

    # The retained PNG serves the DIB on demand - one generation, two
    # machines - and losing it raises OSError for the blob fallback.
    d3, w3, h3 = asyncio.run(svc.dib_from_stem(stem))
    check("dib_from_stem keeps the original size", (w3, h3), (64, 40))
    check("...and is a well-formed DIB",
          struct.unpack("<I", d3[:4])[0], 40)
    try:
        asyncio.run(svc.dib_from_stem("conv1/never-existed"))
        failures.append("a missing PNG did not raise OSError")
    except OSError:
        pass


if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("-", f)
    sys.exit(1)
print("test_dibimg: all checks passed")
