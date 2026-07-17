/**
 * C64 LLM Client - PETSCII/ASCII conversion
 *
 * cc65 encodes C string literals as PETSCII on the c64 target, and conio
 * expects PETSCII. The wire protocol and the LLM API are ASCII. These
 * helpers convert at the serial boundary: outgoing text PETSCII->ASCII,
 * incoming text ASCII->PETSCII.
 */

#ifndef TEXT_H
#define TEXT_H

#include "common.h"

uint8_t petscii_to_ascii(uint8_t c);
uint8_t ascii_to_petscii(uint8_t c);

/* In-place conversion of null-terminated strings */
void petscii_to_ascii_str(char* s);
void ascii_to_petscii_str(char* s);

#endif /* TEXT_H */
