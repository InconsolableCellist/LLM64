# Linux Proxy Server - Technical Design Document

## Overview
A Python-based proxy server that bridges serial communication from the C64 client to OpenAI-compatible API endpoints, manages conversation persistence in Open WebUI-compatible format, and handles streaming responses.

## Technology Stack
- **Language**: Python 3.10+
- **Serial**: pyserial
- **HTTP**: httpx (async support)
- **Storage**: JSON files (Open WebUI compatible)
- **Async**: asyncio
- **Config**: python-dotenv, TOML config file
- **Logging**: Python logging module

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Linux Proxy Server                 │
│                                                 │
│  ┌──────────────────────────────────────────┐  │
│  │         Main Event Loop (asyncio)        │  │
│  └────┬──────────────────────────────┬──────┘  │
│       │                              │         │
│  ┌────▼─────────┐              ┌─────▼──────┐  │
│  │   Serial     │              │  Protocol  │  │
│  │   Handler    │◄────────────►│  Handler   │  │
│  └────┬─────────┘              └─────┬──────┘  │
│       │                              │         │
│       │  ┌───────────────────────┐   │         │
│       │  │  Conversation         │   │         │
│       └─►│  Manager              │◄──┘         │
│          └──────┬────────────────┘             │
│                 │                              │
│          ┌──────▼──────┐   ┌─────────────┐    │
│          │  Storage    │   │  API Client │    │
│          │  (JSON)     │   │  (OpenAI)   │    │
│          └─────────────┘   └─────────────┘    │
└─────────────────────────────────────────────────┘
```

## Module Design

### Project Structure
```
c64llm_proxy/
├── src/
│   ├── __init__.py
│   ├── main.py              - Entry point, event loop
│   ├── serial_handler.py    - Serial communication
│   ├── protocol.py          - Protocol encode/decode
│   ├── conversation.py      - Conversation management
│   ├── storage.py           - JSON persistence
│   ├── api_client.py        - OpenAI API interface
│   └── config.py            - Configuration management
├── data/
│   └── conversations/       - Conversation JSON files
├── config.toml              - Configuration file
├── requirements.txt         - Python dependencies
└── README.md
```

## Module Specifications

### 1. main.py - Application Entry Point

**Responsibilities:**
- Initialize all components
- Run main async event loop
- Handle graceful shutdown
- Command-line argument parsing

```python
import asyncio
import argparse
import logging
from serial_handler import SerialHandler
from protocol import ProtocolHandler
from conversation import ConversationManager
from api_client import APIClient
from config import Config

async def main():
    """Main entry point"""
    # Parse command line args
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', default='/dev/ttyUSB0', help='Serial port')
    parser.add_argument('--baud', type=int, default=1200, help='Baud rate')
    parser.add_argument('--config', default='config.toml', help='Config file')
    args = parser.parse_args()

    # Load configuration
    config = Config(args.config)

    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # Initialize components
    api_client = APIClient(config)
    conv_manager = ConversationManager(config)
    protocol = ProtocolHandler(conv_manager, api_client)
    serial = SerialHandler(args.port, args.baud, protocol)

    try:
        # Run serial handler (this blocks in event loop)
        await serial.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await serial.close()
        await api_client.close()

if __name__ == '__main__':
    asyncio.run(main())
```

### 2. serial_handler.py - Serial Communication

**Responsibilities:**
- Manage serial port connection
- Async read/write to serial
- Buffer management
- XON/XOFF flow control

```python
import asyncio
import serial_asyncio
import logging
from typing import Callable

class SerialHandler:
    """Handles serial port communication"""

    def __init__(self, port: str, baud: int, protocol_handler):
        self.port = port
        self.baud = baud
        self.protocol = protocol_handler
        self.reader = None
        self.writer = None
        self.logger = logging.getLogger(__name__)

    async def connect(self):
        """Open serial connection"""
        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=self.port,
            baudrate=self.baud,
            bytesize=8,
            parity='N',
            stopbits=1,
            xonxoff=True  # Enable software flow control
        )
        self.logger.info(f"Connected to {self.port} at {self.baud} baud")

    async def run(self):
        """Main serial read loop"""
        await self.connect()

        # Register write callback with protocol
        self.protocol.set_write_callback(self.write)

        try:
            while True:
                # Read one byte at a time
                data = await self.reader.read(1)
                if data:
                    # Pass to protocol handler
                    await self.protocol.process_byte(data[0])
        except asyncio.CancelledError:
            self.logger.info("Serial handler cancelled")

    async def write(self, data: bytes):
        """Write data to serial port"""
        self.writer.write(data)
        await self.writer.drain()

    async def close(self):
        """Close serial connection"""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
