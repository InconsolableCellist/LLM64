;
; Crash post-mortem block layout (see src/diag.s).
;
; $02A7-$02B6: 16 bytes of page-2 scratch that BASIC and the KERNAL both
; leave alone, so the block outlives a crash to BASIC and costs zero
; bytes of the linked image. include/diag.h mirrors these offsets for
; the C side and docs/crash-postmortem.md for the PEEK readout - all
; three must move together.
;

DBLK            = $02A7

D_MAGIC         = DBLK+0        ; $C6 once diag_init has run
D_IDX           = DBLK+1        ; next slot in the breadcrumb ring
D_CRUMBN        = DBLK+2        ; breadcrumbs pushed (wraps at 256)
D_MUSIC         = DBLK+3        ; music_state at the last breadcrumb
D_KEY           = DBLK+4        ; last key handle_key dispatched
D_HWLOW         = DBLK+5        ; lowest hardware SP seen in the IRQ
D_MODN          = DBLK+6        ; overlay modules loaded since boot
D_MODLAST       = DBLK+7        ; suffix of the last module loaded
D_TRAIL         = DBLK+8        ; 8-deep breadcrumb ring

DIAG_LEN        = 16
DIAG_MAGIC      = $C6
DIAG_TRAIL_MASK = 7

; C-stack canary. Derived from the linker's own symbols so a change to
; the stack size or HIMEM in c64-soft80.cfg carries over automatically.
.ifdef SOFT80
        .import __HIMEM__, __STACKSIZE__
CANARY          = $A5
CAN_START       = __HIMEM__ - __STACKSIZE__     ; $AA00: stack bottom
CAN_END         = CAN_START + $0400             ; cover the deepest 1K
.endif
