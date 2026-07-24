;
; LLM64 Client - payload XOR checksum inner loop (assembly)
;
; proto_calc_crc stays in C for the 3 header bytes; only the per-payload
; XOR moves here. cc65 compiled that loop with a stack-relative counter
; and tosxora0/incax1 helper calls - ~100-150 cycles/byte, and it runs
; over every payload byte of every received frame (up to 512 B each).
; This is a plain eor (ptr),y sweep at ~10 cycles/byte, and it competes
; directly with the RX ring for the wire-rate cycle budget.
;
; uint8_t __fastcall__ crc_xor(const uint8_t* buf, uint16_t len);
;   fastcall: len (rightmost arg) arrives in A/X (A=lo, X=hi); buf is on
;   the C stack -> jsr popax (A=lo, X=hi). Returns the XOR of all len
;   bytes in A (X=0). Seeds from 0, which is correct for XOR-accumulate:
;   the C caller does crc ^= crc_xor(payload, length).
;
; len can exceed 255 (MAX_PAYLOAD is 512) so the count is 16-bit; a zero
; length returns 0 without dereferencing buf.
;

        .export _crc_xor
        .import popax
        .importzp ptr1, tmp1, tmp2

        .code

_crc_xor:
        sta tmp1                ; len low
        stx tmp2                ; len high (whole pages to sweep)
        jsr popax               ; buf -> A/X
        sta ptr1
        stx ptr1+1

        ldy #0
        lda #0                  ; running XOR held in A across the sweep
        ldx tmp2
        beq @remainder          ; no full pages
@fullpage:
        eor (ptr1),y
        iny
        bne @fullpage           ; 256 bytes, then Y wraps to 0
        inc ptr1+1
        dex
        bne @fullpage           ; Y is 0 again - continue at the next page
@remainder:
        ldx tmp1                ; 0..255 bytes left; Y is 0 here either way
        beq @done
@rem:
        eor (ptr1),y
        iny
        dex
        bne @rem
@done:
        ldx #0                  ; uint8_t return: high byte clear
        rts
