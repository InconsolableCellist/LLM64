#!/usr/bin/env python3
"""Build the shareware intro's disk assets: a drawn logo and a SID tune.

    tools/make_intro_assets.py <tune.sid> -o c64_client/intro/assets/

Writes three files into the output directory:

  intro_data.bin    the complete "c64 llm.d" disk file: a $4000 load address
                    followed by bitmap 8000 + screen 1000 + the tune's
                    memory image. Those offsets are a contract with intro.s.
  intro_gen.inc     ca65 include with the constants intro.s needs, including
                    the bar palette, so the drawn colors and the animated
                    ones can never drift apart.
  intro_preview.png what the C64 will show, at 2x, with the bottom five rows
                    dimmed - those are the text panel and are drawn by the
                    intro itself, not by this script.

The logo is DRAWN, not converted from a picture: chunky block letters
reading LLM64 over the Commodore rainbow. That buys exact control of the
color layout, which is what makes the bars animatable - see "Color model".

Color model (multicolor bitmap, and the whole reason this is drawn):
every lit pixel is %01, whose color comes from the upper nibble of that
cell's screen RAM byte. %00 is the global background ($D021, black), and
%10/%11 are never used. So a cell's color is one byte in screen RAM, one
row of bars is 40 identical bytes, and cycling the rainbow at run time is
six 40-byte fills - no bitmap rewriting, no color RAM at all. Color RAM is
therefore absent from the blob: nothing reads it above the text panel.
"""

import argparse
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "c64llm_proxy"))

# Blob layout. Load address first, then the parts back to back.
BLOB_LOAD = 0x4000
BITMAP_LEN, SCREEN_LEN = 8000, 1000
ADDR_BITMAP = BLOB_LOAD
ADDR_SCREEN = ADDR_BITMAP + BITMAP_LEN          # $5F40
ADDR_TUNE = ADDR_SCREEN + SCREEN_LEN            # $6328

TUNE_MAX = 0x1000                # the $B000 window
MC_W, H = 160, 200               # multicolor fat pixels
CELLS_X, CELLS_Y = 40, 25
PIC_ROWS = 20                    # rows above the text panel

# Chunky 5x7 block glyphs. Only the five characters the logo needs.
GLYPHS = {
    'L': ["X....", "X....", "X....", "X....", "X....", "X....", "XXXXX"],
    'M': ["X...X", "XX.XX", "X.X.X", "X...X", "X...X", "X...X", "X...X"],
    '6': [".XXX.", "X....", "X....", "XXXX.", "X...X", "X...X", ".XXX."],
    '4': ["...X.", "..XX.", ".X.X.", "X..X.", "XXXXX", "...X.", "...X."],
}
GLYPH_W, GLYPH_H = 5, 7

LOGO_COLOR = 1                   # white letters
# The Commodore rainbow, top to bottom. One entry per bar row; the run-time
# animation rotates this list, so its length is also the bar count.
# Deliberately the bright half of the palette, and no orange: the C64's
# red (2) and orange (8) are dark browns next to yellow, so the textbook
# red-orange-yellow rainbow reads as mud on black. Light red into purple
# spans the same spectrum with nothing muddy in it.
BAR_COLORS = [10, 7, 13, 3, 14, 4]  # lt red, yellow, lt green, cyan, lt blue, purple

# Layout, in fat pixels (x) and raster lines (y).
BLOCK_W, BLOCK_H = 6, 14         # one glyph cell of the big "LLM"
LOGO_TOP = 12
GAP = BLOCK_W                    # between LLM and 64
# Bars fill whole character rows from BAR_ROW to the bottom of the picture.
BAR_ROW = PIC_ROWS - len(BAR_COLORS)


def draw_logo():
    """Return a set of lit (x, y) fat pixels for LLM64."""
    big_w = 3 * GLYPH_W * BLOCK_W + 2 * BLOCK_W          # LLM plus letterspacing
    small_bw, small_bh = BLOCK_W // 2, BLOCK_H // 2
    small_w = 2 * GLYPH_W * small_bw + small_bw          # 64 plus letterspacing
    x0 = (MC_W - (big_w + GAP + small_w)) // 2

    lit = set()

    def stamp(ch, ox, oy, bw, bh):
        for ry, row in enumerate(GLYPHS[ch]):
            for rx, c in enumerate(row):
                if c != 'X':
                    continue
                for dy in range(bh):
                    for dx in range(bw):
                        lit.add((ox + rx * bw + dx, oy + ry * bh + dy))

    x = x0
    for ch in "LLM":
        stamp(ch, x, LOGO_TOP, BLOCK_W, BLOCK_H)
        x += GLYPH_W * BLOCK_W + BLOCK_W
    x = x0 + big_w + GAP
    # "64" is half height and TOP-justified with LLM, not centered on it.
    for ch in "64":
        stamp(ch, x, LOGO_TOP, small_bw, small_bh)
        x += GLYPH_W * small_bw + small_bw

    bottom = LOGO_TOP + GLYPH_H * BLOCK_H
    if bottom > BAR_ROW * 8:
        raise SystemExit(f"logo reaches y={bottom} but the bars start at "
                         f"y={BAR_ROW * 8}: shrink BLOCK_H or LOGO_TOP")
    return lit


