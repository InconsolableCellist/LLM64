;
; C64 LLM Client - ACIA Driver (Assembly)
; 6551 ACIA at $DE00 (SwiftLink compatible)
;
; RX is interrupt-driven: the ACIA raises an IRQ per received byte and the
; handler stores it in a 256-byte ring buffer, so the main program can spend
; milliseconds updating the screen without losing data. TX polls the TDRE
; status bit with a timeout fallback (some real 6551s have a stuck TDRE).
;

        .export _serial_init
        .export _serial_dial
        .export _serial_disconnect
        .export _serial_is_connected
        .export _serial_available
        .export _serial_read
        .export _serial_read_block
        .export _serial_write
        .export _serial_can_write
        .export _serial_flush
        .export _serial_rx_pause
        .export _serial_rx_resume
        .export _acia_init_hw
        .export _acia_send_at_command
        .export _acia_get_status
        .export _serial_rx_count
        .export _serial_overflows
        .export _serial_overruns
        .export rx_used
        ; exported for e2e memory probes via labels.txt (no code cost)
        .export overflows
        .export rx_masked

        .import popa, popax
        .import _kb_scan
        .import _music_play
        .importzp ptr1, ptr2, tmp1, tmp2
        .importzp sp                    ; cc65 C-stack ptr (DIAG low-water)

        .include "diag.inc"

CIA1_ICR = $DC0D

; ACIA registers
ACIA_DATA    = $DE00
ACIA_STATUS  = $DE01
ACIA_COMMAND = $DE02
ACIA_CONTROL = $DE03

; Status bits
ACIA_SR_RDRF = $08      ; Receive Data Register Full
ACIA_SR_TDRE = $10      ; Transmit Data Register Empty

; Control register: bit4 = internal baud generator, bits 0-3 = rate.
; By the 6551's own table %1110 = 9600 and %1111 = 19200, but a real
; SwiftLink - and the C64U's emulation of one - runs a doubled crystal,
; so on hardware those land at 19200 and 38400 respectively.
;
; BAUD38400 is OPT-IN and should stay that way until the client is back
; on NMI. On NMI the ACIA is drained even inside SEI sections, which is
; the only reason the existing rate is safe; on IRQ, every
; interrupts-disabled window longer than one byte time costs a byte, and
; at 38400 a byte time is ~260 cycles - shorter than kb_scan, shorter
; than plenty of ordinary code. See the CLI dance around _music_play
; below, which exists because a play routine already outran one byte
; time at 9600.
.ifdef BAUD38400
ACIA_CTRL_VALUE = $1F
.else
ACIA_CTRL_VALUE = $1E
.endif

; Command register: DTR active (bit0=1), RX IRQ enabled (bit1=0),
; RTS active + TX IRQ off (bits3-2=10), no echo, no parity
ACIA_CMD_VALUE  = %00001001

; KERNAL interrupt vectors
IRQ_VECTOR = $0314
NMI_VECTOR = $0318

        .bss
; 8KB RX ring: bigger than any single response burst can be, so the ACIA
; data register never has to wait on a full ring (VICE's RX delivery can
; stall unrecoverably if RDRF sits unread). Access goes through
; self-modifying absolute-address stubs (below, in DATA) with a 16-bit
; fill counter; the reader masks IRQs around its non-atomic updates.
RX_RING_SIZE = 4096          ; halved from 8KB: worst observed backlog
                             ; is ~300 bytes and the ov counter stands
                             ; guard - freed 4KB banked for the module slot
rx_used:        .res 2          ; bytes currently buffered
connected:      .res 1
vectors_saved:  .res 1
overflows:      .res 1          ; ring-full drops (consumer too slow)
overruns:       .res 1          ; ACIA overrun flags seen (IRQ too late)
in_music:       .res 1          ; music_play reentrancy guard (see tick)
rx_masked:      .res 1          ; RX IRQ currently masked (ring was full).
                                ; Tracked so serial_available only writes
                                ; ACIA_COMMAND on a real transition: the
                                ; C64U modem re-evaluates DTR/RTS on every
                                ; command write, and with 'drop on DTR
                                ; low' / 'RTS handshake' enabled a kHz
                                ; stream of rewrites glitches the line.
