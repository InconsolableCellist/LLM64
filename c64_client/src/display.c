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
static uint8_t lines_dirty;  /* a line committed since last full redraw */
static uint8_t commits_pending;  /* commits since last full draw (scroll opt) */

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
    chat_redraw();
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

void chat_redraw(void) {
    uint8_t r;
    uint8_t color;

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
}

void chat_redraw_stream(void) {
    /* Streaming fast path: rendering all 19 rows is far slower than a
       chunk's wire time (especially in SOFT80), so repaint as little as
       possible. */
    uint8_t r;
    uint8_t color;

    if (view_scroll) {
        chat_redraw();
        return;
    }

    if (lines_dirty) {
#ifdef SOFT80
        /* Lines committed: scroll the bitmap up and render only the
           freshly exposed tail rows. */
        if (commits_pending <= 3
                && line_count + 1 > CHAT_HEIGHT
                && line_count - commits_pending + 1 >= CHAT_HEIGHT) {
            uint8_t n = commits_pending;
            soft80_scroll_chat(n);
            for (r = CHAT_HEIGHT - n - 1; r < CHAT_HEIGHT; ++r) {
                color = build_view_row(r);
                ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
            }
            lines_dirty = 0;
            commits_pending = 0;
            return;
        }
#endif
        chat_redraw();
        return;
    }

    /* Only the line under construction changed */
    r = CHAT_HEIGHT - 1;
    {
        uint8_t partial = (cur_len || wlen) ? 1 : 0;
        int16_t total = line_count + partial;
        if (total < CHAT_HEIGHT) {
            r = (uint8_t)total - 1;
        }
    }
    color = build_view_row(r);
    ui_blit_row(CHAT_START_ROW + r, rowbuf, color);
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
