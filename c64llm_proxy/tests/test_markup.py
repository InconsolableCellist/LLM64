#!/usr/bin/env python3
"""Inline colour markup -> marker cells. Run: python3 tests/test_markup.py

The property that matters most is SPACING: a marker occupies a column,
so the rendered line must come out character-for-character identical to
the plain text. Every case here checks that as well as the markers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.markup import (colorize_for_wire, strip_markup, split_safe,
                        PALETTE, M_CLOSE, M_BOLD_ON, M_BOLD_OFF,
                        M_COLOR_BASE)
from src.music import MusicDirectiveFilter, DIRECTIVE_RE
from src.dice import ROLL_RE

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def rendered(data: bytes) -> str:
    """What the C64 actually shows: markers draw as spaces."""
    return ''.join(' ' if b < 0x20 and b not in (0x0A, 0x0D) else chr(b)
                   for b in data)


GREY = M_COLOR_BASE | PALETTE['grey']
RED = M_COLOR_BASE | PALETTE['red']

# --- the headline case ------------------------------------------------
src = "the [color=grey]steel door[/color] to the north"
out = colorize_for_wire(src)
check("marker replaces the space before the tag", out,
      b"the" + bytes([GREY]) + b"steel door" + bytes([M_CLOSE]) + b"to the north")
check("spacing survives", rendered(out), "the steel door to the north")

# --- punctuation is hoisted so it colours with the word ---------------
out = colorize_for_wire("a [color=red]sword[/color], rusted")
check("close hoisted past the comma", out,
      b"a" + bytes([RED]) + b"sword," + bytes([M_CLOSE]) + b"rusted")
check("hoist keeps spacing", rendered(out), "a sword, rusted")

# --- start of line: nothing to swallow, marker takes a column ---------
out = colorize_for_wire("[color=red]Danger[/color] ahead")
check("leading tag", out,
      bytes([RED]) + b"Danger" + bytes([M_CLOSE]) + b"ahead")

# --- bold is reverse video, consumed at append time, no column --------
out = colorize_for_wire("go **north** now")
check("bold markers", out,
      b"go " + bytes([M_BOLD_ON]) + b"north" + bytes([M_BOLD_OFF]) + b" now")

# --- unknown colours strip rather than leak ---------------------------
out = colorize_for_wire("a [color=chartreuse]frog[/color] sits")
check("unknown colour strips the tag", out, b"a frog" + bytes([M_CLOSE]) + b"sits")
if b'[' in out or b'color' in out.lower():
    failures.append(f"tag leaked to the wire: {out!r}")

out = colorize_for_wire("a [color]broken[/color] tag")
check("malformed open left alone but never as a tag",
      b'[color]' in out, True)

# --- spelling / spacing / case tolerance ------------------------------
for variant in ("[colour=grey]", "[COLOR: grey]", "[ color = Grey ]"):
    out = colorize_for_wire("x " + variant + "y[/color] z")
    check(f"tolerates {variant}", out,
          b"x" + bytes([GREY]) + b"y" + bytes([M_CLOSE]) + b"z")

# --- nesting-free sanity: two runs in a line --------------------------
out = colorize_for_wire("a [color=red]b[/color] and [color=grey]c[/color] d")
check("two runs", rendered(out), "a b and c d")

# --- strip_markup for anything that is not the C64 --------------------
check("strip", strip_markup("the [color=grey]steel door[/color], ok"),
      "the steel door, ok")
check("strip bold", strip_markup("go **north**"), "go north")

# --- must not collide with the other bracket syntaxes -----------------
for probe in ("[color=red]", "[/color]"):
    if DIRECTIVE_RE.search(probe):
        failures.append(f"directive filter matched colour markup: {probe!r}")
    if ROLL_RE.search(probe):
        failures.append(f"dice matched colour markup: {probe!r}")
for probe in ("[[MUSIC: eerie]]", "[MUSIC: eerie]", "[roll:1d20]"):
    if colorize_for_wire(probe) != probe.encode():
        failures.append(f"colorize mangled another syntax: {probe!r}")

# --- a tag split across stream chunks must be held back ---------------
f = MusicDirectiveFilter()
parts, held = [], "the [color=gr"
parts.append(f.feed(held))
if '[color=gr' in parts[0]:
    failures.append("partial colour tag was released to the wire")
parts.append(f.feed("ey]door[/color] there") + f.flush())
whole = ''.join(parts)
check("held tag reassembles intact", whole,
      "the [color=grey]door[/color] there")

# --- streaming: markup cut across chunks must survive -----------------
# The transform sees arbitrary slices, so anything that could still
# become markup - including a space a tag might swallow - is held.
def stream(chunks):
    out, hold = bytearray(), ''
    for c in chunks:
        emit, hold = split_safe(hold + c)
        out.extend(colorize_for_wire(emit))
    out.extend(colorize_for_wire(hold))
    return bytes(out)

whole = "the [color=grey]steel door[/color], go **north** now"
want = colorize_for_wire(whole)
for cut in range(1, len(whole)):
    got = stream([whole[:cut], whole[cut:]])
    if got != want:
        failures.append(
            f"split at {cut} ({whole[:cut]!r} | {whole[cut:]!r}):\n"
            f"  got  {got!r}\n  want {want!r}")
        break

# byte-at-a-time is the worst case
if stream(list(whole)) != want:
    failures.append("char-by-char stream did not match whole-string")

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all markup tests pass")
