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
; Deepest cc65 C stack ever seen, sampled in the IRQ. This exists
; because the canary it duplicates CANNOT be read after a crash: the
; stack lives under BASIC ROM, so PEEK at the READY prompt returns ROM
; bytes, not the RAM the client used. Kept here in page 2, which is
; always RAM and therefore always PEEK-able. Appended after the ring so
; the original 16-byte readout keeps working unchanged.
D_SPLO          = DBLK+16
D_SPHI          = DBLK+17

DIAG_LEN        = 18
DIAG_MAGIC      = $C6
DIAG_TRAIL_MASK = 7

; C-stack canary. Derived from the linker's own symbols so a change to
; the stack size or HIMEM in c64-soft80.cfg carries over automatically.
.ifdef SOFT80
        .import __HIMEM__, __STACKSIZE__
CANARY          = $A5
CAN_START       = __HIMEM__ - __STACKSIZE__     ; $AC00: stack bottom
; Cover the deepest 768 B of the 1K stack. Must stay well short of
; __HIMEM__: diag_init's own callers have live frames at the top, and
; the fill would scribble them (it did cover 1K when the stack was
; 1.5K). Mirrored in emu/test_e2e.py CAN_END.
CAN_END         = CAN_START + $0300
.endif
