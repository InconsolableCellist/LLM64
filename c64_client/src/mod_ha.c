/**
 * LLM64 Client - Home Assistant overview + graphs (overlay module #6)
 *
 * The proxy places every cell and computes every pixel column; this
 * module blits and plots. That is what keeps it inside the 3 KB slot,
 * and it lets the dashboard change with no client rebuild - the same
 * contract as the server-fed menu.
 *
 * Frames consumed:
 *   HA_ROWS  [first][count] then per row: [color:40][cells:80]
 *   HA_PLOT  [row][cell0][ncells] then ncells*8 bitmap bytes
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

extern ProtoContext proto;
extern volatile uint8_t sys_ticks[2];

void mod_ha_run(void);

#pragma code-name (push, "OVERLAY6")
#pragma rodata-name (push, "OVERLAY6")
#pragma bss-name (push, "OVL6BSS")

#define MAT(row)  ((uint8_t*)(0xCC00 + (uint16_t)(row) * 40))
#define BITMAP    0xE000

static const char S_WAIT[]  = "   asking the proxy...";
static const char S_LOST[]  = "No reply - r retries, f8 then f1 to redial.";
static const char S_SILENT[] = "Link silent - f8, then f1 to reconnect.";

static uint8_t  rbuf[80];
static uint8_t  pend;          /* waiting on HA_ROWS */
static uint8_t  req_at;
static uint8_t  req_retry;
static uint8_t  cur_view;

#define REQ_UNITS 90
/* A keystroke is always answered by at least one row, so silence after
   one means the link is gone. sys_ticks[1] steps about every 4.3s. */
#define ACT_UNITS 4
static uint8_t act_at, act_wait;

/* Bitmap row bases. Writes to $E000 reach RAM; READS return KERNAL ROM,
   which is why the proxy sends finished bytes and this only ever
   copies. */

static uint8_t* const rowbase[25] = {
    (uint8_t*)(BITMAP +  0*320), (uint8_t*)(BITMAP +  1*320),
    (uint8_t*)(BITMAP +  2*320), (uint8_t*)(BITMAP +  3*320),
    (uint8_t*)(BITMAP +  4*320), (uint8_t*)(BITMAP +  5*320),
    (uint8_t*)(BITMAP +  6*320), (uint8_t*)(BITMAP +  7*320),
    (uint8_t*)(BITMAP +  8*320), (uint8_t*)(BITMAP +  9*320),
    (uint8_t*)(BITMAP + 10*320), (uint8_t*)(BITMAP + 11*320),
    (uint8_t*)(BITMAP + 12*320), (uint8_t*)(BITMAP + 13*320),
    (uint8_t*)(BITMAP + 14*320), (uint8_t*)(BITMAP + 15*320),
    (uint8_t*)(BITMAP + 16*320), (uint8_t*)(BITMAP + 17*320),
    (uint8_t*)(BITMAP + 18*320), (uint8_t*)(BITMAP + 19*320),
    (uint8_t*)(BITMAP + 20*320), (uint8_t*)(BITMAP + 21*320),
    (uint8_t*)(BITMAP + 22*320), (uint8_t*)(BITMAP + 23*320),
    (uint8_t*)(BITMAP + 24*320)
};

/* HA_PLOT: [row][cell0][ncells] then ncells*8 finished bitmap bytes.
   Only the graph's own cells are written, so the label beside it
   survives. The proxy rasterizes because reads here return KERNAL ROM
   and a read-modify-write would OR ROM into the picture. */
static void ha_plot(void) {
    const uint8_t* pl = proto_get_payload(&proto);
    uint16_t len   = proto_get_length(&proto);
    uint8_t row    = pl[0];
    uint8_t cell0  = pl[1];
    uint8_t ncells = pl[2];
    if (row > 24 || (uint16_t)cell0 + ncells > 40) return;
    if ((uint16_t)ncells * 8u + 3u > len) return;
    memcpy(rowbase[row] + ((uint16_t)cell0 << 3), pl + 3,
           (uint16_t)ncells << 3);
}

/* Color matrix then cells, already placed by the proxy. */
static void ha_row(uint8_t row, const uint8_t* col, const uint8_t* cells) {
    memcpy(MAT(row), col, 40);
    memcpy(rbuf, cells, 80);
    soft80_span(row, rbuf, 0, 40);
}

