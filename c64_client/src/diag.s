;
; C64 LLM Client - crash post-mortem block
;
; The bug this exists for (adventure mode, streamed tune playing, an
; illustration recently dismissed, machine drops to READY while typing)
; destroys the only witness it has: the screen. So the evidence is kept
; somewhere the crash cannot reach.
;
; Everything below lives at $02A7 - the 89 spare bytes of page 2 that
; neither BASIC nor the KERNAL touch. Two properties earn that address:
;
;   * It is OUTSIDE the linked image, so the block costs nothing against
;     the module-slot headroom (only the code writing it does - and the
;     budget there is ~307 bytes, see c64-soft80.cfg).
;   * It SURVIVES a crash to BASIC. Once the machine is sitting at READY
;     the trail still names the last paths that ran, and PEEK reads it
;     straight out - see docs/crash-postmortem.md.
;
; The C-stack canary is the other half. The stack ($AA00-$AFFF in the
; soft-80 build) is already reserved, so pattern-filling it is free: the
; lowest byte still holding the pattern is the deepest the C stack ever
; got. If that reaches $AA00 the stack has run into the overlay slot
; below it, which is the leading theory for the crash.
;

; Built only for DIAG=1. The block costs no RAM, but the calls that feed
; it cost ~240 bytes of CODE - and CODE growth eats the module-slot
; headroom 1:1 (rule 9 in HANDOFF.md), which is ~307 bytes total. So the
; instrumentation is opt-in: `make MODE80=1 DIAG=1` for a bug hunt, and
; the shipping build keeps its budget.

.ifdef DIAG

        .export _diag_init
        .export _diag_crumb
        .export _diag_note_key
        .export _diag_note_mod

        .import _music_state

        .include "diag.inc"

;---------------------------------------
; void diag_init(void)
; Stamp the magic, clear the block, lay the canary. Called once from
; main() before anything deep runs - the fill would otherwise scribble
; the caller's own stack frame.
;---------------------------------------
_diag_init:
        lda #0
        ldx #DIAG_LEN-1
@clr:   sta DBLK,x
        dex
        bpl @clr

        lda #DIAG_MAGIC
        sta D_MAGIC
        lda #$FF                ; low-water marks start high
        sta D_HWLOW
        sta D_SPLO
        sta D_SPHI

.ifdef SOFT80
        ; Canary over the bottom 1K of the C stack. The stack grows DOWN
        ; from __HIMEM__, so filling [CAN_START, CAN_END) leaves the live
        ; frames near the top alone while covering every byte the deepest
        ; call chains could reach.
        lda #<CAN_START
        sta @sm+1
        lda #>CAN_START
        sta @sm+2
        ldx #0
        lda #CANARY
@sm:    sta CAN_START,x
        inx
        bne @sm
        inc @sm+2
        ldy @sm+2
        cpy #>CAN_END
        bne @sm
.endif
        rts

;---------------------------------------
; void diag_crumb(uint8_t code)   (fastcall: code in A)
; Push one breadcrumb into an 8-deep ring, and snapshot what the crash
; report needs alongside it. A ring rather than a shifted trail: same
; history, a third of the code, and the write is O(1).
;---------------------------------------
_diag_crumb:
        ldx D_IDX
        sta D_TRAIL,x
        inx
        txa
        and #DIAG_TRAIL_MASK
        sta D_IDX
        inc D_CRUMBN            ; wraps; nonzero proves the block is live
        lda _music_state
        sta D_MUSIC
        rts

;---------------------------------------
; void diag_note_key(uint8_t k)   - last key handle_key saw
; void diag_note_mod(uint8_t n)   - last overlay module loaded, + count
;---------------------------------------
_diag_note_key:
        sta D_KEY
        rts

_diag_note_mod:
        sta D_MODLAST
        inc D_MODN
        rts

; No routine reads the canary back: the two consumers that matter can
; both scan it themselves. BASIC walks it with a FOR loop at the READY
; prompt (docs/crash-postmortem.md) and the VICE harness reads the range
; straight out of memory. A resident scanner would just cost headroom.

.endif ; DIAG
