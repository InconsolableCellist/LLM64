# LLM64 for the Commodore 64

The LLM64 C64 client is a cc65 program that talks to the LLM64 proxy
through a SwiftLink-compatible 6551 ACIA at `$DE00`. It runs on a real C64
or C128, on a C64 Ultimate, or in VICE, and gives you chat, adventures,
pictures, SID music, the map and your character sheet in 80 columns of
soft-80 bitmap on a stock 64 KB machine.

![An adventure in progress, in 80 columns of soft-80 bitmap](../screenshots/c64_client.png)

You need a running proxy first: see
[installation](../llm64_proxy/README.md#installation).

## What you need

| For | Install |
|-----|---------|
| Building | `cc65` 2.19+, GNU make |
| Making the disk image | VICE's `c1541` (a distro package or the `net.sf.VICE` flatpak; `emu/vice-run.sh` finds either) |
| Running in VICE | VICE's `x64sc` |
| Deploying to a C64 Ultimate | `curl` and `python3` |
| The Hayes dial test in VICE | `tcpser` |

## Build

```bash
# From the repo root. MODE80=1 is the 80-column client; SERVER_IP and
# SERVER_PORT only pre-fill the config editor on first boot.
make -C c64_client clean
make -C c64_client MODE80=1 CONNECT=hayes SERVER_IP=192.168.1.39 SERVER_PORT=6400
make -C c64_client disk        # -> c64_client/build/llm64.d64
```

You get `build/llm64.prg`, the overlay modules (`llm64.prg.1`, `.2`, and
so on), and a bootable D64 holding all of them. `LOAD"*",8,1` starts the
client, and the F1 menu loads the config editor, conversation manager,
jukebox and disk copier off the same disk as you use them.

The modules are linked against the PRG they were built with, so keep them
together on one disk. Mixing builds crashes the machine as soon as the F1
menu loads a module.

`make disk` in the repository root runs those three lines for you, and
`make release` builds this disk along with the Windows clients and the
proxy binaries.

`make -C c64_client info` prints the sources, objects, and the FTP path it
would upload to.

### Build modes

| Flag | Meaning |
|------|---------|
| `MODE80=1` | Soft-80 bitmap UI. Pictures and `/print` need this |
| `CONNECT=hayes` | AT-command dial, for real hardware or VICE with tcpser (the default) |
| `CONNECT=direct` | No modem handshake: the ACIA is the socket. Use this for VICE |
| `SERVER_IP=` / `SERVER_PORT=` | Pre-fills the config editor. `llm64.cfg` on the disk overrides it once you save settings |
| `BAUD38400=1` | Makes 38400 the boot default. All three rates are selectable in F1 -> E -> Speed on every build; this only picks the default |
| `DIAG=1` | Compiles in the crash post-mortem: a breadcrumb ring and a C-stack canary at `$02A7`, both readable with PEEK after the machine drops to BASIC |
| `DEBUG_CLIENT=1` | Runs a scripted diagnostic session instead of the TUI |

Leave `MODE80=1` off and you get the 40-column build, which the older
`emu` and `c64` targets still use. It has no pictures and no printing.

## Running it

### In VICE

```bash
./run.sh emu-80                  # build, make the boot disk, launch x64sc
./run.sh emu-80 10.0.0.5:6400    # a proxy elsewhere, for this run only
./run.sh config                  # only if the proxy is not on this machine
```

`emu-80` works out of the box: with no `run.conf` it aims at
`127.0.0.1:6400` and starts a proxy itself if nothing is listening there.
Set `PROXY_HOST` in `run.conf` when your proxy is on another machine.

The emulator build uses `CONNECT=direct`, so VICE opens the TCP connection
itself and the client's own config editor cannot change where it connects
(the address shown there is ignored). To exercise the real dial path
instead, `make test-emu-hayes` builds `CONNECT=hayes` and puts `tcpser`
behind VICE's RS-232 as the modem.

VICE has no SwiftLink crystal, so a rate you select in the client runs at
half its label under emulation. On hardware the label is the real rate.

`make test-all` from the repo root runs the automated end-to-end suites,
which you only need when working on the client itself.

### On a C64 Ultimate

Enable the Ultimate's FTP server (F2 -> Network settings), then deploy the
whole disk:

```bash
make deploy-c64u-disk-80 C64U_IP=<ultimate-ip> \
     C64_PROXY_IP=<proxy-ip> C64_PROXY_PORT=6400
```

That uploads `llm64.d64` to `/Flash` and drives the Ultimate's own menu
over telnet to mount and run it. Booting from the disk gets you the
fastloader, the overlay modules, and `llm64.cfg` saved back into the image.
The disk ships without a config, so the first boot opens the config editor.

Two shortcuts take the same addresses from `run.conf` instead (`C64U_HOST`
and `PROXY_HOST`, which `./run.sh config` sets up):

```bash
./run.sh c64-80     # build the PRG, upload it to /Flash, run it
./run.sh install    # build and upload the PRG to /Flash without running it
```

Both upload the PRG on its own, which is fast for a code change. With no
disk beside it the F1 menu falls back to its built-in form, and the entries
backed by overlay modules (config editor, conversation manager, jukebox,
disk copier) report a module load failure instead of running.

You can also move the D64 across yourself: FTP it to `/Flash`, or put it on
a USB stick and mount it from the Ultimate's menu.

Give the client an address the C64 can reach on its own, because it dials
the proxy itself. `127.0.0.1` means the C64, and a VPN or tailnet address
that resolves on your workstation is not where the proxy is.

Set the ACIA up in the Ultimate's Cartridge/IO settings:

- SwiftLink-compatible 6551 at **$DE00**, with modem emulation on.
- Interrupt on **NMI**. The client's NMI handler keeps draining the ACIA
  through disk loads and SID playback, which is what makes the higher
  rates reliable. IRQ also works (it is what VICE uses) but is slower to
  recover.
- In the modem settings, disable *drop connection on DTR low* and *RTS
  handshake RX*, and enable *automatic RX pushback*. The emulated control
  lines are re-evaluated on ACIA command writes, and the wrong settings
  drop data.
- For `/print`, enable F2 -> Software IEC Settings -> *IEC Drive and
  Printer*. It is off by default, and without it `/print` reports that
  there is no printer on device 4.

Pick the wire speed in the config editor (F1 -> E -> Speed): **9600,
19200 or 38400**. SwiftLink's crystal doubles the 6551's baud table and
that doubling is already in the labels, so the number you pick is the rate
you get. There is nothing to set on the Ultimate's modem side (it follows
the ACIA) or on the proxy (the client reports its rate on connect and the
proxy paces to match). If a rate drops data on your firmware, step it down
and reboot.

