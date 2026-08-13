/**
 * LLM64 Client - TUI display implementation
 *
 * The chat scrollback stores pre-wrapped TEXT_COLS-wide lines of "cells".
 * A cell is a screen code in the 40-column build, or ASCII in the SOFT80
 * bitmap build; bit 7 always means reverse video. All rendering funnels
 * through ui_blit_row(), which pokes screen RAM (40) or calls the
 * bitmap blitter (80).
 */

#include <string.h>
#include <c64.h>
#include "ui.h"
#include "text.h"
#include "buildhash.h"
#ifdef SOFT80
#include "soft80.h"
#endif

#define SCREEN ((uint8_t*)0x0400)
#define COLORS ((uint8_t*)0xD800)

#ifdef SOFT80
/* 121 (not 160): ~3KB of BSS traded for the $B000 SID window + image
   + diagnostics + resumable-transfer code - see c64-soft80.cfg (the
   hayes build is the tight one; CODE growth eats BSS 1:1 there). Deep
   reading has /history now; reclaiming this rent is the overlay/module
   system's job. */
#define MAX_LINES 121
#else
#define MAX_LINES 160
#endif

/* Committed, pre-wrapped lines (cells, space padded) */
static uint8_t line_text[MAX_LINES][TEXT_COLS];
static uint8_t line_color[MAX_LINES];
static uint8_t line_head;    /* ring index of next write */
static uint8_t line_count;   /* committed lines (caps at MAX_LINES) */

/* Line under construction */
static uint8_t cur[TEXT_COLS];
static uint8_t cur_len;
static uint8_t cur_color;

/* Pending word for wrapping. Non-static under SOFT80: the asm fast path
   in append.s writes wbuf/wlen directly for the common printable char. */
#ifdef SOFT80
uint8_t wbuf[TEXT_COLS];
uint8_t wlen;
#else
static uint8_t wbuf[TEXT_COLS];
static uint8_t wlen;
#endif

#ifdef SOFT80
uint8_t view_scroll;         /* 0 = pinned to bottom; append.s clears it */
#else
static uint8_t view_scroll;  /* 0 = pinned to bottom */
#endif
static uint8_t frozen;       /* suppress rendering (bulk conversation load) */
static uint8_t lines_dirty;  /* a line committed since last full redraw */
static uint8_t commits_pending;  /* commits since last full draw (scroll opt) */
static int16_t stream_drawn_total;  /* total lines at last stream draw */
static uint8_t stream_partial_end;  /* drawn length of the partial line */

static uint8_t rowbuf[TEXT_COLS];

/* Role colors. Role 3 = attention line (pic-ready notice): rainbow
   in soft-80 (0xFF = per-cell color-cycle sentinel, see
   chat_row_blit), plain yellow in 40 columns. */
#ifdef SOFT80
#define COLOR_ATTENTION 0xFF
#else
#define COLOR_ATTENTION COLOR_YELLOW
#endif
static const uint8_t role_colors[4] = {
    COLOR_CYAN,        /* user */
    COLOR_LIGHTGREEN,  /* assistant */
    COLOR_GRAY2,       /* system */
    COLOR_ATTENTION    /* attention */
};

/* --- cell encoding -------------------------------------------------- */

static uint8_t cell_from_ascii(uint8_t c) {
#ifdef SOFT80
    /* 0x7F included: it carries the music note (tools/make_font.py).
       It was excluded only because that slot held the fallback block. */
    if (c >= 0x20 && c <= 0x7F) return c;
    return 0x3F;  /* '?' */
#else
    return ascii_to_screen(c);
#endif
}

static uint8_t cell_from_petscii(uint8_t c) {
    return cell_from_ascii(petscii_to_ascii(c));
}

uint8_t ui_cell_from_petscii(uint8_t c) {
    return cell_from_petscii(c);
}

/* Inline color markup (docs/08-inline-color.md). Marker cells travel
   INSIDE the text: the soft-80 renderer draws anything below 0x20 as a
   space (soft80.s clamps an underflowing font index to glyph 0), so a
   marker costs the column the proxy swallowed a space from. Nothing
   downstream needs to know - wrap, scrollback and reload treat them as
   ordinary cells. */
