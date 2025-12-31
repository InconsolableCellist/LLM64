# C64 LLM Interface - System Architecture & Communication Protocol

## Overview
A Commodore 64 client application communicates over TCP/IP (via the C64 Ultimate's WiFi-enabled emulated ACIA modem) with a Linux host that proxies requests to an OpenAI-compatible API. The system enables interactive LLM chat sessions with full conversation history.

## High-Level Architecture

```
┌─────────────────────────────────────────┐
│         Commodore 64 Ultimate           │
│  ┌──────────────────────────────────┐   │
│  │   TUI Application (C/ASM)        │   │
│  │  - Text Editor                   │   │
│  │  - Chat Display                  │   │
│  │  - Conversation Selector         │   │
│  │  - F-key Menu System             │   │
│  └──────────────┬───────────────────┘   │
│                 │                       │
│  ┌──────────────▼───────────────────┐   │
│  │  ACIA Driver (ASM)               │   │
│  │  - 6551 registers at $DE00       │   │
│  │  - IRQ-driven RX                 │   │
│  │  - Protocol handler              │   │
│  └──────────────┬───────────────────┘   │
│                 │                       │
│  ┌──────────────▼───────────────────┐   │
│  │  Ultimate WiFi Modem Emulation   │   │
│  │  - Hayes AT command set          │   │
│  │  - TCP/IP over WiFi              │   │
│  └──────────────┬───────────────────┘   │
└─────────────────┼───────────────────────┘
                  │ WiFi TCP/IP
                  │ (9600 baud ACIA interface)
┌─────────────────▼───────────────────────┐
│         Linux Host                      │
│  ┌──────────────────────────────────┐   │
│  │  TCP Server (Python)             │   │
│  │  - Protocol handler              │   │
│  │  - Conversation manager          │   │
│  └──────┬───────────────┬───────────┘   │
│         │               │               │
│  ┌──────▼─────┐  ┌──────▼────────┐      │
│  │ OpenAI API │  │ Conversation  │      │
│  │ Client     │  │ Storage (JSON)│      │
│  └────────────┘  └───────────────┘      │
└─────────────────────────────────────────┘
```

## Communication Protocol

### Physical Layer
- **Interface**: 6551 ACIA (emulated by C64 Ultimate at $DE00-$DE03)
- **Transport**: TCP/IP over WiFi (Ultimate's built-in modem emulation)
- **Baud Rate**: 9600 baud (or higher: 19200 possible)
- **Format**: 8N1 (8 data bits, no parity, 1 stop bit)
- **Flow Control**: Hardware (RTS/CTS) + protocol-level ACKs

### Connection Establishment
The C64 uses Hayes AT commands to establish a TCP connection:
```
C64 → Modem:  ATZ              (reset modem)
Modem → C64:  OK
C64 → Modem:  ATE0             (echo off)
Modem → C64:  OK
C64 → Modem:  ATDThost:6400    (dial = TCP connect)
Modem → C64:  CONNECT 9600
[Binary protocol begins]
```

**Configuration:**
- Default server: Configurable hostname/IP (e.g., "raspberrypi.local:6400")
- Port: 6400 (configurable)
- Timeout: 30 seconds for connection
- Fallback: Display error if connection fails

### Protocol Design Principles
1. **Binary frame-based** - not line-based text
2. **Length-prefixed** messages for efficiency
3. **Type-tagged** messages for extensibility
4. **Interruptible** - support for cancel operations
5. **Incremental** - streaming responses for responsiveness

### Message Frame Format

All multi-byte integers are little-endian (6502 native).

```
┌──────────┬──────────┬──────────────┬─────────────┬─────────┐
│  SYNC    │  TYPE    │  LENGTH      │   PAYLOAD   │   CRC   │
│ (1 byte) │ (1 byte) │  (2 bytes)   │  (N bytes)  │(1 byte) │
└──────────┴──────────┴──────────────┴─────────────┴─────────┘
```

- **SYNC**: 0xC6 (magic byte for frame synchronization)
- **TYPE**: Message type (see below)
- **LENGTH**: Payload length (0-1024 bytes typical, max 2048)
- **PAYLOAD**: Message-specific data
- **CRC**: XOR checksum of TYPE + LENGTH + PAYLOAD bytes

### Message Types

#### C64 → Linux (Commands)

| Type | Name | Description |
|------|------|-------------|
| 0x01 | CHAT_REQUEST | Send user message, expects streaming response |
| 0x02 | CANCEL_REQUEST | Cancel current streaming operation |
| 0x03 | LIST_CONVERSATIONS | Request list of past conversations |
| 0x04 | LOAD_CONVERSATION | Load a specific conversation by ID |
| 0x05 | NEW_CONVERSATION | Start a new conversation |
| 0x06 | PING | Keepalive / connection test |
| 0x10 | ACK | Acknowledge received message |
| 0x11 | NAK | Negative acknowledge (error/retry) |

#### Linux → C64 (Responses)

| Type | Name | Description |
|------|------|-------------|
| 0x20 | CHAT_CHUNK | Streaming response chunk (incremental text) |
| 0x21 | CHAT_DONE | Response complete |
| 0x22 | CHAT_ERROR | Error during generation |
| 0x23 | CONVERSATION_LIST | List of conversations (multiple frames) |
| 0x24 | CONVERSATION_DATA | Full conversation history |
| 0x25 | STATUS | Status message (e.g., "Connecting to API...") |
| 0x10 | ACK | Acknowledge |
| 0x11 | NAK | Error/retry request |

### Message Payload Formats

#### CHAT_REQUEST (0x01)
```
┌──────────────────────────────────┐
│  Message text (null-terminated)  │
└──────────────────────────────────┘
```

#### CHAT_CHUNK (0x20)
```
┌──────────┬────────────────────────┐
│  SEQ     │  Text chunk            │
│ (1 byte) │  (null-terminated)     │
└──────────┴────────────────────────┘
```
- SEQ: Sequence number for ordering chunks (wraps at 255)

#### CHAT_DONE (0x21)
```
┌──────────┬──────────────┐
│  SEQ     │  FINAL_LEN   │
│ (1 byte) │  (2 bytes)   │
└──────────┴──────────────┘
```
- SEQ: Final sequence number
- FINAL_LEN: Total response length for verification

#### CONVERSATION_LIST (0x23)
```
┌──────────┬──────────┬──────────────────────────┐
│  COUNT   │  MORE    │  Conversation entries... │
│ (1 byte) │ (1 byte) │  (variable)              │
└──────────┴──────────┴──────────────────────────┘

Each entry:
┌──────────────┬──────────────┬────────────────────┐
│  ID          │  TIMESTAMP   │  TITLE             │
│  (4 bytes)   │  (4 bytes)   │  (null-terminated) │
└──────────────┴──────────────┴────────────────────┘
```
- MORE: 0x00 = last frame, 0x01 = more frames follow
- TIMESTAMP: Unix timestamp (32-bit)

#### LOAD_CONVERSATION (0x04)
```
┌──────────────┐
│  ID          │
│  (4 bytes)   │
└──────────────┘
```

#### CONVERSATION_DATA (0x24)
```
┌──────────┬──────────┬─────────────────────┐
│  COUNT   │  MORE    │  Message entries... │
│ (1 byte) │ (1 byte) │  (variable)         │
└──────────┴──────────┴─────────────────────┘

Each message entry:
┌──────────┬────────────────────────────┐
│  ROLE    │  Text                      │
│ (1 byte) │  (null-terminated)         │
└──────────┴────────────────────────────┘
```
- ROLE: 0x00 = user, 0x01 = assistant, 0x02 = system
- May require multiple frames for long conversations

### Protocol Flow Examples

#### Simple Chat Exchange
```
C64 → Linux:  CHAT_REQUEST "What is BASIC?"
Linux → C64:  ACK
Linux → C64:  STATUS "Contacting API..."
Linux → C64:  CHAT_CHUNK seq=0 "BASIC is"
Linux → C64:  CHAT_CHUNK seq=1 " a programming"
Linux → C64:  CHAT_CHUNK seq=2 " language..."
Linux → C64:  CHAT_DONE seq=2 len=125
C64 → Linux:  ACK
```

#### Interrupted Request
```
C64 → Linux:  CHAT_REQUEST "Write a long story..."
Linux → C64:  ACK
Linux → C64:  CHAT_CHUNK seq=0 "Once upon"
Linux → C64:  CHAT_CHUNK seq=1 " a time..."
C64 → Linux:  CANCEL_REQUEST
Linux → C64:  ACK
Linux → C64:  CHAT_DONE seq=1 len=45
```

#### Load Conversation
```
C64 → Linux:  LIST_CONVERSATIONS
Linux → C64:  CONVERSATION_LIST count=5 more=0 [entries...]
C64 → Linux:  ACK
C64 → Linux:  LOAD_CONVERSATION id=12345
Linux → C64:  CONVERSATION_DATA count=3 more=0 [messages...]
C64 → Linux:  ACK
```

### Error Handling
1. **Timeout**: If no response within 5 seconds, C64 retries (max 3 attempts)
2. **CRC Failure**: Send NAK, sender retransmits
3. **Frame Sync Loss**: Discard bytes until SYNC byte found
4. **Buffer Overflow**: Send NAK with error code, sender must fragment
5. **API Errors**: Linux sends CHAT_ERROR with error message

### Flow Control
- ACIA hardware flow control (RTS/CTS) handles buffer management
- Protocol ACKs provide additional backpressure
- C64 maintains ~1KB receive buffer (larger due to better performance)
- Linux maintains per-conversation state

### Responsiveness Strategy
- **IRQ-driven RX**: ACIA interrupt on byte received (very responsive)
- **Polled TX**: Check ACIA status before sending
- **Incremental rendering**: Display CHAT_CHUNKs as they arrive
- **Keyboard scanning**: Check keyboard every frame (60Hz)
- **Interrupt support**: User can press F3 to cancel anytime

## Performance Considerations

### Bandwidth Usage
- At 9600 baud: ~960 bytes/sec effective throughput (8x faster than 1200!)
- At 19200 baud: ~1920 bytes/sec (if stable)
- Frame overhead: 5 bytes per message (~4% for 100-byte payloads)
- Typical chat message: 100-500 bytes (0.5-5 seconds transfer)
- Response chunks: 50-100 bytes each for smooth display
- TCP/IP overhead: Minimal, handled by Ultimate firmware

### Latency
- ACIA transmission: ~0.1 sec per 100 bytes at 9600 baud
- TCP round-trip: <10ms on local network
- API latency: Variable (1-10 seconds typical)
- First token: ~1-3 seconds (user sees "thinking" status)
- Subsequent tokens: Stream as CHAT_CHUNKs arrive, displayed immediately
- Overall: Very responsive, limited mainly by API speed

### C64 Memory Constraints
- Receive buffer: 1024 bytes (circular, IRQ fills)
- Transmit buffer: 256 bytes (polled transmission)
- Current message display: ~2KB (active conversation window)
- Conversation list cache: ~1KB (20-30 entries)
- Total conversation history: Stored on Linux side only

## Security Considerations
- **No authentication** in v1 (trusted network connection)
- **No encryption** (WiFi WPA2, TCP plaintext)
- **Network exposure**: Server should bind to localhost or use firewall
- Future: Could add simple shared-secret challenge/response
- Conversation data privacy maintained by Linux proxy
- Ultimate modem already on trusted LAN

## Future Extensions
- File transfer protocol for saving/loading conversations to C64 disk
- Binary conversation format for faster loading
- Compression for large messages (RLE or simple dictionary)
- Multi-user support with session IDs
- Configuration protocol for API settings
