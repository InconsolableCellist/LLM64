# Handoff: C64 LLM Client — state, rules, and the road ahead

This document briefs a fresh session (any model) to continue development
without re-learning the hard lessons. Read it fully before touching code.
The user is experienced, direct, and appreciates initiative — but several
rules below were earned through hours of debugging. Do not relax them.

## What this project is

A Commodore 64 talks to modern LLMs through a Python proxy:

```
C64 (or C64 Ultimate) --19200 baud ACIA/SwiftLink modem--> 
  Python proxy (mlboy, port 6400) --HTTPS--> llama.cpp (Gemma) / Claude Code CLI
```

Features shipped and working on real hardware: streaming chat in a soft-80
bitmap TUI, adventure/roleplay modes with LLM-driven SID music (10k-tune
library) and generated multicolor illustrations, persistent conversations,
a disk-loaded overlay module system (config editor, conversation manager,
disk copier, server-fed F1 menu), Claude Code mode (`/code`), and a full
VICE-based e2e test suite.

## Topology and deploy facts

- **Proxy runs on mlboy** (`ssh mlboy`, `~/c64llm_proxy`). Managed by cron
  (@reboot + per-minute idempotent `start-proxy.sh` watchdog), NOT systemd.
  Manual restart: `ssh mlboy "pkill -f 'src.main --host'"` then
  `ssh mlboy "cd c64llm_proxy && ./start-proxy.sh"` — as TWO separate
  invocations; combining them in one `ssh` exits 1 and never starts it.
  Deploy = `rsync -a --delete c64llm_proxy/src/ mlboy:c64llm_proxy/src/` + restart.
- **LLM endpoint**: `https://mlboy.tail99c274.ts.net:5000/v1` — TLS only,
  always this hostname even from mlboy itself. Resident model
  `gemma4-26b-a4b-it-qat-q4-mlboy` is a THINKING fine-tune: requests must
  send `chat_template_kwargs: {enable_thinking: false}` or replies are empty.
- **C64 Ultimate** at 192.168.1.64 (FTP + telnet menu), dials 192.168.1.21:6400.
- **Canonical client distribution = ONE d64**: PRG + overlay modules
  `c64llm.1`–`.4` + `c64llm.cfg`. `make deploy-c64u-disk-80` builds and
  deploys; `emu/u64_telnet.py` mirrors to BOTH `/Temp` (RAM disk, wiped on
  power-off) and `/Flash` (persistent; the user boots from /Flash).
- Fresh d64s carry no cfg (first boot opens the config editor). When
  deploying to the user's machine, inject their cfg first:
  blob = `b'\x00\x10\xc6\x01' + b'192.168.1.21'.ljust(32,b'\0') + b'6400'.ljust(6,b'\0')`,
  then `c1541 build/c64llm.d64 -write user.cfg c64llm.cfg`.
- The user has JiffyDOS; disk speed is acceptable and a stated requirement
  for users generally.
- Proxy restarts while the user is connected are ALLOWED during active dev
  (they said so explicitly). `deploy_and_run` reboots their C64 — fine
  during dev, mention it.
- Batch jobs on the main workstation: cap at `-j 4` (UPS power limit).

## Non-negotiable rules (each one cost hours)

1. **COMMIT FIRST, THEN build the deploy artifact.** The title bar shows
   the git hash ('+' = dirty). A pre-commit build bakes the wrong hash and
   defeats the user's deploy verification. They CHECK this.
2. **Client and proxy deploy in lockstep** whenever the wire format changes.
3. **No proxy frame may exceed client MAX_PAYLOAD (512 bytes)** — split
   long content across frames (role bit 7 = continuation for load frames).
4. **Every multi-frame proxy send must be paced** (`_send_bulk` /
   `_send_bulk_stream`, never bare send loops): the C64U modem silently
   drops burst tails. Every client wait-for-frames state must be covered
   by the response watchdog.
