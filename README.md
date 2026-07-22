# C64 LLM Interface

Chat with modern LLMs from a Commodore 64 — with an AI game master,
a 10,000-tune SID soundtrack it conducts itself, and generated
multicolor illustrations, captioned and burned into the frame. A
native TUI client (cc65 C + 6502 assembly) talks through a 6551 ACIA
to a Python proxy that bridges to any OpenAI-compatible API, and the
whole stack is verified end-to-end by automated tests running in VICE.

![Status](https://img.shields.io/badge/status-working-brightgreen)
![Platform](https://img.shields.io/badge/platform-C64%20%2F%20VICE%20%2F%20C64%20Ultimate-red)
![Language](https://img.shields.io/badge/c64-C%2FASM%20(cc65)-orange)
![Language](https://img.shields.io/badge/proxy-Python%203.10%2B-green)

```
┌─────────────────┐         ┌──────────────┐         ┌──────────────────┐
│  C64 / VICE /   │  TCP    │ Linux proxy  │  HTTPS  │ OpenAI-compatible│
│  C64 Ultimate   │ ◄─────► │  (Python,    │ ◄─────► │ API (llama.cpp,  │
│  TUI client     │ 9600bd  │   asyncio)   │   SSE   │ OpenAI, Ollama…) │
└─────────────────┘         └──────────────┘         └──────────────────┘
```

## What works today

- **Soft-80 bitmap TUI**: an 80-column display on stock hardware (VIC
  bank 3 bitmap with a 4×8 font), streaming word-wrapped chat,
  ~120-line scrollback, 120-char input editor with Emacs bindings,
  conversation browser, build hash in the title bar
- **AI-conducted SID music**: a pipeline (HVSC scan → `sidreloc`
  relocation to a protected 4KB window → LLM mood-tagging → loudness
  normalization) produced a 10,000-tune library across 15 moods; in
  adventure and roleplay modes the model steers the soundtrack with
  `[[MUSIC: mood]]` directives, stripped from the visible text and
  streamed to the SID mid-conversation
- **Generated illustrations**: `[[IMAGE: …]]` directives (or `/pic`)
  render scenes via an image model, converted to C64 multicolor
  (160×200, Pepto palette, Floyd–Steinberg dither, auto-levels), with
  an LLM-written caption burned into the frame — scene descriptions
  carry character/setting continuity from earlier illustrations
- **A hardened wire protocol**: CRC-framed messages, BEGIN handshakes,
  windowed flow control (the client ACKs every 4th chunk so the
  modem's buffer can never overflow), offset-addressed chunks, and
  per-transfer loss diagnostics in the status bar — the result of an
  extended real-hardware debugging campaign against a
  packet-dropping modem bridge
- **Interrupt-driven serial**: 6551 ACIA driver with an 8KB IRQ/NMI RX
  ring — verified zero data loss on multi-KB streams at real C64 speed
- **Custom keyboard driver**: replaces the KERNAL scanner; full
  rollover with ghost-blocking, verified with real X11 keystrokes
- **Interaction modes**: `/adventure [theme]` text-adventure GM,
  `/char <name>` SillyTavern character-card roleplay (both with music
  and illustration directives), `/chat` plain chat — per-mode sampling
- **Conversation tooling**: persistent conversations with mode/music/
  image metadata (loading one restores the adventure, the soundtrack,
  and the character), a full conversation manager (F5: paged list,
  starring, delete-with-confirm — an overlay module), `/history`
  paging, `/find` search, `/findall` cross-conversation search,
  `/save`//`/restore` checkpoints
- **Disk-loaded overlay modules**: sub-applications live on the boot
  disk as cc65 overlay files and load on demand into a fixed RAM slot
  below the C stack — modal UIs without growing the resident client.
  Modules so far: a config editor that un-bakes the proxy address into
  `c64llm.cfg` on disk (runs at boot when no config exists), the
  conversation manager, a disk copier that replicates the distribution
  onto another drive, and the F1 menu itself — a floating retro dialog
  whose entries are **server-fed** (label + command pairs from the
  proxy, mode-aware), so the menu and `/help` share one source of
  truth and new commands need no client rebuild. JiffyDOS (or any
  fastloader) strongly recommended — stock KERNAL loads work but crawl
- **Automated end-to-end tests in VICE**: `make test-all` boots mock
  LLM → proxy → emulated C64 and asserts on actual screen contents and
  memory (70+ asserts, including image bitmap bytes and SID play
  vectors), plus a frame-drop/watchdog recovery suite

## Quick start (emulator)

Prereqs: `cc65`, `vice` (x64sc), `python3`, and `tcpser` for the
Hayes-mode test. VICE can be a distro package or the `net.sf.VICE`
flatpak — `emu/vice-run.sh` finds either.

```bash
# one-time proxy setup
cd c64llm_proxy && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.toml.example config.toml   # point it at your API
cd ..

make test-all       # run the automated suites
make run-live       # interactive TUI against your configured API
```

See [docs/04-emulator-setup.md](docs/04-emulator-setup.md) for the VICE
wiring details (and the archaeology of why naive VICE setups fail).

## Quick start (real C64 Ultimate)

```bash
make deploy-c64u-disk-80   # build, make the d64, mount + run on the U64
```

Everything ships as one disk image: `make -C c64_client disk` produces
`build/c64llm.d64` holding the client (`LOAD"*",8,1` boots it) and the
overlay modules. Mount it on the Ultimate's 1541 (JiffyDOS fastload
applies) — or write it to a real floppy. On first boot the config
editor asks for the proxy address and saves `c64llm.cfg` back onto the
disk itself; from then on the disk carries its own settings (edit any
time via F1 → E). The baked `SERVER_IP` is only the pre-filled default.

Note: the overlay modules are linked against their exact PRG — they
always travel together on the disk, never mix builds.

Checklist: [docs/05-ultimate-setup.md](docs/05-ultimate-setup.md)
(ACIA/SwiftLink at $DE00 + modem emulation enabled). Modem settings
that matter: disable *drop connection on DTR low* and *RTS handshake
RX*, enable *automatic RX pushback* — the emulated control lines are
re-evaluated on ACIA command writes and the wrong settings drop data.

## Keys and commands

| Key | Action |
|-----|--------|
| Return | Send message |
| F1 | Menu — server-fed panel, hotkeys or cursor+Return |
| F2 / F3 | New conversation / cancel reply |
| F4 / F6 | Page chat up / down |
| F5 | Conversation manager (load, star, delete, pages) |
| F7 | Help |
| CRSR up/down | Scroll chat |
| Ctrl-A/E, Ctrl-K/D, CLR | Editor: home/end, kill, delete, clear |

`/help` on the C64 lists all slash commands: modes, `/music <mood>`,
`/pic [desc|n]`, `/pics`, `/history`, `/find`, `/findall`, `/save`,
`/restore`, `/model`, `/stats`.

## Repository layout

```
c64_client/     cc65 client (main.c TUI + transfer state machines,
                serial.s ACIA driver, soft80.s bitmap renderer,
                music.s SID player, display/editor/protocol)
c64llm_proxy/   Python proxy (src/), SID pipeline tools (tools/):
                sid_scan, sid_reloc_batch, sid_mood (LLM tagger),
                sid_loudness, sid_makedb, img2c64
emu/            VICE automation: e2e harness, mock LLM, watchdog suite,
                binary-monitor client
docs/           design docs + setup guides
```

## Build modes

| Flag | Meaning |
|------|---------|
| `MODE80=1` | Soft-80 bitmap UI (the primary experience) |
| `CONNECT=direct` | No modem handshake; ACIA pipe is the connection (VICE) |
| `CONNECT=hayes` | AT-command dial (C64 Ultimate, or VICE+tcpser) |
| `SERVER_IP=` / `SERVER_PORT=` | Default proxy address (overridden by `c64llm.cfg` on disk) |
| `DEBUG_CLIENT=1` | Scripted diagnostic session instead of the TUI |

## Configuration (proxy)

Copy `c64llm_proxy/config.toml.example` to `config.toml` and point it
at your server. Optional sections enable the extras: `[images]`
(generation mode ask/auto/off), music activates automatically when
`data/sids/moods.json` exists. Environment overrides: `OPENAI_API_BASE`,
`OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_SYSTEM_PROMPT`.

## Roadmap

More overlay modules (sound window with oscilloscope, prompt/template
editor), 19200/38400 baud, screensaver/ambient mode. Claude Code
integration shipped: `/code` drives a coding-agent session from the
C64, tool approvals answered at the prompt.

## Documentation

- [00-overview.md](docs/00-overview.md), [01-system-architecture.md](docs/01-system-architecture.md),
  [02-c64-client-design.md](docs/02-c64-client-design.md), [03-linux-proxy-design.md](docs/03-linux-proxy-design.md) — original design
- [04-emulator-setup.md](docs/04-emulator-setup.md) — VICE + automation
- [05-ultimate-setup.md](docs/05-ultimate-setup.md) — real hardware
- [06-modes.md](docs/06-modes.md) — adventure & character-card roleplay

---