```

**Key Features:**
- Uses `serial_asyncio` for non-blocking serial I/O
- Integrates with asyncio event loop
- Automatic XON/XOFF handling
- Byte-by-byte processing for protocol parsing

### 3. protocol.py - Protocol Handler

**Responsibilities:**
- Encode/decode protocol messages
- Frame synchronization
- CRC validation
- Dispatch to appropriate handlers
- Message queue management

```python
import struct
import asyncio
from enum import IntEnum
from typing import Callable, Optional
import logging

class MessageType(IntEnum):
    """Message type codes"""
    CHAT_REQUEST = 0x01
    CANCEL_REQUEST = 0x02
    LIST_CONVERSATIONS = 0x03
    LOAD_CONVERSATION = 0x04
    NEW_CONVERSATION = 0x05
    PING = 0x06
    ACK = 0x10
    NAK = 0x11
    CHAT_CHUNK = 0x20
    CHAT_DONE = 0x21
    CHAT_ERROR = 0x22
    CONVERSATION_LIST = 0x23
    CONVERSATION_DATA = 0x24
    STATUS = 0x25

class ProtocolState(IntEnum):
    """Protocol parsing state"""
    SYNC_SEARCHING = 0
    READING_TYPE = 1
    READING_LENGTH = 2
    READING_PAYLOAD = 3
    VALIDATING_CRC = 4

