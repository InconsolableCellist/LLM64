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
#include "cfg.h"
#include "loader.h"
#include "modapi.h"
#include "diag.h"

#ifdef SOFT80
/* Overlay module entries: 1 config, 2 conversation manager,
   3 disk copy, 4 server-fed menu, 5 jukebox. (No "#N" at a comment start
   here: cc65 2.17 parses it as a directive when SOFT80 is undefined
   and this block is skipped - broke the 40col build once.) */
void mod_config_run(void);
void mod_convmgr_run(void);
void mod_diskcopy_run(void);
void mod_menu_run(void);
void mod_sound_run(void);
#endif

/* music.s */
extern uint8_t music_state;
extern uint16_t music_ext_init;
extern uint16_t music_ext_play_addr;
extern uint8_t music_ext_song;
extern uint8_t music_ext_vol;
void music_ext_begin(void);
void music_ext_stop(void);
void music_hold_begin(void);
void music_hold_end(void);

#ifdef SOFT80
/* Streamed-SID window (see c64-soft80.cfg) */
#define SID_WIN_START 0xB000u
#define SID_WIN_END   0xC000u
/* Fullscreen image. Two formats share the streaming path:
   hires (bitmap 8000 + matrix 1000; soft-80's own VIC mode) and
   multicolor (bitmap 8000 + matrix 1000 + color RAM 1000; needs the
   VIC multicolor bit and the background register during display). */
#define IMG_BITMAP    ((uint8_t*)0xE000)
#define IMG_MATRIX    ((uint8_t*)0xCC00)
#define IMG_COLRAM    ((uint8_t*)0xD800)
#define IMG_BITMAP_SZ 8000u
#define IMG_HIRES_SZ  9000u
#define IMG_MC_SZ     10000u
#define VIC_CTRL2     (*(volatile uint8_t*)0xD016)
#define VIC_BG        (*(volatile uint8_t*)0xD021)
#endif

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
#define ST_LOADING   3  /* bulk conversation load, chat frozen */

/* Modal overlays */
#define MODAL_NONE   0
#define MODAL_CONV   1
#define MODAL_MENU   3
#define MODAL_MODULE 4  /* an overlay module owns keys + screen */

ProtoContext proto;
static uint8_t payload_buffer[MAX_PAYLOAD];

static uint8_t state = ST_IDLE;
static uint8_t modal = MODAL_NONE;
uint8_t crc_fail_count;
uint16_t chunk_frames;
/* Chunk sequence tracking: a dropped/corrupted CHAT_CHUNK otherwise
   loses text silently (each frame carries a seq byte; CHAT_DONE carries
   the next expected one). */
static uint8_t rx_seq, chunk_lost;

#ifdef SOFT80
/* Streamed-SID transfer progress */
static uint8_t sid_active;
static uint16_t sid_expect, sid_got;
static uint8_t* sid_dst;
static char sid_name[25];
/* Fullscreen image state. img_shown is non-static so tests can watch
   transfer completion via the label file. */
static uint8_t img_active;   /* transfer in progress */
uint8_t img_shown;           /* displayed; next key dismisses */
static uint8_t img_mc;       /* multicolor: VIC state to restore */
static uint8_t img_d021_save;
static uint16_t img_got, img_expect;

/* Received-chunk bitmap (256-byte chunks; SID and image transfers never
   overlap so they share it). DATA frames carry their own offset, so a
   RESUMED transfer only needs the chunks the modem ate last time -
   successive lossy passes converge instead of starting over. */
static uint8_t xfer_map[8];
/* Window flow control: ACK every xfer_window-th received DATA frame so
   the proxy never overfills the modem's packet queue (it drops whole
   frames when scheduling jitter bunches paced writes) */
static uint8_t xfer_window, xfer_count;
static void xfer_flow_tick(void) {
    if (xfer_window && ++xfer_count >= xfer_window) {
        xfer_count = 0;
        proto_send_ack();
    }
}
static uint8_t map_test_set(uint8_t idx) {
    uint8_t m = 1 << (idx & 7);
    if (xfer_map[idx >> 3] & m) return 0;   /* already have this chunk */
    xfer_map[idx >> 3] |= m;
    return 1;
}

static void img_restore_vic(void) {
    if (img_mc) {
        VIC_CTRL2 &= (uint8_t)~0x10;
        VIC_BG = img_d021_save;
        img_mc = 0;
    }
}

/* Leave the fullscreen image: the picture painted over the input rows
   too, so the editor needs repainting along with frame and chat.
   (Music no longer pauses for transfers: the ACIA-blinding theory was
   disproven once the modem's DTR/RTS glitching was fixed.) */
