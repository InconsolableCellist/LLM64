/**
 * C64 LLM Client - jukebox / sound window (overlay module #5)
 *
 * A floating panel showing what the LLM put on: title, author, a
 * progress bar against the tune's real length, and a meter driven off
 * the SID's own voice-3 oscillator ($D41B) - the classic way to get
 * audio feedback out of the chip. It reflects voice 3 only, which is
 * why it is labelled that rather than dressed up as a master level.
 *
 * Everything except the meter comes from the proxy in one NOWPLAYING
 * frame - the client never knew a tune's duration (nothing in a SID
 * file says), so the server reads it from the HVSC song-length database
 * and sends elapsed/total already worked out. Same shape as the
 * server-fed menu: ask, then render what comes back.
 *
 * This is the first module to use the resident tick hook. The clock has
 * to advance and the meter has to move with no keypress and no frame
 * arriving, so mod_modal_tick() gets called from the main loop; between
 * server updates the elapsed time is carried forward locally off the
 * 60Hz counter rather than asking again.
 *
 * Panel drawing follows mod_menu.c: soft80_span() paints glyphs without
 * touching the color matrix, so the matrix is poked separately and the
 * chat underneath merely dims.
 */

#ifdef SOFT80

#include <c64.h>
#include "common.h"
#include "modapi.h"
#include "ui.h"
#include "text.h"
#include "soft80.h"
#include "protocol.h"

extern ProtoContext proto;          /* RX context (main.c) */
extern volatile uint8_t sys_ticks[2];
extern uint8_t music_state;

void mod_sound_run(void);

#define SID_OSC3 (*(volatile uint8_t*)0xD41B)

#pragma code-name (push, "OVERLAY5")
#pragma rodata-name (push, "OVERLAY5")
/* Statics (incl. -Cl static locals) live in slot RAM past the loaded
   code: zero resident bytes, but NOT zero-initialized - store before
   read, always. */
#pragma bss-name (push, "OVL5BSS")

#define JB_STOP 3

/* cc65 emits anonymous string literals into "RODATA" whatever the
   pragma says; named const arrays honor it and stay in the overlay */
static const char S_TITLE[] = " c64 llm jukebox ";
static const char S_FETCH[] = "asking the server...";
static const char S_SILENT[] = "nothing playing";
static const char S_HINT[]  = "type /music <mood> to start one";
static const char S_FOOT1[] = "n = next tune     f = favorite";
static const char S_FOOT2[] = "f1 or stop = close";
static const char S_READY[] = "Ready.";
static const char S_BY[]    = "by ";
static const char S_FAV[]   = "*favorite*";
static const char S_SIG[]   = "voice 3";
static const char S_NEXT[]  = "/music ";   /* + mood, built at send time */

/* Panel geometry, mirroring the menu module: all even so the 2-column
   color granularity lines up. */
#define PN_TOP    3
#define PN_COL    14
#define PN_W      52
#define PN_PAIR   (PN_COL / 2)
#define PN_PAIRS  (PN_W / 2)
#define MAT(row)  ((uint8_t*)(0xCC00 + (uint16_t)(row) * 40))

#define MC_BORDER ((COLOR_LIGHTBLUE << 4) | COLOR_BLUE)
#define MC_TEXT   ((COLOR_WHITE << 4) | COLOR_BLUE)
#define MC_HI     ((COLOR_YELLOW << 4) | COLOR_BLUE)
#define MC_DIM    ((COLOR_GRAY2 << 4) | COLOR_BLUE)
#define MC_SHADOW (COLOR_GRAY1 << 4)     /* gray on black: dims chat */

/* Rows inside the panel */
#define R_TITLE   PN_TOP
#define R_SONG    (PN_TOP + 2)
#define R_AUTHOR  (PN_TOP + 3)
#define R_BAR     (PN_TOP + 5)
#define R_SIG     (PN_TOP + 7)
#define R_FOOT1   (PN_TOP + 9)
#define R_FOOT2   (PN_TOP + 10)
#define R_BOT     (PN_TOP + 11)

