/**
 * LLM64 Client - server-fed menu (overlay module #4)
 *
 * F1 opens a floating dialog whose entries come from the PROXY as
 * [key][label][command] triples - /help and the menu share one
 * server-side source of truth, and new commands appear here with
 * zero client bytes. Commands starting with '!' are local actions
 * (config editor, disk copy, ...) dispatched by the resident loop
 * AFTER this module's code is off the call stack, because they may
 * load another module into this very slot.
 *
 * The panel floats over the chat: soft80_span() renders glyph pairs
 * without touching the color matrix, so we paint the panel span and
 * poke the matrix ourselves - blue panel, solid light-blue bars, a
 * selection bar, and a matrix-only drop shadow that dims the chat
 * underneath instead of erasing it.
 *
 * Hook-driven modal (like the conversation manager): the resident
 * loop keeps pumping serial while we wait for the MENU_LIST frame.
 */

#ifdef SOFT80

#include <string.h>
#include <c64.h>
#include "common.h"
#include "modapi.h"
#include "ui.h"
#include "text.h"
#include "soft80.h"
#include "protocol.h"

extern ProtoContext proto;   /* RX context (main.c) */

void mod_menu_run(void);

#pragma code-name (push, "OVERLAY4")
#pragma rodata-name (push, "OVERLAY4")
/* Statics (incl. -Cl static locals) live in slot RAM past the loaded
   code - zero resident bytes, but NOT zero-initialized: everything
   here is stored before it is read */
#pragma bss-name (push, "OVL4BSS")

#define MN_STOP   3

/* cc65 emits anonymous string literals into "RODATA" regardless of
   the pragma; named const arrays honor it, so the module's strings
   are declared explicitly to stay in the overlay */
static const char S_TITLE[] = " llm64 menu ";
static const char S_FETCH[] = "   fetching from server...";
static const char S_EMPTY[] = "   (no entries - /help still works)";
static const char S_FOOT[]  = " key or return = run    f1 = close";
static const char S_READY[] = "Ready.";

/* Panel geometry: text columns [PN_COL, PN_COL+PN_W), matrix pairs
   [PN_PAIR, PN_PAIR+PN_PAIRS). All even so the 2-column color
   granularity lines up. */
#define PN_TOP    2
#define PN_COL    16
#define PN_W      48
#define PN_PAIR   (PN_COL / 2)
#define PN_PAIRS  (PN_W / 2)
#define MAT(row)  ((uint8_t*)(0xCC00 + (uint16_t)(row) * 40))

#define MC_BORDER ((COLOR_LIGHTBLUE << 4) | COLOR_BLUE)
#define MC_TEXT   ((COLOR_WHITE << 4) | COLOR_BLUE)
#define MC_SEL    ((COLOR_YELLOW << 4) | COLOR_BLUE)
#define MC_DIM    ((COLOR_GRAY2 << 4) | COLOR_BLUE)
#define MC_SHADOW (COLOR_GRAY1 << 4)   /* gray on black: dims chat */

static uint8_t pbuf[80];

/* Matrix for one panel row: light-blue border pairs, given interior,
   plus the 2-pair right-hand drop shadow (skipped on the title row
   so the shadow starts one row below the panel top). */
static void pn_mat(uint8_t row, uint8_t interior) {
    uint8_t* m = MAT(row);
    uint8_t i;
    m[PN_PAIR] = MC_BORDER;
    for (i = PN_PAIR + 1; i < PN_PAIR + PN_PAIRS - 1; ++i) m[i] = interior;
    m[PN_PAIR + PN_PAIRS - 1] = MC_BORDER;
    if (row > PN_TOP) {
        m[PN_PAIR + PN_PAIRS] = MC_SHADOW;
        m[PN_PAIR + PN_PAIRS + 1] = MC_SHADOW;
    }
}

/* Fill the panel span of pbuf: solid (reverse-space) side borders,
   interior spaces carrying the given reverse mask */
static void pn_fill(uint8_t rev) {
    uint8_t i;
    pbuf[PN_COL] = pbuf[PN_COL + 1] = 0x20 | 0x80;
    for (i = PN_COL + 2; i < PN_COL + PN_W - 2; ++i) pbuf[i] = 0x20 | rev;
    pbuf[PN_COL + PN_W - 2] = pbuf[PN_COL + PN_W - 1] = 0x20 | 0x80;
}

/* Write a PETSCII literal into pbuf at panel-relative column x */
static void pn_write(uint8_t x, const char* pet, uint8_t rev) {
    uint8_t i;
    x += PN_COL;
    for (i = 0; pet[i] && x < PN_COL + PN_W - 2; ++i, ++x) {
        pbuf[x] = ui_cell_from_petscii((uint8_t)pet[i]) | rev;
    }
}

/* Solid bar row (top/bottom); text, if any, is carved into the bar
   as blue-on-lightblue */
static void pn_bar(uint8_t row, const char* pet) {
    uint8_t i;
    for (i = PN_COL; i < PN_COL + PN_W; ++i) pbuf[i] = 0x20 | 0x80;
    if (pet) pn_write(4, pet, 0x80);
    pn_mat(row, MC_BORDER);
    soft80_span(row, pbuf, PN_PAIR, PN_PAIRS);
}

