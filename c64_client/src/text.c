/**
 * C64 LLM Client - PETSCII/ASCII conversion
 */

#include "text.h"

/* PETSCII uppercase letters live at 0xC1-0xDA, lowercase at 0x41-0x5A.
   ASCII digits and most punctuation are identical in both encodings. */

uint8_t petscii_to_ascii(uint8_t c) {
    if (c >= 0xC1 && c <= 0xDA) return c - 0x80;  /* A-Z */
    if (c >= 0x41 && c <= 0x5A) return c + 0x20;  /* a-z */
    if (c == 0x0D) return 0x0D;
    return c;
}

/* NOTE: hex constants only - cc65 translates character literals like 'A'
   to PETSCII in the compiled code, which would corrupt these range checks */
uint8_t ascii_to_petscii(uint8_t c) {
    if (c >= 0x41 && c <= 0x5A) return c + 0x80;  /* ASCII A-Z -> PETSCII */
    if (c >= 0x61 && c <= 0x7A) return c - 0x20;  /* ASCII a-z -> PETSCII */
    return c;
}

/* Screen-code conversion is 40-column-only: the soft-80 renderer works
   in ASCII cells end to end (~190 bytes reclaimed in that build) */
#ifndef SOFT80
uint8_t ascii_to_screen(uint8_t c) {
    if (c >= 0x61 && c <= 0x7A) return c - 0x60;  /* a-z -> 1-26 */
    if (c >= 0x41 && c <= 0x5A) return c;         /* A-Z -> 65-90 */
    if (c >= 0x20 && c <= 0x3F) return c;         /* space..? unchanged */
    if (c == 0x40) return 0x00;                   /* @ */
    if (c == 0x5B) return 0x1B;                   /* [ */
    if (c == 0x5D) return 0x1D;                   /* ] */
    if (c == 0x5F) return 0x64;                   /* _ (underline glyph) */
    if (c == 0x09) return 0x20;                   /* tab -> space */
    return 0x3F;                                  /* anything else -> ? */
}

uint8_t petscii_to_screen(uint8_t c) {
    return ascii_to_screen(petscii_to_ascii(c));
}

void petscii_to_ascii_str(char* s) {
    while (*s) {
        *s = petscii_to_ascii((uint8_t)*s);
        ++s;
    }
}
#endif

void ascii_to_petscii_str(char* s) {
    while (*s) {
        *s = ascii_to_petscii((uint8_t)*s);
        ++s;
    }
}
