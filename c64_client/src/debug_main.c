/**
 * C64 LLM Client - Scripted debug session (build with DEBUG_CLIENT=1)
 *
 * Runs a fixed connect -> PING -> new conversation -> chat -> stream
 * sequence with on-screen diagnostics. The automated e2e tests drive this
 * build; the interactive TUI lives in main.c.
 */
#ifdef DEBUG_CLIENT

#include <c64.h>
#include <conio.h>
#include <stdio.h>
#include <string.h>
#include "common.h"
#include "serial.h"
#include "protocol.h"
#include "text.h"

/* Server configuration - override with -DSERVER_IP=\"x.x.x.x\" at build time */
#ifndef SERVER_IP
#define SERVER_IP   "192.168.1.39"
#endif
#ifndef SERVER_PORT
#define SERVER_PORT "6400"
#endif

/* Message the scripted debug session sends */
#ifndef TEST_MESSAGE
#define TEST_MESSAGE "Hello from C64!"
#endif

/* Global protocol context */
ProtoContext proto;
uint8_t payload_buffer[MAX_PAYLOAD];

/* Debug row for modem output */
static uint8_t debug_row = 3;

/* Status display */
void show_status(const char* msg) {
    uint8_t i;
    gotoxy(0, STATUS_ROW);
    textcolor(COLOR_YELLOW);
    bgcolor(COLOR_BLUE);
    cputs(msg);
    /* Clear to end of line manually */
    for (i = wherex(); i < SCREEN_WIDTH; i++) {
        cputc(' ');
    }
}

/* Debug: show a line of modem output */
void debug_print(const char* prefix, const char* msg) {
    uint8_t i;
    gotoxy(0, debug_row);
    textcolor(COLOR_WHITE);
    bgcolor(COLOR_BLACK);
    cputs(prefix);
    cputs(msg);
    /* Clear to end of line */
    for (i = wherex(); i < SCREEN_WIDTH; i++) {
        cputc(' ');
    }
    debug_row++;
    if (debug_row > 20) debug_row = 3;  /* Wrap around */
}

/* Debug: show a hex byte */
void debug_hex(uint8_t byte) {
    static const char hex[] = "0123456789ABCDEF";
    cputc(hex[(byte >> 4) & 0x0F]);
    cputc(hex[byte & 0x0F]);
    cputc(' ');
}

/* Clear screen and setup */
void init_screen(void) {
    clrscr();
    bordercolor(COLOR_BLUE);
    bgcolor(COLOR_BLACK);
    textcolor(COLOR_LIGHTGREEN);
    *(uint8_t*)0xD018 = 0x17;  /* shifted charset: mixed-case text */

    /* Title */
    gotoxy(0, 0);
    cputs("C64 LLM CLIENT v0.1 - DEBUG MODE");
    
    /* Server info */
    gotoxy(0, 1);
    textcolor(COLOR_CYAN);
    cputs("Server: ");
    cputs(SERVER_IP);
    cputs(":");
    cputs(SERVER_PORT);
    cputs(" @ 9600 baud");

    /* Debug area header */
    gotoxy(0, 2);
    textcolor(COLOR_YELLOW);
    cputs("--- MODEM I/O ---");

    /* Status line */
    show_status("Initializing...");
}

