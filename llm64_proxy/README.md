# LLM64 Proxy Server

TCP server that bridges C64 Ultimate clients to OpenAI-compatible APIs.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set API key
export OPENAI_API_KEY=sk-your-key-here

# Run server
python -m src.main
```

## Configuration

Create `config.toml` from example:
```bash
cp config.toml.example config.toml
# Edit config.toml as needed
```

Or use environment variables:
- `OPENAI_API_KEY` - API key (required)
- `OPENAI_API_BASE` - API endpoint (optional)
- `OPENAI_MODEL` - Model name (optional)
- `LLM64_PRINTER_BACKEND` / `LLM64_PRINTER_QUEUE` - `/print` routing
  (see below)

### Printing to a real printer (`/print`)

By default `/print` sends the document to the C64, which prints it on IEC
device 4 - the proxy host needs nothing installed. Set
`[printer] backend = "cups"` (or `"both"`) and it spools to a CUPS queue
here instead, which is also how `/print` works with no C64 printer at all:

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

## Usage

```bash
# Basic usage (listens on 0.0.0.0:6400)
python -m src.main

# Custom host/port
python -m src.main --host 127.0.0.1 --port 6400

# Verbose logging
python -m src.main -v

# Custom config file
python -m src.main --config my-config.toml
```

## Features

- ✅ TCP server for C64 clients
- ✅ Binary protocol with framing and CRC
- ✅ OpenAI API streaming support
- ✅ Conversation persistence (Open WebUI format)
- ✅ Multi-client support
- ✅ Async architecture for performance

## Testing

Test with a simple TCP client:
```bash
# Connect with netcat
nc localhost 6400
```

Or use the included test script (coming soon).

## Directory Structure

```
llm64_proxy/
├── src/
│   ├── main.py              # Entry point
│   ├── tcp_server.py        # TCP server
│   ├── protocol.py          # Protocol handler
│   ├── api_client.py        # OpenAI client
│   ├── conversation.py      # Conversation management
│   └── config.py            # Configuration
├── data/
│   └── conversations/       # Stored conversations
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

**Can't connect from C64:**
```bash
# Check if server is listening
netstat -tuln | grep 6400

# Check firewall
sudo ufw allow 6400/tcp
```
