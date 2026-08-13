# 16 - A Windows 3.11 client for LLM64

*Investigation, 2026-07-27, with the Phase 0 spike built the same day.
Sections 0-9 are the survey; section 10 records what has since been
measured. The client lives in `win311_client/`, beside `c64_client/`,
and shares the proxy.*

## 0. Verdict

**The proxy is already the whole application.** ~15,000 lines of Python
own the conversation, the modes, the adventure, the map, dice, character
generation, history and search, save/restore, image generation, the
10,000-tune music library, and the print composition. None of it knows
what a C64 is except at the last inch -- the egress encoder.

**The C64 client is not portable, and almost none of it needs to be.**
Of its ~7,000 lines of C and 6502 assembly, roughly 5,500 exist *only*
because the target is a 64 KB machine with a 6551 ACIA: the soft-80
bitmap renderer, the SID player, the IRQ serial driver, the keyboard
matrix scanner, the overlay-module loader, PETSCII translation. Windows
3.11 supplies all of that in the OS. What survives the trip is the frame
parser (`protocol.c`, ~245 lines, ports nearly 1:1) and the *logic* of
the transfer state machines in `main.c` -- reusable as a specification,
not as source.

**Estimate: ~3,500–5,000 lines of new Win16 C plus resources for the
client; ~400–700 lines of change in the proxy.** The proxy changes are
all additive and backwards-compatible; the C64 keeps working untouched.

**The transport question has a clean answer: Winsock 1.1.** No modem, no
serial link, no named pipe. The proxy is a plain TCP server
(`tcp_server.py`) -- the modem only ever existed because the C64 has no
TCP stack. A Win16 app opens a socket and speaks the identical byte
protocol. On WfW 3.11 that means Microsoft's TCP/IP-32 add-on (or
Trumpet Winsock on plain 3.1), both period-correct.

**Wine works -- measured, not assumed.** See §9.3: Wine 11.6 on this box
loads `winevdm.exe`, `krnl386.exe16`, `user.exe16`, `gdi.exe16`,
`mmsystem.dll16` and runs a 16-bit NE binary. Wine also ships
`winsock.dll16` and `ctl3d.dll16` (untested here).

**Best early win:** images need *zero* proxy changes. The client can
decode the existing 10,001-byte C64 multicolor blob into a DIB itself
(~80 lines) and display it in a modal window with the Pepto palette --
authentically wrong-looking, and free.

**Music is MIDI, not SID.** A SID tune is a 6502 memory image; there is
no plausible way to run one on a 486, and a chiptune from a machine this
program is not running on is not period-correct anyway. What a 1993
Windows program played was a `.MID` file through the MIDI Mapper -- which
also means the client does no audio work at all beyond handing the file
to MCI, and the choice of synthesis (FM, MT-32, Sound Canvas, a modern
SoundFont under Wine) belongs to the machine, not to us. See §6.2.

**The proxy becomes multi-client rather than C64-with-exceptions.** One
`CLIENT_HELLO` selects a *profile* -- `c64`, `win16`, later `dos` -- and
the profile owns text width, image format and art-style template, music
format and library, printing sink, and the payload/pacing limits. Every
C64 concession in the code today becomes a value on the C64 profile
instead of a constant. See §7.

---

## 1. Where the seam already is

```
   C64 client                    │  wire  │        proxy
   ───────────────────────────── │ ────── │ ─────────────────────────
   scrollback, wrapping, editor  │        │  conversation + history
   keyboard, screen, colors     │ framed │  modes / adventure / map
   SID playback, bitmap display  │ binary │  LLM streaming, dice
   IEC printer channel           │ frames │  image gen + C64 conversion
                                 │        │  SID library + relocation
                                 │        │  print composition, CUPS
```

Everything left of the wire is display technology. Everything right of
it is the product. A second client is a second left-hand column.

Three facts make the port unusually cheap:

1. **The wire carries ASCII, not PETSCII.** `markup.py:colorize_for_wire`
   emits 7-bit ASCII with in-band single-byte marker cells; anything
   ≥ 0x80 becomes `?`, and Unicode punctuation is folded to ASCII first.
   The C64 client converts PETSCII → ASCII on send (`protocol.c:204`).
   A Windows client needs no charset layer at all beyond CP1252 → ASCII.
2. **The proxy does not wrap text.** It streams unwrapped prose and lets
   the client word-wrap (`ui.h`, `chat_append_ascii`). The one exception
   is the adventure map, drawn to `MAP_WIDTH = 78`, and `/print`, laid
   out to `printer_width`. So a Windows client can be any width it likes
   for free.
3. **The color model is a byte stream, not an escape language.**
   `0x01` = close, `0x02`/`0x03` = bold on/off, `0x10|c` = color *c*
   (1..14). Trivial to interpret in a `WM_PAINT` handler.

---

## 2. The protocol, exactly as it stands

Both directions, unchanged since the C64's constraints shaped it:

```
┌──────┬──────┬───────────────┬─────────┬─────┐
│ SYNC │ TYPE │ LEN lo, LEN hi│ PAYLOAD │ CRC │
│ 0x42 │  1B  │ each +0x20    │  N B    │ 1B  │
└──────┴──────┴───────────────┴─────────┴─────┘
```

- `SYNC` = `0x42` (`'B'`) -- chosen because VICE's IP232 mangled `0xC6`.
- `LEN` is little-endian, **each byte biased by +0x20**, decoded with
  uint8 wrap-around (`protocol.py:222`). Easy to get wrong; it is the
  one genuinely surprising thing on the wire.
- `CRC` = XOR of type ^ len_lo ^ len_hi ^ every payload byte.
- Type codes are deliberately printable ASCII (`common.h:99`), same
  reason.
- Payload cap: proxy refuses to send > 512 bytes because that is the
  client's buffer (`protocol.py:381`); parser accepts up to 2048.

Message inventory (`common.h:99-164` / `protocol.py:33`):

| Direction | Messages |
|---|---|
| → proxy | CHAT_REQUEST, CANCEL, LIST/LOAD/NEW/DELETE/STAR_CONVERSATION, PING, LIST_MODELS, SET_MODEL, GET_MENU, GET_NOWPLAYING, FAV_TUNE, SET_BAUD, ACK, NAK |
| → client | CHAT_CHUNK, CHAT_DONE, CHAT_ERROR, STATUS, NOTICE, CONVERSATION_LIST, CONVERSATION_DATA, MODEL_LIST, MENU_LIST, HINT, NOWPLAYING, MUSIC_STOP, SID_BEGIN/DATA/END, IMG_BEGIN/DATA/END, PRINT_BEGIN/DATA/END, ACK, NAK |

