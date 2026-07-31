/*
 * LLM64 for Windows - the transcript
 *
 * The Phase 0 spike kept the transcript as a 200 x 160 array of
 * already-wrapped lines in the default data segment. That is 32 KB of
 * the 64 KB DGROUP, and because the wrapping happened as text was
 * appended, resizing the window could not re-flow anything already on
 * screen. Both problems have the same fix, and it is this file:
 *
 *   - logical lines are stored *unwrapped*, in far blocks off the heap,
 *     so DGROUP holds only the bookkeeping;
 *   - wrapping happens at paint time, through one iterator, so a
 *     re-flow is just a paint at a different width.
 *
 * There is no Windows in here. Under the 16-bit large memory model
 * every pointer is already far and malloc() draws on the far heap - the
 * same global memory GlobalAlloc hands out - so the same source compiles
 * for the host and `make test` can exercise the wrapping without Wine,
 * an emulator or a proxy anywhere in the loop.
 *
 * Text is stored with the proxy's in-band markers still in it (docs/08:
 * 0x01 close, 0x02/0x03 bold, 0x10|c colour, and for a rich-text client
 * the italic/underline/heading markers and the three-byte extended
 * colour). They occupy no screen cell, which is precisely why wrapping
 * has to be marker-aware and the spike's byte-counting wrap was subtly
 * short.
 *
 * A marker is not always ONE byte: the extended colour marker is three.
 * Scan with sb_marker_len() rather than testing bytes, or the operand
 * bytes get counted as glyphs and every line carrying one wraps short.
 */

#ifndef SCROLL_H
#define SCROLL_H

/* 8 blocks of 8 KB is 64 KB of transcript, about a thousand lines of
   prose, and it is all outside DGROUP. Raising these costs global
   memory and nothing else. */
#define SB_BLOCK_SIZE   8192
#define SB_MAX_BLOCKS   8
#define SB_MAX_LINES    512

/* The longest logical line kept whole, and the one place the design
   shows a seam: text past this is committed and continued on a fresh
   logical line, which after wrapping reads as one short row in the
   middle of a paragraph. The break is taken at a space so it never
   lands mid-word. The proxy sends real newlines between paragraphs, so
   only a single unbroken 2 KB block of prose can reach it. This buffer
   is the one part of the transcript still in DGROUP; it is 2 KB of the
   32 KB the fixed array used to hold. */
#define SB_MAX_LINE     2048

/* How far back the seam looks for a space to break on. */
#define SB_CARRY        80

/* Who a line came from, for the painter's per-role background band.
   Client-local: nothing about this reaches the wire. */
#define SB_WHO_OTHER  0     /* the model, and everything else */
#define SB_WHO_USER   1     /* the player's own line, echoed */

typedef struct {
    unsigned      block;    /* arena block holding the text */
    unsigned      off;      /* byte offset within that block */
    unsigned      len;      /* bytes, markers included */
    unsigned      rows;     /* display rows at the current width */
    unsigned char color;    /* colour in force at the first cell */
    unsigned char who;      /* SB_WHO_*, stamped at commit */
} SbLine;

typedef struct {
    char     *blocks[SB_MAX_BLOCKS];
    unsigned  nblocks;      /* blocks actually allocated so far */
    unsigned  cur_block;    /* block being filled */
    unsigned  cur_off;      /* fill point within it */

    SbLine   *lines;        /* ring of SB_MAX_LINES committed lines */
    unsigned  head;         /* ring slot of the oldest */
    unsigned  count;        /* committed lines held */

    /* The line being appended to. It lives here rather than in the
       arena because it grows a character at a time, and the arena is
       append-only. It is committed on a newline. */
    char      open[SB_MAX_LINE];
    unsigned  open_len;
    unsigned char open_color;   /* colour at the open line's first cell */

    /* Where the open line's final display row starts, so appending a
       character re-wraps one row rather than the whole line. */
    unsigned      open_tail;
    unsigned      open_rows;    /* rows before open_tail */
    unsigned char open_tail_color;

    unsigned char color;        /* colour for text appended from now on */
    unsigned char origin;       /* SB_WHO_* for lines committed from now on */
    unsigned      cols;         /* display width, in cells */
    unsigned long total_rows;   /* committed rows; the open line is extra */
} Scrollback;

/* Colour slots. The one-byte marker carries 1..15; the extended marker
   reaches 63, so a painter must mask with this and not with 0x0F. */
#define SB_COLOR_MASK   0x3F

