/**
 * C64 LLM Client - resident API for overlay modules (SOFT80)
 *
 * Module-modal framework: a loaded overlay can take over the screen,
 * keyboard and selected protocol messages while the resident main
 * loop keeps running - serial frames still flow, so a module can hold
 * a live request/response dialog with the proxy (the conversation
 * manager does). Contrast with a blocking module like the config
 * editor, which owns the CPU until it returns.
 *
 * The msg hook gets first chance at every received message and
 * returns nonzero to consume it; the key hook gets every key while
 * the modal is open. mod_modal_end() restores the chat screen.
 */

#ifndef MODAPI_H
#define MODAPI_H

#include "common.h"

extern uint8_t (*mod_msg_hook)(uint8_t msg_type);
extern void (*mod_key_hook)(uint8_t key);
/* Optional: called from the resident main loop while the modal is open,
   for anything that has to keep moving with no key or frame to drive it
   (the jukebox's progress bar and signal meter). Set it AFTER
   mod_modal_begin, which clears it; mod_modal_end clears it too, so a
   module can never be ticked once its code may have been overwritten. */
extern void (*mod_tick_hook)(void);

void mod_modal_begin(uint8_t (*msg)(uint8_t), void (*key)(uint8_t));
void mod_modal_tick(void (*tick)(void));
void mod_modal_end(void);

/* Shared conversation-list state, filled by the resident parser
   conv_list_frame() from CONVERSATION_LIST frames */
#define MAX_CONVS 17
typedef struct {
    uint32_t id;
    char title[37];  /* PETSCII, null-terminated */
} ConvEntry;
extern ConvEntry convs[MAX_CONVS];
extern uint8_t conv_count;
extern uint8_t conv_sel;
extern uint8_t conv_loading;
extern uint8_t conv_more_pages;

void conv_list_frame(void);

/* Kick off a conversation load (clears chat, freezes rendering, arms
   the watchdog); the stream is handled by the resident dispatcher */
void load_conversation_by_id(uint32_t id);

/* --- server-fed menu (module #4) ------------------------------------ */

/* Menu entries reuse the convs[] storage byte-for-byte: the menu and
   the conversation manager are alternatives in the same overlay slot,
   so their list state can never coexist. key/label are raw ASCII off
   the wire (ASCII is the soft-80 cell encoding); cmd is converted to
   PETSCII on receipt so it can be sent like typed text. */
typedef struct {
    uint8_t key;     /* ASCII hotkey */
    char label[29];
    char cmd[11];    /* PETSCII; "!x" = local action x */
} MenuEntry;
#define menu_entries ((MenuEntry*)(void*)convs)
/* 13 fits: MenuEntry is the same 41 bytes as ConvEntry and convs[] holds
   17, while the panel's last row still lands inside the chat area. */
#define MAX_MENU 13

/* Set by the menu module before mod_modal_end(); the RESIDENT loop
   dispatches after the key hook returns. A local action may load
   another module into the slot - the menu's own code must already be
   off the call stack by then. */
extern uint8_t menu_action;      /* local action letter, 0 = none */
extern const char* menu_pcmd;    /* proxy command to send, 0 = none */

#endif /* MODAPI_H */