#define MK_CLOSE     0x01
#define MK_BOLD_ON   0x02
#define MK_BOLD_OFF  0x03
#define MK_COLOR_LO  0x11      /* 0x10|c, c = 1..14 */
#define MK_COLOR_HI  0x1E

/* --- low-level row output ------------------------------------------- */

/* Hard freeze: while a fullscreen image owns the bitmap, every draw
   primitive becomes a no-op so stray status/editor updates can't paint
   over it. Cleared by the image dismiss, which redraws everything. */
uint8_t ui_frozen;

void ui_blit_row(uint8_t row, const uint8_t* cells, uint8_t color) {
    if (ui_frozen) return;
#ifdef SOFT80
    if (color == 0xFF) {
        /* Attention line: glyphs in one pass, then the color matrix
           rewritten as a classic rainbow ramp (one hue per 8x8 cell,
           i.e. per 2 chars - the soft-80 color granularity) */
        static const uint8_t ramp[8] = {
            COLOR_RED, COLOR_ORANGE, COLOR_YELLOW, COLOR_GREEN,
            COLOR_CYAN, COLOR_LIGHTBLUE, COLOR_PURPLE, COLOR_LIGHTRED
        };
        uint8_t* mat = (uint8_t*)(0xCC00 + (uint16_t)row * 40);
        uint8_t i;
        soft80_row(row, cells, COLOR_WHITE);
        for (i = 0; i < 40; ++i) mat[i] = ramp[i & 7] << 4;
        return;
    }
    soft80_row(row, cells, color);
#else
    memcpy(SCREEN + (uint16_t)row * 40, cells, 40);
    memset(COLORS + (uint16_t)row * 40, color, 40);
#endif
}

#ifdef SOFT80
/* Per-PAIR color for one chat row. The C64's matrix is one entry per
   8x8 cell = two soft-80 glyphs, so a pair takes the run color when it
   holds a run glyph; the proxy's space-swallow means the only cells that
   can be caught by that granularity are the marker-spaces themselves.
   Markers are rewritten to spaces in place - rowbuf is scratch that
   every caller rebuilds, and soft80.s then sees pure ASCII. */
/* Non-static: the asm colorize_row (colorize.s) fills it. */
uint8_t matbuf[40];

/* The 80-cell scan + 40-byte fill, per colored chat row, lives in
   colorize.s as a bit-exact port of the old C loop (marker cells -> run
   color + rewrite to space; matbuf[i>>1] holds the run). */
uint8_t __fastcall__ colorize_row(uint8_t* buf, uint8_t carry, uint8_t base);

/* Chat rows only: chrome (status, title, editor) never carries markers
   and must not pay for the scan. `color` is the encoded line_color -
   carry in the high nibble, base in the low. */
static void chat_row_blit(uint8_t row, uint8_t* cells, uint8_t color) {
    uint8_t base, carry;
    if (ui_frozen) return;
    if (color == 0xFF) {          /* attention line: rainbow, no runs */
        ui_blit_row(row, cells, color);
        return;
    }
    base = color & 0x0F;
    carry = color >> 4;
    if (!carry && !colorize_row(cells, 0, base)) {
        ui_blit_row(row, cells, base);
        return;
    }
    if (carry) colorize_row(cells, carry, base);
    soft80_row(row, cells, base);
    {
        uint8_t* mat = (uint8_t*)(0xCC00 + (uint16_t)row * 40);
        uint8_t i;
        for (i = 0; i < 40; ++i) mat[i] = matbuf[i] << 4;
    }
}
#else
#define chat_row_blit(row, cells, color) ui_blit_row((row), (cells), (color))
#endif

/* Repaint only cells [first, first+count) of a row whose color hasn't
   changed. Much cheaper than a full row in SOFT80 (~0.2ms/cell). */
void ui_blit_span(uint8_t row, const uint8_t* cells, uint8_t first,
                  uint8_t count) {
    if (ui_frozen) return;
    if (count == 0) return;
#ifdef SOFT80
    {
        uint8_t fp = first >> 1;
        uint8_t lp = (uint8_t)(first + count - 1) >> 1;
        soft80_span(row, cells, fp, lp - fp + 1);
    }
#else
    memcpy(SCREEN + (uint16_t)row * 40 + first, cells + first, count);
#endif
}