Bulk transfers (SID, image) have a handshake worth keeping even on a
fast link, because it is also the *rendering* handshake: `_send_begin`
waits for the client's ACK -- which means "I have silenced music and
finished drawing, start sending" -- then `_send_bulk_stream` sends
256-byte offset-tagged chunks and pauses every `FLOW_WINDOW = 4` frames
for an ACK (`protocol.py:1033-1074`). On TCP the flow control is
redundant but harmless; the offset tag makes each chunk self-placing.

**C64-isms a Windows client should negotiate away, not inherit:**

| Concession | Why it exists | Windows |
|---|---|---|
| 512-byte payload cap | client RAM | negotiate 4 KB |
| `chunk_pace_base/per_byte` sleeps | 9600–38400 baud wire | zero |
| `SET_BAUD` | proxy paces to the wire | send once, or skip |
| 256-byte bulk chunks + flow window | C64U modem drops burst tails | keep BEGIN handshake, widen window |
| SID as relocated 6502 memory image | there is a SID chip | needs MIDI (§6.2) |
| Image as 10,001-byte C64 blob | there is a VIC-II | decode it, or ask for DIB (§6.1) |

---

## 3. Port matrix -- the C64 client, file by file

| File | LOC | Verdict |
|---|---:|---|
| `protocol.c` + `crc.s` | 300 | **Port ~1:1.** The frame state machine is the one piece of real reuse. |
| `main.c` | 1573 | **Rewrite, keep the design.** The message dispatch (`case MSG_*`, lines 679-1100) and the SID/IMG/PRINT transfer state machines are the specification for the Windows equivalents. Maybe 60% survives as pseudocode, 0% as source. |
| `serial.s` | 800 | **Delete.** ACIA registers, IRQ/NMI ring buffers, Hayes dialling, baud switching. Replaced by ~250 lines of Winsock. |
| `soft80.s`, `font48.s`, `colorize.s`, `display.c` | 1600 | **Delete.** Bitmap 80-column renderer, 4×8 font, color-matrix painter, screen-code scrollback. Replaced by a GDI text pane (~600 lines). The *scrollback data structure* (pre-wrapped lines + per-line color) is worth copying wholesale. |
| `keyboard.s` | 342 | **Delete.** Hand-rolled matrix scan for n-key rollover; Windows gives you `WM_CHAR`. |
| `music.s` | 137 | **Delete.** 6502 SID player driven from the raster IRQ. |
| `editor.c` | 190 | **Replace** with a multiline `EDIT` control, subclassed for Return-sends and Ctrl-A/E/K/D (~120 lines). |
| `loader.c`, `modslot.s`, `append.s` | 140 | **Delete.** The overlay-module system exists purely because the program does not fit in RAM. Win16 does the same trick in the linker: `MOVEABLE DISCARDABLE` code segments. |
| `mod_menu.c` | 306 | **Replace** with the real menu bar + accelerators, populated at runtime from `MENU_LIST` (~150 lines). Keep the protocol: a server-fed menu is a good idea on any platform. |
| `mod_convmgr.c` | 262 | **Replace** with a `DialogBox` + `LISTBOX` (~200 lines). |
| `mod_sound.c` | 456 | **Replace** with a jukebox dialog (~200 lines); `NOWPLAYING`/`FAV_TUNE`/`MUSIC_STOP` port unchanged. |
| `mod_config.c` | 179 | **Replace** with a settings dialog (~150 lines). |
| `cfg.c` | 130 | **Replace** with `GetPrivateProfileString` on `LLM64.INI` -- period-correct and about 60 lines. |
| `mod_diskcopy.c` | 192 | **Delete.** "Copy myself to a blank disk" is a C64 problem. |
| `text.c`, `diag.*`, `debug_main.c` | 870 | **Delete / optional.** |

Net: ~5,500 of ~7,000 lines evaporate. What replaces them is Windows
boilerplate plus roughly 3,500–5,000 lines of new C.

---

## 4. Transport

### 4.1 Winsock (recommended)

The proxy is a bare `asyncio.start_server` on port 6400. Nothing about
the protocol assumes a modem -- the modem is the C64's TCP stack. So:

- **WfW 3.11:** Microsoft TCP/IP-32 (free add-on, 1994) installs a 32-bit
  VxD stack and a 16-bit `WINSOCK.DLL`. Requires WfW specifically, not
  plain 3.1, and an NDIS driver for the NIC.
- **Plain Windows 3.1:** Trumpet Winsock 2.x over a packet driver, SLIP
  or PPP.
- **Wine:** ships `winsock.dll16` (untested here -- see §12).

The API shape matters, and it happens to suit this application
perfectly. Win16 is cooperatively multitasked: a blocking `recv()`
freezes *the entire operating system*. The correct pattern is
`WSAAsyncSelect(s, hwnd, WM_SOCKET, FD_CONNECT|FD_READ|FD_CLOSE)`, which
posts a window message when bytes arrive. Every byte then enters the
frame parser from a message handler -- which is *structurally the same
program* as the C64's "drain the ring buffer each pass of the main
loop", just with Windows owning the loop. `FD_WRITE` gives the same
back-pressure the ACIA's TDRE bit did.

### 4.2 Alternatives, and why not

- **Serial via `COMM.DRV`** (`OpenComm`/`ReadComm`/`WriteComm`): works,
  and under a VM you can wire COM1 to a host pipe or TCP socket. Worth
  keeping as a fallback for a machine with no network card -- it is
  strictly a subset of what the C64 already does, so the proxy needs
  nothing. Real hardware could even use the *same* Hayes path.
- **Named pipes:** a Win16 client cannot; the redirector is server-side
  (LAN Manager). Not a route.
- **DDE / NetDDE:** would require a Windows-side helper. Absurd, and it
  moves the problem rather than solving it.
- **A dumb terminal (TERMINAL.EXE over the modem):** the protocol is
  binary and framed. Non-starter without a proxy line-mode, which is a
  different product.

---

## 5. The UI, in the period toolkit

The point of the exercise is that it should look like it shipped in 1993.

**The application is MDI** -- one top-level frame window holding document
windows, rather than a scatter of top-levels. That was briefly the
fashion in the other direction, and MDI is both the period-correct
answer and the one that aged better: Word, Excel, File Manager and
Program Manager all worked this way. It also decides where the later
windows go -- the picture viewer, jukebox and conversation manager become
documents in the workspace rather than free-floating windows, and the
Window menu lists them. Structurally it costs `DefFrameProc` and
`DefMDIChildProc` in place of `DefWindowProc`, a `MDICLIENT` between the
frame and its documents, and `TranslateMDISysAccel` in the message loop.

The split is: the *frame* owns the menu bar, the status strip and the
socket, because those are the application's; a *document* owns a
transcript pane and an input box. The transcript itself belongs to
neither -- it is application state, so closing every window on it loses
nothing and a reply that arrives while no window is open is still there
when one is opened again.

