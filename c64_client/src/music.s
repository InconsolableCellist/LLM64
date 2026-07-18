;
; C64 LLM Client - SID music player
;
; A compact 3-voice pattern player with tunes built in as data, instead
; of external .sid files: HVSC SIDs mostly load at $1000 (over our code)
; and clobber zero page at will, while this player owns exactly two ZP
; bytes ($FB/$FC - unused by cc65 and the KERNAL runtime) and lives in
; normal linked code.
;
; music_play is called from the 60Hz CIA tick in the serial IRQ handler.
; Sequences are (note, duration) pairs; note $FF = rest, $FE = loop.
; Note indices: C2=0 .. B5=47 (see table below).
;

        .export _music_play
        .export _music_next
        .export _music_state
        .export _music_ext_init
        .export _music_ext_play_addr
        .export _music_ext_song
        .export _music_ext_vol
        .export _music_ext_begin
        .export _music_ext_stop

SID       = $D400
SID_V1    = SID
SID_VOL   = SID+$18
ZP_PTR    = $FB          ; 2 bytes, free on stock C64 + cc65

NUM_TUNES = 2
EXT_STATE = $FF          ; _music_state value: streamed SID at $B000 active

        .bss
_music_state: .res 1     ; 0 = off, 1..NUM_TUNES = pattern tune, $FF = ext SID
_music_ext_init:      .res 2   ; relocated PSID init address
_music_ext_play_addr: .res 2   ; relocated PSID play address
_music_ext_song:      .res 1   ; 0-based subtune for the init call
_music_ext_vol:       .res 1   ; $D418 override (loudness normalization,
                               ; filter bits included); 0 = no override
vseq_lo:  .res 3         ; current sequence position per voice
vseq_hi:  .res 3
vbase_lo: .res 3         ; loop restart position
vbase_hi: .res 3
vdur:     .res 3         ; ticks left on current event
vwave:    .res 3         ; waveform byte (gate bit managed here)

        .rodata
; PAL SID pitch values, C2 (index 0) .. B5 (index 47)
note_lo:
  .byte $5A,$9C,$E2,$2D,$7B,$CF,$27,$85,$E8,$51,$C1,$37
  .byte $B4,$38,$C4,$59,$F7,$9D,$4E,$0A,$D0,$A2,$81,$6D
  .byte $67,$70,$89,$B2,$ED,$3B,$9C,$13,$A0,$45,$02,$DA
  .byte $CE,$E0,$11,$64,$DA,$76,$39,$26,$40,$89,$04,$B4
note_hi:
  .byte $04,$04,$04,$05,$05,$05,$06,$06,$06,$07,$07,$08
  .byte $08,$09,$09,$0A,$0A,$0B,$0C,$0D,$0D,$0E,$0F,$10
  .byte $11,$12,$13,$14,$15,$17,$18,$1A,$1B,$1D,$1F,$20
  .byte $22,$24,$27,$29,$2B,$2E,$31,$34,$37,$3A,$3E,$41

; Note index helpers (C2=0)
REST = $FF
LOOP = $FE

; --- Tune 1: "Dungeon Depths" - slow A-minor, for dark adventures -----
; eighth note = 16 ticks (~112 bpm feel in 6/8)

; voice 1: bass, triangle
t1_bass:
  .byte  9,48,  9,48, 21,24,  9,24    ; A2 A2 A3 A2
  .byte  5,48,  5,48, 17,24,  5,24    ; F2 F2 F3 F2
  .byte  7,48,  7,48,  4,24,  7,24    ; G2 G2 E2 G2
  .byte  9,48,  4,48,  9,48, REST,48  ; A2 E2 A2 .
  .byte LOOP

; voice 2: melody, pulse
t1_mel:
  .byte REST,48
  .byte 21,16, 24,16, 28,32           ; A3 C4 E4
  .byte 26,16, 24,16, 23,32           ; D4 C4 B3
  .byte 21,48, REST,16
  .byte 24,16, 28,16, 33,32           ; C4 E4 A4
  .byte 31,16, 28,16, 26,32           ; G4 E4 D4
  .byte 28,64, REST,32
  .byte 23,16, 24,16, 26,32           ; B3 C4 D4
  .byte 24,16, 23,16, 21,48           ; C4 B3 A3
  .byte REST,64
  .byte LOOP

