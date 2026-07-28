# LLM64 for Windows

A Windows 3.1 / Windows for Workgroups 3.11 client for the LLM64 proxy —
the same conversations, adventures, pictures and printing the C64 gets,
in an authentic Win16 program.

![Phase](https://img.shields.io/badge/phase-1%20in%20progress-yellow)
![Target](https://img.shields.io/badge/target-Win16%20NE%20(3.1%2F3.11)-blue)
![Build](https://img.shields.io/badge/toolchain-Open%20Watcom%20V2-orange)

It talks to the *unmodified* proxy over TCP with Winsock 1.1. There is no
modem, no serial port and no C64 in the loop: the modem only ever existed
because a 6510 has no TCP stack.

See [docs/16-windows-311-client.md](../docs/16-windows-311-client.md) for
the design and the phase plan.

## Status: Phase 1 (in progress)

Verified against a real proxy, running under Wine's 16-bit subsystem:

- connects, PINGs, and gets its ACK
- sends `CHAT_REQUEST`, renders streamed `CHAT_CHUNK` replies
- renders the proxy's in-band colour markers, and bold in a real bold face
- **MDI**: one frame window, documents inside it, a Window menu that
  cascades, tiles and lists them
- **`/print` lands on virtual paper** — the proxy composes the document
  and ships it as `PRINT_*` frames; instead of a printer, the client
  catches it in a document window. Up to four sheets at once, and no new
  wire messages were needed
- **Two themes**: Paper (black on white, a 1993 business application) and
  C64 Screen (the Pepto palette on black). Settings ▸, saved to the INI
- menu bar, transcript pane with scroll bar, input box, status strip
- reads `LLM64.INI`; the command line overrides it
- **Settings ▸ Server…** retypes the host and port and reconnects,
  saving both back to the INI — the only way to change the address on a
  machine with no command line
- **the transcript re-flows on a resize**, and lives outside the 64 KB
  default data segment (see below)

Still to do in Phase 1: the editor keys, the `HINT` chrome in its own half
of the status strip, `MENU_LIST`, and help. After that: pictures, MIDI,
printing, the conversation manager, settings and history dialogs.

## Build

Open Watcom V2 hosts on Linux and still targets Win16. It is not
packaged on most distributions, so fetch the snapshot once:

```sh
mkdir -p ~/Programs && cd ~/Programs
curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2
```

Then:

```sh
make                 # -> build/LLM64.EXE   (NE binary)
make test            # wire + transcript unit tests, compiled for the host
make WATCOM=/elsewhere/open-watcom-v2
```

`make test` first on a new machine: it is the one signal that needs no
Watcom, no Wine and no proxy, so a green run means the checkout is good
before any environment fight.

## Run it

```sh
./tools/devproxy.sh 6410      # mock LLM + real proxy, scratch data dir
make run PORT=6410            # launch under Wine
./tools/wine_smoke.sh 6410    # or: drive it and photograph the result
```

### The mock, and the real model

`devproxy.sh` deliberately runs the repo's **mock** model
(`emu/mock_llm.py`) against a real proxy on a scratch port with a scratch
data directory. That is the loop for working on the client: no API key,
no GPU, deterministic replies, and `LONGTEST` / `PICTEST` / `MUSICTEST`
on tap. It will never talk to a real model, by design.

For the real thing, do not start a proxy here at all — point the client
at the same one the C64 uses. `run.conf` at the repo root already names
it (`PROXY_HOST` / `PROXY_PORT`), and it is often on another machine:

```sh
make run HOST=<PROXY_HOST> PORT=<PROXY_PORT>
make floppy VMHOST=<PROXY_HOST> VMPORT=<PROXY_PORT>   # for a VM
```

A QEMU guest on user-mode networking reaches an outside address through
the host's stack, so a proxy over Tailscale or a LAN works from the VM
as long as the host can reach it.

Running a proxy *locally* against a real model needs
`llm64_proxy/config.toml` (copy `config.toml.example` and set the model
endpoint and API key) and then `./run.sh proxy` from the repo root.
Without that file the proxy starts, accepts the client, and fails every
reply with an API 401.

**`/print` needs the proxy's printer backend to be `c64`** — the
shipped default, and the one that sends `PRINT_*` frames to the client.
A proxy configured for `cups` spools the document to a real print queue
instead and the client never sees it; `both` does both. It is
`[printer] backend` in `config.toml`, or `LLM64_PRINTER_BACKEND=c64` in
the environment.

On a real machine there is no command line, so it reads `LLM64.INI` from
its own directory — *beside the EXE*, which is not what an unqualified
name means to `GetPrivateProfileString` (that resolves against the
Windows directory), so the path is derived from `GetModuleFileName`:

```ini
[Server]
Host=192.168.1.10
Port=6400
```

## In a VM

`make floppy` writes a 1.44 MB image holding the EXE and a matching INI:

```sh
make floppy                       # -> build/llm64.img, pointing at 10.0.2.2:6410
make floppy VMHOST=10.0.2.2 VMPORT=6400
```

Attach it with `-fda build/llm64.img` and run `A:\LLM64.EXE`. Under
QEMU's user-mode networking the host is **10.0.2.2** from inside the
guest, and a proxy bound to the host's loopback is reachable there — so
`./tools/devproxy.sh 6410` on the host needs no extra plumbing.

The guest needs TCP/IP bound to its network card (Control Panel →
Network); slirp runs a DHCP server, so "obtain an IP address
automatically" is enough. Windows 95 and 98 run the NE binary natively
and ship their own 16-bit `WINSOCK.DLL`, so nothing else is required
there. Windows 3.1 needs Trumpet Winsock; WfW 3.11 wants Microsoft's
TCP/IP-32.

### When it does not connect

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
                 src/main.c    MDI frame, conversation document, menu,
                               transcript pane, input, status strip,
                               frame dispatch.
                 src/llm64.rc  menu resource.
tests/           test_wire.c   framing tests, including the +0x20 length
                               bias and its 8-bit wrap.
                 test_scroll.c re-flow, marker-aware wrapping, eviction.
tools/           devproxy.sh   proxy + mock LLM for development
                 wine_smoke.sh launch under Wine, type, resize, screenshot
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

**Wine's 16-bit layer does not honour the MDI system accelerators, and
real Windows does.** Ctrl+F4 (close document) and Ctrl+F5 (restore) do
nothing under Wine 11.0, while every mouse route works — close box,
minimize, restore, and the Window menu's Cascade and Tile. On Windows 95
OSR2 Ctrl+F4 closes the document and leaves the frame up, as it should.
So `TranslateMDISysAccel` is doing its job and Wine is the gap.

The lesson generalises past this one key: **Wine proves the protocol and
the drawing; it does not prove the shell.** Anything that depends on
Windows' own keyboard handling, menu behaviour or window management
needs a real machine before it can be called working.