**Frame window** -- `WNDCLASS` with a menu bar, sizeable frame, an
`ICON`, and `CTL3D.DLL` for the sunken-border look that every serious
3.1 app used.

- **Menu bar** replaces the F1 panel: `&File` (New, Open Conversation…,
  Save, Print…, Print Setup…, Exit), `&Edit` (Cut/Copy/Paste on the
  input box, Find… , Find in All…), `&Mode` (Chat, Adventure, Character…,
  Assistant, Claude Code), `&Media` (Illustrate, Pictures…, Jukebox…,
  Stop Music), `&Settings` (Server…, Model…, Fonts, Colors), `&Help`
  (Commands, About). The proxy's `MENU_LIST` entries append to `&Mode`
  at runtime, so a server-side menu change still needs no client rebuild.
- **Output pane** -- *not* an `EDIT` control. The stock multiline edit
  tops out in the tens of kilobytes, repaints badly, and cannot do
  per-run color. Own the pane: a child window, a fixed-pitch font
  (`Terminal`/`FixedSys`, or ship a bitmap font), `TextOut` per run
  between color markers, a real scroll bar via `SetScrollRange`/
  `SetScrollPos`, `WM_MOUSEWHEEL` doesn't exist yet so PgUp/PgDn and the
  bar are the controls. This is the same job `display.c` already does,
  minus the pain.
- **Input pane** -- a 3-line multiline `EDIT` at the bottom, subclassed so
  Return sends and Shift+Return newlines. Ctrl-A/E/K/D can stay for
  muscle memory.
- **Status bar** -- hand-drawn strip: left = status text, right = the
  proxy-composed chrome from the `HINT` frame (place, now-playing), plus
  the `!P` / `PIC:n` tally. The proxy already composes this into 40
  characters (`CHROME_MAX`); a wider client can simply show it as-is.

**Modal windows**

- **Picture viewer** -- `DialogBox` with a client area, `StretchDIBits`,
  the caption in the title bar, OK/Save As…/Print. On a 256-color
  driver, build a `LOGPALETTE` from the Pepto values in `imaging.py` and
  `RealizePalette`; on a 16-color VGA driver, GDI dithers to the fixed
  VGA palette and it looks even more like 1993.
- **Conversation manager** -- `LISTBOX` (`LBS_OWNERDRAWFIXED` for the
  star), Load/Delete/Star/Close, paging identical to `mod_convmgr.c`.
- **Jukebox** -- listbox of moods/tunes, Play/Stop/Favorite, driven by
  `GET_NOWPLAYING` and `MENU_LIST`.
- **Settings** -- server IP/port, model (from `MODEL_LIST`), fonts,
  colors; persisted to `LLM64.INI`.
- **Help / About** -- the About box with a bitmap is obligatory.

**Printing** -- `File > Print...` opens `PrintDlg` from `COMMDLG` and the
document goes out through Print Manager. It needs no new protocol at
all: see §8.

---

## 6. Media

### 6.1 Images -- three paths, in cost order

`IMG_BEGIN` already carries a format byte (1 = multicolor, 0 = hires) and
a background color. The multicolor blob is 8000 bytes bitmap + 1000
screen + 1000 color + bg = 10,001 bytes at 160×200 in the Pepto
palette.

- **Path A (zero proxy change):** decode the blob client-side into a
  256-color DIB and blit it. ~80 lines. You get the exact C64 image,
  chunky pixels, burned-in caption and all. Because the transfer already
  works and is already flow-controlled, this can ship on day one.
- **Path B (optional upgrade):** the proxy keeps the *original* PNG for
  every generated image (`data/images/<conv>/<epoch>.png`). Add
  `fmt = 2` meaning "a Windows DIB follows", and have Pillow emit a
  256-color BMP at, say, 640×400. Win16 cannot decode PNG; it decodes
  DIBs natively. ~100 lines of Python, no new client code beyond
  `StretchDIBits`.

- **Path C (the right long-term answer):** the *profile* picks both the
  art prompt and the format. A C64 scene is prompted for "flat color
  areas, strong silhouettes, 16-color palette" because that is what
  survives the converter; a 1993 PC scene wants "256-color VGA pixel
  art, dithered, 320×200 DOS adventure game art", which is a different
  picture, not a different encoding of the same one. `images.py` already
  has the hook -- `DEFAULT_STYLE_PREFIX` and `[images].style_prefix` --
  it just needs to be per-profile instead of global (§7).

Path A is more in the spirit of the thing and ships first; Path C is
what makes a Windows client feel like it belongs to its own era rather
than like a C64 emulator with better fonts.

One real tradeoff to decide: an image is generated once and its original
PNG is retained, so a second client can re-*convert* the same artwork
cheaply. But if the art prompt is per-profile, the same scene wants two
*different* generations. Recommendation: generate for the profile that
asked, retain the original, and convert on demand for anyone else --
regenerating per profile only if the operator turns it on. Consistency
across clients in a shared world is worth more than per-platform art
purity, and it is one flag either way.

### 6.2 Music -- MIDI

Not SID. The C64 receives a relocated 6502 memory image and runs its
play routine off the raster IRQ; a 486 has no SID and emulating a 6510
inside a 16-bit cooperatively-multitasked app is not a weekend. More to
the point, a SID tune is not what this machine would have played. In
1993 a Windows program played a `.MID` file through the MIDI Mapper.

This turns the hardest media problem into the easiest one:

- **Wire:** `MIDI_BEGIN` / `MIDI_DATA` / `MIDI_END` (free type codes
  `0x65`–`0x67`), reusing the existing offset-tagged bulk stream.
  `MIDI_BEGIN` carries title, author, mood and length. A MIDI file is
  5–50 KB -- one transfer, not a stream, and nothing to starve.
- **Client:** write it to a temp file and hand it to MCI
  (`mciSendCommand` with `MCI_OPEN` on the `sequencer` device, then
  `MCI_PLAY` with `MCI_NOTIFY` so `MM_MCINOTIFY` can loop or advance
  it). ~120 lines and no DSP, no double-buffering, no starvation when
  the user holds a menu open.
- **Synthesis is the machine's business, not ours.** The Windows 3.1
  MIDI Mapper routes to whatever is installed: OPL2/OPL3 FM on an Adlib
  or Sound Blaster, an MT-32 or SC-55 on the MPU-401 port, or -- under
  Wine -- the host's ALSA sequencer, which means FluidSynth with a
  SoundFont, Munt for authentic MT-32, or an OPL3 emulator. "Various
  emulation options" costs the client exactly zero lines: it is a
  control-panel setting on real hardware and an ALSA connection under
  Wine. Worth documenting for the user; not worth coding.
