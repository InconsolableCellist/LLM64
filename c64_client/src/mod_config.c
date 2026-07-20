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

#define FLD_HOST 0
#define FLD_PORT 1
#define MODKEY_STOP 3

static char fld[2][CFG_HOST_MAX];
static uint8_t flen[2];
static uint8_t fsel;

static void field_draw(uint8_t idx) {
    char line[CFG_HOST_MAX + 10];
    uint8_t n = 0;
    uint8_t i;
    const char* lab = idx ? "  Port: " : "  Host: ";

    for (i = 0; lab[i]; ++i) line[n++] = lab[i];
    for (i = 0; i < flen[idx]; ++i) line[n++] = fld[idx][i];
    line[n] = 0;
    ui_draw_row(4 + idx * 2, line,
                fsel == idx ? COLOR_WHITE : COLOR_CYAN, fsel == idx);
}

static uint8_t field_printable(uint8_t c) {
    return c >= 0x20 && c <= 0x5F;
}

void mod_config_run(void) {
    uint8_t k;

    chat_area_clear_screen();
    ui_draw_row(2,  "  Server Config", COLOR_WHITE, 0);
    ui_draw_row(8,  "  return: next field / save", COLOR_GRAY2, 0);
    ui_draw_row(9,  "  stop:   keep current, no save", COLOR_GRAY2, 0);
    ui_draw_row(11, "  (c64llm.cfg on drive 8)", COLOR_GRAY2, 0);

    strcpy(fld[FLD_HOST], g_host);
    strcpy(fld[FLD_PORT], g_port);
    flen[FLD_HOST] = (uint8_t)strlen(g_host);
    flen[FLD_PORT] = (uint8_t)strlen(g_port);
    fsel = FLD_HOST;
    field_draw(FLD_HOST);
    field_draw(FLD_PORT);
    ui_status("Edit the proxy address.");

    for (;;) {
        k = cgetc();
        if (k == MODKEY_STOP) {
            ui_status("Config unchanged.");
            return;
        }
        if (k == KEY_RETURN) {
            if (fsel == FLD_HOST) {
                fsel = FLD_PORT;
                field_draw(FLD_HOST);
                field_draw(FLD_PORT);
            } else {
                break;  /* save */
            }
        } else if (k == KEY_CRSR_DOWN || k == KEY_CRSR_UP) {
            fsel ^= 1;
            field_draw(FLD_HOST);
            field_draw(FLD_PORT);
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
        ui_status("Empty field - config unchanged.");
        return;
    }
    strcpy(g_host, fld[FLD_HOST]);
    strcpy(g_port, fld[FLD_PORT]);
    ui_status(config_save()
              ? "Config saved."
              : "Save failed - drive 8 present?");
}

#pragma code-name (pop)

#endif /* SOFT80 */
