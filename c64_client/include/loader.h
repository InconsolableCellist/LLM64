/**
 * LLM64 Client - overlay module loader
 *
 * Modules are cc65 overlays (segment OVERLAY1) linked with the resident
 * client and emitted to a separate file that lives on device 8 next to
 * the PRG. module_load() pulls one into the fixed slot below the C
 * stack (__OVERLAYSTART__, see c64-soft80.cfg); the caller then just
 * calls the module's entry symbol (resolved at link time).
 *
 * Speed relies on JiffyDOS or a user-supplied fastloader - a stock
 * KERNAL LOAD works but crawls.
 */

#ifndef LOADER_H
#define LOADER_H

#include "common.h"

/* Load the named overlay file from device 8 into the module slot.
   Returns nonzero on success. */
uint8_t module_load(const char* name);

/* Nonzero if the named module is ALREADY in the slot from an earlier
   load, i.e. it can be re-entered without touching the disk. Nothing
   else writes the slot, so this stays true until the next
   module_load() of a different module. */
uint8_t module_in_slot(const char* name);

#endif /* LOADER_H */
