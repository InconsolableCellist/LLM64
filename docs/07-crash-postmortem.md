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

One line, in immediate mode — no program to type, and a typo just gives
`SYNTAX ERROR`:

```basic
FORA=679TO694:PRINTPEEK(A);:NEXT
```

That prints all 16 bytes in table order: magic, idx, crumbs, music, key,
hw_sp, modules, last-module, then the 8-slot ring. The ring is circular,
so read it starting at `idx` and wrapping: entry `(idx + n) mod 8` for
n = 0..7 gives oldest to newest.

**Check the magic first.** If the first number is not 198 the block is
stale or was cleared, and every other number is meaningless.

## The C-stack canary CANNOT be read from BASIC

Do not try. `$AA00-$ADFF` lies under **BASIC ROM** (`$A000-$BFFF`). The
client runs with `$01 = $36`, BASIC banked out, so its stack there is
RAM — but at the READY prompt `$01 = $37`, and `PEEK` returns ROM bytes
instead. A scan looks plausible and is pure fiction: BASIC ROM happens
to contain exactly 24 bytes equal to `$A5` in that range, so a canary
scan reports "1000 of 1024 disturbed" on a machine whose stack never
went near it. That misreading cost real time on 2026-07-20.

Reading it needs the ROM banked out, which BASIC cannot do while
running from that same ROM. Two workable routes:

- **The e2e harness**, which reads RAM directly through the VICE
  monitor. `make test-emu-diag` reports the high-water mark honestly.
- **Have the client measure it**, which is the right long-term fix:
  sample cc65's stack pointer (zero page `$02/$03`) in the IRQ, keep a
  running minimum, and store it *in the `$02A7` block* where `PEEK`
  genuinely works. Not yet implemented.

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
1536 available bytes, and the hardware stack used 45 of 256.

## What the first real capture showed (2026-07-20)

Block read at the READY prompt after a live crash:

```
magic 198   crumbs 41   music 0   key 13 (Return)
hw_sp $DA (37 of 256 bytes used)   modules 9   last '4'
trail: SIDRECV MUSICBEG IMGSHOW IMGCLOSE IMGDONE SIDRECV SIDRECV MUSICBEG
```

The trail ends on `MUSICBEG`: the crash happened while a freshly
transferred tune was playing, moments after its init returned — the
proxy logged the ACK that `main.c` sends *after* `music_ext_begin()`.
The doubled `SIDRECV` matches the proxy's own "SID_BEGIN not ACKed —
resending" from the same second, so two independent sources agree on
the sequence.

No `MODLOAD` anywhere in the trail, so no disk load was in flight: the
music-hold fix in `mod_open` was real hardening but was not this bug.
The hardware stack was nowhere near trouble. See HANDOFF.md for the
working hypothesis — an ACIA read racing the NMI, invisible to the XOR
checksum.
