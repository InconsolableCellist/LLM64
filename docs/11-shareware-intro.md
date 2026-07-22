# 11 — Shareware intro (free vs. registered disk)

Status: IMPLEMENTED (see §11 for where reality differed from this spec).
Written to be executed in a fresh session; read it fully first, every
section is load-bearing. Two of its original claims were wrong and are
corrected inline below — §11 says why, so the same mistakes do not get
"fixed" back in.

## 1. What we are building

Two distribution disks from one codebase:

- **Registered disk** — `build/c64llm.d64`, exactly as today. First file
  is the client PRG; the Ultimate's Run Disk / `LOAD"*",8,1` boots
  straight into the app. **Do not change this disk or the resident
  client in any way.**
- **Free disk** — `build/c64llm-free.d64`. Identical contents PLUS a
  standalone intro PRG as the FIRST file on the disk. Booting the disk
  runs the intro: a multicolor picture (made with the same pipeline as
  adventure-mode illustrations), SID music, a short pitch for C64 LLM,
  the support links (foxipso.com and patreon.com/c/foxipso), a nag
  countdown, then "press any key". On keypress the intro loads the real
  client from the same disk and jumps into it. From there everything is
  exactly the stock experience.

The ONLY difference between free and registered is the intro screen.
Nothing in the resident client knows or cares which disk it booted from.

### Why this shape (design rationale — do not redesign)

- **Zero resident bytes.** The client's module-slot headroom is ~300-1500
  bytes depending on build and every resident byte is contested
  (HANDOFF.md rule 9). A standalone intro PRG that is overwritten by the
  main LOAD costs the client nothing. Any design that puts free/paid
  logic *inside* the client is wrong for this codebase.
- **No lockstep.** No wire-format change, no proxy change, no module
  change. The intro never touches the ACIA. It can ship, be rebuilt, or
  be deleted independently.
- **Period-authentic.** A crack-intro-style bootstrap that chain-loads
  the payload is exactly how 1980s shareware and scene releases worked.

## 2. Deliverables checklist

1. `c64_client/intro/` — new directory: `intro.s` (ca65 assembly),
   `intro.cfg` (ld65 config), `Makefile`, `assets/` (committed binary
   assets + the script-readable metadata that generated them).
2. `tools/make_intro_assets.py` — converts a source PNG + a relocated
   `.sid` into `intro_data.bin` and the generated `intro_gen.inc`
   (addresses/constants) that `intro.s` includes.
3. `c64_client/Makefile` — new `disk-free` target (added AFTER the
   `all:` rule, per HANDOFF.md rule 10).
4. Root `Makefile` — new `deploy-c64u-disk-80-free` target.
5. A committed image asset and a committed tune asset (see §5).
6. Emulator smoke test (§9) passing; existing `make test-all` still
   passing untouched (you did not modify anything it covers, so a
   failure means you strayed).

## 3. Boot flow and chain-load contract

```
LOAD"*",8,1 / C64U "Run Disk"
  └─ intro code PRG "c64 llm" (first file on free disk, stub at $0801)
       ├─ prints LOADING..., KERNAL-LOADs data blob "c64 llm.d" → $4000
       ├─ copies assets into place (§4), starts raster IRQ (music+split)
       ├─ shows picture + text + countdown
       ├─ waits for any key (armed only after countdown)
       └─ on key: silence SID, restore KERNAL IRQ, copy 30-byte stub
          to $0334, JMP $0334
            └─ stub: KERNAL LOAD "C64LLM",dev,1  (overwrites the intro
               at $0801 — we are executing from $0334, that is fine)
                 └─ JMP $080D  (cc65's BASIC header is SYS 2061=$080D;
                    this is stable across builds)
```

Facts the stub must respect:

- **Device number**: read from `$BA` at intro start and reuse it (the
  client does the same — `boot_device` in cfg.c). Do not hardcode 8.
