/**
 * C64 LLM Client - config editor (overlay module #1)
 *
 * Edits the proxy address and saves it to c64llm.cfg on device 8.
 * Runs at boot when no valid config exists, and from the F1 menu (E).
 *
 * All code here lives in segment OVERLAY1: it is NOT part of the
 * resident PRG but loaded into the module slot on demand by
 * module_load("c64llm.1"). It calls resident helpers (ui_*, cfg)
 * directly - the single link resolves them. Its statics land in
 * resident BSS/RODATA, so keep them lean.
 */

#ifdef SOFT80

#include <string.h>
#include <conio.h>
#include <c64.h>
#include "common.h"
#include "cfg.h"
#include "ui.h"

void mod_config_run(void);

#pragma code-name (push, "OVERLAY1")
#pragma rodata-name (push, "OVERLAY1")
/* Slot RAM past the loaded code: zero resident bytes, zero file bytes,
   and NOT zero-initialized - mod_config_run stores before reading. */
#pragma bss-name (push, "OVL1BSS")

/* Named, not anonymous: cc65 emits anonymous literals into "RODATA"
   whatever the pragma says, and they were sitting resident. */
static const char S_PORT[] = "  Port: ";
static const char S_HOST[] = "  Host: ";
static const char S_BAUD[] = "  Speed: ";
static const char S_TITLE[] = "  Server Config";
static const char S_H1[] = "  return: next field / save";
static const char S_H2[] = "  stop:   keep current, no save";
static const char S_HB[] = "  crsr up/down picks a field;";
static const char S_HB2[] = "  a key cycles Speed";
static const char S_H3[] = "  (c64llm.cfg on drive 8)";
static const char S_EDIT[] = "Edit the proxy address.";
static const char S_UNCHANGED[] = "Config unchanged.";
static const char S_EMPTY[] = "Empty field - config unchanged.";
static const char S_SAVED[] = "Config saved.";
static const char S_SAVEFAIL[] = "Save failed - drive 8 present?";

#define FLD_HOST 0
#define FLD_PORT 1
#define FLD_BAUD 2
#define N_FLD    3
#define MODKEY_STOP 3

static char fld[2][CFG_HOST_MAX];
static uint8_t flen[2];
static uint8_t fsel;
static uint8_t bidx;    /* working copy of g_baud_idx */

/* Hardware-rate labels (nominal x2 on a SwiftLink/C64U crystal). */
static const char S_B9600[]  = "9600 baud";
static const char S_B19200[] = "19200 baud";
static const char S_B38400[] = "38400 baud";
static const char* const baud_txt[3] = { S_B9600, S_B19200, S_B38400 };

static void field_draw(uint8_t idx) {
    char line[CFG_HOST_MAX + 12];
    uint8_t n = 0;
    uint8_t i;
    const char* lab;
    uint8_t hi = fsel == idx;

    if (idx == FLD_BAUD) {
        const char* v = baud_txt[bidx > BAUD_IDX_38400 ? BAUD_IDX_19200 : bidx];
        for (i = 0; S_BAUD[i]; ++i) line[n++] = S_BAUD[i];
        for (i = 0; v[i]; ++i) line[n++] = v[i];
        line[n] = 0;
        ui_draw_row(8, line, hi ? COLOR_WHITE : COLOR_CYAN, hi);
        return;
    }
    lab = idx ? S_PORT : S_HOST;
    for (i = 0; lab[i]; ++i) line[n++] = lab[i];
    for (i = 0; i < flen[idx]; ++i) line[n++] = fld[idx][i];
    line[n] = 0;
    ui_draw_row(4 + idx * 2, line, hi ? COLOR_WHITE : COLOR_CYAN, hi);
}

static void fields_draw(void) {
    field_draw(FLD_HOST);
    field_draw(FLD_PORT);
    field_draw(FLD_BAUD);
}

static uint8_t field_printable(uint8_t c) {
    return c >= 0x20 && c <= 0x5F;
}

void mod_config_run(void) {
    uint8_t k;

    chat_area_clear_screen();
    ui_draw_row(2,  S_TITLE, COLOR_WHITE, 0);
    ui_draw_row(10, S_H1, COLOR_GRAY2, 0);
    ui_draw_row(11, S_H2, COLOR_GRAY2, 0);
    ui_draw_row(12, S_HB, COLOR_GRAY2, 0);
    ui_draw_row(13, S_HB2, COLOR_GRAY2, 0);
    ui_draw_row(15, S_H3, COLOR_GRAY2, 0);

    strcpy(fld[FLD_HOST], g_host);
    strcpy(fld[FLD_PORT], g_port);
    flen[FLD_HOST] = (uint8_t)strlen(g_host);
    flen[FLD_PORT] = (uint8_t)strlen(g_port);
    bidx = g_baud_idx;
    fsel = FLD_HOST;
    fields_draw();
    ui_status(S_EDIT);

    for (;;) {
        k = cgetc();
        if (k == MODKEY_STOP) {
            ui_status(S_UNCHANGED);
            return;
        }
        if (k == KEY_RETURN) {
            /* return advances host->port, then saves. Baud is not in
               the return chain (kept the two-return save the muscle
               memory and the e2e both rely on); reach it with the
               cursor. */
            if (fsel == FLD_HOST) {
                fsel = FLD_PORT;
                fields_draw();
            } else {
                break;  /* save */
            }
        } else if (k == KEY_CRSR_DOWN) {
            fsel = (fsel + 1) % N_FLD;
            fields_draw();
        } else if (k == KEY_CRSR_UP) {
            fsel = (uint8_t)((fsel + N_FLD - 1) % N_FLD);
            fields_draw();
        } else if (fsel == FLD_BAUD) {
            /* No free text on the speed field: any key cycles it, up to
               the build's ceiling (38400 only on a BAUD38400 build). */
            bidx = (bidx >= BAUD_IDX_MAX) ? 0 : (uint8_t)(bidx + 1);
            field_draw(FLD_BAUD);
        } else if (k == KEY_DEL) {
            if (flen[fsel]) {
                --flen[fsel];
                field_draw(fsel);
            }
        } else if (field_printable(k)) {
            /* port stays short; host uses the full field */
            uint8_t max = fsel ? CFG_PORT_MAX - 1 : CFG_HOST_MAX - 1;
            if (flen[fsel] < max) {
                fld[fsel][flen[fsel]++] = (char)k;
                field_draw(fsel);
            }
        }
    }

    fld[FLD_HOST][flen[FLD_HOST]] = 0;
    fld[FLD_PORT][flen[FLD_PORT]] = 0;
    if (!flen[FLD_HOST] || !flen[FLD_PORT]) {
        ui_status(S_EMPTY);
        return;
    }
    strcpy(g_host, fld[FLD_HOST]);
    strcpy(g_port, fld[FLD_PORT]);
    g_baud_idx = bidx;
    baud_apply();       /* next acia_init_hw brings the link up at bidx */
    ui_status(config_save()
              ? S_SAVED
              : S_SAVEFAIL);
}

#pragma bss-name (pop)
#pragma rodata-name (pop)
#pragma code-name (pop)

#endif /* SOFT80 */