static void img_close(void) {
    /* Bracketed: this is the deepest call chain the key handler can
       start (full 80-col repaint + editor), and it runs while the music
       IRQ is nesting on top of it. A trail ending on DC_IMGCLOSE with
       no DC_IMGDONE says the machine died inside the repaint. */
    diag_crumb(DC_IMGCLOSE);
    img_restore_vic();
    ui_frozen = 0;
    ui_redraw_all();
    editor_redraw();
    ui_status("Ready.");  /* the image's colors linger there otherwise */
    diag_crumb(DC_IMGDONE);
}
#endif

/* Status-line scratch shared by the data-loss diagnostics and the
   dial status (sized for "Dialing " + a full-length host:port) */
static char dm[48];
static const char hx[] = "0123456789abcdef";
static void hx2(uint8_t i, uint8_t v) {
    dm[i] = hx[v >> 4];
    dm[i + 1] = hx[v & 15];
}

#ifdef SOFT80
/* Counter snapshots at BEGIN so the fail line shows THIS transfer's
   losses, not the session's history (cumulative counts misattributed
   old CRC failures to a fresh transfer once) */
static uint8_t xf_ov0, xf_hw0, xf_cr0;
static void xfer_mark(void) {
    xf_ov0 = serial_overflows();
    xf_hw0 = serial_overruns();
    xf_cr0 = crc_fail_count;
}

/* A failed media transfer: say WHERE the bytes died. got/expect plus
   ring / hw-overrun / crc counters since BEGIN - all three zero means
   the modem dropped them silently. */
static void xfer_fail(char tag, uint16_t got, uint16_t expect) {
    strcpy(dm, "? fail g=????/???? ov=?? hw=?? cr=??");
    dm[0] = tag;
    hx2(9, (uint8_t)(got >> 8));
    hx2(11, (uint8_t)got);
    hx2(14, (uint8_t)(expect >> 8));
    hx2(16, (uint8_t)expect);
    hx2(22, (uint8_t)(serial_overflows() - xf_ov0));
    hx2(28, (uint8_t)(serial_overruns() - xf_hw0));
    hx2(34, (uint8_t)(crc_fail_count - xf_cr0));
    ui_status(dm);
}
#endif

/* Response watchdog. keyboard.s _sys_ticks is a free-running 16-bit ~60Hz
   counter; we read only the HIGH byte (atomic vs the IRQ, ~4.3s/unit). */
extern volatile uint8_t sys_ticks[2];
static uint8_t watchdog_at;
/* ~40s. Prompt-eval on a long context can delay the first frame, but the
   proxy ACKs a received request within ~1s, so quiet this long means the
   request or a frame was lost in transit. */
#ifndef WATCHDOG_UNITS
#define WATCHDOG_UNITS 10         /* * ~4.3s */
#endif

static void watchdog_reset(void) {
    watchdog_at = sys_ticks[1];
}
static uint8_t watchdog_expired(void) {
    return (uint8_t)(sys_ticks[1] - watchdog_at) >= WATCHDOG_UNITS;
}

/* What the next ACK acknowledges */
#define PA_NONE    0
#define PA_NEWCONV 1
#define PA_CANCEL  2
static uint8_t pending_ack = PA_NONE;

/* Conversation list state - shared with overlay modules (modapi.h);
   the resident 40-col browser and the SOFT80 manager module both
   render from these */
ConvEntry convs[MAX_CONVS];
uint8_t conv_count;
uint8_t conv_sel;
uint8_t conv_loading;
uint8_t conv_more_pages;
static uint8_t load_count;   /* messages received during a bulk load */
static uint8_t load_open;    /* a split message is mid-assembly */

#ifdef SOFT80
/* Module-modal hooks (modapi.h): while a module modal is open it gets
   first chance at messages and owns the keyboard, but the main loop
   keeps pumping serial */
uint8_t (*mod_msg_hook)(uint8_t msg_type);
void (*mod_key_hook)(uint8_t key);
void (*mod_tick_hook)(void);

void mod_modal_begin(uint8_t (*msg)(uint8_t), void (*key)(uint8_t)) {
    mod_msg_hook = msg;
    mod_key_hook = key;
    mod_tick_hook = 0;   /* opt in afterwards; most modules never tick */
    modal = MODAL_MODULE;
}

void mod_modal_tick(void (*tick)(void)) {
    mod_tick_hook = tick;
}

void mod_modal_end(void) {
    mod_msg_hook = 0;
    mod_key_hook = 0;
    mod_tick_hook = 0;   /* before the slot can be reloaded */
    modal = MODAL_NONE;
    chat_redraw();
}
#endif

/* Deferred menu activation (modapi.h): the menu module fills these
   and closes; handle_key dispatches once the hook has returned, so a
   local action can safely load another module over the menu's slot */
uint8_t menu_action;
const char* menu_pcmd;

/* Model browser */

/* ------------------------------------------------------------------ */

static uint8_t pump_serial(void);

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

