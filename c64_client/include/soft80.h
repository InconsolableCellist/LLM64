/**
 * LLM64 Client - soft-80 bitmap renderer (see soft80.s)
 */

#ifndef SOFT80_H
#define SOFT80_H

#include "common.h"

void soft80_init(void);

/* Render one 80-cell text row. cells: ASCII, bit7 = reverse video. */
void __fastcall__ soft80_row(uint8_t row, const uint8_t* cells,
                             uint8_t color);

/* Re-render only pairs [first_pair, first+count) of a row (cells = full
   row buffer). Row color must be unchanged. */
void __fastcall__ soft80_span(uint8_t row, const uint8_t* cells,
                              uint8_t first_pair, uint8_t pair_count);

/* Scroll the chat area (rows 1-19) up n text rows without re-rendering */
void __fastcall__ soft80_scroll_chat(uint8_t n);

#endif /* SOFT80_H */
