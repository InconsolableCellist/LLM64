"""Protocol message encoding/decoding and handling"""

import struct
import asyncio
from enum import IntEnum
from typing import Callable, Optional
import logging


class MessageType(IntEnum):
    """Message type codes - using printable ASCII to avoid tcpser/IP232 corruption"""
    # C64 -> Server
    CHAT_REQUEST = 0x31  # '1' - was 0x01
    CANCEL_REQUEST = 0x32  # '2' - was 0x02
    LIST_CONVERSATIONS = 0x33  # '3' - was 0x03
    LOAD_CONVERSATION = 0x34  # '4' - was 0x04
    NEW_CONVERSATION = 0x35  # '5' - was 0x05
    PING = 0x36  # '6' - was 0x06
    ACK = 0x40  # '@' - was 0x10
    NAK = 0x41  # 'A' - was 0x11

    # Server -> C64
    CHAT_CHUNK = 0x50  # 'P' - was 0x20
    CHAT_DONE = 0x51  # 'Q' - was 0x21
    CHAT_ERROR = 0x52  # 'R' - was 0x22
    CONVERSATION_LIST = 0x53  # 'S' - was 0x23
    CONVERSATION_DATA = 0x54  # 'T' - was 0x24
    STATUS = 0x55  # 'U' - was 0x25


class ProtocolState(IntEnum):
    """Protocol parsing state"""
    SYNC_SEARCHING = 0
    READING_TYPE = 1
    READING_LENGTH = 2
    READING_PAYLOAD = 3
    VALIDATING_CRC = 4