/* Interior text row from a PETSCII literal (0 = blank line) */
static void pn_line(uint8_t row, const char* pet, uint8_t interior) {
    pn_fill(0);
    if (pet) pn_write(4, pet, 0);
    pn_mat(row, interior);
    soft80_span(row, pbuf, PN_PAIR, PN_PAIRS);
}

/* One menu entry: "k  Label", selection = reverse video + yellow */
static void pn_entry(uint8_t i) {
    MenuEntry* e = &menu_entries[i];
    uint8_t rev = (i == conv_sel) ? 0x80 : 0x00;
    uint8_t x, j;
    pn_fill(rev);
    pbuf[PN_COL + 4] = e->key | rev;      /* raw ASCII = cell */
    x = PN_COL + 7;
    for (j = 0; e->label[j] && x < PN_COL + PN_W - 3; ++j, ++x) {
        pbuf[x] = (uint8_t)e->label[j] | rev;
    }
    pn_mat(PN_TOP + 2 + i, rev ? MC_SEL : MC_TEXT);
    soft80_span(PN_TOP + 2 + i, pbuf, PN_PAIR, PN_PAIRS);
}

/* Bottom drop shadow: matrix only - the chat glyphs stay and dim */
static void pn_shadow(uint8_t row) {
    uint8_t* m = MAT(row);
    uint8_t i;
    if (row > PN_TOP + 17) return;   /* keep inside the chat area */
    for (i = PN_PAIR + 1; i < PN_PAIR + PN_PAIRS + 2; ++i) m[i] = MC_SHADOW;
}

static void mn_draw(void) {
    uint8_t n = conv_count ? conv_count : 1;
    uint8_t i;
    pn_bar(PN_TOP, S_TITLE);
    pn_line(PN_TOP + 1, 0, MC_TEXT);
    if (conv_loading) {
        pn_line(PN_TOP + 2, S_FETCH, MC_DIM);
    } else if (!conv_count) {
        pn_line(PN_TOP + 2, S_EMPTY, MC_DIM);
    } else {
        for (i = 0; i < conv_count; ++i) pn_entry(i);
    }
    pn_line(PN_TOP + 2 + n, 0, MC_TEXT);
    pn_line(PN_TOP + 3 + n, S_FOOT, MC_DIM);
    pn_bar(PN_TOP + 4 + n, 0);
    pn_shadow(PN_TOP + 5 + n);
}

static void mn_parse(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint8_t n = p[0];
    uint16_t off = 2;
    uint8_t i, t;
    for (i = 0; i < n && conv_count < MAX_MENU && off < plen; ++i) {
        MenuEntry* e = &menu_entries[conv_count];
        e->key = p[off++];
        t = 0;
        while (off < plen && p[off] && t < sizeof(e->label) - 1) {
            e->label[t++] = p[off++];              /* raw ASCII */
        }
        e->label[t] = 0;
        ++off;
        t = 0;
        while (off < plen && p[off] && t < sizeof(e->cmd) - 1) {
            e->cmd[t++] = ascii_to_petscii(p[off++]);
        }
        e->cmd[t] = 0;
        ++off;
        ++conv_count;
    }
    if (!(p[1] & 1)) conv_loading = 0;
}

static uint8_t mn_msg(uint8_t t) {
    if (t == MSG_MENU_LIST) {
        mn_parse();
        if (!conv_loading) mn_draw();
        return 1;
    }
    return 0;
}

/* Activate entry i. Close the modal FIRST, then leave the action for
   the resident dispatcher - local actions ('!') may overwrite this
   slot with another module. */
static void mn_go(uint8_t i) {
    char* c = menu_entries[i].cmd;
    mod_modal_end();
    if (c[0] == '!') {
        menu_action = (uint8_t)c[1];
    } else {
        menu_pcmd = c;   /* points into convs[] BSS: safe after close */
    }
}

static void mn_key(uint8_t k) {
    uint8_t i, o;
    if (k == KEY_F1 || k == MN_STOP) {
        mod_modal_end();
        ui_status(S_READY);
        return;
    }
    if (conv_loading || !conv_count) return;
    switch (k) {
        case KEY_CRSR_UP:
            if (conv_sel) {
                o = conv_sel--;
                pn_entry(o);
                pn_entry(conv_sel);
            }
            return;
        case KEY_CRSR_DOWN:
            if (conv_sel + 1 < conv_count) {
                o = conv_sel++;
                pn_entry(o);
                pn_entry(conv_sel);
            }
            return;
        case KEY_RETURN:
            mn_go(conv_sel);
            return;
    }
    o = petscii_to_ascii(k);
    for (i = 0; i < conv_count; ++i) {
        if (menu_entries[i].key == o) {
            mn_go(i);
            return;
        }
    }
}

void mod_menu_run(void) {
    conv_count = 0;
    conv_sel = 0;
    conv_loading = 1;
    menu_action = 0;
    menu_pcmd = 0;
    mod_modal_begin(mn_msg, mn_key);
    proto_send_message(MSG_GET_MENU, 0, 0);
    mn_draw();
}

#pragma bss-name (pop)
#pragma rodata-name (pop)
#pragma code-name (pop)

#endif /* SOFT80 */
