#!/usr/bin/env python3
"""Generate src/font48.s: a 4x8 font for soft-80 mode (ASCII 0x20-0x7F).

Glyphs are 3 pixels wide (the 4th column is inter-character spacing) and
drawn in rows 1-6 with descenders in 6-7. Pixels live in the HIGH nibble
of each byte; the blitter shifts for odd columns.

Edit the GLYPHS table and re-run: python3 tools/make_font.py
"""

from pathlib import Path

# Each glyph: 8 strings of 3 chars, '#' = pixel. Row 0 usually blank.
G = {}

def g(ch, *rows):
    assert len(rows) == 8 and all(len(r) == 3 for r in rows), ch
    G[ch] = rows

B = '...'
g(' ', B, B, B, B, B, B, B, B)
g('!', B, '.#.', '.#.', '.#.', '.#.', B, '.#.', B)
g('"', B, '#.#', '#.#', B, B, B, B, B)
g('#', B, '#.#', '###', '#.#', '###', '#.#', B, B)
g('$', B, '.##', '##.', '.#.', '.##', '##.', B, B)
g('%', B, '#.#', '..#', '.#.', '#..', '#.#', B, B)
g('&', B, '.#.', '#.#', '.#.', '#.#', '.##', B, B)
g("'", B, '.#.', '.#.', B, B, B, B, B)
g('(', B, '..#', '.#.', '.#.', '.#.', '..#', B, B)
g(')', B, '#..', '.#.', '.#.', '.#.', '#..', B, B)
g('*', B, B, '#.#', '.#.', '#.#', B, B, B)
g('+', B, B, '.#.', '###', '.#.', B, B, B)
g(',', B, B, B, B, B, '.#.', '#..', B)
g('-', B, B, B, '###', B, B, B, B)
g('.', B, B, B, B, B, '.#.', B, B)
g('/', B, '..#', '..#', '.#.', '#..', '#..', B, B)
g('0', B, '.#.', '#.#', '#.#', '#.#', '.#.', B, B)
g('1', B, '.#.', '##.', '.#.', '.#.', '###', B, B)
g('2', B, '##.', '..#', '.#.', '#..', '###', B, B)
g('3', B, '##.', '..#', '.#.', '..#', '##.', B, B)
g('4', B, '#.#', '#.#', '###', '..#', '..#', B, B)
g('5', B, '###', '#..', '##.', '..#', '##.', B, B)
g('6', B, '.##', '#..', '###', '#.#', '.#.', B, B)
g('7', B, '###', '..#', '.#.', '.#.', '.#.', B, B)
g('8', B, '.#.', '#.#', '.#.', '#.#', '.#.', B, B)
g('9', B, '.#.', '#.#', '.##', '..#', '##.', B, B)
g(':', B, B, '.#.', B, '.#.', B, B, B)
g(';', B, B, '.#.', B, '.#.', '#..', B, B)
g('<', B, '..#', '.#.', '#..', '.#.', '..#', B, B)
g('=', B, B, '###', B, '###', B, B, B)
g('>', B, '#..', '.#.', '..#', '.#.', '#..', B, B)
g('?', B, '##.', '..#', '.#.', B, '.#.', B, B)
g('@', B, '.#.', '#.#', '#.#', '#..', '.##', B, B)
g('A', B, '.#.', '#.#', '###', '#.#', '#.#', B, B)
g('B', B, '##.', '#.#', '##.', '#.#', '##.', B, B)
g('C', B, '.##', '#..', '#..', '#..', '.##', B, B)
g('D', B, '##.', '#.#', '#.#', '#.#', '##.', B, B)
g('E', B, '###', '#..', '##.', '#..', '###', B, B)
g('F', B, '###', '#..', '##.', '#..', '#..', B, B)
g('G', B, '.##', '#..', '#.#', '#.#', '.##', B, B)
g('H', B, '#.#', '#.#', '###', '#.#', '#.#', B, B)
g('I', B, '###', '.#.', '.#.', '.#.', '###', B, B)
g('J', B, '..#', '..#', '..#', '#.#', '.#.', B, B)
g('K', B, '#.#', '#.#', '##.', '#.#', '#.#', B, B)
g('L', B, '#..', '#..', '#..', '#..', '###', B, B)
g('M', B, '#.#', '###', '###', '#.#', '#.#', B, B)
g('N', B, '#.#', '###', '###', '###', '#.#', B, B)
g('O', B, '.#.', '#.#', '#.#', '#.#', '.#.', B, B)
g('P', B, '##.', '#.#', '##.', '#..', '#..', B, B)
g('Q', B, '.#.', '#.#', '#.#', '##.', '.##', B, B)
g('R', B, '##.', '#.#', '##.', '#.#', '#.#', B, B)
g('S', B, '.##', '#..', '.#.', '..#', '##.', B, B)
g('T', B, '###', '.#.', '.#.', '.#.', '.#.', B, B)
g('U', B, '#.#', '#.#', '#.#', '#.#', '.#.', B, B)
g('V', B, '#.#', '#.#', '#.#', '.#.', '.#.', B, B)
g('W', B, '#.#', '#.#', '###', '###', '#.#', B, B)
g('X', B, '#.#', '#.#', '.#.', '#.#', '#.#', B, B)
g('Y', B, '#.#', '#.#', '.#.', '.#.', '.#.', B, B)
g('Z', B, '###', '..#', '.#.', '#..', '###', B, B)
g('[', B, '.##', '.#.', '.#.', '.#.', '.##', B, B)
g('\\', B, '#..', '#..', '.#.', '..#', '..#', B, B)
g(']', B, '##.', '.#.', '.#.', '.#.', '##.', B, B)
g('^', B, '.#.', '#.#', B, B, B, B, B)
g('_', B, B, B, B, B, B, '###', B)
g('`', B, '#..', '.#.', B, B, B, B, B)
g('a', B, B, '.##', '#.#', '#.#', '.##', B, B)
g('b', B, '#..', '##.', '#.#', '#.#', '##.', B, B)
g('c', B, B, '.##', '#..', '#..', '.##', B, B)
g('d', B, '..#', '.##', '#.#', '#.#', '.##', B, B)
g('e', B, B, '.#.', '###', '#..', '.##', B, B)
g('f', B, '..#', '.#.', '###', '.#.', '.#.', B, B)
g('g', B, B, '.##', '#.#', '.##', '..#', '##.', B)
g('h', B, '#..', '##.', '#.#', '#.#', '#.#', B, B)
g('i', B, '.#.', B, '.#.', '.#.', '.#.', B, B)
g('j', B, '..#', B, '..#', '..#', '#.#', '.#.', B)
g('k', B, '#..', '#.#', '##.', '#.#', '#.#', B, B)
g('l', B, '.#.', '.#.', '.#.', '.#.', '..#', B, B)
g('m', B, B, '###', '###', '#.#', '#.#', B, B)
g('n', B, B, '##.', '#.#', '#.#', '#.#', B, B)
g('o', B, B, '.#.', '#.#', '#.#', '.#.', B, B)
g('p', B, B, '##.', '#.#', '##.', '#..', '#..', B)
g('q', B, B, '.##', '#.#', '.##', '..#', '..#', B)
g('r', B, B, '#.#', '##.', '#..', '#..', B, B)
g('s', B, B, '.##', '##.', '.##', '##.', B, B)
g('t', B, '.#.', '###', '.#.', '.#.', '..#', B, B)
g('u', B, B, '#.#', '#.#', '#.#', '.##', B, B)
g('v', B, B, '#.#', '#.#', '.#.', '.#.', B, B)
g('w', B, B, '#.#', '#.#', '###', '###', B, B)
g('x', B, B, '#.#', '.#.', '.#.', '#.#', B, B)
g('y', B, B, '#.#', '#.#', '.##', '..#', '##.', B)
g('z', B, B, '###', '.##', '#..', '###', B, B)
g('{', B, '..#', '.#.', '##.', '.#.', '..#', B, B)
g('|', B, '.#.', '.#.', '.#.', '.#.', '.#.', B, B)
g('}', B, '#..', '.#.', '.##', '.#.', '#..', B, B)
g('~', B, B, '.##', '##.', B, B, B, B)
# 0x7F is a real glyph now, not the fallback block it used to be: a
# quarter note, so the status row can say "music" in one column. PETSCII
# has no note and there was no spare slot below 0x7F; cell_from_ascii()
# gated this code off (c < 0x7F) only because nothing lived here. The
# 40-col build has no font of ours to extend and still shows '?'.
g('\x7f', B, '..#', '..#', '..#', '..#', '###', '##.', B)