/* Hayes escape guard time (~1.2s of 60Hz ticks, wrap-safe) */
static void guard_pause(void) {
    uint8_t last = sys_ticks[0];
    uint8_t elapsed = 0;
    while (elapsed < 72) {
        uint8_t now = sys_ticks[0];
        elapsed += (uint8_t)(now - last);
        last = now;
    }
}

static uint8_t modem_connect(void) {
    char resp[64];

    /* The C64U's modem survives a C64 reset: rebooting mid-session
       leaves it in data mode, where every AT command below would
       vanish into the OLD connection as data (field: 'Connect
       failed' after a warm reboot). Escape and hang up first; in
       command mode the stray +++ is harmless. */
    ui_status("Hanging up any old session...");
    guard_pause();
    serial_write('+'); serial_write('+'); serial_write('+');
    serial_flush();
    guard_pause();
    at_command("ATH", resp, sizeof(resp));

    ui_status("Resetting modem...");
    at_command("ATZ", resp, sizeof(resp));
    at_command("ATE0", resp, sizeof(resp));
    at_command("ATV1", resp, sizeof(resp));

    /* g_dial holds "ATDT<host>:<port>" from the disk config (or the
       baked default); +4 skips the ATDT for the status line */
    strcpy(dm, "Dialing ");
    strcat(dm, g_dial + 4);
    ui_status(dm);
    at_command(g_dial, resp, sizeof(resp));

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

/* --- modal: main menu (F1) ------------------------------------------- */
/* Help lives server-side now (/help streams into the scrollback):
   ~700 bytes of resident text reclaimed for the module slot. */

static void menu_open(void) {
    modal = MODAL_MENU;
    chat_area_clear_screen();
#ifdef SOFT80
    /* Compact fallback only: F1's real UI is the server-fed menu
       module. Shown when the boot disk is missing or mid-stream
       (where X = cancel must stay reachable); hotkeys work as ever. */
    ui_draw_row(2, "  Menu", COLOR_WHITE, 0);
    ui_draw_row(4, "  n)ew  c)onvs  a)dventure  r)oles  m)odels",
                COLOR_CYAN, 0);
    ui_draw_row(5, "  s)ilence  x)cancel  e)config  d)iskcopy  h)elp",
                COLOR_CYAN, 0);
    ui_draw_row(7, "  f1 or stop: close", COLOR_GRAY2, 0);
#else
    ui_draw_row(2,  "  Menu", COLOR_WHITE, 0);
    ui_draw_row(4,  "  N  new conversation", COLOR_CYAN, 0);
    ui_draw_row(5,  "  M  models (/model <n> picks)", COLOR_CYAN, 0);
    ui_draw_row(6,  "  C  conversations", COLOR_CYAN, 0);
    ui_draw_row(7,  "  A  adventure mode", COLOR_CYAN, 0);
    ui_draw_row(8,  "  R  list characters", COLOR_CYAN, 0);
    ui_draw_row(9,  "  X  cancel reply", COLOR_CYAN, 0);
    ui_draw_row(10, "  H  help", COLOR_CYAN, 0);
    if (music_state == 0) {
        ui_draw_row(11, "  S  music (off)", COLOR_CYAN, 0);
    } else {
        ui_draw_row(11, "  S  stop the music", COLOR_CYAN, 0);
    }
    ui_draw_row(13, "  F1 or stop: close", COLOR_GRAY2, 0);
#endif
}

/* Model browser removed: /models lists (numbered), /model <n> or a
   name prefix switches - ~900 bytes reclaimed for the module slot. */

/* --- conversation list: shared parser + load kickoff ----------------- */

/* Start loading a conversation (called from the browser or the
   manager module; the caller has already closed its modal) */
void load_conversation_by_id(uint32_t id) {
#ifdef SOFT80
    /* A playing tune's SEI windows corrupt the incoming load frames
       (field: big load stalled at 'Loading... 19'); the loaded
       conversation's own soundtrack resumes from meta afterwards */
    music_ext_stop();
#endif
    chat_clear();
    chat_freeze(1);  /* render once at the end: jump straight to the bottom */
    load_count = 0;
    load_open = 0;
    /* Arm the watchdog: a frame lost in transit would otherwise leave the
       chat frozen at 'Loading... NN' forever */
    state = ST_LOADING;
    watchdog_reset();
    ui_status("Loading conversation...");
    proto_send_message(MSG_LOAD_CONVERSATION, (uint8_t*)&id, 4);
}

/* Parse a CONVERSATION_LIST frame: count, more, then
   [id(4) timestamp(4) title\0] per entry. 'more' is a bitfield:
   bit0 = more frames coming, bit1 = more pages exist beyond these.
   Callers draw; this only fills convs[]. */
void conv_list_frame(void) {
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
    conv_more_pages = (more >> 1) & 1;
    if (!(more & 1) || conv_count >= MAX_CONVS) {
        conv_loading = 0;
    }
}

#ifndef SOFT80
/* --- modal: conversation browser (40-col builds only; SOFT80 uses
   the manager module on disk, loaded via F5) ------------------------- */

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
    load_conversation_by_id(id);
}
#endif /* !SOFT80 */