fg_lock:        .res 1          ; foreground is mid-pickup of the ACIA data
                                ; register - the NMI must keep its hands off
                                ; it (SEI cannot mask NMI). See fg_pickup.
rx_buffer:      .res RX_RING_SIZE
rx_buffer_end:

        .data
; Chain to the previous NMI handler via a patched absolute JMP. An
; indirect "jmp (vector)" would hit the 6502 page-boundary bug if the
; vector byte ever landed at $xxFF.
nmi_chain:      jmp $0000       ; operand patched in acia_init_hw

; Self-modifying ring access stubs (operands = current write/read
; position inside rx_buffer; initialized by acia_init_hw)
ring_write:                     ; A -> ring, advances write ptr (IRQ ctx)
wr_st:  sta rx_buffer
        inc wr_st+1
        bne @nowrap
        inc wr_st+2
@nowrap:
        lda wr_st+2
        cmp #>rx_buffer_end
        bne @done
        lda wr_st+1
        cmp #<rx_buffer_end
        bne @done
        lda #<rx_buffer
        sta wr_st+1
        lda #>rx_buffer
        sta wr_st+2
@done:  rts

ring_read:                      ; ring -> A, advances read ptr
rd_st:  lda rx_buffer
        inc rd_st+1
        bne @nowrap
        inc rd_st+2
@nowrap:
        pha
        lda rd_st+2
        cmp #>rx_buffer_end
        bne @done
        lda rd_st+1
        cmp #<rx_buffer_end
        bne @done
        lda #<rx_buffer
        sta rd_st+1
        lda #>rx_buffer
        sta rd_st+2
@done:  pla
        rts

        .code

;---------------------------------------
; uint8_t acia_get_status(void)
;---------------------------------------
_acia_get_status:
        lda ACIA_STATUS
        ldx #0
        rts

;---------------------------------------
; void acia_init_hw(void)
; Reset the ACIA, set baud/format, enable RX interrupts, and install
; the interrupt handler on both the IRQ and NMI KERNAL vectors (VICE
; uses IRQ; real SwiftLink cartridges are commonly wired to NMI).
;---------------------------------------
_acia_init_hw:
        ; Programmed reset: any write to the status register
        sta ACIA_STATUS

        ; Small settle delay
        ldy #10
@reset_delay:
        dey
        bne @reset_delay

        lda #ACIA_CTRL_VALUE
        sta ACIA_CONTROL

        ; Reset ring buffer
        lda #0
        sta rx_used
        sta rx_used+1
        sta connected
        lda #<rx_buffer
        sta wr_st+1
        sta rd_st+1
        lda #>rx_buffer
        sta wr_st+2
        sta rd_st+2

        ; Install interrupt handlers (once)
        lda vectors_saved
        bne @vectors_done

        sei
        lda #<acia_irq_entry
        sta IRQ_VECTOR
        lda #>acia_irq_entry
        sta IRQ_VECTOR+1

        lda NMI_VECTOR
        sta nmi_chain+1
        lda NMI_VECTOR+1
        sta nmi_chain+2
        lda #<acia_nmi_entry
        sta NMI_VECTOR
        lda #>acia_nmi_entry
        sta NMI_VECTOR+1

        lda #<raw_irq_entry
        sta $FFFE
        lda #>raw_irq_entry
        sta $FFFF
        lda #<raw_nmi_entry
        sta $FFFA
        lda #>raw_nmi_entry
        sta $FFFB

        lda #1
        sta vectors_saved
        cli

@vectors_done:
        ; Clear any stale byte, then enable the receiver + RX IRQ
        lda ACIA_DATA
        lda ACIA_STATUS
        lda #ACIA_CMD_VALUE
        sta ACIA_COMMAND
        lda #0
        sta rx_masked

        ldy #20
@init_delay:
        dey
        bne @init_delay
        rts

