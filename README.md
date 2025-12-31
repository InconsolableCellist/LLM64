# C64 LLM Interface

A complete LLM chat interface for the Commodore 64, featuring a native TUI client and Python proxy server.

![Status](https://img.shields.io/badge/status-design%20complete-blue)
![Platform](https://img.shields.io/badge/platform-C64%20Ultimate-red)
![Language](https://img.shields.io/badge/c64-C%2FASM-orange)
![Language](https://img.shields.io/badge/proxy-Python%203.10%2B-green)

## Overview

Chat with modern LLMs (GPT-3.5, GPT-4, local models) directly from your Commodore 64! This project provides:

- **C64 TUI Client** - Full-screen interface with text editing, scrolling, and conversation management
- **Linux TCP Proxy** - Bridges C64 to OpenAI-compatible APIs with conversation persistence
- **WiFi Connectivity** - Uses C64 Ultimate's modem emulation (no cables!)
- **Fast Performance** - 9600 baud ACIA communication for responsive streaming

## Features

### C64 Client
- ✨ Full-screen TUI with 19-line scrollable chat area
- ⌨️ Multi-line text editor with Emacs keybindings (Ctrl-A/E/K)
- 📜 Conversation history browser
- 🎮 F-key shortcuts (F1: Send, F3: Cancel, F5: Sidebar, F7: Help)
- ⚡ Real-time streaming responses
- 🎨 Native PETSCII graphics and colors

### Linux Proxy
- 🌐 TCP server (no serial cables needed!)
- 🤖 OpenAI-compatible API integration
- 💾 Open WebUI-compatible JSON storage
- 👥 Multi-client support
- 🚀 Async architecture for performance

## Architecture

```
┌─────────────────┐         ┌──────────────┐         ┌─────────────┐
│  C64 Ultimate   │  WiFi   │ Linux Server │  HTTPS  │  OpenAI API │
│  ┌───────────┐  │ ◄─────► │ ┌──────────┐ │ ◄─────► │             │
│  │ TUI Client│  │  TCP    │ │TCP Proxy │ │   API   │ GPT-3.5/4   │
│  │  (C/ASM)  │  │ 9600bd  │ │ (Python) │ │         │ Local LLMs  │
│  └───────────┘  │         │ └──────────┘ │         │             │
└─────────────────┘         └──────────────┘         └─────────────┘
```

## Quick Start

### Prerequisites
- Commodore 64 with C64 Ultimate (configured for WiFi)
- Linux server on same network
- OpenAI API key (or compatible endpoint)

### Setup Linux Proxy
```bash
# Clone repository
git clone <repo-url>
cd c64_llm

# Install dependencies
pip install -r requirements.txt

# Configure API key
export OPENAI_API_KEY=sk-your-key-here

# Start server
python -m src.main --host 0.0.0.0 --port 6400
```

### Run on C64
```
LOAD"C64LLM",8,1
RUN
```

The client will connect to the server via Hayes AT commands and you're ready to chat!

## Documentation

Comprehensive design documentation is available in the `docs/` directory:

- **[00-overview.md](docs/00-overview.md)** - Project overview and FAQ
- **[01-system-architecture.md](docs/01-system-architecture.md)** - Protocol specification, message formats
- **[02-c64-client-design.md](docs/02-c64-client-design.md)** - C64 implementation details, memory layout, modules
- **[03-linux-proxy-design.md](docs/03-linux-proxy-design.md)** - Python server design, API integration

## Technology Stack

| Component | Technology |
|-----------|------------|
| C64 Compiler | cc65 |
| C64 Language | C + 6502 Assembly |
| Communication | 6551 ACIA (SwiftLink compatible) |
| Transport | TCP/IP over WiFi (Hayes AT) |
| Baud Rate | 9600 (up to 19200) |
| Proxy Language | Python 3.10+ |
| Proxy Framework | asyncio |
| API Protocol | OpenAI-compatible |
| Storage Format | Open WebUI JSON |

## Performance

- **Throughput**: ~960 bytes/sec (9600 baud)
- **Latency**: ~100ms per 100 bytes transfer
- **API Response**: 1-3 seconds for first token
- **Streaming**: Real-time display as chunks arrive
- **Message Size**: Up to 1024 bytes
- **Scrollback**: 16 messages in C64 memory

## Development Status

**Current Phase**: Design Complete ✅

All design documents are finished and ready for implementation.

### Roadmap
- [x] System architecture design
- [x] C64 client design
- [x] Linux proxy design
- [ ] Implement Linux proxy
- [ ] Implement C64 ACIA driver
- [ ] Implement C64 protocol layer
- [ ] Implement C64 UI/editor
- [ ] Integration testing
- [ ] Hardware testing
- [ ] Documentation
- [ ] Release v1.0

## Building (Once Implemented)

### C64 Client
```bash
cd c64
make clean
make
# Creates c64llm.prg
```

### Linux Proxy
No build required - pure Python!

## Configuration

### Server Configuration (`config.toml`)
```toml
[api]
base_url = "https://api.openai.com/v1"
model = "gpt-3.5-turbo"

[storage]
data_dir = "./data"
```

### Environment Variables
```bash
export OPENAI_API_KEY=sk-...        # Required
export OPENAI_MODEL=gpt-4           # Optional
export OPENAI_API_BASE=https://...  # Optional
```

## Usage

### Keyboard Controls

| Key | Action |
|-----|--------|
| F1 | Send message |
| F3 | Cancel current request |
| F5 | Toggle conversation sidebar |
| F7 | Show help |
| Ctrl-A | Beginning of line |
| Ctrl-E | End of line |
| Ctrl-K | Kill to end of line |
| Ctrl-D | Delete character |
| Cursor Keys | Navigate |
| Return | New line in editor |

### Example Session
```
[Chat window shows previous messages]
You: What is BASIC?
AI: BASIC (Beginner's All-purpose Symbolic
Instruction Code) is a high-level programming
language designed in 1964...

[Type your next message in editor area]
> How do I use FOR loops?_

[Press F1 to send]
```

## API Compatibility

Works with any OpenAI-compatible API:
- ✅ OpenAI (GPT-3.5, GPT-4)
- ✅ Anthropic Claude (via proxy)
- ✅ Local models (Ollama, LM Studio, text-generation-webui)
- ✅ Azure OpenAI
- ✅ Any other OpenAI-compatible endpoint

## Hardware Requirements

### Required
- Commodore 64
- C64 Ultimate cartridge or Ultimate 64 (with WiFi configured)

### Alternative (Without Ultimate)
- Commodore 64
- SwiftLink, Turbo232, or compatible ACIA cartridge
- Network connection via:
  - WiFi adapter connected to ACIA, OR
  - Serial cable to Linux machine with ser2net/socat

## Contributing

Contributions welcome! Areas of interest:
- Bug fixes and testing
- Feature enhancements
- Documentation improvements
- Support for other platforms (C128, VIC-20, etc.)
- UI/UX improvements

## License

TBD - Will be open source

## Credits

- **cc65** - C compiler for 6502
- **C64 Ultimate** - FPGA C64 platform with WiFi
- **VICE** - C64 emulator for development
- **OpenAI** - API platform

## See Also

- [cc65 Documentation](https://cc65.github.io/)
- [C64 Ultimate Manual](https://ultimate64.com/)
- [OpenAI API Docs](https://platform.openai.com/docs)
- [Open WebUI](https://github.com/open-webui/open-webui)

---

**Made with ❤️ for the C64 community**

*"Bringing 1980s hardware into the 2020s AI era"*
