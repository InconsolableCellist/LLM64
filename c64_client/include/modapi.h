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

void mod_modal_begin(uint8_t (*msg)(uint8_t), void (*key)(uint8_t));
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

#endif /* MODAPI_H */