;---------------------------------------
; IRQ entry (via $0314; the KERNAL stub already saved A/X/Y).
;
; The KERNAL service routine is NEVER chained: at 9600 baud a byte
; arrives every ~1000 cycles and the KERNAL's keyboard scan costs about
; that much, so chaining it per byte saturates the CPU and drops frames.
; ACIA bytes take the ~50 cycle fast path below; CIA1 timer ticks (60Hz)
; run our own matrix scanner (keyboard.s) instead of the KERNAL's.
;---------------------------------------
drain_sub:
        lda fg_lock             ; foreground is mid-pickup: leave the data
        bne @rts                ; register alone, it finishes the read
@chk:   lda ACIA_STATUS
        tax
        and #$04                ; overrun flag: a byte was lost in hardware
        beq @no_ovr
        inc overruns
@no_ovr:
        txa
        and #ACIA_SR_RDRF
        beq @rts
        lda rx_used+1
        cmp #>RX_RING_SIZE      ; ring full?
        bcs @full
        lda ACIA_DATA
        jsr ring_write
        inc rx_used
        bne @chk
        inc rx_used+1
        jmp @chk
@full:  inc overflows
        lda #ACIA_CMD_VALUE | 2 ; ring full: mask the RX interrupt (the
        sta ACIA_COMMAND        ; line is level-asserted while RDRF sits,
        lda #1
        sta rx_masked
        rts                     ; so just returning would storm) and leave
                                ; the byte unread - delivery pauses behind
                                ; it. serial_available's pickup unmasks
                                ; and reads it once the ring drains.
@rts:   rts

acia_irq_entry:
.ifdef DIAG
        ; Hardware-stack low-water mark. The music branch below drops I
        ; mid-handler so a tune's play routine can be preempted, which
        ; means IRQ frames can sit on top of each other; this records how
        ; close page 1 ever came to wrapping. Cheapest possible probe,
        ; and X is dead here (the KERNAL stub already saved it).
        tsx
        cpx D_HWLOW
        bcs :+
        stx D_HWLOW
:
        ; cc65 C-stack low-water. sp is 16-bit in zero page and grows
        ; DOWN from __HIMEM__, so a smaller value means deeper: compare
        ; the high byte, and only consult the low byte on a tie.
        lda sp+1
        cmp D_SPHI
        bcc @newsp
        bne @nosp
        lda sp
        cmp D_SPLO
        bcs @nosp
@newsp: lda sp
        sta D_SPLO
        lda sp+1
        sta D_SPHI
@nosp:
.endif
        lda ACIA_STATUS         ; bit7 = this ACIA caused the interrupt
        bpl @not_acia
        jsr drain_sub
        jmp @exit

@not_acia:
        lda CIA1_ICR            ; read acks ALL CIA1 int flags
        and #$01                ; timer A (the 60Hz system tick)?
        beq @exit
        jsr _kb_scan
        ; Let the ACIA preempt the music routine: a streamed SID's play
        ; call can exceed the one-byte grace period at 9600 baud, and
        ; with I set that drops RX bytes mid-response. The CIA flag is
        ; already acked (ICR read above); the guard skips a tick that
        ; lands while music is still running rather than nesting it.
        lda in_music
        bne @exit
        inc in_music
        cli
        jsr _music_play
        sei
        dec in_music

@exit:
        pla                     ; unwind the KERNAL stub's saves
        tay
        pla
        tax
        pla
        rti

;---------------------------------------
; Raw CPU-vector entries, used while the KERNAL ROM is banked out
; (the soft-80 scroll must read the bitmap under the ROM). Written to
; RAM at $FFFE/$FFFA by acia_init_hw; the CPU fetches them from RAM
; whenever HIRAM is off, so serial keeps flowing during the copy.
; The 60Hz keyboard scan is skipped here - its decode tables are in ROM.
;---------------------------------------
raw_irq_entry:
        pha
        txa
        pha
        lda ACIA_STATUS
        bpl @cia
        jsr drain_sub
        jmp @out