- **Filenames**: the client PRG is written to disk as `c64llm`. In a
  KERNAL SETNAM call the matching bytes are PETSCII `$43 $36 $34 $4C
  $4C $4D`, and those are what `ca65 -t c64` emits for the **lowercase**
  string `"c64llm"`. The c64 target's charmap maps ASCII `a-z` → `$41-5A`
  and `A-Z` → `$C1-DA` (the same mapping HANDOFF.md records for cc65
  string literals), so uppercase `"C64LLM"` in the source assembles to
  `$C3 $36 $34 $CC $CC $CD` and the LOAD fails. Verified with ca65 2.17;
  an earlier revision of this doc had it exactly backwards. Same rule for
  the intro's own data blob: c1541 name `c64 llm.d` ↔ ca65 `"c64 llm.d"`.
- **Secondary address 1** (load to the file's own address, $0801).
- **Banking**: the intro runs at `$01 = $36` — BASIC out, KERNAL and I/O
  in — and restores `$37` immediately before the chain LOAD. It must not
  stay at the boot default `$37`, because that banks the BASIC ROM over
  `$A000-$BFFF` and `jsr SID_PLAY` at `$B003` would execute ROM instead
  of the relocated tune. `$36` is what the client itself runs at
  (HANDOFF.md, "Banking"); KERNAL LOAD, CHROUT and the `$EA31` IRQ chain
  all work there. This is the only `$01` write in the program.
- **Before the LOAD**: `SEI`, write `$D418 = 0` (SID off), restore the
  IRQ vector `$0314/$0315` to `$EA31`, set `$D01A = 0` (raster IRQ off),
  ack `$D019`, re-enable the CIA1 timer IRQ (`$DC0D = $81`), restore
  `$D011/$D016/$D018` to power-on text-mode values (`$1B / $C8 / $15`),
  and put `$01` back to `$37`, `CLI`. `$DD00` needs no restoring: the
  intro never writes it, bank 0 being the power-on default.
  Music MUST be stopped before the LOAD, not held: IEC transfers starve
  the tick and produce audible warble (HANDOFF.md §3c5) — a clean cut
  to silence is the correct behavior here.
- **Clear the keyboard buffer** (`$C6 = 0`) before jumping, so the key
  that dismissed the intro does not leak into the client's editor.
- **Failure path**: if LOAD returns carry set, print `LOAD ERROR` via
  `$FFD2` and `JMP` to itself (halt). Do not try to be clever; a bad
  disk is a bad disk.

The chained client behaves exactly as a directly-booted one: fresh disk
with no `c64llm.cfg` → the config editor module opens, etc. No client
change is needed and none is permitted.

## 4. Intro program: memory map and structure

Pure ca65 assembly, ONE source file (`intro.s`), custom ld65 config.
Do not use cc65 C here — the program is a static screen with an IRQ,
and C would only add a runtime to fight with.

Everything runs in VIC bank 0 ($0000–$3FFF), the power-on bank — no
`$DD00` juggling.

The intro is TWO files on disk:

- **`c64 llm`** — the code PRG. Loads at $0801 (BASIC stub, `SYS`
  entry), runs immediately. Contains all logic plus the text strings.
  HARD CONSTRAINT: it must end below **$0C00** (~1K of room), because
  $0C00 becomes the text screen. A static intro in assembly fits with
  room to spare; if it ever doesn't, move logic, not the screen.
- **`c64 llm.d`** — one concatenated data blob with a $4000 load
  address, fetched by the intro itself via KERNAL LOAD (secondary
  address 1): bitmap 8000 + screen 1000 + colram 1000 + tune
  (≤4096), ~14K at $4000–~$77FF (offsets in §5.3). Print `LOADING...` via `$FFD2`
  before this LOAD — on a stock 1541 it takes ~45s and a silent black
  screen would read as a hang (JiffyDOS: a few seconds).

Why two files: every copy destination ($0400, $0C00, $2000, $B000,
$D800) is comfortably clear of both the code PRG and the $4000 blob,
so init is five dumb non-overlapping copies. A single self-contained
PRG at $0801 would have its asset bytes sitting ON TOP of the $0C00
and $2000 destinations, forcing order-sensitive overlapping moves —
an entire bug class for zero benefit. Bonus: the SETNAM/SETLFS/LOAD
subroutine written for the data blob is the same code the exit stub
needs; write it once.

Runtime layout after init's copies:

| Range        | Contents                                               |
|--------------|--------------------------------------------------------|
| $0334–$03FF  | chain-load stub (copied here at exit; cassette buffer) |
| $0400–$07E7  | bitmap screen RAM (fg/bg nibble pairs)                 |
| $0801–<$0C00 | intro code PRG (stub, init, IRQs, text strings)        |
| $0C00–$0FE7  | text screen for the bottom five rows (rendered by init)|
| $2000–$3F3F  | multicolor bitmap (copied from $4000)                  |
| $4000–~$77FF | the loaded data blob (source of all copies)            |
| $B000–$BFFF  | the relocated SID tune (copied from the blob)          |
| $D800–$DBE7  | color RAM (copied from the blob, then rows 20–24       |
|              | overwritten with the text color)                       |

Init order: LOAD blob → copy tune → copy colram → copy screen → copy
bitmap → render text rows into $0C00 → set text color RAM rows →
`SID init` call → install IRQ vectors → enable raster IRQ.

Other notes:

- The tune comes from the project's own relocated library and therefore
  lives at $B000 with ZP confined to $FB–$FE and verified no-OOB writes
  (§5.2). Nothing else in the intro occupies $B000+, so no relocation
  work is needed and the KERNAL/BASIC zero page stays intact for the
  LOAD.

### 4.1 Screen layout (raster split)

Top: rows 0–19 of the multicolor bitmap (the picture).
Bottom: rows 20–24 in TEXT mode (the pitch + links + countdown).

This is the classic split-screen intro trick and costs ~40 lines of
assembly. Two raster IRQs per frame via `$0314` vector, `$D01A = 1`:

- **IRQ A at line $2D** (just above the visible area):
  `$D011 = $3B` (bitmap on), `$D016 = $D8` (multicolor + 40 col),
  `$D018 = $18` (screen $0400, bitmap $2000), `$D021 = PIC_BG`. Then call the tune's play address once, then set up IRQ
  B's raster line and `JMP $EA31` (keyboard scan + jiffy clock keep
  running — the countdown and GETIN depend on this).
