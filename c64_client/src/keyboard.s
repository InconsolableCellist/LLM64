;
; LLM64 Client - custom keyboard matrix scanner
;
; Replaces the KERNAL's SCNKEY: scans the full 8x8 matrix each tick,
; detects EVERY newly pressed key in the frame (the KERNAL registers only
; one), decodes via the KERNAL ROM tables, and pushes into the KERNAL
; keyboard buffer so conio's kbhit/cgetc keep working unchanged.
;
; Rollover: as close to n-key as the hardware allows. The matrix has no
; diodes, so three held keys on the corners of a rectangle ghost a phantom
; fourth; a new key that is rectangle-ambiguous with held keys is dropped
; instead of guessed.
;
; Called from the serial driver's IRQ handler on CIA1 timer ticks (60Hz).
; Uses no cc65 zeropage temps (IRQ context).
;

        .export _kb_scan
        .export joy_mask
        .export _sys_ticks      ; free-running 60Hz counter (KERNAL jiffy
                                ; clock is dead since we own the IRQ)

CIA1_PA   = $DC00
CIA1_PB   = $DC01

KBUF      = $0277       ; KERNAL keyboard buffer
NDX       = $C6         ; chars in buffer
KBUF_MAX  = 10

; KERNAL ROM decode tables (stock KERNAL, matrix code -> PETSCII)
TAB_NORM  = $EB81
TAB_SHIFT = $EBC2
TAB_CBM   = $EC03
TAB_CTRL  = $EC78

; Matrix codes of the modifier keys (col*8 + row)
CODE_LSHIFT = 15        ; col 1 row 7
CODE_RSHIFT = 52        ; col 6 row 4
CODE_CTRL   = 58        ; col 7 row 2
CODE_CBM    = 61        ; col 7 row 5

MOD_SHIFT = 1
MOD_CBM   = 2
MOD_CTRL  = 4

REPEAT_DELAY = 24       ; frames before auto-repeat (~0.4s)
REPEAT_NEXT  = 25       ; counter reload => repeat every ~3 frames

        .bss
state:    .res 8        ; current matrix state, 1 = pressed
prev:     .res 8
newk:     .res 8        ; newly pressed this frame
mods:     .res 1
code:     .res 1
gcol:     .res 1        ; scratch: column of key being handled
growbit:  .res 1        ; scratch: row bit of key being handled
_sys_ticks: .res 2      ; free-running 60Hz counter
joy_mask: .res 1        ; rows pulled low by a control-port device
not_joy:  .res 1        ; its complement
rpt_code: .res 1        ; matrix code of the repeat candidate
rpt_char: .res 1
rpt_cnt:  .res 1
rpt_ok:   .res 1        ; decoded char is repeatable

        .rodata
colmask:  .byte $FE,$FD,$FB,$F7,$EF,$DF,$BF,$7F
bitmask:  .byte $01,$02,$04,$08,$10,$20,$40,$80

        .code

;---------------------------------------
; void kb_scan(void) - one 60Hz tick
;---------------------------------------
_kb_scan:
        inc _sys_ticks          ; 16-bit tick, ~60Hz
        bne :+
        inc _sys_ticks+1
:
        ; --- joystick rejection ---
        ; A joystick/mouse on control port 1 pulls CIA1 PB lines low
        ; independently of the column select, which reads as an entire
        ; matrix ROW held down (port-1 fire = row 4 = ">MBCZ", space,
        ; rshift, F1...). Keyboard keys can only pull a row low through
        ; a selected column, so with NO column selected anything still
        ; low is external - mask those rows out of this frame's scan.
        lda #$FF
        sta CIA1_PA
@jdeb:  lda CIA1_PB
        cmp CIA1_PB
        bne @jdeb
        eor #$FF
        sta joy_mask            ; 1 = row driven by the joystick
        eor #$FF
        sta not_joy             ; complement, for masking below

        ; --- read the matrix ---
        ldx #7
@col:   lda colmask,x
        sta CIA1_PA
@deb:   lda CIA1_PB
        cmp CIA1_PB
        bne @deb
        eor #$FF                ; 1 = pressed
        and not_joy             ; drop joystick-driven rows
        sta state,x
        dex
        bpl @col
        lda #$7F                ; KERNAL idle value
        sta CIA1_PA

        ; --- modifier state ---
        lda #0
        sta mods
        lda state+1
        and #$80                ; left shift / shift lock
        beq @ls
        lda #MOD_SHIFT
        sta mods
@ls:    lda state+6
        and #$10                ; right shift
        beq @rs
        lda mods
        ora #MOD_SHIFT
        sta mods
@rs:    lda state+7
        and #$04                ; ctrl
        beq @ct
        lda mods
        ora #MOD_CTRL
        sta mods
