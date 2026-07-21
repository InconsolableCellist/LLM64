/**
 * C64 LLM Client - overlay module loader (see loader.h)
 */

#ifdef SOFT80

#include <cbm.h>
#include "loader.h"
#include "cfg.h"

uint8_t module_load(const char* name) {
    /* NULL: honor the file's own 2-byte header, which the linker set
       to __OVERLAYSTART__ (OVL1ADDR segment in modslot.s). Modules
       live on whatever drive we booted from. */
    return cbm_load(name, boot_device, (void*)0) != 0;
}

#endif /* SOFT80 */
