# Hayes + MODE80 build overflows BSS (blocks real-hardware deploy)

**Status:** FIXED 2026-07-24 — Option 3 (both knobs), see *Resolution*
at the bottom. Kept for the analysis and the measurement method.

**Introduced by:** `ef94001` (*Printer: /print hardcopy to IEC device 4*),
confirmed by bisect: the commit before it (`0755c54`) links with ~306 B
of BSS headroom in the Hayes+MODE80 config; `ef94001` overflows by 503 B.
The one commit after it (the LLM64 rename) *reduced* the overflow to
493 B. Post-mortem note: the printer commit added only ~29 B of actual
BSS — the pressure is ~780 B of new resident CODE/RODATA pushing BSS's
start address up (the client print path reuses `payload_buffer`; there
are no big new buffers).

## Symptom

```
$ make -C c64_client CONNECT=hayes SERVER_IP=192.168.1.21 MODE80=1
...
ld65: Warning: c64-soft80.cfg(24): Segment `BSS' overflows memory area `BSS' by 492 bytes
ld65: Error: Cannot generate most of the files due to memory area overflow
```

This is exactly the configuration every hardware deploy uses:
`deploy-c64u-80`, `deploy-c64u-disk-80`, `deploy-c64u-disk-80-free`,
`deploy-c64u-disk-80-diag` all build `CONNECT=hayes MODE80=1`.

## Why the test suite is green anyway

The whole automated suite builds `CONNECT=direct` (see the `test-emu*`
targets in the top-level `Makefile`). `direct` mode compiles out the
Hayes AT-dial state machine and its buffers via `-DCONNECT_DIRECT`, so
the emulator client is ~900 bytes of BSS lighter and links with room to
spare. `make test-all` therefore cannot see this class of regression.

Measured, with `CONNECT=direct MODE80=1` (a build that DOES link):

| Segment | Range | Notes |
|---|---|---|
| BSS top | `$57AA … $9A6A` | ends **406 bytes** below the cap |
| BSS cap | `$9C00` | `= __OVERLAYSTART__` |

The Hayes build adds ~898 bytes of BSS on top of the `direct` build:
406 bytes of headroom consumed, then 492 bytes over the cap.

## The memory map (soft-80 build, `c64_client/c64-soft80.cfg`)

BSS is squeezed between the resident program below it and the overlay
module slot above it. From low to high:

```
  ... CODE / RODATA / DATA / ONCE / BSS ...
  $9C00  __OVERLAYSTART__   <- BSS must end below here
         [ overlay module slot, __OVERLAYSIZE__ = $0E00 = 3584 B ]
  $AA00  = __HIMEM__ - __STACKSIZE__
         [ C stack, __STACKSIZE__ = $0600 = 1536 B, grows down from $B000 ]
  $B000  __HIMEM__          (streamed-SID window $B000-$BFFF above this)