/* Text starts past the two-cell left border. Writing at 0 overwrote it
   and punched holes in the frame - the border IS reverse-space cells. */
#define TX 3

#define BAR_CELLS 28          /* progress bar width in cells */
#define MET_CELLS 16          /* voice-3 meter width */

static uint8_t pbuf[80];
static char jb_title[37];
static char jb_author[25];
static char jb_mood[13];
static char jb_cmd[21];       /* "/music <mood>" in PETSCII */
static uint16_t jb_elapsed;   /* seconds, carried forward locally */
static uint16_t jb_secs;      /* total, 0 = unknown */
static uint8_t jb_flags;      /* bit0 playing, bit1 favorite */
static uint8_t jb_loading;
static uint8_t jb_tick;       /* sys_ticks low byte at the last second */
static uint8_t jb_peak;       /* decaying signal-meter peak */

/* --- panel primitives (same approach as mod_menu.c) ------------------ */

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

static void pn_fill(void) {
    uint8_t i;
    pbuf[PN_COL] = pbuf[PN_COL + 1] = 0x20 | 0x80;
    for (i = PN_COL + 2; i < PN_COL + PN_W - 2; ++i) pbuf[i] = 0x20;
    pbuf[PN_COL + PN_W - 2] = pbuf[PN_COL + PN_W - 1] = 0x20 | 0x80;
}

static void pn_write(uint8_t x, const char* pet, uint8_t rev) {
    uint8_t i;
    x += PN_COL;
    for (i = 0; pet[i] && x < PN_COL + PN_W - 2; ++i, ++x) {
        pbuf[x] = ui_cell_from_petscii((uint8_t)pet[i]) | rev;
    }
}

/* Text straight off the wire is already ASCII, which IS the cell code */
static void pn_ascii(uint8_t x, const char* s) {
    uint8_t i;
    x += PN_COL;
    for (i = 0; s[i] && x < PN_COL + PN_W - 2; ++i, ++x) {
        pbuf[x] = (uint8_t)s[i];
    }
}

static void pn_bar_row(uint8_t row, const char* pet) {
    uint8_t i;
    for (i = PN_COL; i < PN_COL + PN_W; ++i) pbuf[i] = 0x20 | 0x80;
    if (pet) pn_write(4, pet, 0x80);
    pn_mat(row, MC_BORDER);
    soft80_span(row, pbuf, PN_PAIR, PN_PAIRS);
}

static void pn_line(uint8_t row, uint8_t interior) {
    pn_mat(row, interior);
    soft80_span(row, pbuf, PN_PAIR, PN_PAIRS);
}

static void pn_shadow(uint8_t row) {
    uint8_t* m = MAT(row);
    uint8_t i;
    if (row > PN_TOP + 17) return;
    for (i = PN_PAIR + 1; i < PN_PAIR + PN_PAIRS + 2; ++i) m[i] = MC_SHADOW;
}

/* --- formatting ------------------------------------------------------ */

/* Fixed-width "mm:ss" (5 chars + NUL) so elapsed and total line up
   under the bar instead of jittering as the minutes roll over. */
#define TIME_W 5
static void fmt_time(char* buf, uint16_t secs) {
    uint16_t m = secs / 60;
    uint8_t s = (uint8_t)(secs - m * 60);
    if (m > 99) m = 99;
    buf[0] = '0' + (uint8_t)(m / 10);
    buf[1] = '0' + (uint8_t)(m % 10);
    buf[2] = ':';
    buf[3] = '0' + (s / 10);
    buf[4] = '0' + (s % 10);
    buf[5] = 0;
}

/* Horizontal meter of `cells` cells, `on` of them lit. Lit cells are
   reverse-space; the rest are a dim middle dot, so an empty bar still
   reads as a track rather than as nothing. */
static void draw_meter(uint8_t x, uint8_t cells, uint8_t on) {
    uint8_t i;
    for (i = 0; i < cells; ++i) {
        pbuf[PN_COL + x + i] = (i < on) ? (0x20 | 0x80) : '.';
    }
}

/* --- rows ------------------------------------------------------------ */

