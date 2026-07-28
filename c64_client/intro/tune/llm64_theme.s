;
; "Attract Mode" - the intro theme, written for this project.
;
; A three-voice PSID player small enough to read in one sitting, so the
; shareware intro can ship a tune nobody else owns. Every HVSC tune is
; copyrighted by its composer; this one is ours, and it is the only
; music in the repository.
;
; Assembled to a flat image at $B000 - the same window the client
; streams relocated tunes into - so make_intro_assets.py treats it like
; any other library tune. Regenerate the .sid with:
;
;     python3 tools/make_intro_tune.py
;
; which also writes song.inc (note table + patterns) from the score kept
; in that script.
;
; Zero page is confined to $FB-$FE, the same discipline sidreloc is told
; to enforce for library tunes, so this can also stand in as a test
; fixture inside the client without colliding with cc65 or the KERNAL.
; (As it happens the player needs no zero page at all.)
;

        .setcpu "6502"
        .segment "TUNE"

SID       = $D400
V2        = 7                   ; voice register strides
V3        = 14

; ---------------------------------------------------------------
; Entry points. The PSID header names these two addresses, so they
; must stay first and in this order.
; ---------------------------------------------------------------
        jmp init                ; $B000
        jmp play                ; $B003

; ---------------------------------------------------------------
; init: A = subtune (there is only one, so it is ignored)
;
; No SEI/CLI: the caller owns the interrupt state. The client's
; music_ext_begin already brackets this call, and the intro calls it
; with interrupts off.
; ---------------------------------------------------------------
init:
        ldx #$18                ; clear every SID register
        lda #0
@clr:   sta SID,x
        dex
        bpl @clr

        lda #$00                ; voice 1: 50% pulse
        sta SID+2
        lda #$08
        sta SID+3
        lda #$28                ; attack 2, decay 8
        sta SID+5
        lda #$59                ; sustain 5, release 9
        sta SID+6

        lda #$18                ; voice 2 (bass): faster attack
        sta SID+V2+5
        lda #$49
        sta SID+V2+6

        lda #$08                ; voice 3 (arpeggio): pluck
        sta SID+V3+5
        lda #$29
        sta SID+V3+6

        lda #$0f                ; full volume, no filter
        sta SID+$18

        lda #0
        sta row1
        sta row2
        sta row3
        sta arp_step
        sta cur3
        sta maj3
        lda #1                  ; first row on the very next play call
        sta tick
        rts

; ---------------------------------------------------------------
; play: called once per frame (50/60 Hz - the client's tick).
; ---------------------------------------------------------------
play:
        jsr do_arp              ; voice 3 moves every frame
        dec tick
        beq @row
        rts

@row:   lda #TEMPO
        sta tick

        ldx row1                ; --- voice 1: melody
        lda trk1,x
        cmp #$ff
        bne :+
        ldx #0
        lda trk1
:       inx
        stx row1
        ldx #0                  ; voice 1 register base
        ldy #$40                ; pulse
        jsr set_note

        ldx row2                ; --- voice 2: bass
        lda trk2,x
        cmp #$ff
        bne :+
        ldx #0
        lda trk2
:       inx
        stx row2
        ldx #V2
        ldy #$20                ; sawtooth
        jsr set_note

        ldx row3                ; --- voice 3: chord root + major flag
        lda trk3,x
        cmp #$ff
        bne :+
        ldx #0
        lda trk3
:       inx
        stx row3
        tax                     ; stash the raw byte
        and #$7f                ; bits 0-6: root note
        sta cur3
        txa
        and #$80                ; bit 7: major chord
        sta maj3
        lda cur3
        ldx #V3
        ldy #$10                ; triangle
        jsr set_note
        rts

; ---------------------------------------------------------------
; set_note: A = note index (0 = rest), X = voice offset,
;           Y = waveform bits. Retriggers the envelope.
; ---------------------------------------------------------------
set_note:
        sty wave
        cmp #0
        beq @off
        tay
        lda freq_lo-1,y         ; the table is 1-based; 0 means rest
        sta SID+0,x
        lda freq_hi-1,y
        sta SID+1,x
        lda wave
        sta SID+4,x             ; gate low...
        ora #$01
        sta SID+4,x             ; ...then high: attack restarts
        rts
@off:   lda wave
        sta SID+4,x             ; gate low only: let it release
        rts

; ---------------------------------------------------------------
; do_arp: cycle voice 3 through root / third / fifth, one step per
; frame. This is what makes three voices sound like a band.
; ---------------------------------------------------------------
do_arp:
        lda cur3
        beq @rts                ; silent: nothing to arpeggiate
        ldy arp_step
        ldx maj3
        beq @minor
        clc
        adc arp_maj,y
        jmp @set
@minor: clc
        adc arp_min,y
@set:   tay
        lda freq_lo-1,y
        sta SID+V3+0
        lda freq_hi-1,y
        sta SID+V3+1
        inc arp_step
        lda arp_step
        cmp #3
        bcc @rts
        lda #0
        sta arp_step
@rts:   rts

arp_min: .byte 0, 3, 7          ; minor triad, in semitones
arp_maj: .byte 0, 4, 7

; ---------------------------------------------------------------
; State. Lives inside the tune image (the $B000 window is RAM), so
; the player needs no zero page and no external storage.
; ---------------------------------------------------------------
tick:     .byte 0
row1:     .byte 0
row2:     .byte 0
row3:     .byte 0
arp_step: .byte 0
cur3:     .byte 0
maj3:     .byte 0
wave:     .byte 0

; TEMPO, freq_lo/freq_hi and trk1..trk3, generated by
; tools/make_intro_tune.py from the score kept there.
        .include "song.inc"
