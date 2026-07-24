# 14 — Printer hardcopy: `/print` to a real (or virtual) dot-matrix printer

Status: **IMPLEMENTED 2026-07-23** and green through the emulator e2e:
`make test-emu-tui-80` prints two documents to VICE's device 4 and
asserts the captured page, `make test-emu-print-fail` proves the
no-printer refusal, `make test-all` is green, and
`tests/test_printdoc.py` pins the page layout.

**Confirmed working on real hardware 2026-07-24**: a live C64U session
printed through the firmware's virtual printer. The one thing that had
to change was on the Ultimate, not in the code — the Software IEC
printer ships **disabled**, and until it was enabled `/print` refused
the job (correctly). That step is now in the setup checklist
(docs/05, §8.1 here). The rest of §8 is still owed: the form-feed
question (§10.1), ASCII output mode, F1/SID coexistence, and the READST
refusal branch (§12) have not been exercised on hardware yet.

**§13 (multi-backend `[printer] backend`) IMPLEMENTED 2026-07-24** —
`cups`/`both` route the composed document to a CUPS queue via `lp`
alongside (or instead of) the IEC bus. `make test-emu-print-cups` proves
it end to end with device 4 off the bus, `tests/test_printcups.py` pins
the command line and every failure path against a stubbed `lp`.
§13.8 records where it differs from the plan.

The design below is as built. Where implementation answered an open
question or diverged from the plan, the section says so inline; §12
collects the deltas. Every codebase claim was verified against the code
at commit 0755c54; the VICE printer-capture path was verified
**empirically** on this workstation (VICE 3.10, §2.2). Printer hardware
claims are sourced from vendor docs (links inline).

The feature: print parts of a conversation onto paper (or a captured
page) — `/print the complete recipe`, `/print my inventory` during an
adventure, `/print` for the last reply. The server composes the
document; the C64 drives a printer on IEC device 4 via the KERNAL.

## 0. Verdict and shape

Feasible with **zero new hardware**:

- The **C64 Ultimate firmware contains a virtual IEC printer** (Software
  IEC, since firmware 3.0): device 4, MPS-801/Epson-FX80 emulation,
  output as PNG page images or ASCII text to `/Usb0/printer`.
  <https://1541u-documentation.readthedocs.io/en/latest/ultimate_printer.html>
- **VICE emulates device-4 printers to a file** — verified working
  headless on this machine (§2.2) — so the e2e suite can assert on
  actual printer output, and emulator users get `/print` for free.
- A real Commodore MPS-803 (~$40–150 used) or a user-port Centronics
  thermal receipt printer can be added later with no protocol change
  (§10).

The shape (each piece specified fully below):

1. **Proxy**: `/print [what]` branch in `handle_command`
   (`protocol.py:480`) → document composer (new `src/printdoc.py`,
   deterministic fast paths + `_ask_model` fallback) → ack-paced
   `PRINT_BEGIN/DATA/END` frames, server silent between blocks.
2. **Client**: three new `case`s in the resident dispatcher
   (`main.c:643`), printing each block through KERNAL `cbm_write` to
   device 4 inside the existing `serial_rx_pause`/`music_hold` bracket.
   No overlay module needed (§5.1 explains why).
3. **Tests**: proxy unit tests for the composer; e2e prints through
   VICE's emulated device-4 printer and asserts the captured file.

## 1. Current code — verified, with line references

Client paths `c64_client/`, proxy paths `llm64_proxy/src/`.

- **KERNAL is banked in; `cbm_*` is already used.** `loader.c:26`
  (`cbm_load`), `mod_diskcopy.c:96-129` (`cbm_open/cbm_read/cbm_write/
  cbm_close` — note: that's overlay code, but the cc65 library routines
  it pulls in are linked into the resident CODE segment, so a resident
  print path adds no new library weight). KERNAL is only transiently
  banked out inside soft-80 bitmap writes (`soft80.s:398,425`).
- **Charset**: wire text is ASCII; Commodore printers eat PETSCII.
  `text.c:19` `ascii_to_petscii()` (hex-constant ranges — see the cc65
  trap note at `text.c:17`). PETSCII has no 0x0A newline; printers want
  0x0D (CR). §5.3 defines the mapping.
- **The IEC-vs-NMI bracket exists and is the law.** `serial.h:83-89`
  (`serial_rx_pause/serial_rx_resume`), used by `mod_open`
  (`main.c:558-562`, with `music_hold_begin/end` around it) and
  documented in `mod_diskcopy.c:7-13`: "IEC transfers and serial NMIs
  don't mix." Any print I/O must sit inside the same bracket.
- **Frame plumbing, client**: dispatcher `handle_message`
  (`main.c:643`), payload buffer `MAX_PAYLOAD` = 512 (`common.h:160`,
  `main.c:97`); `proto_send_ack/nak` (`protocol.h:67-68`). Message
  codes live in `common.h:100-146` and mirror `protocol.py:28-72`;
  both use printable ASCII to survive tcpser/IP232. **Server→client
  codes 0x50–0x61 are taken; 0x62 'b', 0x63 'c', 0x64 'd' are free.**
- **Frame plumbing, proxy**: `send_message` caps payloads at 512
  (`protocol.py:355`); `_send_bulk` paces each frame past its wire time
  (`protocol.py:945`); `_send_begin` sends a BEGIN and re-sends until
  the client ACKs (`protocol.py:987-1006`); the ACK dispatcher routes
  to `_begin_ack` then `_flow_ack` (`protocol.py:289-298`); NAK
  handling at `protocol.py:305-331`. The pacing constants and the
  C64U burst-tail rationale: `protocol.py:896-925`.
- **State machine, client**: `state` ST_IDLE/ST_LOADING (`main.c:85-99`);
  media transfers set ST_LOADING + `watchdog_reset()` so typing is
  rejected as Busy (`main.c:1006-1008`) and a lost tail can't hang the
  UI. Watchdog: ~43 s (`WATCHDOG_UNITS` 10 × ~4.3 s, `main.c:208-223`),
  reset by ANY serial arrival (`main.c:1380-1381`), expiry cleanup at
  `main.c:1382-1405` (this is where print cleanup hooks in).
- **The SID/IMG BEGIN-ACK handshake is the model**: client ACKs
  SID_BEGIN only when it's safe to stream (`main.c:842-844`), proxy
  holds data until then (`protocol.py:1030-1058`).
- **Document sources on the proxy**: adventure state JSON (hp, gold,
  inventory, appearance…) is parsed from `[[STATE:]]` and persisted as
  meta `adv_state` (`protocol.py:1754`, read back at `:1121`); the
  character sheet is `AdventureMode.background`/`.character`
  (`modes.py:166-175`); transcript via `self.conv_manager.get_messages()`
  (`protocol.py:1114`). The utility-LLM-call pattern to imitate:
  `_derive_scene_prompt` → `_ask_model(question, limit=400)`
  (`protocol.py:1104-1132`).
- **Resident headroom**: the Makefile DIAG comment (~819 bytes,
  `c64_client/Makefile:62-65`) is STALE — the 2026-07-21 OVLnBSS/RODATA
  retrofit raised it to ~1,467 bytes (DIAG ~1,277). Verify against
  `build/llm64.map` after building; the print path budget is §9.
- Nothing anywhere in the repo mentions printing — greenfield.

## 2. Printer backends