/* Character attributes, as a bitmask rather than a field each: the wrap
   iterator carries this state across every row of a line and through
   every re-flow, and one byte to copy stays one byte however many
   attributes the proxy learns to send.

   The values are 1/2/4 so that the bold/italic/underline bits ARE an
   index into the font table - see attr_font in main.c. */
#define SB_ATTR_BOLD    0x01
#define SB_ATTR_ITALIC  0x02
#define SB_ATTR_ULINE   0x04
#define SB_ATTR_HEAD    0x08

/* One display row: a slice of a logical line, plus the rendering state
   in force at its first cell. text points into the arena or the open
   buffer and is valid until the next append. */
typedef struct {
    const char   *text;
    unsigned      len;
    unsigned char color;    /* colour here */
    unsigned char base;     /* the logical line's colour, for MARK_CLOSE */
    unsigned char attr;     /* SB_ATTR_* in force at the first cell */
    unsigned char who;      /* SB_WHO_* of the logical line (sb_view_next
                               fills it; a bare sb_wrap_next does not) */
} SbRow;

/* Wrap iterator over one logical line. The single implementation of the
   wrapping rules: counting rows and finding row N are the same walk. */
typedef struct {
    const char   *text;
    unsigned      len;
    unsigned      cols;
    unsigned      pos;
    unsigned char color;
    unsigned char base;
    unsigned char attr;
    int           done;
} SbWrap;

/* A cursor over display rows, so painting a screenful is one seek and
   then one step per row rather than a seek per row. */
typedef struct {
    const Scrollback *sb;
    unsigned          slot;     /* ring slot of the current logical line */
    unsigned          left;     /* committed lines still ahead */
    SbWrap            w;
    unsigned char     who;      /* SB_WHO_* of the current logical line */
    int               in_open;  /* the open line is the last one */
    int               done;
} SbView;

int      sb_init(Scrollback *sb);
void     sb_free(Scrollback *sb);
void     sb_clear(Scrollback *sb);

/* Colour for text appended from here on. Applied to the open line
   itself when nothing has been written to it yet - otherwise the first
   chunk of a reply inherits the colour of whatever preceded it. */
void     sb_color(Scrollback *sb, unsigned char color);

/* Who the lines committed from here on belong to (SB_WHO_*). The
   painter draws the user's own lines on a faintly different ground. */
void     sb_origin(Scrollback *sb, unsigned char who);

void     sb_putc(Scrollback *sb, char c);   /* '\n' closes the line */
void     sb_puts(Scrollback *sb, const char *s);
void     sb_newline(Scrollback *sb);

/* A whole line in one colour, closed at both ends: the shape almost
   every status or error message wants. */
void     sb_say(Scrollback *sb, unsigned char color, const char *s);

/* Set the display width and re-flow. Cheap enough to call from WM_SIZE:
   it re-walks the retained text once. */
void     sb_width(Scrollback *sb, unsigned cols);

/* Display rows in the whole transcript, the open line included. */
unsigned long sb_rows(const Scrollback *sb);

/* Logical lines retained, the open one included - for tests and for the
   eviction accounting. */
unsigned sb_lines(const Scrollback *sb);

/* Position a cursor at a display row. Returns 0 if the row is past the
   end. */
int      sb_view(const Scrollback *sb, unsigned long row, SbView *v);
int      sb_view_next(SbView *v, SbRow *out);

/* Exposed for the tests: wrap one logical line by hand. */
void     sb_wrap_begin(SbWrap *w, const char *text, unsigned len,
                       unsigned cols, unsigned char base);
int      sb_wrap_next(SbWrap *w, SbRow *out);

/* Nonzero if this byte STARTS an in-band marker and so occupies no
   cell. Kept because most markers are one byte and a caller that only
   asks "is this a glyph?" reads better for it - but anything WALKING the
   text has to use sb_marker_len, or it will treat the two operand bytes
   of an extended colour marker as text. */
int      sb_is_marker(unsigned char c);

/* Fold the marker at p into the running render state. THE painter and
   THE wrapper both call this, and that is the point: a row painted with
   different state than it was wrapped with is a row whose colours creep
   as the window resizes. One state machine, two callers. */
void     sb_mark_apply(const char *p, unsigned len, unsigned char base,
                       unsigned char *color, unsigned char *attr);

/* Length in bytes of the marker starting at p, or 0 if p is a glyph.
   `remaining` bounds the read: a marker truncated by the end of the line
   is not a marker, because rows are slices of an arena block and reading
   past one is a fault in protected mode, not a stray character. */
unsigned sb_marker_len(const char *p, unsigned remaining);

#endif /* SCROLL_H */
