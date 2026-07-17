/**
 * C64 LLM Client - interactive TUI
 *
 * Full-screen chat interface: scrollable chat area (rows 1-19), 3-row
 * input editor (21-23), status bar (24). Keys: F1/Return send, F2 new
 * conversation, F3 cancel a streaming reply, F5 conversation browser,
 * F7 help, cursor up/down scroll the chat area.
 *
 * Built out when DEBUG_CLIENT is defined (debug_main.c takes over).
 */

#ifndef DEBUG_CLIENT

#include <string.h>
#include <conio.h>
#include <c64.h>
#include "common.h"
#include "serial.h"
#include "protocol.h"
#include "text.h"
#include "ui.h"
#include "editor.h"

#ifndef SERVER_IP
#define SERVER_IP   "192.168.1.39"
#endif
#ifndef SERVER_PORT
#define SERVER_PORT "6400"
#endif

#define KEY_F2 137
#define KEY_STOP 3

/* App state */
#define ST_IDLE      0
#define ST_WAITING   1  /* message sent, reply not started */
#define ST_STREAMING 2

/* Modal overlays */
#define MODAL_NONE 0
#define MODAL_CONV 1
#define MODAL_HELP 2

static ProtoContext proto;
static uint8_t payload_buffer[MAX_PAYLOAD];

static uint8_t state = ST_IDLE;
static uint8_t modal = MODAL_NONE;

/* What the next ACK acknowledges */
#define PA_NONE    0
#define PA_NEWCONV 1
#define PA_CANCEL  2
static uint8_t pending_ack = PA_NONE;

/* Conversation browser */
#define MAX_CONVS 17
typedef struct {
    uint32_t id;
    char title[37];  /* PETSCII, null-terminated */
} ConvEntry;
static ConvEntry convs[MAX_CONVS];
static uint8_t conv_count;
static uint8_t conv_sel;
static uint8_t conv_loading;

/* ------------------------------------------------------------------ */

static void pump_serial(void);

/* Busy-wait roughly n "ticks" while still draining serial */
static uint8_t wait_for_ack(uint16_t tries) {
    uint16_t t;
    uint8_t i;
    for (t = 0; t < tries; ++t) {
        while (serial_available()) {
            if (proto_process_byte(&proto, serial_read()) == MSG_ACK) {
                return 1;
            }
        }
        for (i = 0; i < 200; ++i);
    }
    return 0;
}

#ifndef CONNECT_DIRECT
/* Minimal Hayes dial: send command, wait for terminator, check result */
static uint8_t at_command(const char* cmd, char* resp, uint8_t max_len) {
    uint8_t i;
    uint16_t idle = 0;
    uint8_t n = 0;

    for (i = 0; cmd[i]; ++i) {
        while (!serial_can_write());
        serial_write(petscii_to_ascii((uint8_t)cmd[i]));
    }
    while (!serial_can_write());
    serial_write(13);
    serial_flush();

    while (idle < 12000 && n < max_len - 1) {
        if (serial_available()) {
            uint8_t b = serial_read();
            idle = 0;
            if (b >= 32 && b < 127) {
                resp[n++] = ascii_to_petscii(b);
            }
        } else {
            ++idle;
        }
    }
    resp[n] = 0;
    return n;
}

static uint8_t modem_connect(void) {
    char resp[64];

    ui_status("Resetting modem...");
    at_command("ATZ", resp, sizeof(resp));
    at_command("ATE0", resp, sizeof(resp));
    at_command("ATV1", resp, sizeof(resp));

    ui_status("Dialing " SERVER_IP ":" SERVER_PORT "...");
    at_command("ATDT" SERVER_IP ":" SERVER_PORT, resp, sizeof(resp));

    if (strstr(resp, "CONNECT") == 0 && strstr(resp, "connect") == 0) {
        return 0;
    }

    /* Drain any modem chatter after CONNECT */
    {
        uint16_t idle;
        for (idle = 0; idle < 8000; ++idle) {
            if (serial_available()) {
                serial_read();
                idle = 0;
            }
        }
    }
    return 1;
}
#endif

/* --- modal: help ---------------------------------------------------- */