#ifdef SOFT80
/* Load an overlay module and run it. RX is masked around the disk
   LOAD (JiffyDOS IEC timing vs serial NMIs) and resumed BEFORE the
   module runs - hook-modal modules request + render immediately.

   The tune is held for the same window. Masking RX keeps the ACIA
   quiet, but the 60Hz CIA tick kept calling a streamed SID's play
   routine straight through the LOAD - up to 1600 cycles of stolen time
   per frame against cycle-counted IEC. Both other timing-critical
   transfers in this client already silence music first (see
   load_conversation_by_id above and mod_diskcopy.c); this path was the
   one that did not, and it is reachable from a bare F1/F5 keypress
   while a tune plays. Held rather than stopped so the song survives. */
static uint8_t mod_open(const char* name, void (*run)(void)) {
    uint8_t ok;
    diag_note_mod(name[7]);   /* "c64llm.N" -> N */
    diag_crumb(DC_MODLOAD);
    /* The tune is HELD across the load - tried playing through it, and
       it is worse. IEC transfers keep interrupts disabled to hold their
       bit-banged timing, so the 60Hz CIA tick that drives music_play is
       not stopped but STARVED: calls arrive late and bunched, and a SID
       whose envelopes and pitch slides advance at irregular intervals
       warbles audibly. Even silence beats uneven playback (field
       verdict, 2026-07-21). Fixing it properly means replacing
       cbm_load with a chunked reader that ticks the music between
       chunks - see HANDOFF.md; baud rate is irrelevant here, the disk
       does not touch the serial link. */
    music_hold_begin();
    serial_rx_pause();
    ok = module_load(name);
    serial_rx_resume();
    music_hold_end();
    diag_crumb(DC_MODLOADED);
    if (ok) run();
    else ui_status("Module load failed - boot disk?");
    diag_crumb(DC_MODDONE);
    return ok;
}

/* Open the conversation manager module (F5 / menu C) */
static void convmgr_open(void) {
    mod_open("c64llm.2", mod_convmgr_run);
}

/* Open the jukebox / sound window (menu J) */
static void sound_open(void) {
    mod_open("c64llm.5", mod_sound_run);
}

/* Open the server-fed menu module (F1). Falls back to the resident
   text menu if the boot disk is missing. */
static void menu_module_open(void) {
    if (!mod_open("c64llm.4", mod_menu_run)) menu_open();
}
#endif

/* Parse a CONVERSATION_DATA frame: count, more, then [role text\0].
   Long messages arrive split across frames - role bit 7 marks a
   continuation, which appends to the open message instead of starting
   a new chat block (frames are capped by MAX_PAYLOAD). */
static void conv_data_frame(void) {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t plen = proto_get_length(&proto);
    uint8_t n = p[0];
    uint8_t more = p[1];
    uint16_t off = 2;
    uint8_t i;

    /* A frame delayed past the watchdog abort must not append into
       the live chat or flip the status after the timeout notice */
    if (state != ST_LOADING) return;

    for (i = 0; i < n && off < plen; ++i) {
        uint8_t role = p[off++];
        if (!(role & 0x80)) {
            if (load_open) chat_finish();
            load_open = 1;
            chat_start(role);   /* 0 user, 1 assistant, 2 system */
            if (!role) chat_append_petscii("> ");
            /* progress in the status bar while the chat is frozen */
            ++load_count;
            {
                static char lm[24];
                strcpy(lm, "Loading... ??");
                lm[11] = '0' + (load_count / 10) % 10;
                lm[12] = '0' + load_count % 10;
                ui_status(lm);
            }
        }
        while (off < plen && p[off]) {
            chat_append_ascii_char(p[off++]);
        }
        ++off;
    }
    if (!more) {
        if (load_open) chat_finish();
        load_open = 0;
        state = ST_IDLE;
        chat_freeze(0);
        ui_status("Conversation loaded. Ready.");
    }
}

/* --- protocol dispatch ---------------------------------------------- */