/* --- chat ring ------------------------------------------------------ */

/* Color-run state for the inline markup (defined with the append path
   below; declared here because commit_line records the carry). */
static uint8_t run_color, run_at_line_start;

static void cur_reset(void) {
    memset(cur, 0x20, TEXT_COLS);
    cur_len = 0;
}

static void commit_line(void) {
    memcpy(line_text[line_head], cur, TEXT_COLS);
    /* Wrap carry (docs/08-inline-color.md §4): the color run in force
       when this line STARTED rides the spare high nibble, so a run that
       crosses the break resumes without the proxy re-emitting a marker.
       Costs no RAM. The rainbow sentinel is stored whole and never
       carries - which is why the palette forbids color 15, so an
       encoded byte can never be mistaken for it. */
#ifdef SOFT80
    line_color[line_head] = (cur_color == 0xFF)
        ? 0xFF
        : (uint8_t)((run_at_line_start << 4) | cur_color);
    run_at_line_start = run_color;
#else
    line_color[line_head] = cur_color;   /* no runs in 40 columns */
#endif
    ++line_head;
    if (line_head >= MAX_LINES) line_head = 0;
    if (line_count < MAX_LINES) ++line_count;
    lines_dirty = 1;
    if (commits_pending < 255) ++commits_pending;
    cur_reset();
}

/* Move the pending word into the current line, wrapping if needed */
static void flush_word(void) {
    if (wlen == 0) return;
    if (cur_len + wlen > TEXT_COLS) {
        commit_line();
    }
    memcpy(cur + cur_len, wbuf, wlen);
    cur_len += wlen;
    wlen = 0;
    if (cur_len >= TEXT_COLS) {
        commit_line();
    }
}

void chat_start(uint8_t role) {
    flush_word();
    if (cur_len) commit_line();
    cur_color = role_colors[role > 3 ? 2 : role];
    view_scroll = 0;
}

/* Recolor the lines that follow. Closes the current line at the old
   color first, so each call cleanly begins a new run of same-colored
   lines within one chat block. Works in both builds: the 40-col path
   stores cur_color per line, the soft-80 path encodes it as the base. */
void chat_color(uint8_t color) {
    flush_word();
    if (cur_len) commit_line();
    cur_color = color;
}

/* Reverse video is per-CHARACTER (cell bit 7) with no pair-granularity
   limit, so bold is consumed at append time and stored in the cell
   itself - zero columns, zero extra RAM. Non-static under SOFT80 so the
   asm fast path can OR it into the stored cell. */
#ifdef SOFT80
uint8_t rev_on;
#else
static uint8_t rev_on;
#endif

void chat_append_ascii_char(uint8_t c) {
    if (c == 0x0D) return;
    if (c == 0x0A) {
        flush_word();
        commit_line();
        return;
    }
    if (c == MK_BOLD_ON || c == MK_BOLD_OFF) {
        rev_on = (c == MK_BOLD_ON) ? 0x80 : 0x00;
        return;
    }
    if (c == MK_CLOSE) {
        /* Glue the close marker to the word it ends, then break: the
           marker replaced the space that followed the run. */
#ifdef SOFT80
        if (wlen < TEXT_COLS) wbuf[wlen++] = MK_CLOSE;
#endif
        flush_word();
        run_color = 0;
        return;
    }
    if (c >= MK_COLOR_LO && c <= MK_COLOR_HI) {
        /* The marker replaced the space BEFORE the run, so it breaks the
           word like that space did, then leads the next one.
           40 columns consumes markers without storing them: a cell there
           is a screen code, so 0x01-0x1E would print as letters. The
           hardware could color per character, but that build paints one
           color per row and adventure ships on soft-80. */
        flush_word();
        run_color = c & 0x0F;
#ifdef SOFT80
        wbuf[wlen++] = c;
#endif
        return;
    }
    if (c == 0x20) {
        flush_word();
        if (cur_len < TEXT_COLS && cur_len > 0) {
            cur[cur_len++] = 0x20;
        }
        return;
    }
    wbuf[wlen++] = cell_from_ascii(c) | rev_on;
    if (wlen >= TEXT_COLS) {
        /* Word longer than a line: hard wrap */
        flush_word();
    }
}