def render():
    """Draw the logo and the bars into (bitmap, screen)."""
    lit = draw_logo()
    bitmap = bytearray(BITMAP_LEN)
    screen = bytearray(SCREEN_LEN)

    # Letters: %01 pixels, one white cell color wherever anything is lit.
    for x, y in lit:
        cell = (y // 8) * CELLS_X + (x // 4)
        bitmap[cell * 8 + (y % 8)] |= 0x40 >> ((x % 4) * 2)
        screen[cell] = LOGO_COLOR << 4

    # Bars: solid rows of %01 ($55 = four lit fat pixels), one color per row.
    for i, color in enumerate(BAR_COLORS):
        row = BAR_ROW + i
        for cx in range(CELLS_X):
            cell = row * CELLS_X + cx
            for b in range(8):
                bitmap[cell * 8 + b] = 0x55
            screen[cell] = color << 4
    return bitmap, screen


def sid_payload(path):
    """(memory image, load, init, play, start, songs, name, author).

    Header parsing matches src/music.py::payload(): data offset is a
    big-endian word at 6-7, and a zero load field means the real address is
    a little-endian word at the front of the data.
    """
    data = path.read_bytes()
    if data[:4] not in (b"PSID", b"RSID"):
        raise SystemExit(f"{path}: not a PSID/RSID file")
    offset = struct.unpack(">H", data[6:8])[0]
    load, init, play = struct.unpack(">HHH", data[8:14])
    songs, start = struct.unpack(">HH", data[14:18])
    payload = data[offset:]
    if load == 0:
        load = struct.unpack("<H", payload[:2])[0]
        payload = payload[2:]
    name = data[0x16:0x36].rstrip(b"\0").decode("latin-1")
    author = data[0x36:0x56].rstrip(b"\0").decode("latin-1")
    return payload, load, init, play, start, songs, name, author


def write_preview(path, bitmap, screen):
    """Render exactly what the VIC will show. Optional: needs PIL."""
    try:
        from PIL import Image
        from src.imaging import PALETTE
    except ImportError:
        print(f"  (no PIL: skipping {path.name})")
        return
    img = Image.new("RGB", (320, H))
    for cell in range(1000):
        cy, cx = divmod(cell, CELLS_X)
        fg = PALETTE[screen[cell] >> 4]
        dim = cy >= PIC_ROWS
        for row in range(8):
            byte = bitmap[cell * 8 + row]
            y = cy * 8 + row
            for px in range(4):
                on = (byte >> ((3 - px) * 2)) & 3
                c = fg if on else (0, 0, 0)
                if dim:
                    c = tuple(v // 4 for v in c)
                x = (cx * 4 + px) * 2
                img.putpixel((x, y), c)
                img.putpixel((x + 1, y), c)
    img.resize((640, H * 2), Image.NEAREST).save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tune", type=Path, help="relocated .sid from data/sids/b000*")
    ap.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    payload, load, init, play, start, songs, name, author = sid_payload(args.tune)
    if load != 0xB000:
        raise SystemExit(f"{args.tune}: load address ${load:04X}, expected $B000 - "
                         "use a tune from the relocated library, not raw HVSC")
    if len(payload) > TUNE_MAX:
        raise SystemExit(f"{args.tune}: {len(payload)} bytes overflows the "
                         f"{TUNE_MAX}-byte $B000 window")

    bitmap, screen = render()
    blob = struct.pack("<H", BLOB_LOAD) + bytes(bitmap) + bytes(screen) + payload
    (args.out / "intro_data.bin").write_bytes(blob)

    bars = ", ".join(f"${c << 4:02X}" for c in BAR_COLORS)
    (args.out / "intro_gen.inc").write_text(
        "; Generated by tools/make_intro_assets.py - do not edit.\n"
        f"; tune: {name} / {author} ({args.tune.name})\n"
        "\n"
        f"SID_LOAD = ${load:04X}\n"
        f"SID_INIT = ${init:04X}\n"
        f"SID_PLAY = ${play:04X}\n"
        f"SID_SIZE = {len(payload)}\n"
        f"SID_SONG = {start - 1}\t\t; A value for the init call\n"
        f"PIC_BG   = 0\t\t; $D021: the logo sits on black\n"
        f"BAR_ROW   = {BAR_ROW}\t\t; first character row of the rainbow\n"
        f"BAR_COUNT = {len(BAR_COLORS)}\n"
        "\n"
        "; Screen-RAM bytes for the bars, top to bottom, already shifted into\n"
        "; the upper nibble. The run-time cycle rotates this table, so it is\n"
        "; emitted here rather than duplicated in intro.s.\n"
        ".macro  BAR_COLOR_TABLE\n"
        f"        .byte   {bars}\n"
        ".endmacro\n"
    )

    write_preview(args.out / "intro_preview.png", bitmap, screen)

    print(f"{args.out / 'intro_data.bin'}: {len(blob)} bytes "
          f"(${BLOB_LOAD:04X}-${BLOB_LOAD + len(blob) - 3:04X})")
    print(f"  bitmap ${ADDR_BITMAP:04X}  screen ${ADDR_SCREEN:04X}  "
          f"tune ${ADDR_TUNE:04X}")
    print(f"  logo: LLM64, {len(BAR_COLORS)} bars from row {BAR_ROW}")
    print(f"  tune: {name} - {author}, {len(payload)} bytes, "
          f"init ${init:04X} play ${play:04X} song {start}/{songs}")


if __name__ == "__main__":
    main()
