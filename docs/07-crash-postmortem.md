# Crash post-mortem: reading the diagnostic block

For the open bug where the machine drops to the BASIC prompt while
typing (adventure mode, streamed tune playing, an illustration recently
dismissed). The crash destroys the screen, so the evidence is kept in
RAM that the crash cannot reach.

Build the instrumented client with `DIAG=1`:

```
make -C c64_client clean
make -C c64_client CONNECT=hayes MODE80=1 DIAG=1
```

Without `DIAG=1` every diagnostic call compiles to nothing, so the
shipping build keeps its module-slot headroom (~268 bytes; the DIAG
build leaves ~107). The music-hold fix in `mod_open()` is *not*
conditional — it ships either way.

## What is recorded

A 16-byte block at **`$02A7` (decimal 679)**, in the spare page-2 bytes
that neither BASIC nor the KERNAL uses, plus a canary over the bottom
1K of the C stack (`$AA00–$ADFF`).

| Addr | Dec | Field | Meaning |
|------|-----|-------|---------|
| `$02A7` | 679 | magic | `198` (`$C6`) once `diag_init()` has run |
| `$02A8` | 680 | idx | next slot in the breadcrumb ring |
| `$02A9` | 681 | crumbs | breadcrumbs pushed (wraps at 256) |
| `$02AA` | 682 | music | `music_state`: 0 off, 1–2 pattern tune, 255 streamed SID |
| `$02AB` | 683 | key | last key `handle_key` dispatched |
| `$02AC` | 684 | hw_sp | lowest hardware stack pointer seen in the IRQ |
| `$02AD` | 685 | modules | overlay modules loaded since boot |
| `$02AE` | 686 | last mod | PETSCII digit of the last module (`49`–`52` = `.1`–`.4`) |
| `$02AF`+ | 687+ | trail | 8-deep breadcrumb ring |

Breadcrumb codes (`include/diag.h`). Keystrokes deliberately get **no**
breadcrumb — one per keypress would flush an 8-deep ring within a word,
erasing exactly the module load or image dismiss that explains the
crash. Only rare, dangerous regions are crumbed, and each is bracketed
so a trail ending on the *entry* code pins the crash inside it:

| Code | Name | Meaning |
|------|------|---------|
| 1 | BOOT | `main()` reached |
| 32 | IMGSHOW | fullscreen illustration put up |
| 33 | IMGCLOSE | dismiss started (full 80-col repaint) |
| 34 | IMGDONE | dismiss finished |
| 48 | MODLOAD | music held, RX masked, disk LOAD starting |
| 49 | MODLOADED | LOAD returned, RX + music resumed |
| 50 | MODDONE | module's `run()` returned |
| 64 | MUSICBEG | streamed tune init called |
| 80 | SIDRECV | SID transfer starting |

A trail ending `MODLOAD` with no `MODLOADED` means the machine died
inside the disk LOAD. Ending `IMGCLOSE` with no `IMGDONE` means it died
in the repaint. Both are bracketed for exactly that reason.

## Reading it after a crash

**Do not power-cycle, and do not `RUN` anything first.** Type this at
the `READY.` prompt the crash left behind:

```basic
10 IF PEEK(679)<>198 THEN PRINT "NO DIAG BLOCK":END
20 PRINT "CRUMBS";PEEK(681);"MUSIC";PEEK(682)
30 PRINT "LASTKEY";PEEK(683);"HWSP";PEEK(684)
40 PRINT "MODULES";PEEK(685);"LASTMOD";PEEK(686)
50 I=PEEK(680)
60 PRINT "TRAIL (OLDEST FIRST):"
70 FOR J=0 TO 7:PRINT PEEK(687+((I+J)AND 7));:NEXT J
80 PRINT
90 FOR A=43520 TO 44543
100 IF PEEK(A)<>165 THEN 130
110 NEXT A
120 PRINT "C STACK NEVER BELOW $AE00 - OK":END
130 PRINT "C STACK HIGH WATER";A;"USED";45056-A
```

Line 10 guards the whole thing: if the magic is not 198 the block is
stale or was cleared, and every other number is meaningless.

### The one caveat

The block survives the *likely* crash path — a BRK or a wild jump that
lands in BASIC's warm start, neither of which clears low RAM. It does
**not** survive a hard reset: the KERNAL's RAMTAS routine clears
`$0200–$03FF` on cold start. So if the magic reads anything but 198
after a crash, that itself is information: the machine went through a
full reset rather than falling into BASIC.

## Checking it without crashing

`make test-emu-diag` runs the full soft-80 TUI e2e against a `DIAG=1`
client and reads the block back out — a streamed SID playing while F1
loads overlay modules off disk, which is the crash scenario in
miniature. It asserts the block is populated and reports the C-stack
high-water mark.

Baseline from that run: 13 module loads, streamed SID active, and the
**C-stack canary completely intact** — peak use stayed under 512 of the
1536 available bytes, and the hardware stack used 45 of 256. Stack
exhaustion is therefore an unlikely explanation for the crash, which is
what motivated fixing the disk-LOAD-versus-music timing instead.