/* Every panel row goes through one of these two, so none can be left
   undrawn: an undrawn row keeps the chat's black background and reads
   as a hole punched through the panel. */
static void pn_blank(uint8_t row) {
    pn_fill();
    pn_line(row, MC_TEXT);
}

static void pn_text(uint8_t row, const char* pet, uint8_t colour) {
    pn_fill();
    pn_write(TX, pet, 0);
    pn_line(row, colour);
}

static void jb_draw_song(void) {
    pn_fill();
    pn_ascii(TX, jb_title);
    pn_line(R_SONG, MC_HI);
    pn_fill();
    if (jb_author[0]) {
        pn_write(TX, S_BY, 0);
        pn_ascii(TX + 3, jb_author);
    }
    if (jb_flags & 2) pn_write(PN_W - 4 - 10, S_FAV, 0);
    pn_line(R_AUTHOR, MC_DIM);
}

static void jb_draw_bar(void) {
    char t[TIME_W + 1];
    uint8_t on = 0;
    uint8_t x = TX + BAR_CELLS + 2;
    pn_fill();
    if (jb_secs) {
        /* Deliberately 16-bit: a uint32 divide would drag cc65's long
           runtime into the RESIDENT image (~330 bytes of module-slot
           headroom, measured). elapsed is clamped to secs below, and
           the longest tune in the library is ~2000s, so the product
           stays inside 16 bits with room to spare. */
        on = (uint8_t)((jb_elapsed * (uint16_t)BAR_CELLS) / jb_secs);
        if (on > BAR_CELLS) on = BAR_CELLS;
    }
    draw_meter(TX, BAR_CELLS, on);
    fmt_time(t, jb_elapsed);
    pn_ascii(x, t);
    if (jb_secs) {
        pbuf[PN_COL + x + TIME_W] = '/';
        fmt_time(t, jb_secs);
        pn_ascii(x + TIME_W + 1, t);
    }
    pn_line(R_BAR, MC_TEXT);
}

static void jb_draw_sig(void) {
    uint8_t lvl = jb_peak >> 4;          /* 0..15 */
    pn_fill();
    pn_write(TX, S_SIG, 0);
    draw_meter(TX + 8, MET_CELLS, lvl);
    pn_line(R_SIG, MC_DIM);
}

/* Draws EVERY row, in order, on every path - the earlier version left
   PN_TOP+4 undrawn when nothing was playing, and that row showed the
   chat's black background straight through the panel. */
static void jb_draw(void) {
    pn_bar_row(R_TITLE, S_TITLE);
    pn_blank(PN_TOP + 1);
    if (jb_loading) {
        pn_text(R_SONG, S_FETCH, MC_DIM);
        pn_blank(R_AUTHOR);
    } else if (!(jb_flags & 1)) {
        pn_text(R_SONG, S_SILENT, MC_DIM);
        pn_text(R_AUTHOR, S_HINT, MC_DIM);
    } else {
        jb_draw_song();
    }
    pn_blank(PN_TOP + 4);
    jb_draw_bar();
    pn_blank(PN_TOP + 6);
    jb_draw_sig();
    pn_blank(PN_TOP + 8);
    pn_text(R_FOOT1, S_FOOT1, MC_DIM);
    pn_text(R_FOOT2, S_FOOT2, MC_DIM);
    pn_bar_row(R_BOT, 0);
    pn_shadow(R_BOT + 1);
}

/* --- wire ------------------------------------------------------------ */

/* [flags][elapsed:2][secs:2] then title\0 author\0 mood\0, all ASCII */
static void jb_parse(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint16_t off = 5;
    uint8_t t;
    if (plen < 5) return;
    jb_flags = p[0];
    jb_elapsed = p[1] | ((uint16_t)p[2] << 8);
    jb_secs = p[3] | ((uint16_t)p[4] << 8);
    t = 0;
    while (off < plen && p[off] && t < sizeof(jb_title) - 1)
        jb_title[t++] = p[off++];
    jb_title[t] = 0;
    ++off;
    t = 0;
    while (off < plen && p[off] && t < sizeof(jb_author) - 1)
        jb_author[t++] = p[off++];
    jb_author[t] = 0;
    ++off;
    t = 0;
    while (off < plen && p[off] && t < sizeof(jb_mood) - 1)
        jb_mood[t++] = p[off++];
    jb_mood[t] = 0;
    jb_loading = 0;
}

