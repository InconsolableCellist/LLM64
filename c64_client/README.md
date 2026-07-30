# LLM64 for the Commodore 64

The original client: a cc65/6502 program that talks to the LLM64 proxy
over a SwiftLink-compatible 6551 ACIA at `$DE00`, and renders the whole
thing — chat, adventures, character sheets, pictures, SID music, the map
— in 80 columns of soft-80 bitmap on a stock 64 KB machine.

![Platform](https://img.shields.io/badge/platform-C64%20%2F%20C64%20Ultimate%20%2F%20VICE-red)
![Toolchain](https://img.shields.io/badge/toolchain-cc65-orange)

![An adventure in progress, 80 columns of soft-80 bitmap](../screenshots/c64_client.png)

**This README is the C64 half only: what to install, how to build, and
how to run it on hardware or in an emulator.** What the program *does*,
its keys and slash commands, and every proxy-side setting (models,
images, music library, printing) live in the
[top-level README](../README.md) — and the proxy has to be running
before any of this is interesting, so start with
[installing the proxy](../llm64_proxy/README.md#installation).

## What you need

| For | Install |
|-----|---------|
| Building | `cc65` (2.19+), GNU make, `git` |
| Making the disk image | VICE's `c1541` (a distro package or the `net.sf.VICE` flatpak — `emu/vice-run.sh` finds either) |
| Running in an emulator | VICE `x64sc` |
| Deploying to a C64 Ultimate | `curl` (FTP upload), `python3` (the telnet mount+run helper) |
| Hayes dial-up test in VICE | `tcpser` |

Nothing here needs root, and the build writes only into `build/`.

## Build

```bash
# From the repo root. MODE80=1 is the real client; SERVER_IP/PORT are
# only the address the config editor pre-fills on first boot.
make -C c64_client clean
make -C c64_client MODE80=1 CONNECT=hayes SERVER_IP=192.168.1.39 SERVER_PORT=6400
make -C c64_client disk        # -> c64_client/build/llm64.d64
```

That produces `build/llm64.prg` plus the overlay modules
(`llm64.prg.1`, `.2`, …) and packs all of them into one bootable D64.
`LOAD"*",8,1` on the disk starts the client, and the F1 menu loads its
config editor, conversation manager, jukebox and disk-copy code from
that same disk.

**The modules are linked against their exact PRG.** They always travel
together on one disk — never mix a PRG from one build with modules from
another, or the F1 menu will load garbage into a live machine.

`make -C c64_client info` prints the sources, objects and the FTP path it
would upload to.

### Build modes

| Flag | Meaning |
|------|---------|
| `MODE80=1` | Soft-80 bitmap UI. The primary experience; pictures and `/print` are 80-column only |
| `CONNECT=hayes` | AT-command dial — real hardware, or VICE + tcpser (default) |
| `CONNECT=direct` | No modem handshake at all: the ACIA *is* the socket. This is the VICE build |
| `SERVER_IP=` / `SERVER_PORT=` | Pre-filled default address; `llm64.cfg` on the disk overrides it once you've saved settings |
| `BAUD38400=1` | Make 38400 the *boot default* rate. All three rates are selectable in F1 → E → Speed on every build; this only picks the default |
| `DIAG=1` | Compile in the crash post-mortem (breadcrumb ring + C-stack canary at `$02A7`, readable with PEEK after a drop to BASIC) |
| `DEBUG_CLIENT=1` | Scripted diagnostic session instead of the TUI |

The 40-column build (no `MODE80=1`) still compiles and is what the older
`emu`/`c64` targets use, but it is legacy — no pictures, no printing, and
half the screen.

## Running it

### In VICE (quickest)

```bash
./run.sh config          # writes run.conf: PROXY_HOST / PROXY_PORT
./run.sh emu-80          # build, make the boot disk, launch x64sc
./run.sh emu-80 10.0.0.5:6400    # somewhere else, for this run only
```

The emulator build is `CONNECT=direct`: **VICE itself** opens the TCP
connection to the proxy, so the address in the client's own config editor
is unused and cannot be changed from inside the program. `run.conf` — or
the argument above — is what decides where it connects. If the configured
proxy is this machine and nothing is listening, `run.sh` starts one for
you.

`make run-live` from the repo root is the older, plainer version of the
same idea: it starts a proxy on `127.0.0.1:6400` from `config.toml` and
launches the **40-column** build against it. `make test-all` runs the
automated end-to-end suites (development only).

To exercise the *real* dial path in an emulator, `make test-emu-hayes`
builds `CONNECT=hayes` and puts `tcpser` behind VICE's RS-232 as the
modem.

One caveat that trips everyone: **VICE has no SwiftLink crystal**, so a
rate selected in the client runs at half its label under emulation. On
hardware the label is the truth (below). The test harness already
accounts for this.

### On a C64 Ultimate / Ultimate 64

The easiest hardware path, and the one this client is developed against.
Enable the Ultimate's FTP server (F2 → Network), put its address in
`run.conf` (`C64U_HOST`), and use the canonical deploy — the whole disk,
uploaded to `/Flash` and booted by driving the Ultimate's own menu over
its telnet port:

```bash
make deploy-c64u-disk-80 C64U_IP=<ultimate-ip> \
     C64_PROXY_IP=<proxy-ip> C64_PROXY_PORT=6400
```

That mounts `llm64.d64` on the emulated 1541 ("Run Disk"), so the
fastloader applies, the overlay modules are where the F1 menu expects
them, and `llm64.cfg` saves persist *inside the image*. It ships
cfg-free, so the first boot is the config editor.

The shortcuts, which read `run.conf` instead of taking variables:

```bash
./run.sh c64-80     # build the bare PRG, upload to /Flash, run it
./run.sh install    # build and upload the PRG to /Flash, don't run it
```

Both of those deploy the PRG *alone*: quick for a code change, but with
no disk beside it the F1 menu falls back to its compact built-in form and
the overlay-backed items (config editor, conversation manager, jukebox,
disk copier) report a module load failure. Deploy the disk when you want
the whole program.

Any other route works too — FTP the D64 to `/Flash` by hand, or carry it
on a USB stick and mount it from the Ultimate's own menu.

**Hardware dials for itself**, so unlike the emulator the proxy address
has to be reachable *by the C64*: a VPN address that works on your
workstation will not do.

**ACIA settings** (Cartridge/IO): SwiftLink-compatible 6551 at **$DE00**,
modem emulation on, interrupt on **NMI** — the client's NMI handler keeps
draining the ACIA through disk loads and SID playback, which is what
makes the higher rates reliable (IRQ works too, and is what VICE uses).
In the modem settings, disable *drop connection on DTR low* and *RTS
handshake RX*, and enable *automatic RX pushback*; the emulated control
lines are re-evaluated on ACIA command writes and the wrong settings drop
data. For `/print`, F2 → Software IEC Settings → *IEC Drive and Printer =
Enabled* (off by default).

**Wire speed** (F1 → E → Speed): **9600 / 19200 / 38400**. SwiftLink's
crystal doubles the 6551's baud table and that doubling is baked into the
labels, so what you pick is what you get — nothing to halve, nothing to
set on the Ultimate's modem side (it follows the ACIA), and nothing to
set on the proxy (the client reports its rate on connect and the proxy
paces to match). If a rate garbles data on your firmware, step it down
and reboot.

Full checklist, including what to do when it won't connect:
[docs/05-ultimate-setup.md](../docs/05-ultimate-setup.md).

### On a real breadbin or C128

Same program, and nothing in it knows the difference — it wants two
things:

1. **A 6551 ACIA at `$DE00`.** A SwiftLink or Turbo232 cartridge is
   exactly that. (The C64 Ultimate's ACIA emulation is the same
   register interface; a plain userport modem is *not*, and will not
   work.)
2. **Something on the ACIA's RS-232 side that answers Hayes AT
   commands** and can `ATDT<host>:<port>` to the proxy — an ESP-based
   WiFi modem, or a null-modem cable to a PC running `tcpser`. Build
   with `CONNECT=hayes` (the default).

Getting the disk there is however you normally move a D64: an SD2IEC, a
ZoomFloppy/X1541 cable to a real 1541, or a Pi1541. A fastloader or
JiffyDOS is strongly recommended — the client loads overlay modules from
disk while you use it, and a stock 1541 makes that a wait.

This is the one path not tested here regularly; the C64U's ACIA
emulation is what the driver is verified against, so if a real SwiftLink
misbehaves, start at 9600 and check the modem's handshake lines.

## First boot

On a cfg-free disk the config editor comes up first and asks for the
proxy address and the wire speed, then saves `llm64.cfg` back onto the
disk itself — from then on the disk carries its own settings, editable
any time via F1 → E. The baked-in `SERVER_IP` is only the pre-filled
default, which is why distribution disks are built without a config.

Then: `/help` for the commands, F1 for the menu, F5 for past
conversations. The full key and command reference is in the
[top-level README](../README.md#keys-and-commands).

![The F1 menu, whose contents the proxy decides](../screenshots/c64_menu.png)

The F1 menu's items come from the proxy, so they follow the mode you are
in — and the overlay modules behind several of them are loaded off the
boot disk when you pick them, which is why the disk matters.

![A scene the narrator illustrated, converted to multicolour with its caption burned in](../screenshots/c64_pic.png)

## Layout

```
src/main.c        TUI, transfer state machines, mode handling
src/serial.s      ACIA driver (IRQ + NMI vectors, ring buffers)
src/soft80.s      80-column bitmap renderer, font48.s its font
src/music.s       SID player driven off the raster IRQ
src/display.c     screen composition, status bars, pictures
src/editor.c      line editor; keyboard.s scans the matrix itself
                  (n-key rollover, ~150 WPM)
src/protocol.c    framing, crc.s, message dispatch
src/cfg.c         llm64.cfg on the boot disk
src/loader.c      overlay loader; modslot.s is the slot it loads into
src/mod_*.c       the overlays themselves: config editor, conversation
                  manager, jukebox/sound, disk copier, F1 menu
src/diag.s        opt-in crash post-mortem (DIAG=1)
include/          headers, generated buildhash.h
intro/            the shareware intro and its own three-voice tune
c64-soft80.cfg    linker config: program capped below $C000, which is
                  where the ASCII shadow, colour matrix and bitmap live
build/            output: llm64.prg, the overlay modules, llm64.d64
```

Design notes: [docs/02-c64-client-design.md](../docs/02-c64-client-design.md).
