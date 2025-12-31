# C64 LLM Proxy Server

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
c64llm_proxy/
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