#ifndef SOFT80
void chat_append_ascii(const char* s) {
    while (*s) {
        chat_append_ascii_char((uint8_t)*s);
        ++s;
    }
    view_scroll = 0;
}
#else
/* SOFT80: the hot loop is in append.s. It inlines the common case
   (printable, non-space, word not full -> wbuf[wlen++] = c | rev_on)
   and calls chat_append_ascii_char for every special byte (space, CR,
   LF, the 0x01-0x1E markers) and for the word-full hard wrap, so the
   state machine stays in one place. Declared in the header. */
#endif

void chat_append_petscii(const char* s) {
    while (*s) {
        chat_append_ascii_char(petscii_to_ascii((uint8_t)*s));
        ++s;
    }
    view_scroll = 0;
}

void chat_finish(void) {
    flush_word();
    if (cur_len) commit_line();
    commit_line();  /* blank separator line */
    view_scroll = 0;
    if (!frozen) chat_redraw();
}

/* Freeze/unfreeze rendering: bulk loads append everything with rendering
   off, then unfreeze draws the final (bottom) view once - loading a long
   conversation jumps straight to its end instead of painting each line. */
void chat_freeze(uint8_t on) {
    frozen = on;
    if (!on) chat_redraw();
}

void chat_clear(void) {
    line_head = 0;
    line_count = 0;
    wlen = 0;
    view_scroll = 0;
    cur_reset();
    chat_redraw();
}

void chat_scroll(int8_t lines_up) {
    int16_t total = line_count + (cur_len || wlen ? 1 : 0);
    int16_t max_scroll = total - CHAT_HEIGHT;
    int16_t v = (int16_t)view_scroll + lines_up;
    if (max_scroll < 0) max_scroll = 0;
    if (v < 0) v = 0;
    if (v > max_scroll) v = max_scroll;
    view_scroll = (uint8_t)v;
    chat_redraw();
}

/* Build the cells + color of visible chat row r (0..CHAT_HEIGHT-1) */
static uint8_t build_view_row(uint8_t r) {
    uint8_t partial = (cur_len || wlen) ? 1 : 0;
    int16_t total = line_count + partial;
    int16_t idx = total - CHAT_HEIGHT - (int16_t)view_scroll + r;

    if (idx < 0 || idx >= total) {
        memset(rowbuf, 0x20, TEXT_COLS);
        return COLOR_BLACK;
    }
    if (idx == line_count) {
        memcpy(rowbuf, cur, TEXT_COLS);
        if (wlen && cur_len + wlen <= TEXT_COLS) {
            memcpy(rowbuf + cur_len, wbuf, wlen);
        }
        return cur_color;
    }
    {
        uint8_t phys = (uint8_t)((line_head + MAX_LINES - line_count
                                  + (uint8_t)idx) % MAX_LINES);
        memcpy(rowbuf, line_text[phys], TEXT_COLS);
        return line_color[phys];
    }
}

/* "NN%" (or "     " when everything fits) at the right end of the title
   bar: how far down the conversation the current view reaches. */
static void draw_scroll_pct(void) {
    uint8_t cells[5];
    uint16_t total = line_count + ((cur_len || wlen) ? 1 : 0);
    uint8_t i;

    for (i = 0; i < 5; ++i) cells[i] = 0x20 | 0x80;
    if (total > CHAT_HEIGHT) {
        uint16_t bottom = total - view_scroll;
        uint16_t pct = (bottom * 100) / total;
        cells[4] = cell_from_ascii(0x25) | 0x80;  /* % */
        cells[3] = cell_from_ascii(0x30 + pct % 10) | 0x80;
        pct /= 10;
        if (pct) {
            cells[2] = cell_from_ascii(0x30 + pct % 10) | 0x80;
            pct /= 10;
            if (pct) cells[1] = cell_from_ascii(0x30 + pct) | 0x80;
        }
    }
    {
        static uint8_t full[TEXT_COLS];
        memset(full, 0x20 | 0x80, TEXT_COLS);  /* span rounds to pairs */
        memcpy(full + TEXT_COLS - 5, cells, 5);
        ui_blit_span(0, full, TEXT_COLS - 5, 5);
    }
}

