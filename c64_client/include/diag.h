/**
 * LLM64 Client - crash post-mortem block (see src/diag.s)
 *
 * A breadcrumb ring plus a C-stack canary, both kept at $02A7 in page-2
 * RAM that survives a crash to BASIC. After the machine drops to READY,
 * docs/crash-postmortem.md reads the whole story back out with PEEK.
 *
 * Breadcrumb codes are grouped by subsystem so a trail is legible in
 * decimal at the BASIC prompt without a lookup table: 16s are keyboard,
 * 32s the image overlay, 48s overlay-module loads, 64s music, 80s
 * incoming media transfers.
 */

#ifndef DIAG_H
#define DIAG_H

#include "common.h"

#define DIAG_BASE       0x02A7
#define DIAG_MAGIC_VAL  0xC6

/* Keep in sync with include/diag.inc */
#define D_MAGIC   (*(volatile uint8_t*)(DIAG_BASE + 0))
#define D_IDX     (*(volatile uint8_t*)(DIAG_BASE + 1))
#define D_CRUMBN  (*(volatile uint8_t*)(DIAG_BASE + 2))
#define D_MUSIC   (*(volatile uint8_t*)(DIAG_BASE + 3))
#define D_KEY     (*(volatile uint8_t*)(DIAG_BASE + 4))
#define D_HWLOW   (*(volatile uint8_t*)(DIAG_BASE + 5))
#define D_MODN    (*(volatile uint8_t*)(DIAG_BASE + 6))
#define D_MODLAST (*(volatile uint8_t*)(DIAG_BASE + 7))
#define D_TRAIL   ((volatile uint8_t*)(DIAG_BASE + 8))
/* Deepest C stack seen, sampled in the IRQ. The canary at $AA00 cannot
   be read back after a crash - it sits under BASIC ROM - so the value
   is kept here in page 2, which PEEK can always see. */
#define D_SPLO    (*(volatile uint8_t*)(DIAG_BASE + 16))
#define D_SPHI    (*(volatile uint8_t*)(DIAG_BASE + 17))

/* Breadcrumb codes. Only rare, dangerous events get one - keystrokes
   deliberately do not, or the ring would hold nothing but typing. Each
   risky region is bracketed (enter/leave) so a trail that stops on the
   entry code pins the crash inside that region. */
#define DC_BOOT        1

#define DC_IMGSHOW    32   /* fullscreen image put up */
#define DC_IMGCLOSE   33   /* img_close entered (dismiss + full repaint) */
#define DC_IMGDONE    34   /* img_close returned */

#define DC_MODLOAD    48   /* mod_open: music held, RX masked, about to LOAD */
#define DC_MODLOADED  49   /* LOAD returned, RX + music resumed, about to run */
#define DC_MODDONE    50   /* module run() returned */

#define DC_MUSICBEG   64   /* music_ext_begin: tune init called */

#define DC_SIDRECV    80   /* SID transfer starting */

/* Opt-in: `make MODE80=1 DIAG=1`. Without it every call below compiles
   to nothing, so the shipping build keeps the whole module-slot budget
   (see the note at the top of src/diag.s). */
#ifdef DIAG
void diag_init(void);
void diag_crumb(uint8_t code);
void diag_note_key(uint8_t k);
void diag_note_mod(uint8_t n);
#else
#define diag_init()      ((void)0)
#define diag_crumb(c)    ((void)0)
#define diag_note_key(k) ((void)0)
#define diag_note_mod(n) ((void)0)
#endif

#endif /* DIAG_H */