### 2.1 C64 Ultimate firmware virtual printer (the deployment target)

F2 menu → Software IEC Settings → "IEC Drive and Printer" = Enabled.
Printer is IEC device 4 (or 5), emulating an MPS-801 (also Epson FX-80
/ IBM modes), honoring secondary address 0 = uppercase/graphics, 7 =
business (lower/upper) — same behavior as real hardware. Output lands
in `/Usb0/printer` as `printer-NNN.png` page images (240×216 dpi,
~15 s/page to compose) or appended ASCII/RAW, selectable in the menu.
Known gotchas: a 256-byte internal buffer means partial pages sit
until "Flush Printer/Eject Page" (F5 menu) or a page-fill; whether a
trailing form feed auto-ejects is UNVERIFIED (§8 tests it; the
`formfeed` flag in §4 exists for exactly this). Software IEC speaks
standard KERNAL protocol only — which is all we use.
Docs: <https://1541u-documentation.readthedocs.io/en/latest/ultimate_printer.html>

### 2.2 VICE (x64sc) — VERIFIED empirically 2026-07-23

VICE 3.10, native install on this workstation. The flags (checked
against `x64sc -help`; older manuals show different names):

```
-devicebackend4 1        device 4 backend: 1 = filesystem
-busdevice4              enable true IEC emulation for device 4
-pr4drv ascii            driver: ascii|raw|mps801|mps802|mps803|nl10|...
-pr4output text          output type: text | graphics
-pr4txtdev 0             route to text device slot 1
-prtxtdev1 <path>        the dump file (appended)
```

Smoke test that PASSED: `petcat`-compiled BASIC
(`10 open4,4,7 : print#4,"hello from device 4" : ...`), run via
`DISPLAY=:0 timeout 25 x64sc -default -warp -sounddev dummy
+confirmonexit <printer flags> -autostartprgmode 1 -autostart
prtest.prg`. The dump file contained exactly the printed lines,
lowercase preserved (SA 7 business mode), one line per PETSCII CR. A
trailing CHR$(12) came out as `.` in the ascii driver (unprintables are
dotted) — harmless. NOTE: the file's content was read after VICE
exited; whether the ascii driver flushes mid-run is UNVERIFIED — the
e2e assert must tolerate reading after teardown (§7.3).

For dot-matrix page images instead: `-pr4drv mps803 -pr4output
graphics` (renders through VICE's gfxoutput subsystem). For the test
suite, `ascii`+`text` is the right choice — deterministic, grep-able.

ANSWERED during implementation (the flags are now always on, in both
`test_e2e.py` and `run_emu.sh`):

- The ascii driver **does** flush mid-run — the e2e finds the page in
  `artifacts/printer4.txt` seconds after the job ends, no exit needed.
  The test still tolerates both (§7.3) and prints which path won.
- The capture **wraps at 74 columns**, so a 78-column rule prints as
  74 + 4. That is VICE's text device, not us and not the printer: a
  real MPS-803 is 80 columns and takes the 78 without wrapping. Left
  alone rather than narrowing the default for the emulator's sake;
  `[printer] width` is there if you disagree.
- The trailing form feed shows up as a single `.` in the capture
  (unprintables are dotted), which is also how you can see it was
  sent at all.

### 2.3 Real paper (all later, no protocol change)

- **Used Commodore MPS-803** on IEC: identical code path, plug in and
  it works. 60 cps ⇒ ~4 s per 240-byte block; §4's per-block ack keeps
  the server waiting patiently. EBay ~$40–150 (spread of live
  listings; check ribbons/rollers).
- **User-port Centronics** (geoCable pinout) into a parallel ESC/POS
  thermal receipt printer or a RetroPrinter Pi module
  (<https://www.retroprinter.com/>): the user port is free (only
  `$DD00` bits 0-1 are used, VIC banking — `soft80.s:74-76`), a polled
  driver is tens of bytes, and it has NO IEC/NMI conflict at all. This
  is a separate small client driver + a `PRINT_BEGIN` flag bit later.
- **Not viable directly**: BLE "cat"/Phomemo pocket printers
  (raster-only BLE; would need an ESP32 bridge), second RS-232 UART
  (the ACIA is the modem; a user-port UART conflicts and needs level
  shifting).

### 2.4 Proxy-side paper: the NDYIN "N80" A4 thermal (investigated 2026-07-23)

The owner's N80 (Amazon B0F9YBMJDV; OEM Zhuhai Jiuyin "ZHJY", also
branded NEKING; driver extracted at `~/Downloads/n80/`) is **not** an
80 mm receipt printer — it is a **portable A4/Letter full-page thermal
printer**: 208 mm print width, 203 dpi, ~10 mm/s (~30 s/page), battery
+ USB-C, tear-bar only. Spec: <https://ndyin.com/pages/spec-n80>.
Facts that matter (PPD + `rastertoN80` filter inspected locally,
vendor manual cross-checked):

- **No WiFi exists on this model** — the WiFi question is moot, not
  app-locked. Bluetooth IS app-only (manual: computers must use USB;
  the Nada Print app is the BT client), and the payload language is a
  proprietary raster command set (`CMD:XPP,XL`; the filter renders
  dithered bitmaps — `BitmapPrintCmdPOS`/`BitmapErrorDiffuse`), so raw
  ESC/POS or plain text sent at the device prints nothing.
- **USB from Linux works the normal CUPS way**: the download is a
  standard CUPS raster driver (`ZHJY-N80.ppd` +
  `filter/{x86_64,i386,armv7l,aarch64}/rastertoN80`, linked only
  against libcups/libcupsimage). Install, add a `usb://` queue, and
  CUPS's own text→raster pipeline makes `lp -d n80 doc.txt` print —
  no ESC/POS needed. The filter/backend split also means the same
  driver would serve a network path if one existed; here it doesn't.
- **Placement**: tethered to whatever box runs the CUPS queue. Either
  the proxy host directly, or a **Raspberry Pi bridge** (the vendor
  ships armv7l/aarch64 filters) sharing the queue over IPP — the
  proxy then prints to `ipp://<pi>/printers/n80` with no local driver.
  That is the "over WiFi" answer: the network hop is CUPS/IPP to the
  Pi, never to the printer.
- **The C64U cannot drive it**: the Ultimate's USB is storage/host for
  its own firmware features only, its printer emulation writes files,
  and it speaks no XPP raster. Irrelevant path.
- **Integration point**: a proxy-side backend beside the PRINT
  frames — designed in full in §13 (`[printer] backend = c64 | cups |
  both`). Being a 203 dpi page printer, the same backend could also
  hardcopy generated adventure illustrations
  (`data/images/...png` → `lp`), which no receipt printer could.
- If the receipt-printer aesthetic (auto-cut, raw TCP 9100, true
  ESC/POS text, LAN station mode) is ever wanted, that is a different
  purchase (e.g. Epson TM-T20II-ETH, Xprinter LAN models, NETUM
  NT-8330) — those work driverless via `python-escpos`/port 9100 and
  also fit the §2.3 user-port Centronics route, which the N80 (no
  parallel port, raster-only) does not.

## 3. The one hard constraint (why the protocol looks like this)

