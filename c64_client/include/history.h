/**
 * LLM64 Client - recall of the last sent message (SOFT80 only)
 *
 * Up-arrow on an empty line brings back what you sent. Stored in the
 * unused RAM between the soft-80 shadow and the color matrix.
 */

#ifndef HISTORY_H
#define HISTORY_H

#include "common.h"

void hist_init(void);
void hist_add(const char* text, uint16_t n);
const char* hist_prev(uint16_t* n);   /* 0 = nothing sent yet */
uint8_t hist_walking(void);           /* a recall is on the line */
void hist_reset(void);                /* typing abandons it */

#endif /* HISTORY_H */
