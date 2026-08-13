/**
 * LLM64 Client - Common Definitions
 */

#ifndef COMMON_H
#define COMMON_H

/* cc65 doesn't have stdint.h in older versions, define our own */
typedef unsigned char uint8_t;
typedef unsigned int uint16_t;
typedef unsigned long uint32_t;
typedef signed char int8_t;
typedef signed int int16_t;
typedef signed long int32_t;

/* Memory layout constants */
/* Note: SCREEN_RAM and COLOR_RAM may be defined in c64.h */
#ifndef SCREEN_RAM
#define SCREEN_RAM       0x0400
#endif
#ifndef COLOR_RAM
#define COLOR_RAM        0xD800
#endif
#define SCREEN_WIDTH     40
#define SCREEN_HEIGHT    25

/* Logical text width: 40 (hardware chars) or 80 (SOFT80 bitmap mode) */
#ifdef SOFT80
#define TEXT_COLS        80
#else
#define TEXT_COLS        40
#endif

/* Application memory areas */
#define SCREEN_BUFFER    0x2000  /* 1024 bytes double buffer */
#define COLOR_BUFFER     0x2400  /* 1024 bytes color shadow */
#define EDIT_BUFFER      0x2800  /* 1024 bytes text edit */
#define RX_BUFFER        0x2C00  /* 1024 bytes serial RX */
#define TX_BUFFER        0x3000  /* 256 bytes serial TX */
#define CONV_BUFFER      0x3100  /* 768 bytes conversation */
#define CONV_LIST        0x3400  /* 1KB conversation list */
#define SCRATCH_BUFFER   0x3800  /* Scratch space */

/* Buffer sizes */
#define EDIT_BUFFER_SIZE    1024
#define RX_BUFFER_SIZE      1024
#define TX_BUFFER_SIZE      256
#define CONV_BUFFER_SIZE    768
#define MAX_MESSAGE_LEN     512

/* ACIA registers (SwiftLink at $DE00) */
#define ACIA_BASE       0xDE00
#define ACIA_DATA       (*(volatile uint8_t*)0xDE00)
#define ACIA_STATUS     (*(volatile uint8_t*)0xDE01)
#define ACIA_COMMAND    (*(volatile uint8_t*)0xDE02)
#define ACIA_CONTROL    (*(volatile uint8_t*)0xDE03)

/* ACIA status bits */
#define ACIA_SR_RDRF    0x08    /* Receive Data Register Full */
#define ACIA_SR_TDRE    0x10    /* Transmit Data Register Empty */
#define ACIA_SR_DCD     0x20    /* Data Carrier Detect */
#define ACIA_SR_DSR     0x40    /* Data Set Ready */
#define ACIA_SR_IRQ     0x80    /* Interrupt Request */

/* ACIA command register bits */
#define ACIA_CMD_DTR    0x01    /* Data Terminal Ready */
#define ACIA_CMD_RIE    0x02    /* Receiver Interrupt Enable */

/* ACIA control register - 9600 baud, 8N1 */
#define ACIA_CTRL_9600  0x1E    /* 9600 baud, 8 data, no parity, 1 stop */

/* Colors - defined in c64.h, no need to redefine */
/* Just use COLOR_BLACK, COLOR_WHITE, etc. from c64.h */

/* Screen layout */
#define CHAT_START_ROW  1
#define CHAT_HEIGHT     19
#define SEPARATOR_ROW   20
#define EDIT_LABEL_ROW  21
#define EDIT_START_ROW  22
#define EDIT_HEIGHT     3
#define STATUS_ROW      24

/* Application states */
typedef enum {
    STATE_CONNECTING,
    STATE_CHAT,
    STATE_SIDEBAR,
    STATE_HELP
} AppState;

/* Message roles */
typedef enum {
    ROLE_USER = 0,
    ROLE_ASSISTANT = 1,
    ROLE_SYSTEM = 2
} MessageRole;

/* Protocol message types - using printable ASCII to avoid tcpser/IP232 corruption */
#define MSG_CHAT_REQUEST        0x31  /* '1' - was 0x01 */
#define MSG_CANCEL_REQUEST      0x32  /* '2' - was 0x02 */
#define MSG_LIST_CONVERSATIONS  0x33  /* '3' - was 0x03 */
#define MSG_LOAD_CONVERSATION   0x34  /* '4' - was 0x04 */
#define MSG_NEW_CONVERSATION    0x35  /* '5' - was 0x05 */
#define MSG_PING                0x36  /* '6' - was 0x06 */
#define MSG_LIST_MODELS         0x37  /* '7' */
#define MSG_SET_MODEL           0x38  /* '8' */
#define MSG_DELETE_CONVERSATION 0x39  /* '9' - id(4); ACK/NAK */
#define MSG_STAR_CONVERSATION   0x3A  /* ':' - id(4) toggle star; ACK/NAK */
#define MSG_GET_MENU            0x3B  /* ';' - request the server-fed menu */
#define MSG_GET_NOWPLAYING      0x3C  /* '<' - ask what is playing */
#define MSG_FAV_TUNE            0x3D  /* '=' - toggle favorite on it */
#define MSG_SET_BAUD            0x3E  /* '>' - tell the proxy our wire
                                         rate so its bulk pacing tracks
                                         it. Payload: 2 bytes LE, nominal
                                         baud / 100 (48/96/192). Fire and
                                         forget - no ACK. */
