;
; LLM64 Client - soft-80 column bitmap renderer
;
; 320x200 hires bitmap = 80 columns of 4x8 glyphs (two per bitmap byte).
; VIC bank 3: bitmap at $E000 (RAM under KERNAL - CPU writes land in RAM,
; and the renderer never reads the bitmap back), matrix at $CC00 (per-cell
; color: glyph color in the high nibble over black). An ASCII shadow of
; the whole screen lives at $C000 (80x25) for the test harness.
;
; Cell format: ASCII 0x20-0x7E, bit 7 = reverse video.
;
; Only assembled into the build; only *used* when SOFT80 is defined.
;

        .export _soft80_init
        .export _soft80_row
        .export _soft80_span
.ifdef SCROLL_OPT
        .export _soft80_scroll_chat
.endif
        .import _font48, _font48lo
        .import popa, popax
        .importzp ptr1, ptr2, ptr3, ptr4, tmp1, tmp2, tmp3, tmp4

BITMAP  = $E000
MATRIX  = $CC00
SHADOW  = $C000

VIC_CTRL1 = $D011
VIC_MEMPTR = $D018
VIC_CTRL2 = $D016
CIA2_PA  = $DD00

CHAT_TOP  = 1           ; text rows of the scrollable chat area
CHAT_ROWS = 19

        .rodata
; per text row: bitmap, matrix, shadow base addresses
bmp_lo: .repeat 25, I
        .byte <(BITMAP + I * 320)
        .endrep
bmp_hi: .repeat 25, I
        .byte >(BITMAP + I * 320)
        .endrep
mat_lo: .repeat 25, I
        .byte <(MATRIX + I * 40)
        .endrep
mat_hi: .repeat 25, I
        .byte >(MATRIX + I * 40)
        .endrep
shd_lo: .repeat 25, I
        .byte <(SHADOW + I * 80)
        .endrep
shd_hi: .repeat 25, I
        .byte >(SHADOW + I * 80)
        .endrep

        .bss
row:     .res 1
colrev:  .res 1
pairs:   .res 1
revmask: .res 1
.ifdef SCROLL_OPT
bank01:  .res 1         ; $01 save across the banked-ROM copy
.endif

        .code

;---------------------------------------
; void soft80_init(void)
;---------------------------------------
_soft80_init:
        sei
        lda CIA2_PA
        and #%11111100          ; VIC bank 3 ($C000-$FFFF)
        sta CIA2_PA
        lda #$38                ; matrix $CC00, bitmap $E000
        sta VIC_MEMPTR
        lda VIC_CTRL1
        ora #$20                ; bitmap mode
        sta VIC_CTRL1
        lda VIC_CTRL2
        and #%11101111          ; hires, not multicolor
        sta VIC_CTRL2
        cli

        ; clear bitmap (8000 bytes of pure writes - ROM is above us but
        ; stores always reach the RAM the VIC sees)
        lda #<BITMAP
        sta ptr1
        lda #>BITMAP
        sta ptr1+1
        ldx #32                 ; 32 pages
        lda #0
        ldy #0
@clr:   sta (ptr1),y
        iny
        bne @clr
        inc ptr1+1
        dex
        bne @clr

        ; clear matrix (color 0 on 0) and shadow (spaces)
        ldy #0
@clr2:  lda #0
        sta MATRIX,y
        sta MATRIX+250,y
        sta MATRIX+500,y
        sta MATRIX+750,y
        lda #$20
        sta SHADOW,y
        sta SHADOW+250,y
        sta SHADOW+500,y
        sta SHADOW+750,y
        sta SHADOW+1000,y
        sta SHADOW+1250,y
        sta SHADOW+1500,y
        sta SHADOW+1750,y
        iny
        cpy #250
        bne @clr2
        rts

;---------------------------------------
; void __fastcall__ soft80_row(uint8_t r, const uint8_t* cells, uint8_t col)
; cells: 80 bytes, ASCII + bit7 reverse. col: color nibble 0-15.
;---------------------------------------
_soft80_row:
        sta colrev
        jsr popax
        sta ptr1                ; cells
        stx ptr1+1
        jsr popa
        sta row
        tax

        ; dst pointers for this row
        lda bmp_lo,x
        sta ptr4
        lda bmp_hi,x
        sta ptr4+1

        ; matrix: fill 40 cells with color<<4
        lda mat_lo,x
        sta ptr2
        lda mat_hi,x
        sta ptr2+1
        lda colrev
        asl
        asl
        asl
        asl
        ldy #39
@mat:   sta (ptr2),y
        dey
        bpl @mat

        ; shadow: 80 ASCII bytes (reverse bit stripped)
        lda shd_lo,x
        sta ptr2
        lda shd_hi,x
        sta ptr2+1
        ldy #79
