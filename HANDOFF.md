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
  `ssh mlboy "cd c64llm_proxy && ./start-proxy.sh"`.
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
5. **Every multi-frame client receive path runs with streamed music
   silenced** (`music_ext_stop`) — SID play routines' SEI windows blind
   the ACIA. Music may play during *display*, not during *transfer*.
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
   `build/c64llm.map`). Currently ~305 bytes free.
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
- Known failing combo (pre-existing, not a regression): hayes+tui+cols80
  SID `music_state` assert — tcpser real-time pacing at 9600; real HW is
  proven at 19200. Don't chase it as part of unrelated work.

## OPEN BUG — top priority next session

**Crash to BASIC while typing** (reported 2026-07-21, build f694a27):
adventure mode, a few turns in, streamed music playing, an illustration
had recently been displayed and dismissed, user was typing in the editor —
the machine dropped to the BASIC prompt. No unusual key combo.

Leads, in rough priority:
1. **C stack overflow**: stack is $AA00–$AFFF (1.5K). Editor + kb_scan +
   serial NMI + CIA music tick + any deep chain could bottom out; overflow
   walks DOWN into the module slot ($9C00–$A9FF) and, if a module was
   loaded, through its code. A crash to BASIC = BRK/garbage jump fits.
   Cheap diagnostic: write a canary (e.g. $A5) at $AA00–$AA0F at boot,
   check it in the main loop, show a status alert if scribbled.
2. **Image display/dismiss path**: multicolor display toggles VIC
   $D016/$D021 and banks; `img_close()` restores. A rare interrupt during
   the restore window, or a stale `ui_frozen`/redraw interaction with the
   music NMI, could corrupt state. Re-audit img_close + the NMI handler's
   assumptions (the banked-ROM + IRQ interaction is a known haunted area —
   see the scroll-blit history).
3. **Music play routine reentrancy**: the CLI + `in_music` guard in
   serial.s's CIA tick is load-bearing; verify it still holds with the
   current tick ordering and that the played tune wasn't writing outside
   its relocation range (a rogue tune scribbling RAM would also explain it).
   Ask the user WHICH tune/mood was playing if they remember.
Repro attempt: VICE, adventure mode, start music, /pic, dismiss, then type
continuously (hold keys) for a while; also try with the fixture SIDs.
A stack canary + the diag counters is probably the fastest path to a signal.

## Roadmap (in order)

### 1. Menu quick-start entries (USER REQUEST, small, do first)
The new-user flow "new conversation → type `/adventure` or
`/char <name>`" is buried. Add F1 menu entries that do it in one step:
- "Start an adventure" and "Talk to the AI assistant" entries in
  `_menu_entries()` (chat mode at minimum; consider replacing the current
  bare 'a' entry).
- **The wire cmd field is ≤10 chars** — `/char ai assistant` does NOT fit.
  Add short proxy commands, e.g. `/newadv` (new conversation + adventure
  kickoff) and `/assist` (new conversation + roleplay with the shipped
  assistant card). Implement them proxy-side as: finish/persist current
  conversation, reset to a fresh one (reuse the NEW_CONVERSATION path
  internals), then run the existing /adventure or /char logic.
- Ship an "AI Assistant" card. NOTE: `cards/` is gitignored (user's
  private cards) — either force-add the one default card or add a
  `c64llm_proxy/data/default_cards/` directory the card loader also
  searches. Keep the card simple: helpful retro-flavored assistant.
- Zero client bytes needed; menu entries appear on the next F1. e2e:
  extend the tui suite — open menu, hit the new key, assert new-conv +
  mode switch (mock replies already cover adventure).

### 2. Sound window module (overlay #5)
Song name, progress bar, volume (vol_byte), prev/next, favorite
(proxy-side), oscilloscope via $D41B/$D41C reads. Needs Songlengths
durations merged into moods.json for real progress. Use the hook-modal
pattern + OVL5BSS. Slot budget is fine (~3.5KB); resident cost must stay
near zero (headroom ~305 bytes — consider the modules-1-3 OVL-BSS
retrofit first to bank more).

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
