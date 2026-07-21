; Overlay module slot support (SOFT80 builds only).
;
; Emits the 2-byte load-address header at the front of the %O.1 overlay
; file, so LOAD"mod",8,1 / cbm_load(name, 8, NULL) drops the module
; exactly at __OVERLAYSTART__ (same trick as cc65's crt0 LOADADDR).

.ifdef SOFT80

.segment "OVL1ADDR"
        .addr *+2

.segment "OVL2ADDR"
        .addr *+2

.segment "OVL3ADDR"
        .addr *+2

.endif