@shd:   lda (ptr1),y
        and #$7F
        sta (ptr2),y
        dey
        bpl @shd

        ; render 40 pairs
        lda #40
        sta pairs
        jmp render_pairs

;---------------------------------------
; void __fastcall__ soft80_span(uint8_t row, const uint8_t* cells,
;                               uint8_t first_pair, uint8_t pair_count)
; Re-render only pairs [first_pair, first_pair+pair_count) of a row.
; cells points at the FULL row buffer; shadow is updated for the span;
; the matrix color is left untouched (same-color row assumed).
;---------------------------------------
_soft80_span:
        sta pairs               ; pair_count
        jsr popa
        sta tmp3                ; first_pair
        jsr popax
        sta ptr1                ; cells (full row)
        stx ptr1+1
        jsr popa
        sta row
        tax

        lda pairs
        beq @done

        ; cells += first_pair*2
        lda tmp3
        asl
        clc
        adc ptr1
        sta ptr1
        bcc :+
        inc ptr1+1
:
        ; shadow span update: 2*pair_count ASCII bytes at offset first*2
        lda shd_lo,x
        clc
        adc tmp3
        sta ptr2
        lda shd_hi,x
        adc #0
        sta ptr2+1
        lda tmp3                ; add first_pair again (total 2*first)
        clc
        adc ptr2
        sta ptr2
        bcc :+
        inc ptr2+1
:       lda pairs
        asl
        tay                     ; 2*count (max 80)
        dey
@shd2:  lda (ptr1),y
        and #$7F
        sta (ptr2),y
        dey
        cpy #$FF
        bne @shd2

        ; bitmap dst = rowbase + first_pair*8
        lda bmp_lo,x
        sta ptr4
        lda bmp_hi,x
        sta ptr4+1
        lda tmp3
        sta tmp1
        lda #0
        sta tmp2
        ldx #3
:       asl tmp1
        rol tmp2
        dex
        bne :-
        clc
        lda ptr4
        adc tmp1
        sta ptr4
        lda ptr4+1
        adc tmp2
        sta ptr4+1
        jmp render_pairs
@done:  rts

;---------------------------------------
; Core: render `pairs` pairs from (ptr1) cells to (ptr4) bitmap
;---------------------------------------
render_pairs:
@pair:
        ldy #0
        lda (ptr1),y            ; left cell
        iny
        ora (ptr1),y            ; both space, no reverse?
        cmp #$20
        beq @blank
        lda (ptr1),y            ; right cell (Y=1)
        jsr font_lo_ptr2        ; -> ptr3, revmask low nibble
        ldy #0
        lda (ptr1),y
        jsr font_hi_ptr         ; -> ptr2, revmask high nibble ORed

        ; blit 8 rows: byte = fontHI[l] | fontLO[r], then reverse mask
        jsr blit8
        jmp @next

@blank: ; both cells are plain spaces: 8 zero bytes
        lda #0
.repeat 8, I
        ldy #I
        sta (ptr4),y
.endrep

@next:
        ; advance: cells += 2, bitmap dst += 8
        clc
        lda ptr1
        adc #2
        sta ptr1
        bcc :+
        inc ptr1+1
:       clc
        lda ptr4
        adc #8
        sta ptr4
        bcc :+
        inc ptr4+1
:       dec pairs
        bne @pair
        rts

blit8:
.repeat 8, I
        ldy #I
        lda (ptr2),y
        ora (ptr3),y
        eor revmask
        sta (ptr4),y
.endrep
        rts

; A = cell -> ptr2 = _font48 + (cell-$20)*8; revmask |= $F0 if bit7
font_hi_ptr:
        tax
        and #$80
        beq :+
        lda revmask
        ora #$F0
        sta revmask
:       txa
        and #$7F
        sec
        sbc #$20
        bcs :+
        lda #0                  ; control chars render as space
:       sta tmp1
        lda #0
        sta tmp2
        asl tmp1
        rol tmp2
        asl tmp1
        rol tmp2
        asl tmp1
        rol tmp2                ; (c-32)*8, 16-bit
        clc
        lda tmp1
        adc #<_font48
        sta ptr2
        lda tmp2
        adc #>_font48
        sta ptr2+1
        rts

; A = cell -> ptr3 = _font48lo + (cell-$20)*8; INITIALIZES revmask
; (called first for each pair; low nibble set if bit7)
font_lo_ptr2:
        tax
        lda #0
        sta revmask
        txa
        and #$80
        beq :+
        lda #$0F
        sta revmask
:       txa
        and #$7F
        sec
        sbc #$20
        bcs :+
        lda #0