#define MSG_ACK                 0x40  /* '@' - was 0x10 */
#define MSG_NAK                 0x41  /* 'A' - was 0x11 */
#define MSG_CHAT_CHUNK          0x50  /* 'P' - was 0x20 */
#define MSG_CHAT_DONE           0x51  /* 'Q' - was 0x21 */
#define MSG_CHAT_ERROR          0x52  /* 'R' - was 0x22 */
#define MSG_CONVERSATION_LIST   0x53  /* 'S' - was 0x23 */
#define MSG_CONVERSATION_DATA   0x54  /* 'T' - was 0x24 */
#define MSG_STATUS              0x55  /* 'U' - was 0x25 */
#define MSG_MODEL_LIST          0x56  /* 'V' */
#define MSG_SID_BEGIN           0x57  /* 'W' - streamed SID: metadata */
#define MSG_SID_DATA            0x58  /* 'X' - streamed SID: raw bytes */
#define MSG_SID_END             0x59  /* 'Y' - streamed SID: start play */
#define MSG_IMG_BEGIN           0x5A  /* 'Z' - fullscreen image incoming */
#define MSG_IMG_DATA            0x5B  /* '[' - image bytes (bitmap+matrix) */
#define MSG_IMG_END             0x5C  /* '\' - image complete, show it */
#define MSG_HINT                0x5D  /* ']' - [flags(bit0: pic)][pics]
                                         [chrome\0]; chrome is the
                                         proxy-composed right-hand
                                         status text (place, music) */
#define MSG_MENU_LIST           0x5E  /* '^' - menu entries: [n][more]
                                         then [key][label\0][cmd\0] each;
                                         cmd "!x" = local action x */
#define MSG_NOTICE              0x60  /* '`' - out-of-band system line
                                         (dice results); rendered as its
                                         own chat block, not the reply */
#define MSG_NOWPLAYING          0x5F  /* '_' - jukebox state: [flags]
                                         [elapsed:2][secs:2] then
                                         title\0 author\0 mood\0 */
#define MSG_MUSIC_STOP          0x61  /* 'a' - silence a streamed SID.
                                         The proxy owns whether music
                                         is playing (it decides when the
                                         narrator may start one again),
                                         so stopping goes through it -
                                         /music stop, and the jukebox's
                                         own stop key, both land here */
#define MSG_PRINT_BEGIN         0x62  /* 'b' - open IEC device 4:
                                         [flags][nblocks]. flags bit0 =
                                         business charset (secondary
                                         address 7), bit1 = form feed
                                         before close. ACK when open,
                                         NAK if there is no printer */
#define MSG_PRINT_DATA          0x63  /* 'c' - one block of ASCII text
                                         (<=240 bytes) to print. ACK per
                                         block: the proxy sends nothing
                                         else while we hold serial RX
                                         paused for the IEC write */
#define MSG_PRINT_END           0x64  /* 'd' - close the channel */

/* Home Assistant. The proxy formats, colors and resamples; these
   frames carry placed cells and computed pixel columns. */
#define MSG_GET_HA              0x45  /* 'E' - [view] send me this screen */
#define MSG_HA_ACTION           0x46  /* 'F' - [key][view]; the proxy
                                         maps the key to an entity */
#define MSG_HA_ROWS             0x6D  /* 'm' - [first][count] then per
                                         row [color:40][cells:80],
                                         chunked to fit MAX_PAYLOAD */
#define MSG_HA_PLOT             0x6F  /* 'o' - [row][rows][x0][n] then n
                                         y-bytes from the band top,
                                         already resampled and scaled */

/* Pseudo message type returned by proto_process_byte on checksum failure
   (not a wire value - chosen outside the protocol's type range) */
#define PROTO_CRC_FAIL          0xFE

/* Protocol constants */
#define SYNC_BYTE       0x42  /* 'B' - safe ASCII byte (was 0xC6, corrupted by VICE IP232) */
#define MAX_PAYLOAD     512

/* Function keys (PETSCII codes) */
#define KEY_F1          133
#define KEY_F3          134
#define KEY_F5          135
#define KEY_F7          136

/* Control keys */
#define KEY_RETURN      13
#define KEY_DEL         20
/* SHIFT+DEL is INSERT ($94) on a C64, not delete. This editor has no
   insert mode - it always inserts at the cursor - so the key is free,
   and treating it as another backspace matters: with SHIFT LOCK down
   the plain DEL is unreachable, so shifted was the ONLY delete
   available and it did nothing. */
#define KEY_INST        148
#define KEY_HOME        19
#define KEY_CLR         147
#define CTRL_A          1
#define CTRL_E          5
#define CTRL_K          11
#define CTRL_D          4

/* Cursor movement */
#define KEY_CRSR_DOWN   17
#define KEY_CRSR_UP     145
#define KEY_CRSR_RIGHT  29
#define KEY_CRSR_LEFT   157

#endif /* COMMON_H */
