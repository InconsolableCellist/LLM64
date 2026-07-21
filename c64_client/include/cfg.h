/**
 * C64 LLM Client - persistent network config (device 8)
 *
 * "c64llm.cfg" on the boot drive stores the proxy address, so the
 * binary no longer bakes it in. Written by the config editor module;
 * read at boot before dialing. Fixed-size binary blob - no parsing.
 */

#ifndef CFG_H
#define CFG_H

#include "common.h"

#define CFG_HOST_MAX 32
#define CFG_PORT_MAX 6

/* Live settings (PETSCII, NUL-terminated). Prefilled with the baked
   defaults; config_load() overwrites them from disk. */
extern char g_host[CFG_HOST_MAX];
extern char g_port[CFG_PORT_MAX];

/* The drive we booted from ($BA snapshot taken at startup, clamped
   to 8 if implausible): modules and config load from here, so the
   client works from any device number - FPGA drive on 9, real
   floppy on 8, whatever. */
extern uint8_t boot_device;
void boot_device_init(void);
/* "ATDT<host>:<port>" - rebuilt by build_dial_string() */
extern char g_dial[CFG_HOST_MAX + CFG_PORT_MAX + 6];

/* Read c64llm.cfg from device 8 into g_host/g_port.
   Returns nonzero if a valid config was found. */
uint8_t config_load(void);

/* Write g_host/g_port to c64llm.cfg (scratch-and-replace).
   Returns nonzero on success. */
uint8_t config_save(void);

void build_dial_string(void);

#endif /* CFG_H */
