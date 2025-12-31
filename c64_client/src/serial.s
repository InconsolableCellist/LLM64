;
; C64 LLM Client - ACIA Driver (Assembly)
; 6551 ACIA at $DE00 (SwiftLink compatible)
;

        .export _serial_init
        .export _serial_disconnect
        .export _serial_is_connected
        .export _serial_available
        .export _serial_read
        .export _serial_write
        .export _serial_can_write
        .export _serial_flush
        .export _acia_init_hw
        .export _acia_send_at_command
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
; Initialize ACIA hardware
; void acia_init_hw(void)
;---------------------------------------
_acia_init_hw:
        ; Reset ACIA
        lda #$00
        sta ACIA_STATUS         ; Software reset

        ; Set control register (9600 baud, 8N1)
        lda #$1E                ; 9600 baud, 8 data bits, 1 stop
        sta ACIA_CONTROL

        ; Set command register (no interrupts for now)
        lda #$09                ; No parity, no echo, RTS low, no TX IRQ, no RX IRQ, DTR low
        sta ACIA_COMMAND

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
;---------------------------------------
send_byte:
        pha
@wait_tx:
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        beq @wait_tx
        pla
        sta ACIA_DATA
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

        ; For now, assume connection works
        ; In full version, we'd send ATDT<host>:<port>
        lda #1
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
; Write one byte (non-blocking)
; uint8_t serial_write(uint8_t byte)
;---------------------------------------
_serial_write:
        ; Check if TX ready
        pha
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        beq @tx_full

        ; Write byte
        pla
        sta ACIA_DATA
        lda #1                  ; Success
        ldx #0
        rts

@tx_full:
        pla
        lda #0                  ; Failed
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
;---------------------------------------
_serial_flush:
@wait:
        lda ACIA_STATUS
        and #ACIA_SR_TDRE
        beq @wait
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
