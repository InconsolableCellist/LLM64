# LLM64

LLM64 lets a Commodore 64 (or a Windows 3.11/95/98 PC) play an
infinite D&D-style text adventure with a local (or remote) Large Language
Model like Gemma 4, ChatGPT, Claude, Grok, GLM, Kimi, etc.

![Status](https://img.shields.io/badge/status-working-brightgreen)
![Platform](https://img.shields.io/badge/clients-C64%20%2F%20C64%20Ultimate%20%2F%20VICE-red)
![Platform](https://img.shields.io/badge/clients-Windows%203.1%20%2F%203.11%20%2F%2095-blue)
![Language](https://img.shields.io/badge/c64-C%2FASM%20(cc65)-orange)
![Language](https://img.shields.io/badge/win16-C%20(Open%20Watcom)-orange)
![Language](https://img.shields.io/badge/proxy-Python%203.10%2B-green)

LLM64 has the following main features:

1. Play a fully interactive, custom, D&D style text adventure, with the narrator
   streaming period-appropriate music, period-appropriate images, and keeping
   track of a map and your character sheet
2. Chat with an AI Assistant personality, the raw model, or with SillyTavern-compatible
   character cards
3. Integrate with Claude Code and drive the session (even updating itself!)
4. Intelligently print any content (e.g., "/print my character sheet" or 
   "/print please give me a summary of the story so far, with plot points and
   the result of combat" or "/print the complete recipe we just discussed")

Everything runs through one **proxy** — a small Python server on a modern
machine that holds the conversations, talks to the model, converts the
images, and streams the music. The clients are programs that speak
a binary protocol to the proxy while being period-correct.

```
┌─────────────────┐  TCP over
│  C64 / VICE /   │  a SwiftLink ACIA ┐
│  C64 Ultimate   │  (9600-38400 bd)  │   ┌──────────────┐         ┌──────────────────┐
└─────────────────┘                   ├──►│ Linux proxy  │  HTTPS  │ OpenAI-compatible│
┌─────────────────┐                   │   │  (Python,    │ ◄─────► │ API (llama.cpp,  │
│ Windows 3.1 /   │  TCP over         │   │   asyncio)   │   SSE   │ OpenAI, Ollama…) │
│ 3.11 / 95 PC    │  Winsock 1.1     ─┘   └──────────────┘         └──────────────────┘
└─────────────────┘
```

## C64 and Win 3.11/95/98 Clients

| | |
|---|---|
| ![The C64 client in an adventure](screenshots/c64_client.png) | ![The Windows 3.11 client](screenshots/win311_client.png) |
| The C64 with soft-80 bitmap text and SID music | the same adventure on Windows 3.11 with MIDI music |

| | [**C64**](c64_client/README.md) | [**Windows 3.11/95/98** ](win311_client/README.md) |
|---|---|---|
| Runs on | a real C64/C128, a C64 Ultimate, or VICE | a real 386/486, a VM, or Wine |
| Built with | cc65 (C + 6502 asm) | Open Watcom V2, cross-compiled from Linux |
| Talks to the proxy through | a SwiftLink-compatible 6551 ACIA at `$DE00`, dialling Hayes AT | a TCP socket, Winsock 1.1 |
| Screen | 80 columns of soft-80 bitmap on a 64 KB machine | MDI: a desk of windows you arrange |
| Pictures | 160x200 multicolour, Pepto palette, dithered | 320x200 8-bit DIB, period palette, dithered |
| Music | SIDs relocated to `$B000` and streamed into RAM | `.MID` files through the MIDI Mapper |
| `/print` | a real printer on IEC device 4 (or the proxy's CUPS queue) | virtual paper in a Notebook window (or real printer with CUPS) |
| Install steps | [c64_client/README.md](c64_client/README.md) | [win311_client/README.md](win311_client/README.md) |

## Using it

Start the proxy first ([installation](#installation)), then
configure the client.

**On a C64**, launch the program by mounting the disk image or real disk
(`LOAD"*",8,1`), which then loads modules from that same disk. A
fastloader or JiffyDOS is of course highly recommended. On initial startup
it'll initialize your Hayes-compatible MODEM and then ask you for the IP
address and port of the LLM64_Proxy running on your network (you can
change this later with the F1 menu). After connecting, you can hit F1 to
browse the various features, or press F5 to quickly get to a sortable list
of your past conversations/roleplays/adventures.

Use `/help` and press return to get more help. Press F4 and F6 to page
up/down, and the cursor keys to scroll.

The top bar displays the program name and build hash, a few shortcuts, a link
to my site ([foxipso.com](https://foxipso.com)), and the scrollback percent.
(Use `/history` to scroll back even more, and `/find` or `/findall` to
search.)

The bottom bar displays some status text, such as "Ready. Type your message."
or the currently playing song (in adventure mode). The bottom right may
display a "!P" to indicate a picture is waiting for you in adventure mode
(use `/pic` to see, or `/pics` to list all past pictures for that adventure).
It may also display "PIC:n" where `n` is the number of pics generated in that
adventure (when no new picture is waiting for you to view it).

**On the Windows 3.11 client**, run `LLM64.EXE` with `LLM64.INI` beside
it; it connects on startup to the address in that file (Settings->Server... changes it from inside the program). The same slash commands work, and most features have their own MDI windows: picture, MIDI, character sheet, items, notebook, map, selectable with the top menubar. The status strip
shows the client's state on the left and the proxy's state (room,
now playing) on the right.

### Pictures

Pictures are generated by configuring the LLM64_Proxy to hit an image
generation backend, such as Nano Banana or a ComfyUI API compatible server
(see [Image generation](llm64_proxy/README.md#image-generation)).

You can generate images with custom prompts using `/pic <prompt>`, or `/pic`
to indicate that the adventure-mode narrator should generate a picture for
you. Images are converted to C64 multicolor (160×200, Pepto palette,
Floyd–Steinberg dither) with an LLM-written caption burned into the frame.

The Windows client gets the same picture
as a 320×200 8-bit DIB (Mode 13h dimensions, a fixed period palette,
Floyd–Steinberg against it) rather than the C64's 16-colour blob.

You can browse past pictures associated with the current conversation using `/pics`

![A generated scene on the C64: multicolour bitmap with the caption burned in](screenshots/c64_pic.png)

The Windows client also has a checkbox to auto-generate a picture on every new room, not just when the narrator chooses.

(Note that with this mode off, or on the C64, pictures are only ever generated when you type /pic, so that tokens aren't spent unnecessarily.)


### Music

With the F1 menu you can also browse and play SIDs, streamed from the proxy,
with the jukebox `j` feature.

In adventure mode the narrator can choose which SIDs to play based on
pre-computed categories, which will stream over the network to your C64's
RAM, where they will reside and play. The narrator will periodically decide
it's time for a new song or mood, and you can also control it with the
jukebox or `/music`. Over 10,000 SIDs are available, across 15 moods. They're
also preprocessed to reside at the proper address, and are volume normalized.

I've automated the assignment of moods based on the game title, but some of them
are mis-sorted, which I'm working through (hopefully).

On Windows the client plays `.MID` files through the MIDI
Mapper from a separate mood-tagged library. See [Music for the Windows client](llm64_proxy/README.md#music-for-the-windows-client-midi)
for how that library is built; the SID library for the C64 is
[here](llm64_proxy/README.md#building-the-sid-music-library-the-c64s-music).

### Conversations, Misc.

All conversations are viewable on the LLM64_Proxy in the
`data/conversations` directory.

The C64 program also contains a small utility to copy itself to a blank disk in
another drive, accessible in the F1 menu.

## Keys and commands

![The F1 menu, fed by the proxy, over a list of past pictures](screenshots/c64_menu.png)

On the C64:

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

`/help` on the C64 lists all slash commands: modes (`/chat`, `/adventure`,
`/char`, `/code`), `/music <mood>`, `/pic [desc|n]`, `/pics`, `/history`,
`/find`, `/findall`, `/save`, `/restore`, `/model`, `/stats`,
`/print [what]`.

LLM64 implements its own keyboard scanning that generally allows for
n-key rollover and typing speed of around 150 WPM.

The Windows client takes the same slash commands, and you can also use `Ctrl+1..7` to toggle the desk's windows, F1 is the server-fed menu as
a box of buttons, F5 the conversation browser.

## Installation

1. [Install the proxy](llm64_proxy/README.md#installation) — a venv and
   three packages on a machine your old hardware can reach
2. [Configure the proxy](llm64_proxy/README.md#configuration) — copy
   `config.toml.example` and point `[api]` at your model, then
   [start it](llm64_proxy/README.md#running-the-proxy)
3. Download (or build) the [C64](c64_client/README.md#build) or
   [Windows](win311_client/README.md#build) client — pre-compiled binaries
   are on the Releases page
4. Run the client — [on a C64, a C64 Ultimate or VICE](c64_client/README.md#running-it),
   or [on a PC, a VM or Wine](win311_client/README.md#running-it)
5. Optionally, [enable more features in the proxy](llm64_proxy/README.md#optional-features)
   — pictures, SID music, MIDI music, real printing, `/code`


## Trying it out

Some prompts to test with once the proxy is up:

- **Chat:** just type anything — `what's special about the SID chip?`
- **Adventure:** `/adventure` picks from a theme chooser, or
  `/adventure haunted castle`. Then classic commands (`LOOK`, `GO NORTH`,
  `EXAMINE the altar`, `INVENTORY`) or free-form actions. Step outside the
  story with `[OOC: make the dragon friendlier]`. Dice roll for real:
  `I attack [roll:1d20]`.
- **Roleplay:** `/chars` lists your character cards, `/char <name>` starts
  one, `/assist` chats with the built-in Assistant card.
- **Pictures:** `/pic` (illustrate the current scene), `/pic a knight at a
  campfire`, `/pics`, `/pic 1` (re-show).
- **Music:** `/music urgent`, `/music next`, `/music stop`, `/auto` (hand
  control back to the narrator).
- **Printing:** `/print` puts the last reply on paper, `/print my
  inventory` the character sheet, `/print the complete recipe`
  whatever you ask for - composed on the proxy and printed through a
  printer on IEC device 4 (a real MPS-803, the C64 Ultimate's virtual
  printer, or VICE's). Soft-80 builds only. `[printer] backend` in
  config.toml can send the same document to a CUPS queue instead
  ("cups") or as well ("both") - a printer on the proxy host or shared
  by a Pi behind the C64, which also gives you /print with no C64
  printer at all ([Printing](llm64_proxy/README.md#printing)). On the Windows client the
  same document arrives as virtual paper in the Notebook window, which
  needs no printer of any kind.
- **Claude Code:** `/code` (or `/code sonnet`) drives a coding-agent session
  from the C64, tool approvals answered at the prompt.
- **Housekeeping:** `/save`, `/restore`, `/history`, `/find <text>`,
  `/findall <text>`, `/stats`.

## Repository layout

```
c64_client/     cc65 client (main.c TUI + transfer state machines,
                serial.s ACIA driver, soft80.s bitmap renderer,
                music.s SID player, display/editor/protocol)
                -> c64_client/README.md
win311_client/  Win16 client, Open Watcom, cross-built from Linux
                (wire.c framing, scroll.c transcript, net.c Winsock,
                main.c the MDI desk); tests/ run on the host
                -> win311_client/README.md
llm64_proxy/    Python proxy (src/), and tools/:
                sid_scan, sid_reloc_batch, sid_mood (LLM tagger),
                sid_loudness, sid_makedb, sid_rank (scene regard),
                sid_review (listen and retag/block by hand),
                sid_build (runs all of the above), img2c64,
                midi_fetch/scan/mood/makedb (the MIDI library),
                midi_audition, midi_dualcheck
emu/            VICE automation: e2e harness, mock LLM, watchdog suite,
                binary-monitor client
tools/          host-side asset builders and setup-printer-pi.sh
docs/           design docs + setup guides
run.sh          one-stop launcher: proxy, emulator, hardware deploys
```

## Documentation

Installation and configuration live with each piece — the
[proxy](llm64_proxy/README.md), the [C64 client](c64_client/README.md),
the [Windows client](win311_client/README.md). The docs below are design
records instead.

- [01-system-architecture.md](docs/01-system-architecture.md),
  [02-c64-client-design.md](docs/02-c64-client-design.md),
  [03-linux-proxy-design.md](docs/03-linux-proxy-design.md) — original design
- [05-ultimate-setup.md](docs/05-ultimate-setup.md) — real hardware setup
- [16-windows-311-client.md](docs/16-windows-311-client.md) — the Windows
  client, the multi-client profile design, and §13b for the desk as it
  stands