The 6551 ACIA has a 1-byte RX register: at 19200 baud a byte lands
every ~520 µs and the NMI handler must drain it or it is lost. KERNAL
IEC transfers are bit-banged with interrupts masked. The codebase's
settled answer (module loads, disk copy) is: mask serial RX for the
IEC window and make sure nothing arrives meanwhile. For printing, the
guarantee is protocol-level: **the server sends one block, then goes
silent until the client acks; the client performs all IEC I/O only
between frames, inside the pause bracket.** Nothing is ever in flight
during an IEC window, so nothing can drop — no reliance on RTS/CTS,
no new serial behavior. (Printing is talker-side IEC, which is more
forgiving than a disk LOAD, but the bracket discipline stays.)

Streamed-SID playback steals IRQ time with SEI windows and would both
warble and threaten IEC timing — the whole job runs under
`music_hold_begin/end`, exactly like `mod_open` (`main.c:549-562`).

## 4. Wire protocol

Three new server→client types (next free printable codes; add to BOTH
`protocol.py:28-72` `MessageType` and `common.h:100-146`, keeping the
comment style):

```
PRINT_BEGIN = 0x62  # 'b' - [flags][nblocks]; open the printer channel
PRINT_DATA  = 0x63  # 'c' - one block of ASCII text (<= 240 bytes)
PRINT_END   = 0x64  # 'd' - close the channel, report
```

Client→server replies reuse `MSG_ACK`/`MSG_NAK` — no new codes.

- **PRINT_BEGIN** payload: `flags(1)` bit0 = business charset (open
  with secondary address 7; clear = SA 0 uppercase/graphics), bit1 =
  send a form feed (0x0C) before closing. `nblocks(1)` = total DATA
  blocks, clamped to 255, for the client's progress status only.
  Client: open the channel (§5.2); ACK when ready, NAK if the device
  is absent. Sent via `_send_begin` (`protocol.py:987`), which
  re-sends until ACKed — so the client must treat a duplicate BEGIN
  while a job is open as "re-ACK, don't reopen".
- **PRINT_DATA** payload: raw ASCII text, **≤ 240 bytes** (fits the
  512-byte buffers with margin; ≈4 s on a 60 cps MPS, far inside the
  ~43 s watchdog). Lines end in `\n` (0x0A); the client maps them to
  CR. No offset/seq bytes: blocks are strictly serialized by the
  one-in-flight rule, and a lost ack aborts rather than resends.
  Client: print the block, then ACK (NAK + abort on write error).
- **PRINT_END** payload: empty. Client: optional form feed, close,
  ACK, status line.

Server sequencing (one in-flight block, ever):

```
compose doc → PRINT_BEGIN ─wait ACK (2s x4, _send_begin)──┐ NAK → abort, status
  for each 240-byte block:                                │
      PRINT_DATA ── wait ACK (30 s, no retry) ── timeout/NAK → abort, status
  PRINT_END ── wait ACK (30 s) → STATUS "Printed NN lines."
```

The 30 s per-block timeout is generous for real printers and is an
ABORT, not the `_send_bulk_stream` "warn and continue"
(`protocol.py:1023-1025`) — continuing would transmit into a client
that is still printing with RX masked.

## 5. Client implementation (`c64_client/`)

### 5.1 Resident, not an overlay module — the decision

A `mod_print` overlay was considered (slot machinery: `c64-soft80.cfg`,
`loader.c`, `mod_open` `main.c:531`). Rejected because: (a) the module
load itself is an IEC transfer needing its own server-silent handshake
before the job's handshake — two brackets instead of one; (b) modules
are loaded from user keys today, not from incoming frames, so BEGIN
would need new resident dispatch anyway; (c) the print path needs no
buffer beyond the existing 512-byte `payload_buffer` (`main.c:97`) —
blocks are printed straight out of it; (d) the cc65 `cbm_*` routines
are already resident (§1). Expected resident cost ~300–450 bytes
against ~1,467 free (§9). If the map says otherwise after building,
the module fallback design is in git history (this doc, first
revision) — but measure first.

Scope: implement inside `#ifdef SOFT80` alongside the SID/IMG cases
(`main.c:797-976`). The 40-column build doesn't link `cbm_open`/
`cbm_write` today and its RAM budget is unaudited — don't widen scope.

### 5.2 New statics and the three cases (`main.c`)

Near `sid_active`/`img_active` declarations, add:

```c
static uint8_t prt_active;    /* printer channel open */
static uint8_t prt_ff;        /* send form feed at END (BEGIN flags bit1) */
static uint8_t prt_total;     /* blocks expected (progress) */
static uint8_t prt_done;      /* blocks printed */
#define LFN_PRT 6             /* 4,5 are the disk-copy module's */
```

`LFN_PRT` = logical file 6 on device 4. `cbm_open(6, 4, sa, "")` does
SETNAM with an empty name — correct for printers.

In `handle_message` (`main.c:643`), inside the existing
`#ifdef SOFT80` block, add three cases modeled on `MSG_SID_BEGIN`
(`main.c:804`):

```c
case MSG_PRINT_BEGIN: {
    uint8_t* p = proto_get_payload(&proto);
    uint8_t sa;
    if (prt_active) { proto_send_ack(); break; }  /* duplicate BEGIN */
    sa = (proto_get_length(&proto) >= 1 && (p[0] & 1)) ? 7 : 0;
    prt_ff = (proto_get_length(&proto) >= 1 && (p[0] & 2)) ? 1 : 0;
    prt_total = (proto_get_length(&proto) >= 2) ? p[1] : 0;
    prt_done = 0;
    music_hold_begin();          /* held for the whole job (see 3) */
    serial_rx_pause();
    if (cbm_open(LFN_PRT, 4, sa, "") != 0) {
        serial_rx_resume();
        music_hold_end();
        proto_send_nak();
        ui_status("No printer on device 4.");
        break;
    }
    serial_rx_resume();
    prt_active = 1;
    if (state == ST_IDLE) { state = ST_LOADING; watchdog_reset(); }
    ui_status("Printing...");
    proto_send_ack();
    break;
}
case MSG_PRINT_DATA: {
    uint8_t* p = proto_get_payload(&proto);
    uint16_t len = proto_get_length(&proto);
    uint16_t i;
    if (!prt_active) break;      /* stray frame after an abort */
    for (i = 0; i < len; ++i)    /* ASCII -> PETSCII, \n -> CR */
        p[i] = (p[i] == 0x0A) ? 0x0D : ascii_to_petscii(p[i]);
    serial_rx_pause();
    if (cbm_write(LFN_PRT, p, len) != (int)len) {
        serial_rx_resume();
        prt_abort();             /* below */
        proto_send_nak();
        ui_status("Printer error - job cancelled.");
        break;
    }
    serial_rx_resume();
    ++prt_done;
    /* "Printing 3/12..." via the load_count pattern (main.c:613-617);
       the /10 %10 idiom is already resident - no new runtime cost */
    watchdog_reset();
    proto_send_ack();
    break;
}
case MSG_PRINT_END: {
    static const uint8_t ff = 0x0C;
    if (!prt_active) break;
    serial_rx_pause();
    if (prt_ff) cbm_write(LFN_PRT, &ff, 1);
    cbm_close(LFN_PRT);
    serial_rx_resume();
    music_hold_end();
    prt_active = 0;
    if (state == ST_LOADING) state = ST_IDLE;
    ui_status("Printed.");
    proto_send_ack();
    break;
}
```

And a small shared abort helper (also used by the watchdog):

