# LLM64

LLM64 lets a 1982 Commodore 64 — or a 1993 Windows 3.11 PC — play an
infinite D&D-style text adventure with a local (or remote) Large Language
Model like Gemma 4, ChatGPT, Claude or Grok.

![Status](https://img.shields.io/badge/status-working-brightgreen)
![Platform](https://img.shields.io/badge/clients-C64%20%2F%20C64%20Ultimate%20%2F%20VICE-red)
![Platform](https://img.shields.io/badge/clients-Windows%203.1%20%2F%203.11%20%2F%2095-blue)
![Language](https://img.shields.io/badge/c64-C%2FASM%20(cc65)-orange)
![Language](https://img.shields.io/badge/win16-C%20(Open%20Watcom)-orange)
![Language](https://img.shields.io/badge/proxy-Python%203.10%2B-green)

It has the following main features:

1. Chat with an AI Assistant personality, the raw model, or with SillyTavern-compatible
   character cards
2. Play a fully interactive, custom, D&D style text adventure, with the narrator
   streaming period-appropriate music, the occasional period-appropriate
   image, and keeping track of a map and your character sheet
3. Integrate with Claude Code and drive the session (even updating itself!)
4. Intelligently print any content (e.g., "/print my character sheet" or 
   "/print please give me a summary of the story so far, with plot points and
   the result of combat" or "/print the complete recipe we just discussed")

Everything runs through one **proxy** — a small Python server on a modern
machine that holds the conversations, talks to the model, converts the
images, and streams the music. The clients are period programs that speak
one binary protocol to it, and nothing else.

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

## The two clients

| | |
|---|---|
| ![The C64 client in an adventure](screenshots/c64_client.png) | ![The Windows 3.11 client](screenshots/win311_client.png) |
| The C64, in 80 columns of soft-80 bitmap | the same adventure on a Windows 3.11 desk |

| | [**C64** →](c64_client/README.md) | [**Windows 3.11** →](win311_client/README.md) |
|---|---|---|
| Runs on | a real C64/C128, a C64 Ultimate, or VICE | a real 386/486, a VM, or Wine |
| Built with | cc65 (C + 6502 asm) | Open Watcom V2, cross-compiled from Linux |
| Talks to the proxy through | a SwiftLink-compatible 6551 ACIA at `$DE00`, dialling Hayes AT | a TCP socket, Winsock 1.1 |
| Screen | 80 columns of soft-80 bitmap on a 64 KB machine | MDI: a desk of windows you arrange |
| Pictures | 160x200 multicolour, Pepto palette, dithered | 320x200 8-bit DIB, period palette, dithered |
| Music | SIDs relocated to `$B000` and streamed into RAM | `.MID` files through the MIDI Mapper |
| `/print` | a real printer on IEC device 4 (or the proxy's CUPS queue) | virtual paper in a Notebook window |
| Install steps | [c64_client/README.md](c64_client/README.md) | [win311_client/README.md](win311_client/README.md) |

Both are equal clients of the same proxy, at the same time, in the same
adventure: `docs/16` §7 covers how the proxy serves each machine what it
can actually eat. The C64 came first and is the reason any of this exists;
the Windows client exists because once the modem was really a socket, the
6510 was the only thing that had needed one.

## Using it

Start the proxy first ([install steps below](#installing-the-proxy)), then
whichever machine you're using.

**On the C64**, launch the program by mounting the disk image or real disk
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
it; it connects on startup to the address in that file (Settings ▸
Server… changes it from inside the program). The same slash commands work,
and everything the C64 crowds onto one screen gets a window of its own —
picture, music, character sheet, items, notebook, map — off a launcher
strip, each window remembering where you last put it. The status strip
carries the client's own state on the left and the proxy's chrome (place,
now playing) on the right.

### Pictures

Pictures are generated by configuring the LLM64_Proxy to hit an image
generation backend, such as Nano Banana or a ComfyUI API compatible server
(see [Image generation](#image-generation) below).

You can generate images with custom prompts using `/pic <prompt>`, or `/pic`
to indicate that the adventure-mode narrator should generate a picture for
you now. Images are converted to C64 multicolor (160×200, Pepto palette,
Floyd–Steinberg dither) with an LLM-written caption burned into the frame.

One generation, two renderings: the Windows client gets the same picture
as a 320×200 8-bit DIB (Mode 13h dimensions, a fixed period palette,
Floyd–Steinberg against it) rather than the C64's 16-colour blob.

You can browse past pictures associated with the current conversation using `/pics`

![A generated scene on the C64: multicolour bitmap with the caption burned in](screenshots/c64_pic.png)

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

A 486 has no SID, so the Windows client gets `.MID` files through the MIDI
Mapper from a separate, identically mood-tagged library — same vocabulary,
so one narrator can score both machines in the same adventure. Building it
is [below](#music-for-the-windows-client-midi).

### Conversations, Misc.

All conversations are viewable on the LLM64_Proxy in the
`data/conversations` directory.

The program also contains a small utility to copy itself to a blank disk in
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

The Windows client takes the same slash commands, and puts the rest on
menus: `Ctrl+1..7` toggle the desk's windows, F1 is the server-fed menu as
a box of buttons, F5 the conversation browser.

## Installing the proxy

Every client needs this and nothing else, so do it first. It wants
Python 3.10+ on a machine the old hardware can reach; the base install is
about two minutes, and each optional part below (images, music, printing)
can be added later without touching the clients.

### 1. Clone, venv, dependencies

```bash
git clone https://github.com/InconsolableCellist/c64_llm.git
cd c64_llm/llm64_proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # httpx, toml, Pillow
```

Three packages, and that's the whole base install: `httpx` for the model,
`toml` for the config, `Pillow` for the picture conversion. No database,
no service, nothing system-wide.

### 2. Write a config

```bash
cp config.toml.example config.toml
$EDITOR config.toml
```

The one section that must be right is `[api]`:

```toml
[api]
base_url = "http://192.168.1.10:5000/v1"   # any OpenAI-compatible endpoint
key = "none"                               # local servers need no key
model = "gemma-3-27b-it"
max_context_tokens = 8192
```

Anything that serves OpenAI-compatible Chat Completions works: llama.cpp's
`llama-server`, LMStudio, vLLM, Ollama (if you must), or OpenAI itself
(then `base_url = "https://api.openai.com/v1"` and a real `key`, or the
`OPENAI_API_KEY` environment variable — every setting has an env
override). Claude is reached through `/code` mode instead, which drives
the `claude` CLI on the proxy host, so Claude Code has to be installed and
authenticated *there*.

Without a `config.toml` the proxy still starts and still accepts clients —
and then every reply fails with an API error, which is the single most
common "it's broken" report.

The full option reference is [Configuration](#configuration-proxy) below.

### 3. Name the proxy once, for the helper scripts

```bash
cd ..            # repo root
./run.sh config  # writes run.conf, then prints what it will use
```

`run.conf` holds `PROXY_HOST` / `PROXY_PORT` (and `C64U_HOST`, if you have
a C64 Ultimate). Everything in `run.sh` and both clients' deploy targets
read it, so the address lives in one place.

### 4. Start it

```bash
./run.sh proxy       # foreground, Ctrl-C stops it
./run.sh proxy-bg    # background; log in llm64_proxy/proxy-live.log
./run.sh stop        # stop the background one
./run.sh status      # what's up and what isn't
```

Or directly, if you'd rather not use the launcher:

```bash
cd llm64_proxy && .venv/bin/python -m src.main --host 0.0.0.0 --port 6400
```

It listens on TCP **6400** by default, on all interfaces — the old machine
dials in, so `0.0.0.0` (not loopback) is the point. Open the port if you
run a firewall: `sudo ufw allow 6400/tcp`.

### 5. Check it before involving a 40-year-old computer

```bash
ss -ltn | grep 6400          # listening?
nc <proxy-host> 6400         # reachable from elsewhere on the LAN?
```

A connection that opens and stays open is the proxy waiting for a framed
message. That is all the confirmation you need — the client will do the
talking.

### 6. Optional, in the order most people want them

| Want | Do this |
|------|---------|
| **Pictures** | A `[images]` backend and a key — one Gemini key is the shortest path. [Image generation](#image-generation) |
| **Music on the C64** | Build a SID library from your own HVSC copy: one resumable command. [Building the SID music library](#building-the-sid-music-library-the-c64s-music) |
| **Music on the PC client** | Build a MIDI library from VGMusic: four steps. [Music for the Windows client](#music-for-the-windows-client-midi) |
| **Paper** | A printer on the C64's IEC bus needs nothing here; a modern printer needs `lp` and a CUPS queue. [Printing](#printing) |
| **`/code`** | Claude Code installed and authenticated on this host, then `[claude]` |

Conversations, images and libraries all land under
`llm64_proxy/data/` (`[storage] data_dir`), which is deliberately outside
git — see [what this repo does and does not carry](#what-this-repo-does-and-does-not-carry).

Proxy-side reference and troubleshooting also live in
[llm64_proxy/README.md](llm64_proxy/README.md).

## Installing a client

Each client has its own README, because the toolchain, the deploy route
and the hardware settings have nothing in common. Both assume the proxy
above is already running.

- **[c64_client/README.md](c64_client/README.md)** — cc65 build, the
  bootable D64, running in VICE, deploying to a C64 Ultimate over FTP, the
  ACIA/`$DE00`/NMI settings, wire speed, and what a real breadbin needs.
- **[win311_client/README.md](win311_client/README.md)** — the Open Watcom
  cross-build, `make test`/`make run` under Wine, the floppy image for a
  VM, and what to install on a real 386/486 (Winsock, MIDI, 256-colour
  driver).

The short version, if you just want to see it move: `./run.sh emu-80`
builds the C64 client and launches VICE against your configured proxy;
`cd win311_client && make test && make run` does the equivalent for the
Windows client under Wine.

## Configuration (proxy)

Refer also to the included `config.toml.example`.

`config.toml` sections (environment variables override the file):

### `[api]` — the LLM backend

| Key | Default | Env override | Meaning |
|-----|---------|--------------|---------|
| `base_url` | `https://api.openai.com/v1` | `OPENAI_API_BASE` | Any OpenAI-compatible endpoint |
| `key` | `"none"` | `OPENAI_API_KEY` | Optional; local servers work keyless |
| `model` | `gpt-3.5-turbo` | `OPENAI_MODEL` | Model name (`/models` lists, `/model` switches) |
| `temperature` | `0.7` | `OPENAI_TEMPERATURE` | Chat-mode sampling |
| `max_tokens` | `2000` | `OPENAI_MAX_TOKENS` | Reply cap |
| `max_context_tokens` | `8192` | `OPENAI_MAX_CONTEXT` | Auto-detected from llama.cpp when possible |
| `system_prompt` | `""` | `OPENAI_SYSTEM_PROMPT` | Prepended to chat mode |
| `disable_thinking` | `true` | — | Suppresses Gemma/Qwen thinking blocks (thinking adds 20-25s of latency on a C64) |

### `[modes]` — adventure & roleplay

`user_name` (what `{{user}}` expands to in character cards), `cards_dir`
(default `./cards`, env `LLM64_CARDS_DIR`) for SillyTavern v1/v2/v3 cards
(`.json` or PNG-embedded). Optional `[modes.adventure]` and
`[modes.roleplay]` sampling tables (`temperature`, `top_p`, `top_k`,
`min_p`, `repetition_penalty`, `max_tokens`); when absent, a Gemma-tuned
preset is used.

### `[storage]`, `[serial]`, `[claude]`

- `[storage] data_dir` (default `./data`, env `LLM64_DATA_DIR`) —
  conversations land in `data/conversations/`, images in `data/images/`.
- `[serial] wire_baud` (default `9600`, env `LLM64_WIRE_BAUD`) — bulk
  transfer pacing. Only a *fallback*: modern clients announce their rate
  on connect (`MSG_SET_BAUD`) and the proxy paces to that automatically,
  so this just covers clients too old to report.
- `[claude] command`, `workdir`, `model` — the `claude` CLI invocation for
  `/code` mode (env `LLM64_CLAUDE_CMD`).

### Image generation

Set `[images] mode` to `ask` (the model suggests, you confirm with `/pic`),
`auto` (striking scenes illustrate themselves, rate-limited), or `off`
(directives ignored; explicit `/pic <desc>` still works). Optional
`style_prefix` wraps every prompt; the default is a dark-fantasy style — set
it to `""` if your ComfyUI workflow carries its own style. Backends
(`[images] backend`):

- **`gemini`** (Nano Banana, the default) — `[images.gemini]` with
  `model = "gemini-2.5-flash-image"` and a key from
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey), via
  `key` or the `GEMINI_API_KEY` env var.
- **`openai`** — any `POST /v1/images/generations` server (OpenAI,
  Together, LocalAI): `base_url`, `model` (default `dall-e-3`), `size`,
  `key` (or env `LLM64_IMAGES_KEY`).
- **`comfyui`** — a local ComfyUI instance: `url` (default
  `http://127.0.0.1:8188`), `workflow` (an API-format JSON export
  containing the literal token `{PROMPT}` in a node input), `timeout`,
  `randomize_seed`. No auth — keep it on a trusted LAN.
- **`fixture`** — a fixed local image, for tests.

Minimum to get pictures: install Pillow (it's in requirements.txt), set
`mode = "ask"`, and supply a Gemini key. Then type `/pic a snake-like
green dragon with one arm lover a burningi village` on the C64 to test.

### Building the SID music library (the C64's music)

Music activates automatically when `data/sids/moods.json` exists. No
library ships with this repo — every tune in HVSC is copyrighted by its
composer, and HVSC's own notice limits use to private enjoyment, so you
build your own from your own copy. One command does the whole thing:

```
# 1. Get HVSC (85 MB) from https://www.hvsc.c64.org/downloads
#    or https://hvsc.brona.dk/HVSC/HVSC_85-all-of-them.7z
# 2. Point the builder at it:
llm64_proxy/tools/sid_build.py --hvsc ~/Downloads/HVSC_85-all-of-them.7z
```

`sid_build.py` runs the seven pipeline stages in order with progress and
time estimates, and it is resumable — every stage is skipped when its
output already exists, so an interrupted build picks up where it stopped:

| stage | what happens | roughly |
| --- | --- | --- |
| unpack | HVSC `.7z` → `data/sids/C64Music/` | 1 min, 457 MB |
| sidreloc | fetch + build Linus Åkesson's relocator (MIT) | 10 s |
| scan | which tunes could fit the client's 4 KB window | 2 min |
| relocate | move each to `$B000`, verify, reject the rest | 1 h |
| songlengths | per-subtune durations from HVSC | 5 s |
| loudness | emulate each tune, measure RMS and `$D418` | 3 h |
| moods | an LLM tags each tune for the narrator | 1–2 h |
| database | assemble `moods.json` | 10 s |
| ranking | cross-reference the scene's opinion (below) | 2 min |

Only the mood tagger needs anything beyond the repo: any OpenAI-compatible
endpoint (`--llm-url`, the same llama.cpp server the proxy uses), or
`--tags` with a prebuilt tag file to skip it. `--no-loudness` skips the
one stage that needs `pyresidfp` + `py65`, at the cost of volume
normalization. `tools/sid_build.py --info` prints the links and the
licence position; `--dry-run` shows what is already done.

If the proxy lives on another machine, finish with

```
llm64_proxy/tools/sid_build.py --deploy user@proxyhost:/path/to/llm64_proxy
```

which rsyncs only what the proxy reads — the database, the ranking and
the relocated tunes, ~50 MB — and not the 457 MB HVSC tree.

#### Which tune is any good

The tagger says what a tune is *for*; nothing in it says whether the tune
is any *good*, and 10k tunes is ~100 hours of listening. So
`tools/sid_rank.py` cross-references the library against the C64 scene's
own published opinion, taken from the
[DeepSID](https://deepsid.chordian.net) database dump (one 8 MB download,
no crawling):

| signal | what it is |
| --- | --- |
| `compo` | party music-competition placings — an audience voted |
| `youtube` | tunes somebody thought worth filming |
| `usage` | how many CSDb releases re-use the tune |
| `composer` | DeepSID's register: the pros and the documented notables |
| `stil` | has an HVSC STIL entry at all |
| `csdb` | user rating of the releases it appears in (`--csdb-ratings`, network) |

```
tools/sid_rank.py --download --explain 40    # build data/sids/ranking.json
tools/sid_rank.py --missing 40               # best HVSC tunes NOT in the library
```

`ranking.json` publishes a percentile per tune ("better regarded than
this fraction of your library") plus the reason in words. `MusicLibrary`
loads it beside `moods.json` and weights selection by it —
`FLOOR + (1-FLOOR)·rank²` in `src/sid_ranking.py`, which measured on the
real library lifts the mean regard of what actually plays from 0.52 to
0.69 while still drawing one pick in nine from the bottom third. It is
deliberately a weighting and not a filter: most of HVSC is obscure demo
music nobody wrote about, and unheard is not the same as bad. Your own
jukebox favourites outrank all of it.

#### What this repo does and does not carry

No SID in this repository belongs to anyone else. The shareware intro's
tune is `c64_client/intro/tune/llm64_theme.s` — a three-voice player and
score written for this project, rebuilt with `make -C c64_client/intro
tune`. The built library, the relocated tunes and everything else under
`data/` stay out of git deliberately: HVSC's
`DOCUMENTS/Disclaimer.txt` limits those tunes to private enjoyment, which
covers your own machine and your own C64 but not redistribution — so
don't publish a built library or a disk image containing one.

Two HVSC files remain as end-to-end test fixtures
(`emu/fixtures/sids/`); swapping them for the intro's own tune is a small
change if that matters to you.

#### Fixing what the tagger got wrong

The tagger works from filenames and STIL notes, so it is often wrong -
an upbeat chiptune tagged `eerie` because the game was a horror game -
and some tunes are simply bad or relocate badly. `tools/sid_review.py`
is the ears in that loop: it deals random tunes, plays them through
VICE's `vsid` at the volume the C64 will use, and lets you retag, block,
or confirm them.

```
tools/sid_review.py                       # unheard tunes, best-regarded first
tools/sid_review.py --mood eerie --as-selected   # audit one mood bucket,
                                          #   likeliest-to-be-heard first
tools/sid_review.py --status blocked      # revisit your own rejects
```

Verdicts go to `src/sid_overrides.json` (version-controlled, deployed
with the proxy), never to the generated `moods.json`, and are applied
both when `sid_makedb` rebuilds the database and when the proxy loads
it - so re-running the tagger cannot undo them. A tune the tagger
guessed at is `source: auto` at runtime; one a person heard is
`source: manual` with the date.

### Music for the Windows client (MIDI)

The Windows 3.11 client has no SID chip, and a relocated 6502 memory
image means nothing to a 486. What that machine would actually have
played in 1993 is a `.MID` file through the MIDI Mapper, so it gets its
own library, built the same way from a different corpus. The moods are
the same words - `tools/midi_mood.py` imports its vocabulary from
`sid_mood.py` rather than copying it - so one narrator can score a
C64 and a PC in the same adventure and neither is offered a mood it
cannot play.

Same licence position as the SIDs, for the same reason: every file in
the corpus is copyrighted by whoever sequenced it, so you build your own
from your own copy and nothing lands in git.

The corpus is [VGMusic](https://www.vgmusic.com/), which is worth the
crawl for one specific reason. HVSC gives the tagger a path; a VGMusic
index page gives it the **game**, a **human-written song title** and the
**sequencer's name** for every file. "Undertale / An Ending" is evidence;
`AN_END.MID` is not.

There is no single `midi_build.py` yet - four steps, run from the repo
root:

```
# 1. Fetch. ~10k files, 300 MB, roughly an hour of polite crawling.
#    --platforms picks which of VGMusic's sections you want.
llm64_proxy/tools/midi_fetch.py

# 2. Scan. Parses every file: exact duration, instruments, and the
#    filters (too short, drum loops, MT-32-voiced). ~93% survive.
llm64_proxy/tools/midi_scan.py

# 3. Tag. Same LLM endpoint the SID tagger uses. Resumable.
#    Two workers, not more - three times out against one llama.cpp.
llm64_proxy/tools/midi_mood.py llm64_proxy/data/midi/scan.json \
    --base-url http://localhost:5000/v1 --workers 2 \
    -o llm64_proxy/data/midi/tags.json

# 4. Assemble the database the proxy reads.
llm64_proxy/tools/midi_makedb.py llm64_proxy/data/midi/scan.json \
    llm64_proxy/data/midi/tags.json -o llm64_proxy/data/midi/midi.json
```

Step 3 is the long pole, exactly as the mood stage is for SIDs: budget
several hours for a full 10k corpus. `--pilot 48` tags a small batch
first so you can read the results before committing to the run.

#### Listening to it before you trust it

The tags say what a tune is *for*. Nothing in them says whether the
result is pleasant, which is what `tools/sid_review.py` exists for on the
SID side. The MIDI equivalent renders the library to audio:

```
llm64_proxy/tools/midi_audition.py --per-mood 3
xdg-open llm64_proxy/data/midi/audition/index.html
```

That writes a page of clips grouped by mood, with the tags and scores
beside each one. Nothing on it is hand-picked: it calls
`MidiLibrary.pick(mood)` - the real selection path, with the real
weighting and the real iconic damping - so if the audition sounds wrong,
the library is wrong.

Rendering needs FluidSynth and a General MIDI SoundFont. Any will do;
on Arch, `soundfont-fluid` puts `FluidR3_GM.sf2` in
`/usr/share/soundfonts/`. Point the tool at it with `--sf2` if it is not
under `data/midi/soundfonts/`.

#### Which tune is any good, without a DeepSID

The SID library weights selection by the C64 scene's own published
opinion (above). Nothing publishes a ranking of game-music MIDI
sequences, so `midi_makedb.py` computes a `quality` percentile from the
file itself: velocity spread first (a human performs dynamics, a
converter emits 100, 100, 100), then how many parts are playing, length,
and drum balance. Like the SID ranking it is a weighting and never a
filter. It is a weaker signal than an audience voting at a demoparty,
and it is honest about being a proxy.

#### Status

Done end to end: the library and its pipeline are tested
(`tests/test_midi_library.py`), `MIDI_BEGIN/DATA/END` carry the file to a
`CAP_MIDI` client, and the Windows client spools it to a temp file and
plays it through MCI's sequencer with its own transport and jukebox. What
a win16 profile resolves to at runtime is therefore music — as long as
`data/midi/midi.json` exists. `tools/midi_dualcheck.py` shows what both
machines hear from one `[[MUSIC:]]` directive, against the two real
libraries.

### Printing

`/print` composes the document on the proxy and sends it to the 
`[printer] backend`:

- **`c64`** (default): the C64 prints it itself, through a printer on IEC
  device 4: a real MPS-80{1,2,3}, the Ultimate's built-in virtual printer, or
  VICE's device-4 emulation. On a C64 Ultimate the
  virtual printer is **off by default** 
  See [docs/05](docs/05-ultimate-setup.md)).
- **`cups`**: the proxy spools the document to a CUPS queue with `lp`
  instead, so any modern printer is supported. I recommend the NDYIN L80
  thermal A4 printer.
- **`both`** 

```toml
[printer]
width = 78         # the PRINTER's columns, not the screen's
formfeed = true    # eject the page at end of job (C64U buffers otherwise)
max_tokens = 2000  # generation budget for the document itself
backend = "both"                     # c64 | cups | both
cups_queue = "n80"                   # required for cups/both
cups_server = "printpi.local:631"    # "" = a queue on this same host
cups_options = "cpi=12 lpi=8"        # 78 columns needs 12 cpi to fit A4
cups_width = 0                       # 0 = share `width`; a roll is narrower
cups_feed_lines = 0                  # blank lines to clear a tear bar
```

**On a receipt/till roll** the defaults are wrong, because `width` is the
C64 printer's line. An 80 mm head prints 576 dots at 203 dpi — 72 mm, about
34 columns at 12 cpi — and a document wrapped at 78 is not re-wrapped by the
driver, it is cropped. Give the paper leg its own layout:

```toml
cups_width = 34                                    # what actually fits
cups_options = "cpi=12 lpi=8 PageSize=Custom.204x842"
cups_feed_lines = 5                                # clears the tear bar
```

Env overrides: `LLM64_PRINTER_BACKEND`, `LLM64_PRINTER_QUEUE`. A `cups`
or `both` backend with no `cups_queue`, or an unknown backend name, logs a
warning and falls back to `c64`.

**Maps and pictures.** `/print the map` prints the adventure map from
stored state — text, so it goes to either printer, drawn to each one's
width. `/print the picture` puts the conversation's last
illustration on the CUPS printer — the C64's own 16-colour, 160x200
rendering decoded from the blob it displayed, not the source image the
model painted, ordered-halftoned to 1-bit dots (`cups_pic_scale` printer
dots per C64 pixel, default 4). The C64 printer can't do this: the IEC
path is a text stream, so `backend = "c64"` refuses it. Both understand
the obvious phrasings (`/print the last image`, `/print what you drew`),
and `/print picture 2` counts the way `/pics` lists.

#### Getting CUPS going (Raspberry Pi print bridge, or the proxy host itself)

The printer hangs off whichever machine runs `cupsd` — a Pi tucked behind
the C64, or the proxy box. The network hop is IPP from the proxy to that
machine, never to the printer.

**On the machine with the printer.** `tools/setup-printer-pi.sh` does the
whole sequence — packages, driver, queue, sharing — with the printer
plugged in and switched on. Copy it over (it needs only bash); `--dry-run`
prints every command and runs none:

```bash
./setup-printer-pi.sh --driver ~/n80-driver --queue n80 --test
./setup-printer-pi.sh --queue laser --no-share        # printer on the proxy host
```

`--driver` is an extracted vendor CUPS driver, needed only for printers
that aren't driverless — e.g. the NDYIN/ZHJY N80 thermal, whose PPD +
`rastertoN80` filter ship for armv7l/aarch64 too. For an ordinary
network/USB laser or inkjet, leave `--driver` off and CUPS picks an
`everywhere` profile. What the script does, if you'd rather do it by hand:

```bash
cd ~/n80-driver && sudo ./install       # PPD + filter into CUPS, restart cupsd
sudo lpinfo -v                          # note the usb://... URI that appears
sudo lpadmin -p n80 -E -v '<that URI>' -P ~/n80-driver/ppd/ZHJY-N80.ppd
echo "hello" | lp -d n80                # a page should come out
sudo cupsctl --share-printers           # skip if the proxy runs on this box
sudo lpadmin -p n80 -o printer-is-shared=true
```

**On the proxy host** (nothing but `lp` — no driver, no cupsd):

```bash
sudo apt install cups-client
echo "hello from the proxy" | lp -h printpi.local:631 -d n80
```

Then set `backend`/`cups_queue`/`cups_server` as above and restart the
proxy. `/print` from the C64 should produce a page (and, on `both`, the
IEC one as well). When it doesn't:

- **`lpinfo -v` lists nothing** — the printer has to be ON and awake
  (battery models enumerate as nothing when asleep), on a data USB-C cable
  rather than a charge-only one. `dmesg | tail` shows the enumeration.
- **`printpi.local` doesn't resolve** — install `avahi-daemon` on the Pi,
  or put the IP in `cups_server`. Different subnets also want
  `sudo cupsctl --remote-any`.
- **The C64 says "Paper print failed: …"** — the short reason is on the
  C64, the full `lp` error is in the proxy log. `lp not installed` = no
  cups-client on the proxy host; `no cups server` = wrong host/port, or
  cupsd isn't sharing; `no such queue` = `cups_queue` isn't the queue's
  name; `timed out` = cupsd took over 20 s to accept the job.
- **The job says it printed but no page appeared** — a spooled job
  completes cleanly into a sleeping printer. Poke its power button, then
  check `lpstat -o` and `journalctl -u cups` on the print host.
- **Bits of different documents dribble out minutes apart, out of order** —
  the printer is dropping off the USB bus and CUPS is retrying each failed
  job on a timer, so several jobs take turns printing fragments. Check
  `dmesg | grep -c over-current` and `dmesg | grep usblp` on the print
  host: a thermal head's peak draw browns out an unpowered port. Put the
  printer on a powered hub or its own supply, then `cancel -a <queue>` to
  clear the retry backlog. (A Pi 5 caps USB at 600 mA unless
  `usb_max_current_enable=1` is set *and* the supply really offers 5 V/5 A —
  most 100 W USB-C bricks only do 100 W at 20 V.)

Full design, the N80 investigation, and the deltas from the original plan:
[docs/14](docs/14-printer-hardcopy.md) §13.

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
  printer at all (docs/14-printer-hardcopy.md). On the Windows client the
  same document arrives as virtual paper in the Notebook window, which
  needs no printer of any kind.
- **Claude Code:** `/code` (or `/code sonnet`) drives a coding-agent session
  from the C64, tool approvals answered at the prompt.
- **Housekeeping:** `/save`, `/restore`, `/history`, `/find <text>`,
  `/findall <text>`, `/stats`.

## Build flags

Both clients build with plain `make` and take their options as variables.
The full tables are in each client's README —
[C64 build modes](c64_client/README.md#build-modes) (`MODE80`, `CONNECT`,
`SERVER_IP`, `BAUD38400`, `DIAG`, `DEBUG_CLIENT`) and
[the Windows build](win311_client/README.md#build) (`WATCOM`, `HOST`,
`PORT`, `VMHOST`, `VMPORT`).

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

Install and run instructions live with each piece:
[the proxy](#installing-the-proxy) here,
[the C64 client](c64_client/README.md),
[the Windows client](win311_client/README.md). The docs below are design
and investigation records.

- [01-system-architecture.md](docs/01-system-architecture.md),
  [02-c64-client-design.md](docs/02-c64-client-design.md),
  [03-linux-proxy-design.md](docs/03-linux-proxy-design.md) — original design
- [05-ultimate-setup.md](docs/05-ultimate-setup.md) — real hardware setup
- [13-adventure-image-fidelity.md](docs/13-adventure-image-fidelity.md) —
  anchored, steerable illustration prompts
- [14-printer-hardcopy.md](docs/14-printer-hardcopy.md) — `/print`, the IEC
  path and the CUPS bridge
- [15-bss-overflow-hayes-mode80.md](docs/15-bss-overflow-hayes-mode80.md) —
  how tight the C64 memory map got, and how it was fixed
- [16-windows-311-client.md](docs/16-windows-311-client.md) — the Windows
  3.11 client, the multi-client profile design, and §13b for the desk as it
  stands
- [17-visual-canon.md](docs/17-visual-canon.md) — what the art is supposed
  to look like
