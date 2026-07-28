/* LLM64 for Windows - wire protocol (see include/wire.h) */

#include "wire.h"

void wire_rx_init(WireRx *rx, unsigned char *buf, unsigned int cap)
{
    rx->state = WS_SYNC;
    rx->type = 0;
    rx->len = 0;
    rx->got = 0;
    rx->payload = buf;
    rx->cap = cap;
    rx->frames = 0;
    rx->crc_fails = 0;
}

int wire_rx_mid_frame(const WireRx *rx)
{
    return rx->state != WS_SYNC;
}

static unsigned char wire_crc(const WireRx *rx)
{
    unsigned int i;
    unsigned char crc;

    crc = (unsigned char)(rx->type
                          ^ (rx->len & 0xFF)
                          ^ ((rx->len >> 8) & 0xFF));
    for (i = 0; i < rx->len; i++)
        crc ^= rx->payload[i];
    return crc;
}

unsigned char wire_rx_byte(WireRx *rx, unsigned char b)
{
    switch (rx->state) {
    case WS_SYNC:
        if (b == WIRE_SYNC)
            rx->state = WS_TYPE;
        return WIRE_NONE;

    case WS_TYPE:
        rx->type = b;
        rx->got = 0;
        rx->state = WS_LEN;
        return WIRE_NONE;

    case WS_LEN:
        rx->lenbuf[rx->got++] = b;
        if (rx->got < 2)
            return WIRE_NONE;
        /* Undo the +0x20 bias in 8 bits - it was applied with
           wrap-around, so a 0xE0-length byte came out as 0x00. */
        rx->len = (unsigned int)((unsigned char)(rx->lenbuf[0] - 0x20))
                | (unsigned int)((unsigned char)(rx->lenbuf[1] - 0x20)) << 8;
        rx->got = 0;
        if (rx->len > rx->cap) {
            /* Too big to hold. Resyncing is the only honest move: the
               payload would overrun, and a truncated one would fail
               CRC anyway. */
            rx->state = WS_SYNC;
            return WIRE_NONE;
        }
        rx->state = rx->len ? WS_PAYLOAD : WS_CRC;
        return WIRE_NONE;

    case WS_PAYLOAD:
        rx->payload[rx->got++] = b;
        if (rx->got >= rx->len)
            rx->state = WS_CRC;
        return WIRE_NONE;

    case WS_CRC:
    default:
        rx->state = WS_SYNC;
        if (b == wire_crc(rx)) {
            rx->frames++;
            return rx->type;
        }
        rx->crc_fails++;
        return WIRE_CRC_FAIL;
    }
}

unsigned int wire_frame(unsigned char *out, unsigned char type,
                        const unsigned char *payload, unsigned int len)
{
    unsigned int i;
    unsigned char crc;

    out[0] = WIRE_SYNC;
    out[1] = type;
    out[2] = (unsigned char)((len & 0xFF) + 0x20);
    out[3] = (unsigned char)(((len >> 8) & 0xFF) + 0x20);

    crc = (unsigned char)(type ^ (len & 0xFF) ^ ((len >> 8) & 0xFF));
    for (i = 0; i < len; i++) {
        out[4 + i] = payload[i];
        crc ^= payload[i];
    }
    out[4 + len] = crc;
    return len + 5;
}