static void help_open(void) {
    modal = MODAL_HELP;
    chat_area_clear_screen();
    ui_draw_row(2,  "  C64 LLM client - help", COLOR_WHITE, 0);
    ui_draw_row(4,  "  F1/Return  send message", COLOR_CYAN, 0);
    ui_draw_row(5,  "  F2         new conversation", COLOR_CYAN, 0);
    ui_draw_row(6,  "  F3         cancel reply", COLOR_CYAN, 0);
    ui_draw_row(7,  "  F5         conversation browser", COLOR_CYAN, 0);
    ui_draw_row(8,  "  F7         this help", COLOR_CYAN, 0);
    ui_draw_row(10, "  crsr up/dn scroll chat", COLOR_CYAN, 0);
    ui_draw_row(11, "  ctrl-a/e   start/end of input", COLOR_CYAN, 0);
    ui_draw_row(12, "  ctrl-k     kill to end", COLOR_CYAN, 0);
    ui_draw_row(13, "  ctrl-d     delete char", COLOR_CYAN, 0);
    ui_draw_row(14, "  clr/home   clear input", COLOR_CYAN, 0);
    ui_draw_row(15, "  modes (type as a message):", COLOR_WHITE, 0);
    ui_draw_row(16, "  /adventure [theme]  /chars", COLOR_CYAN, 0);
    ui_draw_row(17, "  /char <name>  /chat  /help", COLOR_CYAN, 0);
    ui_draw_row(18, "  server: " SERVER_IP ":" SERVER_PORT, COLOR_GRAY2, 0);
    ui_draw_row(19, "  press any key to close", COLOR_WHITE, 0);
}

/* --- modal: conversation browser ------------------------------------ */

static void conv_draw(void) {
    uint8_t i;
    chat_area_clear_screen();
    ui_draw_row(1, " Conversations (return=load, f5=close)",
                COLOR_WHITE, 0);
    if (conv_loading) {
        ui_draw_row(3, "  loading...", COLOR_GRAY2, 0);
        return;
    }
    if (conv_count == 0) {
        ui_draw_row(3, "  (none found)", COLOR_GRAY2, 0);
        return;
    }
    for (i = 0; i < conv_count; ++i) {
        ui_draw_row(2 + i, convs[i].title, COLOR_CYAN, i == conv_sel);
    }
}

static void conv_open(void) {
    modal = MODAL_CONV;
    conv_count = 0;
    conv_sel = 0;
    conv_loading = 1;
    conv_draw();
    proto_send_list_conversations();
}

static void conv_close(void) {
    modal = MODAL_NONE;
    chat_redraw();
}

static void conv_load_selected(void) {
    uint32_t id = convs[conv_sel].id;
    conv_close();
    chat_clear();
    ui_status("Loading conversation...");
    proto_send_message(MSG_LOAD_CONVERSATION, (uint8_t*)&id, 4);
}

/* Parse a CONVERSATION_LIST frame: count, more, then
   [id(4) timestamp(4) title\0] per entry */
static void conv_list_frame(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint8_t n = p[0];
    uint8_t more = p[1];
    uint16_t off = 2;
    uint8_t i;

    for (i = 0; i < n && conv_count < MAX_CONVS && off + 8 < plen; ++i) {
        ConvEntry* e = &convs[conv_count];
        uint8_t t = 0;
        memcpy(&e->id, p + off, 4);
        off += 8;  /* id + timestamp */
        while (off < plen && p[off] && t < sizeof(e->title) - 1) {
            e->title[t++] = ascii_to_petscii(p[off++]);
        }
        e->title[t] = 0;
        ++off;  /* null */
        ++conv_count;
    }
    if (!more || conv_count >= MAX_CONVS) {
        conv_loading = 0;
    }
    if (modal == MODAL_CONV) conv_draw();
}

/* Parse a CONVERSATION_DATA frame: count, more, then [role text\0] */
static void conv_data_frame(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint8_t n = p[0];
    uint8_t more = p[1];
    uint16_t off = 2;
    uint8_t i;

    for (i = 0; i < n && off < plen; ++i) {
        uint8_t role = p[off++];
        chat_start(role ? 1 : 0);
        if (!role) chat_append_petscii("> ");
        while (off < plen && p[off]) {
            chat_append_ascii_char(p[off++]);
        }
        ++off;
        chat_finish();
    }
    if (!more) {
        ui_status("Conversation loaded. Ready.");
    }
}

/* --- protocol dispatch ---------------------------------------------- */

