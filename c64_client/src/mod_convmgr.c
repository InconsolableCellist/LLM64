/**
 * C64 LLM Client - conversation manager (overlay module #2)
 *
 * Full conversation management from the C64: paged list (16 per page,
 * starred entries first with a '*' prefix, star = yellow), load,
 * delete with confirm, star toggle. Replaces the resident F5 browser
 * in SOFT80 builds.
 *
 * Lives in segment OVERLAY2 (same slot as the config editor - only
 * one module is loaded at a time). Runs as a HOOK-DRIVEN modal: the
 * resident main loop keeps pumping serial and feeds us messages and
 * keys via mod_msg_hook/mod_key_hook (modapi.h), so the list arrives
 * and refreshes live while the modal is open.
 */

#ifdef SOFT80

#include <string.h>
#include <c64.h>
#include "common.h"
#include "modapi.h"
#include "ui.h"
#include "protocol.h"

void mod_convmgr_run(void);

#pragma code-name (push, "OVERLAY2")

#define MK_STOP    3
#define ROW_FIRST  2   /* first list row (header is row 1) */
#define ROW_FOOTER 19

/* pending server op awaiting ACK/NAK */
#define OP_NONE   0
#define OP_DELETE 1
#define OP_STAR   2

static uint8_t page;
static uint8_t pend;
static uint8_t del_arm;   /* next key confirms the delete */

static void mgr_row(uint8_t i) {
    ui_draw_row(ROW_FIRST + i, convs[i].title,
                convs[i].title[0] == '*' ? COLOR_YELLOW : COLOR_CYAN,
                i == conv_sel);
}

static void mgr_draw(void) {
    uint8_t i;
    char foot[36];
    chat_area_clear_screen();
    ui_draw_row(1, " Conversations (return=load, d=del, s=star,"
                   " f5=close)", COLOR_WHITE, 0);
    if (conv_loading) {
        ui_draw_row(3, "  loading...", COLOR_GRAY2, 0);
        return;
    }
    if (!conv_count) {
        ui_draw_row(3, "  (none found)", COLOR_GRAY2, 0);
    }
    for (i = 0; i < conv_count; ++i) mgr_row(i);
    if (page || conv_more_pages) {
        strcpy(foot, " page ??   crsr left/right = page");
        foot[6] = '0' + (page + 1) / 10;
        foot[7] = '0' + (page + 1) % 10;
        if (foot[6] == '0') foot[6] = ' ';
        ui_draw_row(ROW_FOOTER, foot, COLOR_GRAY2, 0);
    }
}

static void mgr_request(void) {
    conv_count = 0;
    conv_sel = 0;
    conv_loading = 1;
    proto_send_message(MSG_LIST_CONVERSATIONS, &page, 1);
}

static uint8_t mgr_msg(uint8_t t) {
    if (t == MSG_CONVERSATION_LIST) {
        conv_list_frame();
        if (!conv_loading) {
            /* deleting the last entry of a tail page: step back */
            if (!conv_count && page) {
                --page;
                mgr_request();
            }
            mgr_draw();
        }
        return 1;
    }
    if (pend && (t == MSG_ACK || t == MSG_NAK)) {
        if (t == MSG_ACK) {
            ui_status(pend == OP_DELETE ? "Deleted." : "Star toggled.");
            pend = OP_NONE;
            mgr_request();
            mgr_draw();
        } else {
            pend = OP_NONE;
            ui_status("Server refused - try again.");
        }
        return 1;
    }
    return 0;
}

static void mgr_key(uint8_t k) {
    uint8_t o;
    if (del_arm) {
        del_arm = 0;
        if (k == 'y') {
            pend = OP_DELETE;
            proto_send_message(MSG_DELETE_CONVERSATION,
                               (uint8_t*)&convs[conv_sel].id, 4);
            ui_status("Deleting...");
        } else {
            ui_status("Delete cancelled.");
        }
        return;
    }
    switch (k) {
        case KEY_CRSR_UP:
            if (conv_sel) {
                o = conv_sel--;
                mgr_row(o);
                mgr_row(conv_sel);
            }
            break;
        case KEY_CRSR_DOWN:
            if (conv_sel + 1 < conv_count) {
                o = conv_sel++;
                mgr_row(o);
                mgr_row(conv_sel);
            }
            break;
        case KEY_CRSR_LEFT:
            if (page) {
                --page;
                mgr_request();
                mgr_draw();
            }
            break;
        case KEY_CRSR_RIGHT:
            if (conv_more_pages) {
                ++page;
                mgr_request();
                mgr_draw();
            }
            break;
        case KEY_RETURN:
            if (conv_count && !pend) {
                uint32_t id = convs[conv_sel].id;
                mod_modal_end();
                load_conversation_by_id(id);
            }
            break;
        case 'd':
            if (conv_count && !pend) {
                del_arm = 1;
                ui_status("Delete selected conversation? y = yes");
            }
            break;
        case 's':
            if (conv_count && !pend) {
                pend = OP_STAR;
                proto_send_message(MSG_STAR_CONVERSATION,
                                   (uint8_t*)&convs[conv_sel].id, 4);
                ui_status("Toggling star...");
            }
            break;
        case KEY_F5:
        case MK_STOP:
            mod_modal_end();
            ui_status("Ready.");
            break;
    }
}

void mod_convmgr_run(void) {
    page = 0;
    pend = OP_NONE;
    del_arm = 0;
    mod_modal_begin(mgr_msg, mgr_key);
    mgr_request();
    mgr_draw();
}

#pragma code-name (pop)

#endif /* SOFT80 */
