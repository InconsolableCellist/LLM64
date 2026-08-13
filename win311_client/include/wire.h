/*
 * LLM64 for Windows - wire protocol
 *
 * The same framing the C64 speaks (docs/01, docs/16 section 2), and
 * deliberately free of any Windows dependency: this file builds under
 * the 16-bit Watcom compiler, under mingw for the Win32 build, and
 * under host gcc for the unit test. Everything platform-shaped lives
 * in net.c.
 *
 *   SYNC(0x42) TYPE LEN_LO+0x20 LEN_HI+0x20 PAYLOAD... CRC
 *
 * The +0x20 bias on both length bytes is not decoration: it exists so
 * a frame header cannot contain NUL or a Telnet IAC, which the C64's
 * modem bridges used to eat. It wraps modulo 256, so the decoder must
 * subtract in 8 bits.
 */

#ifndef WIRE_H
#define WIRE_H

#define WIRE_SYNC        0x42
#define WIRE_MAX_PAYLOAD 2048

/* Client -> proxy */
#define MSG_CHAT_REQUEST        0x31
#define MSG_CANCEL_REQUEST      0x32
#define MSG_LIST_CONVERSATIONS  0x33
#define MSG_LOAD_CONVERSATION   0x34
#define MSG_NEW_CONVERSATION    0x35
#define MSG_PING                0x36
#define MSG_LIST_MODELS         0x37
#define MSG_SET_MODEL           0x38
#define MSG_DELETE_CONVERSATION 0x39
#define MSG_STAR_CONVERSATION   0x3A
#define MSG_GET_MENU            0x3B
#define MSG_GET_NOWPLAYING      0x3C
#define MSG_FAV_TUNE            0x3D
#define MSG_SET_BAUD            0x3E
#define MSG_CLIENT_HELLO        0x3F
#define MSG_ACK                 0x40
#define MSG_NAK                 0x41
/* [opt][value], fire-and-forget like SET_BAUD. Session toggles the
   proxy has no config lever for because they are the PLAYER's call -
   room_pics may cost real API money per picture. */
#define MSG_SET_OPTION          0x43
#define OPT_ROOM_PICS           1

/* Proxy -> client */
#define MSG_CHAT_CHUNK          0x50
#define MSG_CHAT_DONE           0x51
#define MSG_CHAT_ERROR          0x52
#define MSG_CONVERSATION_LIST   0x53
#define MSG_CONVERSATION_DATA   0x54
#define MSG_STATUS              0x55
#define MSG_MODEL_LIST          0x56
#define MSG_SID_BEGIN           0x57
#define MSG_SID_DATA            0x58
#define MSG_SID_END             0x59
#define MSG_IMG_BEGIN           0x5A
#define MSG_IMG_DATA            0x5B
#define MSG_IMG_END             0x5C
#define MSG_HINT                0x5D
#define MSG_MENU_LIST           0x5E
#define MSG_NOWPLAYING          0x5F
#define MSG_NOTICE              0x60
#define MSG_MUSIC_STOP          0x61
#define MSG_PRINT_BEGIN         0x62
#define MSG_PRINT_DATA          0x63
#define MSG_PRINT_END           0x64

/* MIDI music, sent only to a client that claimed CAP_MIDI. The file
   ships whole - the client hands it to MCI's sequencer, and synthesis
   is the machine's business (FM, MT-32, a SoundFont under emulation).

   MIDI_BEGIN:  0     flow window, data frames per ACK
                1-4   total length in bytes, LE
                5..   title\0 author\0 mood\0   (the controls' display)
   MIDI_DATA:   [offset:4 LE][bytes] - 4-byte offsets like IMG fmt=2,
                because a .MID can pass 64 KB
   MIDI_END:    play it */
#define MSG_MIDI_BEGIN          0x65
#define MSG_MIDI_DATA           0x66
#define MSG_MIDI_END            0x67

/* The adventure's normalized [[STATE:]] block, verbatim + NUL, sent
   only to a client that claimed CAP_STATE_JSON. Compact json.dumps
   output by contract - double-quoted keys, no whitespace - which is
   what lets a 16-bit client scan it without a real JSON parser. The
   Character and Inventory windows render from it; an empty object
   clears them (new conversation). */
#define MSG_STATE_JSON          0x68

/* The conversation's picture roster, newest first: [count] then
   [n][title\0] per entry, n being the /pic <n> index that re-fetches
   it. Sent on load and new-conversation; an empty roster clears the
   browser. The client lists them as "ghosts" - titles without bytes -
   and a click on one asks the server for the real thing. */
#define MSG_PIC_LIST            0x69

/* The mood vocabulary of the library this machine plays from:
   [count] then [mood\0] per entry. Fills the Music window's picker. */
#define MSG_MOOD_LIST           0x6A

/* The STATIC half of the character sheet - name, race, class, ability
   scores, skills, spells, starting gear. The proxy rolls these once at
   the start of an adventure and owns them for its whole length; the
   narrator owns only what changes (MSG_STATE_JSON). Compact JSON + NUL,
   depth 1 by contract - strings, numbers, and arrays of strings, so the
   same flat scanner reads both sheets. Empty object = no adventure.
   Sent only to a client that claimed CAP_CHAR_SHEET. */
#define MSG_CHAR_SHEET          0x6B

/* The adventure map as structure rather than as ASCII art, for a client
   that claimed CAP_MAP_DATA. Tab-separated lines + NUL:

       M<turn>\t<cols>\t<rows>
       R<num>\t<gx>\t<gy>\t<flags>\t<name>     flags: 1 visited, 2 you
       E<a>\t<b>\t<dir>\t<flags>               dir n s e w u d or -
       X<hidden>                               rooms that did not fit

   Grid coordinates, not pixels: the proxy's layout pass decides the
   geography, the client decides how big a room is on screen. */
