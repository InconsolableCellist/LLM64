/**
 * C64 LLM Client - Main Entry Point
 */

#include <c64.h>
#include <conio.h>
#include <stdio.h>
#include <string.h>
#include "common.h"
#include "serial.h"
#include "protocol.h"

/* Global protocol context */
ProtoContext proto;
uint8_t payload_buffer[MAX_PAYLOAD];

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

/* Clear screen and setup */
void init_screen(void) {
    clrscr();
    bordercolor(COLOR_BLUE);
    bgcolor(COLOR_CYAN);  /* Light blue */
    textcolor(COLOR_BLUE);

    /* Title */
    gotoxy(0, 0);
    cputs("C64 LLM CLIENT v0.1");

    /* Status line */
    show_status("Initializing...");
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

    /* Initialize screen */
    init_screen();

    /* Initialize protocol */
    proto_init(&proto, payload_buffer, MAX_PAYLOAD);

    /* Show status */
    show_status("Connecting to server...");

    /* Initialize ACIA and connect */
    result = serial_init("raspberrypi.local", 6400);

    if (result != 0) {
        show_status("ERROR: Failed to initialize ACIA!");
        goto error;
    }

    show_status("Connection established!");

    /* Small delay */
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }

    /* Send PING */
    show_status("Sending PING...");
    proto_send_ping();

    /* Wait for ACK */
    if (wait_for_message(MSG_ACK, 300)) {  /* 5 second timeout */
        show_status("PONG! Server responded!");
    } else {
        show_status("Timeout waiting for PONG");
        goto error;
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
    proto_send_chat("Hello from C64!");

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
        uint8_t row = 5;

        gotoxy(0, row);

        while (!done && timeout < 1800) {  /* 30 second timeout */
            if (serial_available()) {
                uint8_t byte = serial_read();
                uint8_t msg_type = proto_process_byte(&proto, byte);

                if (msg_type == MSG_STATUS) {
                    /* Status message */
                    uint8_t* payload = proto_get_payload(&proto);
                    show_status((char*)payload);

                } else if (msg_type == MSG_CHAT_CHUNK) {
                    /* Chat chunk */
                    uint8_t* payload = proto_get_payload(&proto);
                    /* Skip sequence number byte */
                    cputs((char*)(payload + 1));

                } else if (msg_type == MSG_CHAT_DONE) {
                    /* Done! */
                    done = 1;
                    show_status("Response complete!");

                } else if (msg_type == MSG_CHAT_ERROR) {
                    /* Error */
                    uint8_t* payload = proto_get_payload(&proto);
                    show_status((char*)payload);
                    done = 1;
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
    cgetc();
    goto cleanup;

error:
    {
        uint16_t i;
        for (i = 0; i < 30000; i++);
    }
    cputs("\n\nPress any key to exit...");
    cgetc();

cleanup:
    serial_disconnect();
    clrscr();
    return 0;
}
