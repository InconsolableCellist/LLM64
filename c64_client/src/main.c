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
#define KEY_F4 138
#define KEY_F6 139
#define KEY_STOP 3

/* Page size for F4/F6: one line of overlap for reading continuity */
#define PAGE_LINES (CHAT_HEIGHT - 1)

/* App state */
#define ST_IDLE      0
#define ST_WAITING   1  /* message sent, reply not started */
#define ST_STREAMING 2

/* Modal overlays */
#define MODAL_NONE 0
#define MODAL_CONV 1
#define MODAL_HELP 2
#define MODAL_MENU 3
#define MODAL_MODEL 4

ProtoContext proto;
static uint8_t payload_buffer[MAX_PAYLOAD];

static uint8_t state = ST_IDLE;
static uint8_t modal = MODAL_NONE;
uint8_t crc_fail_count;
uint16_t chunk_frames;

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

/* Model browser */
#define MAX_MODELS 16
static char models[MAX_MODELS][37];  /* PETSCII names */
static uint8_t model_count;
static uint8_t model_sel;
static uint8_t model_loading;

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
    ui_draw_row(4,  "  Return     send message", COLOR_CYAN, 0);
    ui_draw_row(5,  "  F1         menu (models, modes...)", COLOR_CYAN, 0);
    ui_draw_row(6,  "  F2/F3      new conversation / cancel", COLOR_CYAN, 0);
    ui_draw_row(7,  "  F5         conversation browser", COLOR_CYAN, 0);
    ui_draw_row(8,  "  F7         this help", COLOR_CYAN, 0);
    ui_draw_row(9,  "  F4 / F6    page chat up/down", COLOR_CYAN, 0);
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

/* --- modal: main menu (F1) ------------------------------------------- */

static void menu_open(void) {
    modal = MODAL_MENU;
    chat_area_clear_screen();
    ui_draw_row(2,  "  Menu", COLOR_WHITE, 0);
    ui_draw_row(4,  "  N  new conversation", COLOR_CYAN, 0);
    ui_draw_row(5,  "  M  select model", COLOR_CYAN, 0);
    ui_draw_row(6,  "  C  conversations", COLOR_CYAN, 0);
    ui_draw_row(7,  "  A  adventure mode", COLOR_CYAN, 0);
    ui_draw_row(8,  "  R  list characters", COLOR_CYAN, 0);
    ui_draw_row(9,  "  X  cancel reply", COLOR_CYAN, 0);
    ui_draw_row(10, "  H  help", COLOR_CYAN, 0);
    ui_draw_row(12, "  F1 or stop: close", COLOR_GRAY2, 0);
}

/* --- modal: model browser --------------------------------------------- */

static void model_draw(void) {
    uint8_t i;
    chat_area_clear_screen();
    ui_draw_row(1, " Models (return=select, f1=close)", COLOR_WHITE, 0);
    if (model_loading) {
        ui_draw_row(3, "  loading...", COLOR_GRAY2, 0);
        return;
    }
    if (model_count == 0) {
        ui_draw_row(3, "  (none reported)", COLOR_GRAY2, 0);
        return;
    }
    for (i = 0; i < model_count; ++i) {
        ui_draw_row(2 + i, models[i], COLOR_CYAN, i == model_sel);
    }
}

static void model_open(void) {
    modal = MODAL_MODEL;
    model_count = 0;
    model_sel = 0;
    model_loading = 1;
    model_draw();
    proto_send_message(MSG_LIST_MODELS, 0, 0);
}

static void model_select(void) {
    modal = MODAL_NONE;
    chat_redraw();
    proto_send_text(MSG_SET_MODEL, models[model_sel]);
    ui_status("Switching model...");
}