5. **Every timing-critical transfer runs with the tune quiet** — SID
   play routines' SEI windows blind the ACIA, and their cycle cost wrecks
   IEC. Multi-frame *receives* use `music_ext_stop`; the disk LOAD in
   `mod_open` uses `music_hold_begin/end`, which mutes the tick without
   forgetting the song so it resumes mid-bar. Music may play during
   *display*, never during *transfer*.
6. **`serial_rx_pause()` must pair with `serial_rx_resume()` immediately
   after a KERNAL LOAD returns**, BEFORE module code runs. A reply arriving
   while RX is masked dies silently in the ACIA data register — no counter
   ever shows it (this exact bug shifted conversation-list indexes so
   star/delete hit the wrong rows).
7. **The disk-copy module holds RX paused for its WHOLE run** (IEC vs
   serial NMIs) — that one is intentional; the proxy is idle.
8. **Hot paths are asm bulk ops** — cc65 C call chains cost ~1ms/byte
   against a 1.04ms/byte 9600-baud budget. Warp-mode tests hide pacing
   bugs: always run `test-emu-long-rt` after touching stream paths.
9. **BSS accounting (hayes-80 is the binding build)**: CODE/RODATA/DATA/BSS
   growth all eat the module-slot headroom 1:1. After ANY resident change:
   `make -C c64_client clean && make -C c64_client MODE80=1` must link
   (default CONNECT=hayes), then check headroom (BSS end vs $9C00 in
   `build/c64llm.map`). Currently ~492 bytes free (a `DIAG=1` build
   spends ~200 more; that is expected and opt-in).
10. **In c64_client/Makefile**, conditional `+=` blocks must come after
    base assignments, and new rules go AFTER `all:` (first rule = default
    target).

## cc65 / C64 gotchas (will bite a fresh session)

- cc65 string literals map a–z→$41-5A, A–Z→$C1-DA (PETSCII). Disk blobs
  and comparisons must match. `petscii_to_ascii`/`ascii_to_petscii` in
  text.h convert; ASCII *is* the soft-80 cell encoding.
- **Anonymous string literals always land in segment "RODATA"** regardless
  of `#pragma rodata-name` (cc65 2.17). In overlay modules, declare
  strings as named `static const char X[]` arrays inside the pragma region.
- **The OVL4BSS pattern** (see mod_menu.c + c64-soft80.cfg): a
  `type = bss` segment loaded into the overlay memory area puts module
  statics AND `-Cl` static locals into slot RAM past the loaded code —
  zero resident bytes, zero file bytes. CAVEAT: not zero-initialized;
  store before read. Modules 1–3 predate this pattern and still leak
  ~150 resident BSS bytes — retrofitting them is a known reclaim.
- **Never start a comment continuation line with `#N`** — inside a
  *skipped* `#ifdef` block, cc65 2.17 parses it as a preprocessor
  directive (this silently broke the 40col build once).
- `cbm_save` writes a 2-byte PRG header; `cbm_load(name, dev, NULL)` uses
  the file's own header. Hand-built cfg blobs need 2 dummy bytes prepended.
- Drives accept OPEN for missing files and only error on first read —
  detect missing sources by reading before opening the target.
- VICE only auto-enables drive 8; unit 9 needs `-drive9type 1541`.
- VICE monitor reads of $E000 need bank 1 (bank 0 sees KERNAL ROM); CPU
  *reads* of $E000+ fetch ROM while writes hit RAM (bitmap scroll lesson).
- $BA holds the boot device; snapshot at main() start (`boot_device`).
- Reading a function label as data lies (opcode bytes) — use exported
  variables in `build/labels.txt` for monitor probes, never map offsets.

## Architecture crib sheet

- **Client** (`c64_client/src`): main.c (state machine, dispatch, keys),
  display.c (chat ring + soft80 blits), serial.s (NMI ring, rx_masked),
  music.s ($B000 SID window), protocol.c (framing: SYNC 0x42, type, len,
  CRC), cfg.c (NetConfig on disk + boot_device), loader.c (cbm_load),
  modslot.s (overlay load headers), soft80.s (bitmap renderer;
  `soft80_span` renders glyph pairs WITHOUT touching the color matrix —
  that is how the menu panel floats over the chat).
