"""Client profiles: what kind of machine is on the other end.

See docs/16-windows-311-client.md section 7. The point is not to add a
Windows special case. It is to stop treating the C64 as the default and
everything else as an exception: every C64 concession moves onto a
profile, and the code downstream reads the profile instead of a
constant.

The profile reaches only the EGRESS EDGE. Nothing about the
conversation, the modes, the adventure, the map or the dice knows a
profile exists - those decisions are the same for every client, which is
why two machines can share one world (section 13).

A client announces itself with CLIENT_HELLO (0x3F). Absence of it means
C64, so an existing client is unaffected by any of this.
"""

from dataclasses import dataclass, field
from typing import Dict


# --- capability bits ---------------------------------------------------
#
# Sent in CLIENT_HELLO. These are a WIRE CONTRACT: numbers here are
# reserved permanently, because a client built today has to keep meaning
# the same thing to a proxy built later. Reserving a bit is free;
# renumbering one silently misreads every old client.

CAP_ZERO_WIDTH_MARKERS = 0x0001  # markers occupy no screen cell
CAP_RICH_TEXT          = 0x0002  # italic/underline/heading, 64 colours
CAP_DIB_IMAGES         = 0x0004  # images as 8-bit DIBs (IMG_BEGIN fmt=2)

# Reserved, and NOT yet consumed by anything - the phases that will read
# them are named so the numbers are not reused meanwhile.
CAP_MIDI               = 0x0008  # phase 5: MIDI_* frames
CAP_PRINT_GDI          = 0x0010  # phase 6: a printer DC sink


# --- the wire colour table --------------------------------------------
#
# Slots 1-15 are the C64's, in the C64's order, because the one-byte
# colour marker (0x10|c) encodes exactly this and both clients already
# render it. Slots 16+ exist only in the extended marker and so only for
# a client that asked for rich text.
#
# These are NAMES, not RGB values, and that is deliberate: the client
# owns the actual ink. The Windows client has two themes - dark screen
# and white paper - and half of the C64 palette is illegible on white at
# any brightness, so it keeps each slot's HUE and picks a value that
# reads on the background it is actually drawing. A proxy that sent RGB
# would be overruling a decision only the client can make.

WIRE_COLORS = [
    'black',                                            # 0, never text
    'white', 'red', 'cyan', 'purple', 'green', 'blue',   # 1-6
    'yellow', 'orange', 'brown', 'pink', 'darkgrey',     # 7-11
    'grey', 'lightgreen', 'lightblue', 'lightgrey',      # 12-15
    # --- extended: rich-text clients only ---
    'teal', 'navy', 'maroon', 'olive', 'gold', 'crimson',    # 16-21
    'lavender', 'sky', 'rose', 'mint', 'amber', 'slate',     # 22-27
    'plum', 'sand', 'moss', 'ink',                           # 28-31
]

# Names the model may use, and the slot each resolves to.
#
# C64: readable-on-black only. Slot 15 is deliberately absent - it would
# let an encoded line_color collide with the 0xFF rainbow sentinel - and
# blue (6) is illegible on black, so it maps to light blue.
PALETTE_C64: Dict[str, int] = {
    'white': 1, 'red': 2, 'cyan': 3, 'purple': 4, 'violet': 4,
    'magenta': 4, 'green': 5, 'yellow': 7, 'orange': 8, 'brown': 9,
    'pink': 10, 'lightred': 10, 'grey': 12, 'gray': 12, 'silver': 12,
    'lightgreen': 13, 'lime': 13, 'blue': 14, 'lightblue': 14,
}

# Rich text: every C64 name still resolves, so a model prompt written for
# one client works on the other. What is added is what the C64 could not
# have - a real blue rather than a substitute, slot 15, and the extended
# slots. Aliases are kept identical so the two never disagree about a
# name they share.
PALETTE_RICH: Dict[str, int] = dict(PALETTE_C64, **{
    'blue': 6, 'lightblue': 14, 'darkgrey': 11, 'darkgray': 11,
    'lightgrey': 15, 'lightgray': 15,
    'teal': 16, 'navy': 17, 'maroon': 18, 'olive': 19, 'gold': 20,
    'crimson': 21, 'lavender': 22, 'sky': 23, 'rose': 24, 'mint': 25,
    'amber': 26, 'slate': 27, 'plum': 28, 'sand': 29, 'moss': 30,
    'ink': 31,
})


