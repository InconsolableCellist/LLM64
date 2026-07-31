# LLM64 Proxy Server

The LLM64 proxy server is a Python TCP server that holds the
conversations, calls an OpenAI-compatible model, converts the pictures,
streams the music and composes the printouts. It currently supports a C64 client over a SwiftLink
ACIA and a Windows 3.x machine over Winsock (at the same time).

| | |
|---|---|
| Install | [venv and requirements](#installation) |
| Configure | [`config.toml`, starting with `[api]`](#configuration) |
| Run | [`./run.sh proxy`, and how to check it](#running-the-proxy) |
| Pictures, music, paper | [Optional features](#optional-features) |
| Problems | [Troubleshooting](#troubleshooting) |

## What it does

- TCP server serving several clients at once, each with its own profile
  (`profiles.py`): widths, payload caps and capabilities per machine
- Binary protocol with framing and CRC, and pacing tuned to the wire
  speed the client reports on connect (developed with a C64 Ultimate)
- OpenAI API streaming (SSE), with the reply filtered for directives as
  it arrives
- Conversation persistence in Open WebUI format
- Adventure machinery: character generation, dice, map, state blocks,
  scene composition for the illustrator
- Media pipelines: image generation and per-client conversion, a
  mood-tagged SID library and a mood-tagged MIDI library
- `/print` composition, to the client or to CUPS
- `/code` mode, driving a Claude Code session on this host
- Async throughout

## Installation

Install Python 3.10+ on a machine the old/emulated hardware can reach.

```bash
git clone https://github.com/InconsolableCellist/c64_llm.git
cd c64_llm/llm64_proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # httpx, toml, Pillow, tomlkit
```

No Python on the target machine? PACKAGING.md builds the proxy into a
single Windows or Linux binary with a desktop launcher: start/stop
buttons, live status, the log, and a config editor in one window.

## Configuration

### Write a config

```bash
cp config.toml.example config.toml
$EDITOR config.toml
```

Be sure to edit the `[api]` section:

```toml
[api]
base_url = "http://192.168.1.10:5000/v1"   # any OpenAI-compatible endpoint (llama.cpp, LM studio, ollama, etc.)
key = "none"                               # local servers generally don't need an API key
model = "gemma-3-27b-it"                   # I recommend gemma 4 models (largest you can run) and a Heretic finetune
max_context_tokens = 8192
```

Anything that serves OpenAI-compatible Chat Completions works: llama.cpp's
`llama-server`, LMStudio, vLLM, Ollama (if you must), OpenRouter (highly recommended) 
or OpenAI itself
(then `base_url = "https://api.openai.com/v1"` and a real `key`, or the
`OPENAI_API_KEY` environment variable -- every setting has an env
override). Claude is reached through `/code` mode instead, which drives
the `claude` CLI on the proxy host, so Claude Code has to be installed and
authenticated on the proxy machine.

Without a `config.toml` the proxy will still start and accept client, but 
every reply will fail with an API error.

Every other setting has a default; the full reference is
[below](#every-configtoml-section).

### Every config.toml section

Refer also to the included `config.toml.example`.

`config.toml` sections (environment variables override the file):

#### `[api]` -- the LLM backend

| Key | Default | Env override | Meaning |
|-----|---------|--------------|---------|
| `base_url` | `https://api.openai.com/v1` | `OPENAI_API_BASE` | Any OpenAI-compatible endpoint |
| `key` | `"none"` | `OPENAI_API_KEY` | Optional; local servers work keyless |
| `model` | `gpt-3.5-turbo` | `OPENAI_MODEL` | Model name (`/models` lists, `/model` switches) |
| `temperature` | `0.7` | `OPENAI_TEMPERATURE` | Chat-mode sampling |
| `max_tokens` | `2000` | `OPENAI_MAX_TOKENS` | Reply cap |
| `max_context_tokens` | `8192` | `OPENAI_MAX_CONTEXT` | Auto-detected from llama.cpp when possible |
| `system_prompt` | `""` | `OPENAI_SYSTEM_PROMPT` | Prepended to chat mode |
| `disable_thinking` | `true` | -- | Suppresses Gemma/Qwen thinking blocks (thinking adds 20-25s of latency on a C64) |

#### `[modes]` -- adventure & roleplay

`user_name` (what `{{user}}` expands to in character cards), `cards_dir`
(default `./cards`, env `LLM64_CARDS_DIR`) for SillyTavern v1/v2/v3 cards
(`.json` or PNG-embedded). Optional `[modes.adventure]` and
`[modes.roleplay]` sampling tables (`temperature`, `top_p`, `top_k`,
`min_p`, `repetition_penalty`, `max_tokens`); when absent, a Gemma-tuned
preset is used.

#### `[storage]`, `[serial]`, `[claude]`

- `[storage] data_dir` (default `./data`, env `LLM64_DATA_DIR`) --
  conversations land in `data/conversations/`, images in `data/images/`.
- `[serial] wire_baud` (default `9600`, env `LLM64_WIRE_BAUD`) -- bulk
  transfer pacing. Only a *fallback*: modern clients announce their rate
  on connect (`MSG_SET_BAUD`) and the proxy paces to that automatically,
  so this just covers clients too old to report.
- `[claude] command`, `workdir`, `model` -- the `claude` CLI invocation for
  `/code` mode (env `LLM64_CLAUDE_CMD`).


## Running the proxy

### Start it

```bash
./run.sh proxy       # foreground, Ctrl-C stops it
./run.sh proxy-bg    # background; log in llm64_proxy/proxy-live.log
./run.sh stop        # stop the background one
./run.sh status      # what's up and what isn't
```

It listens on TCP **6400** by default, on all interfaces. Open the port if you
run a firewall: `sudo ufw allow 6400/tcp`.

### Check it

Check that the connection opens and stays open:

```bash
ss -ltn | grep 6400          # listening?
nc <proxy-host> 6400         # reachable from elsewhere on the LAN?
```

## Optional features

| Want | Do this |
|------|---------|
| **Pictures** | A `[images]` backend and a key, such as a Gemini key. See [Image generation](#image-generation) |
| **Music on the C64** | Build a SID library from your own HVSC copy. See [Building the SID music library](#building-the-sid-music-library-the-c64s-music) |
| **Music on the PC client** | Build a MIDI library from VGMusic. See [Music for the Windows client](#music-for-the-windows-client-midi) |
| **Real printing** | A printer on the C64's IEC bus needs nothing on the proxy; a modern printer needs `lp` and a CUPS queue. [Printing](#printing) |
| **`/code`** | Claude Code installed and authenticated on this host, then `[claude]` |

### Image generation

Set `[images] mode` to `ask` (the model suggests, you confirm with `/pic`),
`auto` (striking scenes illustrate themselves, rate-limited), or `off`
(directives ignored; explicit `/pic <desc>` still works). Optional
`style_prefix` wraps every prompt; the default is a dark-fantasy style -- set
it to `""` if your ComfyUI workflow carries its own style. Backends
(`[images] backend`):

- **`gemini`** (Nano Banana, the default) -- `[images.gemini]` with
  `model = "gemini-2.5-flash-image"` and a key from
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey), via
  `key` or the `GEMINI_API_KEY` env var.
- **`openai`** -- any `POST /v1/images/generations` server (OpenAI,
  Together, LocalAI): `base_url`, `model` (default `dall-e-3`), `size`,
  `key` (or env `LLM64_IMAGES_KEY`).
- **`comfyui`** -- a local ComfyUI instance: `url` (default
  `http://127.0.0.1:8188`), `workflow` (an API-format JSON export
  containing the literal token `{PROMPT}` in a node input), `timeout`,
  `randomize_seed`. No auth -- keep it on a trusted LAN.
- **`fixture`** -- a fixed local image, for tests.

Minimum to get pictures: install Pillow (it's in requirements.txt), set
`mode = "ask"`, and supply a Gemini key. Then type `/pic a snake-like
green dragon with one arm lover a burningi village` on the C64 to test.

### Building the SID music library (the C64's music)

Music activates automatically when `data/sids/moods.json` exists. No
library ships with this repo for copyright reasons. 

```
# 1. Get HVSC (85 MB) from https://www.hvsc.c64.org/downloads
#    or https://hvsc.brona.dk/HVSC/HVSC_85-all-of-them.7z
# 2. Point the builder at it:
llm64_proxy/tools/sid_build.py --hvsc ~/Downloads/HVSC_85-all-of-them.7z
```

`sid_build.py` runs the seven pipeline stages in order with progress and
time estimates, and is resumable. 

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

which rsyncs only what the proxy reads -- the database, the ranking and
the relocated tunes, ~50 MB -- and not the 457 MB HVSC tree.

#### Which tune is any good

The tagger says what a tune is *for*; nothing in it says whether the tune
is any *good*, and 10k tunes is ~100 hours of music. So
`tools/sid_rank.py` cross-references the library against the C64 scene's
own published opinion, taken from the
[DeepSID](https://deepsid.chordian.net) database dump (one 8 MB download,
no crawling):

| signal | what it is |
| --- | --- |
| `compo` | party music-competition placings -- an audience voted |
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
loads it beside `moods.json` and weights selection by it --
`FLOOR + (1-FLOOR)·rank²` in `src/sid_ranking.py`, which measured on the
real library lifts the mean regard of what actually plays from 0.52 to
0.69 while still drawing one pick in nine from the bottom third. It is
deliberately a weighting and not a filter: most of HVSC is obscure demo
music nobody wrote about, and unheard is not the same as bad. Your own
jukebox favourites outrank all of it.

#### What this repo does and does not carry

No SID in this repository belongs to anyone else. The shareware intro's
tune is `c64_client/intro/tune/llm64_theme.s` written for this project, rebuilt with `make -C c64_client/intro
tune`. The built library, the relocated tunes and everything else under
`data/` stay out of git deliberately: HVSC's
`DOCUMENTS/Disclaimer.txt` limits those tunes to private enjoyment, which
covers your own machine and your own C64 but not redistribution, so
don't publish a built library or a disk image containing one.

#### Fixing what the tagger got wrong

The tagger works from filenames and STIL notes, so it is often wrong, and some tunes are simply bad or relocate badly. You can use `tools/sid_review.py` to manually peruse the SIDs and rank them yourself.

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

As the Windows 3.11 client has no SID chip, MIDIs are used instead.

To get MIDIs you'll again need to process it yourself due to copyright. 
Run these from the repo root:

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

Step 3 will take a while, just like the mood stage for SIDs.
`--pilot 48` tags a small batch
first so you can read the results before committing to the run.

#### Listening to it before you trust it

You can use `tools/sid_review.py` to review and recategorize/score MIDIs:

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

### Printing

`/print` composes the document on the proxy and sends it to the 
`[printer] backend`:

- **`c64`** (default): the C64 prints it itself, through a printer on IEC
  device 4: a real MPS-80{1,2,3}, the Ultimate's built-in virtual printer, or
  VICE's device-4 emulation. On a C64 Ultimate the
  virtual printer is **off by default** 
  See [docs/05](../docs/05-ultimate-setup.md)).
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

#### Getting CUPS going (Raspberry Pi print bridge, or the proxy host itself)

You can use a real printer (such as an N80 thermal printer) instead of an authentic Commodore 64 printer, and merely need to connect it to a machine capable of running CUPS (such as a Raspberry Pi near your real C64). If you wish to do so, run the following on the computer to serve as a print server: 

**On the machine with the printer.** `tools/setup-printer-pi.sh` does the
whole sequence for the N80. Copy it over (it needs only bash); `--dry-run`
prints every command and runs none:

```bash
./setup-printer-pi.sh --driver ~/n80-driver --queue n80 --test
./setup-printer-pi.sh --queue laser --no-share        # printer on the proxy host
```

`--driver` is an extracted vendor CUPS driver, needed only for printers
that aren't driverless -- e.g. the NDYIN/ZHJY N80 thermal, whose PPD +
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

**On the proxy host** (just `lp`):

```bash
sudo apt install cups-client
echo "hello from the proxy" | lp -h printpi.local:631 -d n80
```

Then set `backend`/`cups_queue`/`cups_server` as above and restart the
proxy. `/print` from the C64 should produce a page (and, on `both`, the
IEC one as well). When it doesn't:

- **`lpinfo -v` lists nothing** -- the printer has to be ON and awake
  (battery models enumerate as nothing when asleep), on a data USB-C cable
  rather than a charge-only one. `dmesg | tail` shows the enumeration.
- **`printpi.local` doesn't resolve** -- install `avahi-daemon` on the Pi,
  or put the IP in `cups_server`. Different subnets also want
  `sudo cupsctl --remote-any`.
- **The C64 says "Paper print failed: …"** -- the short reason is on the
  C64, the full `lp` error is in the proxy log. `lp not installed` = no
  cups-client on the proxy host; `no cups server` = wrong host/port, or
  cupsd isn't sharing; `no such queue` = `cups_queue` isn't the queue's
  name; `timed out` = cupsd took over 20 s to accept the job.
- **The job says it printed but no page appeared** -- a spooled job
  completes cleanly into a sleeping printer. Poke its power button, then
  check `lpstat -o` and `journalctl -u cups` on the print host.
- **Bits of different documents dribble out minutes apart, out of order** --
  the printer is dropping off the USB bus and CUPS is retrying each failed
  job on a timer, so several jobs take turns printing fragments. Check
  `dmesg | grep -c over-current` and `dmesg | grep usblp` on the print
  host: a thermal head's peak draw browns out an unpowered port. Put the
  printer on a powered hub or its own supply, then `cancel -a <queue>` to
  clear the retry backlog. (A Pi 5 caps USB at 600 mA unless
  `usb_max_current_enable=1` is set *and* the supply really offers 5 V/5 A --
  most 100 W USB-C bricks only do 100 W at 20 V.)

## Testing

The unit tests are standalone scripts -- no pytest, no fixtures directory:

```bash
.venv/bin/python tests/test_map.py          # one of them
for t in tests/test_*.py; do .venv/bin/python "$t" || break; done
```

`test_client.py` is a hand-driven client for poking the wire protocol,
and the full end-to-end suite (real client, real proxy, VICE) is
`make test-all` from the repo root.

## Directory Structure

```
llm64_proxy/
├── src/
│   ├── main.py              # entry point, CLI, logging
│   ├── tcp_server.py        # TCP server
│   ├── protocol.py          # framing and every message type
│   ├── profiles.py          # per-client capabilities and limits
│   ├── api_client.py        # OpenAI-compatible client (SSE)
│   ├── conversation.py      # conversation storage
│   ├── modes.py             # chat / adventure / roleplay / code prompts
│   ├── advmap.py advsetup.py advtemplates.py chargen.py dice.py
│   ├── images.py imagegen.py imaging.py scenecomp.py printpic.py
│   ├── music.py midi_library.py sid_ranking.py sid_overrides.py
│   ├── printdoc.py printcups.py
│   ├── claude_session.py    # /code mode
│   └── config.py            # configuration
├── tests/                   # standalone unit tests
├── tools/                   # SID and MIDI library pipelines, img2c64
├── data/                    # conversations, images, sids, midi (not in git)
├── requirements.txt
├── config.toml.example
└── README.md
```

## Troubleshooting

**Port already in use:**
```bash
# Check what's using port 6400
sudo lsof -i :6400

# Use a different port
python -m src.main --port 6401
```

**API key not set:**
```bash
export OPENAI_API_KEY=sk-your-key-here
```

**Can't connect from a client:**
```bash
# Check if server is listening - and on 0.0.0.0, not loopback
ss -ltn | grep 6400

# Check firewall
sudo ufw allow 6400/tcp
```

Then check it from the client's own network, not from this host: a VPN or
tailnet address the proxy machine can reach may be nowhere the C64 or the
486 can go.

**Replies fail with an API error:** there is no `config.toml`, or `[api]
base_url` points at a model server that isn't running. `-v` logs the
request.

**Pictures say unavailable:** no `[images]` backend configured, no key, or
Pillow missing from the venv.

**Music never plays:** the library hasn't been built.
`data/sids/moods.json` is what the C64 side waits for,
`data/midi/midi.json` the Windows side; both are built by the tools in
`tools/` -- see [Optional features](#optional-features).
