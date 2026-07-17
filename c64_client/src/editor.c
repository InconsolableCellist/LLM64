/**
 * C64 LLM Client - input editor implementation
 *
 * Single logical line up to 120 chars, displayed across three screen rows
 * with a software cursor (reverse video cell). Emacs-style bindings.
 */

#include <string.h>
#include <c64.h>
#include "editor.h"
#include "text.h"

#define SCREEN ((uint8_t*)0x0400)
#define COLORS ((uint8_t*)0xD800)

#define EDIT_ROW_FIRST 21
#define EDIT_ROWS      3

static char buf[EDIT_MAX + 1];  /* PETSCII */
static uint8_t len;
static uint8_t cur;

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
    uint8_t* dst = SCREEN + EDIT_ROW_FIRST * SCREEN_WIDTH;
    uint8_t* cdst = COLORS + EDIT_ROW_FIRST * SCREEN_WIDTH;
    uint8_t i;
    uint8_t total = EDIT_ROWS * SCREEN_WIDTH;

    for (i = 0; i < total; ++i) {
        uint8_t code = (i < len)
            ? petscii_to_screen((uint8_t)buf[i])
            : 0x20;
        if (i == cur) code |= 0x80;  /* software cursor */
        dst[i] = code;
    }
    memset(cdst, COLOR_YELLOW, total);
}