class ProtocolHandler:
    """Handles protocol message encoding/decoding"""

    SYNC_BYTE = 0xC6
    MAX_PAYLOAD = 2048

    def __init__(self, conv_manager, api_client):
        self.conv_manager = conv_manager
        self.api_client = api_client
        self.logger = logging.getLogger(__name__)

        # Parser state
        self.state = ProtocolState.SYNC_SEARCHING
        self.msg_type = 0
        self.msg_length = 0
        self.payload = bytearray()
        self.bytes_read = 0

        # Write callback (set by serial handler)
        self.write_callback: Optional[Callable] = None

        # Current streaming task
        self.stream_task: Optional[asyncio.Task] = None

    def set_write_callback(self, callback: Callable):
        """Set callback for writing to serial"""
        self.write_callback = callback

    async def process_byte(self, byte: int):
        """Process one byte from serial"""

        if self.state == ProtocolState.SYNC_SEARCHING:
            if byte == self.SYNC_BYTE:
                self.state = ProtocolState.READING_TYPE

        elif self.state == ProtocolState.READING_TYPE:
            self.msg_type = byte
            self.state = ProtocolState.READING_LENGTH
            self.bytes_read = 0

        elif self.state == ProtocolState.READING_LENGTH:
            if self.bytes_read == 0:
                self.msg_length = byte
                self.bytes_read = 1
            else:
                self.msg_length |= (byte << 8)
                self.payload = bytearray()
                self.bytes_read = 0
                if self.msg_length > 0:
                    self.state = ProtocolState.READING_PAYLOAD
                else:
                    self.state = ProtocolState.VALIDATING_CRC

        elif self.state == ProtocolState.READING_PAYLOAD:
            self.payload.append(byte)
            if len(self.payload) >= self.msg_length:
                self.state = ProtocolState.VALIDATING_CRC

        elif self.state == ProtocolState.VALIDATING_CRC:
            # Validate CRC
            expected_crc = self._calculate_crc()
            if byte == expected_crc:
                # Valid message, dispatch
                await self._dispatch_message()
            else:
                self.logger.warning(f"CRC mismatch: expected {expected_crc}, got {byte}")
                await self.send_nak()
            # Reset to search for next message
            self.state = ProtocolState.SYNC_SEARCHING

    def _calculate_crc(self) -> int:
        """Calculate XOR checksum"""
        crc = self.msg_type
        crc ^= (self.msg_length & 0xFF)
        crc ^= ((self.msg_length >> 8) & 0xFF)
        for b in self.payload:
            crc ^= b
        return crc & 0xFF

    async def _dispatch_message(self):
        """Dispatch received message to handler"""
        msg_type = MessageType(self.msg_type)

        self.logger.info(f"Received message: {msg_type.name}, length={self.msg_length}")

        if msg_type == MessageType.CHAT_REQUEST:
            await self.handle_chat_request()
        elif msg_type == MessageType.CANCEL_REQUEST:
            await self.handle_cancel()
        elif msg_type == MessageType.LIST_CONVERSATIONS:
            await self.handle_list_conversations()
        elif msg_type == MessageType.LOAD_CONVERSATION:
            await self.handle_load_conversation()
        elif msg_type == MessageType.NEW_CONVERSATION:
            await self.handle_new_conversation()
        elif msg_type == MessageType.PING:
            await self.send_ack()
        elif msg_type == MessageType.ACK:
            pass  # Acknowledge received
        else:
            self.logger.warning(f"Unknown message type: {self.msg_type}")

    async def send_message(self, msg_type: MessageType, payload: bytes = b''):
        """Send a protocol message"""
        if not self.write_callback:
            self.logger.error("Write callback not set")
            return

        # Build frame
        frame = bytearray()
        frame.append(self.SYNC_BYTE)
        frame.append(msg_type)
        frame.append(len(payload) & 0xFF)
        frame.append((len(payload) >> 8) & 0xFF)
        frame.extend(payload)

        # Calculate and append CRC
        crc = msg_type
        crc ^= (len(payload) & 0xFF)
        crc ^= ((len(payload) >> 8) & 0xFF)
        for b in payload:
            crc ^= b
        frame.append(crc & 0xFF)

        # Send
        await self.write_callback(bytes(frame))

    async def send_ack(self):
        """Send ACK"""
        await self.send_message(MessageType.ACK)

    async def send_nak(self):
        """Send NAK"""
        await self.send_message(MessageType.NAK)

    async def send_status(self, status: str):
        """Send status message"""
        payload = status.encode('ascii') + b'\x00'
        await self.send_message(MessageType.STATUS, payload)

    # Message handlers

    async def handle_chat_request(self):
        """Handle chat request from C64"""
        # Extract message text (null-terminated)
        text = self.payload.rstrip(b'\x00').decode('ascii', errors='replace')

        await self.send_ack()

        # Add user message to conversation
        self.conv_manager.add_message('user', text)

        # Start streaming task
        self.stream_task = asyncio.create_task(self._stream_response())

    async def _stream_response(self):
        """Stream API response to C64"""
        try:
            await self.send_status("Contacting API...")

            seq = 0
            full_response = ""

            # Stream from API
            async for chunk in self.api_client.stream_chat(
                self.conv_manager.get_messages()
            ):
                if chunk:
                    full_response += chunk

                    # Send chunk to C64
                    payload = bytearray()
                    payload.append(seq)
                    payload.extend(chunk.encode('ascii', errors='replace'))
                    payload.append(0x00)  # Null terminator

                    await self.send_message(MessageType.CHAT_CHUNK, bytes(payload))

                    seq = (seq + 1) % 256

                    # Small delay to avoid overwhelming C64
                    await asyncio.sleep(0.05)

            # Send completion
            payload = struct.pack('<BH', seq, len(full_response))
            await self.send_message(MessageType.CHAT_DONE, payload)

            # Save assistant response to conversation
            self.conv_manager.add_message('assistant', full_response)
            self.conv_manager.save()

        except Exception as e:
            self.logger.error(f"Error streaming response: {e}")
            error_msg = str(e).encode('ascii', errors='replace') + b'\x00'
            await self.send_message(MessageType.CHAT_ERROR, error_msg)

    async def handle_cancel(self):
        """Handle cancel request"""
        if self.stream_task:
            self.stream_task.cancel()
            self.stream_task = None
        await self.send_ack()

    async def handle_list_conversations(self):
        """Send conversation list to C64"""
        await self.send_ack()

        conversations = self.conv_manager.list_conversations()

        # Send in chunks (max 5 per message to keep under size limit)
        chunk_size = 5
        for i in range(0, len(conversations), chunk_size):
            chunk = conversations[i:i+chunk_size]
            more = 1 if (i + chunk_size) < len(conversations) else 0

            payload = bytearray()
            payload.append(len(chunk))  # Count
            payload.append(more)  # More flag

            for conv in chunk:
                # ID (4 bytes), timestamp (4 bytes), title (null-terminated)
                payload.extend(struct.pack('<II', conv['id'], conv['timestamp']))
                title = conv['title'][:38]  # Truncate for C64
                payload.extend(title.encode('ascii', errors='replace'))
                payload.append(0x00)

            await self.send_message(MessageType.CONVERSATION_LIST, bytes(payload))
            await asyncio.sleep(0.1)  # Give C64 time to process

    async def handle_load_conversation(self):
        """Load a conversation"""
        conv_id = struct.unpack('<I', self.payload[:4])[0]

        await self.send_ack()

        if self.conv_manager.load_conversation(conv_id):
            messages = self.conv_manager.get_messages()

            # Send messages in chunks
            chunk_size = 3
            for i in range(0, len(messages), chunk_size):
                chunk = messages[i:i+chunk_size]
                more = 1 if (i + chunk_size) < len(messages) else 0

                payload = bytearray()
                payload.append(len(chunk))
                payload.append(more)

                for msg in chunk:
                    # Role (1 byte), text (null-terminated)
                    role = 0 if msg['role'] == 'user' else 1
                    payload.append(role)
                    text = msg['content'][:500]  # Truncate long messages
                    payload.extend(text.encode('ascii', errors='replace'))
                    payload.append(0x00)

                await self.send_message(MessageType.CONVERSATION_DATA, bytes(payload))
                await asyncio.sleep(0.1)
        else:
            error = b"Conversation not found\x00"
            await self.send_message(MessageType.CHAT_ERROR, error)

    async def handle_new_conversation(self):
        """Start a new conversation"""
        self.conv_manager.new_conversation()
        await self.send_ack()