- **IRQ B at line ~$D2** (visible area starts at raster line $33; row
  20 begins at $33 + 20*8 = $D3 — tune the exact line in VICE until
  the seam sits between the picture and the text rows): `$D011 = $1B`
  (text mode), `$D016 = $C8` (multicolor off), `$D018 = $36` (text
  screen $0C00, lowercase charset ROM at $1800). Set up IRQ A's line,
  ack `$D019`, then `JMP $EA81` (pulls the registers the KERNAL entry
  pushed, then RTI — a bare RTI from a `$0314` handler corrupts A/X/Y).
  Music and the `$EA31` chain happen only in IRQ A.
- Only bitmap rows 0–19 (160 of 200 lines) are ever visible — compose
  or crop the source picture so nothing important sits in the bottom
  fifth, or letterbox to 160×160 before conversion.
- A one-to-two line flicker at the seam is acceptable for v1. If it
  offends, the standard fix is switching registers during the horizontal
  border (busy-wait on `$D012` change then a few NOPs) — polish, not
  required.

The TEXT screen lives at **$0C00**: init renders it with a small loop
from a string table in the code PRG (screen codes, not PETSCII —
lowercase charset screen codes are a–z=1–26, A–Z=65–90, digits/
punctuation as ASCII; verify against `font48.s`-era habits in VICE
rather than trusting a table from memory). Text color: set color RAM
$D800+ for rows 20–24 during init — order matters: copy the image's
1000 colram bytes first, then overwrite the last 5 rows, i.e. offsets
800–999, with the text color.

IMPORTANT — screen RAM conflict: the bitmap's color pairs occupy
$0400 (used by IRQ A via `$D018=$18`), the text occupies $0C00 (used by
IRQ B via `$D018=$36`). They are different screens on purpose; do not
try to share one.

### 4.2 Text content (bottom five rows, 40 columns)

Row 0 of the text area: centered title. Rows 1–2: pitch. Row 3: links.
Row 4: countdown / prompt. Suggested copy (edit freely for fit, keep
the URLs exact):

```
        C64 LLM  *  evaluation copy
 chat with an AI, play D&D adventures
 with live SID music and AI pictures.
  foxipso.com | patreon.com/c/foxipso
     please wait 10 seconds... 
```

