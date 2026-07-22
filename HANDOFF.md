# HANDOFF — client performance & baud work

The six-task performance/baud plan in the previous handoff is **done in
software** (commits on `master`, newest first):

- `a286267` Task 3 — chat_append_ascii fast path in asm (SOFT80)
- `e67cfed` Task 4 — colorize_row in asm (SOFT80)
- `0bd60fb` Task 6 — runtime baud config, client + proxy in lockstep
- `197e352` Task 2 — payload CRC sweep in asm
- `c8d3acb` Task 1 — warm F1 menu (re-enter the module in the slot)
- `0df96d9` Task 5 — the NMI / foreground ACIA read race (the crash fix)

`make test-all` is green after each. Everything below is what a real
box still has to confirm, plus the follow-ups that were always going to
outlive this batch.

## Orientation (unchanged)

- Build: `make -C c64_client disk MODE80=1` → `c64_client/build/c64llm.d64`
  (main PRG + overlay modules `c64llm.prg.1..5`, one linked set — never
  mix builds on a disk).
- Tests: `make test-all` (VICE e2e). Run before AND after every change.
  The suite is real-time in places; an occasional single-test timeout
  under heavy machine load is flaky, not a regression — re-run the one
  test standalone to confirm.
- Real hardware: `make deploy-c64u-disk-80` (limit parallel make to
  `-j 4` on the UPS'd box). Deploy and check yourself.
- Diagnostics: status bar shows `hw=` (ACIA overruns), `ov=` (ring
  overflows), `cr=` (CRC failures). `DIAG=1` adds the crash post-mortem
  block (breadcrumbs + C-stack canary, PEEK-able) at ~240 B.
- Memory map (MODE80 DIAG-off): resident BSS now ends ~`$98CD`; overlay
  slot `$9C00-$A9FF`; C stack `$AA00-$AFFF`; SID window `$B000-$BFFF`.
  Free resident headroom: **~819 bytes** (was ~1,040; the four asm files
  and the baud/set-baud code spent the difference and the CRC/append
  loops handed some back). The linker hard-fails past `$9C00`.
- Commits: plain messages, no co-author trailer.

---

## THE GATE: hardware soak of Task 5, before 38400 is exposed

Task 5 (the NMI read race) is the prescribed fix for the open
crash-to-BASIC bug and the prerequisite for 38400. It is in and green in
VICE, but **VICE runs the IRQ path — the whole fix is on the NMI path**,
which only real SwiftLink / C64U hardware exercises (`fg_lock` is inert
under IRQ). So the emulator proves it did not break the IRQ path; it
proves nothing about the NMI path. Someone with the box must:

1. **Default rate soak.** `make deploy-c64u-disk-80`, then a long
   adventure with music streaming + several images. Require
   `hw=0 ov=0 cr=0` and, above all, **no drop to BASIC** — that is the
   bug this was meant to kill (music + a recent picture loaded while
   typing, last seen at f694a27). If it still drops, build `DIAG=1` and
   read the post-mortem block at `$02A7` from BASIC ($AA00 canary is
   shadowed by BASIC ROM and NOT PEEK-able).
2. **38400 soak.** Build `BAUD38400=1` (this both raises the default and
   unlocks 38400 in the F1→E Speed cycle). Set the proxy to its matching
   nominal (`[serial] wire_baud = 19200`, though the client now also
   sends MSG_SET_BAUD so the proxy self-tunes — verify the proxy log
   says "SET_BAUD: pacing bulk to 38400"). Repeat the soak. A swapped
   adjacent byte in a SID payload is the classic signature (order-blind
   XOR still passes it — see the Fletcher note below).

Until both pass on hardware, **38400 stays out of the default cycle**:
a stock build's Speed field only offers 9600 / 19200 (BAUD_IDX_MAX in
`cfg.h` gates it; only `BAUD38400=1` raises the ceiling).

---

## What Task 6 shipped, and the one thing to eyeball on hardware

Runtime baud lives in `c64llm.cfg` (now v2: a `baud_idx` byte appended;
`config_load` still accepts v1 blobs, so every existing disk and the
inline `.cfg` generators keep working). F1 → E has a third field,
**Speed**; cursor to it, any key cycles the hardware rate. On connect the
client sends **MSG_SET_BAUD (0x3E)** — 2 bytes LE, nominal baud/100 — and
the proxy paces bulk transfers to it (`_wire_baud` per session, with
`[serial] wire_baud` as the old-client fallback).

- On hardware, confirm F1 → E → cursor to Speed → key → save → reboot
  brings the link up at the shown rate, and that a changed rate actually
  re-paces the proxy (its log line). Changing baud takes effect on the
  next `acia_init_hw` (reboot / reconnect), not live — by design.
- First-boot flow: delete `c64llm.cfg` from the disk, boot, confirm the
  editor prompts and the saved blob is v2 (magic `C6 02`, 41 bytes).

---

## Follow-ups (not started; roughly in priority order)

- **Fletcher-16 checksum (wire change).** XOR is order-blind, which is
  exactly why a Task-5-class byte swap used to pass CRC and reach the SID
  as corrupt code. With the race fixed the pressure is off, but
  Fletcher-16 is the belt-and-braces: order-sensitive, still a tight
  `adc` loop. It is a **wire change** — `crc.s` (client) and
  `_calculate_crc` (`c64llm_proxy/src/protocol.py`) must deploy together,
  d64 redeploy included. Fold it into `crc.s` if you take it; otherwise
  leave XOR bit-exact (it is, today).
- **Chunked module loader.** `mod_open` still HOLDS music across an IEC
  load because `cbm_load` starves the 60Hz tick (warble). Proper fix is a
  chunked reader that ticks music between chunks (`main.c:531` comment).
  Task 1 already removed the load entirely for a warm F1 re-open, so this
  is less visible than it was, but F5 / first-F1 / config-save still pay.
- **Backlog (from the roadmap memory):** history viewer/search →
  conversation manager → overlay module system → sound window → baud
  doubling (now unblocked once the soak passes) → screensaver/assistant →
  Claude Code mode. Cleanup still owed: 457MB HVSC, demo dir, sidreloc
  home.

## Map of what moved this batch (re-grep before editing)

- `serial.s` — `fg_lock` + `fg_pickup`; both NMI entries and the TX poll
  route through it; `acia_nmi_entry` now shares `drain_sub`; `_acia_ctrl`
  is a live DATA byte.
- `crc.s`, `colorize.s`, `append.s` — new asm; the latter two are
  `.ifdef SOFT80`. `display.c` lost the C bodies (SOFT80) and un-static'd
  `wbuf/wlen/rev_on/view_scroll/matbuf`.
- `loader.c` — `slot_owner` + `module_in_slot`; `main.c` `mod_open`
  short-circuits a warm slot.
- `cfg.c/.h` — v2 blob, `g_baud_idx`, `baud_apply`, `baud_nominal_div100`.
- `mod_config.c` — Speed field. `common.h`/`protocol.[ch]` —
  `MSG_SET_BAUD`, `proto_send_set_baud`. `c64llm_proxy/src/protocol.py` —
  `SET_BAUD` handler + per-session `_wire_baud`. `emu/test_e2e.py` — cfg
  readback now expects the v2 blob.