@cia:   lda CIA1_ICR            ; ack the timer tick, skip the scan
@out:   pla
        tax
        pla
        rti

raw_nmi_entry:
        pha
        txa
        pha
        lda ACIA_STATUS
        bmi @drain              ; bit7: the ACIA raised this one
        and #ACIA_SR_RDRF       ; bit7 clear but a byte is waiting? then a
        beq @out                ; foreground status read ate our flag - the
@drain: jsr drain_sub           ; interrupt is still ours (see acia_nmi_entry)
@out:   pla
        tax
        pla
        rti

;---------------------------------------
; NMI entry (via $0318; registers NOT yet saved at this point - the
; KERNAL stub that saves them runs after the vector). Real SwiftLink
; cartridges commonly raise NMI, so drain here too; anything that is
; not ours (RESTORE key) chains to the KERNAL with registers intact.
;---------------------------------------
; Bit 7 of the status register is NOT a reliable "was it me" test: the
; 6551 clears it on ANY status read, and the foreground reads status in
; four places (serial_available, serial_write's TDRE poll,
; serial_can_write, serial_flush). If one of those lands between the
; ACIA asserting the line and this handler running, bit 7 reads clear,
; we chain to the KERNAL RESTORE handler, and the received byte stays
; stranded in the data register - fatal at 38400, where the next byte
; overruns before serial_available's poll heals it. So also accept the
; interrupt when RDRF says a byte is waiting; the cost is that a
; RESTORE keypress arriving in the same microseconds as a byte is
; swallowed instead of chained (press it again when the line is idle).
acia_nmi_entry:
        pha
        txa
        pha
        lda ACIA_STATUS
        bmi @drain              ; ours: the interrupt flag is still set
        and #ACIA_SR_RDRF       ; flag eaten by a foreground read?
        beq @chain              ; no byte either: RESTORE etc.
@drain: jsr drain_sub           ; honours fg_lock; counts overruns
        pla
        tax
        pla
        rti
@chain:
        pla
        tax
        pla
        jmp nmi_chain

;---------------------------------------
; fg_pickup - rescue a byte the interrupt never got (foreground context)
;
; Both contexts read the same data register, so the read has to be
; owned by exactly one of them. SEI is not enough: it stops the IRQ
; path but the 6551 raises NMI on real hardware, and an NMI landing
; between our status read and our "lda ACIA_DATA" makes both contexts
; read the register - the NMI banks the byte, we bank a stale copy, and
; the ring gets a duplicate that fails CRC downstream.
;
; fg_lock closes that window without touching ACIA_COMMAND (command
; writes are poison on the C64U modem - it re-evaluates DTR/RTS on each
; one). With the lock held the NMI returns without reading, and this
; routine's re-read of the status register decides who actually takes
; the byte: RDRF still set means it is ours, RDRF clear means the
; interrupt beat us to it and there is nothing to do. Either way
; exactly one context reads ACIA_DATA, and the byte is always read by
; someone - which matters because the 6551 asserts the interrupt LINE
; while RDRF stands, and the 6502's NMI is edge-triggered: a byte whose
; edge was missed would otherwise sit there forever with no new edge
; coming, i.e. serial dead.
;
; Preserves A/X/Y (callers use them for loop state).
;---------------------------------------
fg_pickup:
        php
        sei                     ; lock out the IRQ path first...
        pha
        txa
        pha
        tya
        pha
        lda #1
        sta fg_lock             ; ...then the NMI path
        lda ACIA_STATUS
        and #ACIA_SR_RDRF
        beq @unlock             ; the interrupt already took it
        lda rx_used+1           ; room in the ring?
        cmp #>RX_RING_SIZE
        bcs @unlock             ; full: the masked path owns this byte
        lda ACIA_DATA
        jsr ring_write
        inc rx_used
        bne @unlock
        inc rx_used+1
@unlock:
        lda #0
        sta fg_lock
        pla
        tay
        pla
        tax
        pla
        plp
        rts