void chat_redraw(void) {
    uint8_t r;
    uint8_t color;

    if (frozen) return;
    for (r = 0; r < CHAT_HEIGHT; ++r) {
        color = build_view_row(r);
        /* Scrolled into history: reverse "v" marker, bottom-right */
        if (view_scroll && r == CHAT_HEIGHT - 1) {
            rowbuf[TEXT_COLS - 3] = 0x20 | 0x80;
            rowbuf[TEXT_COLS - 2] = cell_from_ascii(0x76) | 0x80;  /* v */
            rowbuf[TEXT_COLS - 1] = 0x20 | 0x80;
            if (color == COLOR_BLACK) color = COLOR_WHITE;
        }
        chat_row_blit(CHAT_START_ROW + r, rowbuf, color);
    }
    lines_dirty = 0;
    commits_pending = 0;
    stream_drawn_total = line_count + ((cur_len || wlen) ? 1 : 0);
    stream_partial_end = cur_len + wlen;
    draw_scroll_pct();
}

void chat_redraw_stream(void) {
    /* Streaming fast path: a full 19-row repaint costs far more than a
       chunk's wire time in SOFT80 and starves the serial consumer, so
       repaint the minimum: before the screen fills, only rows that
       changed; afterwards, scroll the bitmap and paint the tail; when
       just the partial line grew, only its new cells. */
    uint8_t r;
    uint8_t color;
    uint8_t partial = (cur_len || wlen) ? 1 : 0;
    int16_t total = line_count + partial;
    uint8_t new_end = cur_len + wlen;

    /* An overlong pending word isn't overlaid by build_view_row, and a
       span computed past TEXT_COLS would blit into the next row */
    if (new_end > TEXT_COLS) new_end = TEXT_COLS;

    if (view_scroll) {
        chat_redraw();
        stream_partial_end = new_end;
        return;
    }

    if (total <= CHAT_HEIGHT) {
        /* Not scrolling yet: rows above the previous total are unchanged */
        int16_t from = stream_drawn_total - 1;
        if (lines_dirty || from < 0) {
            if (from < 0) from = 0;
        } else {
            from = total - 1;  /* only the partial row */
        }
        if (from < 0) from = 0;
        for (r = (uint8_t)from; r < (uint8_t)total; ++r) {
            color = build_view_row(r);
            chat_row_blit(CHAT_START_ROW + r, rowbuf, color);
        }
        lines_dirty = 0;
        commits_pending = 0;
        stream_drawn_total = total;
        stream_partial_end = new_end;
        return;
    }

    if (lines_dirty) {
/* The scroll-blit fast path is DISABLED pending investigation: the
   banked-ROM bitmap copy provokes a serial-delivery stall + phantom
   RX ingestion under real-time streaming (see repo memory / commit
   log). Full redraws fit comfortably within the proxy pacing.
   Re-enable with -DSCROLL_OPT once the banked-window interaction is
   understood. */
#if defined(SOFT80) && defined(SCROLL_OPT)
        /* Lines committed while full: scroll the bitmap up and render
           only the freshly exposed tail rows. Requires the previous
           drawn state to have been full too. */
        if (commits_pending <= 3
                && stream_drawn_total >= CHAT_HEIGHT) {
            uint8_t n = commits_pending;
            soft80_scroll_chat(n);
            for (r = CHAT_HEIGHT - n - 1; r < CHAT_HEIGHT; ++r) {
                color = build_view_row(r);
                chat_row_blit(CHAT_START_ROW + r, rowbuf, color);
            }
            lines_dirty = 0;
            commits_pending = 0;
            stream_drawn_total = total;
            stream_partial_end = new_end;
            return;
        }
#endif
        chat_redraw();
        stream_drawn_total = total;
        stream_partial_end = new_end;
        return;
    }

    /* Only the line under construction grew: repaint just its new cells.
       (cur_len never shrinks within a line, and cells finalized out of
       the word buffer keep the same glyphs, so earlier cells are valid.) */
    r = CHAT_HEIGHT - 1;
    color = build_view_row(r);
#ifndef NO_SPAN
    if (new_end > stream_partial_end) {
        uint8_t from = stream_partial_end ? stream_partial_end - 1 : 0;
        ui_blit_span(CHAT_START_ROW + r, rowbuf, from, new_end - from);
    } else {
        chat_row_blit(CHAT_START_ROW + r, rowbuf, color);
    }
#else
    ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
#endif
    stream_partial_end = new_end;
    draw_scroll_pct();
}

