# LLM64 for Windows

A Windows 3.1 / Windows for Workgroups 3.11 client for the LLM64 proxy —
the same conversations, adventures, pictures, music and printing the C64
gets, in an authentic Win16 program.

![Target](https://img.shields.io/badge/target-Win16%20NE%20(3.1%2F3.11)-blue)
![Build](https://img.shields.io/badge/toolchain-Open%20Watcom%20V2-orange)
![Runs](https://img.shields.io/badge/runs-real%20HW%20%2F%20VM%20%2F%20Wine-green)

![The desk: conversation, picture with its shelf, and the MIDI transport](../screenshots/win311_client.png)

It talks to the *unmodified* proxy over TCP with Winsock 1.1. There is no
modem, no serial port and no C64 in the loop: the modem only ever existed
because a 6510 has no TCP stack.

**This README is the Windows half only: what to install, how to build,
and how to run it on real hardware, in a VM, or under Wine.** What the
program *does* and every proxy-side setting live in the
[top-level README](../README.md), and the proxy has to be running first —
see [Installing the proxy](../README.md#installing-the-proxy). The design and the
phase plan are
[docs/16-windows-311-client.md](../docs/16-windows-311-client.md).

## What works

All of it verified against a real proxy under Wine. The client also runs
on Windows 95 OSR2 in a VM, which is where the accelerators Wine ignores
were shown to work:

- connects, PINGs, streams `CHAT_CHUNK` replies, renders the proxy's
  in-band colour markers with bold in a real bold face
- **MDI with a remembered desk** — one frame, documents inside it, and a
  launcher strip: Menu, then Conversation, Picture, Music, Character,
  Items, Notebook, Map on `Ctrl+1..7` and on the Window menu. Each window
  comes back where you left it; the strip wraps to a second row rather
  than fall off a 640x480 screen
- **Pictures**, decoded from the proxy's 320x200 8-bit DIB — a real
  period rendering (fixed palette, Floyd–Steinberg), not the C64's
  16-colour blob, with the conversation's whole picture roster on a shelf
- **Music is MIDI**, streamed as `MIDI_*` frames and played through MCI's
  sequencer, with its own window: transport, mood, and the jukebox picker
- **The character sheet and the map as structures**, not scraped text:
  `CHAR_SHEET` and `MAP_DATA` frames, a full sheet with ability scores
  and gauges, and a drawn map — ruled rooms on parchment, ink corridors,
  dotted for one-way passages
- **`/print` lands on virtual paper** — the proxy composes the document
  and ships it as `PRINT_*` frames; the Notebook window indexes every
  sheet printed this session. No printer required, no new wire messages
- **A conversation browser** (F5) that loads, stars and lists what the
  proxy has stored
- **Two themes** — Paper (black on white, a 1993 business application)
  and C64 Screen (the Pepto palette on black) — saved to `LLM64.INI`
  along with the server address and the picture setting
- **The transcript re-flows on a resize**, and lives outside the 64 KB
  default data segment (see the notes at the bottom)
- **Settings ▸ Server…** retypes host and port and reconnects, which on a
  machine with no command line is the only way to change the address

Not done: the input box's editor keys, `MENU_LIST` appended to the Mode
menu at runtime, in-program help, and the `CLIENT_HELLO`/`ClientProfile`
negotiation of §7 (widths and payload caps are still assumed rather than
agreed). Accelerators are inert under Wine and work on real Windows —
every one of them is also on a menu.

## Build

The build hosts on Linux and cross-compiles to a 16-bit NE binary; you
never need a Windows machine to produce one.

| For | Install |
|-----|---------|
| Building | Open Watcom V2 (below), GNU make |
| `make floppy` | `mtools` — `pacman -S mtools`, `apt install mtools`, `dnf install mtools` |
| `make run` | `wine` (its 16-bit subsystem) |
| `make test` | nothing but a host C compiler |
| `tools/wine_smoke.sh` | `wine`, `xdotool`, `imagemagick`, `Xvfb`, and a window manager (`openbox`) |
| `tools/devproxy.sh` | the proxy's venv, for Pillow — see the [top-level README](../README.md#installing-the-proxy) |

Open Watcom is not packaged on most distributions, so fetch the snapshot
once (522 MB extracted):

```sh
mkdir -p ~/Programs && cd ~/Programs
curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2
```

The Makefile defaults to `~/Programs/open-watcom-v2` (or `~/opt/`), puts
`binl64` on `PATH` itself, and needs nothing symlinked. Then, from this
directory:

```sh
make test            # wire + transcript unit tests, compiled for the host
make                 # -> build/LLM64.EXE   (NE binary)
make WATCOM=/elsewhere/open-watcom-v2
```

`make test` first on a new machine: it is the one signal that needs no
Watcom, no Wine and no proxy, so a green run means the checkout is good
before any environment fight. `Current-build` is a rolling tag — pin a
dated build if two machines must produce identical output.

## Which proxy, and the two ways to get one

Everything below needs a proxy to talk to, and there are only two kinds:

**The mock, for working on the client.** `./tools/devproxy.sh 6410` runs
the repo's mock model (`emu/mock_llm.py`) against a *real* proxy on a
scratch port with a scratch data directory: no API key, no GPU,
deterministic replies, and `LONGTEST` / `PICTEST` / `MUSICTEST` on tap.
It will never reach a real model, by design. Add a bind address for a
bridged VM or a real machine: `./tools/devproxy.sh 6410 0.0.0.0`.

**The real one**, which is the same proxy the C64 uses — usually on
another box, and often already running. Don't start a second one here;
point the client at it. `run.conf` at the repo root names it
(`PROXY_HOST` / `PROXY_PORT`). Installing it from scratch — venv,
requirements, `config.toml`, model endpoint, image and music backends —
is the [top-level README](../README.md#installing-the-proxy).

**`/print` needs the proxy's printer backend to be `c64`** — the shipped
default, and the one that sends `PRINT_*` frames to the client. A proxy
set to `cups` spools the document to a real print queue and the client
never sees it; `both` does both. That is `[printer] backend` in
`config.toml`, or `LLM64_PRINTER_BACKEND=c64` in the environment.

## Run it: under Wine

The development loop, and the fastest way to see a build:

```sh
./tools/devproxy.sh 6410      # mock LLM + real proxy, scratch data dir
make run PORT=6410            # launch under Wine
make run HOST=192.168.1.21 PORT=6400        # or the real proxy
./tools/wine_smoke.sh 6410    # drive it and photograph the result
```

`make run` passes host and port on the command line, which overrides the
INI. `WINEPREFIX` defaults to `~/.wine-llm64` so the client gets a
prefix of its own.

Wine proves the protocol and the drawing; **it does not prove the
shell.** Accelerators, menu behaviour and window management need a real
machine (or a VM) before they can be called working — see the last
section. Sound needs an ALSA sequencer client listening: see
[Getting sound out of Wine](#getting-sound-out-of-wine).

## Run it: in a VM

`make floppy` writes a 1.44 MB image holding the EXE and a matching INI,
which is how the binary gets into a guest:

```sh
make floppy                       # -> build/llm64.img, pointing at 10.0.2.2:6410
make floppy VMHOST=10.0.2.2 VMPORT=6400
make vm-in                        # rebuild and swap it into a *running* VM
make vm-out                       # eject
```

Attach it with `-fda build/llm64.img` and run `A:\LLM64.EXE`. `vm-in`
makes the edit/build/run loop bearable: the guest sees the new binary
without rebooting Windows (`tools/vmfloppy.sh` explains why it ejects
first).

Under QEMU's user-mode networking the host is **10.0.2.2** from inside
the guest, and a proxy bound to the host's loopback is reachable there —
so `./tools/devproxy.sh 6410` on the host needs no extra plumbing. slirp
NATs outward too, so a proxy on the LAN or over Tailscale works from the
guest as long as the host can reach it; give `VMHOST` that address.

The guest needs TCP/IP bound to its network card (Control Panel →
Network); slirp runs a DHCP server, so "obtain an IP address
automatically" is enough. Windows 95 and 98 run the NE binary natively
and ship their own 16-bit `WINSOCK.DLL`, so nothing else is required
there. Windows 3.1 needs Trumpet Winsock; WfW 3.11 wants Microsoft's
TCP/IP-32.

A **bridged** guest is not on the host's loopback, so the proxy has to
bind wider: `./tools/devproxy.sh 6410 0.0.0.0`.

## Run it: on real hardware

A 386 or better running Windows 3.1, WfW 3.11, or 95/98. What the machine
needs:

1. **A Winsock 1.1 stack.** WfW 3.11: Microsoft's TCP/IP-32 (free, and
   the period-correct answer). Plain 3.1: Trumpet Winsock over Ethernet
   or SLIP/PPP. 95/98: already there, and it runs the NE binary
   natively — nothing to install.
2. **`LLM64.EXE` and `LLM64.INI` in the same directory.** Anywhere:
   `C:\LLM64\`, or straight off the floppy.
3. **A route to the proxy.** The client dials nothing and negotiates
   nothing — it opens a TCP socket to the address in the INI, so that
   address has to be reachable *from the old machine*. Its own LAN
   address, not a VPN address that only your workstation can resolve.

Build the floppy with the real proxy's address baked into the INI, then
copy both files off it:

```sh
make floppy VMHOST=192.168.1.21 VMPORT=6400
# write build/llm64.img to a diskette (dd, or a Greaseweazle/Kryoflux),
# or serve it however you normally move files to a period machine:
# a shared folder over SMB, a CF-card IDE adapter, LapLink, ...
```

Optional, and worth having:

- **A sound card with a MIDI Mapper entry** — an AWE32/SB16 with its
  synth, a Roland MT-32 or SCC-1, or the OPL3 FM voices. The client asks
  MCI for the `sequencer` device and lets the Control Panel decide how it
  is synthesised; with nothing configured it says so in the status strip
  and carries on silently.
- **A 256-colour display driver** for the pictures. They are 320x200
  8-bit DIBs with their own realizable palette, so a 256-colour driver
  shows them as intended; a 16-colour VGA driver still displays them,
  dithered by GDI.

On a real machine there is no command line, so the address comes from
`LLM64.INI` in the EXE's own directory — *beside the EXE*, which is not
what an unqualified name means to `GetPrivateProfileString` (that
resolves against the Windows directory), so the path is derived from
`GetModuleFileName`:

```ini
[Server]
Host=192.168.1.10
Port=6400
```

**Settings ▸ Server…** rewrites that file from inside the program, which
is the whole point of the dialog: you never have to make another floppy
to change the address.

## Music

**Music here is MIDI, not SID.** The C64 gets a relocated 6502 memory
image and runs its play routine off the raster IRQ; a 486 has no SID,
and what this machine would actually have played in 1993 is a `.MID`
through the MIDI Mapper. The proxy keeps a separate mood-tagged MIDI
library for exactly this client — building it is documented in the
[main README](../README.md#music-for-the-windows-client-midi), and the
mood vocabulary is shared with the SID side, so one narrator can score
both machines in the same adventure.

The client plays it: `MIDI_BEGIN/DATA/END` arrive as a stream, land in a
temp file, and go to MCI's `sequencer` device. The Music window has the
transport, the current mood and the jukebox picker; the narrator scores
the adventure the same way it does on the C64. **Nothing plays until the
proxy has a MIDI library** — that build is four steps in the
[main README](../README.md#music-for-the-windows-client-midi), and until
it exists the jukebox says so.

### What was measured before any of it was written

A standalone Win16 probe (`mciSendString` against the `sequencer`
device) was built with the same toolchain and run under Wine 11.6:

| question | answer |
|---|---|
| does a 16-bit binary see a MIDI device? | yes — `midiOutGetNumDevs()` = 2, device 0 was the running FluidSynth |
| does `open … type sequencer` work? | yes |
| does it play? | yes — a 76.8 s tune took **76.67 s** wall clock, so the tempo is right |
| is `status … length` trustworthy? | **no** |

That last one is a real trap. For a tune that is 76.8 s, MCI reported
**96,000 ms** — exactly the 500000/400000 ratio between the SMF default
tempo and the file's own. Wine computes the length at 120 bpm and
ignores the tempo map. Playback is unaffected, but any progress display
must take its duration from the proxy's database, never from MCI.

Two more things that constrain the design:

- **`MCI_OPEN` takes a filename.** There is no play-from-memory form, so
  the client has to spool the streamed bytes to a temp file and
  `MCI_CLOSE` before overwriting it on the next mood change.
- **Files are bigger than §6.2 assumed.** It says 5–50 KB; measured over
  a real 9,301-tune corpus the median is 22 KB but the 99th percentile
  is 171 KB and the largest is 380 KB. Still one transfer over a socket,
  but size the buffers for the tail.

### Getting sound out of Wine

Wine routes MIDI to the ALSA sequencer, so it needs something listening
there. FluidSynth with a General MIDI SoundFont is the easy one:

```sh
fluidsynth -is -a alsa -m alsa_seq /usr/share/soundfonts/FluidR3_GM.sf2 &
aconnect -o          # 'FLUID Synth' should appear as a client
```

Anything else that registers an ALSA sequencer port works the same way,
which is where the period options live: **Munt** for a real MT-32, an
OPL3 emulator for Adlib/Sound Blaster FM. On actual 1993 hardware this
is a Control Panel setting and costs the client nothing either way — the
choice of synthesis belongs to the machine, not to us.

With no synth running, `midiOutGetNumDevs()` still returns the kernel's
`Midi Through` port, so "no devices" is not a reliable no-op test; the
notes simply go nowhere.

## When it does not connect

**Read the status strip.** It names the address it dialled while
connecting, and the failure afterwards — `Connection refused or
unreachable (Winsock error 10061)` means the address was reached and
nothing was listening; a timeout means it was not reached at all. The
same text is in the transcript, in red.

Then, in order:

- **Settings ▸ Server…** — retype the address without rebuilding the
  floppy. It saves to the INI, so it sticks.
- **10.0.2.2 is the host**, from inside a QEMU user-mode guest. Not the
  host's LAN address, and not a VPN address — a `10.8.x` or `100.x`
  address belongs to WireGuard or Tailscale and is not where the proxy
  is. `ping 10.0.2.2` from a DOS box in the guest settles reachability.
- **Is the proxy up?** `ss -ltn | grep 6410` on the host.
- **Bridged VM or real hardware** is not on the host's loopback, so the
  proxy has to bind wider: `./tools/devproxy.sh 6410 0.0.0.0`. Under
  slirp this is unnecessary — connections to 10.0.2.2 are rewritten to
  the host's loopback.

## Layout

```
include/wire.h   src/wire.c    framing: SYNC/TYPE/LEN/PAYLOAD/CRC.
                               No Windows in it - it compiles for the
                               host so the unit test needs no emulator.
include/scroll.h src/scroll.c  the transcript: unwrapped logical lines in
                               far blocks, wrapped at paint time. Also
                               free of Windows, and tested on the host.
include/net.h    src/net.c     Winsock 1.1, asynchronous. Windows 3.x is
                               cooperatively multitasked, so a blocking
                               recv() would freeze the whole system;
                               everything is WSAAsyncSelect + messages.
                 src/main.c    MDI frame and every window on the desk -
                               conversation, picture, music, character,
                               items, notebook, map - plus the launcher,
                               input, status strip and frame dispatch.
                 src/llm64.rc  menus and dialogs.
tests/           test_wire.c   framing tests, including the +0x20 length
                               bias and its 8-bit wrap.
                 test_scroll.c re-flow, marker-aware wrapping, eviction.
tools/           devproxy.sh   proxy + mock LLM for development
                 wine_smoke.sh launch under Wine, type, resize, screenshot
                 vmfloppy.sh   swap the floppy image in a running VM
build/           LLM64.EXE, llm64.img, and the smoke-test screenshots
```

## Notes for whoever picks this up

**The +0x20 length bias is the trap.** Both length bytes are sent biased
by +0x20 with 8-bit wrap-around, so a payload of 224 bytes encodes its
low length byte as `0x00`. A decoder that subtracts in 16 bits reads it
as zero and desynchronises. `tests/test_wire.c` pins this case.

**Never block.** No `recv()` without `FD_READ`, no long loops, no
blocking dialogs while a transfer is running. In Win16 a stalled message
pump stalls every other program on the machine. The one deliberate
exception is `gethostbyname` at connect time (see `net.c`).

**The transcript stores nothing wrapped.** Logical lines go into far
blocks off the global heap; the wrapping happens at paint time through
one iterator, which is what makes a resize re-flow text already on
screen. Two things follow that are easy to get wrong:

- *Markers occupy no cell.* The wrap counts screen cells, not bytes, so a
  coloured line does not break early. The Phase 0 wrap counted bytes.
- *Rows are not NUL-terminated.* A row is a slice of an arena block, and
  in protected mode reading one byte past the end of that block is a
  fault, not a stray character. The painter stops at `len`.

The one seam is `SB_MAX_LINE` (2 KB): a single unbroken paragraph longer
than that is continued on a fresh logical line, which reads as one short
row mid-paragraph. The break is taken at a space so it never cuts a word.
The proxy sends real newlines between paragraphs, so it takes a stress
case like the mock's `LONGTEST` to reach it.

**The GUI harness types with XTEST.** `tools/wine_smoke.sh` brings up its
own Xvfb by default, because `xdotool type` goes to whatever has focus —
point it at `DISPLAY=:0` and it types into your session instead. It also
starts a window manager on that Xvfb, and not for decoration: with no WM
to confirm the resize, Wine never turns the X ConfigureNotify into
`WM_SIZE`, so the client keeps painting at its old width and the re-flow
shots silently prove nothing. `xdotool type --window` looks like the
tidier answer and is not — that path is `XSendEvent`, and the 16-bit VDM
drops most of those, so the message reaches the proxy as its first word
only.

**Callbacks need `_export`** so the compiler emits the prologue that
reloads DS. `-zu` is set for the same reason: in a Win16 callback,
DS != SS.

**Every document is a `View`** — a `Scrollback`, a scroll position, and
whether it re-flows. The pane window keeps a far pointer to its View in
its extra window bytes (`cbWndExtra`), which is what lets one `PaneProc`
draw the transcript and every sheet of paper. The transcript re-flows
because it is a conversation; paper does not, because the proxy already
laid it out to a printer width and re-wrapping it would be re-typesetting
someone else's document.

**An MDI child wanted maximized must still be created with a real size.**
`WS_MAXIMIZE` in the `MDICREATESTRUCT` together with `CW_USEDEFAULT`
leaves the *normal* rect degenerate, so the first un-maximize restores
the window to no area at all — squashed flat on Windows, and gone
entirely under Wine. Create it with explicit `x/y/cx/cy` and unmaximized,
then send `WM_MDIMAXIMIZE`: that records the created rect as the one to
come back to.

**The frame's caption is not a constant.** An MDI frame appends its
maximized document's title, so the window is really called
`LLM64 - [Conversation]`. `wine_smoke.sh` matches on the `LLM64` prefix;
an exact match silently finds nothing and reports that the client never
started.

**Mnemonics inside one popup must be distinct.** `&Server...` and a
`&Screen` item beside it both answer to Alt+S, the first one wins, and
the menu looks broken in a way that reads as a theme bug. Caught only by
driving the menu from the keyboard.

**It is an MDI application**, so three things are not optional:
`DefFrameProc`/`DefMDIChildProc` in place of `DefWindowProc`,
`TranslateMDISysAccel` in the message loop, and letting `WM_COMMAND`
fall through to `DefFrameProc` for anything the frame does not handle —
swallowing the default case is how an MDI app quietly loses the list of
open documents on its Window menu.

A document window can be *closed*, which is normal in MDI and leaves an
empty workspace. `ConvProc`'s `WM_DESTROY` therefore clears `g_pane` and
`g_input`, and everything that touches them tolerates NULL: a reply
arriving with no window open is still appended, because the transcript
belongs to the application and not to any window. Window > New
Conversation Window opens one on it again, scrollback intact.

**Wine's 16-bit layer does not do accelerators at all, and real Windows
does.** Ctrl+F4 and Ctrl+F5 (MDI system accelerators) do nothing under
Wine 11.0, and neither do F1/F2/F3 from the app's own table —
`LoadAccelerators` succeeds there and `TranslateAccelerator` is simply
inert. Every mouse route works. On Windows 95 OSR2 both Ctrl+F4 and F1
work as they should. So the client is right and Wine is the gap; every
accelerator here is also on a menu, which works everywhere.

The lesson generalises past this one key: **Wine proves the protocol and
the drawing; it does not prove the shell.** Anything that depends on
Windows' own keyboard handling, menu behaviour or window management
needs a real machine before it can be called working.