/* Send AT command with debug output and capture response */
uint8_t send_at_debug(const char* cmd, char* response, uint8_t max_len) {
    uint8_t i;
    uint16_t timeout;
    uint8_t resp_idx = 0;
    uint8_t byte;
    uint8_t cmd_len;
    uint8_t echo_skip = 0;
    
    /* Show command being sent */
    debug_print("TX> ", cmd);
    
    /* Calculate command length for echo detection */
    for (cmd_len = 0; cmd[cmd_len] != 0; cmd_len++);
    
    /* Send the command character by character (modem wants ASCII) */
    for (i = 0; cmd[i] != 0; i++) {
        while (!serial_can_write());  /* Wait for TX ready */
        serial_write(petscii_to_ascii(cmd[i]));
    }
    
    /* Send CR */
    while (!serial_can_write());
    serial_write(13);
    
    /* Flush */
    serial_flush();
    
    /* Wait for response with timeout */
    timeout = 0;
    resp_idx = 0;
    
    /* Longer delay for modem to process */
    {
        uint16_t j;
        for (j = 0; j < 30000; j++);
    }
    
    /* Read response - skip echo of command if present */
    while (timeout < 5000 && resp_idx < max_len - 1) {
        if (serial_available()) {
            byte = serial_read();
            
            /* Skip echo of the command we sent (echo arrives as ASCII) */
            if (echo_skip < cmd_len && byte == petscii_to_ascii(cmd[echo_skip])) {
                echo_skip++;
                timeout = 0;
                continue;
            }

            /* Store printable chars that aren't part of echo */
            if (byte >= 32 && byte < 127) {
                response[resp_idx++] = ascii_to_petscii(byte);
            } else if (byte == 13 || byte == 10) {
                /* CR/LF - could be end of response line */
                if (resp_idx > 0) {
                    /* Got a real response, wait a bit more for additional data */
                    uint16_t extra_wait;
                    for (extra_wait = 0; extra_wait < 1000; extra_wait++) {
                        if (serial_available()) {
                            byte = serial_read();
                            if (byte >= 32 && byte < 127) {
                                response[resp_idx++] = ascii_to_petscii(byte);
                            } else if ((byte == 13 || byte == 10) && resp_idx > 0) {
                                break;
                            }
                            extra_wait = 0;
                        }
                    }
                    break;
                }
            }
            timeout = 0;  /* Reset timeout on data */
        } else {
            timeout++;
            /* Small delay */
            {
                uint8_t j;
                for (j = 0; j < 50; j++);
            }
        }
    }
    
    response[resp_idx] = 0;  /* Null terminate */
    
    /* Show response */
    if (resp_idx > 0) {
        debug_print("RX< ", response);
    } else {
        debug_print("RX< ", "(no response)");
    }
    
    return resp_idx;
}

/* Check if response contains a string */
uint8_t response_contains(const char* response, const char* search) {
    uint8_t i, j;
    for (i = 0; response[i] != 0; i++) {
        for (j = 0; search[j] != 0; j++) {
            if (response[i + j] != search[j]) break;
        }
        if (search[j] == 0) return 1;  /* Found */
    }
    return 0;
}

/* Wait for a specific message type with timeout */
uint8_t wait_for_message(uint8_t expected_type, uint16_t timeout_frames) {
    uint16_t frames = 0;

    while (frames < timeout_frames) {
        /* Check for serial data */
        if (serial_available()) {
            uint8_t byte = serial_read();
            uint8_t msg_type = proto_process_byte(&proto, byte);

            if (msg_type != 0) {
                /* Got a complete message */
                if (msg_type == expected_type) {
                    return 1;  /* Success */
                }
            }
        }

        /* Small delay (approximate frame) */
        {
            uint8_t i;
            for (i = 0; i < 100; i++);
        }
        frames++;
    }

    return 0;  /* Timeout */
}

