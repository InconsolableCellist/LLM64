;
; LLM64 Client - inline-color matrix builder (assembly, SOFT80 only)
;
; colorize_row scans one 80-cell chat row for inline color markers,
; rewrites each marker cell to a space in place, and fills a 40-entry
; color matrix (one entry per 8x8 cell = two soft-80 glyphs). It runs
; on every colored chat redraw, up to 19 rows at a time, so it was a
; visible cost in C (80-cell scan + 40-byte fill per row). The logic is
; a flat per-cell decision with no call overhead - a natural fit for asm.
;
; uint8_t __fastcall__ colorize_row(uint8_t* buf, uint8_t carry, uint8_t base)
;   fastcall: base (rightmost) in A; then the C stack holds carry then
;   buf -> jsr popa (carry), jsr popax (buf), in that order. Returns
;   `any` (nonzero if a marker was seen or a run carried in) in A, X=0.
;
; Bit-exact port of the C in display.c:
;   run = carry; any = carry ? 1 : 0;
;   for i in 0..39: matbuf[i] = base
;   for i in 0..79:
;       c = buf[i] & 0x7F           ; bit7 is reverse, not color
;       if c == MK_CLOSE:           run=0;        buf[i]=' '; any=1
;       elif MK_COLOR_LO<=c<=MK_COLOR_HI: run=c&0x0F; buf[i]=' '; any=1
;       elif run:                   matbuf[i>>1] = run
;

.ifdef SOFT80

        .export _colorize_row
        .import popa, popax
        .import _matbuf
        .importzp ptr1

; Marker constants (must match display.c)
MK_CLOSE    = $01
MK_COLOR_LO = $11
MK_COLOR_HI = $1E

        .bss
cr_base:    .res 1
cr_run:     .res 1
cr_any:     .res 1

        .code

_colorize_row:
        sta cr_base             ; base (fastcall register arg)
        jsr popa
        sta cr_run              ; run = carry
        jsr popax
        sta ptr1                ; buf
        stx ptr1+1

        ; matbuf[0..39] = base
        ldx #39
        lda cr_base
@fill:  sta _matbuf,x
        dex
        bpl @fill

        ; any = (carry != 0) ? 1 : 0
        lda cr_run
        beq @any0
        lda #1
@any0:  sta cr_any

        ldy #0
@loop:
        lda (ptr1),y
        and #$7F                ; strip reverse bit -> c
        cmp #MK_CLOSE
        bne @not_close
        ; close marker: end the run
        lda #0
        sta cr_run
        jsr @space
        jmp @mark
@not_close:
        cmp #MK_COLOR_LO
        bcc @run_cell           ; c < $11: not a marker
        cmp #MK_COLOR_HI+1
        bcs @run_cell           ; c > $1E: not a marker
        ; color marker: start a run of color (c & 0x0F)
        and #$0F
        sta cr_run
        jsr @space
@mark:  lda #1
        sta cr_any
        jmp @next
@run_cell:
        lda cr_run
        beq @next               ; no active run: leave matbuf at base
        tya                     ; matbuf[i>>1] = run
        lsr a
        tax
        lda cr_run
        sta _matbuf,x
@next:
        iny
        cpy #80
        bne @loop

        lda cr_any
        ldx #0
        rts

; buf[Y] = ' ' (a plain space, clearing any reverse bit - matches the C
; unconditional store). Preserves Y; A is dead across the call sites.
@space:
        lda #$20
        sta (ptr1),y
        rts

.endif ; SOFT80