/* Parse a MODEL_LIST frame: count, more, then name\0 per entry */
static void model_list_frame(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint8_t n = p[0];
    uint8_t more = p[1];
    uint16_t off = 2;
    uint8_t i;

    for (i = 0; i < n && model_count < MAX_MODELS && off < plen; ++i) {
        uint8_t t = 0;
        while (off < plen && p[off] && t < 36) {
            models[model_count][t++] = ascii_to_petscii(p[off++]);
        }
        models[model_count][t] = 0;
        while (off < plen && p[off]) ++off;  /* skip overlong remainder */
        ++off;
        ++model_count;
    }
    if (!more || model_count >= MAX_MODELS) {
        model_loading = 0;
    }
    if (modal == MODAL_MODEL) model_draw();
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
            ++chunk_frames;
            if (state != ST_STREAMING) {
                state = ST_STREAMING;
                chat_start(1);
                ui_status("Receiving... (F3 to cancel)");
            }
            chat_append_ascii((char*)(p + 1));  /* skip sequence byte */
            chat_redraw_stream();
            break;
        }
        case MSG_CHAT_DONE:
            if (state != ST_IDLE) {
                chat_finish();
                state = ST_IDLE;
                if (serial_overflows() || serial_overruns()
                        || crc_fail_count) {
                    /* Data was lost - a bug if it ever shows. Counters:
                       ring drops / hw overruns / crc failures (hex). */
                    static char dm[41];
                    static const char hx[] = "0123456789abcdef";
                    uint8_t v;
                    strcpy(dm, "Ready. [data loss ov=?? hw=?? cr=??]");
                    v = serial_overflows();
                    dm[21] = hx[v >> 4]; dm[22] = hx[v & 15];
                    v = serial_overruns();
                    dm[27] = hx[v >> 4]; dm[28] = hx[v & 15];
                    v = crc_fail_count;
                    dm[33] = hx[v >> 4]; dm[34] = hx[v & 15];
                    ui_status(dm);
                } else {
                    ui_status("Ready. Type your message.");
                }
                chunk_frames = 0;
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
        case MSG_MODEL_LIST:
            model_list_frame();
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
        uint8_t msg;
        if (proto_in_payload(&proto)) {
            proto_fill_payload(&proto);  /* bulk path keeps up with 9600 */
            continue;
        }
        msg = proto_process_byte(&proto, serial_read());
        if (msg == PROTO_CRC_FAIL) {
            ++crc_fail_count;
        } else if (msg) {
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

/* Send a slash command through the normal chat path */
static void send_command(const char* cmd) {
    if (state != ST_IDLE) {
        ui_status("Busy - X/F3 cancels the reply first.");
        return;
    }
    proto_send_chat(cmd);
    state = ST_WAITING;
    ui_status("Working...");
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
    if (modal == MODAL_MENU) {
        modal = MODAL_NONE;
        chat_redraw();
        switch (k) {
            case 'n': new_conversation(); break;
            case 'm': model_open(); break;
            case 'c': if (state == ST_IDLE) conv_open(); break;
            case 'a': send_command("/adventure"); break;
            case 'r': send_command("/chars"); break;
            case 'x': cancel_stream(); break;
            case 'h': help_open(); break;
            default: break;  /* F1/STOP/anything else: just close */
        }
        return;
    }
    if (modal == MODAL_MODEL) {
        switch (k) {
            case KEY_CRSR_UP:
                if (model_sel > 0) { --model_sel; model_draw(); }
                break;
            case KEY_CRSR_DOWN:
                if (model_sel + 1 < model_count) { ++model_sel; model_draw(); }
                break;
            case KEY_RETURN:
                if (model_count) model_select();
                break;
            case 133: /* F1 */
            case KEY_STOP:
                modal = MODAL_NONE;
                chat_redraw();
                break;
        }
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
        case KEY_RETURN:
        case 0x8D:  /* shift-Return: shift lock is easy to leave on */
            send_message();
            break;
        case 133: /* F1 */
            menu_open();
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
        case KEY_F4:
            chat_scroll(PAGE_LINES);
            break;
        case KEY_F6:
            chat_scroll(-PAGE_LINES);
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