;---------------------------------------
; uint8_t serial_available(void)
;
; Always poll the ACIA directly, whatever the ring holds: if a byte's
; interrupt was lost to a status-read race (the 6551 clears its IRQ
; flag on any status read), the byte sits in the data register and -
; with delivery paused behind it - would deadlock the stream. The main
; loop calls this constantly, so a stranded byte heals within
; microseconds. Status READS are line-safe; the ACIA_COMMAND write is
; gated to real mask transitions because the C64U modem re-evaluates
; DTR/RTS on every command write (kHz rewrites glitched the line:
; dropped packets, even hangups with 'drop on DTR low' enabled).
; The pickup itself goes through fg_pickup, which keeps the ring writer
; single-threaded against BOTH interrupt paths.
;---------------------------------------
_serial_available:
        php
        sei
        lda rx_masked
        beq @pick               ; unmask only on a real transition,
        lda rx_used+1           ; and only once the ring has drained
        ora rx_used
        bne @pick
        lda #ACIA_CMD_VALUE
        sta ACIA_COMMAND
        lda #0
        sta rx_masked
@pick:  lda ACIA_STATUS
        and #ACIA_SR_RDRF
        beq @counts
        jsr fg_pickup           ; stranded byte: pick it up ourselves
@counts:
        plp
        lda rx_used
        ora rx_used+1
        beq @empty
        lda #1
        ldx #0
        rts
@empty: lda #0
        ldx #0
        rts

;---------------------------------------
; void serial_rx_pause(void)
;
; Mask the ACIA RX interrupt before a KERNAL disk LOAD (overlay
; modules): JiffyDOS bit-bangs cycle-exact IEC transfers and a serial
; NMI mid-byte would corrupt them. Piggybacks on the ring-full mask
; machinery - _serial_available unmasks automatically once the main
; loop resumes and the ring is drained, so there is no explicit
; resume call.
;---------------------------------------
_serial_rx_pause:
        php
        sei
        lda rx_masked
        bne @done               ; already masked (ring full)
        lda #ACIA_CMD_VALUE | 2
        sta ACIA_COMMAND
        lda #1
        sta rx_masked
@done:  plp
        rts

;---------------------------------------
; void serial_rx_resume(void)
;
; Explicit unmask after a KERNAL disk LOAD. The automatic drain-path
; unmask (in _serial_available) only runs once the main loop pumps
; again - too late when the caller sends a request and then renders
; a full modal before returning (the reply's first ~200 bytes died
; in the masked ACIA's data register: no ring, no counters, no CRC,
; just silence - found via the conversation manager's missing page
; head). Call as soon as the LOAD returns.
;---------------------------------------
_serial_rx_resume:
        php
        sei
        lda rx_masked
        beq @done
        lda rx_used+1           ; genuinely full ring: stay masked,
        cmp #>RX_RING_SIZE      ; the drain path owns the unmask
        bcs @done
        lda #ACIA_CMD_VALUE
        sta ACIA_COMMAND
        lda #0
        sta rx_masked
@done:  plp
        rts

;---------------------------------------
; uint8_t serial_read(void)
; Returns next buffered byte (0 if empty - call serial_available first)
;---------------------------------------
_serial_read:
        lda rx_used
        ora rx_used+1
        beq @empty
        php
        sei
        lda rx_used             ; 16-bit decrement (IRQ-safe)
        bne @dl
        dec rx_used+1
@dl:    dec rx_used
        jsr ring_read
        plp
        ldx #0
        rts
@empty:
        lda #0
        ldx #0
        rts

;---------------------------------------
; uint8_t serial_rx_count(void)
; Bytes currently waiting in the ring buffer
;---------------------------------------
_serial_rx_count:
        lda rx_used+1
        beq @small
        lda #255                ; clamp to the uint8 API
        ldx #0
        rts
@small: lda rx_used
        ldx #0
        rts

;---------------------------------------
; uint8_t serial_read_block(uint8_t* dest, uint8_t max)
; Bulk-copy up to max buffered bytes into dest; returns count copied.
; ~20 cycles/byte vs ~200+ for a serial_read() call per byte - needed to
; keep up with sustained 9600 baud (1042 cycles/byte budget).
;---------------------------------------
_serial_read_block:
        sta tmp1                ; max
        jsr popax               ; dest
        sta ptr1
        stx ptr1+1
        ldy #0
