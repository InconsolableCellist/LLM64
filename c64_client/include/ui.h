/**
 * C64 LLM Client - TUI display (chat area, status bar, title)
 *
 * All rendering is done with direct screen/color RAM writes; the chat
 * scrollback stores pre-wrapped 40-column lines as screen codes.
 */

#ifndef UI_H
#define UI_H

#include "common.h"

void ui_init(void);

/* Nonzero = all draw primitives are no-ops (fullscreen image showing) */
extern uint8_t ui_frozen;

/* Persistent status-row indicators (bit0: unviewed pic suggestion) */
void ui_set_hints(uint8_t flags);

/* Full redraw of the static frame (title, separator) + chat + status */
void ui_redraw_all(void);

/* Status bar (row 24). Takes a PETSCII string (i.e. a C literal). */
void ui_status(const char* msg);

/* Chat area ------------------------------------------------------- */

/* Begin a new message; sets color and optional prefix by role */
void chat_start(uint8_t role);

/* Append incoming ASCII text to the current message (word-wrapped) */
void chat_append_ascii(const char* s);
void chat_append_ascii_char(uint8_t c);

/* Append a PETSCII string (C literal or editor text) */
void chat_append_petscii(const char* s);

/* End the current message (flush pending word, blank separator line) */
void chat_finish(void);

/* Suppress rendering during bulk loads; unfreezing draws the final view */
void chat_freeze(uint8_t on);

/* Clear all scrollback */
void chat_clear(void);

/* Scroll view: positive = toward older lines. Resets on new content. */
void chat_scroll(int8_t lines_up);

void chat_redraw(void);

/* Cheap redraw for use per streamed chunk: repaints only the in-progress
   line unless a full line was committed since the last redraw. */
void chat_redraw_stream(void);

/* Modal overlays (conversation list, help) draw over the chat area;
   call chat_redraw() to restore. Helper to blank the area: */
void chat_area_clear_screen(void);

/* Draw one line of text (PETSCII) at a chat-area row, optional reverse */
void ui_draw_row(uint8_t row, const char* petscii, uint8_t color, uint8_t reverse);

/* Low-level: blit one row of cells (screen codes in 40-col, ASCII in
   SOFT80; bit7 = reverse in both). cells must be TEXT_COLS long. */
void ui_blit_row(uint8_t row, const uint8_t* cells, uint8_t color);

/* Repaint only cells [first, first+count) of a row (same color) */
void ui_blit_span(uint8_t row, const uint8_t* cells, uint8_t first,
                  uint8_t count);

/* Convert one PETSCII char to a display cell for the current mode */
uint8_t ui_cell_from_petscii(uint8_t c);

#endif /* UI_H */