@dataclass(frozen=True)
class ClientProfile:
    """One machine's shape, at the egress edge.

    Only fields something actually reads are here. Images, MIDI and
    print routing are phases 3, 5 and 6; they get their fields when
    there is code to consume them, so that nothing in this table looks
    live before it is.
    """

    name: str = 'c64'

    # Columns the client shows. What /map and /print lay out to.
    text_width: int = 80

    # The client's frame buffer. send_message refuses anything larger,
    # because an oversized frame desyncs the client's parser rather than
    # merely truncating (field bug: 1000-char load messages).
    max_payload: int = 512

    # Pace bulk transfers to the wire rate. True for anything behind a
    # serial line; false for a socket, which has its own flow control.
    pace: bool = True

    # Does an in-band marker occupy a screen column?
    #
    # On the C64 it does: cell values 0x00-0x1F draw as a space, which is
    # what makes colour free, and the transform therefore SWALLOWS the
    # space beside a tag so the spacing comes out identical to the plain
    # text. A client that draws markers as zero-width must not have that
    # space taken from it, or every coloured phrase loses the space on
    # each side of it ("You see asteel doorahead.").
    marker_cells: bool = True

    # Italic, underline, headings, and the extended colour marker.
    rich_text: bool = False

    # Images leave as an 8-bit DIB rendered from the retained original
    # PNG (IMG_BEGIN fmt=2) instead of the C64 multicolor blob. Like
    # rich_text this follows the CAPABILITY, not the table row: only a
    # client that claimed CAP_DIB_IMAGES has the fmt=2 parser.
    dib_images: bool = False

    # What this machine can play. 'sid' is a relocated 6502 memory image
    # run off the raster IRQ - meaningful to a C64 and to nothing else
    # on earth. None means no music yet; the win16 row gets 'midi' when
    # the MIDI phase lands (docs/16 section 6.2). Field bug this guards:
    # the proxy streamed a SID at a Windows client, which printed
    # "[frame 0x57]" and never ACKed, and every tune cost four BEGIN
    # retries before the abort.
    music_fmt: str = 'sid'

    # The art-style prompt this machine's pictures are generated with
    # (docs/16 section 6.1, path C): a C64 scene wants flat areas and
    # strong silhouettes because that is what survives the 160x200
    # dither, a 1993 PC scene wants VGA pixel art. None = images.py's
    # C64 default. An explicit [images].style_prefix in config.toml
    # overrides every profile - the operator outranks the table.
    image_style: str = None

    # Colour name -> wire slot.
    palette: Dict[str, int] = field(default_factory=lambda: PALETTE_C64)

    @property
    def max_color_slot(self) -> int:
        """Highest slot this client can be sent."""
        return len(WIRE_COLORS) - 1 if self.rich_text else 14


C64 = ClientProfile()

# What a 1993 PC's pictures should look like. 640x400 in 256 colours is
# what the client-side DIB actually is, so the prompt asks for art that
# is honest at that resolution rather than a downsampled oil painting.
VGA_STYLE = (
    "256-color VGA pixel art from a 1993 MS-DOS adventure game, "
    "hand-dithered gradients, visible pixels, painted backdrop in the "
    "style of Sierra and LucasArts VGA, no text, no UI, no border. "
    "Scene: ")

WIN16 = ClientProfile(
    name='win16',
    # Superseded by whatever CLIENT_HELLO reports: the pane is resizable,
    # so its width is a runtime fact and not a property of the machine.
    text_width=80,
    # A socket, not a 2400-baud modem behind a bridge that drops burst
    # tails. Still bounded by the client's own buffer, which HELLO gives.
    max_payload=2048,
    pace=False,
    marker_cells=False,
    rich_text=True,
    music_fmt=None,     # 'midi' once there is a MIDI pipeline to serve
    image_style=VGA_STYLE,
    palette=PALETTE_RICH,
)

PROFILES = {'c64': C64, 'win16': WIN16}


def from_hello(payload: bytes):
    """Parse a CLIENT_HELLO payload -> (ClientProfile, caps) or None.

    Layout, all little-endian:

        0     hello version (1)
        1     text width in columns, 0 = unknown
        2-3   max payload the client's frame buffer can hold
        4-5   capability bits
        6     profile name length
        7..   profile name, ASCII

    Trailing bytes are ignored so a later client can say more without
    breaking this one. Anything malformed returns None and the caller
    keeps the C64 default: a client that cannot introduce itself is
    indistinguishable from one that never tried.
    """
    if len(payload) < 7:
        return None
    version = payload[0]
    if version != 1:
        return None

    width = payload[1]
    max_payload = payload[2] | (payload[3] << 8)
    caps = payload[4] | (payload[5] << 8)
    n = payload[6]
    if len(payload) < 7 + n:
        return None
    name = bytes(payload[7:7 + n]).decode('ascii', 'replace').lower()

    base = PROFILES.get(name)
    if base is None:
        # An unknown client is not an error - it is a machine this proxy
        # predates. Serve it the conservative profile and let its own
        # capability bits and limits do the talking. Except music: SID
        # bytes are garbage to anything that is not a C64, and a machine
        # introducing itself by name is not a C64.
        base = ClientProfile(name=name or 'unknown', music_fmt=None)

    rich = bool(caps & CAP_RICH_TEXT)

    # The client's own numbers win over the table: it knows its buffer
    # and its window, and the table is only a default for what it omits.
    return ClientProfile(
        name=base.name,
        text_width=width if width else base.text_width,
        max_payload=max_payload if max_payload else base.max_payload,
        pace=base.pace,
        marker_cells=not (caps & CAP_ZERO_WIDTH_MARKERS),
        rich_text=rich,
        # Capability-gated for the same reason as the palette below: a
        # client without the fmt=2 parser must never be sent one.
        dib_images=bool(caps & CAP_DIB_IMAGES),
        music_fmt=base.music_fmt,
        image_style=base.image_style,
        # The palette follows the CAPABILITY, never the table entry. A
        # win16 build that predates rich text still matches the win16
        # row, and handing it the rich palette would let a [color=teal]
        # encode to an extended marker it has no parser for - which is
        # the whole reason the bit is on the wire. Tie the vocabulary to
        # what the client said it can read.
        palette=PALETTE_RICH if rich else PALETTE_C64,
    ), caps
