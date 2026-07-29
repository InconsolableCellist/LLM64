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

# --- per-client profiles (docs/16 section 7) --------------------------
#
# The C64 swallows the space beside a tag because its marker occupies a
# column. A client whose markers are zero-width must keep that space, or
# every coloured phrase loses one on each side.

from src.markup import (M_ITALIC_ON, M_ITALIC_OFF, M_ULINE_ON, M_ULINE_OFF,
                        M_HEAD_ON, M_HEAD_OFF, M_ESC, ESC_COLOR,
                        ESC_OPERAND_BIAS, prompt_snippet)
from src.profiles import (C64, WIN16, ClientProfile, from_hello,
                          CAP_ZERO_WIDTH_MARKERS, CAP_RICH_TEXT,
                          PALETTE_RICH, WIRE_COLORS)


def visible(data: bytes, profile) -> str:
    """What the client actually shows. C64 markers draw as a space;
    zero-width markers draw as nothing at all, and the extended colour
    marker takes its two operand bytes with it."""
    out = []
    i = 0
    while i < len(data):
        b = data[i]
        # The verb is checked because the CLIENT checks it (scroll.c
        # sb_marker_len): an escape byte not followed by 'C' is not a
        # marker there, and a helper that swallows it anyway hides
        # exactly the bug that shipped as "the []slate rooftops".
        if (b == M_ESC and profile.rich_text and i + 2 < len(data)
                and data[i + 1] == ESC_COLOR):
            i += 3           # ESC, verb, operand - all consumed
            continue
        if b < 0x20 and b not in (0x0A, 0x0D):
            out.append(' ' if profile.marker_cells else '')
        else:
            out.append(chr(b))
        i += 1
    return ''.join(out)


plain = "You see a steel door ahead."
src = "You see a [color=grey]steel door[/color] ahead."

# The headline defect: identical on screen for BOTH clients, by
# different means - the C64 by swallowing a space it gets back as a
# marker cell, the Windows client by never giving it up.
check("c64 spacing survives", visible(colorize_for_wire(src, C64), C64), plain)
check("win16 spacing survives",
      visible(colorize_for_wire(src, WIN16), WIN16), plain)
check("win16 keeps the space the c64 swallows",
      colorize_for_wire(src, WIN16),
      b"You see a " + bytes([GREY]) + b"steel door" + bytes([M_CLOSE])
      + b" ahead.")

# The default must not have moved: every existing caller passes no
# profile at all, and a C64 in the field cannot be rebuilt.
check("default profile is the c64, byte for byte",
      colorize_for_wire(src), colorize_for_wire(src, C64))

# --- attributes -------------------------------------------------------

attr = "the [i]Aeon Codex[/i], [u]signed[/u], [h]Chapter One[/h]"
check("rich text emits attribute markers",
      colorize_for_wire(attr, WIN16),
      b"the " + bytes([M_ITALIC_ON]) + b"Aeon Codex" + bytes([M_ITALIC_OFF])
      + b", " + bytes([M_ULINE_ON]) + b"signed" + bytes([M_ULINE_OFF])
      + b", " + bytes([M_HEAD_ON]) + b"Chapter One" + bytes([M_HEAD_OFF]))

# One typeface means the tag strips, exactly as an unknown colour does.
# What must never happen is a bracket reaching the screen.
c64_attr = visible(colorize_for_wire(attr, C64), C64)
check("a c64 sees the words and none of the tags", c64_attr,
      "the Aeon Codex, signed, Chapter One")
for bad in ('[', ']', 'i]', 'u]'):
    if bad in c64_attr:
        failures.append(f"attribute tag leaked to the c64 screen: {bad!r} "
                        f"in {c64_attr!r}")

# --- the extended colour marker ---------------------------------------

