/**
 * C64 LLM Client - Serial/ACIA Interface
 */

#ifndef SERIAL_H
#define SERIAL_H

#include "common.h"

/* Connection configuration */
#define DEFAULT_HOST    "raspberrypi.local"
#define DEFAULT_PORT    6400

/**
 * Initialize ACIA and connect to server
 * Returns 0 on success, error code on failure
 */
uint8_t serial_init(const char* hostname, uint16_t port);

/**
 * Dial connection using ATDT command
 * dial_str should be formatted like "ATDT192.168.1.39:6400"
 * Returns 0 on success
 */
uint8_t serial_dial(const char* dial_str);

/**
 * Disconnect and reset ACIA
 */
void serial_disconnect(void);

/**
 * Check if connected
 */
uint8_t serial_is_connected(void);

/**
 * Check if data available to read
 */
uint8_t serial_available(void);

/**
 * Non-blocking read (returns 0 if no data)
 */
uint8_t serial_read(void);

/**
 * Read multiple bytes
 * Returns count actually read
 */
uint16_t serial_read_buffer(uint8_t* dest, uint16_t max_len);

/**
 * Non-blocking write (returns 0 if buffer full)
 */
uint8_t serial_write(uint8_t byte);

/**
 * Write multiple bytes
 * Returns count actually written
 */
uint16_t serial_write_buffer(const uint8_t* data, uint16_t len);

/**
 * Check if TX buffer has space
 */
uint8_t serial_can_write(void);

/**
 * Flush TX buffer (blocking until sent)
 */
void serial_flush(void);

/**
 * Bytes currently waiting in the RX ring buffer
 */
uint8_t serial_rx_count(void);

/* Internal functions (implemented in ASM) */
void __fastcall__ acia_init_hw(void);
uint8_t __fastcall__ acia_send_at_command(const char* cmd);
uint8_t acia_get_status(void);

#endif /* SERIAL_H */