@ct:    lda state+7
        and #$20                ; commodore
        beq @cb
        lda mods
        ora #MOD_CBM
        sta mods
@cb:
        ; --- diff against previous frame ---
        ldx #7
@dif:   lda prev,x
        eor #$FF
        and state,x
        sta newk,x
        lda state,x
        sta prev,x
        dex
        bpl @dif

        ; --- handle every newly pressed key ---
        ldx #7
@ncol:  lda newk,x
        beq @nnext
        ldy #0
@nrow:  lda bitmask,y
        and newk,x
        beq @nskip
        jsr handle_key          ; X=col, Y=row (preserved)
@nskip: iny
        cpy #8
        bne @nrow
@nnext: dex
        bpl @ncol

        jmp do_repeat

;---------------------------------------
; One newly pressed key at column X, row Y. Preserves X and Y.
;---------------------------------------
handle_key:
        txa
        pha
        tya
        pha

        stx gcol                ; scratch: this key's column
        lda bitmask,y
        sta growbit             ; scratch: this key's row bit

        ; code = col*8 + row
        txa
        asl
        asl
        asl
        sta code
        tya
        ora code
        sta code

        ; ignore the modifier keys themselves
        cmp #CODE_LSHIFT
        beq @done
        cmp #CODE_RSHIFT
        beq @done
        cmp #CODE_CTRL
        beq @done
        cmp #CODE_CBM
        beq @done

        ; --- ghost check: another held key in this column AND another
        ;     held key on this row elsewhere = rectangle ambiguity; the
        ;     hardware would ghost a phantom key, so drop this press ---
        lda growbit
        eor #$FF
        and state,x
        beq decode              ; column otherwise empty: safe
        ldx #7
@grow:  cpx gcol
        beq @gnext
        lda state,x
        and growbit
        bne @done               ; row-mate exists too: ambiguous, drop
@gnext: dex
        bpl @grow
        jmp decode

@done:
        pla
        tay
        pla
        tax
        rts

;---------------------------------------
; Decode `code` through the ROM table for the current modifiers and
; push into the KERNAL buffer.
;---------------------------------------
decode:
        ldy code
        lda mods
        and #MOD_CTRL
        beq @not_ctrl
        lda TAB_CTRL,y
        jmp @have
@not_ctrl:
        lda mods
        and #MOD_SHIFT
        beq @not_shift
        lda TAB_SHIFT,y
        jmp @have
@not_shift:
        lda mods
        and #MOD_CBM
        beq @normal
        lda TAB_CBM,y
        jmp @have
@normal:
        lda TAB_NORM,y
@have:
        cmp #$FF
        beq @done
        cmp #0
        beq @done
        jsr push_char

        ; repeat bookkeeping: this key is the new repeat candidate
        pha
        lda code
        sta rpt_code
        lda #0
        sta rpt_cnt
        pla
        sta rpt_char
        jsr is_repeatable
        sta rpt_ok
@done:
        pla
        tay
        pla
        tax
        rts

;---------------------------------------
; A = char; result A=1 if key should auto-repeat (cursor/del/space)
;---------------------------------------
is_repeatable:
        lda rpt_char
        cmp #$14                ; DEL
        beq @yes
        cmp #$94                ; SHIFT+DEL (INST) - also a backspace
        beq @yes
        cmp #$20                ; space
        beq @yes
        cmp #$11                ; cursor down
        beq @yes
        cmp #$91                ; cursor up
        beq @yes
        cmp #$1D                ; cursor right
        beq @yes
        cmp #$9D                ; cursor left
        beq @yes
        lda #0
        rts
@yes:   lda #1
        rts

;---------------------------------------
; Push A into the KERNAL keyboard buffer (drops when full)
;---------------------------------------
push_char:
        ldx NDX
        cpx #KBUF_MAX
        bcs @full
        sta KBUF,x
        inc NDX
@full:  rts

;---------------------------------------
; Auto-repeat for the held candidate key
;---------------------------------------
do_repeat:
        lda rpt_ok
        beq @none
        lda rpt_code
        cmp #$FF
        beq @none
        ; still held?
        lsr
        lsr
        lsr
        tax                     ; column
        lda rpt_code
        and #7
        tay
        lda bitmask,y
        and state,x
        beq @release
        inc rpt_cnt
        lda rpt_cnt
        cmp #REPEAT_DELAY+4
        bcc @none
        lda rpt_char
        jsr push_char
        lda #REPEAT_NEXT
        sta rpt_cnt
@none:  rts
@release:
        lda #$FF
        sta rpt_code
        lda #0
        sta rpt_ok
        sta rpt_cnt
        rts