- **Library:** mirror the SID pipeline rather than replacing it. A
  mood-tagged General MIDI corpus and a `MidiLibrary` with the same
  interface `MusicLibrary` has, so the narrator's `[[MUSIC: mood]]`
  directive, the mood vocabulary, the jukebox and the ranking logic are
  all unchanged. The C64 profile resolves a mood to a SID, the Windows
  profile to a MIDI. The tagging tools (`sid_mood.py`'s LLM tagger,
  `sid_rank.py`) transfer almost verbatim -- they read titles and
  metadata, not audio.
- **Optional continuity:** SID→MIDI conversion exists and is lossy. It
  would let both clients hear "the same" tune in a shared world. File
  under nice-to-have, well behind having a good MIDI corpus at all.
- **No sound card:** MIDI silently no-ops. The feature is already
  optional everywhere it appears.

### 6.3 Fonts and color

The color markers map to the C64 palette; on Windows you can either
match Pepto exactly (nice on a 256-color driver) or snap to the 16 VGA
colors (nice on a 16-color driver, and more authentically Windows).
Make it a setting. A period-correct default: `Terminal` at 8×12, C64
colors, black background -- an obvious fake-C64 look -- versus `FixedSys`
on white, which looks like a 1993 business app. Both are one `CreateFont`
call apart.

---

## 7. The proxy becomes multi-client

The change worth making is not "add a Windows path". It is to stop
treating the C64 as the default and every other machine as an exception:
**introduce a client profile, and move every C64 concession onto it.**

```python
class ClientProfile:
    name          = 'c64'      # 'c64' | 'win16' | 'dos' (later)
    text_width    = 80         # what /map and /print lay out to
    max_payload   = 512        # protocol.py's hard cap today
    pace          = True       # chunk_pace_*, bulk pacing, flow window
    image_fmt     = 'c64mc'    # 'c64mc' | 'c64hires' | 'dib8'
    image_style   = C64_STYLE  # the art prompt, not just the encoding
    music_fmt     = 'sid'      # 'sid' | 'midi' | None
    music_lib     = sid_lib    # mood -> tune, same interface either way
    print_sink    = 'iec'      # 'iec' | 'client_gdi' | 'cups'
```

Everything downstream reads the profile instead of a constant. Nothing
about the conversation, the modes, the adventure, the map or the dice
changes at all -- the profile only reaches the egress edge, which is
where the C64-shaped decisions already live.

The changes, all additive; an old C64 client must not notice:

1. **`CLIENT_HELLO` (`0x3F`, `'?'`)** -- first frame after connect:
   version, profile name, capability bits, text width, max payload.
   Absence of it means the `c64` profile, so the existing client is
   unaffected. Unknown message types are already logged and ignored
   (`protocol.py:355`), so a new client also degrades gracefully
   against an old proxy. ≈ 100 lines.
2. **Lift the caps from the profile** -- `send_message`'s hard 512-byte
   refusal (`protocol.py:381`), `chunk_pace_*`, `bulk_pace_per_byte`,
   `FLOW_WINDOW`. Keep the BEGIN handshake even for a fast client: it
   is a rendering barrier as much as a flow-control one. ≈ 60 lines.
3. **Per-profile images** -- `IMG_BEGIN fmt = 2` for a DIB, and the art
   prompt moves from `images.py`'s module-level `DEFAULT_STYLE_PREFIX`
   to the profile (§6.1). ≈ 150 lines.
4. **`MIDI_BEGIN/DATA/END` and a `MidiLibrary`** -- §6.2. The mood
   vocabulary, selection and jukebox are shared; only the resolver and
   the payload differ. ≈ 250 lines plus a tagged corpus.
5. **Width plumbing** -- `MAP_WIDTH = 78` and `printer_width` come from
   the profile. `advmap.render_ascii(m, width=...)` and
   `_body_at(body, width)` already take a width, so this is threading a
   parameter rather than new code. ≈ 60 lines.
6. **Print routing** -- §8. ≈ 80 lines.

Call it 700 lines including tests, against a suite already factored
along exactly these seams (`llm64_proxy/tests/`, 13 modules).

**The payoff is not just Windows.** Once the profile exists, a DOS
client, a terminal, or a web client is a table entry plus a renderer --
and two clients can share one world (§13).

## 8. Printing

Printing is the feature that turns out to need **no new wire messages at
all**, because the existing ones are already the right shape.

`/print` composes a document server-side and ships it as
`PRINT_BEGIN` → N × `PRINT_DATA` (blocks of ASCII, ≤240 bytes, ACKed
one at a time) → `PRINT_END`. The C64 writes those blocks to IEC device
4. A Windows client writes the same blocks to a printer DC. Only the
sink differs:

```
PRINT_BEGIN  -> PrintDlg (COMMDLG) or the default printer
                Escape(hdc, STARTDOC)
PRINT_DATA   -> TextOut per line in a fixed-pitch font;
                Escape(hdc, NEWFRAME) at the page break
PRINT_END    -> Escape(hdc, ENDDOC)
```

The `flags` byte already carries "business charset" and "form feed
before close"; for a GDI sink the first is meaningless and the second is
a page eject, so the semantics survive translation.

**Paper before a printer.** The first sink built is not a printer DC at
all: it is a document window. `PRINT_BEGIN`/`DATA`/`END` are caught, the
blocks ACKed exactly as the C64 ACKs them, and the composed document
opened as a sheet in the workspace -- up to four on the desk at once. It
needs no printer installed, no driver, and no proxy change, and on a
1993 machine it is what Print Preview was. A real `PrintDlg` and printer
DC then become a second sink for a document already in hand, rather than
the only way to see what `/print` produced.

Details worth getting right:

- **Width.** The proxy lays a document out to `printer_width` (78 by
  default) because a C64 printer is a fixed-pitch dot matrix. A Windows
  client knows its real usable width only after it has chosen a printer
  and a font, so the width belongs in `CLIENT_HELLO` (or per job, once
  a printer is chosen). The composition path is already width-taking.
- **Pictures.** `/print <picture>` on the C64 goes through
  `printpic.py`'s bitmap path. On Windows it is `StretchDIBits` onto the
  printer DC, scaled to the page -- one call, and it can print the
  *original* rather than the 160×200 conversion.
- **The CUPS backend stays useful.** A machine with no printer driver
  installed can still ask the proxy to spool it (`printcups.py`), which
  is also how the Raspberry Pi thermal printer already works. A profile
  picks the sink; nothing else changes.
- **Period accuracy is free here.** Windows 3.1 shipped GDI drivers for
  the Epson FX/LQ and IBM ProPrinter families, so a real dot matrix on
  LPT1 prints from Print Manager exactly as it would have in 1993.

---

## 9. Toolchain, and where the binary runs