Row 4 counts down `10 ... 9 ... 8` (rewrite the two digit cells each
second), then becomes:

```
  press any key - registered skips this
```

(Mind the 40-column budget on every row; count characters, the
assembler will not do it for you.)

That last parenthetical is the entire monetization pitch of the nag —
keep it friendly, not scolding. `NAG_SECONDS = 10` is a single `.define`
at the top of `intro.s`; the user may later prefer 5.

### 4.3 Countdown and key wait

- During countdown: watch the seconds counter, redraw the digits; **drain
  and discard** the keyboard buffer (`GETIN` / `$FFE4` in the main
  loop) so holding a key early does not skip. Seconds come from a frame
  counter in IRQ A, not the jiffy clock: the intro turns the CIA1 timer
  IRQ off (`$DC0D = $7F`) so the raster IRQ is the only one running, and
  the jiffy clock then advances at the raster rate rather than 60Hz.
  One PAL second = 50 IRQ-A calls; on NTSC the nag runs ~20% short,
  which is harmless.
- After countdown: swap row 4's text, then loop on `GETIN` until any
  nonzero key arrives (STOP is not special; it is just a key here).
- Music plays the whole time, through countdown and wait — it stops
  only at the moment of chaining (§3).

## 5. Assets

Committed to git under `c64_client/intro/assets/` so the build never
needs PIL, the network, or mlboy. The generator script and its inputs
are committed too so assets are reproducible.

### 5.1 Picture

Produced with the SAME converter the adventure pipeline uses:
`c64llm_proxy/src/imaging.py::convert_to_c64_mc(img)` → returns
`(bitmap 8000, screen 1000, colram 1000, bg byte)`. The first three go
into the data blob in that order; `bg` becomes the `PIC_BG` constant
in `intro_gen.inc`.

Source image options, in order of preference:

1. **Generate one with the adventure image backend** (nano-banana /
   ComfyUI via the proxy's imaging path) — one-off, done by the human
   or by you WITH THE USER'S GO-AHEAD (API quota is theirs; ask first,
   per project convention). Prompt suggestion: *"Retro 1980s airbrushed
   box-art of a Commodore 64 on a desk, its screen glowing, speech
   bubble of text connecting to a friendly robot; bold 'C64 LLM' logo
   across the top; vivid colors, dramatic lighting."* Iterate with
   `render_preview_mc` until it reads well at 160×200.
2. Any existing art/PNG the user supplies, run through the same
   function (see `c64llm_proxy/tools/img2c64.py` for the hires version
   of exactly this wrapper — write the small `--mc` variant or extend
   that script).

Commit the source PNG alongside the generated blob, plus the preview
PNG from `render_preview_mc` so reviewers can see what shipped.

### 5.2 Music

Take a tune from the project's verified relocated library — these are
already relocated to `$B000`, ZP-confined to `$FB–$FE`, checked for
out-of-bounds writes, and loudness-measured. Do NOT grab a raw HVSC
file; unverified tunes clobber arbitrary ZP and would break the KERNAL
LOAD the intro depends on.

- Metadata: `c64llm_proxy/data/sids/moods.json` — each entry has
  `file`, `load` (45056 = $B000), `init`, `play`, `secs`, `rms_db`,
  moods. Pick something `triumphant`/`heroic`/`playful` with high
  `confidence`, `size` ≤ 4096, and `secs` ≥ 60 (or any length — SIDs
  loop). The full `b000_full/` library lives on mlboy
  (`scp mlboy:c64llm_proxy/data/sids/b000_full/<file> .`); a nine-tune
  sample sits locally in `c64llm_proxy/data/sids/b000/` and is fine for
  development. **Ask the user for the final tune pick** (matter of
  taste + HVSC licensing note below), but wire everything with a
  placeholder from the local nine first.
- `tools/make_intro_assets.py` strips the PSID header exactly as
  `music.py::payload()` does (data offset at header bytes 6–7 big-
  endian; skip a 2-byte embedded load address if header bytes 8–9 are
  zero), appends the memory image to the data blob, and emits
  `init`/`play` addresses into `intro_gen.inc`.