static void ha_rows_frame(void) {
    const uint8_t* pl = proto_get_payload(&proto);
    uint16_t len = proto_get_length(&proto);
    uint8_t first = pl[0];
    uint8_t count = pl[1];
    const uint8_t* p = &pl[2];
    uint8_t i;

    act_wait = 0;                 /* the proxy answered */
    /* Trust nothing in here. The frame checksum is one XOR byte, so
       about one corrupt frame in 256 arrives looking valid, and this
       link drops bytes for a living. An unchecked row index writes
       40 bytes at $CC00 + row*40 and hands soft80_span an index past
       its 25-entry tables - which is a hang, minutes or hours later. */
    if ((uint16_t)count * 120u + 2u > len) return;
    for (i = 0; i < count; ++i) {
        if ((uint16_t)first + i > 24) break;
        ha_row(first + i, p, p + 40);
        p += 120;
    }
    pend = 0;
}

static void ha_request(uint8_t view) {
    uint8_t arg[1];
    arg[0] = view;
    pend = 1;
    req_at = sys_ticks[1];
    proto_send_message(MSG_GET_HA, arg, 1);
}

static uint8_t ha_msg(uint8_t t) {
    if (t == MSG_HA_ROWS)  { ha_rows_frame(); return 1; }
    if (t == MSG_HA_PLOT)  { ha_plot(); return 1; }
    return 0;
}

static void ha_tick(void) {
    if (act_wait && (uint8_t)(sys_ticks[1] - act_at) >= ACT_UNITS) {
        act_wait = 0;
        ui_status(S_SILENT);
    }
    if (!pend) return;
    if ((uint8_t)(sys_ticks[1] - req_at) < REQ_UNITS) return;
    if (!req_retry) { req_retry = 1; ha_request(cur_view); ui_status(S_WAIT); }
    else { pend = 0; ui_status(S_LOST); }
}

/* Keys go to the proxy as bytes; the client does not know which letter
   is which entity. */
static void ha_key(uint8_t k) {
    uint8_t arg[2];
    /* Numeric, not 'V': cc65 encodes C character literals as PETSCII,
       so 'V' would go out as $D6 and the proxy would never match it. */
    if (k == KEY_F7) {            /* f7 - the view picker */
        arg[0] = 0x56;            /* ASCII 'V' */
        arg[1] = 0;
        proto_send_message(MSG_HA_ACTION, arg, 2);
        return;                   /* the proxy draws it; do not re-ask */
    }
    if (k == 138 || k == 139) {   /* f4 / f6 - page a long view */
        arg[0] = (k == 139) ? 0x4E : 0x50;   /* ASCII 'N' / 'P' */
        arg[1] = 0;
        proto_send_message(MSG_HA_ACTION, arg, 2);
        return;
    }
    if (k == 140 || k == 133) {   /* f8, and f1 because that is the key
                                     everyone reaches for - leave */
        /* Tell the proxy, or it keeps pushing state changes at a screen
           that is gone - and those frames land in the middle of whatever
           the chat is streaming next. */
        arg[0] = 0x51;            /* ASCII 'Q' */
        arg[1] = 0;
        proto_send_message(MSG_HA_ACTION, arg, 2);
        mod_modal_end();
        return;
    }
    if (k == 3) {                 /* run/stop - the C64's escape: back
                                     one screen, which only the proxy
                                     knows how to do */
        arg[0] = 0x1B;
        arg[1] = 0;
        proto_send_message(MSG_HA_ACTION, arg, 2);
        return;
    }
    if (k == 'r') { ha_request(cur_view); return; }
    arg[0] = petscii_to_ascii(k);
    arg[1] = cur_view;
    proto_send_message(MSG_HA_ACTION, arg, 2);
    act_at = sys_ticks[1];
    act_wait = 1;
    /* No status line: row 24 is the proxy's key hints, and covering it
       hides the way back out. The screen redraw is the feedback. */
}

void mod_ha_run(void) {
    pend = 0;
    req_retry = 0;
    act_wait = 0;
    cur_view = 0;
    chat_area_clear_screen();
    mod_modal_begin(ha_msg, ha_key);
    mod_modal_tick(ha_tick);
    ui_status(S_WAIT);
    ha_request(0);
}

#pragma bss-name (pop)
#pragma rodata-name (pop)
#pragma code-name (pop)

#endif /* SOFT80 */