```c
static void prt_abort(void) {
    cbm_close(LFN_PRT);          /* rx already paused by caller, or: */
    music_hold_end();            /* wrap close in pause/resume if not */
    prt_active = 0;
    if (state == ST_LOADING) state = ST_IDLE;
}
```

**Watchdog expiry** (`main.c:1382-1405`): in the SOFT80 cleanup branch
that already handles `sid_active || img_active`, add `prt_active`:
close the channel (inside a pause/resume bracket), clear the flag,
DON'T send a NAK-for-resend (unlike SID, there is no auto-retry;
the proxy's own 30 s timeout has already aborted its side).

cc65 traps that apply here (all previously paid for in blood — roadmap
3j): compare against ASCII HEX constants, never char literals (`'f'`
compiles to PETSCII in module/main code); no new 16/32-bit divides
(the progress line reuses the existing `/10 %10` uint8 idiom); status
strings are resident RODATA — keep them short.

`common.h`: add the three `#define MSG_PRINT_*` lines (§4).
No changes to `serial.s`, `loader.c`, the linker config, the Makefile,
or the d64 targets. Nothing new ships on the boot disk.

### 5.3 Charset rule (fixed here so both sides agree)

Server sends plain ASCII, lines terminated `\n`, no NULs, no markup
control cells (composer guarantees, §6.2). Client maps per byte:
`0x0A → 0x0D`, else `ascii_to_petscii()` (`text.c:19`). With SA 7
(business mode) this prints mixed-case text exactly as chatted. The
conversion happens in place in `payload_buffer` — the frame is
consumed immediately, nothing else reads it afterwards.

## 6. Proxy implementation (`llm64_proxy/`)

### 6.1 `MessageType` + command dispatch (`src/protocol.py`)

Add the three codes (§4) to `MessageType`. In `handle_command`
(`:480`), add before the unknown-command fallthrough:

```python
elif cmd == 'print':
    await self._print_command(arg)
```

Add `/print [what] - hardcopy to the printer` to the `/help` text
(`:487-509`) — mind the 40-column wrap rule visible there (short
lines). Optionally add a menu entry in the server-fed menu
(`handle_get_menu`) later; not required.

### 6.2 The composer — new `src/printdoc.py`

One public function, mirroring `scenecomp.py`'s shape:

```python
def compose_request(arg, msgs, adv_state, character, background) -> str | None
def render_sheet(adv_state, character, background, width) -> str
def finish(title, body, width) -> str
```

Behavior spec for `_print_command` (in `protocol.py`, using these):

1. **Bare `/print`** → last assistant message from
   `self.conv_manager.get_messages()`, reflowed. No LLM call.
2. **Sheet fast path**: `arg` matching
   `r'\b(inventory|character|char|sheet|stats)\b'` (case-insensitive)
   AND `adv_state` meta present → `render_sheet(...)`: name/appearance
   (from `AdventureMode.character` / `background`), HP, gold,
   location, one inventory item per line, companions. Deterministic —
   no LLM call, no tokens.
3. **Everything else** (`/print the complete recipe`) → utility call
   via the existing `self._ask_model(question, limit=800)`
   (`protocol.py:1132` shows the pattern). The question MUST contain
   the literal phrase **`PRINTABLE DOCUMENT`** (the mock keys on it,
   §7.1) and instructs: "Extract and compose a PRINTABLE DOCUMENT from
   this conversation for: {arg}. Plain text only, no markdown, no
   commentary, a short title on the first line." Feed the last ~12
   messages like `_derive_scene_prompt` does (`:1114-1118`).

   **Amended (2026-07-24) — a document is not a chat turn.** As first
   built, every one of these numbers was inherited from the interactive
   path, and each one truncated the page independently:

   - `api.max_tokens` (800 live) is tuned so a reply reaches the C64
     fast. At 78 columns it runs dry near line 40 and stops mid-step
     with `finish_reason: 'length'` — no error, just a short page. The
     print path now passes its own `sampling={'max_tokens': ...}` from
     **`[printer] max_tokens`** (default 2000, ~a full page with
     headroom). `_ask_model` grew a `sampling` parameter for this;
     chat, `/pic` and the caption call are untouched.
   - The per-message context clip is **4000 chars** here, not the 800
     `_derive_scene_prompt` uses. The document being asked for *is* one
     of those messages; 800 chars is ~10 printed lines, so the tail was
     lost before the model ever saw it.
   - `limit=` on the reply is 12000 chars, kept clear of
     `printer_max_tokens` so it can't behead a page the model finished.

   The question itself now reads two things off `arg`, both opt-in
   (`wants_synthesis()`, `target_lines()`):

   - **Fidelity.** The default stays a strict extraction — "do not
     invent any" is unchanged, because a printed page is easy to
     mistake for a record. But an explicit ask to complete/fill in/
     flesh out/collate (`SYNTH_RE`, plus `COMPLETE_VERB_RE` so *"please
     complete this recipe"* synthesizes while *"the complete recipe"*
     still only extracts) swaps in wording that supplies what's missing
     — measurements, an ingredient list, omitted steps — while quoting
     what was actually said and contradicting none of it.
   - **Length.** "detailed"/"one-page" → aim for ~55 lines; "brief"/
     "summary" → under 20. Neither word present → no length line at
     all, i.e. exactly the original prompt. `brief` wins a tie.
4. **`finish()`** (applied to all three): strip `[[...]]` directives
   (`re.sub(r'\[\[.*?\]\]', '', s, flags=re.S)` — stored text should
   already be clean, this is belt and braces), apply
   `UNICODE_TO_ASCII` (`protocol.py:76`), `encode('ascii','replace')`,
   `textwrap` each paragraph to the configured width, add a header
   (title, date line, `-----` rule) and footer rule. Result is pure
   ASCII with `\n` line ends. Empty/failed compose → send a canned
   "Nothing to print." and stop.

Config: a `[printer]` section with `width` (default 78) and `formfeed`
(default true) in the proxy config, read like the existing sections.

### 6.3 The send job (`src/protocol.py`)

`_print_command` composes, then runs the job via
`self._spawn_media(self._send_print(doc))` (`:962`) so the reader task
stays free to dispatch the ACKs the job waits on (same reason as
SID/IMG — see the `_media_tasks` comment at `:120-123`).

```python
PRINT_BLOCK = 240
PRINT_ACK_TIMEOUT = 30.0

async def _send_print(self, doc: str):
    data = doc.encode('ascii', 'replace')
    blocks = [data[i:i + self.PRINT_BLOCK]
              for i in range(0, len(data), self.PRINT_BLOCK)]
    flags = 0x01 | (0x02 if self.config.printer_formfeed else 0)
    head = bytes([flags, min(len(blocks), 255)])
    self._print_active = True
    self._print_refused = False
    try:
        async with self._media_lock:
            if not await self._send_begin(MessageType.PRINT_BEGIN, head):
                await self.send_status("Printer not responding.")
                return
            if self._print_refused:
                await self.send_status(
                    "No printer on the bus (device 4).")
                return
            for b in blocks:
                if not await self._print_block(
                        MessageType.PRINT_DATA, b):
                    return
            if await self._print_block(MessageType.PRINT_END, b''):
                await self.send_status(
                    f"Printed {doc.count(chr(10)) + 1} lines.")
    finally:
        self._print_active = False

async def _print_block(self, msg_type, payload) -> bool:
    """One frame, then silence until the client acks. Timeout or NAK
    aborts the job - never transmit into a client that is printing
    with serial RX masked."""
    self._flow_ack = asyncio.Event()
    try:
        await self._send_bulk(msg_type, payload)
        await asyncio.wait_for(self._flow_ack.wait(),
                               self.PRINT_ACK_TIMEOUT)
    except asyncio.TimeoutError:
        await self.send_status("Printer stalled - job cancelled.")
        return False
    finally:
        self._flow_ack = None
    if self._print_refused:
        await self.send_status("Printer error - job cancelled.")
        return False
    return True
```