- Call `init` once at startup with A = start_song−1 (X=Y=0), then
  `play` once per frame from IRQ A. PAL runs it at 50Hz vs the client's
  60Hz tick — tempo will be a touch slower than in-app; accepted.
- Volume: most tunes write `$D418` themselves; if the chosen tune is
  quiet/loud, `moods.json`'s `rms_db` and the client's `music_ext_vol`
  mechanism show how the proxy normalizes — for the intro, picking a
  tune with `rms_db` near the library median is simpler than porting
  the override.
- **Licensing note for the human**: HVSC tunes are fan-preserved,
  rights sit with composers. For a disk that asks for money, prefer a
  tune whose author permits use, or long-term replace with a
  commissioned/original tune. Flag this in the PR description; do not
  silently ship. (The in-app streaming case is the user's existing
  editorial call; the intro bakes a copy into the distributed image,
  which is a different posture.)

### 5.3 Generator script contract

`tools/make_intro_assets.py <source.png> <tune.sid> -o c64_client/intro/assets/`
writes:

- `intro_data.bin` — the complete `c64 llm.d` file: 2-byte load
  address ($00 $40), then bitmap 8000 + screen 1000 + colram 1000 +
  tune memory image. Fixed offsets, so the intro's copy loops use
  constants: bitmap at $4000, screen $5F40, colram $6328, tune $6710.
- `intro_gen.inc` (ca65 include: `SID_INIT`, `SID_PLAY`, `SID_SONG`,
  `SID_SIZE`, `PIC_BG` constants)
- `intro_preview.png`

It imports `convert_to_c64_mc`/`render_preview_mc` from
`c64llm_proxy/src/imaging.py` the same way `img2c64.py` does
(`sys.path` insert). The Makefile copies `intro_data.bin` into
`build/` for c1541; `intro.s` includes `intro_gen.inc` only — no
`.incbin` of assets into the code PRG.

## 6. Build integration

### 6.1 `c64_client/intro/Makefile` (self-contained)

```make
CC65_HOME = /usr/share/cc65
BUILD = ../build
all: $(BUILD)/intro.prg $(BUILD)/intro_data.bin
$(BUILD)/intro.prg: intro.s intro.cfg assets/intro_gen.inc
	CC65_HOME=$(CC65_HOME) ca65 -t c64 -I assets intro.s -o $(BUILD)/intro.o
	CC65_HOME=$(CC65_HOME) ld65 -C intro.cfg -o $@ $(BUILD)/intro.o
$(BUILD)/intro_data.bin: assets/intro_data.bin
	cp $< $@
```