static uint8_t jb_msg(uint8_t t) {
    if (t == MSG_NOWPLAYING) {
        jb_parse();
        jb_tick = sys_ticks[0];
        jb_draw();
        return 1;
    }
    return 0;
}

/* --- tick ------------------------------------------------------------ */

/* Called from the resident main loop. Advances the clock off the 60Hz
   counter instead of re-asking the server every second, and samples the
   SID's voice-3 oscillator for the meter (a genuine reading of what the
   chip is doing, with a slow decay so it moves rather than flickers). */
static void jb_ticker(void) {
    uint8_t now = sys_ticks[0];
    uint8_t lvl = SID_OSC3;
    if (lvl > jb_peak) jb_peak = lvl;
    else if (jb_peak > 6) jb_peak -= 6;
    else jb_peak = 0;

    /* sys_ticks is 16-bit at ~60Hz; the low byte wraps every ~4.3s, so
       compare deltas rather than absolute values. */
    if ((uint8_t)(now - jb_tick) >= 60) {
        jb_tick = (uint8_t)(jb_tick + 60);
        if (music_state == 0xFF) {
            ++jb_elapsed;
            if (jb_secs && jb_elapsed > jb_secs) jb_elapsed = jb_secs;
            jb_draw_bar();
        }
    }
    jb_draw_sig();
}

/* --- keys ------------------------------------------------------------ */

/* No volume control here, deliberately. $D418 is a single global
   register that the tune's own play routine writes; 4187 of the 10032
   tunes have no vol_byte at all, so the client leaves that register
   entirely to them. Setting music_ext_vol from here would start
   overwriting it 60 times a second with a zero filter nibble, fighting
   the tune - which on real hardware is a loud buzz, not a volume
   change. The loudness pass already normalizes across the library. */

static void jb_key(uint8_t k) {
    uint8_t v;
    if (k == KEY_F1 || k == JB_STOP) {
        mod_modal_end();
        ui_status(S_READY);
        return;
    }
    switch (petscii_to_ascii(k)) {
        case 'f':
            if (jb_flags & 1) {
                jb_flags ^= 2;          /* optimistic: server confirms */
                proto_send_message(MSG_FAV_TUNE, 0, 0);
                jb_draw_song();
            }
            return;
        case 'n':
            /* Ask the proxy for another tune in the same mood. Closing
               first is the module rule: the resident loop dispatches the
               command once this code is off the call stack. */
            if (jb_mood[0]) {
                uint8_t i;
                /* S_NEXT is a cc65 literal, so it is already PETSCII -
                   which is what menu_pcmd is sent as. The mood came off
                   the wire as ASCII and does need converting. */
                for (i = 0; S_NEXT[i]; ++i) jb_cmd[i] = S_NEXT[i];
                for (v = 0; jb_mood[v] && i < sizeof(jb_cmd) - 1; ++v, ++i)
                    jb_cmd[i] = ascii_to_petscii((uint8_t)jb_mood[v]);
                jb_cmd[i] = 0;
                mod_modal_end();
                menu_pcmd = jb_cmd;
            }
            return;
    }
}

void mod_sound_run(void) {
    jb_title[0] = 0;
    jb_author[0] = 0;
    jb_mood[0] = 0;
    jb_elapsed = 0;
    jb_secs = 0;
    jb_flags = 0;
    jb_peak = 0;
    jb_loading = 1;
    jb_tick = sys_ticks[0];
    menu_action = 0;
    menu_pcmd = 0;
    mod_modal_begin(jb_msg, jb_key);
    mod_modal_tick(jb_ticker);
    proto_send_message(MSG_GET_NOWPLAYING, 0, 0);
    jb_draw();
}

#pragma bss-name (pop)
#pragma rodata-name (pop)
#pragma code-name (pop)

#endif /* SOFT80 */