ACK routing needs NO change: `_send_begin` uses `_begin_ack`, blocks
use `_flow_ack`, and the dispatcher already routes in that order
(`:289-298`). **NAK routing needs one addition** at the TOP of the NAK
branch (`:305`), before the `_img_sent` check so a print NAK can never
be misrouted into the image/SID retry logic:

```python
if getattr(self, '_print_active', False):
    self._print_refused = True
    ev = self._begin_ack or self._flow_ack
    if ev and not ev.is_set():
        ev.set()
elif getattr(self, '_img_sent', False):
    ...
```

(`_send_begin` returns True on that set event; the `_print_refused`
check right after it turns the refusal into the friendly status.)

Init `self._print_active = False` in `__init__` next to `_begin_ack`
(`:117`). The `_media_lock` serializes against SID/IMG sends; a print
during streamed music works because MUSIC holds are client-side and
the lock prevents interleaved transfers.

## 7. Tests

### 7.1 Mock (`emu/mock_llm.py`)

Add a branch ABOVE the generic fallthrough, keyed on the composer's
marker (mirroring the `'CURRENT SCENE FOR AN ILLUSTRATOR'` branch at
`mock_llm.py:69-77`, including its "must precede" ordering care):

```python
elif 'PRINTABLE DOCUMENT' in upper:
    text = ("Grandma's Fire Stew\n"
            "Serves four adventurers.\n"
            "1. Brown the salted pork.\n"
            "2. Add root vegetables and stock.\n"
            "3. Simmer until the dragon calms.")
```

### 7.2 VICE flags (`emu/test_e2e.py`, `emu/run_emu.sh`)

In the VICE argv (`test_e2e.py:341-353`), add unconditionally:

```python
'-devicebackend4', '1', '-busdevice4',
'-pr4drv', 'ascii', '-pr4output', 'text',
'-pr4txtdev', '0', '-prtxtdev1', str(artifacts / 'printer4.txt'),
```

Same flags (with a `build/printer4.txt` path) appended in
`run_emu.sh:43-47` so interactive emulator users get `/print` working
out of the box. These flags are independent of drives 8/9 and the
ACIA — no interaction with existing tests.

### 7.3 e2e flow (`test_e2e.py`, TUI section) — as built

The client cases are `#ifdef SOFT80` (§5.1), so the print steps live in
the `if args.cols80:` block, first thing after the adventure has
started — which is also what makes the sheet path testable, since the
state block exists by then. Both composer paths run:

```python
monitor.keyboard_feed('/print the recipe\r')      # model composes
wait_for_screen(monitor, r'printed \d+ lines', 60, ...)
monitor.keyboard_feed('/print my inventory\r')    # sheet, no model call
wait_for_screen(monitor, r'printed \d+ lines', 60, ...)
```

`printed \d+ lines` is the PROXY's closing status, which only arrives
after the client ACKed PRINT_END — a screen match on the client's own
"Printed." would pass one handshake too early.

The capture is asserted after `monitor.quit()`, alongside the d64 and
meta assertions: the composed title and body, plus the sheet's
appearance line and `Inventory:`, plus a line-count floor proving the
`\n`→CR mapping produced real lines instead of one long run. The
mid-run poll (10 s, `fire stew`) still runs first and only decides
which message is printed — VICE flushes mid-run in practice (§2.2).

`artifacts/printer4.txt` is deleted at the start of every run: VICE
APPENDS to it, so without that, last run's page satisfies this run's
assertion.

Also worth one cheap negative test: run the same `/print` with
`-devicebackend4 0` in a variant (or just note it) — expected: client
NAKs BEGIN, proxy prints "No printer on the bus (device 4).", client
returns to Ready. This proves the abort path never wedges the client.
(An always-on flag set means the main suite won't exercise it; a
follow-up `--no-printer` harness switch is acceptable scope.)

### 7.4 Proxy unit tests (`llm64_proxy/tests/test_printdoc.py`)

Pure-Python, joining the existing suite (see `tests/test_dice.py` for
conventions): sheet rendering from a fixture `adv_state` (exact
expected text), wrapping at width 78 and 40, directive stripping,
unicode translation, bare-`/print` last-message selection, marker
phrase present in the LLM question, block split at 240 including the
exact-multiple boundary case.

Run everything through the existing aggregate: `make test-all` (top
Makefile) — the print steps ride `test-emu-tui-80`. The refusal path
has its own target, `make test-emu-print-fail` (§7.3), deliberately
NOT in `test-all`: it is the whole 80-column suite again for one
assertion. Run it whenever the print path changes.

## 8. C64U hardware verification checklist (after emulator green)

Deploy with `make deploy-c64u-80` (top Makefile; pushes via
`emu/u64_telnet.py`, checklist in `docs/05-ultimate-setup.md`).

1. **DONE 2026-07-24.** F2 → Software IEC Settings → IEC Drive and
   Printer = Enabled; printer output = PNG first, USB stick present.
   **This is disabled out of the box**, and it is the first thing to
   check when a live session refuses to print: the client's refusal is
   indistinguishable from a broken feature, and it took a config
   investigation to find that nothing was wrong with the code. Now
   documented in `docs/05-ultimate-setup.md` too.
2. **DONE 2026-07-24** — printed from a live session. `/print the
   recipe` in chat mode → expect "Printing..." → "Printed." →
   `printer-001.png` in `/Usb0/printer` (PNG composition takes ~15 s —
   wait before judging).
3. **Form-feed question (§10.1)**: with `formfeed=true`, does the page
   eject without the F5 "Flush/Eject" menu action? Try `formfeed=false`
   + manual flush for comparison. Set the config default to whichever
   gives one-shot UX.
4. Coexistence: F1 (menu module = IEC disk load) immediately before
   and after a print; then `/print` during streamed SID playback
   (music should hold, resume after). Watch for the burst-tail
   signature (`cr=00` losses) in the status line.
5. ASCII output mode: switch the U64 printer emulation to ASCII and
   confirm the appended text file matches the VICE capture.
6. **No printer at all**: turn the virtual printer OFF (or unplug the
   MPS) and `/print` again. Expect "No printer on the bus (dev 4)"
   from the READST branch (§12) — the one refusal path the emulator
   cannot reach — and the editor usable immediately afterwards, not
   after the ~43 s watchdog.
7. Leave it printed on the fridge. (Optional but recommended.)

## 9. Budgets

MEASURED, not estimated (`build/llm64.map`, MODE80, after the build):