@loop:  cpy tmp1
        bcs @done
        lda rx_used
        ora rx_used+1
        beq @done               ; ring empty
        php
        sei
        lda rx_used
        bne @dl
        dec rx_used+1
@dl:    dec rx_used
        jsr ring_read
        plp
        sta (ptr1),y
        iny
        bne @loop
@done:  tya
        ldx #0
        rts

_serial_overflows:
        lda overflows
        ldx #0
        rts

_serial_overruns:
        lda overruns
        ldx #0
        rts

;---------------------------------------
; uint8_t serial_write(uint8_t byte)
; Waits for TDRE with a timeout fallback (~4 byte times) so a stuck
; TDRE (real 65C51 bug) degrades to pacing instead of hanging.
;
; Every poll of the status register here clears the ACIA's interrupt
; flag, so a byte arriving during a TX burst can lose its interrupt
; entirely - and the client does send windowed ACKs while a stream is
; still arriving. Rather than leave that byte for the next main-loop
; poll (too late at 38400), rescue it right here: the spin is idle time
; anyway. fg_pickup preserves X (the timeout counter) and Y (the byte
; being sent).
;---------------------------------------
_serial_write:
        tay                     ; save byte
        ldx #0                  ; timeout: 256 * ~17 cycles per inner pass
@wait_outer:
        lda ACIA_STATUS
        sta tmp2                ; tmp1 belongs to push_and_write
        and #ACIA_SR_TDRE
        bne @send
        lda tmp2
        and #ACIA_SR_RDRF
        beq @nopick
        jsr fg_pickup
@nopick:
        inx
        bne @wait_outer
        ; TDRE never set: fall back to a fixed one-byte-time delay
        lda #11
@fb_delay:
        ldx #95
@fb_inner:
        dex
        bne @fb_inner
        sec
        sbc #1
        bne @fb_delay
@send:
        sty ACIA_DATA
        lda #1
        ldx #0
        rts

;---------------------------------------
; uint8_t serial_can_write(void)
;---------------------------------------
_serial_can_write:
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        beq @cannot
        lda #1
        ldx #0
        rts
@cannot:
        lda #0
        ldx #0
        rts

;---------------------------------------
; void serial_flush(void)
; Wait until the transmitter is idle (TDRE set, with timeout)
;---------------------------------------
_serial_flush:
        ldx #0
@wait:
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        bne @done
        inx
        bne @wait
@done:
        rts

;---------------------------------------
; uint8_t acia_send_at_command(const char* cmd)
; Send string + CR (response is read by the caller via serial_read)
;---------------------------------------
_acia_send_at_command:
        sta ptr1
        stx ptr1+1
        ldy #0
@send_loop:
        lda (ptr1),y
        beq @send_cr
        jsr push_and_write
        iny
        bne @send_loop
@send_cr:
        lda #13
        jsr push_and_write
        lda #0
        rts

push_and_write:
        sty tmp1                ; _serial_write clobbers Y
        jsr _serial_write
        ldy tmp1
        rts

;---------------------------------------
; uint8_t serial_dial(const char* dial_str)
; Send an AT dial command; caller watches for CONNECT via serial_read.
;---------------------------------------
_serial_dial:
        jsr _acia_send_at_command
        lda #1
        sta connected
        lda #0
        rts

;---------------------------------------
; uint8_t serial_init(const char* hostname, uint16_t port)
;---------------------------------------
_serial_init:
        jsr _acia_init_hw
        lda #0
        sta connected
        lda #0
        rts

;---------------------------------------
; void serial_disconnect(void)
;---------------------------------------
_serial_disconnect:
        lda #0
        sta connected
        rts

;---------------------------------------
; uint8_t serial_is_connected(void)
;---------------------------------------
_serial_is_connected:
        lda connected
        ldx #0
        rts