/* Main program */
int main(void) {
    uint8_t result;
    char response[64];
    char dial_cmd[48];

    /* Initialize screen */
    init_screen();

    /* Initialize protocol */
    proto_init(&proto, payload_buffer, MAX_PAYLOAD);

    /* Show status */
    show_status("Initializing ACIA hardware...");

    /* Initialize ACIA hardware directly (don't use serial_init's AT commands) */
    acia_init_hw();
    
    debug_print("INFO", "ACIA initialized at $DE00 (9600 baud)");
    
    /* Show ACIA status register */
    {
        uint8_t status = acia_get_status();
        gotoxy(0, debug_row);
        textcolor(COLOR_CYAN);
        cputs("ACIA Status: ");
        debug_hex(status);
        /* Decode status bits */
        cputs(" [");
        if (status & 0x08) cputs("RDRF ");  /* Receive Data Register Full */
        if (status & 0x10) cputs("TDRE ");  /* Transmit Data Register Empty */
        if (!(status & 0x20)) cputs("DCD ");  /* Data Carrier Detect (active low) */
        if (!(status & 0x40)) cputs("DSR ");  /* Data Set Ready (active low) */
        if (status & 0x80) cputs("IRQ");    /* Interrupt */
        cputs("]");
        debug_row++;
        if (debug_row > 20) debug_row = 3;
    }
    
    /* Check status again after delay */
    {
        uint8_t status = acia_get_status();
        gotoxy(0, debug_row);
        textcolor(COLOR_CYAN);
        cputs("Status after delay: ");
        debug_hex(status);
        debug_row++;
        if (debug_row > 20) debug_row = 3;
    }

#ifdef CONNECT_DIRECT
    /* Direct mode: the ACIA pipe IS the connection (VICE rsdev -> proxy).
       No modem in the loop, so skip the Hayes AT handshake entirely. */
    debug_print("INFO", "Direct mode: no modem handshake");
#else
    /* Send ATZ (reset modem) with debug */
    show_status("Resetting modem...");
    send_at_debug("ATZ", response, sizeof(response));

    /* Send ATE0 (echo OFF - critical!) */
    show_status("Disabling echo...");
    send_at_debug("ATE0", response, sizeof(response));
    
    /* Send ATV1 (verbose responses - "OK" instead of "0") */
    show_status("Setting verbose mode...");
    send_at_debug("ATV1", response, sizeof(response));

    /* Build dial command with correct server IP */
    strcpy(dial_cmd, "ATDT");
    strcat(dial_cmd, SERVER_IP);
    strcat(dial_cmd, ":");
    strcat(dial_cmd, SERVER_PORT);
    
    /* Dial the server */
    show_status("Dialing server...");
    send_at_debug(dial_cmd, response, sizeof(response));
    
    /* Check for CONNECT in response */
    if (response_contains(response, "CONNECT")) {
        show_status("CONNECT received!");
    } else if (response_contains(response, "OK")) {
        /* Some modems return OK first, then CONNECT */
        debug_print("INFO", "Got OK, waiting for CONNECT...");
        
        /* Wait for CONNECT */
        {
            uint16_t timeout;
            uint8_t idx = 0;
            
            for (timeout = 0; timeout < 5000 && idx < sizeof(response) - 1; timeout++) {
                if (serial_available()) {
                    uint8_t byte = serial_read();
                    if (byte >= 32 && byte < 127) {
                        response[idx++] = ascii_to_petscii(byte);
                    }
                    timeout = 0;
                }
            }
            response[idx] = 0;
            
            if (idx > 0) {
                debug_print("RX< ", response);
            }
        }
        
        if (!response_contains(response, "CONNECT")) {
            show_status("ERROR: No CONNECT received!");
            goto error;
        }
        show_status("CONNECT received!");
    } else if (response_contains(response, "NO CARRIER") || 
               response_contains(response, "ERROR") ||
               response_contains(response, "BUSY")) {
        show_status("ERROR: Connection failed!");
        goto error;
    } else if (response[0] == 0) {
        /* No response at all - modem might not be responding */
        show_status("ERROR: No modem response!");
        debug_print("ERR ", "Check ACIA at $DE00");
        goto error;
    } else {
        /* Unknown response - show it and continue anyway */
        debug_print("WARN", "Unexpected response, continuing...");
    }

    /* Drain any remaining modem output thoroughly */
    debug_print("INFO", "Draining modem buffer...");
    {
        uint16_t timeout;
        uint8_t drain_count = 0;
        for (timeout = 0; timeout < 2000; timeout++) {
            if (serial_available()) {
                uint8_t b = serial_read();
                drain_count++;
                /* Show drained bytes for debugging */
                if (drain_count <= 10) {
                    gotoxy(0, debug_row);
                    textcolor(COLOR_GRAY1);
                    cputs("Drain: ");
                    debug_hex(b);
                    if (b >= 32 && b < 127) {
                        cputc('\''); cputc(b); cputc('\'');
                    }
                    debug_row++;
                    if (debug_row > 20) debug_row = 3;
                }
                timeout = 0;
            }
        }
        if (drain_count > 0) {
            gotoxy(0, debug_row);
            cputs("Drained ");
            {
                char buf[8];
                uint8_t idx = 0;
                uint8_t n = drain_count;
                if (n == 0) buf[idx++] = '0';
                else while (n > 0) { buf[idx++] = '0' + (n % 10); n /= 10; }
                buf[idx] = 0;
                /* Reverse */
                {
                    uint8_t i;
                    for (i = 0; i < idx / 2; i++) {
                        char t = buf[i]; buf[i] = buf[idx-1-i]; buf[idx-1-i] = t;
                    }
                }
                cputs(buf);
            }
            cputs(" bytes");
            debug_row++;
        }
    }
    
#endif /* !CONNECT_DIRECT */

    show_status("Connection established!");

    /* Longer delay to ensure connection is stable */
    {
        uint32_t i;
        for (i = 0; i < 50000UL; i++);
    }

    /* Send PING */
    show_status("Sending PING...");
    debug_print("PROTO", "Sending PING message");
    proto_send_ping();
    
    /* Small delay after sending */
    {
        uint16_t i;
        for (i = 0; i < 5000; i++);
    }

    /* Debug: show bytes being received */
    debug_print("PROTO", "Waiting for PONG (ACK)...");
    
    /* Wait for ACK with debug */
    {
        uint16_t frames = 0;
        uint8_t got_pong = 0;
        uint8_t rx_count = 0;
        
        while (frames < 600 && !got_pong) {  /* 10 second timeout */
            if (serial_available()) {
                uint8_t byte = serial_read();
                uint8_t msg_type;
                
                /* Show raw bytes received */
                gotoxy(0, debug_row);
                textcolor(COLOR_GRAY2);
                cputs("RX byte: ");
                debug_hex(byte);
                if (byte >= 32 && byte < 127) {
                    cputc('\'');
                    cputc(byte);
                    cputc('\'');
                }
                debug_row++;
                if (debug_row > 20) debug_row = 3;
                
                rx_count++;
                
                msg_type = proto_process_byte(&proto, byte);
                if (msg_type != 0) {
                    if (msg_type == MSG_ACK) {
                        got_pong = 1;
                        debug_print("PROTO", "Got ACK (PONG)!");
                    } else {
                        /* Show what message type we got */
                        gotoxy(0, debug_row);
                        textcolor(COLOR_LIGHTRED);
                        cputs("Got msg type: ");
                        debug_hex(msg_type);
                        if (msg_type == MSG_PING) cputs("(PING-echo!)");
                        else if (msg_type == MSG_NAK) cputs("(NAK)");
                        debug_row++;
                    }
                }
            }
            
            /* Small delay */
            {
                uint8_t i;
                for (i = 0; i < 100; i++);
            }
            frames++;
        }
        
        if (got_pong) {
            show_status("PONG! Server responded!");
        } else {
            gotoxy(0, debug_row);
            textcolor(COLOR_LIGHTRED);
            cputs("Timeout! RX bytes: ");
            /* Print rx_count */
            {
                char buf[8];
                uint8_t idx = 0;
                uint8_t n = rx_count;
                if (n == 0) {
                    buf[idx++] = '0';
                } else {
                    while (n > 0) {
                        buf[idx++] = '0' + (n % 10);
                        n /= 10;
                    }
                }
                buf[idx] = 0;
                /* Reverse */
                {
                    uint8_t i;
                    for (i = 0; i < idx / 2; i++) {
                        char t = buf[i];
                        buf[i] = buf[idx - 1 - i];
                        buf[idx - 1 - i] = t;
                    }
                }
                cputs(buf);
            }
            debug_row++;
            show_status("Timeout waiting for PONG");
            goto error;
        }
    }

    /* Small delay */
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }

    /* Create new conversation */
    show_status("Creating new conversation...");
    proto_send_new_conversation();

    if (wait_for_message(MSG_ACK, 300)) {
        show_status("Conversation created!");
    } else {
        show_status("Timeout creating conversation");
        goto error;
    }

    /* Small delay */
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }

    /* Send a test message */
    show_status("Sending test message...");
    proto_send_chat(TEST_MESSAGE);

    /* Wait for ACK */
    if (!wait_for_message(MSG_ACK, 300)) {
        show_status("Timeout waiting for ACK");
        goto error;
    }

    /* Wait for response chunks */
    show_status("Receiving response...");

    {
        uint8_t done = 0;
        uint16_t timeout = 0;
        uint16_t chunk_count = 0;
        uint8_t crc_fails = 0;
        uint8_t cx = 0, cy = 21;  /* chunk text area: rows 21-23 */

        while (!done && timeout < 1800) {  /* 30 second timeout */
            /* Drain EVERY buffered byte before pausing: at 9600 baud a
               byte arrives every ~1ms and one pass of the idle delay
               below costs more than that - delaying per byte loses data */
            while (serial_available() && !done) {
                uint8_t byte;
                uint8_t msg_type;
                if (proto_in_payload(&proto)) {
                    proto_fill_payload(&proto);  /* bulk path */
                    continue;
                }
                byte = serial_read();
                msg_type = proto_process_byte(&proto, byte);

                if (msg_type == MSG_STATUS) {
                    /* Status message (arrives as ASCII) */
                    uint8_t* payload = proto_get_payload(&proto);
                    ascii_to_petscii_str((char*)payload);
                    show_status((char*)payload);

                } else if (msg_type == MSG_CHAT_CHUNK) {
                    /* Chat chunk - skip sequence number byte */
                    uint8_t* payload = proto_get_payload(&proto);
                    ++chunk_count;
                    ascii_to_petscii_str((char*)(payload + 1));
                    gotoxy(cx, cy);
                    textcolor(COLOR_WHITE);
                    cputs((char*)(payload + 1));
                    cx = wherex();
                    cy = wherey();
                    if (cy > 23) cy = 21;  /* wrap within chunk area */

                } else if (msg_type == MSG_CHAT_DONE) {
                    /* Done! */
                    done = 1;
                    show_status("Response complete!");

                } else if (msg_type == MSG_CHAT_ERROR) {
                    /* Error */
                    uint8_t* payload = proto_get_payload(&proto);
                    ascii_to_petscii_str((char*)payload);
                    show_status((char*)payload);
                    done = 1;

                } else if (msg_type == PROTO_CRC_FAIL) {
                    ++crc_fails;
                }

                timeout = 0;  /* Reset timeout on any data */
            }

            /* Small delay */
            {
                uint8_t i;
                for (i = 0; i < 100; i++);
            }
            timeout++;
        }

        /* Show receive statistics in the debug area */
        gotoxy(0, debug_row);
        textcolor(COLOR_CYAN);
        cputs("Chunks: ");
        debug_hex((uint8_t)(chunk_count >> 8));
        debug_hex((uint8_t)chunk_count);
        cputs(" CRC fails: ");
        debug_hex(crc_fails);
        debug_row++;
        gotoxy(0, debug_row);
        cputs("Ring drops: ");
        debug_hex(serial_overflows());
        cputs(" HW overruns: ");
        debug_hex(serial_overruns());
        debug_row++;

        if (!done) {
            show_status("Timeout waiting for response");
        }
    }

    /* Success! */
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }
    show_status("Test complete! Press any key...");
    goto park;

error:
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }
    cputs("\n\nDone (diagnostic build).");

park:
    /* Park forever so the final screen stays up for the test harness
       (and for humans reading the diagnostics). Stray keystrokes from
       autostart must not blank the screen - reset to exit. */
    for (;;) {
        if (kbhit()) cgetc();
    }
    return 0;
}

#endif /* DEBUG_CLIENT */