check("a slot past 15 uses the three-byte marker",
      colorize_for_wire("a [color=teal]bowl[/color] here", WIN16),
      b"a " + bytes([M_ESC, ESC_COLOR,
                     ESC_OPERAND_BIAS | PALETTE_RICH['teal']])
      + b"bowl" + bytes([M_CLOSE]) + b" here")

check("a slot within 15 still uses the one-byte marker",
      colorize_for_wire("a [color=red]bowl[/color]", WIN16),
      b"a " + bytes([RED]) + b"bowl" + bytes([M_CLOSE]))

# Slot 11's one-byte form IS the escape byte, so it has to travel in the
# three-byte form even though it is within 15. The field symptom was a
# box glyph glued to the tinted word, and - when the phrase began with
# 'C' - two characters eaten as a bogus operand.
check("slot 11 escapes rather than colliding with M_ESC",
      colorize_for_wire("the [color=darkgrey]slate[/color] roof", WIN16),
      b"the " + bytes([M_ESC, ESC_COLOR,
                       ESC_OPERAND_BIAS | PALETTE_RICH['darkgrey']])
      + b"slate" + bytes([M_CLOSE]) + b" roof")
check("that phrase shows no stray glyph",
      visible(colorize_for_wire(
          "the [color=darkgrey]slate[/color] roof", WIN16), WIN16),
      "the slate roof")

# The general rule, over the whole palette: no one-byte colour marker may
# be a byte a client reads as something else.
for name, slot in PALETTE_RICH.items():
    if slot <= 15 and (0x10 | slot) == M_ESC:
        got = colorize_for_wire(f"a [color={name}]bowl[/color]", WIN16)
        if bytes([M_ESC, ESC_COLOR, ESC_OPERAND_BIAS | slot]) not in got:
            failures.append(f"colour {name} (slot {slot}) emits a bare "
                            f"escape byte as a one-byte marker")

# Every operand has to stay clear of NUL, CR/LF and the marker range, or
# a client cannot resynchronise on it.
for name, slot in PALETTE_RICH.items():
    if slot <= 15:
        continue
    operand = ESC_OPERAND_BIAS | slot
    if operand < 0x20 or operand in (0x0A, 0x0D) or operand > 0x7F:
        failures.append(f"colour {name} (slot {slot}) encodes to an unsafe "
                        f"operand {operand:#04x}")

# A name the C64 has no slot for must strip rather than encode to
# something else - the C64 renders 0x1B as a space, not as a colour.
teal_c64 = colorize_for_wire("a [color=teal]bowl[/color] here", C64)
if M_ESC in teal_c64:
    failures.append("extended colour marker reached the c64")
check("an out-of-range colour strips on the c64",
      visible(teal_c64, C64), "a bowl here")

# Rich names must not have redefined a name the C64 already had, or the
# same prompt means two things.
from src.profiles import PALETTE_C64
for name, slot in PALETTE_C64.items():
    if name in ('blue', 'lightblue'):
        continue      # deliberately un-substituted for rich text
    check(f"palettes agree on {name!r}", PALETTE_RICH[name], slot)
check("rich text gets a real blue", PALETTE_RICH['blue'], 6)
check("...and the c64 keeps its substitute", PALETTE_C64['blue'], 14)

# Every palette slot must name a real colour.
for name, slot in PALETTE_RICH.items():
    if slot >= len(WIRE_COLORS):
        failures.append(f"{name} -> slot {slot}, past WIRE_COLORS")

# --- CLIENT_HELLO parsing ---------------------------------------------


def hello(version=1, width=80, payload_max=2048,
          caps=CAP_ZERO_WIDTH_MARKERS | CAP_RICH_TEXT, name=b'win16'):
    return bytes([version, width, payload_max & 0xFF, payload_max >> 8,
                  caps & 0xFF, caps >> 8, len(name)]) + name


got = from_hello(hello())
check("hello yields a profile", got is not None, True)
prof, caps = got
check("hello names the profile", prof.name, 'win16')
check("hello sets zero-width markers", prof.marker_cells, False)
check("hello sets rich text", prof.rich_text, True)
check("hello reports the pane width", prof.text_width, 80)
check("hello lifts the payload cap", prof.max_payload, 2048)

