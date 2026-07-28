/*
 * Wire protocol unit test - builds on the host with cc.
 *
 * The framing is the one piece of this client that has to agree with
 * another implementation byte for byte, and it is also the piece with
 * the trap in it (the +0x20 length bias, applied with 8-bit wrap).
 * Testing it here means the emulator is never in the loop for a
 * protocol bug.
 */

#include <stdio.h>
#include <string.h>
#include "wire.h"

static int failures = 0;

static void check(int cond, const char *what)
{
    printf("%-56s %s\n", what, cond ? "ok" : "FAIL");
    if (!cond)
        failures++;
}

static unsigned char rxbuf[WIRE_MAX_PAYLOAD];

/* Feed a whole buffer; return the last frame type produced. */
static unsigned char feed(WireRx *rx, const unsigned char *b, unsigned n)
{
    unsigned i;
    unsigned char last = WIRE_NONE, t;
    for (i = 0; i < n; i++) {
        t = wire_rx_byte(rx, b[i]);
        if (t != WIRE_NONE)
            last = t;
    }
    return last;
}

int main(void)
{
    unsigned char out[WIRE_MAX_PAYLOAD + 8];
    unsigned char big[600];
    WireRx rx;
    unsigned n, i;

    /* 1. A short frame matches the proxy's encoder byte for byte. */
    n = wire_frame(out, MSG_PING, NULL, 0);
    check(n == 5, "PING frame is 5 bytes");
    check(out[0] == 0x42, "sync is 'B'");
    check(out[1] == MSG_PING, "type survives");
    check(out[2] == 0x20 && out[3] == 0x20, "zero length encodes as 0x20,0x20");
    check(out[4] == MSG_PING, "crc of an empty payload is the type");

    /* 2. Text frames carry the NUL, as the proxy's parsers expect. */
    n = wire_frame(out, MSG_CHAT_REQUEST, (const unsigned char *)"hi", 3);
    check(n == 8, "3-byte payload makes an 8-byte frame");
    check(out[2] == 0x23, "length 3 encodes as 0x23");
    check(out[7] == (unsigned char)(MSG_CHAT_REQUEST ^ 3 ^ 'h' ^ 'i' ^ 0),
          "crc covers type, length and payload");

    /* 3. Round trip through the parser. */
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    check(feed(&rx, out, n) == MSG_CHAT_REQUEST, "parser accepts our own frame");
    check(rx.len == 3 && memcmp(rx.payload, "hi", 3) == 0,
          "payload comes back intact");

    /* 4. The bias wraps: a length whose low byte is >= 0xE0 encodes to
       a value below 0x20, and the decoder has to subtract in 8 bits.
       224 is the smallest such length and the one a naive decoder
       reads as 0. */
    memset(big, 'x', sizeof(big));
    n = wire_frame(out, MSG_CHAT_CHUNK, big, 224);
    check(out[2] == 0x00, "length 224 encodes to 0x00 (the wrap case)");
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    check(feed(&rx, out, n) == MSG_CHAT_CHUNK, "wrapped length parses");
    check(rx.len == 224, "wrapped length decodes back to 224");

    /* 5. Two-byte lengths. */
    n = wire_frame(out, MSG_IMG_DATA, big, 600);
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    check(feed(&rx, out, n) == MSG_IMG_DATA, "600-byte frame parses");
    check(rx.len == 600, "high length byte decodes");

    /* 6. A corrupt byte is caught, and the parser resyncs rather than
       wedging - which is what the C64's link watchdog exists for. */
    n = wire_frame(out, MSG_STATUS, (const unsigned char *)"ab", 3);
    out[5] ^= 0xFF;
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    check(feed(&rx, out, n) == WIRE_CRC_FAIL, "corruption fails the checksum");
    check(!wire_rx_mid_frame(&rx), "parser is back at sync after a bad frame");

    n = wire_frame(out, MSG_PING, NULL, 0);
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    check(feed(&rx, out, n) == MSG_PING, "and the next good frame is accepted");

    /* 7. Garbage before a frame is discarded, not misread. */
    wire_rx_init(&rx, rxbuf, sizeof(rxbuf));
    for (i = 0; i < 7; i++)
        wire_rx_byte(&rx, (unsigned char)(i * 17 + 1));
    n = wire_frame(out, MSG_ACK, NULL, 0);
    check(feed(&rx, out, n) == MSG_ACK, "resyncs through leading garbage");

    /* 8. A length past the buffer is refused without overrunning it. */
    wire_rx_init(&rx, rxbuf, 16);
    n = wire_frame(out, MSG_CHAT_CHUNK, big, 600);
    check(feed(&rx, out, n) == WIRE_NONE, "oversized frame is dropped");
    check(!wire_rx_mid_frame(&rx), "and does not leave the parser stuck");

    printf("\n%s\n", failures ? "FAILURES" : "all wire tests passed");
    return failures ? 1 : 0;
}
