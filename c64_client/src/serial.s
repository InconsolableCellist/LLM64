;
; C64 LLM Client - ACIA Driver (Assembly)
; 6551 ACIA at $DE00 (SwiftLink compatible)
;

        .export _serial_init
        .export _serial_dial
        .export _serial_disconnect
        .export _serial_is_connected
        .export _serial_available
        .export _serial_read
        .export _serial_write
        .export _serial_can_write
        .export _serial_flush
        .export _acia_init_hw
        .export _acia_send_at_command
        .export _acia_get_status
        .export acia_irq_handler

        .import popa, popax
        .importzp ptr1, ptr2, tmp1, tmp2

; ACIA registers
ACIA_DATA    = $DE00
ACIA_STATUS  = $DE01
ACIA_COMMAND = $DE02
ACIA_CONTROL = $DE03

; Status bits
ACIA_SR_RDRF = $08      ; Receive Data Register Full
ACIA_SR_TDRE = $10      ; Transmit Data Register Empty

; Circular buffer pointers (zero page would be ideal, but using BSS)
        .bss
rx_head:        .res 2          ; RX buffer head pointer
rx_tail:        .res 2          ; RX buffer tail pointer
tx_head:        .res 2          ; TX buffer head pointer
tx_tail:        .res 2          ; TX buffer tail pointer
connected:      .res 1          ; Connection status
old_irq:        .res 2          ; Original IRQ vector

; Buffers (in fixed memory locations)
RX_BUFFER_ADDR  = $2C00
TX_BUFFER_ADDR  = $3000
RX_BUFFER_SIZE  = 1024
TX_BUFFER_SIZE  = 256

        .code

;---------------------------------------
; Get ACIA status register (for debugging)
; uint8_t acia_get_status(void)
;---------------------------------------
_acia_get_status:
        lda ACIA_STATUS
        ldx #0
        rts

;---------------------------------------
; Initialize ACIA hardware
; void acia_init_hw(void)
;---------------------------------------
_acia_init_hw:
        ; Programmed reset - disable interrupts, set known state
        ; This is the proper way to reset the 6551
        lda #%00001010          ; DTR disabled, RTS high, interrupts off
        sta ACIA_COMMAND

        ; Small delay after reset
        ldy #10
@reset_delay:
        dey
        bne @reset_delay

        ; Set control register (9600 baud, 8N1)
        ; Bits 0-3: 1110 = 9600 baud
        ; Bit 4: 1 = internal baud rate generator
        ; Bits 5-6: 00 = 8 data bits
        ; Bit 7: 0 = 1 stop bit
        lda #$1E                ; 9600 baud, 8 data bits, 1 stop
        sta ACIA_CONTROL

        ; Read status to clear any pending flags
        lda ACIA_STATUS

        ; Set command register for operation
        ; Bit 0: 1 = DTR low (active/ready)
        ; Bit 1: 0 = RX IRQ disabled
        ; Bits 2-3: 10 = RTS low (active), TX IRQ disabled (polling mode)
        ; Bit 4: 0 = no echo
        ; Bits 5-7: 000 = no parity
        lda #%00001001          ; DTR active, RTS active, polling mode, no parity
        sta ACIA_COMMAND

        ; Small delay for hardware to stabilize
        ldy #20
@init_delay:
        dey
        bne @init_delay

        ; Initialize buffer pointers
        lda #<RX_BUFFER_ADDR
        sta rx_head
        sta rx_tail
        lda #>RX_BUFFER_ADDR
        sta rx_head+1
        sta rx_tail+1

        lda #<TX_BUFFER_ADDR
        sta tx_head
        sta tx_tail
        lda #>TX_BUFFER_ADDR
        sta tx_head+1
        sta tx_tail+1

        lda #0
        sta connected

        rts

;---------------------------------------
; Send AT command and wait for response
; uint8_t acia_send_at_command(const char* cmd)
; Returns 0 on success
;---------------------------------------
_acia_send_at_command:
        sta ptr1                ; Store string pointer
        stx ptr1+1

        ; Send the command string
@send_loop:
        ldy #0
        lda (ptr1),y
        beq @send_cr            ; Null terminator
        jsr send_byte
        inc ptr1
        bne @send_loop
        inc ptr1+1
        bne @send_loop

@send_cr:
        lda #13                 ; Send CR
        jsr send_byte

        ; Wait for response (simplified - just wait a bit)
        ; In a full implementation, we'd parse "OK" or "CONNECT"
        ldx #50                 ; Wait ~500ms
@wait_loop:
        ldy #100
@inner_wait:
        dey
        bne @inner_wait
        dex
        bne @wait_loop

        lda #0                  ; Success
        rts

;---------------------------------------
; Send a single byte (blocking)
; Note: 6551 TDRE flag is buggy, so we use a fixed delay
;---------------------------------------
send_byte:
        pha

        ; Write byte to ACIA
        pla
        sta ACIA_DATA

        ; Fixed delay to allow byte to transmit
        ; At 9600 baud: ~1042 microseconds per byte
        ; On 1 MHz C64: need ~1042 cycles
        pha
        lda #11                 ; Outer loop count