check("a client's own width wins over the table",
      from_hello(hello(width=132))[0].text_width, 132)
check("a width of zero falls back to the table",
      from_hello(hello(width=0))[0].text_width, WIN16.text_width)

# Malformed or unknown must degrade to the C64, never raise: a client
# that cannot introduce itself is one that never tried.
check("a short payload is refused", from_hello(b'\x01\x50'), None)
check("an empty payload is refused", from_hello(b''), None)
check("a future hello version is refused", from_hello(hello(version=2)), None)
check("a truncated name is refused",
      from_hello(bytes([1, 80, 0, 8, 3, 0, 40]) + b'win16'), None)

# An unknown client is a machine this proxy predates, not an error. Its
# own capability bits still have to be honoured.
unk = from_hello(hello(name=b'dos32', caps=CAP_RICH_TEXT))
check("an unknown client is served", unk is not None, True)
check("...under its own name", unk[0].name, 'dos32')
check("...with its rich text honoured", unk[0].rich_text, True)
check("...and its markers still counted as cells", unk[0].marker_cells, True)

# A client claiming nothing gets the conservative treatment.
bare = from_hello(hello(name=b'win16', caps=0))[0]
check("no caps means marker cells", bare.marker_cells, True)
check("no caps means no rich text", bare.rich_text, False)
check("no caps means the c64 palette", bare.palette, C64.palette)
check("a plain client renders identically to a c64",
      colorize_for_wire(src, bare), colorize_for_wire(src, C64))

# --- the prompt the model is given ------------------------------------

check("the c64 prompt warns about columns",
      'costs a column' in prompt_snippet(C64), True)
check("the rich prompt does not",
      'costs a column' in prompt_snippet(WIN16), False)
check("the rich prompt teaches the attribute tags",
      '[i]' in prompt_snippet(WIN16), True)
check("the c64 prompt does not",
      '[i]' in prompt_snippet(C64), False)
check("the rich prompt offers teal", 'teal' in prompt_snippet(WIN16), True)
check("the c64 prompt does not", 'teal' in prompt_snippet(C64), False)
# Sparing use is not negotiable on either machine.
for p in (C64, WIN16):
    check(f"{p.name} prompt still says SPARINGLY",
          'SPARINGLY' in prompt_snippet(p), True)

# strip_markup is what titles, logs and captions use: no tag of any kind
# may survive it.
check("strip_markup removes attribute tags",
      strip_markup("the [i]Codex[/i] and [u]seal[/u]"),
      "the Codex and seal")

# --- streaming, on the rich profile -----------------------------------
#
# split_safe holds back a tail that could still become markup. It is
# profile-blind, so the same guarantee has to hold for the encoding that
# has more markup in it.

def stream_p(chunks, profile):
    out = bytearray()
    hold = ''
    for c in chunks:
        emit, hold = split_safe(hold + c)
        out.extend(colorize_for_wire(emit, profile))
    out.extend(colorize_for_wire(hold, profile))
    return bytes(out)


rich_whole = "the [color=teal]astrolabe[/color], [i]hers[/i], **north** now"
rich_want = colorize_for_wire(rich_whole, WIN16)
for cut in range(1, len(rich_whole)):
    if stream_p([rich_whole[:cut], rich_whole[cut:]], WIN16) != rich_want:
        failures.append(
            f"rich split at {cut} ({rich_whole[:cut]!r} | "
            f"{rich_whole[cut:]!r}):\n"
            f"  got  {stream_p([rich_whole[:cut], rich_whole[cut:]], WIN16)!r}"
            f"\n  want {rich_want!r}")
        break
if stream_p(list(rich_whole), WIN16) != rich_want:
    failures.append("rich char-by-char stream did not match whole-string")

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all markup tests pass")
