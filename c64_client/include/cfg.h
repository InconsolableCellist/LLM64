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

/* Wire-speed setting, stored in the cfg and pushed to the ACIA divisor.
   Index into an internal control-byte table:
     0 = 4800  nominal / 9600  on real HW ($1C)  slow-link fallback
     1 = 9600  nominal / 19200 on real HW ($1E)  default
     2 = 19200 nominal / 38400 on real HW ($1F)  BAUD38400 builds only
   Show the hardware rate in the UI; VICE runs the nominal rate. */
#define BAUD_IDX_9600   0
#define BAUD_IDX_19200  1
#define BAUD_IDX_38400  2
#ifdef BAUD38400
#define BAUD_IDX_DEFAULT BAUD_IDX_38400
#else
#define BAUD_IDX_DEFAULT BAUD_IDX_19200
#endif
/* 38400 is field-proven on the C64U in NMI mode, so it is ALWAYS offered
   in the F1->E Speed cycle regardless of build; BAUD38400 now only picks
   the boot DEFAULT. A distribution disk can therefore ship the safe
   19200 default (build without BAUD38400) and still let a user opt up to
   38400 - so every rate is reachable in every build. */
#define BAUD_IDX_MAX     BAUD_IDX_38400

extern uint8_t g_baud_idx;

/* Nominal baud / 100 for the current index (48 / 96 / 192) - the
   MSG_SET_BAUD payload the proxy paces from. */
uint16_t baud_nominal_div100(void);

/* Copy g_baud_idx's control byte into the ACIA driver's acia_ctrl, so
   the next acia_init_hw brings the link up at that rate. */
void baud_apply(void);

#endif /* CFG_H */