### 9.1 Building Win16 from Linux

**Open Watcom V2** is the practical answer: actively maintained, hosts
on Linux, and still targets Win16 (`wcl -bt=windows -l=windows`,
`wrc` for resources). Not in the Arch repos (AUR / upstream binary
tarball), unlike `mingw-w64-gcc` which is (`extra/mingw-w64-gcc`).

Authentic alternatives, both needing DOSBox (installed) or a VM (`qemu`
installed): **Borland C++ 3.1 / 4.52** or **MSVC 1.52c**. Borland is the
nicer period experience; Watcom is the one that fits a Makefile in this
repo.

### 9.2 The dual-target trick

Write the source Win16-first with disciplined types (`HANDLE`, no
pointer arithmetic across 64 KB, `WORD`/`LONG` not `int`) and it also
compiles as **Win32 with mingw-w64**. That gives:

- `LLM311.EXE` -- 16-bit NE, runs on real WfW 3.11, in a VM, and under
  Wine's Win16 layer.
- `LLM32.EXE` -- 32-bit PE, runs natively on modern Windows and cleanly
  under Wine, no 16-bit subsystem required.

Same UI code, same protocol code, same look (the Win32 build still uses
the 3.1-era controls; it just isn't crippled by segments). This is also
the insurance policy against §12's Wine risk.

Note the trap: **Win32s** (running 32-bit PEs *on* 3.11) is not a third
target worth having. Modern mingw output will not run on Win32s -- that
needs MSVC 2.0-era tooling -- and Win32s was flaky in its own lifetime.
Win16 or a real 32-bit Windows, nothing in between.

### 9.3 Wine -- measured on this machine

Wine 11.6 (Arch, new-WoW64 mode). Running a 16-bit builtin:

```
WINEDEBUG=+loaddll wine winhelp.exe
  ...syswow64\winevdm.exe : builtin
  ...system32\krnl386.exe16 : builtin
  ...system32\system.drv16, comm.drv16, gdi.exe16, user.exe16
  ...system32\display.drv16, keyboard.drv16, mouse.drv16
  ...system32\mmsystem.dll16, sound.drv16
  MODULE_LoadModule16 Loaded module "winhelp.exe" : builtin
```

So the 16-bit subsystem loads and executes here despite new-WoW64. The
tree also contains `winsock.dll16`, `ctl3d.dll16`, `commdlg.dll16`,
`ver.dll16`, `wing.dll16` -- every DLL this design would touch. What is
**not** yet verified is that `winsock.dll16` actually connects; that is
the first thing the spike in §11 proved.

For real-machine fidelity: `qemu-system-i386` is installed and WfW 3.11
+ TCP/IP-32 over an NE2000 with SLIRP is a well-trodden setup; 86Box or
PCem give a more convincing 486. On modern 64-bit Windows the 16-bit
build needs **otvdm/winevdm** -- or you just ship `LLM32.EXE`.

**Windows 95/98 run this binary with nothing added.** They keep a full
Win16 subsystem, and -- unlike 3.1 -- ship a 16-bit `WINSOCK.DLL` as part
of the OS, so the client needs only that TCP/IP is bound to the adapter.
That makes a Win95 VM the cheapest real-machine test there is, at the
cost of the one thing the exercise is about: the app wears Win95 chrome,
so it stops looking like 1993 and starts looking like 1995. Good for
proving the protocol on real hardware-ish; not the fidelity target.
`make floppy` builds a 1.44 MB image with the EXE and an INI on it,
since a VM has no command line to pass the host and port on.

---

## 10. Testing

`emu/` has no analogue, but two pieces port straight across, and both
are now in use:

- **`emu/mock_llm.py`** -- the deterministic fake model already used by
  the e2e suite works for any client; it is upstream of the wire.
  `win311_client/tools/devproxy.sh` wires it to a real proxy on a
  scratch port with a scratch data dir. No VICE, no GPU.
- **The harness shape** -- `test_e2e.py` drives VICE and reads the screen
  through the binary monitor. `win311_client/tools/wine_smoke.sh` is the
  same idea one level up: launch under Wine, find the window by its VDM
  class, type, screenshot.

The third piece is new and the most valuable: **the framing is testable
on the host.** `wire.c` has no Windows in it, so
`win311_client/tests/test_wire.c` compiles with `cc` and asserts the
protocol -- including the +0x20 wrap case -- in milliseconds, with no
emulator anywhere in the loop.

---

## 11. Phasing

| Phase | Deliverable | Proves |
|---|---|---|
| **0 -- spike** ✅ | Win16 app: connect, `PING`, streamed chat, color markers, menu bar, status strip | the *whole* toolchain risk (see below) |
| **1 -- MVP** ◐ | Far-memory scrollback with re-flow ✅, bold ✅, MDI frame and document windows ✅; editor keys, `HINT` chrome, `MENU_LIST`, help still to do | it is a usable client |
| **2 -- dialogs** ◐ | Server settings ✅; conversation manager, model picker, fonts/colors, find/history still to do | feature parity with the F-keys |
| **3 -- pictures** | Blob → DIB modal viewer (Path A, no proxy change) | media transfer end-to-end |
| **4 -- profiles** | `CLIENT_HELLO`, the proxy's `ClientProfile`, negotiated widths and payloads, per-profile art (§7) | the proxy is multi-client, not C64-with-exceptions |
| **5 -- MIDI** | `MIDI_*` frames, a mood-tagged GM corpus, MCI playback, jukebox dialog | the era's music, and the last new subsystem |
| **6 -- printing** ◐ | `PRINT_*` caught in a paper document window ✅; a GDI printer DC via `PrintDlg` (§8) still to do | hardcopy, with no new wire messages |
| **7 -- polish** | CTL3D, icon, About, Win32 build, installer | it looks like it shipped |

### What Phase 0 actually measured

Built and run on 2026-07-27, on this machine:

- **Open Watcom V2** (Linux-hosted snapshot) compiles and links a Win16
  NE binary: `build/LLM64.EXE`, ~9 KB, `NE version 5 for MS Windows 3.10`,
  menu resource bound with `wrc`.
- **It runs under Wine 11.6** in new-WoW64 mode, via `winevdm.exe` and
  the 16-bit module set.
- **`winsock.dll16` works** -- this was the open risk in the survey. The
  client connects to a real proxy, and the proxy logs
  `Received: PING (length=0)` with a valid CRC and answers `ACK`.
- **A full chat round trip renders**: typed line → `CHAT_REQUEST` →
  streamed `CHAT_CHUNK`s → `CHAT_DONE`, word-wrapped in the pane.
- **The color markers render as color** -- the in-band `0x10|c` cells
  drawn from the Pepto palette, which is the one design claim in §1 that
  was worth seeing rather than believing.
- **The wire unit test passes on the host**, +0x20 wrap case included.

Two bugs found and fixed in the doing, both of the kind only a running
program surfaces: a line's base color was stamped at line creation, so
the first chunk of a reply inherited the *user's* color; and the
startup banner was written before the pane had been sized, so it wrapped
at the placeholder width of 10 columns.

### What Phase 1's scrollback measured

Built and run on 2026-07-27, on the second machine (Bazzite; Watcom in
`~/Programs`, Wine 11.0 Staging inside the `my-distrobox` toolbox):

- **The transcript is out of DGROUP and re-flows.** `src/scroll.c` keeps
  unwrapped logical lines in `malloc`'d far blocks -- under the large
  memory model that is the far heap, which is the global memory
  `GlobalAlloc` hands out -- and wraps them at paint time through a single
  iterator. Resizing the window re-lays out text already on screen,
  which the Phase 0 fixed array could not do at all. 64 KB of text in 8
  recycled blocks, 512 logical lines, against 32 KB of DGROUP before.
- **`tests/test_scroll.c` pins it on the host**, alongside the wire test:
  33 assertions covering re-flow in both directions, marker-aware
  wrapping, eviction, the row cursor agreeing with the cached row count,
  and the over-long-line seam. No Wine, no proxy, no emulator.
- **Verified under Wine at scale**: four `LONGTEST` replies (2760 chars
  each, no newline in them) spill the transcript past one arena block;
  narrowing, widening, and paging to the top and back all hold.
- **Bold markers now render in a bold face**, built from the fixed
  system font's `LOGFONT` and rejected if it does not keep the cell
  width.

Three bugs of the kind only this exercise surfaces:

1. **The painter read one byte past a row.** Rows are slices of an arena
   block, not NUL-terminated arrays, and in protected mode reading past
   the end of a segment is a fault rather than a stray character. The
   Phase 0 code got away with the same loop because its lines *were*
   terminated arrays.
2. **The wrap counted bytes, not cells**, so any line carrying color
   markers broke early -- invisible in the spike because the only marked
   line was short.
3. **The over-long-line seam cut mid-word.** `SB_MAX_LINE` has to break
   somewhere; it now backs up to the last space, so the seam costs one
   short row and never half a word.

And two in the harness, both of which had been quietly weakening it:
`xdotool type --window` is `XSendEvent`, which the 16-bit VDM mostly
drops -- the message arrived at the proxy as its first word only -- and a
headless Xvfb with no window manager never produces a `WM_SIZE`, so a
resize test on it proves nothing. See the README.

### What the MDI restructure measured

Verified under Wine on the same day: the menu bar merges the maximized
document's system menu and buttons, the Window menu lists the open
document and its Cascade and Tile work, focus follows `WM_MDIACTIVATE`
into the document's input box, and the transcript re-flows to a restored
child's narrower width exactly as it does to the frame's.

Closing a document is the case worth having tested. It leaves an empty
workspace -- correct for MDI -- and it used to leave `g_pane` and
`g_input` naming windows that no longer existed. They are now cleared on
`WM_DESTROY` and every use tolerates their absence, so a reply streaming
in with no window open is appended rather than painted into nothing;
Window > New Conversation Window opens onto it with the scrollback
intact. Confirmed end to end: close, chat, reopen, and the earlier text
is still there.

One question the emulator could not answer, now answered on the metal:
Wine's 16-bit layer did not act on Ctrl+F4 or Ctrl+F5 at all, while
every mouse route worked. On Windows 95 OSR2 **Ctrl+F4 closes the
document and leaves the frame standing**, and Window > New Conversation
Window brings it back. `TranslateMDISysAccel` was doing its job all
along; Wine is the gap.

That is worth more than the keystroke. **Wine proves the protocol and
the drawing; it does not prove the shell.** Winsock, the framing, the
transcript and every pixel of the pane are testable under Wine in
seconds. Anything resting on Windows' own keyboard handling, menu
behavior or window management is not, and has to be believed only after
a real machine has been asked.

### The first real-machine run, and what it asked for

The client runs on Windows 95 OSR2 under QEMU: it connects to the proxy,
holds a conversation, and its document windows close and reopen from the
keyboard. The first thing that went
wrong there was not the client: it was not knowing which address the
program was dialling, and having no way to change it without rebuilding
the floppy it booted from. Both are now answered:

- **Settings ▸ Server…** -- a modal dialog for host and port, saved back
  to `LLM64.INI` and optionally reconnecting. On a machine with no
  command line this is the only route, which makes it the first dialog
  worth building rather than the fourth.
- **The status strip already names the failure** -- the address while
  connecting, and `Connection refused or unreachable (Winsock error
  10061)` after. Worth saying out loud in the README, because the
  distinction between "reached the host, nothing listening" and "never
  reached the host" is the whole diagnosis.
- **`devproxy.sh` takes a bind address**, for a bridged VM or a real
  machine. Under user-mode networking it is unnecessary: a guest reaches
  the host at **10.0.2.2**, and slirp rewrites that to the host's
  loopback, so a proxy on 127.0.0.1 is already reachable. The addresses
  that are *not* the host are worth naming too -- a `10.8.x` or `100.x`
  address belongs to WireGuard or Tailscale, not to the VM's host.

The dialog also fixes the reconnect ordering trap: it returns its intent
as a result code rather than calling into the socket from inside a modal
proc that is already half torn down.

### Paper, and the color it is printed on

Two changes that turned out to be the same change. Both landed after the
Win95 run.

**A second kind of document.** Every document is now a `View`: a
`Scrollback`, a scroll position, and whether it re-flows. The pane window
carries a far pointer to its View in its extra window bytes, so one
window procedure draws the transcript and every sheet of paper. That is
what made `/print` cheap -- `PRINT_BEGIN`/`DATA`/`END` are caught and
ACKed exactly as the C64 ACKs them, and the composed document opens as a
sheet in the workspace (§8). No proxy change, no new wire message, no
printer required.

Paper is the case that proves the abstraction was drawn in the right
place: it is a View that does *not* re-flow, because the proxy already
laid it out to `printer_width` and re-wrapping it would be re-typesetting
someone else's document. The transcript re-flows because a conversation
has no layout of its own to respect.

**A palette that is not the C64's.** White paper, black ink, by default.
This is not the Pepto table dimmed: half of those colors are illegible
on white at any brightness, and the two worst are yellow and the light
green that every assistant reply arrives in. So the light theme keeps
each marker slot's *hue* and gives it a value that reads as ink -- index 1
becomes black, index 13 a dark green. The C64 palette on black is still
there under Settings, and the choice is saved to the INI, which is what
§6.3 said it should be from the start.

One bug, and only the keyboard finds it: `&Server...` and a `&Screen`
item in the same popup both answer to Alt+S, the first one wins, and what
you see is a theme setting that appears not to work. Mnemonics inside a
popup have to be distinct.

---

## 12. Risks

1. ~~**`winsock.dll16` under Wine is unproven.**~~ Retired: proven in
   Phase 0. The Win32 build (§9.2) remains worth having for modern
   Windows, but it is no longer insurance.
2. ~~**64 KB segments vs. a long scrollback.**~~ Retired: done in Phase 1
   (§10). The transcript is unwrapped logical lines in far blocks,
   wrapped at paint time, and it re-flows. It did produce the predicted
   Win16-specific bug -- a one-byte overread past an arena block, which
   in protected mode faults -- so the prediction was right about the
   *kind* of trouble as well as the need.
3. **Cooperative multitasking.** Holding a menu open or dragging a
   window stops the message pump. This was the main argument against
   streaming PCM; with MIDI handed to MCI (§6.2) the risk largely goes
   away, which is a second reason to prefer it.
4. **A MIDI corpus has to be assembled and mood-tagged.** The SID
   pipeline took real work (10,000 tunes, an LLM tagger, loudness
   normalisation, hand review); the MIDI equivalent is smaller but not
   free, and it is the long pole in Phase 5.
5. **Real 3.11 hardware needs an NDIS driver** for whatever NIC the
   machine has. A solved problem, but a shopping problem. Windows 95
   needs nothing: it runs the NE binary and ships its own 16-bit
   `WINSOCK.DLL`, which is why it was the first real machine this ran
   on.
6. **Protocol drift.** Two clients means the wire is now an interface,
   not an implementation detail. `CLIENT_HELLO` and the profile table
   are what keep that honest; without them every future change has to be
   simultaneously C64-safe and Windows-safe by accident.
7. **Wine cannot test the shell.** Not a risk to the design but a
   standing limit on the harness: Winsock, the framing, the transcript
   and every pixel of the pane are testable under Wine in seconds, while
   keyboard handling, menu behavior and window management have to be
   confirmed on a real machine. The MDI accelerators are the case that
   proved it (§10) -- inert under Wine, correct on Windows 95.

---

## 13. The pleasant side effect

Both clients talk to the same proxy and the same conversation store. A
C64 and a Windows 3.11 box on the same LAN can take turns in the same
adventure -- same world, same map, same moods -- one machine hearing a SID
chip through a VIC-II picture, the other a General MIDI score over VGA
art, each rendered for the machine it is on. That is a better demo than
either client alone, and it costs nothing extra: it falls out of the
profile table, which is the only structural change the proxy needs.

---

## 13b. The desk as it stands, 2026-07-29

*Added with the batch that fixed window placement and gave the desk three
more rooms. The sections above are the original survey; this is what the
client and the proxy actually do now.*

### Windows

Eight launcher buttons, `Ctrl+1..7` and the Window menu on the same ids:
Conversation, Picture, Music, Character, Items, **Notebook**, **Map**.
The strip wraps to a second row when the frame is too narrow for all
eight (588 px), so nothing falls off the right edge on a 640x480 screen.

- **Notebook** (`NoteProc`) replaced the per-sheet Printout windows. One
  window: an index of every sheet printed this session down the left,
  the selected page beside it. Each row is the sheet's own first line,
  captured as the bytes arrive (`paper_name_feed`) because
  `printdoc.finish` always puts the title there. `MAX_PAPER` is 6, and
  eviction is oldest-first. Window > Empty the Notebook (was "Close All
  Printouts") discards the sheets, which are the memory; closing the
  window keeps them.
- **Map** (`MapProc`) draws the proxy's map: ruled boxes on parchment,
  the room number in each, ink lines for corridors, dotted for one-way
  passages and stairs, filled black for where you are, dotted outline for
  a place only heard about. Grid coordinates come from the proxy
  (`advmap.layout`); how big a room is on screen is the client's
  business. The ASCII `/map` reply is unchanged and still goes to both
  machines.
- **Character** carries a whole sheet now, not four gauges: name, race,
  class, level, the rolled ability scores, HP bar, mana, AC, gold, XP,
  score, afflictions, skills, spells, starting kit, appearance,
  companions, and a red "(as of N turns ago)" when the narrator has
  stopped emitting state blocks.
- **Picture** has an "Illustrate every room" checkbox along its bottom -
  the same setting as Settings > Pictures, where nobody found it - and
  takes only the height its art's aspect needs in the default desk
  instead of the whole column.

Geometry survives a close: `desk_remember` / `desk_place` keep each
window's restored rectangle in MDI client coordinates, so a launcher
toggle brings a window back where it was rather than at its built-in
coordinates. The frame's own `MoveWindow(g_mdi, ...)` runs inside the
`g_in_layout` guard now; without that, one drag of the frame border made
the desk "user-arranged" and every later toggle jumped.

### Three frames added to the protocol

| | |
|---|---|
| `CHAR_SHEET` 0x6B | The STATIC half of the sheet, depth-1 JSON + NUL: `name race class abil hd skills spells gear`. `chargen.sheet()` always rolled these and then flattened them into prose; now the structure survives. `CAP_CHAR_SHEET 0x0040`, budget 900 B (lists trim, kit first). |
| `MAP_DATA` 0x6C | The map as text: `M<turn>\t<cols>\t<rows>`, `R<num>\t<gx>\t<gy>\t<flags>\t<name>` (flags 1 visited, 2 you are here), `E<a>\t<b>\t<dir>\t<flags>` (dir `n s e w u d` or `-`, flags 1 one-way), `X<hidden>`. `CAP_MAP_DATA 0x0080`, budget 1400 B, dropping rooms farthest from the player first. |
| `GET_SHEET` 0x44 | Client → proxy, no payload: resend character, state and map out of stored meta. Never an LLM or image call - it is what a window asks for when it opens. |

`STATE_JSON` gains one proxy-owned key, `_age`: turns since the narrator
last emitted a valid block, computed against `adv_map['turn']` *after*
the ingest and sent on every adventure reply, so the turns where nothing
arrived are reported rather than invisible. Meta `adv_state` stays exactly
what the model wrote, so the re-injected prompt state is unchanged.

### Bugs this batch closed

- `_emit_color` emitted the one-byte marker for color slot 11, and
  `0x10|11` **is** `0x1B`, the marker escape byte - so `[color=darkgrey]`
  reached the screen as a box glyph glued to the word ("the []slate
  rooftops"), or ate two characters when the phrase began with C. Slot 11
  now takes the three-byte form. Rich-text clients only; the C64 has no
  slot 11 and its bytes did not change.
- `chr_paint` formatted "With you: " plus a 159-byte companions list into
  a 120-byte stack buffer.
- An adventure loaded from a conversation lost `mode.background` and
  `mode.character` - neither was ever persisted - so the restored system
  prompt had no world bible and no character, and the illustrator lost
  the player's fixed appearance (docs/13).
- `MusicDirectiveFilter.MAX_HOLD` was 600, and a `[[STATE:]]` block past
  it was printed to the player verbatim *and* never parsed, so game state
  silently stopped updating. A 12-item inventory reaches 700. Now 1500.
- The Character window's `valid` test named four fields, so a block
  carrying only gold and a level rendered "No adventure state yet" over
  live data. The Inventory title said "(16)" for 20 items; it says
  "(16 of 20)".
- `printdoc.render_sheet` printed HP/Mana/Gold/Score and dropped level,
  XP and AC, which were on the wire and on screen.
- `chargen`'s `hit_die` was computed and read by nothing, while the model
  invented level-1 HP. It is in the prompt now as the authoritative
  basis.

### Pictures look like 1993 now

`convert_to_dib8` was "downscale to 640x400 and quantize to 256 adaptive
colors", and the docstring's claim of Floyd-Steinberg was false - Pillow
ignores `dither` unless a palette is given, so nothing dithered. A palette
fitted per image is also why the art kept the diffusion model's smooth
gradients. It is now 320x200 (Mode 13h, and the client upscales with pixel
replication), `auto_levels` first, then a real Floyd-Steinberg dither
against the **fixed** palette in `imaging.period_palette()` - the 6x6x6
cube, a 16-step gray ramp, the 16 EGA entries. `[images] dib_style =
"clean"` restores the old behavior. Wire cost fell from ~161 KB to
~65 KB, which is also the client's largest single allocation.

## 14. Handoff

*Written 2026-07-27 for picking this up on another machine. Note that
the repo's `HANDOFF.md` is a different, still-live handoff about the
client performance / baud batch -- it is not this work.*

### Where the work is

| | |
|---|---|
| Branch | `worktree-win311-client`, one commit, tree clean |
| Base | `origin/master` -- **not** the author's local master, which was 8 commits ahead when this branched |
| Worktree | `.claude/worktrees/win311-client` (nothing in it is special; it is an ordinary branch) |
| Code | `win311_client/` -- see its README for the per-file map |
| Merged? | No. Not rebased onto the newer master either. |

### What has to exist on the new machine

Everything the repo needs is tracked, including `emu/fixtures` and the
whole proxy. Three things are not:

1. **Open Watcom V2** -- 522 MB extracted, so re-fetch rather than copy:
   ```sh
   mkdir -p ~/Programs && cd ~/Programs
   curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
   mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2
   ```
   The Makefile defaults to `~/Programs/open-watcom-v2`; `make WATCOM=...`
   overrides. Nothing needs symlinking into `~/bin`: the Makefile puts
   `binl64` on `PATH` itself, which it has to, because `wcl` shells out
   to `wcc` and `wlink` by name. `Current-build` is a rolling tag -- pin a
   dated build if two machines need identical output.
2. **The proxy venv**, because `tools/devproxy.sh` borrows it for
   Pillow: `cd llm64_proxy && python -m venv .venv && .venv/bin/pip
   install -r requirements.txt`.
3. **Wine, xdotool, imagemagick, Xvfb and a window manager** -- only to
   *run* the client. On an ostree host (Bazzite, Silverblue) put them in
   a toolbox rather than layering them onto the image: `distrobox enter
   my-distrobox -- sudo dnf install wine xdotool xorg-x11-server-Xvfb
   ImageMagick openbox`. The 16-bit modules land in
   `/usr/lib64/wine-wow64/wine/i386-windows/` and work from there.
   `wine_smoke.sh` starts its own Xvfb and window manager; the proxy runs
   on the host, and a toolbox shares the network namespace so
   `127.0.0.1` still reaches it.

No `config.toml` is required: `main.py` skips it when absent
(`main.py:63`) and `devproxy.sh` supplies everything by environment.

### Resuming

```sh
cd win311_client
make test                      # host unit test - no Watcom, no Wine, no proxy
make                           # -> build/LLM64.EXE
./tools/devproxy.sh 6410 &
./tools/wine_smoke.sh 6410     # screenshots into build/
```

`make test` first: it is the one signal that needs no toolchain at all,
so a green run means the checkout is good before any environment fight.

### Next task, and why it is first

~~Phase 1's scrollback rewrite~~ -- done, see §10. What is left of Phase 1
is the rest of the list: the editor keys (Ctrl-A/E/K/D on the input box),
the `HINT` chrome in its own half of the status strip, `MENU_LIST`
appended to `&Mode` at runtime, and help. None of them is structural;
the data structure everything else grows around is now in place.

The one thing still worth doing early is the **`CLIENT_HELLO` /
`ClientProfile` work in §7**, which is Phase 4 in the table but is the
only remaining change that reaches into the proxy. Everything after it
(pictures, MIDI, printing) is easier once widths and payload caps are
negotiated rather than assumed.

### Decisions still open

- **Per-profile art** (§6.1): the same scene wants two *generations*,
  not two encodings, once the art prompt is per-profile. Recommendation
  on file is generate-for-whoever-asked and convert for everyone else,
  but it is a flag either way.
- **Where the MIDI corpus comes from** (§6.2, §12 risk 4) -- the long
  pole in Phase 5, and unstarted.

### Traps already paid for

- `wlink` wants directive syntax, not switches; the Makefile links
  through `wcl`, which needs `binl64` on `PATH` or it cannot find
  `wlink` at all.
- Win16 callbacks need `_export` (and `-zu`, since DS != SS in one).
- `pkill -f LLM64.EXE` matches the *shell running it* and kills the
  session. Kill by PID, from a script file.
- `xdotool search --name LLM64` also matches a file manager or editor
  with the project open; match on the `winevdm.exe` window class too.
- `xdotool type --window <id>` is `XSendEvent`, and the 16-bit VDM drops
  most synthetic keystrokes: the symptom is a message that reaches the
  proxy as its first word. Focus the window and type through XTEST.
- XTEST types into whatever has focus, so a smoke run on `:0` types into
  whatever *you* are doing. `wine_smoke.sh` brings up its own Xvfb.
- A headless Xvfb needs a window manager or Wine never delivers
  `WM_SIZE`, and every resize test on it passes without testing
  anything.
- Wine's 16-bit `TranslateMDISysAccel` is inert: Ctrl+F4 and Ctrl+F5 do
  nothing there and work on real Windows. Do not debug the client over
  it -- and more generally, do not conclude anything about keyboard,
  menu or window-management behavior from Wine alone.
- Anything written to the transcript before the first `WM_SIZE` wraps at
  the placeholder pane width. That is why the banner is emitted from
  `start_session()` and not `WM_CREATE`.