(Adjust to taste; the point is it shares `build/` and touches nothing
in the parent Makefile's variable soup.)

### 6.2 `c64_client/Makefile` — free disk

Add AFTER the existing `all:` and `disk:` rules (rule 10: first rule =
default target; conditional blocks after base assignments):

```make
# Free (shareware) distribution disk: intro first, then the standard
# contents. Registered disk ($(D64)) is untouched.
D64FREE = $(TARGETDIR)/c64llm-free.d64
INTRO = $(TARGETDIR)/intro.prg
INTRODATA = $(TARGETDIR)/intro_data.bin

$(INTRO): FORCE
	$(MAKE) -C intro

disk-free: $(D64FREE)
$(D64FREE): $(PRG) $(INTRO)
	@test -f $(MOD1) || { echo "No $(MOD1) - build with MODE80=1"; exit 1; }
	@$(VICE_RUN) c1541 -format "c64llm free,01" d64 $(D64FREE) \
	       -write $(INTRO) "c64 llm" \
	       -write $(PRG) "c64llm" \
	       -write $(INTRODATA) "c64 llm.d" \
	       -write $(MOD1) "c64llm.1" \
	       -write $(MOD2) "c64llm.2" \
	       -write $(MOD3) "c64llm.3" \
	       -write $(MOD4) "c64llm.4" \
	       -write $(MOD5) "c64llm.5" >/dev/null
	@echo "Free disk image: $(D64FREE)"
```

The intro's directory name is `c64 llm` (with a space) so it cannot
collide with the client's `c64llm` that the stub loads by exact name.
Write order puts it first, which is all `LOAD"*"` and Run Disk care
about. Add `disk-free` to `.PHONY` and `$(D64FREE)` to `clean`.

### 6.3 Root `Makefile`

```make
deploy-c64u-disk-80-free:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) MODE80=1
	$(MAKE) -C c64_client disk-free
	$(PYTHON) emu/u64_telnet.py c64_client/build/c64llm-free.d64
```

**Rule 1 still applies**: commit FIRST, then build the deploy artifact —
the client PRG inside the free disk carries the title-bar hash and the
user checks it.

## 7. What NOT to do (each of these looks tempting and is wrong)

- Do not add a `FREE=1` flag to the client build or any free/paid
  `#ifdef` in resident code. Zero resident bytes is the design.
- Do not touch `$01` in the intro; do not touch the ACIA.
- Do not keep music playing across the chain LOAD (§3c5 warble).
- Do not scatter assets across per-asset disk files — exactly one data
  blob (`c64 llm.d`), loaded once. And do not fold the assets into the
  code PRG either: their file bytes would sit on top of the $0C00/$2000
  copy destinations (§4).
- Do not use a raw HVSC .sid (§5.2).
- Do not put anything the stub needs above $0801 — the LOAD overwrites
  the entire intro image; the stub at $0334 must be self-contained
  (its own SETNAM string bytes included, copied along with it or
  placed in the $0334 block).
- Do not change `disk:`/`$(D64)` or any existing target; another
  session may be working in this repo concurrently.

## 8. Testing

1. **Emulator, free disk**: `make -C c64_client clean && make -C
   c64_client CONNECT=direct MODE80=1 && make -C c64_client disk-free`
   then `./emu/vice-run.sh x64sc c64_client/build/c64llm-free.d64`.
   Expect: `LOADING...` for ~40s (emulated stock 1541, no JiffyDOS),
   then picture + music + text; countdown counts; early keypresses
   ignored; after 10s the prompt swaps; any key → screen blanks to the
   LOAD, client boots. Which screen it boots to depends on the build:
   `main.c` gates `config_load()` on `#ifndef CONNECT_DIRECT`, so a
   `CONNECT=direct` client goes straight to "Contacting server..." and
   only a hayes build meets the config editor on a fresh disk.
2. **Emulator, registered disk**: `make -C c64_client disk` still boots
   the client directly. (This is the no-regression check.)
3. **Automated**: `emu/test_intro.py` — boots the free d64, waits for
   `patreon` in the panel, injects a key and asserts the countdown is
   still up (nag not skippable), waits for the prompt swap, injects a
   key, and waits for the client's title bar. Runs headless via
   `x64sc -console`, ~40s. Deliberately off `test-all` until it has
   proven flake-free in VICE (autostart keystroke leftovers are the
   known flake source).
4. **Hardware**: `make deploy-c64u-disk-80-free` (commit first). The
   Ultimate's Run Disk must boot the intro; JiffyDOS chain-load must
   land in the client. Leave this step to the user unless told
   otherwise — it reboots their machine.

## 9. Acceptance criteria

- Free disk boots to intro; registered disk unchanged byte-for-byte in
  behavior.
- Intro shows the generated multicolor picture, plays the chosen tune,
  shows the pitch + both URLs, enforces the countdown, chains cleanly
  into the stock client on any key.
- No resident-client source file modified. `make -C c64_client clean &&
  make -C c64_client MODE80=1` links with unchanged headroom.
- `make test-all` untouched and passing.
- Committed: intro source, cfg, Makefiles, generator script, source
  PNG, `intro_data.bin`, `intro_gen.inc`, preview PNG, this doc updated with any
  deviations discovered during implementation (update the doc, don't
  silently diverge).

## 10. Stretch ideas (only after §9 is green, each its own commit)

- **1-line scroller** in text row 4 during the wait phase (after the
  countdown) — the quintessential intro touch. Soft-scroll via `$D016`
  x-scroll bits in IRQ B plus a char shift every 8th frame.
- **Color wash** on the title row (rotate a small color table through
  $D800+800..839 once per frame in IRQ A). Nearly free, very scene.
- **Rasterbars** behind the text area (a handful of `$D021` writes in
  IRQ B). Classic, but mind the seam-flicker interaction.
- A different picture per boot (N pictures on disk, pick by jiffy LSB) —
  costs disk space and load time; probably not worth it.
- Fade the SID volume over ~0.5s before the chain LOAD instead of a
  hard cut ($D418 15→0 across frames) — small, tasteful.

## 11. Implementation notes (what actually happened)

Built and green in VICE. The shape in §1–§4 survived contact; these are
the corrections and the things worth knowing next time.

**Two spec claims were wrong and are fixed inline above.**

1. *Filename case* (§3). `ca65 -t c64` applies the C64 charmap to string
   literals, so uppercase source produces `$C1-DA`, not `$41-5A`. The
   disk names c1541 writes are `$43 $36 $34 ...`, so the source strings
   must be **lowercase**. Verified by assembling both and dumping the
   linked bytes — do that again rather than trusting either version of
   this paragraph.
2. *Banking* (§3, §7). `$01` must be `$36` while the intro runs, or the
   `$B000` tune is BASIC ROM. `$37` is restored before the chain LOAD.
   The "never touch `$01`" rule was the one thing in the spec that would
   have silently produced a crash instead of music.

**Other deviations, all deliberate:**

- **Seconds come from a frame counter**, not the jiffy clock — see §4.3.
  The intro owns the only IRQ source, so the jiffy clock is no longer
  wall-clock accurate.
- **The KERNAL cursor blink is switched off** (`$CC = 1`) before the
  picture goes up. `$0400` is the bitmap's color data by then, and a
  blinking cursor would corrupt one cell of it twice a second.
- **The exit stub is a linker segment**, `STUBCODE`, with `load = MAIN,
  run = STUBMEM($0334)`. Its bytes ship inside the PRG, every label in it
  resolves to $0334, and `__STUBCODE_LOAD__/_RUN__/_SIZE__` drive the
  copy loop. That is what makes "self-contained at $0334" structural
  rather than a thing to remember.
- **`intro.cfg` enforces the $0C00 ceiling**: `MAIN` is sized `$03FF`
  from $0801, so outgrowing the text screen is a link error, not a
  mystery. Current build ends at $0BD0 — 48 bytes spare.
- **The generator crops the source to 16:10 before conversion.** The
  adventure pipeline's `_letterbox` pads to 320x200 preserving aspect,
  so feeding it a square image would put black bars down both sides and
  waste half the screen. Compose art with the bottom fifth expendable:
  `intro_preview.png` dims it so this is visible at review time.
- **Panel text says LLM64**, matching the logo in the artwork; the app
  rename from C64 LLM was in flight when this shipped. Disk filenames
  (`c64llm`, `c64 llm`, `c64 llm.d`) are unchanged and are a contract
  with the registered disk — renaming them means touching `disk:`.

**Shipped assets:** picture generated with the adventure image backend
(nano-banana), source PNG committed as `assets/intro_art.png`. Tune is
`We Are Mature (tune 3)` by Alexander Wiklund / FairLight, from the
relocated `b000_full` library — 3989 bytes at $B000-$BF95. **The §5.2
licensing note still stands**: an HVSC tune is baked into a disk that
asks for money, and long-term this wants a commissioned or explicitly
licensed track.

**Testing.** `emu/test_intro.py` boots the free disk and asserts the
whole chain: panel painted, a key during the countdown ignored, prompt
swap, key, client up at the config editor. It is not in `test-all`
(§8.3) — it runs unwarped because the nag is a wall-clock claim, ~40s.

Two VICE facts that saved time and will again:

- **`x64sc -console` runs headless** — no X display needed, and the
  binary monitor works normally. That is how `test_intro.py` runs.
- **`-exitscreenshot` produces an all-black PNG in console mode.** For a
  visual check of the raster split you need a real display
  (`DISPLAY=:0`), and you need to wait out the load: a 14K blob off an
  emulated stock 1541 takes ~40s, which is exactly why `LOADING...`
  prints first.

One bug worth naming, because it is a 6502 evergreen: rendering the
panel with `ldx #199 / ... / dex / bpl` writes exactly one byte. 199 has
bit 7 set, so the first `dex` already leaves N set. Count up and `cpx`,
or split the loop. The prompt row's `ldx #39` loop was correct, which
made the failure look like a memory-map problem for a while.