:       sta tmp1
        lda #0
        sta tmp2
        asl tmp1
        rol tmp2
        asl tmp1
        rol tmp2
        asl tmp1
        rol tmp2
        clc
        lda tmp1
        adc #<_font48lo
        sta ptr3
        lda tmp2
        adc #>_font48lo
        sta ptr3+1
        rts

;---------------------------------------
; Scroll-blit fast path - NOT ASSEMBLED unless SCROLL_OPT is
; defined, and nothing defines it. display.c gates its only call
; site the same way: the banked-ROM bitmap copy provoked a serial
; stall and phantom RX under real-time streaming, so full redraws
; are used instead. Kept for whenever that is understood, but
; assembling it spends scarce module-slot headroom on code that
; cannot run. Everything from here to EOF - mul320, mul40, mul80
; and copy_fwd included - is reachable only from this routine.
;---------------------------------------
.ifdef SCROLL_OPT

;---------------------------------------
; void __fastcall__ soft80_scroll_chat(uint8_t n)
; Scroll the chat area (text rows 1..19) up by n text rows: bitmap,
; matrix and shadow. Much cheaper than re-rendering 19 rows of glyphs.
;---------------------------------------
_soft80_scroll_chat:
        sta tmp4                ; n
        beq @done

        ; The bitmap lives UNDER the KERNAL ROM: writes always reach the
        ; RAM the VIC displays, but READS fetch ROM - so the copy below
        ; must bank the ROM out (HIRAM=0; IO stays mapped so the serial
        ; IRQ still works via the raw RAM vectors serial.s installed).
        lda $01
        sta bank01
        and #%11111101
        sta $01

        ; bitmap: move (CHAT_ROWS-n)*320 bytes from row (1+n) to row 1
        ldx tmp4
        lda bmp_lo+CHAT_TOP,x   ; src = row 1+n
        sta ptr1
        lda bmp_hi+CHAT_TOP,x
        sta ptr1+1
        lda bmp_lo+CHAT_TOP
        sta ptr2                ; dst = row 1
        lda bmp_hi+CHAT_TOP
        sta ptr2+1
        ; byte count = (19-n)*320
        lda #CHAT_ROWS
        sec
        sbc tmp4
        sta tmp1                ; rows to move
        jsr mul320_to_tmp23     ; tmp2/tmp3 = tmp1*320
        jsr copy_fwd

        lda bank01              ; KERNAL ROM back in
        sta $01

        ; matrix: (19-n)*40
        ldx tmp4
        lda mat_lo+CHAT_TOP,x
        sta ptr1
        lda mat_hi+CHAT_TOP,x
        sta ptr1+1
        lda mat_lo+CHAT_TOP
        sta ptr2
        lda mat_hi+CHAT_TOP
        sta ptr2+1
        lda #CHAT_ROWS
        sec
        sbc tmp4
        sta tmp1
        jsr mul40_to_tmp23
        jsr copy_fwd

        ; shadow: (19-n)*80
        ldx tmp4
        lda shd_lo+CHAT_TOP,x
        sta ptr1
        lda shd_hi+CHAT_TOP,x
        sta ptr1+1
        lda shd_lo+CHAT_TOP
        sta ptr2
        lda shd_hi+CHAT_TOP
        sta ptr2+1
        lda #CHAT_ROWS
        sec
        sbc tmp4
        sta tmp1
        jsr mul80_to_tmp23
        jmp copy_fwd
@done:  rts

; tmp1 rows -> tmp2(lo)/tmp3(hi) byte count
mul320_to_tmp23:
        lda #0
        sta tmp3
        lda tmp1                ; rows*320 = rows*5*64
        asl
        asl
        clc
        adc tmp1                ; *5 (max 95)
        sta tmp2
        lda #0
        sta tmp3
        ; now *64: shift left 6 (16-bit)
        ldx #6
:       asl tmp2
        rol tmp3
        dex
        bne :-
        rts

mul40_to_tmp23:
        lda #0
        sta tmp3
        lda tmp1
        asl
        asl
        clc
        adc tmp1                ; *5
        sta tmp2
        ldx #3                  ; *8
:       asl tmp2
        rol tmp3
        dex
        bne :-
        rts

mul80_to_tmp23:
        jsr mul40_to_tmp23
        asl tmp2
        rol tmp3
        rts

; forward copy of tmp3:tmp2 bytes from (ptr1) to (ptr2); src > dst
copy_fwd:
        ldy #0
        ldx tmp3                ; full pages
        beq @tail
@page:  lda (ptr1),y
        sta (ptr2),y
        iny
        bne @page
        inc ptr1+1
        inc ptr2+1
        dex
        bne @page
@tail:  cpy tmp2
        beq @done
        lda (ptr1),y
        sta (ptr2),y
        iny
        bne @tail
@done:  rts

.endif ; SCROLL_OPT
