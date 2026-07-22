#!/usr/bin/env python3
"""Build the shareware intro's disk assets from a PNG and a relocated SID.

    tools/make_intro_assets.py <source.png> <tune.sid> -o c64_client/intro/assets/

Writes three files into the output directory:

  intro_data.bin    the complete "c64 llm.d" disk file: a $4000 load address
                    followed by bitmap 8000 + screen 1000 + colram 1000 +
                    the tune's memory image. The intro copies out of it at
                    fixed offsets, so those offsets are a contract with
                    intro.s (see BLOB_* below and the table in intro.s).
  intro_gen.inc     ca65 include with the constants intro.s needs.
  intro_preview.png what the conversion actually looks like, at 2x, with
                    the bottom five rows dimmed - those are covered by the
                    text screen at run time and are never displayed.

The picture goes through the same converter as the adventure-mode
illustrations (src/imaging.py::convert_to_c64_mc), so what ships here and
what the C64 draws mid-game come out of one code path. The source is
pre-cropped to 16:10 first: imaging's letterbox would otherwise pad a
square image with black bars and waste half the screen.

The tune must come from the project's relocated library (data/sids/b000*),
which is verified to run at $B000 with its zero-page use confined to
$FB-$FE. A raw HVSC file would clobber zero page the KERNAL LOAD needs.
"""

import argparse
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "c64llm_proxy"))

from PIL import Image

from src.imaging import convert_to_c64_mc, render_preview_mc

# Blob layout. Load address first, then the four parts back to back.
BLOB_LOAD = 0x4000
BITMAP_LEN, SCREEN_LEN, COLRAM_LEN = 8000, 1000, 1000
# Where each part lands once the blob is in memory - intro.s hardcodes these.
ADDR_BITMAP = BLOB_LOAD
ADDR_SCREEN = ADDR_BITMAP + BITMAP_LEN          # $5F40
ADDR_COLRAM = ADDR_SCREEN + SCREEN_LEN          # $6328
ADDR_TUNE = ADDR_COLRAM + COLRAM_LEN            # $6710

# The tune is copied to $B000 and must not run off the end of the window.
TUNE_MAX = 0x1000
# Only the top 20 of 25 rows show the picture; the rest is the text screen.
PIC_ROWS = 20


def crop_to_screen_aspect(img):
    """Crop to 320:200 so imaging's letterbox scales instead of padding.

    Biased upward - the logo lives at the top of the art, and the bottom
    fifth is lost to the text rows anyway.
    """
    w, h = img.size
    want_h = round(w * 200 / 320)
    if want_h <= h:
        top = (h - want_h) // 3
        return img.crop((0, top, w, top + want_h))
    want_w = round(h * 320 / 200)
    left = (w - want_w) // 2
    return img.crop((left, 0, left + want_w, h))


def sid_payload(path):
    """(memory image, load, init, play, start_song) from a PSID file.

    Header parsing matches src/music.py::payload(): the data offset is a
    big-endian word at 6-7, and a zero load field means the real address
    is a little-endian word at the front of the data.
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path, help="source picture (any PIL format)")
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

    img = crop_to_screen_aspect(Image.open(args.image))
    bitmap, screen, colram, bg = convert_to_c64_mc(img)
    assert (len(bitmap), len(screen), len(colram)) == (BITMAP_LEN, SCREEN_LEN, COLRAM_LEN)

    blob = struct.pack("<H", BLOB_LOAD) + bitmap + screen + colram + payload
    (args.out / "intro_data.bin").write_bytes(blob)

    inc = args.out / "intro_gen.inc"
    inc.write_text(
        "; Generated by tools/make_intro_assets.py - do not edit.\n"
        f"; picture: {args.image.name}\n"
        f"; tune:    {name} / {author} ({args.tune.name})\n"
        "\n"
        f"SID_LOAD = ${load:04X}\n"
        f"SID_INIT = ${init:04X}\n"
        f"SID_PLAY = ${play:04X}\n"
        f"SID_SIZE = {len(payload)}\n"
        f"SID_SONG = {start - 1}\t\t; A value for the init call\n"
        f"PIC_BG   = {bg}\t\t; $D021 while the bitmap is displayed\n"
    )

    preview = render_preview_mc(bitmap, screen, colram, bg)
    for y in range(PIC_ROWS * 8, preview.height):
        for x in range(preview.width):
            r, g, b = preview.getpixel((x, y))
            preview.putpixel((x, y), (r // 4, g // 4, b // 4))
    preview.resize((preview.width * 2, preview.height * 2),
                   Image.NEAREST).save(args.out / "intro_preview.png")

    print(f"{args.out / 'intro_data.bin'}: {len(blob)} bytes "
          f"(${BLOB_LOAD:04X}-${BLOB_LOAD + len(blob) - 3:04X})")
    print(f"  bitmap ${ADDR_BITMAP:04X}  screen ${ADDR_SCREEN:04X}  "
          f"colram ${ADDR_COLRAM:04X}  tune ${ADDR_TUNE:04X}")
    print(f"  tune: {name} - {author}, {len(payload)} bytes, "
          f"init ${init:04X} play ${play:04X} song {start}/{songs}")
    print(f"  picture background: {bg}")
    print(f"{inc}")
    print(f"{args.out / 'intro_preview.png'} (bottom {25 - PIC_ROWS} rows dimmed: "
          "covered by the text screen)")


if __name__ == "__main__":
    main()
