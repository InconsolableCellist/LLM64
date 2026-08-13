/**
 * LLM64 Client - conversation manager (overlay module #2)
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
#pragma rodata-name (push, "OVERLAY2")
/* Module statics live in slot RAM past the loaded code: zero resident
   bytes and zero file bytes, but NOT zero-initialized - mod_convmgr_run
   stores every one before it is read. */
#pragma bss-name (push, "OVL2BSS")

/* cc65 emits ANONYMOUS string literals into "RODATA" whatever the
   pragma says; named const arrays honor it. Every user-visible string
   here is named for that reason alone - as literals they sat resident
   and cost scrollback. */
static const char S_HEAD[]   = " Conversations (return=load, d=del,"
                               " s=star, f5=close)";
static const char S_LOADING[] = "  loading...";
static const char S_NONE[]   = "  (none found)";
static const char S_PAGE[]   = " page ??   crsr left/right = page";
static const char S_DELETED[] = "Deleted.";
static const char S_STARRED[] = "Star toggled.";
static const char S_REFUSED[] = "Server refused - try again.";
static const char S_DELETING[] = "Deleting...";
static const char S_DELCANC[] = "Delete canceled.";
static const char S_DELCONF[] = "Delete selected conversation? y = yes";
static const char S_STARING[] = "Toggling star...";
static const char S_READY[]  = "Ready.";
static const char S_RETRYING[] = "No reply - retrying...";
static const char S_NOREPLY[] = "No reply - close and reopen (f5).";

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

/* Request timeout. Nothing here goes through the resident response
   watchdog - that one only runs while the chat state machine is busy,
   and a manager request leaves it idle - so a reply lost on the wire
   (the C64U bridge drops burst tails) parked the modal at
   'loading...' forever with no way out. ~15s: long enough that the
   resident loop has already resynced a parser left mid-frame by a
   truncated reply (STALL_UNITS in main.c), so the retry does not feed
   its own reply to a jammed parser. */
extern volatile uint8_t sys_ticks[2];
#define REQ_UNITS  4      /* * ~4.3s (sys_ticks high byte) */
static uint8_t req_at;    /* tick at the last request */
static uint8_t req_retry; /* the automatic retry is already spent */

static void mgr_row(uint8_t i) {
    ui_draw_row(ROW_FIRST + i, convs[i].title,
                convs[i].title[0] == '*' ? COLOR_YELLOW : COLOR_CYAN,
                i == conv_sel);
}

static void mgr_draw(void) {
    uint8_t i;
    char foot[36];
    chat_area_clear_screen();
    ui_draw_row(1, S_HEAD, COLOR_WHITE, 0);
    if (conv_loading) {
        ui_draw_row(3, S_LOADING, COLOR_GRAY2, 0);
        return;
    }
    if (!conv_count) {
        ui_draw_row(3, S_NONE, COLOR_GRAY2, 0);
    }
    for (i = 0; i < conv_count; ++i) mgr_row(i);
    if (page || conv_more_pages) {
        strcpy(foot, S_PAGE);
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
    req_at = sys_ticks[1];
    proto_send_message(MSG_LIST_CONVERSATIONS, &page, 1);
}

/* A request the USER asked for: spends a fresh automatic retry. The
   retry itself goes through mgr_request, so it can never re-arm. */
static void mgr_fresh(void) {
    req_retry = 0;
    mgr_request();
}

/* Called from the resident main loop while the modal is open. */
static void mgr_tick(void) {
    if (!conv_loading && !pend) return;
    if ((uint8_t)(sys_ticks[1] - req_at) < REQ_UNITS) return;
    req_at = sys_ticks[1];
    if (pend) {
        /* Never repeat a delete or a star on its own: the server may
           have done the work and lost only the ACK. Say so and let the
           list refresh show what actually happened. */
        pend = OP_NONE;
        ui_status(S_NOREPLY);
        return;
    }
    if (!req_retry) {
        req_retry = 1;
        mgr_request();
        ui_status(S_RETRYING);
        return;
    }
    conv_loading = 0;   /* stop claiming to be loading */
    mgr_draw();
    ui_status(S_NOREPLY);
}

static uint8_t mgr_msg(uint8_t t) {
    if (t == MSG_CONVERSATION_LIST) {
        conv_list_frame();
        if (!conv_loading) {
            /* deleting the last entry of a tail page: step back */
            if (!conv_count && page) {
                --page;
                mgr_fresh();
            }
            mgr_draw();
        }
        return 1;
    }
    if (pend && (t == MSG_ACK || t == MSG_NAK)) {
        if (t == MSG_ACK) {
            ui_status(pend == OP_DELETE ? S_DELETED : S_STARRED);
            pend = OP_NONE;
            mgr_fresh();
            mgr_draw();
        } else {
            pend = OP_NONE;
            ui_status(S_REFUSED);
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
            req_at = sys_ticks[1];
            proto_send_message(MSG_DELETE_CONVERSATION,
                               (uint8_t*)&convs[conv_sel].id, 4);
            ui_status(S_DELETING);
        } else {
            ui_status(S_DELCANC);
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
                mgr_fresh();
                mgr_draw();
            }
            break;
        case KEY_CRSR_RIGHT:
            if (conv_more_pages) {
                ++page;
                mgr_fresh();
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
                ui_status(S_DELCONF);
            }
            break;
        case 's':
            if (conv_count && !pend) {
                pend = OP_STAR;
                proto_send_message(MSG_STAR_CONVERSATION,
                                   (uint8_t*)&convs[conv_sel].id, 4);
                ui_status(S_STARING);
            }
            break;
        case KEY_F5:
        case MK_STOP:
            mod_modal_end();
            ui_status(S_READY);
            break;
    }
}

void mod_convmgr_run(void) {
    page = 0;
    pend = OP_NONE;
    del_arm = 0;
    mod_modal_begin(mgr_msg, mgr_key);
    mod_modal_tick(mgr_tick);   /* AFTER begin, which clears the hook */
    mgr_fresh();
    mgr_draw();
}

#pragma bss-name (pop)
#pragma rodata-name (pop)
#pragma code-name (pop)

#endif /* SOFT80 */