- **Overlay modules** (SOFT80 only, one at a time in slot $9C00, $0E00
  max): `.1` config editor (blocking), `.2` conversation manager
  (hook-modal), `.3` disk copier (blocking, RX paused), `.4` server-fed
  menu (hook-modal). Hook-modal = `mod_modal_begin(msg_hook, key_hook)`;
  the resident loop keeps pumping serial. Modules are BUILD-SPECIFIC:
  always ship PRG + modules together (the d64 enforces this).
- **Slot-overwrite hazard**: module code must NEVER trigger loading
  another module while its own code is on the call stack. The menu sets
  `menu_action`/`menu_pcmd` (modapi.h) and closes; handle_key dispatches
  after the hook returns. Preserve this pattern.
- **Server-fed menu wire**: client sends GET_MENU 0x3B; proxy replies
  MENU_LIST 0x5E: `[count][more]` then `[key][label\0][cmd\0]` per entry.
  Labels ≤26 chars, **commands ≤10 chars** (client cmd[11]), ≤12 entries.
  `!x` commands = client-local actions (n/c/s/x/e/d). Entries built in
  `_menu_entries()` (protocol.py), mode-aware. Menu entry storage reuses
  convs[] (menu and conv manager can never coexist).
- **Proxy** (`c64llm_proxy/src`): protocol.py (the big one: dispatch,
  pacing, media streaming with BEGIN-handshake + windowed flow control),
  modes.py (system prompts incl. adventure [[STATE]] JSON), music.py
  (directive hold-back filter for [[MUSIC/IMAGE/STATE]]), imaging.py
  (nano-banana → C64 multicolor), conversation.py (persistence + meta),
  claude_session.py (/code driver). Handlers that await a client reply
  must NOT run on the reader task — use `_spawn_media()` (deadlock class).
- **Adventure state**: LLM emits `[[STATE: {...}]]`, stripped by the
  filter, normalized into meta `adv_state`, re-injected into the system
  prompt each turn. Old conversations lack it until the LLM first emits
  one, and transcript precedent can suppress the status bar in old
  adventures — known, accepted.

## Testing discipline

- `make test-all` = direct, long, long-rt, hayes, tui, tui-80, watchdog.
  Minimum bar for client changes: tui-80 + tui + hayes + watchdog; add
  long-rt for anything near streaming. Tests use TESTPORT 6464 and never
  bind 6400 (the user keeps a live proxy).
- e2e drives the REAL proxy against `emu/mock_llm.py` (never add a --live
  Claude variant; user said keep the mock).
- Keys fed while a modal shows 'loading…' are silently ignored — wait for
  entry text, not the title (see `open_f1_menu()` in test_e2e.py).
- `wait_for_screen` is case-insensitive; raw `in screen` checks are not.
- Media-transfer completion asserts must poll client state flags via
  labels.txt (e.g. `_img_shown`, `_music_state`), not screen text.
- VICE e2e flakes are almost always autostart-keystroke leftovers or
  warp-vs-wallclock timing, not protocol bugs.
- `make test-emu-diag` = the tui-80 run against a `DIAG=1` client, then
  reads the crash post-mortem block back out (breadcrumb trail + C-stack
  high-water). Use it after touching serial/music/module-load paths.
- Known failing combo (pre-existing, not a regression): hayes+tui+cols80
  SID `music_state` assert — tcpser real-time pacing at 9600; real HW is
  proven at 19200. Don't chase it as part of unrelated work.

## OPEN BUG — crash to BASIC while typing (fix deployed, unconfirmed)

**Symptom** (reported 2026-07-21, build f694a27): adventure mode, a few
turns in, streamed music playing, an illustration recently displayed and
dismissed, user typing in the editor — machine dropped to the BASIC
prompt. No unusual key combo.

