# LLM64 for Windows

A Windows 3.1 / Windows for Workgroups 3.11 client for the LLM64 proxy —
the same conversations, adventures, pictures and printing the C64 gets,
in an authentic Win16 program.

![Phase](https://img.shields.io/badge/phase-0%20spike-yellow)
![Target](https://img.shields.io/badge/target-Win16%20NE%20(3.1%2F3.11)-blue)
![Build](https://img.shields.io/badge/toolchain-Open%20Watcom%20V2-orange)

It talks to the *unmodified* proxy over TCP with Winsock 1.1. There is no
modem, no serial port and no C64 in the loop: the modem only ever existed
because a 6510 has no TCP stack.

See [docs/16-windows-311-client.md](../docs/16-windows-311-client.md) for
the design and the phase plan.

## Status: Phase 0 (spike) — working

Verified against a real proxy, running under Wine's 16-bit subsystem:

- connects, PINGs, and gets its ACK
- sends `CHAT_REQUEST`, renders streamed `CHAT_CHUNK` replies
- renders the proxy's in-band colour and bold markers
- menu bar, transcript pane with scroll bar, input box, status strip
- reads `LLM64.INI`; the command line overrides it

Not yet: pictures, MIDI, printing, the conversation manager, settings and
history dialogs, and a scrollback that is not a fixed array (see below).

## Build

Open Watcom V2 hosts on Linux and still targets Win16. It is not
packaged on most distributions, so fetch the snapshot once:

```sh
mkdir -p ~/opt && cd ~/opt
curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2
```

Then:

```sh
make                 # -> build/LLM64.EXE   (NE binary, ~9 KB)
make test            # wire protocol unit test, compiled for the host
make WATCOM=/elsewhere/open-watcom-v2
```

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
include/net.h    src/net.c     Winsock 1.1, asynchronous. Windows 3.x is
                               cooperatively multitasked, so a blocking
                               recv() would freeze the whole system;
                               everything is WSAAsyncSelect + messages.
                 src/main.c    window, menu, transcript pane, input,
                               status strip, frame dispatch.
                 src/llm64.rc  menu resource.
tests/           test_wire.c   framing tests, including the +0x20 length
                               bias and its 8-bit wrap.
tools/           devproxy.sh   proxy + mock LLM for development
                 wine_smoke.sh launch under Wine, type, screenshot
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

**The scrollback is a fixed array and will not survive Phase 1.** It is
200 x 160 chars of the 64 KB default data segment, and lines are wrapped
as they are appended — so resizing the window does not re-flow text
already on screen. The replacement is the transcript in `GlobalAlloc`'d
far blocks holding *unwrapped* logical lines, wrapped into a small
display array at paint time. That fixes re-flow and the size ceiling in
one move.

**Callbacks need `_export`** so the compiler emits the prologue that
reloads DS. `-zu` is set for the same reason: in a Win16 callback,
DS != SS.