# Anything the table does not define. Reachable only if a code in
# 0x20-0x7F is left out - kept SEPARATE from 0x7F so that putting a
# glyph there cannot silently change what a missing glyph looks like.
FALLBACK = ('###', '###', '###', '###', '###', '###', '###', '###')

def glyph_bytes(code):
    rows = G.get(chr(code), FALLBACK)
    by = []
    for r in rows:
        v = 0
        for i, c in enumerate(r):
            if c == '#':
                v |= 0x80 >> i
        by.append(v)
    return by


def main():
    out = ["; Generated by tools/make_font.py - do not edit by hand",
           "        .export _font48",
           "        .export _font48lo", "", "        .rodata",
           "; pixels in the high nibble (even columns)", "_font48:"]
    for code in range(0x20, 0x80):
        by = glyph_bytes(code)
        name = chr(code) if chr(code) not in ';:\\' and code != 0x7f \
            else f'chr{code}'
        out.append("        .byte " + ",".join(f"${b:02X}" for b in by)
                   + f"   ; '{name}'")
    out += ["", "; same glyphs pre-shifted to the low nibble (odd columns)",
            "_font48lo:"]
    for code in range(0x20, 0x80):
        by = [b >> 4 for b in glyph_bytes(code)]
        out.append("        .byte " + ",".join(f"${b:02X}" for b in by))
    Path(__file__).parent.parent.joinpath(
        'c64_client/src/font48.s').write_text("\n".join(out) + "\n")
    print(f"{len(G)} glyphs x2 -> c64_client/src/font48.s")

if __name__ == '__main__':
    main()
