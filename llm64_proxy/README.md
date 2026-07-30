# LLM64 Proxy Server

The bridge every LLM64 client talks to: a TCP server that holds the
conversations, calls an OpenAI-compatible model, converts the pictures,
and streams the music — for a C64 over a SwiftLink ACIA and for a Windows
3.1 machine over Winsock, at the same time.

**Installation is documented once, in the
[top-level README](../README.md#installing-the-proxy)** — venv,
`config.toml`, starting it, and the optional image, music and printing
backends. This file is the operational reference: what the flags are, how
the tests run, and what to do when something is wrong. The clients have
their own READMEs: [c64_client](../c64_client/README.md),
[win311_client](../win311_client/README.md).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.toml.example config.toml     # then point [api] at your model
.venv/bin/python -m src.main           # listens on 0.0.0.0:6400
```

From the repo root, `./run.sh proxy` (or `proxy-bg` / `stop` / `status`)
does the same thing and reads the address out of `run.conf`.

## Usage

```bash
# Basic usage (listens on 0.0.0.0:6400)
python -m src.main

# Custom host/port
python -m src.main --host 127.0.0.1 --port 6400

# Verbose logging - every frame, every directive
python -m src.main -v

# Custom config file
python -m src.main --config my-config.toml
```

## Configuration

`config.toml` (copied from `config.toml.example`) is the source of truth,
and every setting has an environment override. The annotated reference for
all of it — `[api]`, `[modes]`, `[storage]`, `[serial]`, `[claude]`,
`[images]`, `[printer]` — is in the
[top-level README](../README.md#configuration-proxy). The ones people set
from the environment:

- `OPENAI_API_KEY` — API key (local model servers need none)
- `OPENAI_API_BASE` — endpoint, if not the one in the file
- `OPENAI_MODEL` — model name
- `LLM64_DATA_DIR` — where conversations, images and libraries live
- `LLM64_PRINTER_BACKEND` / `LLM64_PRINTER_QUEUE` — `/print` routing
- `GEMINI_API_KEY` — picture generation, when the backend is `gemini`

No `config.toml` is required to *start*: the proxy runs without one and
accepts clients, then fails every reply with an API error.

### Printing to a real printer (`/print`)

By default `/print` sends the document to the C64, which prints it on IEC
device 4 - the proxy host needs nothing installed. (The Windows client
catches the same frames and shows them as virtual paper, which needs
nothing either.) Set `[printer] backend = "cups"` (or `"both"`) and it
spools to a CUPS queue here instead, which is also how `/print` works with
no C64 printer at all:

```toml
[printer]
backend = "cups"
cups_queue = "n80"                   # required; else it falls back to c64
cups_server = "printpi.local:631"    # "" = cupsd on this host
cups_options = "cpi=12 lpi=8"        # 78 columns needs 12 cpi to fit A4
cups_width = 0                       # 0 = share `width` (the C64 printer's)
cups_feed_lines = 0                  # blank lines to clear a roll's tear bar
```

`width` is the IEC printer's 78 columns. A till roll holds about 34 at
12 cpi and the driver crops rather than re-wraps, so a roll wants
`cups_width = 34`, `PageSize=Custom.204x842` in `cups_options`, and a few
`cups_feed_lines` so the last line clears the tear bar.

Requirements on THIS host: just `lp` (`apt install cups-client`, Arch
`pacman -S cups`) - no printer driver, even when the printer is a
driverless-unfriendly model, because the driver lives wherever `cupsd`
and the printer are. Setup steps for that machine (a Raspberry Pi bridge
or this box) are in the top-level README's "Printing" section, automated
in `tools/setup-printer-pi.sh`, and specified in
`docs/14-printer-hardcopy.md` §13.

## Features

- TCP server serving several clients at once, each with its own profile
  (`profiles.py`): widths, payload caps and capabilities per machine
- Binary protocol with framing and CRC, and pacing tuned to the wire
  speed the client reports on connect
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

## Testing

The unit tests are standalone scripts — no pytest, no fixtures directory:

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
`tools/` (see the top-level README).