| Piece | Estimate | Actual |
|---|---|---|
| Client resident (3 cases + close + statics) | ~300–450 B | **809 B** (CODE ~671, RODATA 113, BSS 25) |
| Free after, MODE80 | — | **395 B** (was 1,204; the Makefile's "~819" comment and this doc's "~1,467" were both stale) |
| Free after, MODE80 DIAG=1 | — | **~190 B** — still links, but that build is now nearly full |
| Proxy | ~150 lines + `printdoc.py` ~120 lines | 130 + 175 lines |
| Harness | ~20 lines e2e + mock branch + run_emu.sh flags | as planned |
| New wire codes | 3 server→client, 0 client→server | 0x62–0x64, free both sides |

The overrun is cc65 codegen on ordinary C, not one expensive thing: the
per-block progress line ("Printing 03/12") costs 144 B of it, and
rewriting the ASCII→PETSCII loop as a pointer walk instead of an
indexed one bought 4 bytes. The `cbm_*` library routines really were
already resident (§1) — they cost nothing here. **The next resident
feature has to reckon with 407 bytes, or move something to an
overlay.**

## 10. Open questions (answer during implementation; defaults chosen)

1. **U64 form-feed eject** — STILL OPEN (needs hardware). Default
   `formfeed=true`; flip per §8.3.
2. **VICE mid-run flush** — ANSWERED: it flushes mid-run (§2.2). The
   e2e still handles both and reports which path it took.
3. **Print width** — fixed 78 via config; the client's 40/80 mode is
   irrelevant to the printer (MPS is 80-col; receipt printers later
   can get a narrower config).
4. **Confirm gate?** — v1 prints immediately (the composed doc is
   cheap to re-run and the U64 path wastes no paper). If real-MPS
   owners complain about noisy misfires, add doc-13-style confirm.
5. **`/print` during adventure SETUP or claude mode** — composer
   sources degrade gracefully (no adv_state → no sheet fast path);
   nothing special needed, but don't crash on empty conversations
   (covered by "Nothing to print.").

## 11. Deferred (v2+, explicitly out of scope now)

- **LLM-marked printable snippets** (the original grey-rule idea):
  server-side span tracking via a `[[PRINTABLE]]` directive through
  `DirectiveFilter` (`music.py:201-275`) + a markup control cell for
  the rule; bare `/print` then offers the last marked span. Same
  machinery as `[[STATE:]]`; add once printing habits are observed.
- **`/map` hardcopy** via SA 0 graphics/PETSCII-art printing.
- **User-port Centronics driver** for thermal receipt printers
  (§2.3) — a `PRINT_BEGIN` flag bit selects the backend.
- **Print history meta** (what was printed when) if wanted for the
  "already printed" indicator idea.

## 12. As built — where the code differs from the plan above

The wire protocol (§4) and the proxy job (§6) landed exactly as
specified. The deltas worth knowing — two of them are bugs the plan
walked into, found by the tests:

- **`UNICODE_TO_ASCII` moved** from `protocol.py` to `markup.py`. The
  composer needs it and `printdoc` must stay importable without
  dragging in the whole protocol stack (its unit tests have no event
  loop and no aiohttp). `markup.py` is the egress-to-the-C64 module, so
  it is the right home; `protocol.py` imports the name and its own use
  site is unchanged.
- **`printdoc.py` exports seven functions, not three**: `wants_sheet`,
  `last_reply`, `compose_question`, `split_title`, `render_sheet`,
  `finish`, plus `blocks()` — the 240-byte split moved out of
  `_send_print` purely so the exact-multiple boundary is testable
  without an event loop.
- **`_print_command` guards re-entry**: a second `/print` while a job
  is in flight answers "A print job is already running." rather than
  queueing behind `_media_lock` (where it would look hung).
- **Wrapping is per line, not per paragraph.** The sheet's
  one-item-per-line layout has to survive, and prose reflows the same
  either way. Continuation lines keep the original indent, and `- `
  items get two extra columns so a wrapped item stays under its
  bullet.
- **`cbm_open` alone cannot tell you there is no printer**, so the
  client also checks `cbm_k_readst() & 0x80` right after the open: a
  KERNAL OPEN to a missing IEC device returns cleanly and only sets ST
  bit 7 (device not present), and without that check the first
  `cbm_write` is what discovers there is no printer — after the client
  has already ACKed the BEGIN and told the proxy the job is on.
  **Both refusals are live paths and only hardware can decide which
  you get.** `make test-emu-print-fail` does NOT reach the READST one:
  VICE with `-devicebackend4 0` leaves device 4 ON the bus with no
  backend behind it, so the OPEN succeeds, ST stays clear, and the
  write fails instead ("Printer error - job cancelled." rather than
  "No printer on the bus"). The test therefore asserts what actually
  matters — the job is abandoned and the client comes back usable —
  and accepts either message. Add "unplug the printer" to §8 to
  exercise the READST branch for real.
- **The client owns ST_LOADING for the whole job, and takes it from
  ST_WAITING as well as ST_IDLE.** The SID/IMG `if (state == ST_IDLE)`
  idiom is wrong here and was copied in from the plan: those transfers
  arrive unbidden while the client is idle, but a print job is the
  answer to the `/print` the user just typed, so the client is sitting
  in ST_WAITING for a reply that never comes (nothing sends CHAT_DONE
  for a print). Left as written, the editor stayed Busy until the ~43 s
  watchdog fired. The e2e now proves the recovery by typing `/mode`
  after each job and reading the STATUS ROW for "Ready." — a
  whole-screen match for `ready.` passes on stale text, and did.
- **The client's `prt_close()` covers both the success and the abort
  path** (the planned `prt_abort`), including the watchdog's. It is
  safe to call with RX already masked: the mask is a flag, not a
  count, so its `serial_rx_pause()` is a no-op and its resume unmasks
  exactly once.
- **The proxy sends no PRINT_END on an abort.** The client has already
  closed its channel in every case where it NAKs, and if it went
  silent instead, its own watchdog closes it — an END into that would
  be answered by nobody.
- **Proxy job logic was exercised without VICE** before the e2e ran:
  the happy path, multi-block, NAK-on-BEGIN ("No printer on the
  bus"), NAK mid-job, and total silence ("Printer not responding"
  after four BEGINs). Worth rebuilding that stub harness if this
  protocol changes — it turns a 10-minute emulator round trip into a
  2-second one.

## 13. Multi-backend routing: `[printer] backend` (designed 2026-07-23, IMPLEMENTED 2026-07-24)

The owner's coming setup: a Raspberry Pi hidden behind the real C64,
the N80 (§2.4) on its USB, thermal pages emerging next to the machine —
plausibly authentic from two feet away. `/print` then needs to know
whether paper means the IEC bus, the Pi, or both. Design decisions:

**1. Routing lives in the proxy config. The client changes by zero
bytes.** The client implements exactly one backend (IEC device 4) and
its refusal paths; everything else is composition + delivery, which is
already server-side. This matters concretely: resident headroom is
395 bytes (§9) — a client-side selector would spend scarce bytes to
choose between things only the server can reach anyway.

**2. The Pi is a stock CUPS server, not a custom sidecar.** CUPS
already provides the network protocol (IPP), spooling, retries, and
status; the vendor ships armv7l/aarch64 filter builds (§2.4); the
proxy host then needs only `lp` (cups-client), no driver. A bespoke
print daemon would reimplement queue/error handling for no gain. What
we ship is a setup script + doc section, not a service.

**3. Config** (extends the as-built `[printer]` table,
`config.py:101-103`, `config.toml.example:87-93`):

