/**
 * C64 LLM Client - overlay module loader (see loader.h)
 */

#ifdef SOFT80

#include <cbm.h>
#include "loader.h"
#include "cfg.h"

/* Which module is sitting in the slot right now ("c64llm.N" -> 'N'),
   0 = unknown/nothing. Tracked here because module_load is the ONLY
   writer of $9C00-$A9FF: the sole references to __OVERLAYSTART__ are
   this file, modslot.s and the linker config, so slot contents survive
   untouched between opens. */
static uint8_t slot_owner;

uint8_t module_in_slot(const char* name) {
    return slot_owner && slot_owner == (uint8_t)name[7];
}

uint8_t module_load(const char* name) {
    /* NULL: honor the file's own 2-byte header, which the linker set
       to __OVERLAYSTART__ (OVL1ADDR segment in modslot.s). Modules
       live on whatever drive we booted from. */
    uint8_t ok = cbm_load(name, boot_device, (void*)0) != 0;
    /* A failed load may have written part of the slot before giving
       up, so only a success names an owner. */
    slot_owner = ok ? (uint8_t)name[7] : 0;
    return ok;
}

#endif /* SOFT80 */