void chat_area_clear_screen(void) {
    uint8_t r;
    memset(rowbuf, 0x20, TEXT_COLS);
    for (r = 0; r < CHAT_HEIGHT; ++r) {
        ui_blit_row(CHAT_START_ROW + r, rowbuf, COLOR_BLACK);
    }
}

/* --- frame / status ------------------------------------------------ */

void ui_draw_row(uint8_t row, const char* petscii, uint8_t color,
                 uint8_t reverse) {
    uint8_t i = 0;
    uint8_t rev = reverse ? 0x80 : 0x00;
    while (petscii[i] && i < TEXT_COLS) {
        rowbuf[i] = cell_from_petscii((uint8_t)petscii[i]) | rev;
        ++i;
    }
    for (; i < TEXT_COLS; ++i) {
        rowbuf[i] = 0x20 | rev;
    }
    ui_blit_row(row, rowbuf, color);
}

/* Persistent indicator flags (bit0: unviewed pic suggestion). Drawn in
   the status row's last cells so ordinary status messages can't clobber
   them - every status repaint re-draws the indicators on top. */
uint8_t ui_hints;

/* How many pictures this conversation has. Shown as a running tally
   rather than only when one is pending - it reads like a score, and it
   is a standing reminder that /pics can bring them back. */
uint8_t ui_pics;

/* Right-hand corner of the status row: "!P" when a scene is waiting,
   then the picture count. Ordinary status text can never clobber it -
   every status repaint redraws this on top. */
/* Soft-80 cells are ASCII; 40-col writes screen codes, where the digits
   happen to coincide with ASCII but the letters do not. */
#ifdef SOFT80
#define HINT_P     'P'
#define HINT_BANG  '!'
#define HINT_BLANK ' '
#else
#define HINT_P     0x10
#define HINT_BANG  0x21
#define HINT_BLANK 0x20
#endif

static void draw_hints(void) {
    uint8_t n = ui_pics;
    uint8_t d0, d1;
    if (n > 99) n = 99;
    /* Blank the corner, then fill in only what applies. Written as
       straight-line stores rather than four ternaries: cc65 generates
       poor code for those, and this corner is not worth much of the
       module-slot budget. */
    rowbuf[TEXT_COLS - 4] = HINT_BLANK | 0x80;
    rowbuf[TEXT_COLS - 3] = HINT_BLANK | 0x80;
    rowbuf[TEXT_COLS - 2] = HINT_BLANK | 0x80;
    rowbuf[TEXT_COLS - 1] = HINT_BLANK | 0x80;
    /* '!P' stays a pair: a pending scene shows it even before any
       picture exists, which is the whole point of the indicator. The
       tally sits beside it, so the corner reads "!P" / " P03" / "!P03". */
    if (ui_hints & 1) {
        rowbuf[TEXT_COLS - 4] = HINT_BANG | 0x80;
        rowbuf[TEXT_COLS - 3] = HINT_P | 0x80;
    }
    if (n) {
        /* Digits by subtraction: a uint8 divide drags cc65's runtime
           helper into the resident image. */
        d0 = '0';
        while (n >= 10) {
            n -= 10;
            ++d0;
        }
        d1 = '0' + n;
        rowbuf[TEXT_COLS - 3] = HINT_P | 0x80;
        rowbuf[TEXT_COLS - 2] = d0 | 0x80;
        rowbuf[TEXT_COLS - 1] = d1 | 0x80;
    }
    ui_blit_span(STATUS_ROW, rowbuf, TEXT_COLS - 4, 4);
}

void ui_set_hints(uint8_t flags) {
    ui_hints = flags;
    draw_hints();
}