```toml
[printer]
width = 78
formfeed = true
backend = "c64"      # "c64" | "cups" | "both"
cups_queue = ""       # e.g. "n80"; required for cups/both
cups_server = ""      # "" = local cupsd; else "printpi.local:631"
                      # (prefer the mDNS hostname over a hardcoded IP)
cups_options = "cpi=12 lpi=8"   # AS BUILT, not in the original plan:
                      # 78 columns does NOT fit A4 at CUPS's 10 cpi
                      # default (§13.8). Empty = the queue's defaults.
```

`config.py` grows `printer_backend` (validated against the three
values, warn + fall back to `c64` on junk), `printer_cups_queue`,
`printer_cups_server`. Default stays `c64`: a fresh install behaves
exactly as shipped, no CUPS anywhere. As built, `backend` and
`cups_queue` also take env overrides (`LLM64_PRINTER_BACKEND`,
`LLM64_PRINTER_QUEUE`) — the e2e harness runs the proxy against the
operator's real `config.toml`, so it has to pin them (§13.8).

**4. Delivery semantics** (`_print_command` after compose):

- `c64` — PRINT frames exactly as shipped (§4/§6.3).
- `cups` — **no PRINT frames at all**: the composed ASCII doc is piped
  to `lp [-h <cups_server>] -d <cups_queue> -` (CUPS's own text→raster
  chain renders it; Courier at the default 12 cpi holds the 78-col
  wrap on A4 — **wrong, see §13.8**: 10 cpi is the default and 78
  columns does not fit, hence `cups_options`). Run off the reader task
  (`asyncio.to_thread` or a `_spawn_media` task) with a ~20 s timeout;
  success "Sent to the paper printer.", failure "Paper print failed:
  <reason>" (lp missing / nonzero exit / timeout — short on the C64,
  full detail in the log; sent as a REPLY rather than a STATUS, §13.8).
  This mode is also the zero-C64-hardware path: /print works with no
  IEC printer and no VICE flags.
- `both` — compose once, deliver twice, **independently**: the IEC job
  runs as shipped; the cups job runs first (**sequentially, not as a
  parallel task — §13.8**). Each reports its own outcome, cups first so
  the IEC path's final "Printed NN lines." lands last; a refusal or
  failure on either side never aborts the other. The existing
  `/print`-re-entry guard (§12) stays global — one composed job at a
  time covers both deliveries.

**5. The use-case matrix** (what each end user sets):

| Setup | backend | /print lands |
|---|---|---|
| Emulator only (shipped default) | c64 | VICE capture file |
| C64U, no printer hardware | c64 | PNG on /Usb0/printer |
| C64U + real MPS-803 | c64 | dot-matrix paper |
| C64U + N80 on the Pi | both | virtual PNG + thermal page |
| Emulator + N80 | cups | thermal page |
| Proxy host has the N80 on USB | cups (server="") | thermal page |

**6. Printer-day runbook** — the complete sequence for when the N80
arrives. Ship the automatable middle of it as
`tools/setup-printer-pi.sh` (proxy side, never on the C64 disk); the
steps below stand alone even if the script doesn't exist yet.

Prerequisites: the vendor Linux driver, already downloaded and
extracted at `~/Downloads/n80/Linux_ZHJY-N80_driver_v1.0.5/`
(re-downloadable from <https://ndyin.com/pages/download> →
`N80-Driver-Linux-v1.0.5.tar.gz`). Contents: `ppd/ZHJY-N80.ppd`,
`filter/{x86_64,i386,armv7l,aarch64}/rastertoN80`, `install`,
`uninstall`. The `install` script picks the filter arch itself (incl.
32-bit userland on 64-bit Pi kernels), copies it into CUPS's
`ServerBin/filter`, the PPD into the model dir, and restarts CUPS.

On the Pi (or directly on the proxy host — identical except skip the
sharing and leave `cups_server = ""`):

1. `sudo apt install cups cups-client` (Arch: `pacman -S cups`,
   `systemctl enable --now cups`).
2. Copy the extracted driver dir over, `sudo ./install` inside it.
3. Power the N80 ON (it's battery — a dead or sleeping printer
   enumerates as nothing) and connect USB-C. `lpinfo -v` → note the
   `usb://...` URI that appears (expect something with `N80` in it;
   the 1284 ID is `MDL:N80`).
4. `sudo lpadmin -p n80 -E -v '<that usb URI>' -P
   /path/to/ppd/ZHJY-N80.ppd` (or pick the installed PPD from
   `lpinfo -m | grep -i n80`).
5. Local smoke test on the Pi: `echo "hello from the pi" | lp -d n80`
   — a page should print (~30 s at 10 mm/s). `lpstat -o` shows the
   job; `journalctl -u cups` on failure.
6. Share it: `sudo cupsctl --share-printers` and
   `sudo lpadmin -p n80 -o printer-is-shared=true`. If the proxy and
   Pi are on different subnets, also `cupsctl --remote-any` (not
   needed on a flat LAN).
7. From the PROXY host (needs only cups-client for `lp`; no driver):
   `echo "hello from the proxy" | lp -h printpi.local:631 -d n80`.
   If `.local` doesn't resolve, install `avahi-daemon` on the Pi or
   fall back to the IP in the config.
8. Flip the proxy config: `[printer] backend = "both"`,
   `cups_queue = "n80"`, `cups_server = "printpi.local:631"`; restart
   the proxy; `/print` from the C64 → expect the IEC/virtual page AND
   a thermal page, with both STATUS lines on the C64.

Known trouble spots: CUPS vs the `usblp` kernel module rarely fight
over the device on modern distros (CUPS's libusb backend detaches
usblp itself) — if `lpinfo -v` shows nothing with the printer on and
awake, check `dmesg` for the enumeration and try another cable (some
USB-C cables are charge-only). The printer sleeps on idle: first job
after a long gap may need a power-button poke — if pages silently
vanish while `lpstat` shows the job done, check that first, then the
Pi's `journalctl -u cups`.

Steps 1–6 of the above ship as **`tools/setup-printer-pi.sh`** (as
built): `--driver <extracted vendor dir>` runs the vendor installer,
`lpinfo -v` picks the `usb://` URI unless `--uri` overrides it, the
queue is created/reconfigured idempotently, `--test` prints a page
(opt-in — it costs paper), `--dry-run` shows every command and runs
none. apt/pacman/dnf are all handled; the script never touches the C64
side of anything.

Tests for the proxy side stub `lp` on PATH (the §12 stub-harness
lesson) — no hardware in CI. As built that is three layers:
`tests/test_printcups.py` (command line, document byte-for-byte on
stdin, every failure mapped to a short reason, plus the config
fallbacks), `make test-emu-print-cups` (the whole chain with device 4
off the bus — the no-C64-printer path), and `make test-emu-print-both`
(both legs, the configuration a Pi bridge behind a real C64U runs:
the same two documents reach lp *and* device 4). Neither e2e target is
in `test-all`, for the same reason as `test-emu-print-fail` — each is
the full 80-column suite again.

**7. Deferred from this design** (beyond the §11 list):

- **Per-invocation override**: a `/print paper ...` prefix collides
  with natural arguments ("/print paper airplane instructions"). If
  wanted, the right shape is a sticky `/printto c64|paper|both`
  toggle stored in conversation meta — but `both` as the configured
  default likely covers the real desire; wait for actual use.