static void handle_message(uint8_t msg_type) {
#ifdef SOFT80
    /* An open module modal gets first chance (conversation manager
       consumes list frames and its op ACKs; anything it declines
       falls through to the resident cases below) */
    if (mod_msg_hook && mod_msg_hook(msg_type)) return;
#endif
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
                if (state == ST_IDLE) {
                    /* Unsolicited (e.g. a late error notice): sync to
                       whatever seq it opens with */
                    rx_seq = p[0];
                    chunk_lost = 0;
                }
                state = ST_STREAMING;
                chat_start(1);
                ui_status("Receiving... (F3 to cancel)");
            }
            if (p[0] != rx_seq) chunk_lost = 1;
            rx_seq = (uint8_t)(p[0] + 1);
            chat_append_ascii((char*)(p + 1));  /* skip sequence byte */
            chat_redraw_stream();
            break;
        }
        case MSG_NOTICE:
            /* Something the PROXY has to say that is not the model's
               reply - a dice result, for now. Opens and closes its own
               block so the reply that follows still starts cleanly.
               Ignored mid-stream: finishing an open assistant block
               here would leave the remaining chunks appending into a
               closed one. Nothing sends it mid-stream today. */
            if (state != ST_STREAMING) {
                chat_start(2);
                chat_append_ascii((char*)proto_get_payload(&proto));
                chat_finish();
                chat_redraw();
            }
            break;
        case MSG_CHAT_DONE:
            if (state != ST_IDLE) {
                chat_finish();
                /* A live media transfer must keep the watchdog armed
                   (DONE mid-transfer used to go idle: a dropped image
                   tail then left the screen frozen forever) */
#ifdef SOFT80
                if (sid_active || img_active) {
                    state = ST_LOADING;
                    watchdog_reset();
                } else
#endif
                {
                    state = ST_IDLE;
                }
                if (proto_get_length(&proto) >= 1
                        && *proto_get_payload(&proto) != rx_seq) {
                    chunk_lost = 1;
                }
                if (chunk_lost) {
                    chunk_lost = 0;
                    chat_start(2);
                    chat_append_petscii(
                        "(some text was lost - /history reshows it)");
                    chat_finish();
                }
                if (serial_overflows() || serial_overruns()
                        || crc_fail_count) {
                    /* Data was lost - a bug if it ever shows. Counters:
                       ring drops / hw overruns / crc failures (hex). */
                    strcpy(dm, "Ready. [data loss ov=?? hw=?? cr=??]");
                    hx2(21, serial_overflows());
                    hx2(27, serial_overruns());
                    hx2(33, crc_fail_count);
                    ui_status(dm);
                } else {
                    ui_status("Ready. Type your message.");
                }
                chunk_frames = 0;
            }
            break;
        case MSG_CHAT_ERROR: {
            char* p = (char*)proto_get_payload(&proto);
            chat_freeze(0);  /* in case a bulk load failed midway */
            chat_start(2);
            chat_append_petscii("error: ");
            chat_append_ascii(p);
            chat_finish();
#ifdef SOFT80
            if (sid_active || img_active) {
                state = ST_LOADING;
                watchdog_reset();
            } else
#endif
            {
                state = ST_IDLE;
            }
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
#ifndef SOFT80
            if (modal == MODAL_CONV) conv_draw();
#endif
            break;
        case MSG_CONVERSATION_DATA:
            conv_data_frame();
            break;
        case MSG_HINT: {
            uint8_t* hp = proto_get_payload(&proto);
            uint8_t f = hp[0];
            /* Byte 1 (picture count) was added later; a frame that
               predates it carries only the flags, and proto zero-fills
               so an absent count reads as none. */
            /* Byte 1 (picture count) was added after the flags byte;
               a frame without it leaves the tally alone. Assigned
               directly rather than passed - a second argument would go
               on the C stack for no gain. */
            if (proto_get_length(&proto) > 1) ui_pics = hp[1];
#ifdef SOFT80
            /* Byte 2 onward: the proxy-composed right-hand chrome, nul
               terminated. Grown the same way the picture count was, so
               a proxy that predates it simply sends nothing here. */
            /* No conversion: the chrome stays ASCII end to end, which
               is what soft-80 cells are (and petscii_to_ascii_str does
               not even exist in this build). */
            if (proto_get_length(&proto) > 2) {
                ui_set_chrome((const char*)hp + 2);
            }
#endif
            /* A scene became ready (bit0 rising): announce it with a
               rainbow attention line, not just the quiet status
               indicator. Only while idle - never mid-stream. */
            if ((f & 1) && !(ui_hints & 1) && state == ST_IDLE
                    && modal == MODAL_NONE) {
                chat_start(3);
                chat_append_petscii("* a scene is ready - /pic paints it *");
                chat_finish();
                chat_redraw();
            }
            ui_set_hints(f);
            break;
        }
#ifdef SOFT80
        case MSG_MUSIC_STOP:
            /* Silence a streamed SID. No ACK: nothing is waiting on it,
               and a lost frame costs one unstopped tune, not a stuck
               transfer. */
            music_ext_stop();
            break;
        case MSG_SID_BEGIN: {
            /* load(2) init(2) play(2) song(1) size(2) vol(1) resume(1)
               window(1) name(nul) */
            uint8_t* p = proto_get_payload(&proto);
            uint16_t load = p[0] | ((uint16_t)p[1] << 8);
            uint16_t size = p[7] | ((uint16_t)p[8] << 8);
            /* wrap-proof: load+size overflowed uint16 for size>=0x5000
               and the old guard passed out-of-window transfers */
            if (load < SID_WIN_START || load >= SID_WIN_END
                    || size > SID_WIN_END - load
                    || proto_get_length(&proto) < 13) {
                proto_send_nak();
                break;
            }
            xfer_window = p[11];
            xfer_count = 0;
            xfer_mark();
            music_ext_stop();       /* silence during the transfer */
            diag_crumb(DC_SIDRECV);
            music_ext_init = p[2] | ((uint16_t)p[3] << 8);
            music_ext_play_addr = p[4] | ((uint16_t)p[5] << 8);
            music_ext_song = p[6];
            music_ext_vol = p[9];
            sid_dst = (uint8_t*)load;
            sid_expect = size;
            if (!p[10]) {           /* fresh, not a resumed retry */
                sid_got = 0;
                memset(xfer_map, 0, sizeof xfer_map);
            }
            sid_active = 1;
            strncpy(sid_name, (char*)(p + 12), 24);
            sid_name[24] = 0;
            ascii_to_petscii_str(sid_name);
            /* Arm the watchdog: a dropped tail must not hang us */
            if (state == ST_IDLE) {
                state = ST_LOADING;
                watchdog_reset();
            }
            /* Handshake: the proxy holds the data until this ACK, so
               nothing streams while a tune could blind the ACIA */
            proto_send_ack();
            break;
        }
        case MSG_SID_DATA: {
            /* offset(2) data... - chunks are 256-aligned */
            uint16_t len = proto_get_length(&proto);
            uint8_t* p = proto_get_payload(&proto);
            uint16_t off;
            if (!sid_active || len < 3) {
                sid_active = 0;
                break;
            }
            off = p[0] | ((uint16_t)p[1] << 8);
            len -= 2;
            p += 2;
            if ((off & 0xFF) || off >= sid_expect
                    || len > sid_expect - off) {
                sid_active = 0;
                break;
            }
            xfer_flow_tick();
            if (!map_test_set((uint8_t)(off >> 8))) break;
            memcpy(sid_dst + off, p, len);
            sid_got += len;
            break;
        }
        case MSG_SID_END:
            if (sid_active && sid_got == sid_expect) {
                diag_crumb(DC_MUSICBEG);
                music_ext_begin();
                proto_send_ack();
                ui_status(sid_name);
            } else {
                proto_send_nak();
                xfer_fail('m', sid_got, sid_expect);
            }
            sid_active = 0;
            if (state == ST_LOADING) state = ST_IDLE;
            break;
        case MSG_IMG_BEGIN: {
            /* Payload: format byte (1 = multicolor) + bg color. The
               image streams straight onto the live screen; freeze all
               text drawing until the user dismisses it. Scrollback is
               untouched, so dismissing is a local redraw - no reload. */
            uint8_t* p = proto_get_payload(&proto);
            img_restore_vic();       /* a retry may follow a failed mc */
            xfer_mark();
            img_active = 1;
            img_shown = 0;
            /* payload: fmt(1) bg(1) resume(1) window(1) */
            if (proto_get_length(&proto) < 3 || !p[2]) {
                img_got = 0;
                memset(xfer_map, 0, sizeof xfer_map);
            }
            xfer_window = (proto_get_length(&proto) >= 4) ? p[3] : 0;
            xfer_count = 0;
            ui_frozen = 1;
            if (proto_get_length(&proto) >= 2 && p[0] == 1) {
                img_expect = IMG_MC_SZ;
                img_d021_save = VIC_BG;
                img_mc = 1;
                VIC_BG = p[1];
                VIC_CTRL2 |= 0x10;   /* progressive paint in mc mode */
            } else {
                img_expect = IMG_HIRES_SZ;
            }
            if (state == ST_IDLE) {
                state = ST_LOADING;   /* watchdog covers the transfer */
                watchdog_reset();
            }
            /* Handshake: music now silenced and rendering drained -
               safe for the proxy to stream */
            proto_send_ack();
            break;
        }
        case MSG_IMG_DATA: {
            /* offset(2) data... memcpy, not a per-byte loop: at 19200
               baud the consumer must stay well under ~520 cycles/byte
               or the RX ring backs up and the modem drops the tail */
            uint16_t len = proto_get_length(&proto);
            uint8_t* p = proto_get_payload(&proto);
            uint16_t off, n;
            if (!img_active || len < 3) {
                img_active = 0;
                break;
            }
            off = p[0] | ((uint16_t)p[1] << 8);
            len -= 2;
            p += 2;
            if ((off & 0xFF) || off >= img_expect
                    || len > img_expect - off) {
                img_active = 0;
                break;
            }
            xfer_flow_tick();
            if (!map_test_set((uint8_t)(off >> 8))) break;
            if (off < IMG_BITMAP_SZ) {
                n = IMG_BITMAP_SZ - off;
                if (n > len) n = len;
                memcpy(IMG_BITMAP + off, p, n);
                img_got += n;
                off += n;
                p += n;
                len -= n;
            }
            if (len && off < IMG_HIRES_SZ) {
                n = IMG_HIRES_SZ - off;
                if (n > len) n = len;
                memcpy(IMG_MATRIX + (off - IMG_BITMAP_SZ), p, n);
                img_got += n;
                off += n;
                p += n;
                len -= n;
            }
            if (len) {
                memcpy(IMG_COLRAM + (off - IMG_HIRES_SZ), p, len);
                img_got += len;
            }
            break;
        }
        case MSG_IMG_END:
            if (img_active && img_got == img_expect) {
                img_shown = 1;   /* key handler dismisses + redraws */
                diag_crumb(DC_IMGSHOW);
                proto_send_ack();
            } else {
                img_close();
                proto_send_nak();
                xfer_fail('i', img_got, img_expect);
            }
            img_active = 0;
            if (state == ST_LOADING) state = ST_IDLE;
            break;
#endif
        default:
            break;
    }
}

