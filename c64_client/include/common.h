/**
 * C64 LLM Client - Common Definitions
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
#define MSG_ACK                 0x40  /* '@' - was 0x10 */
#define MSG_NAK                 0x41  /* 'A' - was 0x11 */
#define MSG_CHAT_CHUNK          0x50  /* 'P' - was 0x20 */
#define MSG_CHAT_DONE           0x51  /* 'Q' - was 0x21 */
#define MSG_CHAT_ERROR          0x52  /* 'R' - was 0x22 */
#define MSG_CONVERSATION_LIST   0x53  /* 'S' - was 0x23 */
#define MSG_CONVERSATION_DATA   0x54  /* 'T' - was 0x24 */
#define MSG_STATUS              0x55  /* 'U' - was 0x25 */

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
