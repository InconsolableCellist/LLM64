"""Inline markup: [color=grey]steel door[/color] -> marker cells.

See docs/08-inline-color.md for the original design and
docs/16-windows-311-client.md section 7 for how it became per-client.
The short version:

Cell values 0x00-0x1F render as a space on the soft-80 client (soft80.s
clamps a font index that underflows to glyph 0), so a marker can live
IN the text as an ordinary cell. That is what makes this free - colour
rides inside the 80 columns it decorates, costing no scrollback RAM.

Because a marker occupies a column ON THE C64, we do not ADD one: we
swallow the space beside the tag. Spacing on screen is then identical to
the plain text, and since the C64's colour matrix is one entry per TWO
characters, the only cells ever caught by that granularity are the
marker-spaces themselves - which are invisible.

**That swallow is C64-specific and it is why this file takes a profile.**
A client that draws markers as zero-width (the Windows client's painter
advances by the run length and a marker contributes nothing) never
needed the column, so taking the space from it deletes a real space:
"You see a [color=grey]steel door[/color] ahead." arrives as
"You see asteel doorahead." Profile.marker_cells decides.

Tags are NOT stripped from stored history: the model should see its own
past usage and stay consistent. This transform runs at egress only.
"""

import re

from .profiles import C64, WIRE_COLORS, PALETTE_C64, PALETTE_RICH

# Common Unicode punctuation -> ASCII approximations, applied before the
# ascii/replace encode so LLM typography doesn't become '?' on the C64.
UNICODE_TO_ASCII = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '“': '"', '”': '"', '„': '"',
    '–': '-', '—': '-', '―': '-', '−': '-',
    '…': '...', '•': '*', '·': '*',
    ' ': ' ', '→': '->', '←': '<-',
    '×': 'x', '÷': '/', '°': ' deg',
})

# Marker cells (docs/08 §2). 0x00 is unusable - payloads are C strings -
# and 0x0A/0x0D stay newline/CR.
M_CLOSE = 0x01
M_BOLD_ON = 0x02
M_BOLD_OFF = 0x03
M_COLOR_BASE = 0x10        # 0x10|c, c = 1..14 (1..15 for rich text)

# Rich text only. A C64 has one face and no attributes beyond reverse, so
# these are never emitted to it - the tags strip to plain text instead.
M_ITALIC_ON = 0x04
M_ITALIC_OFF = 0x05
M_ULINE_ON = 0x06
M_ULINE_OFF = 0x07
M_HEAD_ON = 0x0E
M_HEAD_OFF = 0x0F

# Colour beyond the sixteen the one-byte marker can encode:
#
#     0x1B 'C' (0x40 | slot)      slot 0..63
#
# The operand is biased into 0x40-0x7F so it can never be a NUL (payloads
# are C strings), a newline, or another marker - which keeps a client's
# marker scanner able to resynchronise on any byte. Three bytes buys 64
# slots; the one-byte form still carries 1..15, so the extended marker
# only appears when a colour actually needs it.
M_ESC = 0x1B
ESC_COLOR = 0x43           # 'C'
ESC_OPERAND_BIAS = 0x40

# Kept as the module-level name it has always been: this is the C64's
# palette, and every existing importer means that one.
PALETTE = PALETTE_C64