```

### 4. conversation.py - Conversation Management

**Responsibilities:**
- Create/load/save conversations
- Maintain message history
- Open WebUI JSON format compatibility

```python
import json
import time
import os
from pathlib import Path
from typing import List, Dict, Optional
import logging

class ConversationManager:
    """Manages conversation storage and retrieval"""

    def __init__(self, config):
        self.config = config
        self.data_dir = Path(config.data_dir) / 'conversations'
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.current_conversation = None
        self.current_id = None
        self.logger = logging.getLogger(__name__)

    def new_conversation(self) -> int:
        """Start a new conversation"""
        conv_id = int(time.time())
        self.current_id = conv_id
        self.current_conversation = {
            'id': conv_id,
            'created_at': conv_id,
            'updated_at': conv_id,
            'title': 'New Conversation',
            'messages': []
        }
        self.logger.info(f"Started new conversation: {conv_id}")
        return conv_id

    def add_message(self, role: str, content: str):
        """Add message to current conversation"""
        if not self.current_conversation:
            self.new_conversation()

        message = {
            'role': role,
            'content': content,
            'timestamp': int(time.time())
        }
        self.current_conversation['messages'].append(message)
        self.current_conversation['updated_at'] = int(time.time())

        # Auto-generate title from first user message
        if role == 'user' and len(self.current_conversation['messages']) == 1:
            title = content[:40].strip()
            if len(content) > 40:
                title += '...'
            self.current_conversation['title'] = title

    def get_messages(self) -> List[Dict]:
        """Get messages for API (role/content only)"""
        if not self.current_conversation:
            return []
        return [
            {'role': msg['role'], 'content': msg['content']}
            for msg in self.current_conversation['messages']
        ]

    def save(self):
        """Save current conversation to disk"""
        if not self.current_conversation:
            return

        filename = f"{self.current_id}.json"
        filepath = self.data_dir / filename

        # Open WebUI compatible format
        data = {
            'id': str(self.current_id),
            'title': self.current_conversation['title'],
            'created_at': self.current_conversation['created_at'],
            'updated_at': self.current_conversation['updated_at'],
            'chat': {
                'messages': self.current_conversation['messages']
            }
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

        self.logger.info(f"Saved conversation: {filepath}")

    def load_conversation(self, conv_id: int) -> bool:
        """Load a conversation from disk"""
        filename = f"{conv_id}.json"
        filepath = self.data_dir / filename

        if not filepath.exists():
            self.logger.warning(f"Conversation not found: {conv_id}")
            return False

        with open(filepath, 'r') as f:
            data = json.load(f)

        self.current_id = conv_id
        self.current_conversation = {
            'id': conv_id,
            'title': data['title'],
            'created_at': data['created_at'],
            'updated_at': data['updated_at'],
            'messages': data['chat']['messages']
        }

        self.logger.info(f"Loaded conversation: {conv_id}")
        return True

    def list_conversations(self) -> List[Dict]:
        """List all conversations"""
        conversations = []

        for filepath in sorted(self.data_dir.glob('*.json'), reverse=True):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    conversations.append({
                        'id': int(data['id']),
                        'title': data['title'],
                        'timestamp': data['updated_at']
                    })
            except Exception as e:
                self.logger.error(f"Error loading {filepath}: {e}")

        return conversations[:50]  # Limit to 50 most recent
```

### 5. api_client.py - OpenAI API Client

**Responsibilities:**
- Interface with OpenAI-compatible API
- Streaming response handling
- Error handling and retries

```python
import httpx
import logging
from typing import AsyncIterator, List, Dict

class APIClient:
    """OpenAI-compatible API client"""

    def __init__(self, config):
        self.config = config
        self.base_url = config.api_base_url
        self.api_key = config.api_key
        self.model = config.model
        self.logger = logging.getLogger(__name__)

        self.client = httpx.AsyncClient(
            timeout=60.0,
            headers={'Authorization': f'Bearer {self.api_key}'}
        )

    async def stream_chat(self, messages: List[Dict]) -> AsyncIterator[str]:
        """Stream chat completion"""

        payload = {
            'model': self.model,
            'messages': messages,
            'stream': True,
            'temperature': 0.7,
            'max_tokens': 2000
        }

        url = f"{self.base_url}/chat/completions"

        try:
            async with self.client.stream('POST', url, json=payload) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith('data: '):
                        data = line[6:]  # Remove 'data: ' prefix

                        if data == '[DONE]':
                            break

                        try:
                            import json
                            chunk = json.loads(data)

                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                if 'content' in delta:
                                    yield delta['content']

                        except json.JSONDecodeError:
                            continue

        except httpx.HTTPError as e:
            self.logger.error(f"API request failed: {e}")
            raise

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
```

### 6. config.py - Configuration Management

**Responsibilities:**
- Load configuration from TOML
- Environment variable overrides
- Validation

```python
import os
import toml
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Config:
    """Application configuration"""

    api_base_url: str
    api_key: str
    model: str
    data_dir: str

    def __init__(self, config_file: str):
        """Load configuration"""

        # Load TOML
        if Path(config_file).exists():
            with open(config_file, 'r') as f:
                config = toml.load(f)
        else:
            config = {}

        # Get values with env var overrides
        self.api_base_url = os.getenv(
            'OPENAI_API_BASE',
            config.get('api', {}).get('base_url', 'https://api.openai.com/v1')
        )

        self.api_key = os.getenv(
            'OPENAI_API_KEY',
            config.get('api', {}).get('key', '')
        )

        self.model = os.getenv(
            'OPENAI_MODEL',
            config.get('api', {}).get('model', 'gpt-3.5-turbo')
        )

        self.data_dir = config.get('storage', {}).get(
            'data_dir',
            './data'
        )

        # Validate
        if not self.api_key:
            raise ValueError("API key not configured")
```

## Configuration File Format

### config.toml
```toml
[api]
base_url = "https://api.openai.com/v1"
# key = "sk-..."  # Better to use OPENAI_API_KEY env var
model = "gpt-3.5-turbo"

[storage]
data_dir = "./data"

[serial]
# Default port and baud (can be overridden by CLI args)
port = "/dev/ttyUSB0"
baud = 1200
```

## Open WebUI Conversation Format

### Conversation JSON Structure
```json
{
  "id": "1703123456",
  "title": "Discussion about C64 programming",
  "created_at": 1703123456,
  "updated_at": 1703125678,
  "chat": {
    "messages": [
      {
        "role": "user",
        "content": "How do I use the VIC-II chip?",
        "timestamp": 1703123456
      },
      {
        "role": "assistant",
        "content": "The VIC-II chip is the graphics processor...",
        "timestamp": 1703123460
      }
    ]
  }
}
```

**Compatibility Notes:**
- ID is Unix timestamp (unique per conversation)
- Timestamps are Unix epoch seconds
- Role values: "user", "assistant", "system"
- Can be imported directly into Open WebUI

## Dependencies

### requirements.txt
```
pyserial-asyncio>=0.6
httpx>=0.24.0
python-dotenv>=1.0.0
toml>=0.10.2
```

## Deployment

### Installation
```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create config
cp config.toml.example config.toml
# Edit config.toml or set OPENAI_API_KEY env var
```

### Running
```bash
# Basic
python -m src.main

# With custom serial port
python -m src.main --port /dev/ttyUSB0 --baud 2400

# With environment variables
OPENAI_API_KEY=sk-... python -m src.main
```

### Systemd Service (Optional)
```ini
[Unit]
Description=C64 LLM Proxy
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user/c64llm_proxy
Environment="OPENAI_API_KEY=sk-..."
ExecStart=/home/your-user/c64llm_proxy/venv/bin/python -m src.main
Restart=always

[Install]
WantedBy=multi-user.target
```

## Error Handling

### Serial Errors
- **Port not found**: Exit with error message
- **Permission denied**: Suggest adding user to dialout group
- **Connection lost**: Attempt reconnection with exponential backoff

### API Errors
- **Rate limit**: Send status to C64, retry with backoff
- **Network error**: Send error message to C64, allow retry
- **Invalid response**: Log error, send error message to C64

### Protocol Errors
- **CRC failure**: Send NAK, expect retransmission
- **Malformed message**: Discard, send NAK
- **Buffer overflow**: Fragment message or send error

## Logging

### Log Levels
- **DEBUG**: Protocol bytes, detailed flow
- **INFO**: Messages sent/received, conversations loaded/saved
- **WARNING**: CRC failures, retries
- **ERROR**: API errors, storage errors

### Log Format
```
2024-01-15 14:32:15 INFO [protocol] Received CHAT_REQUEST, length=45
2024-01-15 14:32:15 INFO [api_client] Streaming chat completion
2024-01-15 14:32:16 INFO [protocol] Sent CHAT_CHUNK seq=0
```

## Testing

### Unit Tests
```python
# test_protocol.py
import pytest
from src.protocol import ProtocolHandler

def test_crc_calculation():
    """Test CRC calculation"""
    handler = ProtocolHandler(None, None)
    # ...

def test_message_encoding():
    """Test message frame encoding"""
    # ...
```

### Integration Tests
```python
# test_integration.py
import pytest
from unittest.mock import Mock
from src.serial_handler import SerialHandler
from src.protocol import ProtocolHandler

@pytest.mark.asyncio
async def test_chat_flow():
    """Test complete chat request/response flow"""
    # Mock serial, API
    # Send chat request
    # Verify chunks received
    # ...
```

### Manual Testing
```bash
# Use socat to create virtual serial port pair
socat -d -d pty,raw,echo=0 pty,raw,echo=0

# Point C64 to one end, proxy to other
# Or use Python script to simulate C64
```

## Performance Considerations

### Throughput
- 1200 baud = ~120 bytes/sec
- Typical chat response: 500-2000 bytes
- Streaming enables display before completion (5-20 sec total)

### Latency
- API first token: 1-3 seconds
- Chunk transmission: ~0.1-0.5 sec per chunk
- Total response time: Depends on length

### Memory
- Minimal memory usage (<50MB typical)
- Conversation files stored on disk
- No in-memory caching of old conversations

## Security Considerations

### API Key Protection
- Never commit API keys to git
- Use environment variables or secure config
- File permissions: `chmod 600 config.toml`

### Conversation Privacy
- Conversations stored locally only
- No cloud storage in v1
- Future: Optional encryption at rest

### Input Validation
- Validate message lengths
- Sanitize before API calls
- Prevent injection attacks (minimal risk with binary protocol)

## Future Enhancements

### Features
- Multiple concurrent C64 clients (multi-serial)
- WebSocket interface for monitoring
- Conversation search/indexing
- Export conversations to other formats
- Configurable system prompts
- Token usage tracking and limits

### Performance
- Compression for large messages
- Caching frequent responses
- Batch API requests if queued

### Reliability
- Automatic reconnection on serial drop
- Message persistence/replay on failure
- Health checks and monitoring

## Troubleshooting

### Common Issues

**Serial port permission denied**
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

**API key not working**
```bash
# Verify key
echo $OPENAI_API_KEY

# Test API directly
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

**Slow responses**
- Check baud rate (try 2400 instead of 1200)
- Verify API endpoint latency
- Check network connection

**Garbled text**
- Verify baud rate matches C64
- Check serial cable connections
- Enable flow control (XON/XOFF)
