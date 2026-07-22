;
; C64 LLM Client - SID music player
;
; Drives a streamed SID relocated server-side (sidreloc) into the
; $B000-$BFFF window, with zero page confined to $FB-$FE so it coexists
; with cc65 and the KERNAL runtime.
;
; This file used to carry a 3-voice PATTERN PLAYER with two tunes built
; in as data ("Dungeon Depths", "Northward Road"), from before the proxy
; could stream real SIDs. A 10,000-tune HVSC library replaced it, the F1
; entry that cycled them is gone, and the whole player - sequencer, note
; table and both tunes - came out with it. See git history if it is ever
; wanted back; it cost ~600 bytes of a resident image with ~400 free.
;
; music_play is called from the 60Hz CIA tick in the serial IRQ handler.
;

        .export _music_play
        .export _music_state
        .export _music_ext_init
        .export _music_ext_play_addr
        .export _music_ext_song
        .export _music_ext_vol
        .export _music_ext_begin
        .export _music_ext_stop
        .export _music_hold_begin
        .export _music_hold_end
        .export _music_hold

SID       = $D400
SID_V1    = SID
SID_VOL   = SID+$18
ZP_PTR    = $FB          ; 2 bytes, free on stock C64 + cc65

EXT_STATE = $FF          ; _music_state value: streamed SID at $B000 active

        .bss
_music_state: .res 1     ; 0 = off, $FF = streamed SID playing
_music_hold:  .res 1     ; nonzero: skip the tick without forgetting the tune
_music_ext_init:      .res 2   ; relocated PSID init address
_music_ext_play_addr: .res 2   ; relocated PSID play address
_music_ext_song:      .res 1   ; 0-based subtune for the init call
_music_ext_vol:       .res 1   ; $D418 override (loudness normalization,
                               ; filter bits included); 0 = no override

        .rodata
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
        jsr music_silence       ; silence any previous tune
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

;---------------------------------------
; void music_hold_begin(void) / void music_hold_end(void)
;
; Mute the 60Hz tick WITHOUT forgetting which tune is playing, so a
; KERNAL disk LOAD gets the machine to itself and the song then carries
; on from where it was instead of restarting. music_ext_stop() would
; also make the load safe, but it costs the user their soundtrack every
; time they press F1 - the tune can only be restarted from bar one.
;
; The volume goes to zero for the duration: a single held note ringing
; through a two-second load is worse than a gap. The next unmuted tick
; reinstates it via the _music_ext_vol override.
;---------------------------------------
_music_hold_begin:
        lda #1
        sta _music_hold
        lda #0
        sta SID_VOL
        rts

_music_hold_end:
        lda #0
        sta _music_hold
        rts

; void music_ext_stop(void)
_music_ext_stop:
        lda #0
        sta _music_state
        jmp music_silence

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
        lda _music_hold
        bne @ret
        lda _music_state
        bne :+
@ret:   rts
:       jsr call_play           ; streamed SID's own play routine
        ; loudness normalization: our volume byte wins over whatever the
        ; tune wrote (vetted server-side: skipped for $D418-live tunes)
        lda _music_ext_vol
        beq @novol
        sta SID_VOL
@novol: rts
