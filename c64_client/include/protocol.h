/**
 * C64 LLM Client - Protocol Handler
 */

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include "common.h"

/* Protocol state */
typedef enum {
    PROTO_SYNC_SEARCHING,
    PROTO_READING_TYPE,
    PROTO_READING_LENGTH,
    PROTO_READING_PAYLOAD,
    PROTO_VALIDATING_CRC
} ProtoState;

/* Protocol context */
typedef struct {
    ProtoState state;
    uint8_t msg_type;
    uint16_t msg_length;
    uint16_t bytes_read;
    uint8_t* payload_buf;
    uint16_t payload_max;
} ProtoContext;

/**
 * Initialize protocol handler
 */
void proto_init(ProtoContext* ctx, uint8_t* payload_buffer, uint16_t buffer_size);

/**
 * Process one byte from serial (state machine)
 * Returns message type if complete message received, 0 otherwise
 */
uint8_t proto_process_byte(ProtoContext* ctx, uint8_t byte);

/**
 * Fast path for streaming: while proto_in_payload(), call
 * proto_fill_payload() to bulk-copy from the serial ring buffer instead
 * of feeding proto_process_byte one byte at a time.
 */
uint8_t proto_in_payload(ProtoContext* ctx);
void proto_fill_payload(ProtoContext* ctx);

/**
 * Get last received message payload
 */
uint8_t* proto_get_payload(ProtoContext* ctx);

/**
 * Get last received message length
 */
uint16_t proto_get_length(ProtoContext* ctx);

/**
 * Send a protocol message
 */
void proto_send_message(uint8_t msg_type, const uint8_t* payload, uint16_t length);

/**
 * Helper functions for common messages
 */
void proto_send_text(uint8_t msg_type, const char* petscii_text);
void proto_send_ack(void);
void proto_send_nak(void);
void proto_send_ping(void);
void proto_send_chat(const char* text);
void proto_send_new_conversation(void);
void proto_send_list_conversations(void);
void proto_send_cancel(void);

/**
 * Calculate CRC
 */
uint8_t proto_calc_crc(uint8_t msg_type, uint16_t length, const uint8_t* payload);

/* XOR of len payload bytes, seeded from 0 (crc.s). The header bytes
   stay in proto_calc_crc's C; this is only the hot per-byte sweep. */
uint8_t __fastcall__ crc_xor(const uint8_t* buf, uint16_t len);

#endif /* PROTOCOL_H */
