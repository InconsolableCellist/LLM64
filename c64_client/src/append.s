;
; LLM64 Client - chat_append_ascii fast path (assembly, SOFT80 only)
;
; Every byte of every streamed reply and every loaded conversation goes
; through this. The common case is a printable, non-space character with
; the pending word not yet full - three lines of C, but cc65 wrapped
; each iteration in a full call to chat_append_ascii_char plus a call to
; cell_from_ascii. This inlines only that case; everything else (space,
; CR, LF, the 0x01-0x1E inline-colour/bold markers, and the word-full
; hard wrap) still routes to chat_append_ascii_char, so the wrap/marker
; state machine lives in exactly one place.
;
; In SOFT80 cell_from_ascii is the identity on 0x20-0x7F (else '?'), so
; for a gated 0x21-0x7F the stored cell is just c | rev_on - no table.
;
; void __fastcall__ chat_append_ascii(const char* s);   ; s in A/X
;

.ifdef SOFT80

        .export _chat_append_ascii
        .import _chat_append_ascii_char
        .import _wbuf, _wlen, _rev_on, _view_scroll
        .importzp ptr1

TEXT_COLS = 80

        .bss
; Saved walk state across the slow-path C call (which clobbers the
; zero-page temporaries, ptr1 included). BSS, not zp, for that reason.
sv_lo:  .res 1
sv_hi:  .res 1
sv_y:   .res 1

        .code

_chat_append_ascii:
        sta ptr1
        stx ptr1+1
        ldy #0
@loop:
        lda (ptr1),y
        beq @done               ; *s == 0
        ; fast path: 0x21 <= c <= 0x7F && wlen < TEXT_COLS-1
        cmp #$21
        bcc @slow               ; c < 0x21: NUL is handled, rest are
                                ;   markers / space / CR / LF -> C
        cmp #$80
        bcs @slow               ; c >= 0x80: let C map it to '?'
        ldx _wlen
        cpx #TEXT_COLS-1
        bcs @slow               ; word full (wlen 79): C stores + wraps
        ora _rev_on             ; c | rev_on
        sta _wbuf,x
        inc _wlen
        jmp @adv
@slow:
        lda ptr1                ; preserve the walk across the C call
        sta sv_lo
        lda ptr1+1
        sta sv_hi
        sty sv_y
        lda (ptr1),y            ; reload c (clobbered by the saves)
        ldx #0                  ; fastcall: char in A, X=0
        jsr _chat_append_ascii_char
        lda sv_lo
        sta ptr1
        lda sv_hi
        sta ptr1+1
        ldy sv_y
@adv:
        iny
        bne @loop
        inc ptr1+1              ; strings can exceed 255 bytes
        jmp @loop
@done:
        lda #0
        sta _view_scroll        ; chat_append_ascii's trailing reset
        rts

.endif ; SOFT80
