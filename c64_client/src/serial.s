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
        .export _acia_init_hw
        .export _acia_send_at_command
        .export _acia_get_status
        .export _serial_rx_count
        .export _serial_overflows
        .export _serial_overruns

        .import popa, popax
        .import _kb_scan
        .importzp ptr1, ptr2, tmp1, tmp2

CIA1_ICR = $DC0D

; ACIA registers
ACIA_DATA    = $DE00
ACIA_STATUS  = $DE01
ACIA_COMMAND = $DE02
ACIA_CONTROL = $DE03

; Status bits
ACIA_SR_RDRF = $08      ; Receive Data Register Full
ACIA_SR_TDRE = $10      ; Transmit Data Register Empty

; Control register: bit4 = internal baud generator, bits 0-3 = rate
; %1110 = 9600 baud (standard 6551 crystal; doubled on real SwiftLink)
ACIA_CTRL_VALUE = $1E

; Command register: DTR active (bit0=1), RX IRQ enabled (bit1=0),
; RTS active + TX IRQ off (bits3-2=10), no echo, no parity
ACIA_CMD_VALUE  = %00001001

; KERNAL interrupt vectors
IRQ_VECTOR = $0314
NMI_VECTOR = $0318

        .bss
rx_head:        .res 1          ; write index (IRQ handler)
rx_tail:        .res 1          ; read index (main program)
connected:      .res 1
vectors_saved:  .res 1
overflows:      .res 1          ; ring-full drops (consumer too slow)
overruns:       .res 1          ; ACIA overrun flags seen (IRQ too late)
rx_buffer:      .res 256        ; ring buffer (8-bit indices wrap naturally)

        .data
; Chain to the previous NMI handler via a patched absolute JMP. An
; indirect "jmp (vector)" would hit the 6502 page-boundary bug if the
; vector byte ever landed at $xxFF.
nmi_chain:      jmp $0000       ; operand patched in acia_init_hw

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
        sta rx_head
        sta rx_tail
        sta connected

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

        lda #1
        sta vectors_saved
        cli

@vectors_done:
        ; Clear any stale byte, then enable the receiver + RX IRQ
        lda ACIA_DATA
        lda ACIA_STATUS
        lda #ACIA_CMD_VALUE
        sta ACIA_COMMAND

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
acia_irq_entry:
        lda ACIA_STATUS         ; bit7 = this ACIA caused the interrupt
        bpl @not_acia
@drain: tax                     ; keep status
        and #$04                ; overrun flag: a byte was lost in hardware
        beq @no_ovr
        inc overruns
@no_ovr:
        txa
        and #ACIA_SR_RDRF
        beq @exit
        lda ACIA_DATA
        ldx rx_head
        sta rx_buffer,x
        inx
        cpx rx_tail             ; full? drop newest rather than corrupt ring
        beq @full
        stx rx_head
        lda ACIA_STATUS         ; another byte already waiting?
        jmp @drain
@full:
        inc overflows

@not_acia:
        lda CIA1_ICR            ; read acks ALL CIA1 int flags
        and #$01                ; timer A (the 60Hz system tick)?
        beq @exit
        jsr _kb_scan

@exit:
        pla                     ; unwind the KERNAL stub's saves
        tay
        pla
        tax
        pla
        rti

;---------------------------------------
; NMI entry (via $0318; registers NOT yet saved at this point - the
; KERNAL stub that saves them runs after the vector). Real SwiftLink
; cartridges commonly raise NMI, so drain here too; anything that is
; not ours (RESTORE key) chains to the KERNAL with registers intact.
;---------------------------------------
acia_nmi_entry:
        pha
        txa
        pha
        lda ACIA_STATUS
        bpl @chain              ; not the ACIA: RESTORE etc.
@drain: and #ACIA_SR_RDRF
        beq @ours
        lda ACIA_DATA
        ldx rx_head
        sta rx_buffer,x
        inx
        cpx rx_tail
        beq @ours
        stx rx_head
        lda ACIA_STATUS
        jmp @drain
@ours:
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
; uint8_t serial_available(void)
;---------------------------------------
_serial_available:
        lda rx_head
        cmp rx_tail
        beq @empty
        lda #1
        ldx #0
        rts
@empty:
        lda #0
        ldx #0
        rts

;---------------------------------------
; uint8_t serial_read(void)
; Returns next buffered byte (0 if empty - call serial_available first)
;---------------------------------------
_serial_read:
        ldx rx_tail
        cpx rx_head
        beq @empty
        lda rx_buffer,x
        inc rx_tail
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
        lda rx_head
        sec
        sbc rx_tail
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
        ldx rx_tail
        cpx rx_head
        beq @done               ; ring empty
        lda rx_buffer,x
        sta (ptr1),y
        inc rx_tail
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
; Waits for TDRE with a timeout fallback (~2 byte times) so a stuck
; TDRE (real 65C51 bug) degrades to pacing instead of hanging.
;---------------------------------------
_serial_write:
        tay                     ; save byte
        ldx #0                  ; timeout: 256 * ~9 cycles per inner pass
@wait_outer:
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        bne @send
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
