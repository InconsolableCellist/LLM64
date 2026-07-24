/**
 * LLM64 Client - input editor (rows 21-23, 120 chars)
 */

#ifndef EDITOR_H
#define EDITOR_H

#include "common.h"

#define EDIT_MAX 960

void editor_init(void);
void editor_clear(void);

/* Handle an editing/typing key. Returns 1 if the key was consumed. */
uint8_t editor_key(uint8_t key);

/* Current text (PETSCII, null-terminated) and length */
char* editor_text(void);
uint16_t editor_len(void);

void editor_redraw(void);

#endif /* EDITOR_H */