**Prime suspect, now fixed (3b089f6).** It probably *was* a keypress.
F1/F2/F3/F5/F7 and the cursor keys are dispatched **unmodified straight
from the typing path** (`handle_key`), and F1-when-idle and F5 both
`cbm_load` an overlay module into $9C00 and then `jsr` into it.
`mod_open()` masked the ACIA for that LOAD but let the 60Hz CIA tick keep
calling the streamed SID's play routine straight through it — up to 1600
cycles per frame stolen from cycle-counted IEC. Both *other*
timing-critical transfers already silenced music first
(`load_conversation_by_id`, `mod_diskcopy.c`); this path did not, and one
stray F-key while a tune plays reaches it. Garbage in the slot → `jsr`
into it → BASIC.

Fixed with `music_hold_begin/end` (music.s): mutes the tick *without*
clearing `music_state`, so the song resumes mid-bar. `music_ext_stop()`
would have worked but costs the user their soundtrack on every F1 — a SID
can only restart from bar one. Costs 30 bytes; headroom 307 → 277.

**Leads now ruled out — do not re-chase:**
- *C-stack overflow* (the original leading theory). Measured under a full
  workload via `make test-emu-diag`: canary **completely intact**, peak
  use under 512 of 1536 bytes, hardware stack 45 of 256.
- *Raw IRQ/NMI vectors not saving Y*: `drain_sub`/`ring_write` are
  deliberately index-register-free (`ring_write` uses self-modifying
  absolute addressing). Correct by design.
- *Banking*: cc65's crt0 does `lda $01 / and #$f8 / ora #$06 / sta $01`
  → `$01 = $36`, BASIC out for the whole run, so the $B000 tune executes
  legitimately. Only soft80's scroll touches `$01`.
- *Rogue tune*: the tune was identified from conversation
  `1784602831.json` — mood "mysterious",
  `MUSICIANS/Z/Zabutom/One_Little_Wish_tune_2`, image shown at msg 1. Its
  relocated image is $B000–$BC30 (inside the window) and it passed
  `sid_reloc_batch` exit-0 (no OOB writes, ZP confined to fb-fe). Weak —
  but note sidreloc verifies over a *bounded* simulation, so late-song
  behaviour is not covered.

**LEADING HYPOTHESIS after the first live capture (2026-07-20).** The
crash was caught with DIAG on: trail ended `... SIDRECV SIDRECV
MUSICBEG`, no `MODLOAD`, hardware stack 37/256. So it fires while a
freshly transferred tune plays — *not* during a disk load, which rules
the music-hold fix out as the cause.

The chain, in one line: **two readers race for `ACIA_DATA`, and the XOR
checksum cannot see the damage.**

- The user's C64U raises the ACIA on **NMI** (confirmed; it is a config
  choice on the unit). `_serial_available`'s stranded-byte pickup guards
  itself with `php`/`sei` — which does **not** mask NMI. If the NMI lands
  between its `lda ACIA_DATA` and its `jsr ring_write`, two adjacent
  bytes enter the ring **reversed**.
- `proto_calc_crc` (protocol.c) is an **XOR checksum**, so it is blind to
  ordering. A transposed pair validates as a good frame.
- Observed directly: the F1 menu once rendered "Back to hcat mode" and
  self-healed. Clean adjacent swap, both bytes intact — a transport fault
  drops or garbles, it does not transpose.
- In chat text that is cosmetic. In a SID payload it is corrupt 6502 code
  landing at $B000, which the client then executes 60×/second.
- **Regression fit**: the user reports it was rock solid before the module
  work. `git log -S'_serial_rx_pause'` returns exactly one commit —
  `9d9983d`, the module system. Before it, stranding a byte needed a full
  ring (rare). After it, every F1/F5 masks RX for a whole disk load, so
  stranding — and the racy pickup — became routine.

**Evidence so far (one run, encouraging, not proof):** switched to IRQ
mode, where `sei` genuinely masks. 3 SID transfers + 1 image over ~11
minutes, no crash. In NMI mode it died on the 3rd SID transfer ~9 minutes
in. Same shape of workload, survived.

