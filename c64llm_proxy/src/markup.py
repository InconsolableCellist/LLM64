"""Inline colour markup: [color=grey]steel door[/color] -> marker cells.

See docs/08-inline-color.md for the full design. The short version:

Cell values 0x00-0x1F render as a space on the soft-80 client (soft80.s
clamps a font index that underflows to glyph 0), so a marker can live
IN the text as an ordinary cell. That is what makes this free - colour
rides inside the 80 columns it decorates, costing no scrollback RAM.

Because a marker occupies a column, we do not ADD one: we swallow the
space beside the tag. Spacing on screen is then identical to the plain
text, and since the C64's colour matrix is one entry per TWO characters,
the only cells ever caught by that granularity are the marker-spaces
themselves - which are invisible.

Tags are NOT stripped from stored history: the model should see its own
past usage and stay consistent. This transform runs at egress to the
C64 only.
"""

import re

# Marker cells (docs/08 §2). 0x00 is unusable - payloads are C strings -
# and 0x0A/0x0D stay newline/CR.
M_CLOSE = 0x01
M_BOLD_ON = 0x02
M_BOLD_OFF = 0x03
M_COLOR_BASE = 0x10        # 0x10|c, c = 1..14

# Readable-on-black only. Colour 15 is deliberately absent: it would let
# an encoded line_color collide with the 0xFF rainbow sentinel. Blue (6)
# is illegible on black, so it maps to light blue.
PALETTE = {
    'white': 1, 'red': 2, 'cyan': 3, 'purple': 4, 'violet': 4,
    'magenta': 4, 'green': 5, 'yellow': 7, 'orange': 8, 'brown': 9,
    'pink': 10, 'lightred': 10, 'grey': 12, 'gray': 12, 'silver': 12,
    'lightgreen': 13, 'lime': 13, 'blue': 14, 'lightblue': 14,
}

OPEN_RE = re.compile(r"\[\s*colou?r\s*[=:]\s*([a-z]+)\s*\]", re.IGNORECASE)
CLOSE_RE = re.compile(r"\[\s*/\s*colou?r\s*\]", re.IGNORECASE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# A close tag sitting before punctuation reads better after it, and it
# keeps the close marker next to a space it can swallow.
HOIST_RE = re.compile(r"(\[\s*/\s*colou?r\s*\])([,.;:!?)\]\"']+)",
                      re.IGNORECASE)


def _swallow_before(out: list) -> None:
    """Drop a trailing space so the marker can take its column."""
    if out and out[-1] == 0x20:
        out.pop()


def colorize_for_wire(text: str) -> bytes:
    """Text with markup -> ASCII bytes with in-band marker cells.

    Unknown colour names strip to plain text: a tag must never reach the
    screen as literal characters, and a missing colour is a cosmetic
    absence rather than garbage.
    """
    # Punctuation first, so the close tag is already adjacent to the
    # space it will swallow.
    text = HOIST_RE.sub(lambda m: m.group(2) + m.group(1), text)
    text = BOLD_RE.sub(
        lambda m: chr(M_BOLD_ON) + m.group(1) + chr(M_BOLD_OFF), text)

    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        m = OPEN_RE.match(text, i)
        if m:
            c = PALETTE.get(m.group(1).lower())
            if c:
                _swallow_before(out)
                out.append(M_COLOR_BASE | c)
            i = m.end()
            continue
        m = CLOSE_RE.match(text, i)
        if m:
            out.append(M_CLOSE)
            i = m.end()
            # The close marker took its own column, so eat the space
            # that follows it instead of the one before.
            if i < n and text[i] == ' ':
                i += 1
            continue
        ch = text[i]
        out.append(ord(ch) if ord(ch) < 0x80 else 0x3F)
        i += 1
    return bytes(out)


def strip_markup(text: str) -> str:
    """Plain text with every tag removed - for anything that is not the
    C64 (titles, logs, the caption of an image)."""
    text = HOIST_RE.sub(lambda m: m.group(2) + m.group(1), text)
    text = BOLD_RE.sub(lambda m: m.group(1), text)
    text = OPEN_RE.sub('', text)
    return CLOSE_RE.sub('', text)


def prompt_snippet() -> str:
    """Teaches the markup. Deliberately short - the adventure prompt
    already carries state and music instructions - and deliberately
    strict about sparing use: everything coloured is nothing coloured,
    and each tag costs a column on an 80-character screen."""
    return (
        "\nColour: wrap a noun in [color=NAME]...[/color] to tint it on "
        "the player's screen, and **word** for emphasis. Colours: "
        + ", ".join(sorted(set(PALETTE))) +
        ". Use it SPARINGLY - objects the player can take, exits, and "
        "hazards. Most sentences should have no colour at all. Never "
        "colour a whole sentence, and never nest tags."
    )


# Everything that could still turn into (or change) markup if more text
# arrives. A COMPLETE close tag is held too: punctuation after it gets
# hoisted inside the run, and we cannot hoist what we already sent.
_TAIL_RE = re.compile(
    r"("
    # close tag, any punctuation to hoist inside it, and the space it
    # will swallow - all three have to arrive before we can transform
    r"\[\s*/\s*colou?r\s*\][,.;:!?)\]\"']*\s*"
    r"|\s*\**\[[^\]]*"                        # partial tag
    r"|\*{1,2}"                                # partial bold
    r"|\s+"                                    # space a tag may swallow
    r")$", re.IGNORECASE)


def split_safe(text: str):
    """(emit, hold) - the largest prefix safe to transform now.

    colorize_for_wire() works on whole strings, but a stream hands it
    arbitrary slices, and markup cut across a slice matches nothing and
    prints literally. This holds back the tail that could still turn
    into markup, so the caller can transform the rest immediately and
    keep streaming.

    A trailing SPACE is held for a different reason: an open tag swallows
    the space before it, and a space already sent cannot be taken back.
    """
    m = _TAIL_RE.search(text)
    emit, hold = (text[:m.start()], m.group(0)) if m else (text, '')
    # An odd number of '**' means a bold run is still open. Hold from its
    # opener: emitted now, the opening pair would never meet its partner
    # and would print as literal asterisks.
    if emit.count('**') % 2:
        i = emit.rfind('**')
        hold = emit[i:] + hold
        emit = emit[:i]
    return emit, hold