- **`print_images`**: hardcopy of `/pic` illustrations via `lp` of the
  stored PNG — only meaningful on the cups backend; trivial once
  wanted.
- **Prettier text rendering** (`paps`/`enscript` → PDF for font
  control) if CUPS's stock Courier ever grates.

**8. As built (2026-07-24) — where §13 differs from the plan above.**
Four deltas, two of them bugs the plan walked into:

- **Delivery is a new module, `src/printcups.py`** — `argv()` builds the
  command line, `send()` spawns it and maps every failure to a
  `Result(ok, reason, detail)`. Same reasoning as `printdoc.py` (§12):
  the tests need it importable with no event loop and no protocol stack,
  and `protocol.py` should not grow subprocess handling. `send()` never
  raises: an exception out of the delivery leg would land in a
  `_spawn_media` task, where nothing but the done-callback logger sees
  it.
- **The two legs run sequentially, not as parallel tasks.** §13.4 said
  the cups job runs alongside the IEC one, which contradicts §3: while
  the client prints, it masks serial RX for every `cbm_write`, so the
  proxy must keep the wire silent. A concurrent leg's status frame would
  be dropped at best and land mid-`PRINT_DATA` at worst. Running cups
  first also makes "cups reports first" exact rather than a race, and
  costs almost nothing — `lp` returns as soon as the job is spooled,
  measured at ~1 s per document in the `both` e2e against a stub.
- **The cups outcome is a canned REPLY, not a STATUS** (the plan said
  STATUS). `/print` is the answer to a line the user typed, so the
  client sits in ST_WAITING (the same trap as §12's ST_LOADING bug); the
  IEC leg leaves that state by taking ST_LOADING for the job, but this
  leg sends no frames at all. With a bare STATUS the page spooled
  correctly and the client then waited out its timeout and printed "(no
  response - message may be lost; try again)" — found by
  `make test-emu-print-cups` on its first run. `_send_canned` carries
  CHAT_DONE, which ends the turn, and leaves the outcome in the
  transcript rather than a status row that scrolls away.
- **`cups_options` (default `cpi=12 lpi=8`) exists because §13.4's cpi
  claim was wrong.** CUPS's text filter defaults to **10** cpi, not 12,
  and an A4 page at 10 cpi with default margins holds ~72 columns — the
  78-column document would have wrapped a second time on every long
  line. Explicit `-o` options make the page deterministic instead of
  dependent on the filter's defaults. Empty = the queue's own settings.
- Plus: `backend`/`cups_queue` take `LLM64_PRINTER_BACKEND` /
  `LLM64_PRINTER_QUEUE` env overrides. The e2e runs the proxy with
  `cwd=llm64_proxy/`, i.e. against the operator's real `config.toml`, so
  the harness has to pin the backend — otherwise a live
  `backend = "both"` would spool every test run's pages to real paper.
  `make test-emu-print-cups` uses them with a stub `lp` and device 4 off
  the bus; the other e2e targets pin `c64`.

**9. First live CUPS session (2026-07-24) — three faults, one of them
not ours.** The N80 on a Pi bridge printed, and printed badly: a line or
two per job instead of a page, fragments of different documents arriving
minutes apart and out of order. Three independent causes, worth keeping
apart because only the middle one was a bug in this repo.

- **Fragments, delays, wrong order: the Pi's USB port, not the queue.**
  `dmesg` on the print host: 2041 `over-current change` events and 37
  `usblp0` attach/detach cycles in under two hours. The thermal head's
  peak draw browns out the port, the device drops off the bus
  mid-transfer, and the backend fails — `Unable to send data to
  printer`, `Backend usb returned status 1`. CUPS then retries each
  failed job on a timer, so jobs 5, 7 and 8 took turns pushing a few
  more centimetres of paper every few minutes. Nothing was buffered and
  nothing was out of order: it was the same three jobs being re-sent.
  A Pi 5 caps total USB current at 600 mA unless `usb_max_current_enable=1`
  is set AND the supply advertises a 5 V/5 A PD profile (most 100 W
  bricks offer 100 W only at 20 V and cap 5 V at 3 A). The fix is to
  keep printer draw off the Pi's rail entirely: a powered hub, or the
  printer on its own supply. **`lp` reporting success proves only that
  cupsd accepted the job** — §13.6's debugging list is the whole
  visibility this backend has, and this is exactly the case it warns
  about.
- **A line or two per job: the compose call was wearing the chat
  persona.** `_ask_model` passed `system_prompt=None`, and
  `api_client.stream_chat` reads None as "use the configured one" —
  which on a live deployment is *"You are chatting with a user on a
  Commodore 64 with a 40-column screen. Keep replies short and
  conversational."* Every document was composed by the persona whose
  entire job is brevity. The proxy log shows what that costs: a whole
  story summary composed to 782 characters, a "detailed one-page recipe"
  to 683. Raising `[printer] max_tokens` to 2000 that morning changed
  nothing, because tokens were never the constraint — a strong evidence
  point that a generation budget and a length *instruction* are
  different levers, and the model obeys the instruction. `/print` now
  passes `printdoc.SYSTEM`, which says the opposite in as many words:
  this is paper, not a 40-column screen, and brevity is not a virtue.
  `_ask_model` grew a `system_prompt` parameter; the scene and caption
  calls keep the default (None), because they *are* chat-adjacent and
  want the chat prompt's brevity.
- **Cropped lines: 78 columns is the MPS-803's number, not the roll's.**
  See §13.10.

**10. Two printers, two page widths (`cups_width`).** `[printer] width`
is the IEC printer's line — 78 columns, an MPS-803 on fanfold. An 80 mm
till roll is not that paper: the N80's head is 576 dots at 203 dpi =
72 mm = about **34 columns at 12 cpi**. Sending it a document wrapped at
78 does not re-wrap; the vendor filter gets a raster wider than the head
and the right-hand end of every line is simply gone.

The queue's page size is the other half of the same mistake. The vendor
PPD offers A4, Letter and a custom size, and A4 is the default — so
every job was rendered 210 mm wide for a 72 mm head:

```
*DefaultPageSize: w595h842      # 210mm
*DefaultResolution: 203dpi      # head = 576 dots = 72mm
```

So the composed document is now laid out **once per backend** rather
than once per job: `_print_job` takes `(title, body)` and calls
`printdoc.finish` with `printer_width` for the IEC leg and
`printer_cups_width` for the CUPS leg. Same text, two wraps.
`cups_width = 0` (the default) means "share `width`", which is right
when the CUPS queue is an ordinary A4 printer — the roll case is the
one that has to say so. For an 80 mm roll:

```toml
cups_width = 34
cups_options = "cpi=12 lpi=8 PageSize=Custom.204x842"
cups_feed_lines = 5
```

`cups_feed_lines` is the tear-off: a roll printer stops with its last
line still inside the mechanism, below the tear bar, so tearing takes
the end of your own document with it. Five blank lines at `lpi=8` is
~16 mm, the usual head-to-bar gap. They go on the wire in
`printcups.send`, not into the composed document — the IEC leg prints
the same text and ejects its own way (`formfeed`). Default 0: a page
printer ejects on its own, and trailing blanks near a page boundary
would cost it a second sheet. If a driver trims trailing blank raster
lines (some receipt drivers do), set the custom page height instead so
the page itself ends past the bar.
