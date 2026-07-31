# LLM64

LLM64 lets a Commodore 64 (or a Windows PC, anything from 3.11 to
Windows 11) play an infinite D&D-style text adventure with a local (or
remote) Large Language Model like Gemma 4, ChatGPT, Claude, Grok, GLM,
Kimi, etc.

You can:

1. Play a fully interactive, custom, D&D style text adventure, with the narrator
   streaming period-appropriate music, period-appropriate images, and keeping
   track of a map and your character sheet
2. Chat with an AI Assistant personality, the raw model, or with SillyTavern-compatible
   character cards
3. Integrate with Claude Code and drive the session (even updating itself!)
4. Intelligently print any content (e.g., "/print my character sheet" or 
   "/print please give me a summary of the story so far, with plot points and
   the result of combat" or "/print the complete recipe we just discussed")


## Support

LLM64 is released as donationware. If you enjoy it, please consider donating
to support my work! The recommended donation is $10.

- Donate on [Ko-fi](https://ko-fi.com/foxipso)
- Or join my [Patreon](https://www.patreon.com/c/foxipso) for early access,
  exclusives, and also to support my work!

Join my Discord for support/updates, and/or my X/Twitter:

- Discord, [Foxipso's Den](https://discord.gg/2jYw4swm3X)
- Twitter/X, [@TheFoxipso](https://x.com/TheFoxipso)

## Quickstart

1. [Install the proxy](llm64_proxy/README.md#installation). You'll setup Python in a venv on any modern machine
   -- or skip Python entirely with the [standalone binary](llm64_proxy/README.md#standalone-binary-no-python),
   one file with a launcher window (status, log, config editor) for Windows or Linux
2. [Configure the proxy](llm64_proxy/README.md#configuration). Copy
   `config.toml.example` and point `[api]` at your model, then
   [start it](llm64_proxy/README.md#running-the-proxy)
3. Download (or build) the [C64](c64_client/README.md#build) or
   [Windows](win311_client/README.md#build) client — pre-compiled binaries
   are on the Releases page
4. Run the client — [on a C64, a C64 Ultimate or VICE](c64_client/README.md#running-it),
   or [on a modern or period PC, a VM or Wine](win311_client/README.md#running-it)
5. Optionally, [enable more features in the proxy](llm64_proxy/README.md#optional-features)
   — pictures, SID music, MIDI music, real printing, `/code`

## The Clients: C64, Win 3.11/95/98, and modern Windows

| | | |
|---|---|---|
| ![The C64 client in an adventure](screenshots/c64_client.png) | ![The Windows 3.11 client](screenshots/win311_client.png) | ![The same desk on Windows 11](screenshots/win11.png) |
| The C64 with soft-80 bitmap text and SID music | the same adventure on Windows 3.11 with MIDI music | and on Windows 11, chrome and all |

| | [**C64**](c64_client/README.md) | [**Windows 3.11/95/98** ](win311_client/README.md) | [**Windows 10/11**](win311_client/README.md#on-modern-windows-10-and-11) |
|---|---|---|---|
| Runs on | a real C64/C128, a C64 Ultimate, or VICE | a real 386/486, a VM, or Wine | any Windows 10/11, x64 or ARM |
| Built with | cc65 (C + 6502 asm) | Open Watcom V2, cross-compiled from Linux | mingw-w64, from the same sources |
| Talks to the proxy through | a SwiftLink-compatible 6551 ACIA at `$DE00`, dialling Hayes AT | a TCP socket, Winsock 1.1 | the same socket, Winsock 2 |
| Screen | 80 columns of soft-80 bitmap on a 64 KB machine | MDI: a desk of windows you arrange | the same desk, its 3.1 chrome drawn by the client |
| Pictures | 160x200 multicolour, Pepto palette, dithered | 320x200 8-bit DIB, period palette, dithered | the same DIBs |
| Music | SIDs relocated to `$B000` and streamed into RAM | `.MID` files through the MIDI Mapper | `.MID` files through the built-in GS synth |
| `/print` | a real printer on IEC device 4 (or the proxy's CUPS queue) | virtual paper in a Notebook window (or real printer with CUPS) | the same Notebook |
| Install steps | [c64_client/README.md](c64_client/README.md) | [win311_client/README.md](win311_client/README.md) | [win311_client/README.md](win311_client/README.md#on-modern-windows-10-and-11) |
```
┌─────────────────┐  TCP over
│  C64 / VICE /   │  a SwiftLink ACIA ┐
│  C64 Ultimate   │  (9600-38400 bd)  │   ┌──────────────┐         ┌──────────────────┐
└─────────────────┘                   ├──►│ Linux proxy  │  HTTPS  │ OpenAI-compatible│
┌─────────────────┐                   │   │  (Python,    │ ◄─────► │ API (llama.cpp,  │
│ Windows 3.1-98, │  TCP over         │   │   asyncio)   │   SSE   │ OpenAI, Ollama…) │
│ 10 or 11 PC     │  Winsock 1.1/2   ─┘   └──────────────┘         └──────────────────┘
└─────────────────┘
```


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

**On the Windows client**, run `LLM64.EXE` (16-bit) or `LLM32.EXE` (on
modern Windows) with `LLM64.INI` beside it; it connects on startup to the address in that file (Settings->Server... changes it from inside the program). The same slash commands work, and most features have their own MDI windows: picture, MIDI, character sheet, items, notebook, map, selectable with the top menubar. The status strip
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
Mapper (on modern Windows, the built-in GS synth) from a separate
mood-tagged library. See [Music for the Windows client](llm64_proxy/README.md#music-for-the-windows-client-midi)
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

## Repository layout

```
c64_client/     cc65 client (main.c TUI + transfer state machines,
                serial.s ACIA driver, soft80.s bitmap renderer,
                music.s SID player, display/editor/protocol)
                -> c64_client/README.md
win311_client/  the Windows client, cross-built from Linux twice from
                one source tree: Win16 NE with Open Watcom, Win32 PE
                with mingw-w64 (llmport.h is the seam; wire.c framing,
                scroll.c transcript, net.c Winsock, main.c the MDI
                desk); tests/ run on the host
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

- [Proxy](llm64_proxy/README.md) docs
- [C64 client](c64_client/README.md)
- [Windows client](win311_client/README.md)

and some additional design docs:

- [01-system-architecture.md](docs/01-system-architecture.md),
  [02-c64-client-design.md](docs/02-c64-client-design.md),
  [03-linux-proxy-design.md](docs/03-linux-proxy-design.md) — original design
- [05-ultimate-setup.md](docs/05-ultimate-setup.md) — real hardware setup
- [16-windows-311-client.md](docs/16-windows-311-client.md) — the Windows
  client, the multi-client profile design

## Version history

### 1.0 -- 2026-07-29

Initial release.