For the full checklist, including what to do when it will not connect, see
[docs/05-ultimate-setup.md](../docs/05-ultimate-setup.md).

### On a real C64 or C128

The client needs two things, and does not care about the rest of the
machine:

1. **A 6551 ACIA at `$DE00`.** A SwiftLink or Turbo232 cartridge is
   exactly that. A userport modem is not, and will not work.
2. **Something on the ACIA's RS-232 side that answers Hayes AT commands**
   and can `ATDT<host>:<port>`. An ESP-based WiFi modem does this, as does
   a null-modem cable to a PC running `tcpser`. Build with
   `CONNECT=hayes`.

Get the disk there however you normally move a D64: an SD2IEC, a Pi1541,
or a ZoomFloppy/X1541 cable to a real 1541. Use a fastloader or JiffyDOS
if you can, because the client loads overlay modules off the disk while
you use it, and a stock 1541 makes that slow.

The driver is tested against the C64 Ultimate's ACIA emulation rather than
a real cartridge, so if a genuine SwiftLink misbehaves, start at 9600 and
check the modem's handshake lines.

## First boot

On a disk with no config, the config editor opens first and asks for the
proxy address and the wire speed, then saves `llm64.cfg` onto the disk
itself. From then on the disk carries its own settings, and you can change
them at any time with F1 -> E. Anything compiled in with `SERVER_IP` is
only the value the editor pre-fills.

## Layout

```
src/main.c        TUI, transfer state machines, mode handling
src/serial.s      ACIA driver (IRQ and NMI vectors, ring buffers)
src/soft80.s      80-column bitmap renderer; font48.s is its font
src/music.s       SID player, driven off the raster IRQ
src/display.c     screen composition, status bars, pictures
src/editor.c      line editor; keyboard.s scans the matrix itself, for
                  n-key rollover at around 150 WPM
src/protocol.c    framing, message dispatch; crc.s
src/cfg.c         llm64.cfg on the boot disk
src/loader.c      overlay loader; modslot.s is the slot they load into
src/mod_*.c       the overlays: config editor, conversation manager,
                  jukebox, disk copier, F1 menu
src/diag.s        crash post-mortem, compiled in with DIAG=1
include/          headers, and the generated buildhash.h
intro/            the shareware intro and its own three-voice tune
c64-soft80.cfg    linker config capping the program below $C000, where
                  the ASCII shadow, colour matrix and bitmap live
build/            llm64.prg, the overlay modules, llm64.d64
```
