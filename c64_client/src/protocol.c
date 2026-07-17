/**
 * C64 LLM Client - Protocol Implementation
 */

#include "protocol.h"
#include "serial.h"
#include "text.h"
#include <string.h>

/* Initialize protocol handler */
void proto_init(ProtoContext* ctx, uint8_t* payload_buffer, uint16_t buffer_size) {
    ctx->state = PROTO_SYNC_SEARCHING;
    ctx->msg_type = 0;
    ctx->msg_length = 0;
    ctx->bytes_read = 0;
    ctx->payload_buf = payload_buffer;
    ctx->payload_max = buffer_size;
}

/* Calculate XOR checksum */
uint8_t proto_calc_crc(uint8_t msg_type, uint16_t length, const uint8_t* payload) {
    uint8_t crc = msg_type;
    uint16_t i;

    crc ^= (length & 0xFF);
    crc ^= ((length >> 8) & 0xFF);

    for (i = 0; i < length; i++) {
        crc ^= payload[i];
    }

    return crc;
}

/* Process one byte (state machine) */
uint8_t proto_process_byte(ProtoContext* ctx, uint8_t byte) {
    static uint8_t length_bytes[2];
    static uint8_t expected_crc;

    switch (ctx->state) {
        case PROTO_SYNC_SEARCHING:
            if (byte == SYNC_BYTE) {
                ctx->state = PROTO_READING_TYPE;
            }
            break;

        case PROTO_READING_TYPE:
            ctx->msg_type = byte;
            ctx->state = PROTO_READING_LENGTH;
            ctx->bytes_read = 0;
            break;

        case PROTO_READING_LENGTH:
            length_bytes[ctx->bytes_read++] = byte;
            if (ctx->bytes_read >= 2) {
                /* Decode length bytes (subtract 0x20 offset) and combine little-endian */
                uint8_t len_lo = length_bytes[0] - 0x20;
                uint8_t len_hi = length_bytes[1] - 0x20;
                ctx->msg_length = len_lo | (len_hi << 8);
                ctx->bytes_read = 0;

                if (ctx->msg_length > 0) {
                    if (ctx->msg_length > ctx->payload_max) {
                        /* Payload too large, reset */
                        ctx->state = PROTO_SYNC_SEARCHING;
                    } else {
                        ctx->state = PROTO_READING_PAYLOAD;
                    }
                } else {
                    /* Zero-length payload, go to CRC */
                    ctx->state = PROTO_VALIDATING_CRC;
                }
            }
            break;

        case PROTO_READING_PAYLOAD:
            ctx->payload_buf[ctx->bytes_read++] = byte;
            if (ctx->bytes_read >= ctx->msg_length) {
                ctx->state = PROTO_VALIDATING_CRC;
            }
            break;

        case PROTO_VALIDATING_CRC:
            /* Validate CRC */
            expected_crc = proto_calc_crc(ctx->msg_type, ctx->msg_length, ctx->payload_buf);

            if (byte == expected_crc) {
                /* Valid message! */
                uint8_t msg_type = ctx->msg_type;
                ctx->state = PROTO_SYNC_SEARCHING;
                return msg_type;  /* Return message type */
            } else {
                /* CRC mismatch */
                ctx->state = PROTO_SYNC_SEARCHING;
                return PROTO_CRC_FAIL;
            }
            break;
    }

    return 0;  /* No complete message yet */
}

/* Get payload pointer */
uint8_t* proto_get_payload(ProtoContext* ctx) {
    return ctx->payload_buf;
}

/* Get payload length */
uint16_t proto_get_length(ProtoContext* ctx) {
    return ctx->msg_length;
}

/* Send a protocol message */
void proto_send_message(uint8_t msg_type, const uint8_t* payload, uint16_t length) {
    uint8_t crc;
    uint16_t i;
    uint8_t len_lo, len_hi;

    /* Encode length bytes to avoid NUL (add 0x20 to shift into printable range) */
    len_lo = (length & 0xFF) + 0x20;
    len_hi = ((length >> 8) & 0xFF) + 0x20;

    /* Send SYNC byte */
    serial_write(SYNC_BYTE);

    /* Send type */
    serial_write(msg_type);

    /* Send encoded length (little-endian) */
    serial_write(len_lo);
    serial_write(len_hi);

    /* Send payload */
    for (i = 0; i < length; i++) {
        serial_write(payload[i]);
    }

    /* Calculate and send CRC (using original length, not encoded) */
    crc = proto_calc_crc(msg_type, length, payload);
    serial_write(crc);

    /* Flush to ensure transmission */
    serial_flush();
}

/* Helper: Send ACK */
void proto_send_ack(void) {
    proto_send_message(MSG_ACK, NULL, 0);
}

/* Helper: Send NAK */
void proto_send_nak(void) {
    proto_send_message(MSG_NAK, NULL, 0);
}

/* Helper: Send PING */
void proto_send_ping(void) {
    proto_send_message(MSG_PING, NULL, 0);
}

/* Helper: Send chat message (converts PETSCII text to ASCII for the wire) */
void proto_send_chat(const char* text) {
    uint16_t len = strlen(text);
    uint16_t i;
    uint8_t buffer[256];  /* Temporary buffer */

    for (i = 0; i < len; i++) {
        buffer[i] = petscii_to_ascii((uint8_t)text[i]);
    }
    buffer[len] = 0;

    proto_send_message(MSG_CHAT_REQUEST, buffer, len + 1);
}

/* Helper: Send new conversation */
void proto_send_new_conversation(void) {
    proto_send_message(MSG_NEW_CONVERSATION, NULL, 0);
}

/* Helper: Send list conversations */
void proto_send_list_conversations(void) {
    proto_send_message(MSG_LIST_CONVERSATIONS, NULL, 0);
}

/* Helper: Send cancel */
void proto_send_cancel(void) {
    proto_send_message(MSG_CANCEL_REQUEST, NULL, 0);
}