OPEN_RE = re.compile(r"\[\s*colou?r\s*[=:]\s*([a-z]+)\s*\]", re.IGNORECASE)
CLOSE_RE = re.compile(r"\[\s*/\s*colou?r\s*\]", re.IGNORECASE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# Attribute tags. Bracketed rather than markdown's asterisks and
# underscores on purpose: UNICODE_TO_ASCII turns a bullet into '*', so a
# single-asterisk italic rule would eat every bullet list, and '_' is
# ordinary inside a filename or an identifier. Bracket tags cannot
# collide with either, and they read like the colour tag beside them.
ATTR_TAGS = {
    'i': (M_ITALIC_ON, M_ITALIC_OFF),
    'u': (M_ULINE_ON, M_ULINE_OFF),
    'h': (M_HEAD_ON, M_HEAD_OFF),
}
ATTR_OPEN_RE = re.compile(r"\[\s*([iuh])\s*\]", re.IGNORECASE)
ATTR_CLOSE_RE = re.compile(r"\[\s*/\s*([iuh])\s*\]", re.IGNORECASE)

# A close tag sitting before punctuation reads better after it, and it
# keeps the close marker next to a space it can swallow.
HOIST_RE = re.compile(r"(\[\s*/\s*colou?r\s*\])([,.;:!?)\]\"']+)",
                      re.IGNORECASE)


def _swallow_before(out: list) -> None:
    """Drop a trailing space so the marker can take its column."""
    if out and out[-1] == 0x20:
        out.pop()


def _emit_color(out: bytearray, slot: int) -> None:
    """Append whichever colour marker can carry this slot."""
    if slot <= 15:
        out.append(M_COLOR_BASE | slot)
    else:
        out.append(M_ESC)
        out.append(ESC_COLOR)
        out.append(ESC_OPERAND_BIAS | slot)


def colorize_for_wire(text: str, profile=C64) -> bytes:
    """Text with markup -> ASCII bytes with in-band marker cells.

    Unknown colour names strip to plain text: a tag must never reach the
    screen as literal characters, and a missing colour is a cosmetic
    absence rather than garbage. The same goes for an attribute tag sent
    to a client with one typeface.

    With the default profile this is byte-for-byte what it always was.
    """
    palette = profile.palette
    top = profile.max_color_slot

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
            c = palette.get(m.group(1).lower())
            if c and c <= top:
                if profile.marker_cells:
                    _swallow_before(out)
                _emit_color(out, c)
            i = m.end()
            continue
        m = CLOSE_RE.match(text, i)
        if m:
            out.append(M_CLOSE)
            i = m.end()
            # The close marker took its own column, so eat the space
            # that follows it instead of the one before. A zero-width
            # marker took no column and owes nothing back.
            if profile.marker_cells and i < n and text[i] == ' ':
                i += 1
            continue
        m = ATTR_OPEN_RE.match(text, i)
        if m:
            if profile.rich_text:
                out.append(ATTR_TAGS[m.group(1).lower()][0])
            i = m.end()
            continue
        m = ATTR_CLOSE_RE.match(text, i)
        if m:
            if profile.rich_text:
                out.append(ATTR_TAGS[m.group(1).lower()][1])
            i = m.end()
            continue
        ch = text[i]
        out.append(ord(ch) if ord(ch) < 0x80 else 0x3F)
        i += 1
    return bytes(out)


def strip_markup(text: str) -> str:
    """Plain text with every tag removed - for anything that is not a
    client screen (titles, logs, the caption of an image)."""
    text = HOIST_RE.sub(lambda m: m.group(2) + m.group(1), text)
    text = BOLD_RE.sub(lambda m: m.group(1), text)
    text = OPEN_RE.sub('', text)
    text = CLOSE_RE.sub('', text)
    text = ATTR_OPEN_RE.sub('', text)
    return ATTR_CLOSE_RE.sub('', text)


def prompt_snippet(profile=C64) -> str:
    """Teaches the markup. Deliberately short - the adventure prompt
    already carries state and music instructions - and deliberately
    strict about sparing use: everything coloured is nothing coloured.

    Only the vocabulary is per-client. The DISCIPLINE is not: a model
    told it may decorate freely does, and a wall of colour reads worse
    than none on a 16-colour VGA than it does on a VIC-II.
    """
    attrs = (
        " Use [i]...[/i] for a title or a remembered voice, [u]...[/u] "
        "for something written down, and [h]...[/h] for a heading on its "
        "own line."
        if profile.rich_text else
        " Each tag costs a column on an 80-character screen."
    )
    return (
        "\nColour: wrap a noun in [color=NAME]...[/color] to tint it on "
        "the player's screen, and **word** for emphasis. Colours: "
        + ", ".join(sorted(set(profile.palette))) +
        ". Tag the WHOLE noun phrase, not just its head word: write "
        "[color=blue]pool of dark water[/color] and "
        "[color=brown]rusted iron key[/color], NOT "
        "[color=blue]pool[/color] of dark water. Adjectives and 'of' "
        "phrases belong inside the tag. Use it SPARINGLY - objects the "
        "player can take, exits, and hazards. Most sentences should have "
        "no colour at all. Never colour a whole sentence, and never nest "
        "tags." + attrs
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