/* Returns 1 if any serial byte was processed this call */
static uint8_t pump_serial(void) {
    uint8_t saw = 0;
    while (serial_available()) {
        uint8_t msg;
        saw = 1;
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
    return saw;
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
    rx_seq = 0;
    chunk_lost = 0;
    pending_ack = PA_NONE;  /* a stale F2 ACK must not wipe the chat */
    watchdog_reset();
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
    rx_seq = 0;
    chunk_lost = 0;
    pending_ack = PA_NONE;
    watchdog_reset();
    ui_status("Working...");
}

static void cancel_stream(void) {
    /* Loads aren't cancellable server-side; the watchdog covers them */
    if (state != ST_WAITING && state != ST_STREAMING) return;
    pending_ack = PA_CANCEL;
    proto_send_cancel();
    ui_status("Cancelling...");
}

/* --- local menu actions ----------------------------------------------
   Shared by the resident F1 menu and the menu module's '!' commands. */

/* The built-in pattern tunes are gone (see music.s), so 's' is a plain
   local stop now. Kept for the 40-column fallback menu: soft-80 reaches
   stopping through the jukebox's own key and /music stop, and its
   server-fed menu no longer lists this at all. */
static void music_stop_local(void) {
    music_ext_stop();
    ui_status("Music off.");
}

#ifdef SOFT80
static void config_open(void) {
    if (mod_open("c64llm.1", mod_config_run)) {
        build_dial_string();  /* used on next boot/redial */
    }
    chat_redraw();
}

static void diskcopy_open(void) {
    /* Disk copy: RX stays paused for the WHOLE run - the copy is one
       long IEC conversation and serial NMIs would corrupt it (proxy
       is idle; nothing arrives) */
    serial_rx_pause();
    if (module_load("c64llm.3")) {
        mod_diskcopy_run();
        serial_rx_resume();
        ui_status("Ready.");
    } else {
        serial_rx_resume();
        ui_status("Module load failed - boot disk?");
    }
    chat_redraw();
}
#endif

static void menu_local(uint8_t a) {
    switch (a) {
        case 'n': new_conversation(); break;
        case 'x': cancel_stream(); break;
        case 's': music_stop_local(); break;
#ifdef SOFT80
        case 'c': if (state == ST_IDLE) convmgr_open(); break;
        case 'e': config_open(); break;
        case 'd': diskcopy_open(); break;
        case 'j': sound_open(); break;
#endif
        default: break;
    }
}

/* --- key handling ----------------------------------------------------- */

static void handle_key(uint8_t k) {
    /* The key is recorded but NOT pushed as a breadcrumb: the crash we
       are chasing happens while typing, and one crumb per keystroke
       would flush the 8-deep ring within a word - erasing the module
       load or image dismiss that actually explains it. Rare events get
       crumbs; the last key gets its own byte. */
    diag_note_key(k);
#ifdef SOFT80
    if (img_shown) {   /* any key dismisses the fullscreen image */
        img_shown = 0;
        img_close();
        return;
    }
    if (modal == MODAL_MODULE) {
        if (mod_key_hook) mod_key_hook(k);
        /* Deferred menu activation, dispatched only now that the
           module's code is off the call stack (a local action may
           load a different module into the slot) */
        if (menu_action) {
            uint8_t a = menu_action;
            menu_action = 0;
            menu_local(a);
        } else if (menu_pcmd) {
            const char* c = menu_pcmd;
            menu_pcmd = 0;
            send_command(c);
        }
        return;
    }
#endif
    if (modal == MODAL_MENU) {
        modal = MODAL_NONE;
        chat_redraw();
        switch (k) {
            case 'n': case 'x': case 's':
#ifdef SOFT80
            case 'c': case 'e': case 'd': case 'j':
#endif
                menu_local(k);
                break;
#ifndef SOFT80
            case 'c': if (state == ST_IDLE) conv_open(); break;
#endif
            case 'm': send_command("/models"); break;
            case 'a': send_command("/adventure"); break;
            case 'r': send_command("/chars"); break;
            case 'h': send_command("/help"); break;
            default: break;  /* F1/STOP/anything else: just close */
        }
        return;
    }
#ifndef SOFT80
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
#endif

    switch (k) {
        case KEY_RETURN:
        case 0x8D:  /* shift-Return: shift lock is easy to leave on */
            send_message();
            break;
        case 133: /* F1 */
#ifdef SOFT80
            /* Server-fed menu module; non-idle falls back to the
               resident menu (it still offers X = cancel mid-stream) */
            if (state == ST_IDLE) menu_module_open();
            else menu_open();
#else
            menu_open();
#endif
            break;
        case KEY_F2:
            new_conversation();
            break;
        case 134: /* F3 */
            cancel_stream();
            break;
        case KEY_F5:
#ifdef SOFT80
            if (state == ST_IDLE) convmgr_open();
#else
            if (state == ST_IDLE) conv_open();
#endif
            break;
        case 136: /* F7 */
            send_command("/help");
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
    boot_device_init();  /* first: $BA still holds the LOADing drive */
    diag_init();         /* lay the canary before anything runs deep */
    diag_crumb(DC_BOOT);
    proto_init(&proto, payload_buffer, MAX_PAYLOAD);
    ui_init();
    editor_init();

#ifndef CONNECT_DIRECT
    /* Disk config before the ACIA wakes: the KERNAL still owns the
       interrupt vectors, so the (JiffyDOS) LOAD runs undisturbed.
       No valid c64llm.cfg -> pull in the config editor module. */
    if (!config_load()) {
#ifdef SOFT80
        if (module_load("c64llm.1")) {
            mod_config_run();
            ui_redraw_all();
            editor_redraw();
        }
#endif
    }
    build_dial_string();
#endif

    ui_status("Initializing ACIA...");
    acia_init_hw();

#ifndef CONNECT_DIRECT
    while (!modem_connect()) {
        ui_status("Connect failed - any key retries.");
        while (!kbhit());
        cgetc();
    }
#endif

    for (;;) {
        ui_status("Contacting server...");
        proto_send_ping();
        if (wait_for_ack(8000)) break;
        ui_status("No server response - any key retries.");
        while (!kbhit());
        cgetc();
    }

    proto_send_new_conversation();
    wait_for_ack(4000);

    chat_start(2);
    chat_append_petscii("Connected to ");
    chat_append_petscii(g_host);
    chat_append_petscii(":");
    chat_append_petscii(g_port);
    chat_finish();

    /* Drop any autostart leftovers before accepting input; the harness
       waits for the Ready status, so drain first */
    while (kbhit()) cgetc();
    ui_status("Ready. Type your message.");

    /* Response watchdog: while awaiting/receiving a reply, track the last
       time serial data arrived. If the link goes quiet for too long the
       request or a frame was lost in transit (no client-side ACK exists
       for the request itself), so abort to idle instead of hanging. */
    for (;;) {
        if (pump_serial()) {
            watchdog_reset();
        } else if (state != ST_IDLE && watchdog_expired()) {
            uint8_t was_loading = (state == ST_LOADING);
            state = ST_IDLE;
#ifdef SOFT80
            if (sid_active || img_active) {  /* transfer lost its tail */
                sid_active = 0;
                if (img_active) {
                    img_active = 0;
                    img_close();
                }
                proto_init(&proto, payload_buffer, MAX_PAYLOAD);
                proto_send_nak();   /* lets the proxy resend the transfer */
                ui_status("Transfer timed out - retrying...");
                continue;
            }
#endif
            proto_init(&proto, payload_buffer, MAX_PAYLOAD);  /* resync */
            if (was_loading) chat_freeze(0);
            chat_start(2);
            chat_append_petscii(was_loading
                ? "(load incomplete - press F5 to retry)"
                : "(no response - message may be lost; try again)");
            chat_finish();
            ui_status("Timed out. Ready.");
        }
#ifdef SOFT80
        /* Modules that have to keep moving without a key or a frame to
           drive them (the jukebox's clock and signal meter) */
        if (mod_tick_hook) mod_tick_hook();
#endif
        if (kbhit()) {
            handle_key(cgetc());
        }
    }

    return 0;
}

#endif /* !DEBUG_CLIENT */