; voice 3: slow drone fifths, sawtooth (quiet sustain)
t1_pad:
  .byte 16,96, 14,96, 16,96, 12,96    ; E3 D3 E3 C3
  .byte LOOP

; --- Tune 2: "Northward Road" - brighter D-mixolydian traveling tune --

; voice 1: bass, triangle (walking)
t2_bass:
  .byte  2,24,  2,24,  9,24, 14,24    ; D2 D2 A2 D3
  .byte  0,24,  0,24,  7,24, 12,24    ; C2 C2 G2 C3
  .byte  7,24,  7,24, 14,24,  7,24    ; G2 G2 D3 G2
  .byte  2,24,  9,24,  2,48           ; D2 A2 D2
  .byte LOOP

; voice 2: melody, pulse
t2_mel:
  .byte 26,24, 30,24, 33,24, 38,24    ; D4 F#4 A4 D5
  .byte 36,24, 33,24, 31,48           ; C5 A4 G4
  .byte 31,24, 33,24, 31,24, 28,24    ; G4 A4 G4 E4
  .byte 26,72, REST,24
  .byte 33,24, 38,24, 40,24, 38,24    ; A4 D5 E5 D5
  .byte 36,24, 33,24, 31,48           ; C5 A4 G4
  .byte 28,24, 31,24, 28,24, 24,24    ; E4 G4 E4 C4
  .byte 26,72, REST,24
  .byte LOOP

; voice 3: off-beat chords feel, saw
t2_pad:
  .byte REST,24, 21,24, REST,24, 21,24  ; . A3 . A3
  .byte REST,24, 19,24, REST,24, 19,24  ; . G3 . G3
  .byte REST,24, 19,24, REST,24, 19,24
  .byte 21,48, 18,48                    ; A3 F#3
  .byte LOOP

; per tune: 3 sequence addresses, waveform and ADSR per voice
tune_seq_lo: .byte <t1_bass, <t1_mel, <t1_pad
             .byte <t2_bass, <t2_mel, <t2_pad
tune_seq_hi: .byte >t1_bass, >t1_mel, >t1_pad
             .byte >t2_bass, >t2_mel, >t2_pad
tune_wave:   .byte $10, $40, $20       ; tri, pulse, saw
             .byte $10, $40, $20
tune_ad:     .byte $0A, $2A, $6A
             .byte $09, $18, $58
tune_sr:     .byte $A9, $69, $39       ; sustain levels: bass>mel>pad
             .byte $99, $79, $49

; SID register offset per voice (0, 7, 14)
voff:        .byte 0, 7, 14

        .code

;---------------------------------------
; Streamed-SID control. The tune was relocated server-side (sidreloc) to
; $B000-$BFFF with zero page confined to $FB-$FE, so it coexists with
; cc65 and the pattern player (which is never active at the same time).
;---------------------------------------

; void music_ext_begin(void) - call from mainline AFTER the window is
; filled and _music_ext_init/_music_ext_play_addr/_music_ext_song are set
_music_ext_begin:
        jsr music_silence       ; stop pattern player / previous tune
        sei
        lda _music_ext_song
        jsr call_init           ; PSID init: A = 0-based song
        lda #EXT_STATE
        sta _music_state
        cli
        rts

call_init:
        jmp (_music_ext_init)

call_play:
        jmp (_music_ext_play_addr)

; void music_ext_stop(void)
_music_ext_stop:
        lda #0
        sta _music_state
        jmp music_silence

;---------------------------------------
; void music_next(void)
; Cycle: off -> tune1 -> tune2 -> off
; While a streamed SID plays, S stops it (back to off).
;---------------------------------------
_music_next:
        lda _music_state
        cmp #EXT_STATE
        bne @cycle
        jmp _music_ext_stop