static void handle_message(uint8_t msg_type) {
    switch (msg_type) {
        case MSG_STATUS: {
            char* p = (char*)proto_get_payload(&proto);
            ascii_to_petscii_str(p);
            ui_status(p);
            break;
        }
        case MSG_CHAT_CHUNK: {
            uint8_t* p = proto_get_payload(&proto);
            if (state != ST_STREAMING) {
                state = ST_STREAMING;
                chat_start(1);
                ui_status("Receiving... (F3 to cancel)");
            }
            chat_append_ascii((char*)(p + 1));  /* skip sequence byte */
            chat_redraw();
            break;
        }
        case MSG_CHAT_DONE:
            if (state != ST_IDLE) {
                chat_finish();
                state = ST_IDLE;
                ui_status("Ready. Type your message.");
            }
            break;
        case MSG_CHAT_ERROR: {
            char* p = (char*)proto_get_payload(&proto);
            chat_start(2);
            chat_append_petscii("error: ");
            chat_append_ascii(p);
            chat_finish();
            state = ST_IDLE;
            ui_status("Error from server. Ready.");
            break;
        }
        case MSG_ACK:
            if (pending_ack == PA_NEWCONV) {
                chat_clear();
                ui_status("New conversation. Ready.");
            } else if (pending_ack == PA_CANCEL) {
                ui_status("Cancelled. Ready.");
            }
            pending_ack = PA_NONE;
            break;
        case MSG_CONVERSATION_LIST:
            conv_list_frame();
            break;
        case MSG_CONVERSATION_DATA:
            conv_data_frame();
            break;
        default:
            break;
    }
}

static void pump_serial(void) {
    while (serial_available()) {
        uint8_t msg = proto_process_byte(&proto, serial_read());
        if (msg && msg != PROTO_CRC_FAIL) {
            handle_message(msg);
        }
    }
}

/* --- actions --------------------------------------------------------- */

static void send_message(void) {
    if (state != ST_IDLE) {
        ui_status("Busy - wait or press F3 to cancel.");
        return;
    }
    if (editor_len() == 0) return;

    chat_start(0);
    chat_append_petscii("> ");
    chat_append_petscii(editor_text());
    chat_finish();

    proto_send_chat(editor_text());
    editor_clear();
    state = ST_WAITING;
    ui_status("Sending...");
}

static void new_conversation(void) {
    if (state != ST_IDLE) return;
    pending_ack = PA_NEWCONV;
    proto_send_new_conversation();
    ui_status("Starting new conversation...");
}

static void cancel_stream(void) {
    if (state == ST_IDLE) return;
    pending_ack = PA_CANCEL;
    proto_send_cancel();
    ui_status("Cancelling...");
}

/* --- key handling ----------------------------------------------------- */

static void handle_key(uint8_t k) {
    if (modal == MODAL_HELP) {
        modal = MODAL_NONE;
        chat_redraw();
        return;
    }
    if (modal == MODAL_CONV) {
        switch (k) {
            case KEY_CRSR_UP:
                if (conv_sel > 0) { --conv_sel; conv_draw(); }
                break;
            case KEY_CRSR_DOWN:
                if (conv_sel + 1 < conv_count) { ++conv_sel; conv_draw(); }
                break;
            case KEY_RETURN:
                if (conv_count) conv_load_selected();
                break;
            case KEY_F5:
            case KEY_STOP:
                conv_close();
                break;
        }
        return;
    }

    switch (k) {
        case 133: /* F1 */
        case KEY_RETURN:
            send_message();
            break;
        case KEY_F2:
            new_conversation();
            break;
        case 134: /* F3 */
            cancel_stream();
            break;
        case KEY_F5:
            if (state == ST_IDLE) conv_open();
            break;
        case 136: /* F7 */
            help_open();
            break;
        case KEY_CRSR_UP:
            chat_scroll(1);
            break;
        case KEY_CRSR_DOWN:
            chat_scroll(-1);
            break;
        default:
            editor_key(k);
    }
}

/* --------------------------------------------------------------------- */

int main(void) {
    proto_init(&proto, payload_buffer, MAX_PAYLOAD);
    ui_init();
    editor_init();
    ui_status("Initializing ACIA...");
    acia_init_hw();

#ifndef CONNECT_DIRECT
    if (!modem_connect()) {
        ui_status("Connect failed! Check server/modem.");
        for (;;) { if (kbhit()) cgetc(); }
    }
#endif

    ui_status("Contacting server...");
    proto_send_ping();
    if (!wait_for_ack(8000)) {
        ui_status("No server response! Check proxy.");
        for (;;) { if (kbhit()) cgetc(); }
    }

    proto_send_new_conversation();
    wait_for_ack(4000);

    chat_start(2);
    chat_append_petscii("Connected to " SERVER_IP ":" SERVER_PORT);
    chat_finish();

    /* Drop any autostart leftovers before accepting input; the harness
       waits for the Ready status, so drain first */
    while (kbhit()) cgetc();
    ui_status("Ready. Type your message.");

    for (;;) {
        pump_serial();
        if (kbhit()) {
            handle_key(cgetc());
        }
    }

    return 0;
}

#endif /* !DEBUG_CLIENT */
