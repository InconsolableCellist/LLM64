/**
 * C64 LLM Client - TUI display implementation
 */

#include <string.h>
#include <c64.h>
#include "ui.h"
#include "text.h"

#define SCREEN ((uint8_t*)0x0400)
#define COLORS ((uint8_t*)0xD800)

#define MAX_LINES 160  /* ~6.4KB scrollback */

/* Committed, pre-wrapped lines (screen codes, space padded) */
static uint8_t line_text[MAX_LINES][SCREEN_WIDTH];
static uint8_t line_color[MAX_LINES];
static uint8_t line_head;    /* ring index of next write */
static uint8_t line_count;   /* committed lines (caps at MAX_LINES) */

/* Line under construction */
static uint8_t cur[SCREEN_WIDTH];
static uint8_t cur_len;
static uint8_t cur_color;

/* Pending word for wrapping */
static uint8_t wbuf[SCREEN_WIDTH];
static uint8_t wlen;

static uint8_t view_scroll;  /* 0 = pinned to bottom */

/* Role colors */
static const uint8_t role_colors[3] = {
    COLOR_CYAN,        /* user */
    COLOR_LIGHTGREEN,  /* assistant */
    COLOR_GRAY2        /* system */
};

static void cur_reset(void) {
    memset(cur, 0x20, SCREEN_WIDTH);
    cur_len = 0;
}

static void commit_line(void) {
    memcpy(line_text[line_head], cur, SCREEN_WIDTH);
    line_color[line_head] = cur_color;
    ++line_head;
    if (line_head >= MAX_LINES) line_head = 0;
    if (line_count < MAX_LINES) ++line_count;
    cur_reset();
}

/* Move the pending word into the current line, wrapping if needed */
static void flush_word(void) {
    if (wlen == 0) return;
    if (cur_len + wlen > SCREEN_WIDTH) {
        commit_line();
    }
    memcpy(cur + cur_len, wbuf, wlen);
    cur_len += wlen;
    wlen = 0;
    if (cur_len >= SCREEN_WIDTH) {
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
        if (cur_len < SCREEN_WIDTH && cur_len > 0) {
            cur[cur_len++] = 0x20;
        }
        return;
    }
    wbuf[wlen++] = ascii_to_screen(c);
    if (wlen >= SCREEN_WIDTH) {
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

void chat_redraw(void) {
    uint8_t partial = (cur_len || wlen) ? 1 : 0;
    int16_t total = line_count + partial;
    int16_t first = total - CHAT_HEIGHT - (int16_t)view_scroll;
    uint8_t r;
    uint8_t* dst = SCREEN + CHAT_START_ROW * SCREEN_WIDTH;
    uint8_t* cdst = COLORS + CHAT_START_ROW * SCREEN_WIDTH;

    for (r = 0; r < CHAT_HEIGHT; ++r) {
        int16_t idx = first + r;
        if (idx < 0 || idx >= total) {
            memset(dst, 0x20, SCREEN_WIDTH);
        } else if (idx == line_count) {
            /* Line under construction (word buffer shown too) */
            memcpy(dst, cur, SCREEN_WIDTH);
            if (wlen && cur_len + wlen <= SCREEN_WIDTH) {
                memcpy(dst + cur_len, wbuf, wlen);
            }
            memset(cdst, cur_color, SCREEN_WIDTH);
        } else {
            uint8_t phys = (uint8_t)((line_head + MAX_LINES - line_count + idx)
                                     % MAX_LINES);
            memcpy(dst, line_text[phys], SCREEN_WIDTH);
            memset(cdst, line_color[phys], SCREEN_WIDTH);
        }
        dst += SCREEN_WIDTH;
        cdst += SCREEN_WIDTH;
    }
}

void chat_area_clear_screen(void) {
    memset(SCREEN + CHAT_START_ROW * SCREEN_WIDTH, 0x20,
           CHAT_HEIGHT * SCREEN_WIDTH);
}

/* --- frame / status ------------------------------------------------ */

void ui_draw_row(uint8_t row, const char* petscii, uint8_t color,
                 uint8_t reverse) {
    uint8_t* dst = SCREEN + (uint16_t)row * SCREEN_WIDTH;
    uint8_t* cdst = COLORS + (uint16_t)row * SCREEN_WIDTH;
    uint8_t i = 0;
    uint8_t rev = reverse ? 0x80 : 0x00;
    while (petscii[i] && i < SCREEN_WIDTH) {
        dst[i] = petscii_to_screen((uint8_t)petscii[i]) | rev;
        ++i;
    }
    for (; i < SCREEN_WIDTH; ++i) {
        dst[i] = 0x20 | rev;
    }
    memset(cdst, color, SCREEN_WIDTH);
}

void ui_status(const char* msg) {
    ui_draw_row(STATUS_ROW, msg, COLOR_WHITE, 1);
}

static void draw_frame(void) {
    uint8_t i;
    uint8_t* sep = SCREEN + SEPARATOR_ROW * SCREEN_WIDTH;
    ui_draw_row(0, " C64 LLM   F1send F2new F3stop F5conv F7", COLOR_WHITE, 1);
    for (i = 0; i < SCREEN_WIDTH; ++i) sep[i] = 0x40;  /* horizontal bar */
    memset(COLORS + SEPARATOR_ROW * SCREEN_WIDTH, COLOR_GRAY1, SCREEN_WIDTH);
}

void ui_init(void) {
    VIC.bordercolor = COLOR_BLACK;
    VIC.bgcolor0 = COLOR_BLACK;
    *(uint8_t*)0xD018 = 0x17;  /* shifted charset for mixed case */
    memset(SCREEN, 0x20, 1000);
    memset(COLORS, COLOR_WHITE, 1000);
    chat_clear();
    draw_frame();
}

void ui_redraw_all(void) {
    memset(SCREEN, 0x20, 1000);
    draw_frame();
    chat_redraw();
}
