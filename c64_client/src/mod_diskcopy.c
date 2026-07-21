/**
 * C64 LLM Client - disk copy (overlay module #3)
 *
 * Copies this distribution's files (client, modules, config) from the
 * boot drive to another drive over the IEC bus - the machine
 * replicates its own disk onto real media, no external copier needed.
 * File-level copy via KERNAL channels: read "name,p,r" on the source,
 * write "@0:name,p,w" on the target, streamed through the $B000 SID
 * window (music is stopped first; the caller holds serial RX paused
 * for the whole run - IEC transfers and serial NMIs don't mix).
 *
 * Blocking module (config-editor style): owns the CPU until it
 * returns. Nothing arrives on serial while the proxy is idle.
 */

#ifdef SOFT80

#include <string.h>
#include <conio.h>
#include <c64.h>
#include <cbm.h>
#include "common.h"
#include "cfg.h"
#include "ui.h"

/* music.s */
void music_ext_stop(void);

void mod_diskcopy_run(void);

#pragma code-name (push, "OVERLAY3")

#define COPYBUF     ((uint8_t*)0xB000)
#define COPYBUF_SZ  4096U
#define LFN_SRC     4
#define LFN_DST     5
#define DK_STOP     3

#define DK_NFILES 5
static const char* const dk_files[DK_NFILES] = {
    "c64llm", "c64llm.1", "c64llm.2", "c64llm.3", "c64llm.cfg"
};

/* 0 = copied, 1 = failed, 2 = not on source (skipped) */
static uint8_t copy_one(const char* name, uint8_t dst, uint8_t row) {
    char nbuf[28];
    char line[44];
    int n;
    uint8_t blocks = 0;

    strcpy(line, "  ");
    strcat(line, name);
    ui_draw_row(row, line, COLOR_CYAN, 0);

    /* A drive accepts an OPEN for a missing file and only errors on
       the first read - so read the first chunk BEFORE opening the
       target (also detects an unreadable source cleanly) */
    strcpy(nbuf, name);
    strcat(nbuf, ",p,r");
    if (cbm_open(LFN_SRC, boot_device, 2, nbuf) != 0) n = -1;
    else n = cbm_read(LFN_SRC, COPYBUF, COPYBUF_SZ);
    if (n <= 0) {
        cbm_close(LFN_SRC);
        strcat(line, " - not found, skipped");
        ui_draw_row(row, line, COLOR_GRAY2, 0);
        return 2;
    }
    strcpy(nbuf, "@0:");
    strcat(nbuf, name);
    strcat(nbuf, ",p,w");
    if (cbm_open(LFN_DST, dst, 3, nbuf) != 0) {
        cbm_close(LFN_DST);
        cbm_close(LFN_SRC);
        strcat(line, " - target drive error");
        ui_draw_row(row, line, COLOR_RED, 0);
        return 1;
    }
    while (n > 0) {
        if (cbm_write(LFN_DST, COPYBUF, (unsigned)n) != n) {
            cbm_close(LFN_DST);
            cbm_close(LFN_SRC);
            strcat(line, " - write failed");
            ui_draw_row(row, line, COLOR_RED, 0);
            return 1;
        }
        /* coarse progress: one tick per 4KB chunk */
        ++blocks;
        line[0] = '0' + blocks / 10;
        line[1] = '0' + blocks % 10;
        ui_draw_row(row, line, COLOR_CYAN, 0);
        n = cbm_read(LFN_SRC, COPYBUF, COPYBUF_SZ);
    }
    cbm_close(LFN_DST);
    cbm_close(LFN_SRC);
    strcat(line, " - ok");
    ui_draw_row(row, line, COLOR_LIGHTGREEN, 0);
    return 0;
}

void mod_diskcopy_run(void) {
    uint8_t k, dst, i, copied = 0, failed = 0;
    char line[44];

    chat_area_clear_screen();
    ui_draw_row(2, "  Copy Client Disk", COLOR_WHITE, 0);
    strcpy(line, "  Source: drive ??  (this boot disk)");
    line[16] = '0' + boot_device / 10;
    line[17] = '0' + boot_device % 10;
    if (line[16] == '0') line[16] = ' ';
    ui_draw_row(4, line, COLOR_CYAN, 0);
    ui_draw_row(6, "  Target drive (8 or 9)?  stop cancels", COLOR_CYAN, 0);
    ui_status("Pick the target drive.");

    for (;;) {
        k = cgetc();
        if (k == DK_STOP) {
            ui_status("Copy cancelled.");
            return;
        }
        if (k == '8' || k == '9') {
            dst = k - '0';
            if (dst == boot_device) {
                ui_status("That IS the source drive - pick the other.");
                continue;
            }
            break;
        }
    }

    /* $B000 becomes the copy buffer: silence any streamed tune */
    music_ext_stop();
    ui_status("Copying... (drive lights will blink)");

    for (i = 0; i < DK_NFILES; ++i) {
        k = copy_one(dk_files[i], dst, 8 + i);
        if (k == 0) ++copied;
        else if (k == 1) ++failed;
    }

    if (failed) {
        strcpy(line, "Copy finished with errors - check target disk.");
    } else {
        strcpy(line, "Copy complete - ? file(s) written.");
        line[16] = '0' + copied;
    }
    ui_draw_row(14, "  any key returns to chat", COLOR_GRAY2, 0);
    ui_status(line);
    while (!kbhit());
    cgetc();
}

#pragma code-name (pop)

#endif /* SOFT80 */