@tx_delay:
        ldx #95                 ; Inner loop count (~95*11 ≈ 1045 cycles)
@tx_inner:
        dex
        bne @tx_inner
        sec
        sbc #1
        bne @tx_delay
        pla
        rts

;---------------------------------------
; Send dial command with hostname:port
; uint8_t serial_dial(const char* dial_str)
;---------------------------------------
_serial_dial:
        sta ptr1
        stx ptr1+1
        jsr _acia_send_at_command

        ; Wait for CONNECT response (with timeout)
        ; The modem should respond with "CONNECT" or "CONNECT 9600"
        ldx #200                ; Timeout counter (~2 seconds)
@wait_connect:
        ldy #100
@inner_wait:
        ; Check if data available
        lda ACIA_STATUS
        and #ACIA_SR_RDRF
        bne @got_data           ; Data available, modem is responding

        dey
        bne @inner_wait
        dex
        bne @wait_connect

        ; Timeout - connection failed
        lda #0
        sta connected
        lda #1                  ; Error
        rts

@got_data:
        ; Data received, connection successful
        ; Drain the CONNECT response and any remaining bytes
        ldx #200                ; Read up to 200 times
@drain_loop:
        lda ACIA_STATUS
        and #ACIA_SR_RDRF
        beq @drain_done         ; No more data
        lda ACIA_DATA           ; Actually read and discard the byte
        dex
        bne @drain_loop

@drain_done:
        ; Small delay to let connection stabilize
        ldy #50
@delay_outer:
        ldx #100
@delay_inner:
        dex
        bne @delay_inner
        dey
        bne @delay_outer

        ; Mark as connected
        lda #1
        sta connected

        lda #0                  ; Success
        rts

;---------------------------------------
; Initialize serial and connect
; uint8_t serial_init(const char* hostname, uint16_t port)
;---------------------------------------
_serial_init:
        ; For now, hostname and port are ignored
        ; We'll just send the Hayes AT commands

        ; Initialize hardware
        jsr _acia_init_hw

        ; Send ATZ (reset modem)
        lda #<at_reset
        ldx #>at_reset
        jsr _acia_send_at_command

        ; Send ATE0 (echo off)
        lda #<at_echo_off
        ldx #>at_echo_off
        jsr _acia_send_at_command

        ; Note: Caller should call serial_dial() separately
        ; to establish actual connection
        lda #0
        sta connected

        lda #0                  ; Success
        rts

;---------------------------------------
; Disconnect
; void serial_disconnect(void)
;---------------------------------------
_serial_disconnect:
        lda #0
        sta connected
        ; Could send +++ and ATH here
        rts

;---------------------------------------
; Check if connected
; uint8_t serial_is_connected(void)
;---------------------------------------
_serial_is_connected:
        lda connected
        ldx #0
        rts

;---------------------------------------
; Check bytes available
; uint8_t serial_available(void)
;---------------------------------------
_serial_available:
        ; Check if hardware has data
        lda ACIA_STATUS
        and #ACIA_SR_RDRF
        beq @no_data

        ; Data available
        lda #1
        ldx #0
        rts

@no_data:
        ; Also check our buffer
        lda rx_head
        cmp rx_tail
        bne @has_buffered
        lda rx_head+1
        cmp rx_tail+1
        bne @has_buffered

        ; No data
        lda #0
        ldx #0
        rts

@has_buffered:
        lda #1
        ldx #0
        rts

;---------------------------------------
; Read one byte (non-blocking)
; uint8_t serial_read(void)
;---------------------------------------
_serial_read:
        ; Check if data available in hardware
        lda ACIA_STATUS
        and #ACIA_SR_RDRF
        beq @no_hw_data

        ; Read from hardware
        lda ACIA_DATA
        ldx #0
        rts

@no_hw_data:
        ; Try buffer (simplified for now)
        lda #0
        ldx #0
        rts

;---------------------------------------
; Write one byte (blocking - waits for TX ready)
; uint8_t serial_write(uint8_t byte)
; Note: 6551 TDRE flag is buggy, use fixed delay
;---------------------------------------
_serial_write:
        ; Byte to write is already in A
        sta ACIA_DATA

        ; Fixed delay for transmission
        ; At 9600 baud: ~1042 microseconds per byte
        pha
        lda #11
@tx_delay:
        ldx #95
@tx_inner:
        dex
        bne @tx_inner
        sec
        sbc #1
        bne @tx_delay
        pla

        lda #1                  ; Return success
        ldx #0
        rts

;---------------------------------------
; Check if can write
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
; Flush TX buffer (wait until sent)
; void serial_flush(void)
; Note: Just add delay since TDRE is buggy
;---------------------------------------
_serial_flush:
        ; Add a longer delay to ensure last byte is fully transmitted
        lda #20
@flush_delay:
        ldx #100
@flush_inner:
        dex
        bne @flush_inner
        sec
        sbc #1
        bne @flush_delay
        rts

;---------------------------------------
; IRQ handler (not used yet)
;---------------------------------------
acia_irq_handler:
        rti

;---------------------------------------
; Data
;---------------------------------------
        .rodata
at_reset:       .asciiz "ATZ"
at_echo_off:    .asciiz "ATE0"