@cycle: lda _music_state
        clc
        adc #1
        cmp #NUM_TUNES+1
        bcc :+
        lda #0
:       sta _music_state
        beq music_silence
        ; fall through: start tune in A (1-based)

; start the tune in _music_state: point voices at sequences, set ADSR
music_start:
        lda _music_state
        sec
        sbc #1                  ; tune index 0-based
        sta tmp_tune
        asl
        clc
        adc tmp_tune            ; * 3
        sta tmp_tune            ; base index into per-voice tables

        sei
        ldx #0                  ; voice 0..2
@voice: txa
        clc
        adc tmp_tune
        tay                     ; Y = tune*3 + voice (table index)
        lda tune_seq_lo,y
        sta vseq_lo,x
        sta vbase_lo,x
        lda tune_seq_hi,y
        sta vseq_hi,x
        sta vbase_hi,x
        lda tune_wave,y
        sta vwave,x
        lda tune_ad,y
        sta tmp_note
        lda tune_sr,y
        pha
        lda voff,x
        tay                     ; Y = SID register base for voice
        lda tmp_note
        sta SID+5,y             ; attack/decay
        pla
        sta SID+6,y             ; sustain/release
        lda #$00
        sta SID+2,y             ; pulse width lo
        lda #$08
        sta SID+3,y             ; pulse width hi (50%)
        lda #1
        sta vdur,x              ; fetch first event on next tick
        inx
        cpx #3
        bne @voice
        lda #$0F
        sta SID_VOL
        cli
        rts

music_silence:
        sei
        ldx #0
@off:   lda voff,x
        tay
        lda #$00
        sta SID+4,y             ; gate off, wave clear
        inx
        cpx #3
        bne @off
        lda #$00
        sta SID_VOL
        cli
        rts

;---------------------------------------
; void music_play(void) - one 60Hz tick (IRQ context; A/X/Y free)
;---------------------------------------
_music_play:
        lda _music_state
        bne :+
        rts
:       cmp #EXT_STATE
        bne @pattern
        jsr call_play           ; streamed SID's own play routine
        ; loudness normalization: our volume byte wins over whatever the
        ; tune wrote (vetted server-side: skipped for $D418-live tunes)
        lda _music_ext_vol
        beq @novol
        sta SID_VOL
@novol: rts
@pattern:
        ldx #0                  ; voice
@voice: dec vdur,x
        beq @advance
        lda vdur,x
        cmp #3                  ; near note end: release gate
        bne @next
        lda vwave,x
        ldy voff,x
        sta SID+4,y             ; gate bit off
        jmp @next

@advance:
        lda vseq_lo,x
        sta ZP_PTR
        lda vseq_hi,x
        sta ZP_PTR+1
        ldy #0
        lda (ZP_PTR),y          ; note (or control)
        cmp #LOOP
        bne @have
        ; loop: reset to base and refetch
        lda vbase_lo,x
        sta ZP_PTR
        sta vseq_lo,x
        lda vbase_hi,x
        sta ZP_PTR+1
        sta vseq_hi,x
        lda (ZP_PTR),y
@have:
        sta tmp_note
        iny
        lda (ZP_PTR),y          ; duration
        sta vdur,x
        ; advance sequence pointer by 2
        lda vseq_lo,x
        clc
        adc #2
        sta vseq_lo,x
        bcc :+
        inc vseq_hi,x
:
        lda tmp_note
        cmp #REST
        beq @rest
        ; start note: freq + gate on
        tay
        lda note_lo,y
        pha
        lda note_hi,y
        ldy voff,x
        sta SID+1,y
        pla
        sta SID+0,y
        lda vwave,x
        ora #$01                ; gate on
        sta SID+4,y
        jmp @next
@rest:
        lda vwave,x
        ldy voff,x
        sta SID+4,y             ; gate off
@next:
        inx
        cpx #3
        beq @done
        jmp @voice
@done:  rts

        .bss
tmp_tune: .res 1
tmp_x:    .res 1
tmp_note: .res 1