#define MSG_MAP_DATA            0x6C

/* Send me the sheets again - character, state and map - out of what the
   proxy already has stored. No payload, no reply guarantee, and by
   contract never an LLM call: it is a refresh, not a question. */
#define MSG_GET_SHEET           0x44

/* Not wire values: parser verdicts outside the protocol's type range */
#define WIRE_NONE               0x00
#define WIRE_CRC_FAIL           0xFE

/* In-band marker cells carried inside CHAT_CHUNK text (docs/08).
   Rendering them is the text pane's job; the parser passes them
   through untouched. */
#define MARK_CLOSE       0x01
#define MARK_BOLD_ON     0x02
#define MARK_BOLD_OFF    0x03
#define MARK_COLOR_BASE  0x10   /* 0x10|c, c = 1..15 */

/* Rich text: only ever sent to a client that asked for it in
   CLIENT_HELLO (see below). A C64 has one face, so these do not exist
   on the wire to it - the proxy strips the tags instead. */
#define MARK_ITALIC_ON   0x04
#define MARK_ITALIC_OFF  0x05
#define MARK_ULINE_ON    0x06
#define MARK_ULINE_OFF   0x07
#define MARK_HEAD_ON     0x0E
#define MARK_HEAD_OFF    0x0F

/* Color past the fifteen the one-byte marker can hold:

       0x1B 'C' (0x40 | slot)        slot 0..63

   Three bytes, and the operand is biased into 0x40-0x7F so it can never
   be mistaken for a NUL, a newline, or another marker - which is what
   lets the scanner resynchronise on any byte in the stream. */
#define MARK_ESC         0x1B
#define MARK_ESC_COLOR   0x43   /* 'C' */
#define MARK_ESC_BIAS    0x40
#define MARK_ESC_LEN     3

/* CLIENT_HELLO payload (llm64_proxy/src/profiles.py), all little-endian:

       0     hello version (1)
       1     text width in columns, 0 = unknown
       2-3   the largest payload our frame buffer can hold
       4-5   capability bits
       6     profile name length
       7..   profile name, ASCII

   Capability bits are a WIRE CONTRACT: never renumber one, because a
   proxy built later still has to read a client built today. Announce
   only what this build can actually RENDER - claiming rich text before
   the painter handles it makes the proxy send markers that would print
   as literal characters. */
#define HELLO_VERSION           1
#define CAP_ZERO_WIDTH_MARKERS  0x0001  /* markers occupy no cell */
#define CAP_RICH_TEXT           0x0002  /* italic/underline/head, 64 colors */
#define CAP_DIB_IMAGES          0x0004  /* images as 8-bit DIBs, fmt 2 */
#define CAP_MIDI                0x0008  /* music as .MID files */
#define CAP_STATE_JSON          0x0020  /* STATE forwarded for sheets */
/* 0x0010 is reserved on the proxy side for a printer-DC sink; it is not
   ours to reuse. */
#define CAP_CHAR_SHEET          0x0040  /* the static half of the sheet */
#define CAP_MAP_DATA            0x0080  /* the map as structure, not art */

/* IMG_BEGIN's first payload byte says what is coming. 0 and 1 are the
   C64's hires and multicolor blobs; 2 is ours, sent only to a client
   that claimed CAP_DIB_IMAGES:

       0     2
       1     flow window, in data frames per ACK
       2     keep_music flag
       3-4   pixel width, LE
       5-6   pixel height, LE
       7-10  total DIB length in bytes, LE
       11..  title, NUL-terminated

   What follows in IMG_DATA is a packed 8-bit DIB - BITMAPINFOHEADER,
   256 RGBQUADs, bottom-up rows - the thing StretchDIBits eats whole.
   fmt=2 data frames tag their offset with FOUR bytes, not the two the
   C64 formats use: a quarter-megabyte DIB laps a 16-bit offset. */
#define IMG_FMT_HIRES    0
#define IMG_FMT_MC       1
#define IMG_FMT_DIB8     2
#define IMG_DIB_HDR      11     /* fixed bytes before the title */

enum wire_state {
    WS_SYNC = 0,
    WS_TYPE,
    WS_LEN,
    WS_PAYLOAD,
    WS_CRC
};

typedef struct {
    int            state;
    unsigned char  type;
    unsigned int   len;
    unsigned int   got;
    unsigned char  lenbuf[2];
    unsigned char *payload;   /* caller-owned, cap bytes */
    unsigned int   cap;
    unsigned long  frames;    /* accepted frames, for diagnostics */
    unsigned long  crc_fails;
} WireRx;

void wire_rx_init(WireRx *rx, unsigned char *buf, unsigned int cap);

/* Feed one byte. Returns the message type once a whole frame has
   arrived and checked out, WIRE_CRC_FAIL if it did not, WIRE_NONE
   otherwise. On a returned type, rx->payload holds rx->len bytes. */
unsigned char wire_rx_byte(WireRx *rx, unsigned char b);

/* Nonzero if the parser is part-way through a frame. A stall here is
   how the C64 detects a wedged link; kept for the same reason. */
int wire_rx_mid_frame(const WireRx *rx);

/* Build a frame into out, which must hold len + 5 bytes. Returns the
   number of bytes written. */
unsigned int wire_frame(unsigned char *out, unsigned char type,
                        const unsigned char *payload, unsigned int len);

#endif /* WIRE_H */