```

The relevant symbols (`c64-soft80.cfg`, SYMBOLS block):

```
__STACKSIZE__:    $0600   (1536 B)
__OVERLAYSIZE__:  $0E00   (3584 B)
__OVERLAYSTART__: __HIMEM__ - __STACKSIZE__ - __OVERLAYSIZE__ = $9C00
```

BSS's memory area is `__ONCE_RUN__ … __OVERLAYSTART__`. So **raising
`__OVERLAYSTART__` is the only way to give BSS more room**, and
`__OVERLAYSTART__` moves up if *either* `__STACKSIZE__` or
`__OVERLAYSIZE__` shrinks. The config comment already names the second
one: *"BSS is capped below the slot; if it overflows, shrink
`__OVERLAYSIZE__`."*

We need to free **≥ 492 bytes**. Both knobs below free 512 (a clean
`$0200`), which leaves BSS ~20 bytes of slack.

## Measured slack in each knob (so you can pick safely)

**Overlay slot** — the slot is `__OVERLAYSIZE__` = 3584 B; the largest
module has to fit inside it. From the linking `direct MODE80` build:

| Module | code+bss end (offset from slot start) | of 3584 B |
|---|---|---|
| mod_sound (`.5`) | `$A7B2` → **2994 B** | 590 B free |
| mod_menu (`.4`) | `$A416` → 2582 B | |
| mod_diskcopy (`.3`) | `$A21D` | |
| mod_config (`.1`) | `$A1F6` | |
| mod_convmgr (`.2`) | `$A0B5` | |

Shrinking the slot to `$0C00` (3072 B) leaves the biggest module (2994 B)
**78 bytes** of slack. Tight but real; if any module grows it overflows
its own slot instead of BSS.

**C stack** — `__STACKSIZE__` = 1536 B. `make test-emu-diag` reads the
crash post-mortem block back after a full session (streamed SID + overlay
loads + print path + adventure) and reports the high-water mark:

```
PASS: C-stack low-water $AFE9 (23 of 1536 bytes used)   <- idle sample
PASS: C-stack canary intact - peak use stayed under 512 of 1536 bytes
```

So the real peak is **under 512 B**. Cutting the stack to `$0400`
(1024 B) still leaves ~2× the measured peak. This is the safer of the
two knobs — the margin it spends is margin we have proof is unused,
whereas the slot margin protects against future module growth.

## Options

Pick one; all three free ≥ 492 B and re-link the Hayes build.

1. **Shrink the C stack** — `__STACKSIZE__: $0600 → $0400`.
   Frees 512 B → BSS links with ~20 B to spare. Stack 1024 B vs measured
   peak < 512 B (2× margin). Overlay slot untouched (keeps 590 B). *This
   is the lowest-risk option: it spends margin we have measured as idle.*

2. **Shrink the overlay slot** — `__OVERLAYSIZE__: $0E00 → $0C00`.
   The remedy the config comment names. Frees 512 B → BSS links. Leaves
   only 78 B above the biggest module, so the next module edit could
   overflow its slot.

3. **Take 512 B from each** — both of the above.
   Frees 1024 B → ~532 B of genuine BSS headroom for the next resident
   feature, at the cost of thinning both other margins as described.

Any of these is a one-line edit in `c64_client/c64-soft80.cfg`
(the `SYMBOLS` block). `intro.cfg` is separate and unaffected.

A fourth path — leave the layout alone and shrink the printer/Hayes
resident footprint back down by ~500 B — is possible but larger surgery;
the printer code in `main.c` and its buffers in `common.h` would need an
audit. Not worth it unless all three margins above are genuinely spoken
for.

## How to verify a fix

```bash
# 1. It must LINK in the hardware config (this is the failing case):
make -C c64_client clean
make -C c64_client CONNECT=hayes SERVER_IP=192.168.1.21 MODE80=1
#   -> expect "Build complete: build/llm64.prg", no BSS overflow

# 2. If you touched __OVERLAYSIZE__, prove no module overflows its slot:
#    the build above emits build/llm64.prg.1..5; a module that no longer
#    fits fails at link with an OVL* overflow. Also re-run the C-stack
#    check if you touched __STACKSIZE__:
make test-emu-diag        # re-reads the stack high-water mark

# 3. Full regression (still CONNECT=direct, but proves nothing else broke):
DISPLAY=:0 make test-all

# 4. Then the deploy that is currently blocked:
make deploy-c64u-disk-80-free    # free disk (intro chain-loads the client)
make deploy-c64u-disk-80         # registered disk, machine ends up here
```

## Resolution (2026-07-24)

Option 3, plus the canary/harness updates the options above did not
account for — shrinking `__STACKSIZE__` is NOT a one-line edit:

- `c64-soft80.cfg`: `__STACKSIZE__ $0600 → $0400`,
  `__OVERLAYSIZE__ $0E00 → $0C00`. New layout: BSS cap
  `__OVERLAYSTART__ = $A000`, slot `$A000-$ABFF`, stack `$AC00-$AFFF`.
- `include/diag.inc`: `CAN_END = CAN_START + $0300` (was `+ $0400` —
  with a 1K stack the old value would have filled the ENTIRE stack and
  scribbled diag_init's callers' live frames).
- `emu/test_e2e.py`: `CAN_START`/`CAN_END` mirror constants (they
  hard-code the stack geometry; without this, `test-emu-diag` reads the
  top of the overlay slot as canary and fails spuriously).
- Stale address comments in `diag.h`, `diag.s`, `loader.c`.

Measured after the fix (Hayes+MODE80, the config that was failing):
BSS ends `$9DEC` → **531 B headroom**; biggest module (mod_sound
code+bss) ends `$ABB2` → 77 B slot slack; `test-emu-diag` reports peak
C-stack use **under 256 of 1024 B** (canary at `$AF00` untouched), so
the 1K stack holds 4× the observed peak.

## Deploy state at the time this was written (2026-07-23)

- Rename C64LLM → LLM64 is committed (`0f37b08`).
- The proxy is already redeployed and live on mlboy from
  `~/llm64_proxy` (crontab keepalive + start-proxy.sh updated; banner in
  the log reads "LLM64 Proxy Server v0.1.0").
- The C64 Ultimate still has the OLD disks in `/Flash`
  (`c64llm.d64`, `c64llm-free.d64`, `C64LLM.PRG`) — **not** replaced,
  because the new client will not link for hardware until this is fixed.
