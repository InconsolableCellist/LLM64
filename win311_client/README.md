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
- menu bar, transcript pane with scroll bar, input box, status strip
- reads `LLM64.INI`; the command line overrides it
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

On a real machine, put `LLM64.INI` beside the EXE:

```ini
[Server]
Host=192.168.1.10
Port=6400
```

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
                 src/main.c    window, menu, transcript pane, input,
                               status strip, frame dispatch.
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