**The fix, when confirmed — HARDER THAN IT LOOKS.** `ACIA_DATA` needs a
single owner, but the obvious approaches all have a catch:
- *Delete the mainline pickup.* It is load-bearing. **6502 NMI is
  edge-triggered** while the 6551 asserts its interrupt on a level, so a
  byte that arrives without producing an edge is stranded with no further
  NMI ever coming. The pickup is what rescues it; removing it also broke
  SID transfers in e2e once already.
- *Have the NMI bail out when a flag says mainline is mid-pickup.* Same
  edge-trigger problem — returning without reading `ACIA_DATA` leaves the
  line asserted and serial dies.
- *Reserve the ring slot before reading.* Does not help: the read IS the
  consuming operation, so both readers still race for the same byte.
- *Mask the RX interrupt around the pickup* is the plausible one, but
  LANDMINE: `785131e` gated ACIA command-register writes to real mask
  transitions because the modem re-evaluates DTR/RTS on every write. A
  mask/unmask pair per stranded byte must stay rare or it reintroduces
  that bug.
Then replace XOR with an order-sensitive checksum (Fletcher-16 is cheap
on 6502) so corruption can never again pass as valid. **Wire-format
change — client and proxy must deploy in lockstep.**

**`bank01` — CLOSED (299b3e8).** It was a single non-reentrant `$01`
save slot, but `_soft80_scroll_chat` has one call site and it sits inside
`#if defined(SOFT80) && defined(SCROLL_OPT)`, which nothing defines. The
routine has never run in a shipped client, `bank01` is never written, and
after cc65's startup **nothing in the client touches `$01` at all** — it
runs at `$36` from boot to crash. That also rules banking out as a
mechanism entirely. The dead path is now behind `.ifdef SCROLL_OPT`
(Makefile flag drives C and asm together): 215 bytes reclaimed.

**C-stack — CLOSED (6440b11).** The IRQ now samples cc65's `sp` and keeps
the minimum in the block at `$02B7/8`, where PEEK can reach it. A full
e2e run peaks at **23 bytes of the 1536 reserved** — unsurprising, since
CFLAGS carries `-Cl` (locals are static, so the C stack holds little
beyond parameters). `__STACKSIZE__` is therefore wildly oversized;
cutting it raises `__OVERLAYSTART__` one-for-one and hands the difference
to BSS headroom. Not done: one emulator run is not a proof about every
path on real hardware.

**Do not try to read the C-stack canary from BASIC.** `$AA00` is under
BASIC ROM, so `PEEK` returns ROM, not the RAM the client used — and that
ROM contains exactly 24 `$A5` bytes in the canary range, so a scan
"reports" 1000 of 1024 disturbed on a perfectly healthy machine. Only the
$02A7 block is PEEK-readable (page 2 is always RAM). See
docs/07-crash-postmortem.md; the proper fix is to have the client sample
cc65's `sp` in the IRQ and store the minimum in the block.

**If it recurs**, the machine now keeps evidence. `DIAG=1` builds a
16-byte post-mortem block at `$02A7` (page-2 RAM: outside the linked
image, so it costs no headroom, and it survives a crash to BASIC).
Breadcrumb ring + C-stack canary; read it with the BASIC program in
`docs/07-crash-postmortem.md`. Deploy it with
`make deploy-c64u-disk-80-diag`. Keystrokes deliberately get no
breadcrumb — one per keypress flushes an 8-deep ring within a word.
Caveat documented there: a *hard reset* clears $0200–$03FF via RAMTAS, so
a magic byte that is not 198 after a crash is itself evidence the machine
fully reset rather than fell into BASIC.

## Roadmap (in order)

### 1. Menu quick-start entries — DONE (50c888a, proxy deployed)
`/newadv` turned out to be unnecessary: `_switch_mode()` already opens a
fresh conversation, so `/adventure` (exactly the 10 chars the wire
allows) was always one-step — it just read like a toggle. Relabelled
"Start an adventure". `/assist` added for the assistant, since
`/char Assistant` does not fit the 10-char field.

