/**
 * C64 LLM Client - input editor implementation
 *
 * Single logical line up to EDIT_MAX chars, displayed across three text
 * rows with a software cursor (reverse video cell). Emacs-style bindings.
 * Rendering goes through ui_blit_row so it works in 40 and 80 columns.
 */

#include <string.h>
#include <c64.h>
#include "editor.h"
#include "text.h"
#include "ui.h"

#define EDIT_ROW_FIRST 21
#define EDIT_ROWS      3

static char buf[EDIT_MAX + 1];  /* PETSCII */
static uint8_t len;
static uint8_t cur;
static uint8_t rowcells[TEXT_COLS];

void editor_init(void) {
    editor_clear();
}

void editor_clear(void) {
    len = 0;
    cur = 0;
    buf[0] = 0;
    editor_redraw();
}

char* editor_text(void) {
    buf[len] = 0;
    return buf;
}

uint8_t editor_len(void) {
    return len;
}

/* Printable PETSCII: space..underscore block and shifted letters */
static uint8_t is_printable(uint8_t c) {
    if (c >= 0x20 && c <= 0x5F) return 1;
    if (c >= 0xC1 && c <= 0xDA) return 1;
    return 0;
}

uint8_t editor_key(uint8_t key) {
    switch (key) {
        case KEY_DEL:
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
    editor_redraw();
    return 1;
}

void editor_redraw(void) {
    uint8_t row;
    uint8_t i;
    uint16_t idx = 0;

    for (row = 0; row < EDIT_ROWS; ++row) {
        for (i = 0; i < TEXT_COLS; ++i, ++idx) {
            uint8_t code = (idx < len)
                ? ui_cell_from_petscii((uint8_t)buf[idx])
                : 0x20;
            if (idx == cur) code |= 0x80;  /* software cursor */
            rowcells[i] = code;
        }
        ui_blit_row(EDIT_ROW_FIRST + row, rowcells, COLOR_YELLOW);
    }
}
