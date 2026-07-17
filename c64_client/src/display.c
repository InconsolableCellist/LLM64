/**
 * C64 LLM Client - TUI display implementation
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
#ifdef SOFT80
#include "soft80.h"
#endif

#define SCREEN ((uint8_t*)0x0400)
#define COLORS ((uint8_t*)0xD800)

#define MAX_LINES 160

/* Committed, pre-wrapped lines (cells, space padded) */
static uint8_t line_text[MAX_LINES][TEXT_COLS];
static uint8_t line_color[MAX_LINES];
static uint8_t line_head;    /* ring index of next write */
static uint8_t line_count;   /* committed lines (caps at MAX_LINES) */

/* Line under construction */
static uint8_t cur[TEXT_COLS];
static uint8_t cur_len;
static uint8_t cur_color;

/* Pending word for wrapping */
static uint8_t wbuf[TEXT_COLS];
static uint8_t wlen;

static uint8_t view_scroll;  /* 0 = pinned to bottom */
static uint8_t frozen;       /* suppress rendering (bulk conversation load) */
static uint8_t lines_dirty;  /* a line committed since last full redraw */
static uint8_t commits_pending;  /* commits since last full draw (scroll opt) */
static int16_t stream_drawn_total;  /* total lines at last stream draw */
static uint8_t stream_partial_end;  /* drawn length of the partial line */

static uint8_t rowbuf[TEXT_COLS];

/* Role colors */
static const uint8_t role_colors[3] = {
    COLOR_CYAN,        /* user */
    COLOR_LIGHTGREEN,  /* assistant */
    COLOR_GRAY2        /* system */
};

/* --- cell encoding -------------------------------------------------- */

static uint8_t cell_from_ascii(uint8_t c) {
#ifdef SOFT80
    if (c >= 0x20 && c < 0x7F) return c;
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

/* --- low-level row output ------------------------------------------- */

void ui_blit_row(uint8_t row, const uint8_t* cells, uint8_t color) {
#ifdef SOFT80
    soft80_row(row, cells, color);
#else
    memcpy(SCREEN + (uint16_t)row * 40, cells, 40);
    memset(COLORS + (uint16_t)row * 40, color, 40);
#endif
}

/* Repaint only cells [first, first+count) of a row whose color hasn't
   changed. Much cheaper than a full row in SOFT80 (~0.2ms/cell). */
void ui_blit_span(uint8_t row, const uint8_t* cells, uint8_t first,
                  uint8_t count) {
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

static void cur_reset(void) {
    memset(cur, 0x20, TEXT_COLS);
    cur_len = 0;
}

static void commit_line(void) {
    memcpy(line_text[line_head], cur, TEXT_COLS);
    line_color[line_head] = cur_color;
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
    cur_color = role_colors[role > 2 ? 2 : role];
    view_scroll = 0;
}

void chat_append_ascii_char(uint8_t c) {
    if (c == 0x0D) return;
    if (c == 0x0A) {
        flush_word();
        commit_line();
        return;
    }
    if (c == 0x20) {
        flush_word();
        if (cur_len < TEXT_COLS && cur_len > 0) {
            cur[cur_len++] = 0x20;
        }
        return;
    }
    wbuf[wlen++] = cell_from_ascii(c);
    if (wlen >= TEXT_COLS) {
        /* Word longer than a line: hard wrap */
        flush_word();
    }
}

void chat_append_ascii(const char* s) {
    while (*s) {
        chat_append_ascii_char((uint8_t)*s);
        ++s;
    }
    view_scroll = 0;
}

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
        ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
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
            ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
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
                ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
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
        ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
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

void ui_status(const char* msg) {
    ui_draw_row(STATUS_ROW, msg, COLOR_WHITE, 1);
}

static void draw_frame(void) {
    uint8_t i;
    ui_draw_row(0, " C64 LLM        F1=menu      Return=send", COLOR_WHITE, 1);
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
