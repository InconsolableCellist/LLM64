/**
 * C64 LLM Client - input editor implementation
 *
 * Single logical text up to EDIT_MAX chars. The three editor rows are a
 * scrolling window over it: the view follows the cursor, so input can be
 * much longer than fits on screen. Emacs-style bindings. Rendering goes
 * through ui_blit_row/span so it works in 40 and 80 columns.
 */

#include <string.h>
#include <c64.h>
#include "editor.h"
#include "text.h"
#include "ui.h"

#define EDIT_ROW_FIRST 21
#define EDIT_ROWS      3

static char buf[EDIT_MAX + 1];  /* PETSCII */
static uint16_t len;
static uint16_t cur;
static uint16_t view_row;       /* first visible logical row */
static uint8_t rowcells[TEXT_COLS];

void editor_init(void) {
    editor_clear();
}

char* editor_text(void) {
    buf[len] = 0;
    return buf;
}

uint16_t editor_len(void) {
    return len;
}

/* Printable PETSCII: space..underscore block and shifted letters */
static uint8_t is_printable(uint8_t c) {
    if (c >= 0x20 && c <= 0x5F) return 1;
    if (c >= 0xC1 && c <= 0xDA) return 1;
    return 0;
}

/* Keep the cursor's logical row inside the 3-row window.
   Returns 1 if the view moved (full repaint needed). */
static uint8_t track_cursor(void) {
    uint16_t crow = cur / TEXT_COLS;
    if (crow < view_row) {
        view_row = crow;
        return 1;
    }
    if (crow > view_row + (EDIT_ROWS - 1)) {
        view_row = crow - (EDIT_ROWS - 1);
        return 1;
    }
    return 0;
}

/* Repaint editor cells [from, to] (logical indices), relative to view */
static void redraw_range(uint16_t from, uint16_t to) {
    uint8_t row;
    uint8_t i;
    uint16_t idx;
    uint16_t row_start;

    for (row = 0; row < EDIT_ROWS; ++row) {
        uint8_t s, e;
        row_start = (view_row + row) * TEXT_COLS;
        if (row_start > to || row_start + TEXT_COLS <= from) continue;
        idx = row_start;
        for (i = 0; i < TEXT_COLS; ++i, ++idx) {
            uint8_t code = (idx < len)
                ? ui_cell_from_petscii((uint8_t)buf[idx])
                : 0x20;
            if (idx == cur) code |= 0x80;
            rowcells[i] = code;
        }
        s = (from > row_start) ? (uint8_t)(from - row_start) : 0;
        e = (to < row_start + TEXT_COLS - 1)
            ? (uint8_t)(to - row_start) : TEXT_COLS - 1;
        ui_blit_span(EDIT_ROW_FIRST + row, rowcells, s, e - s + 1);
    }
}

/* Full repaint via ui_blit_row: unlike the span path, this also sets
   the editor's color - spans alone leave color RAM untouched, which on
   the 80-col matrix means invisible black-on-black glyphs. */
static void full_repaint(void) {
    uint8_t row;
    uint8_t i;
    uint16_t idx;

    for (row = 0; row < EDIT_ROWS; ++row) {
        idx = (view_row + row) * TEXT_COLS;
        for (i = 0; i < TEXT_COLS; ++i, ++idx) {
            uint8_t code = (idx < len)
                ? ui_cell_from_petscii((uint8_t)buf[idx])
                : 0x20;
            if (idx == cur) code |= 0x80;
            rowcells[i] = code;
        }
        ui_blit_row(EDIT_ROW_FIRST + row, rowcells, COLOR_YELLOW);
    }
}

void editor_redraw(void) {
    track_cursor();
    full_repaint();
}

void editor_clear(void) {
    len = 0;
    cur = 0;
    view_row = 0;
    buf[0] = 0;
    editor_redraw();
}

uint8_t editor_key(uint8_t key) {
    uint16_t old_cur = cur;
    uint16_t old_len = len;
    uint16_t lo, hi;

    switch (key) {
        case KEY_DEL:
        case KEY_INST:   /* SHIFT+DEL, and the only DEL under shift lock */
            if (cur > 0) {
                memmove(buf + cur - 1, buf + cur, len - cur);
                --cur;
                --len;
            }
            break;
        case KEY_CRSR_LEFT:
            if (cur > 0) --cur;
            break;
        case KEY_CRSR_RIGHT:
            if (cur < len) ++cur;
            break;
        case CTRL_A:
        case KEY_HOME:
            cur = 0;
            break;
        case CTRL_E:
            cur = len;
            break;
        case CTRL_K:
            len = cur;
            break;
        case CTRL_D:
            if (cur < len) {
                memmove(buf + cur, buf + cur + 1, len - cur - 1);
                --len;
            }
            break;
        case KEY_CLR:
            len = 0;
            cur = 0;
            break;
        default:
            if (is_printable(key) && len < EDIT_MAX) {
                memmove(buf + cur + 1, buf + cur, len - cur);
                buf[cur] = key;
                ++cur;
                ++len;
            } else {
                return 0;  /* not consumed */
            }
    }

    if (track_cursor()) {
        /* view scrolled: repaint the whole window (and its color) */
        full_repaint();
        return 1;
    }

    /* Repaint only what changed: from the leftmost affected cell to the
       furthest of (old/new end, old/new cursor). */
    lo = (cur < old_cur) ? cur : old_cur;
    hi = (len > old_len) ? len : old_len;
    if (old_cur > hi) hi = old_cur;
    if (cur > hi) hi = cur;
    redraw_range(lo, hi);
    return 1;
}