#ifdef SOFT80
/* Server-composed right-hand chrome: where you are, and what is
   playing. The PROXY owns the text - it arrives preformatted in the
   HINT frame and the client only places it - so what the row says can
   change without ever touching the client again. Same bargain as the
   server-fed F1 menu.

   Soft-80 only: 40 columns have no room for it beside the status text,
   and no font of ours to draw a note with. */
char ui_chrome[UI_CHROME_MAX + 1];

/* Columns the last draw actually used. One byte, and it is what lets a
   chrome string SHRINK without leaving its own tail on the row - the
   alternative was keeping a copy of the status text (~77 bytes) just to
   repaint under it. */
static uint8_t chrome_w;

/* Right-aligned, one column clear of the hint corner, so a short string
   hugs the corner instead of floating. Like draw_hints() this draws
   over whatever ui_draw_row left in rowbuf and blits only its own span,
   which is what stops ordinary status text from clobbering it. */
static void draw_chrome(void) {
    uint8_t n = 0;
    uint8_t start, pad, i;
    while (ui_chrome[n]) ++n;
    if (n > TEXT_COLS - 6) n = TEXT_COLS - 6;
    if (!n && !chrome_w) return;
    /* if/else, not a ternary: cc65 generates poor code for those, and
       draw_hints() below already pays that lesson forward. */
    if (n >= chrome_w) {
        pad = 0;
    } else {
        pad = chrome_w - n;
    }
    start = TEXT_COLS - 5 - n - pad;
    for (i = 0; i < pad; ++i) rowbuf[start + i] = 0x20 | 0x80;
    for (i = 0; i < n; ++i) {
        rowbuf[start + pad + i] =
            cell_from_ascii((uint8_t)ui_chrome[i]) | 0x80;
    }
    ui_blit_span(STATUS_ROW, rowbuf, start, n + pad);
    chrome_w = n;
}

void ui_set_chrome(const char* s) {
    uint8_t i = 0;
    while (s[i] && i < UI_CHROME_MAX) {
        ui_chrome[i] = s[i];
        ++i;
    }
    ui_chrome[i] = 0;
    draw_chrome();
}
#endif

void ui_status(const char* msg) {
    ui_draw_row(STATUS_ROW, msg, COLOR_WHITE, 1);
#ifdef SOFT80
    /* After the row, before the hints: every status write would
       otherwise blank the chrome, which is exactly what happens to
       anything not redrawn on top here. */
    chrome_w = 0;               /* the row repaint already blanked it */
    draw_chrome();
#endif
    if (ui_hints || ui_pics) draw_hints();
}

static void draw_frame(void) {
    uint8_t i;
#ifdef SOFT80
    /* Return=send is self-evident once you have typed anything; the
       scroll keys are not discoverable at all, so they get the space. */
    ui_draw_row(0, " LLM64 " GIT_HASH "  F1=menu  F5=convs  F4/F6=scroll",
                COLOR_WHITE, 1);
#else
    /* 40 columns: F5 hint doesn't fit; the F1 menu lists it. Single
       spaces here - with a dirty hash the row is otherwise exactly 40
       and one more character would be truncated. */
    ui_draw_row(0, " LLM64 " GIT_HASH " F1=menu F4/F6=scroll", COLOR_WHITE, 1);
#endif
    for (i = 0; i < TEXT_COLS; ++i) {
#ifdef SOFT80
        rowbuf[i] = 0x2D;  /* '-' */
#else
        rowbuf[i] = 0x40;  /* horizontal bar glyph */
#endif
    }
    ui_blit_row(SEPARATOR_ROW, rowbuf, COLOR_GRAY1);
}

void ui_init(void) {
    VIC.bordercolor = COLOR_BLACK;
    VIC.bgcolor0 = COLOR_BLACK;
#ifdef SOFT80
    soft80_init();
#else
    *(uint8_t*)0xD018 = 0x17;  /* shifted charset for mixed case */
    memset(SCREEN, 0x20, 1000);
    memset(COLORS, COLOR_WHITE, 1000);
#endif
    chat_clear();
    draw_frame();
}

void ui_redraw_all(void) {
    draw_frame();
    chat_redraw();
}