The bundled card lives in **`c64llm_proxy/src/default_cards/`**, not the
`data/default_cards/` originally suggested: `data/` is gitignored
wholesale, and the deploy rsyncs `src/` only, so anything outside it
never reaches the server. `_all_cards()` merges bundled + user cards with
user cards shadowing bundled ones by name. Menu key is **`i`**, not `t` —
`t` is already "Save checkpoint" in the adventure/roleplay menu, and a
key that means different things per mode would eventually cost someone a
conversation (this one starts a new one).

### 2. Sound window module (overlay #5) — IN PROGRESS
Song name, progress bar, volume (vol_byte), prev/next, favorite
(proxy-side), oscilloscope via $D41B/$D41C reads. Use the hook-modal
pattern + OVL5BSS.

**Durations DONE (e8fbe84, deployed).** `tools/sid_songlengths.py`
parses HVSC `DOCUMENTS/Songlengths.md5` (keyed off the path comment, not
the MD5 — that hash is of the original .sid, which our relocated copies
no longer match); `sid_makedb.py --songlengths` stamps `secs` on each
tune, picking the subtune `start_song` actually selects. moods.json on
mlboy now carries `secs` for all 10,032 tunes, median 103s. Regenerating
was verified byte-comparable to the deployed library apart from the new
field, so tune selection is unchanged.

Remaining: put the duration on the wire (SID_BEGIN grows a field —
**wire change, lockstep deploy**), then the module itself. Headroom is
492 bytes now and OVL5BSS costs zero resident, so the modules-1-3
OVL-BSS retrofit is no longer a prerequisite.

Natural follow-on once the duration is known client-side: MusicLibrary's
`stale()` still hardcodes 300s for the tune-staleness nudge; it could
count actual loops instead.

### 3. Baud doubling (38400)
SwiftLink's doubled crystal makes its "19200" divisor yield 38400; the
C64U emulation honors it. Client: ACIA control value + retune pacing
constants; proxy: wire-time constants. RISK: VICE `-acia1mode 1` hangs
the client at init (open thread) — hardware-speed CI needs that solved,
otherwise validate on real hardware with the diag counters. Halves every
transfer; do it before the screensaver (push traffic).

### 4. Screensaver / always-on assistant
Idle detection client-side; unsolicited server frames already work
(QUIET-stream + hint machinery). CRT-safe visuals (dim/moving), jukebox
integration. Later: Home Assistant bridge.

### 5. Prompt/template editor + color themes
Both proxy-heavy. Themes ≈ 8 bytes/theme proxy-stored; client applies at
draw time.

### 6. Smaller / cleanup
- Retrofit OVL-BSS segments to modules 1–3 (~150 resident bytes back).
- Client DCD carrier-loss detection + redial UX.
- SEI-audit for tunes (py65 trace; flag ACIA-blinding tunes in moods.json).
- Rollback candidate once field-stable: resumable-transfer machinery
  (~500B) — windowing + firmware fixed the real cause.
- Delete 457MB HVSC extraction at c64llm_proxy/data/sids/C64Music, old
  data/sids/b000 demo dir; give sidreloc a durable home (vendor source).
- F4-at-scrollback-top auto-invoking /history (~32 bytes, user must price).
- Endgame demo: point /code's [claude] workdir at this repo so the C64
  rebuilds its own client.

## Field-test threads the user is running

- Real-1541 saga: HD media confirmed unusable (no hub ring, error 66 /
  track 75 garbage); DD floppies on order; head cleaning + white-drive
  bisect pending. Drive-B FPGA replication WORKS.
- Adventure-state quality in a fresh adventure; /pic scene-description
  quality; rainbow pic-ready line — verdicts pending.

## Style notes for the next session

- Commit messages: plain, no Co-Authored-By trailer.
- The user enjoys the retro aesthetic and playful tone ("uwu"), corrects
  firmly when you hedge ("Hang on, cupcake"), and values period-authentic
  choices (disk > streaming). Ask before burning their API quota or
  rebooting things outside active dev flow; inside it, move fast.
- Update the auto-memory files (project state + roadmap) at session end;
  anchor deployed hashes and open threads.
