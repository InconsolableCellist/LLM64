# C64 LLM Interface

Chat with modern LLMs from a Commodore 64. A native TUI client (cc65
C + 6502 assembly) talks through a 6551 ACIA to a Python proxy that
bridges to any OpenAI-compatible API — and the whole stack is verified
end-to-end by automated tests running in VICE.

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

- **Interactive TUI on the C64**: scrollable word-wrapped chat, streaming
  responses, 120-char input editor with Emacs bindings, conversation
  browser, help overlay
- **Interrupt-driven serial**: 6551 ACIA driver with an IRQ/NMI RX ring
  buffer — no dropped bytes at 9600 baud while the screen updates
- **Python proxy**: async TCP server, OpenAI SSE streaming, Open
  WebUI-style conversation persistence, ASCII sanitization, C64-aware
  system prompt
- **Automated end-to-end tests in VICE**: the CI-able `make test-all`
  boots mock LLM → proxy → emulated C64 and asserts on actual screen
  contents (including typing into the emulator through VICE's binary
  monitor). Verified against a real llama.cpp server too.

## Quick start (emulator)

Prereqs: `cc65`, `vice` (x64sc), `python3`, and `tcpser` for the
Hayes-mode test.

```bash
# one-time proxy setup
cd c64llm_proxy && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..

make test-all       # run the four automated suites
make run-live       # interactive TUI against the API in c64llm_proxy/config.toml
```

See [docs/04-emulator-setup.md](docs/04-emulator-setup.md) for the VICE
wiring details (and the archaeology of why naive VICE setups fail).

## Quick start (real C64 Ultimate)

```bash
make -C c64_client clean
make -C c64_client CONNECT=hayes SERVER_IP=<proxy-lan-ip>
# copy build/c64llm.prg to the Ultimate, run the proxy, LOAD"C64LLM",8,1
```

Checklist: [docs/05-ultimate-setup.md](docs/05-ultimate-setup.md)
(ACIA/SwiftLink at $DE00 + modem emulation enabled).

## Keys (TUI client)

| Key | Action |
|-----|--------|
| F1 / Return | Send message |
| F2 | New conversation |
| F3 | Cancel streaming reply |
| F5 | Conversation browser (Return loads, F5 closes) |
| F7 | Help |
| CRSR up/down | Scroll chat |
| Ctrl-A / Ctrl-E | Start / end of input |
| Ctrl-K / Ctrl-D | Kill to end / delete char |
| CLR/HOME | Clear input |

## Repository layout

```
c64_client/     cc65 client (src/main.c TUI, src/debug_main.c scripted
                diagnostics, serial.s ACIA driver, display/editor/protocol)
c64llm_proxy/   Python proxy (src/, config.toml, .venv)
emu/            VICE automation: e2e harness, mock LLM, binary-monitor client
docs/           design docs (00-03) + setup guides (04-05)
```

## Build modes

| Flag | Meaning |
|------|---------|
| `CONNECT=direct` | No modem handshake; ACIA pipe is the connection (VICE) |
| `CONNECT=hayes` | AT-command dial (C64 Ultimate, or VICE+tcpser) |
| `SERVER_IP=` / `SERVER_PORT=` | Proxy address baked into the PRG |
| `DEBUG_CLIENT=1` | Scripted diagnostic session instead of the TUI |

## Configuration (proxy)

`c64llm_proxy/config.toml`:

```toml
[api]
base_url = "https://your-server:5000/v1"
model = "your-model"
system_prompt = "You are chatting with a user on a Commodore 64..."

[storage]
data_dir = "./data"
```

Environment overrides: `OPENAI_API_BASE`, `OPENAI_API_KEY` (optional for
local servers), `OPENAI_MODEL`, `OPENAI_SYSTEM_PROMPT`.

## Documentation

- [00-overview.md](docs/00-overview.md), [01-system-architecture.md](docs/01-system-architecture.md),
  [02-c64-client-design.md](docs/02-c64-client-design.md), [03-linux-proxy-design.md](docs/03-linux-proxy-design.md) — original design
- [04-emulator-setup.md](docs/04-emulator-setup.md) — VICE + automation
- [05-ultimate-setup.md](docs/05-ultimate-setup.md) — real hardware

---

*"Bringing 1980s hardware into the 2020s AI era"*
