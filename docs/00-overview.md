# C64 LLM Interface - Project Overview

## What Is This?

A complete system for running an LLM chat interface on a Commodore 64, using the C64 Ultimate's WiFi modem emulation to communicate with a modern Linux server that proxies requests to OpenAI-compatible APIs.

## Why?

Because chatting with an AI on a 40-year-old computer is awesome! This project combines retro computing with modern AI capabilities, creating a unique and functional interface that feels native to the C64 while providing access to cutting-edge language models.

## Architecture at a Glance

```
C64 Ultimate (WiFi)  ←→  Linux Server  ←→  OpenAI API
    └─ TUI Client         └─ TCP Proxy        └─ GPT-3.5/4, etc.
```

### C64 Client Features
- **Full-screen TUI** with scrollable chat history
- **Text editor** with Emacs-style keybindings (Ctrl-A/E/K)
- **F-key shortcuts** for common actions
- **Conversation management** - browse and load past chats
- **Responsive design** - interrupt requests, real-time streaming
- **Native feel** - PETSCII graphics, C64 colors, 40-column layout

### Linux Proxy Features
- **TCP server** accepting connections from C64 clients
- **OpenAI-compatible API** integration with streaming
- **Conversation persistence** in Open WebUI JSON format
- **Multi-client support** - multiple C64s can connect
- **Async architecture** for high performance

## Key Technology Choices

### ACIA + TCP/IP (Via C64 Ultimate)
Instead of physical serial cables and bit-banging, we use:
- **6551 ACIA emulation** at $DE00 (SwiftLink compatible)
- **Hayes AT commands** for connection (ATDT host:port)
- **WiFi connectivity** through Ultimate's modem emulation
- **9600+ baud** for fast data transfer

**Advantages:**
- ✅ 8x faster than serial bit-banging (9600 vs 1200 baud)
- ✅ No cables required (WiFi built-in)
- ✅ Hardware UART with interrupts (very responsive)
- ✅ Standard ACIA driver code (well-documented)
- ✅ Can connect to server anywhere on LAN

### Binary Protocol
A custom frame-based protocol for efficient communication:
- **5-byte overhead** per message (SYNC, TYPE, LENGTH, CRC)
- **Streaming support** for incremental response display
- **Type-tagged messages** for extensibility
- **CRC checksums** for reliability

### Open WebUI Compatible Storage
Conversations stored in JSON format compatible with Open WebUI, enabling:
- Easy import/export of conversation history
- Integration with other tools
- Human-readable storage format
- Timestamped message history

## Development Stack

### C64 Client
- **Language**: C (cc65 compiler) + 6502 Assembly
- **Tools**: cc65, ca65, ld65
- **Testing**: VICE emulator + real C64 Ultimate
- **Size**: ~20-30KB .prg file

### Linux Proxy
- **Language**: Python 3.10+
- **Framework**: asyncio (built-in)
- **Dependencies**: httpx, toml, python-dotenv
- **Deployment**: Standalone script or systemd service

## Performance

| Metric | Value |
|--------|-------|
| Baud rate | 9600 (upgradable to 19200) |
| Throughput | ~960 bytes/sec |
| Latency | ~100ms per 100 bytes |
| API first token | 1-3 seconds |
| User message size | Up to 1024 bytes |
| Scrollback buffer | 16 messages (~8KB) |
| Conversation list | 30 conversations cached |

## User Experience Flow

1. **Startup**: C64 sends Hayes AT commands to connect to server
2. **Connected**: Status bar shows "Connected!"
3. **Type message**: User types in 3-line editor at bottom
4. **Send (F1)**: Message transmitted to server
5. **API call**: Server shows "Contacting API..." status
6. **Streaming**: Response chunks appear in real-time in chat area
7. **Done**: Full response displayed, user can scroll or type next message
8. **Cancel (F3)**: User can interrupt long responses anytime
9. **Sidebar (F5)**: Browse and load past conversations
10. **Help (F7)**: Display help screen with keybindings

## Design Documents

Detailed technical specifications are available:

1. **[System Architecture](01-system-architecture.md)** - Protocol specification, message formats, performance analysis
2. **[C64 Client Design](02-c64-client-design.md)** - Memory layout, module design, ACIA driver, UI implementation
3. **[Linux Proxy Design](03-linux-proxy-design.md)** - TCP server, API client, conversation storage

## Implementation Status

Currently in **design phase**. All design documents complete and ready for implementation.

### Next Steps
1. ✅ Design documents complete
2. ⏸️ Implement Linux proxy (Python TCP server)
3. ⏸️ Implement C64 ACIA driver (assembly)
4. ⏸️ Implement C64 protocol handler
5. ⏸️ Implement C64 UI and text editor
6. ⏸️ Integration testing
7. ⏸️ Real hardware testing on C64 Ultimate

## Quick Start (Once Implemented)

### Linux Server
```bash
# Install dependencies
pip install -r requirements.txt

# Configure API key
export OPENAI_API_KEY=sk-...

# Start server
python -m src.main --host 0.0.0.0 --port 6400
```

### C64 Ultimate
```
LOAD"C64LLM",8,1
RUN
[Connecting...]
[Connected!]
[Ready to chat!]
```

## Configuration

### C64 Client
Edit configuration at startup or in code:
- Server hostname/IP (e.g., "raspberrypi.local")
- Server port (default: 6400)
- Colors and display preferences

### Linux Proxy
Edit `config.toml`:
```toml
[api]
base_url = "https://api.openai.com/v1"
model = "gpt-3.5-turbo"

[storage]
data_dir = "./data"
```

## Requirements

### Hardware
- Commodore 64 with C64 Ultimate cartridge/board
- C64 Ultimate configured for WiFi
- Linux server on same network (Raspberry Pi, laptop, etc.)
- Internet connection for API access

### Software
- C64: Just the compiled .prg file
- Linux: Python 3.10+, pip packages
- OpenAI API key (or compatible endpoint)

## Frequently Asked Questions

**Q: Can I use a real C64 without the Ultimate?**
A: Yes, but you'll need an ACIA cartridge (SwiftLink, Turbo232, etc.) and a physical network connection or serial cable to a Linux machine. The Ultimate just makes it wireless.

**Q: What LLM providers are supported?**
A: Any OpenAI-compatible API: OpenAI, Anthropic (via proxy), local models (Ollama, LM Studio, text-generation-webui), etc.

**Q: How much does it cost to run?**
A: Only API costs. GPT-3.5-turbo is very cheap (~$0.002 per conversation). Local models are free.

**Q: Can multiple C64s connect at once?**
A: Yes! The server supports multiple concurrent clients, each with independent conversations.

**Q: Is the code available?**
A: Will be open-sourced once implementation is complete.

**Q: Can I save conversations to C64 disk?**
A: Not in v1, but planned for future enhancement. Currently all history is server-side.

**Q: What about the C128?**
A: Should work! The C128 in C64 mode has the same ACIA compatibility. Potential for 80-column mode in the future.

## Contributing

Contributions welcome once the initial implementation is complete! Areas of interest:
- Additional features (syntax highlighting, disk save, etc.)
- Support for other retro computers (Apple II, Atari, etc.)
- UI enhancements
- Performance optimizations
- Documentation improvements

## License

TBD (will be open source)

## Credits

- cc65 compiler team
- C64 Ultimate developers
- VICE emulator team
- OpenAI for the API
- The amazing C64 community

## Contact

TBD

---

**Status**: Design Complete | Implementation: Not Started
**Last Updated**: 2025-12-31