class ProtocolHandler:
    """Handles protocol message encoding/decoding"""

    SYNC_BYTE = 0x42  # 'B' - safe ASCII byte (was 0xC6, corrupted by VICE IP232/Telnet encoding)
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
        self.length_bytes = bytearray()

        # Write callback (set by TCP server)
        self.write_callback: Optional[Callable] = None

        # Current streaming task
        self.stream_task: Optional[asyncio.Task] = None

    def set_write_callback(self, callback: Callable):
        """Set callback for writing to client"""
        self.write_callback = callback

    async def process_byte(self, byte: int):
        """Process one byte from client (state machine)"""

        if self.state == ProtocolState.SYNC_SEARCHING:
            if byte == self.SYNC_BYTE:
                self.state = ProtocolState.READING_TYPE
                self.logger.debug("SYNC byte found")

        elif self.state == ProtocolState.READING_TYPE:
            self.msg_type = byte
            self.state = ProtocolState.READING_LENGTH
            self.length_bytes = bytearray()
            self.logger.debug(f"Message type: 0x{byte:02X}")

        elif self.state == ProtocolState.READING_LENGTH:
            self.length_bytes.append(byte)
            if len(self.length_bytes) >= 2:
                # Decode length bytes (subtract 0x20 offset) and combine little-endian
                len_lo = (self.length_bytes[0] - 0x20) & 0xFF
                len_hi = (self.length_bytes[1] - 0x20) & 0xFF
                self.msg_length = len_lo | (len_hi << 8)
                self.payload = bytearray()
                self.logger.debug(f"Message length: {self.msg_length}")

                if self.msg_length > 0:
                    if self.msg_length > self.MAX_PAYLOAD:
                        self.logger.warning(f"Payload too large: {self.msg_length}")
                        self.state = ProtocolState.SYNC_SEARCHING
                    else:
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
                self.logger.debug("CRC valid, dispatching message")
                # Valid message, dispatch
                await self._dispatch_message()
            else:
                self.logger.warning(
                    f"CRC mismatch: expected 0x{expected_crc:02X}, got 0x{byte:02X}"
                )
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
        try:
            msg_type = MessageType(self.msg_type)
            self.logger.info(
                f"Received: {msg_type.name} (length={self.msg_length})"
            )

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
                self.logger.debug("ACK received")
            else:
                self.logger.warning(f"Unknown message type: 0x{self.msg_type:02X}")

        except ValueError:
            self.logger.error(f"Invalid message type: 0x{self.msg_type:02X}")

    async def send_message(self, msg_type: MessageType, payload: bytes = b''):
        """Send a protocol message"""
        if not self.write_callback:
            self.logger.error("Write callback not set")
            return

        if len(payload) > self.MAX_PAYLOAD:
            self.logger.error(f"Payload too large: {len(payload)}")
            return

        # Build frame
        frame = bytearray()
        frame.append(self.SYNC_BYTE)
        frame.append(msg_type)
        # Encode length bytes (add 0x20 to avoid NUL bytes for IP232/Telnet compatibility)
        frame.append((len(payload) & 0xFF) + 0x20)
        frame.append(((len(payload) >> 8) & 0xFF) + 0x20)
        frame.extend(payload)

        # Calculate and append CRC
        crc = msg_type
        crc ^= (len(payload) & 0xFF)
        crc ^= ((len(payload) >> 8) & 0xFF)
        for b in payload:
            crc ^= b
        frame.append(crc & 0xFF)

        # Send
        self.logger.debug(f"Sending: {msg_type.name} (length={len(payload)})")
        await self.write_callback(bytes(frame))

    async def send_ack(self):
        """Send ACK"""
        await self.send_message(MessageType.ACK)

    async def send_nak(self):
        """Send NAK"""
        await self.send_message(MessageType.NAK)

    async def send_status(self, status: str):
        """Send status message"""
        payload = status.encode('ascii', errors='replace') + b'\x00'
        await self.send_message(MessageType.STATUS, payload)

    # Message handlers

    async def handle_chat_request(self):
        """Handle chat request from C64"""
        # Extract message text (null-terminated)
        text = self.payload.rstrip(b'\x00').decode('ascii', errors='replace')
        self.logger.info(f"Chat request: {text[:50]}...")

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

            self.logger.info(f"Response complete: {len(full_response)} bytes")

        except asyncio.CancelledError:
            self.logger.info("Stream cancelled")
            # Send partial completion
            payload = struct.pack('<BH', seq, len(full_response))
            await self.send_message(MessageType.CHAT_DONE, payload)
            # Still save what we have
            if full_response:
                self.conv_manager.add_message('assistant', full_response)
                self.conv_manager.save()

        except Exception as e:
            self.logger.error(f"Error streaming response: {e}")
            error_msg = str(e)[:200].encode('ascii', errors='replace') + b'\x00'
            await self.send_message(MessageType.CHAT_ERROR, error_msg)

    async def handle_cancel(self):
        """Handle cancel request"""
        self.logger.info("Cancel request received")
        if self.stream_task:
            self.stream_task.cancel()
            try:
                await self.stream_task
            except asyncio.CancelledError:
                pass
            self.stream_task = None
        await self.send_ack()

    async def handle_list_conversations(self):
        """Send conversation list to C64"""
        self.logger.info("List conversations request")
        await self.send_ack()

        conversations = self.conv_manager.list_conversations()
        self.logger.info(f"Found {len(conversations)} conversations")

        # Send in chunks (max 5 per message to keep under size limit)
        chunk_size = 5
        for i in range(0, len(conversations), chunk_size):
            chunk = conversations[i:i+chunk_size]
            more = 1 if (i + chunk_size) < len(conversations) else 0

            payload = bytearray()
            payload.append(len(chunk))  # Count
            payload.append(more)  # More flag

            count = 0
            for conv in chunk:
                try:
                    # ID and timestamp masked to 32 bits: some stored ids are
                    # millisecond timestamps that overflow the 4-byte field.
                    # handle_load_conversation resolves masked ids.
                    entry = bytearray()
                    entry.extend(struct.pack(
                        '<II',
                        int(conv['id']) & 0xFFFFFFFF,
                        int(conv['timestamp']) & 0xFFFFFFFF))
                    title = str(conv['title'])[:36]
                    entry.extend(title.encode('ascii', errors='replace'))
                    entry.append(0x00)
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"Skipping malformed conversation: {e}")
                    continue
                payload.extend(entry)
                count += 1
            payload[0] = count

            await self.send_message(MessageType.CONVERSATION_LIST, bytes(payload))
            await asyncio.sleep(0.1)  # Give C64 time to process

    async def handle_load_conversation(self):
        """Load a conversation"""
        if len(self.payload) < 4:
            self.logger.error("Invalid LOAD_CONVERSATION payload")
            await self.send_nak()
            return

        masked_id = struct.unpack('<I', self.payload[:4])[0]
        self.logger.info(f"Load conversation: {masked_id}")

        await self.send_ack()

        # The wire id is 32-bit; stored ids may be wider (ms timestamps),
        # so resolve by masked comparison.
        conv_id = masked_id
        for conv in self.conv_manager.list_conversations():
            if int(conv['id']) & 0xFFFFFFFF == masked_id:
                conv_id = int(conv['id'])
                break

        if self.conv_manager.load_conversation(conv_id):
            messages = self.conv_manager.get_messages()

            # One message per frame: the C64 client's payload buffer is
            # small (512 bytes), so keep each frame well under that.
            for i, msg in enumerate(messages):
                more = 1 if i + 1 < len(messages) else 0

                payload = bytearray()
                payload.append(1)
                payload.append(more)
                role = 0 if msg['role'] == 'user' else 1
                payload.append(role)
                text = msg['content'][:400]
                payload.extend(text.encode('ascii', errors='replace'))
                payload.append(0x00)

                await self.send_message(MessageType.CONVERSATION_DATA, bytes(payload))
                await asyncio.sleep(0.1)
        else:
            error = b"Conversation not found\x00"
            await self.send_message(MessageType.CHAT_ERROR, error)

    async def handle_new_conversation(self):
        """Start a new conversation"""
        self.logger.info("New conversation request")
        self.conv_manager.new_conversation()
        await self.send_ack()
