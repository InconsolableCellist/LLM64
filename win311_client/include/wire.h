/*
 * LLM64 for Windows - wire protocol
 *
 * The same framing the C64 speaks (docs/01, docs/16 section 2), and
 * deliberately free of any Windows dependency: this file builds under
 * the 16-bit Watcom compiler, under mingw for the Win32 build, and
 * under host gcc for the unit test. Everything platform-shaped lives
 * in net.c.
 *
 *   SYNC(0x42) TYPE LEN_LO+0x20 LEN_HI+0x20 PAYLOAD... CRC
 *
 * The +0x20 bias on both length bytes is not decoration: it exists so
 * a frame header cannot contain NUL or a Telnet IAC, which the C64's
 * modem bridges used to eat. It wraps modulo 256, so the decoder must
 * subtract in 8 bits.
 */

#ifndef WIRE_H
#define WIRE_H

#define WIRE_SYNC        0x42
#define WIRE_MAX_PAYLOAD 2048

/* Client -> proxy */
#define MSG_CHAT_REQUEST        0x31
#define MSG_CANCEL_REQUEST      0x32
#define MSG_LIST_CONVERSATIONS  0x33
#define MSG_LOAD_CONVERSATION   0x34
#define MSG_NEW_CONVERSATION    0x35
#define MSG_PING                0x36
#define MSG_LIST_MODELS         0x37
#define MSG_SET_MODEL           0x38
#define MSG_DELETE_CONVERSATION 0x39
#define MSG_STAR_CONVERSATION   0x3A
#define MSG_GET_MENU            0x3B
#define MSG_GET_NOWPLAYING      0x3C
#define MSG_FAV_TUNE            0x3D
#define MSG_SET_BAUD            0x3E
#define MSG_ACK                 0x40
#define MSG_NAK                 0x41

/* Proxy -> client */
#define MSG_CHAT_CHUNK          0x50
#define MSG_CHAT_DONE           0x51
#define MSG_CHAT_ERROR          0x52
#define MSG_CONVERSATION_LIST   0x53
#define MSG_CONVERSATION_DATA   0x54
#define MSG_STATUS              0x55
#define MSG_MODEL_LIST          0x56
#define MSG_SID_BEGIN           0x57
#define MSG_SID_DATA            0x58
#define MSG_SID_END             0x59
#define MSG_IMG_BEGIN           0x5A
#define MSG_IMG_DATA            0x5B
#define MSG_IMG_END             0x5C
#define MSG_HINT                0x5D
#define MSG_MENU_LIST           0x5E
#define MSG_NOWPLAYING          0x5F
#define MSG_NOTICE              0x60
#define MSG_MUSIC_STOP          0x61
#define MSG_PRINT_BEGIN         0x62
#define MSG_PRINT_DATA          0x63
#define MSG_PRINT_END           0x64

/* Not wire values: parser verdicts outside the protocol's type range */
#define WIRE_NONE               0x00
#define WIRE_CRC_FAIL           0xFE

/* In-band marker cells carried inside CHAT_CHUNK text (docs/08).
   Rendering them is the text pane's job; the parser passes them
   through untouched. */
#define MARK_CLOSE       0x01
#define MARK_BOLD_ON     0x02
#define MARK_BOLD_OFF    0x03
#define MARK_COLOR_BASE  0x10   /* 0x10|c, c = 1..14 */

enum wire_state {
    WS_SYNC = 0,
    WS_TYPE,
    WS_LEN,
    WS_PAYLOAD,
    WS_CRC
};

typedef struct {
    int            state;
    unsigned char  type;
    unsigned int   len;
    unsigned int   got;
    unsigned char  lenbuf[2];
    unsigned char *payload;   /* caller-owned, cap bytes */
    unsigned int   cap;
    unsigned long  frames;    /* accepted frames, for diagnostics */
    unsigned long  crc_fails;
} WireRx;

void wire_rx_init(WireRx *rx, unsigned char *buf, unsigned int cap);

/* Feed one byte. Returns the message type once a whole frame has
   arrived and checked out, WIRE_CRC_FAIL if it did not, WIRE_NONE
   otherwise. On a returned type, rx->payload holds rx->len bytes. */
unsigned char wire_rx_byte(WireRx *rx, unsigned char b);

/* Nonzero if the parser is part-way through a frame. A stall here is
   how the C64 detects a wedged link; kept for the same reason. */
int wire_rx_mid_frame(const WireRx *rx);

/* Build a frame into out, which must hold len + 5 bytes. Returns the
   number of bytes written. */
unsigned int wire_frame(unsigned char *out, unsigned char type,
                        const unsigned char *payload, unsigned int len);

#endif /* WIRE_H */
