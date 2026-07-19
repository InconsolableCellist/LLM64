"""Protocol message encoding/decoding and handling"""

import struct
import asyncio
import time
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional
import logging

from .modes import (Mode, AdventureMode, RoleplayMode, CharacterCard,
                    find_cards)
from .music import MusicLibrary, MusicDirectiveFilter
from .images import ImageService


class MessageType(IntEnum):
    """Message type codes - using printable ASCII to avoid tcpser/IP232 corruption"""
    # C64 -> Server
    CHAT_REQUEST = 0x31  # '1' - was 0x01
    CANCEL_REQUEST = 0x32  # '2' - was 0x02
    LIST_CONVERSATIONS = 0x33  # '3' - was 0x03
    LOAD_CONVERSATION = 0x34  # '4' - was 0x04
    NEW_CONVERSATION = 0x35  # '5' - was 0x05
    PING = 0x36  # '6' - was 0x06
    LIST_MODELS = 0x37  # '7'
    SET_MODEL = 0x38  # '8'
    ACK = 0x40  # '@' - was 0x10
    NAK = 0x41  # 'A' - was 0x11

    # Server -> C64
    CHAT_CHUNK = 0x50  # 'P' - was 0x20
    CHAT_DONE = 0x51  # 'Q' - was 0x21
    CHAT_ERROR = 0x52  # 'R' - was 0x22
    CONVERSATION_LIST = 0x53  # 'S' - was 0x23
    CONVERSATION_DATA = 0x54  # 'T' - was 0x24
    STATUS = 0x55  # 'U' - was 0x25
    MODEL_LIST = 0x56  # 'V'
    SID_BEGIN = 0x57  # 'W' - streamed SID: metadata
    SID_DATA = 0x58  # 'X' - streamed SID: raw bytes into the $B000 window
    SID_END = 0x59  # 'Y' - streamed SID: start playback
    IMG_BEGIN = 0x5A  # 'Z' - fullscreen image incoming
    IMG_DATA = 0x5B  # '[' - image bytes (8000 bitmap + 1000 matrix)
    IMG_END = 0x5C  # '\' - image complete
    HINT = 0x5D  # ']' - persistent status flags (bit0: pic suggestion)


# Common Unicode punctuation -> ASCII approximations, applied before the
# ascii/replace encode so LLM typography doesn't become '?' on the C64.
UNICODE_TO_ASCII = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'",
    '“': '"', '”': '"', '„': '"',
    '–': '-', '—': '-', '―': '-', '−': '-',
    '…': '...', '•': '*', '·': '*',
    ' ': ' ', '→': '->', '←': '<-',
    '×': 'x', '÷': '/', '°': ' deg',
})


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
        self.config = api_client.config
        self.mode = Mode(self.config)
        self.model_override: Optional[str] = None
        # Media sends run from both the stream task and the NAK-retry
        # path; interleaving their DATA frames on the wire (and in the
        # client's shared chunk map) would corrupt both transfers.
        self._media_lock = asyncio.Lock()
        # BEGIN handshake event: set when the client ACKs a SID/IMG
        # BEGIN (music silenced, rendering drained - clear to stream)
        self._begin_ack = None
        # Window flow-control event (see _send_bulk_stream)
        self._flow_ack = None
        # Media sends triggered from the reader task must run as
        # background tasks: _send_begin waits for an ACK that only the
        # reader can dispatch, so awaiting it inline would deadlock
        self._media_tasks = set()
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

        self._started = time.monotonic()
        self._tunes_sent = 0
        self._img_sent = False
        self._sid_retry = None

        # SID music library (optional: absent moods.json disables music)
        self.music = MusicLibrary(
            Path(self.config.data_dir) / 'sids' / 'moods.json')
        if self.music.available:
            self.logger.info(
                f"Music library: {len(self.music.tunes)} tunes")

        # Scene illustrations (optional: needs Pillow + the gemini key,
        # or the C64LLM_IMG_FIXTURE test hook)
        self.images = ImageService(Path(self.config.data_dir),
                                   mode=getattr(self.config, 'images_mode',
                                                'ask'))
        if self.images.available:
            self.logger.info(f"Images enabled (mode: {self.images.mode})")

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
            elif msg_type == MessageType.LIST_MODELS:
                await self.handle_list_models()
            elif msg_type == MessageType.SET_MODEL:
                await self.handle_set_model()
            elif msg_type == MessageType.ACK:
                self.logger.debug("ACK received")
                if self._begin_ack is not None \
                        and not self._begin_ack.is_set():
                    # BEGIN handshake: client stopped its music and
                    # drained rendering - clear to stream
                    self._begin_ack.set()
                elif getattr(self, '_flow_ack', None) is not None \
                        and not self._flow_ack.is_set():
                    self._flow_ack.set()   # window flow control
                else:
                    self._sid_retry = None
            elif msg_type == MessageType.NAK:
                # A NAK often means the client was still digesting the
                # previous bulk send (post-load rendering backlog) when
                # this transfer arrived - retrying instantly hits the
                # same congestion. Wait it out first, and allow two
                # attempts (seen in the field: back-to-back retries
                # failed identically).
                if getattr(self, '_img_sent', False):
                    # No auto-retry for images (user preference: work
                    # first time or fail visibly); /pic <n> re-sends
                    # from cache on demand
                    self._img_sent = False
                    # No status frame here: the client just printed its
                    # loss diagnostics (i fail g=...) in the status bar
                    # and a STATUS would overwrite them
                    self.logger.warning("Image NAKed - not retrying")
                elif getattr(self, '_sid_retry', None) \
                        and getattr(self, '_sid_tries', 0):
                    self._sid_tries -= 1
                    self.logger.warning(
                        f"SID NAKed - retrying in {self.RETRY_DELAY}s "
                        f"({self._sid_tries} attempts left)")
                    await self.send_status("Retrying music...")
                    tune = self._sid_retry
                    if not self._sid_tries:
                        self._sid_retry = None
                    self._spawn_media(self._retry_sid(tune))
            else:
                self.logger.warning(f"Unknown message type: 0x{self.msg_type:02X}")

        except ValueError as e:
            if 'is not a valid MessageType' in str(e):
                self.logger.error(
                    f"Invalid message type: 0x{self.msg_type:02X}")
            else:
                self.logger.error("Handler error", exc_info=True)
        except Exception:
            self.logger.error("Handler error", exc_info=True)

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
        # Encode length bytes (+0x20, wrapped: reduces NUL bytes for
        # IP232/Telnet; the decoder subtracts with uint8 wrap-around)
        frame.append(((len(payload) & 0xFF) + 0x20) & 0xFF)
        frame.append((((len(payload) >> 8) & 0xFF) + 0x20) & 0xFF)
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
        text = text.strip()
        self.logger.info(f"Chat request: {text[:50]}...")

        await self.send_ack()

        if text.startswith('/'):
            await self.handle_command(text)
            return

        # Add user message to conversation
        self.conv_manager.add_message('user', text)

        # Start streaming task
        self.stream_task = asyncio.create_task(self._stream_response())

    # --- slash commands / modes ---------------------------------------

    async def handle_command(self, text: str):
        """Mode-switching commands typed on the C64 (/adventure, /char...)"""
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ''
        arg = parts[1].strip() if len(parts) > 1 else ''

        if cmd == 'help':
            await self._send_canned(
                "Commands:\n"
                "/chat - plain chat mode\n"
                "/adventure [theme] - text adventure\n"
                "/chars - list character cards\n"
                "/char <name> - roleplay with a card\n"
                "/models - list models, /model <name> - switch\n"
                "/music <mood> - play a tune (/music for moods)\n"
                "/pic [desc|n] - illustrate scene / re-show pic n\n"
                "/pics - list this conversation's pictures\n"
                "/save [name] - checkpoint this conversation\n"
                "/saves - list, /restore <n> - roll back\n"
                "/history [page] - browse the full conversation\n"
                "/find <text> - search this conversation\n"
                "/findall <text> - search all conversations\n"
                "/stats - server statistics\n"
                "/mode - show current mode")

        elif cmd == 'mode':
            await self._send_canned(f"Current mode: {self.mode.label}")

        elif cmd == 'chat':
            self._switch_mode(Mode(self.config))
            await self._send_canned("Chat mode. New conversation started.")

        elif cmd in ('adventure', 'adv'):
            mode = AdventureMode(self.config, theme=arg)
            self._attach_snippets(mode)
            self._switch_mode(mode)
            await self.send_status("Generating your adventure...")
            self.stream_task = asyncio.create_task(
                self._stream_response(hidden_user_msg=self.mode.kickoff()))

        elif cmd == 'chars':
            cards = find_cards(Path(self.config.cards_dir))
            if cards:
                lines = ["Characters:"]
                lines += [f"- {name}" for name, _ in cards]
                lines.append("Start with: /char <name>")
                await self._send_canned("\n".join(lines))
            else:
                await self._send_canned(
                    f"No cards found in {self.config.cards_dir}. "
                    "Drop SillyTavern .json cards there.")

        elif cmd == 'char':
            await self._start_roleplay(arg)

        elif cmd in ('pic', 'pics'):
            if not self.images.available:
                await self._send_canned("Images not enabled on this server.")
            elif arg.isdigit():
                await self._resend_pic(int(arg))
            elif cmd == 'pics':
                await self._list_pics()
            else:
                # Priority: explicit description > pending suggestion >
                # ask the model to describe the current scene itself
                prompt = arg or self.images.pending_prompt
                if not prompt:
                    await self.send_status("Studying the scene...")
                    prompt = await self._derive_scene_prompt()
                if not prompt:
                    await self._send_canned(
                        "Couldn't picture the scene - try "
                        "/pic <description>.")
                else:
                    self.images.pending_prompt = None
                    await self.send_message(MessageType.HINT, bytes([0]))
                    # Complete the chat round-trip first: the client must
                    # return to idle before the image transfer starts
                    await self._send_canned(f"Illustrating: {prompt[:300]}")
                    self._spawn_media(self._generate_and_send_image(prompt))

        elif cmd in ('history', 'hist'):
            await self._show_history(arg)

        elif cmd == 'find':
            await self._find_history(arg)

        elif cmd == 'findall':
            await self._find_all(arg)

        elif cmd == 'testpat':
            # Debug probe: stream a synthetic image of one repeated byte
            # through the real transfer path. '/testpat 55 music' keeps
            # the tune playing during the transfer (tests whether SID
            # play routines really blind the ACIA on this firmware).
            parts = arg.split()
            keep = len(parts) > 1 and parts[1].lower() == 'music'
            try:
                byte = int(parts[0] if parts else '0', 16) & 0xFF
            except ValueError:
                await self._send_canned(
                    "Usage: /testpat <hex byte> [music]")
                return
            await self._send_canned(
                f"Test pattern 0x{byte:02x}"
                f"{' with music' if keep else ''} - any key dismisses.")
            self._spawn_media(self.send_image_blob(
                bytes([byte]) * 10000, 0, keep_music=keep))

        elif cmd == 'save':
            label = self.conv_manager.save_checkpoint(arg)
            if label:
                await self._send_canned(f"Checkpoint saved: {label}\n"
                                        "(/saves lists, /restore <n> rolls back)")
            else:
                await self._send_canned("Nothing to save yet.")

        elif cmd in ('saves', 'checkpoints'):
            cps = self.conv_manager.list_checkpoints()
            if cps:
                lines = ["Checkpoints:"]
                lines += [f"{i}. {c['name']} ({c['messages']} msgs)"
                          for i, c in enumerate(cps, 1)]
                lines.append("Restore with: /restore <n>")
                await self._send_canned("\n".join(lines))
            else:
                await self._send_canned(
                    "No checkpoints for this conversation. /save makes one.")

        elif cmd == 'restore':
            try:
                name = self.conv_manager.restore_checkpoint(int(arg))
            except ValueError:
                name = ''
            if name:
                await self._send_canned(
                    f"Restored: {name}. The story continues from there...")
            else:
                await self._send_canned("Usage: /restore <n> (see /saves)")

        elif cmd == 'stats':
            convs = self.conv_manager.list_conversations()
            msgs = 0
            chars = 0
            for c in convs:
                msgs += c.get('message_count', 0)
            up = int(time.monotonic() - self._started)
            lines = [
                "Server stats:",
                f"conversations: {len(convs)}",
                f"messages: {msgs}",
                f"music library: {len(self.music.tunes)} tunes",
                f"tunes played this session: {self._tunes_sent}",
                f"proxy uptime: {up // 3600}h {(up % 3600) // 60}m",
                f"model: {self.model_override or self.config.model}",
            ]
            await self._send_canned("\n".join(lines))

        elif cmd == 'music':
            if not self.music.available:
                await self._send_canned("No music library on this server.")
            elif not arg:
                await self._send_canned(
                    "Moods: " + ", ".join(self.music.moods)
                    + "\nUse: /music <mood>  (S key stops)")
            else:
                tune = self.music.pick(arg.lower())
                if tune:
                    await self._send_canned(
                        f"Playing: {tune['title']} ({tune['author']})")
                    self._spawn_media(self.send_sid(tune))
                    self.conv_manager.set_meta('music', {
                        'mood': arg.lower(), 'tune': tune['id']})
                    self.conv_manager.save()
                else:
                    await self._send_canned(
                        f"No tune fits '{arg}'. /music lists moods.")

        elif cmd == 'models':
            try:
                names = await self.api_client.list_models()
            except Exception as e:
                names = []
            if names:
                await self._send_canned(
                    "Models:\n" + "\n".join(f"- {n}" for n in names)
                    + "\nSwitch with: /model <name>")
            else:
                await self._send_canned("Could not fetch the model list.")

        elif cmd == 'model':
            if arg:
                await self._set_model(arg)
                await self._send_canned("Model switched.")
            else:
                current = self.model_override or self.config.model
                await self._send_canned(f"Current model: {current}")

        else:
            await self._send_canned(
                f"Unknown command: /{cmd} (try /help)")

    async def handle_list_models(self):
        """Send the server's model list to the C64 (MODEL_LIST frames)."""
        await self.send_ack()
        try:
            names = await self.api_client.list_models()
        except Exception as e:
            self.logger.error(f"Model list failed: {e}")
            names = []

        chunk_size = 8
        if not names:
            await self.send_message(MessageType.MODEL_LIST, bytes([0, 0]))
            return
        for i in range(0, len(names), chunk_size):
            chunk = names[i:i + chunk_size]
            more = 1 if (i + chunk_size) < len(names) else 0
            payload = bytearray([len(chunk), more])
            for name in chunk:
                payload.extend(name[:36].encode('ascii', errors='replace'))
                payload.append(0x00)
            await self.send_message(MessageType.MODEL_LIST, bytes(payload))
            await asyncio.sleep(0.05)

    async def handle_set_model(self):
        name = self.payload.rstrip(b'\x00').decode('ascii', errors='replace')
        await self.send_ack()
        await self._set_model(name)

    async def _set_model(self, query: str):
        """Resolve a (possibly truncated) model name and switch to it."""
        try:
            names = await self.api_client.list_models()
        except Exception:
            names = []
        match = query
        q = query.lower()
        for name in names:
            if name.lower().startswith(q) or q in name.lower():
                match = name
                break
        self.model_override = match
        self.logger.info(f"Model -> {match}")
        await self.send_status(f"Model: {match[:32]}")

    def _attach_snippets(self, mode):
        """Give an AdventureMode the directive instructions for whichever
        media services are live on this server."""
        snippet = ''
        if self.music.available:
            snippet += self.music.prompt_snippet()
        if self.images.available:
            snippet += self.images.prompt_snippet()
        mode.music_snippet = snippet

    def _switch_mode(self, mode):
        self.mode = mode
        self.conv_manager.new_conversation()
        # Record the mode on the conversation so loading it later can
        # restore the same experience (prompt, sampling, music directives)
        self.conv_manager.set_meta('mode', mode.name)
        if getattr(mode, 'theme', ''):
            self.conv_manager.set_meta('theme', mode.theme)
        self.logger.info(f"Mode -> {mode.label}")

    def _find_card(self, query: str):
        """Match a card by name prefix/substring: (name, path) or None."""
        q = query.lower()
        for name, path in find_cards(Path(self.config.cards_dir)):
            if name.lower().startswith(q) or q in name.lower():
                return (name, path)
        return None

    async def _start_roleplay(self, query: str):
        if not query:
            await self._send_canned("Usage: /char <name> (see /chars)")
            return
        match = self._find_card(query)
        if not match:
            await self._send_canned(
                f"No card matching '{query}'. See /chars.")
            return

        card = CharacterCard.load(match[1], user_name=self.config.user_name)
        mode = RoleplayMode(self.config, card)
        self._attach_snippets(mode)
        self._switch_mode(mode)
        # Card name in meta so loading this conversation later can
        # rebuild the same character
        self.conv_manager.set_meta('char', match[0])

        greeting = self.mode.greeting()
        if greeting:
            # Stream the card's first_mes as the opening assistant turn
            self.conv_manager.add_message('assistant', greeting)
            self.conv_manager.save()
            await self._send_canned(greeting)
        else:
            await self._send_canned(
                f"You are now talking to {card.name}.")

    # The C64 spends ~0.7ms of CPU per received byte (protocol parsing +
    # rendering) against a 1.04ms/byte wire rate at 9600 baud. Send small
    # frames, paced slightly below the wire rate, so sustained streams can
    # never outrun it - LLM APIs emit arbitrarily large chunks, which is
    # exactly what corrupted long responses before.
    # Pacing must stay below the C64's real consumption rate, which is
    # set by rendering, not the wire: the soft-80 build needs ~20-30ms
    # per frame plus ~130ms per scrolled line. These values yield ~480
    # chars/s - still several times faster than reading speed - with
    # comfortable headroom for both screen modes.
    CHUNK_TEXT_MAX = 60
    CHUNK_PACE_BASE = 0.016       # per frame
    CHUNK_PACE_PER_BYTE = 0.0018  # per payload byte

    # Bulk frames (conversation list/load) are wire-bound: pace each frame
    # at just over its 9600-baud transmit time so the burst never piles up
    # in the modem's TCP->serial buffer. The C64U bridge drops the tail
    # when that buffer fills - losing the final more=0 frame left the
    # client frozen at 'Loading... NN' forever.
    BULK_PACE_BASE = 0.01
    BULK_PACE_PER_BYTE = 0.0012   # ~15% over the 1.04ms/byte wire rate

    async def _send_bulk(self, msg_type: MessageType, payload: bytes):
        """Send one bulk frame and sleep out its wire time."""
        await self.send_message(msg_type, payload)
        await asyncio.sleep(self.BULK_PACE_BASE
                            + len(payload) * self.BULK_PACE_PER_BYTE)

    SID_CHUNK = 256

    # The C64U bridge drops WHOLE PACKETS from its TCP->serial queue
    # when scheduling jitter bunches our paced writes (field signature:
    # exactly 1-2 chunks missing, cr=00 - clean frame-sized holes, any
    # content). Windowed flow control bounds the queue by construction:
    # the client ACKs every FLOW_WINDOW chunks and the proxy sends no
    # further until it does, so at most ~1KB is ever in flight.
    FLOW_WINDOW = 4
    RETRY_DELAY = 2.0       # quiet time before resending a NAKed transfer

    def _spawn_media(self, coro):
        """Run a media send off the reader task (see _media_tasks)."""
        task = asyncio.create_task(coro)
        self._media_tasks.add(task)
        task.add_done_callback(self._media_tasks.discard)

    async def _retry_sid(self, tune):
        """Spaced SID resend (resume) after a NAK."""
        await asyncio.sleep(self.RETRY_DELAY)
        await self.send_sid(tune, is_retry=True)

    async def _resume_tune(self, tune, frames: int):
        """Resume a loaded conversation's soundtrack once the client has
        had time to render the load (a SID streamed into that backlog
        overflowed its RX ring in the field; the BEGIN handshake also
        gates this, the sleep is belt and braces)."""
        await asyncio.sleep(min(5.0, 1.0 + 0.2 * frames))
        await self.send_sid(tune)

    async def _send_begin(self, msg_type: MessageType,
                          payload: bytes) -> bool:
        """Send a SID/IMG BEGIN and wait for the client's ACK before any
        data flows. A playing tune's SEI windows can eat bytes at the
        ACIA, including the BEGIN frame itself - so BEGIN is re-sent
        until ACKed, and the ACK guarantees the client has silenced its
        music and finished rendering. Returns False if it never ACKs."""
        for attempt in range(4):
            self._begin_ack = asyncio.Event()
            await self._send_bulk(msg_type, payload)
            try:
                await asyncio.wait_for(self._begin_ack.wait(), 2.0)
                return True
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"{msg_type.name} not ACKed (attempt {attempt + 1})"
                    " - resending")
            finally:
                self._begin_ack = None
        return False

    async def _send_bulk_stream(self, msg_type: MessageType, data: bytes):
        """Chunk a large blob into paced, flow-controlled frames. Each
        frame carries its byte offset so the client can place it even
        after a gap; every FLOW_WINDOW frames the proxy waits for the
        client's ACK so the modem's packet queue can never overflow."""
        n = 0
        self._flow_ack = asyncio.Event()
        try:
            for i in range(0, len(data), self.SID_CHUNK):
                await self._send_bulk(msg_type, struct.pack('<H', i)
                                      + data[i:i + self.SID_CHUNK])
                n += 1
                if n % self.FLOW_WINDOW == 0:
                    try:
                        await asyncio.wait_for(self._flow_ack.wait(), 3.0)
                    except asyncio.TimeoutError:
                        self.logger.warning(
                            f"Flow ACK timeout at chunk {n} - continuing")
                    self._flow_ack = asyncio.Event()
        finally:
            self._flow_ack = None

    async def send_sid(self, tune, is_retry: bool = False):
        """Stream a relocated SID into the client's $B000 window, paced
        like any other bulk transfer (the C64U modem drops burst tails).
        Data flows only after the client ACKs the BEGIN; a NAK
        afterwards (short/corrupt transfer) triggers spaced resends."""
        data = self.music.payload(tune)
        head = struct.pack('<HHHBHBBB', tune['load'], tune['init'],
                           tune['play'],
                           max(0, tune.get('start_song', 1) - 1), len(data),
                           tune.get('vol_byte') or 0,
                           1 if is_retry else 0,
                           self.FLOW_WINDOW)
        name = tune['title'][:24].encode('ascii', errors='replace')
        async with self._media_lock:
            if not await self._send_begin(MessageType.SID_BEGIN,
                                          head + name + b'\x00'):
                self.logger.error("SID_BEGIN never ACKed - aborting send")
                return
            await self._send_bulk_stream(MessageType.SID_DATA, data)
            await self._send_bulk(MessageType.SID_END, b'')
        self._tunes_sent += 1
        self.music.tune_started = time.monotonic()
        if not is_retry:
            self._sid_retry = tune
            self._sid_tries = 2
        self.logger.info(f"Sent SID {tune['id']} ({len(data)} bytes"
                         f"{', retry' if is_retry else ''})")

    async def send_image_blob(self, blob: bytes, bg: int = 0,
                              is_retry: bool = False, fmt: int = 1,
                              keep_music: bool = False):
        """Stream a converted image into the client's bitmap, paced.
        BEGIN payload: format byte (1 = multicolor, 0 = hires) + bg
        color + resume flag; data flows only after the client ACKs the
        BEGIN. No auto-retry on NAK - the client restores its screen
        and shows diagnostics; /pic <n> re-sends from cache."""
        self._img_sent = True
        async with self._media_lock:
            if not await self._send_begin(
                    MessageType.IMG_BEGIN,
                    bytes([fmt, bg & 0x0F, 1 if is_retry else 0,
                           self.FLOW_WINDOW,
                           1 if keep_music else 0])):
                self.logger.error("IMG_BEGIN never ACKed - aborting send")
                await self.send_status("Image transfer couldn't start.")
                return
            await self._send_bulk_stream(MessageType.IMG_DATA, blob)
            await self._send_bulk(MessageType.IMG_END, b'')
        self.logger.info(f"Sent image ({len(blob)} bytes"
                         f"{', retry' if is_retry else ''})")

    async def _ask_model(self, question: str, limit: int = 300) -> str:
        """One-shot utility question to the chat model, plain text back."""
        out = ""
        try:
            async for kind, chunk in self.api_client.stream_chat(
                    [{'role': 'user', 'content': question}],
                    system_prompt=None, sampling={},
                    model=self.model_override):
                if kind != 'reasoning' and chunk:
                    out += chunk
        except Exception as e:
            self.logger.error(f"Utility query failed: {e}")
            return ''
        return out.strip()[:limit]

    async def _derive_scene_prompt(self) -> str:
        """Ask the chat model for a visual description of the current
        scene (bare /pic with nothing pending). Reads well back into the
        transcript and at the prompts of earlier illustrations so
        recurring characters and places keep their established look."""
        msgs = self.conv_manager.get_messages()[-12:]
        if not msgs:
            return ''
        convo = "\n".join(f"{m['role']}: {m['content'][:500]}"
                          for m in msgs)
        prior = [p['prompt'] for p in
                 self.conv_manager.get_meta('images', [])[-3:]]
        consistency = ''
        if prior:
            consistency = (
                "\n\nEarlier illustrations in this story showed:\n"
                + "\n".join(f"- {p}" for p in prior)
                + "\nKeep characters and places visually consistent "
                  "with those.")
        return await self._ask_model(
            "Below is the latest part of a text adventure. Write ONE "
            "sentence visually describing the CURRENT scene for an "
            "illustrator. Include the established appearance of the "
            "characters and setting (clothing, hair, architecture, "
            "lighting) from earlier in the story. Reply with only that "
            "sentence." + consistency + "\n\nTranscript:\n" + convo,
            limit=400)

    async def _make_caption(self, prompt: str) -> str:
        """A short atmospheric caption, burned into the picture and
        echoed into the chat. Falls back to the prompt itself."""
        out = await self._ask_model(
            "Write ONE short atmospheric caption (at most 10 words, no "
            "quotation marks) for this adventure-game illustration, "
            "addressed to the player. Reply with only the caption. "
            "Scene: " + prompt, limit=100)
        return out.strip('"').strip() or prompt[:60]

    # The client's scrollback is the view, not the archive: /history pages
    # through the full stored conversation from the proxy, /find searches
    # it. A page of 3 messages x 1500 chars wraps to ~55 lines at 80
    # cols, inside the ~120-line scrollback (this is where full text
    # lives, so the snippet cap is generous).
    HISTORY_PAGE = 3
    HISTORY_SNIP = 1500

    async def _show_history(self, arg: str):
        msgs = self.conv_manager.get_messages()
        if not msgs:
            await self._send_canned("No history yet.")
            return
        pages = (len(msgs) + self.HISTORY_PAGE - 1) // self.HISTORY_PAGE
        try:
            page = int(arg) if arg else pages
        except ValueError:
            await self._send_canned("Usage: /history [page]")
            return
        page = max(1, min(page, pages))
        lo = (page - 1) * self.HISTORY_PAGE
        sel = msgs[lo:lo + self.HISTORY_PAGE]
        lines = [f"--- page {page}/{pages} "
                 f"(msgs {lo + 1}-{lo + len(sel)} of {len(msgs)}) ---"]
        for i, m in enumerate(sel, lo + 1):
            body = m['content']
            snip = body[:self.HISTORY_SNIP]
            if len(body) > self.HISTORY_SNIP:
                snip += " [...]"
            who = '>' if m['role'] == 'user' else ':'
            lines.append(f"[{i}]{who} {snip}")
        nav = []
        if page > 1:
            nav.append(f"/history {page - 1} = older")
        if page < pages:
            nav.append(f"/history {page + 1} = newer")
        if nav:
            lines.append("(" + ", ".join(nav) + ")")
        await self._send_canned("\n".join(lines))

    async def _find_history(self, needle: str):
        if not needle:
            await self._send_canned("Usage: /find <text>")
            return
        msgs = self.conv_manager.get_messages()
        low = needle.lower()
        hits = []
        for i, m in enumerate(msgs, 1):
            pos = m['content'].lower().find(low)
            if pos >= 0:
                hits.append((i, m, pos))
        if not hits:
            await self._send_canned(f'No match for "{needle[:40]}".')
            return
        shown = hits[-10:]
        lines = [f'{len(hits)} match(es) for "{needle[:40]}":']
        for i, m, pos in shown:
            page = (i - 1) // self.HISTORY_PAGE + 1
            start = max(0, pos - 20)
            ctx = m['content'][start:start + 70].replace('\n', ' ')
            lines.append(f"[{i}] p{page}: ...{ctx}...")
        if len(hits) > len(shown):
            lines.append(f"(newest {len(shown)} shown)")
        lines.append("View a hit with /history <p>.")
        await self._send_canned("\n".join(lines))

    async def _find_all(self, needle: str):
        """Search every saved conversation (conversation-manager seed)."""
        if not needle:
            await self._send_canned("Usage: /findall <text>")
            return
        hits = self.conv_manager.search_all(needle)
        if not hits:
            await self._send_canned(
                f'No conversation mentions "{needle[:40]}".')
            return
        lines = [f'Conversations mentioning "{needle[:40]}":']
        for h in hits:
            when = time.strftime('%b %d', time.localtime(h['timestamp']))
            lines.append(f"- {h['title'][:36]} ({when}, {h['hits']} "
                         f"hit{'s' if h['hits'] != 1 else ''})")
            lines.append(f"    ...{h['snippet']}...")
        lines.append("Open one via F5, then /find to jump.")
        await self._send_canned("\n".join(lines))

    async def _list_pics(self):
        """This conversation's generated pictures, newest first."""
        pics = self.conv_manager.get_meta('images', [])
        if not pics:
            await self._send_canned("No pictures in this conversation "
                                    "yet. /pic <desc> makes one.")
            return
        cur = len(self.conv_manager.get_messages())
        lines = ["Pictures (newest first):"]
        for i, p in enumerate(reversed(pics[-9:]), 1):
            ago = ''
            if 'at_msg' in p:
                turns = max(0, (cur - p['at_msg']) // 2)
                ago = f" ({turns} turns ago)" if turns else " (this turn)"
            lines.append(f"{i}. {p['prompt'][:48]}{ago}")
        lines.append("Show again with: /pic <n>")
        await self._send_canned("\n".join(lines))

    async def _resend_pic(self, n: int):
        """Re-stream picture #n from the /pics list (cached blob, no
        generation cost)."""
        pics = list(reversed(self.conv_manager.get_meta('images', [])[-9:]))
        if not 1 <= n <= len(pics):
            await self._send_canned("No such picture. /pics lists them.")
            return
        path = self.images.dir / f"{pics[n - 1]['stem']}.blob"
        try:
            data = path.read_bytes()
        except OSError:
            await self._send_canned("That picture's data is gone.")
            return
        await self._send_canned(
            f"Showing: {pics[n - 1].get('caption') or pics[n - 1]['prompt'][:60]}")
        if len(data) == 10001:      # multicolor: blob + bg byte
            self._spawn_media(self.send_image_blob(data[:10000],
                                                   data[10000]))
        else:                       # hires-era blob
            self._spawn_media(self.send_image_blob(data, 0, fmt=0))

    async def _generate_and_send_image(self, prompt: str):
        await self.send_status("Illustrating... (10-20s)")
        hb = asyncio.create_task(self._heartbeat("Illustrating..."))
        try:
            caption = await self._make_caption(prompt)
            blob, stem, bg = await self.images.generate_blob(
                prompt, self.conv_manager.current_id, caption)
        except Exception as e:
            self.logger.error(f"Image generation failed: {e}")
            await self.send_status("Illustration failed.")
            return
        finally:
            hb.cancel()
        pics = self.conv_manager.get_meta('images', [])
        pics.append({'stem': stem, 'prompt': prompt[:200],
                     'caption': caption[:100],
                     'at_msg': len(self.conv_manager.get_messages())})
        self.conv_manager.set_meta('images', pics)
        self.conv_manager.save()
        # The caption lands in the scrollback before the screen freezes,
        # so it's also the last chat line when the picture is dismissed
        await self._send_canned(f'"{caption}"')
        await self.send_image_blob(blob, bg)

    async def _send_text_chunk(self, seq: int, piece: bytes) -> int:
        """Send one CHAT_CHUNK frame and pace; returns next seq."""
        payload = bytearray()
        payload.append(seq)
        payload.extend(piece)
        payload.append(0x00)
        await self.send_message(MessageType.CHAT_CHUNK, bytes(payload))
        await asyncio.sleep(self.CHUNK_PACE_BASE
                            + len(piece) * self.CHUNK_PACE_PER_BYTE)
        return (seq + 1) % 256

    async def _heartbeat(self, label: str, interval: float = 10.0,
                         beats: int = 18):
        """Keep the client's response watchdog fed during long silent
        waits (cold prompt-eval of a big conversation, model load, image
        generation). Capped: if the wait outlives the cap, the silence
        lets the client watchdog abort as a last resort."""
        try:
            for i in range(1, beats + 1):
                await asyncio.sleep(interval)
                await self.send_status(f"{label} ({int(i * interval)}s)")
        except asyncio.CancelledError:
            pass

    async def _send_canned(self, text: str):
        """Stream local text to the C64 as a normal reply (no API call)."""
        seq = 0
        data = text.encode('ascii', errors='replace')
        for i in range(0, len(data), self.CHUNK_TEXT_MAX):
            seq = await self._send_text_chunk(
                seq, data[i:i + self.CHUNK_TEXT_MAX])
        await self.send_message(MessageType.CHAT_DONE,
                                struct.pack('<BH', seq, len(text)))

    async def _stream_response(self, hidden_user_msg: str = None):
        """Stream API response to C64"""
        hb = None
        try:
            await self.send_status("Contacting API...")

            seq = 0
            full_response = ""

            messages = self.conv_manager.get_messages()
            if hidden_user_msg:
                # Mode kickoff: sent to the API but not persisted
                messages = messages + [
                    {'role': 'user', 'content': hidden_user_msg}]

            # In adventure mode the model may steer the soundtrack with
            # [[MUSIC: mood]]; strip that from what the C64 sees (and from
            # saved history) and act on it after the response completes.
            mfilter = None
            if self.mode.name in ('adventure', 'roleplay') and (
                    self.music.available or self.images.available):
                mfilter = MusicDirectiveFilter()

            # Nudge, don't nag: after ~5 minutes of one tune looping,
            # remind the narrator it owns the soundtrack
            sys_prompt = self.mode.system_prompt()
            if (mfilter and self.music.available and self.music.stale()
                    and sys_prompt):
                sys_prompt += ("\n(The background music has been looping "
                               "for several minutes; if the scene's tone "
                               "warrants it, this reply is a good moment "
                               "for a MUSIC directive.)")

            # Heartbeat until the first token arrives (a 79K-char
            # conversation's cold prompt-eval outlives the client's 40s
            # watchdog otherwise)
            hb = asyncio.create_task(self._heartbeat("Thinking..."))
            thinking_notified = False
            async for kind, chunk in self.api_client.stream_chat(
                messages,
                system_prompt=sys_prompt,
                sampling=self.mode.sampling(),
                model=self.model_override
            ):
                if hb:
                    hb.cancel()
                    hb = None
                if kind == 'reasoning':
                    if not thinking_notified:
                        thinking_notified = True
                        await self.send_status("Thinking...")
                    continue
                if chunk:
                    if mfilter:
                        chunk = mfilter.feed(chunk)
                        if not chunk:
                            continue
                    full_response += chunk

                    # Split into small paced frames regardless of the size
                    # the API chose to emit (see CHUNK_TEXT_MAX comment)
                    data = chunk.translate(UNICODE_TO_ASCII).encode(
                        'ascii', errors='replace')
                    for i in range(0, len(data), self.CHUNK_TEXT_MAX):
                        seq = await self._send_text_chunk(
                            seq, data[i:i + self.CHUNK_TEXT_MAX])

            if mfilter:
                tail = mfilter.flush()
                if tail:
                    full_response += tail
                    data = tail.translate(UNICODE_TO_ASCII).encode(
                        'ascii', errors='replace')
                    for i in range(0, len(data), self.CHUNK_TEXT_MAX):
                        seq = await self._send_text_chunk(
                            seq, data[i:i + self.CHUNK_TEXT_MAX])

            # Send completion
            payload = struct.pack('<BH', seq, len(full_response))
            await self.send_message(MessageType.CHAT_DONE, payload)

            # Save assistant response to conversation
            self.conv_manager.add_message('assistant', full_response)
            self.conv_manager.save()

            self.logger.info(f"Response complete: {len(full_response)} bytes")

            # Model requested a scene illustration: generate now (auto,
            # rate-limited) or park it as a /pic suggestion (ask)
            if mfilter and mfilter.images and self.images.available:
                prompt = mfilter.images[0]
                if self.images.auto_ok():
                    self.images.mark_auto()
                    await self._generate_and_send_image(prompt)
                elif self.images.mode == 'ask':
                    self.images.pending_prompt = prompt
                    await self.send_status(
                        "Scene available - /pic to illustrate")
                    await self.send_message(MessageType.HINT, bytes([1]))

            # Model asked for a music change: honor at most one, after the
            # text is fully delivered (the client is idle again), unless a
            # change happened too recently
            if mfilter and mfilter.moods:
                if self.music.rate_limited():
                    self.logger.info(
                        f"Music directive rate-limited: {mfilter.moods}")
                else:
                    tune = self.music.pick(mfilter.moods[0])
                    if tune:
                        await self.send_sid(tune)
                        self.music.mark_changed()
                        # Remember what's playing: loading this
                        # conversation later resumes the soundtrack
                        self.conv_manager.set_meta('music', {
                            'mood': mfilter.moods[0], 'tune': tune['id']})
                        self.conv_manager.save()

            # Give the conversation a meaningful LLM-generated title
            # (fire-and-forget; does not delay the DONE frame above)
            asyncio.create_task(self._maybe_title())

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
        finally:
            if hb:
                hb.cancel()

    async def _maybe_title(self):
        """Ask the model for a short conversation title, once per
        conversation, after the first exchange completes."""
        conv = self.conv_manager.current_conversation
        if not conv or conv.get('auto_titled'):
            return
        msgs = self.conv_manager.get_messages()
        if len(msgs) < 2:
            return
        try:
            excerpt = "\n".join(
                f"{m['role']}: {m['content'][:300]}" for m in msgs[:4])
            prompt = [{'role': 'user', 'content':
                       "Reply with ONLY a short title (3-5 words, plain "
                       "ASCII, no quotes or punctuation) describing this "
                       "conversation:\n\n" + excerpt}]
            out = ''
            async for kind, chunk in self.api_client.stream_chat(
                    prompt, system_prompt='',
                    sampling={'max_tokens': 24, 'temperature': 0.3},
                    model=self.model_override):
                if kind == 'content':
                    out += chunk
            title = out.strip().strip('"\'').splitlines()[0].strip()[:38]
            if title:
                self.conv_manager.set_title(title, auto=True)
        except Exception as e:
            self.logger.warning(f"Title generation failed: {e}")

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

            await self._send_bulk(MessageType.CONVERSATION_LIST, bytes(payload))

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

            # Window to what the client can actually keep: its scrollback
            # holds ~120 wrapped lines, so send only the NEWEST messages
            # fitting a ~6KB budget instead of replaying a whole epic at
            # 9600 baud (a long roleplay used to take minutes and looked
            # like a hang). Long messages are cut at 1000 chars with a
            # visible marker; /history has the full text.
            MSG_CAP = 1000
            budget = 6000
            window = []
            for m in reversed(messages):
                cost = min(len(m['content']), MSG_CAP) + 2
                if budget - cost < 0 and window:
                    break
                budget -= cost
                window.append(m)
            window.reverse()
            omitted = len(messages) - len(window)

            def clip(text):
                if len(text) <= MSG_CAP:
                    return text
                return text[:MSG_CAP] + " [... /history shows the rest]"

            frames = []
            if omitted > 0:
                frames.append((2, f'(... {omitted} earlier messages '
                                  f'not shown - /history has them ...)'))
            frames += [(0 if m['role'] == 'user' else 1,
                        clip(m['content'])) for m in window]
            # Zero frames would leave the client waiting forever: the
            # 'load done' signal is the final more=0 frame
            if not frames:
                frames.append((2, '(empty conversation)'))

            # One message per frame: the C64 client's payload buffer is
            # small (512 bytes), so keep each frame well under that.
            for i, (role, text) in enumerate(frames):
                more = 1 if i + 1 < len(frames) else 0

                payload = bytearray()
                payload.append(1)
                payload.append(more)
                payload.append(role)
                payload.extend(text.encode('ascii', errors='replace'))
                payload.append(0x00)

                await self._send_bulk(MessageType.CONVERSATION_DATA,
                                      bytes(payload))

            # Restore what the conversation was: mode (an adventure loaded
            # into chat mode would lose its prompt and music directives)...
            meta_mode = self.conv_manager.get_meta('mode')
            if meta_mode is None and messages \
                    and messages[0].get('role') == 'assistant':
                # Pre-meta conversation: adventures (and roleplay) start
                # with an assistant message (hidden kickoff/greeting);
                # plain chats start with the user. Treat as adventure and
                # make it stick.
                meta_mode = 'adventure'
                self.conv_manager.set_meta('mode', 'adventure')
                self.conv_manager.save()
            if meta_mode == 'adventure' and self.mode.name != 'adventure':
                mode = AdventureMode(
                    self.config, theme=self.conv_manager.get_meta('theme', ''))
                self._attach_snippets(mode)
                self.mode = mode  # not _switch_mode: keep the conversation
                self.logger.info("Restored adventure mode from conversation")
            elif meta_mode == 'roleplay' and self.mode.name != 'roleplay':
                cname = self.conv_manager.get_meta('char', '')
                m = self._find_card(cname) if cname else None
                if m:
                    card = CharacterCard.load(
                        m[1], user_name=self.config.user_name)
                    mode = RoleplayMode(self.config, card)
                    self._attach_snippets(mode)
                    self.mode = mode
                    self.logger.info(
                        f"Restored roleplay mode ({m[0]}) from conversation")

            # ...and the soundtrack (exact tune if still in the library,
            # else another tune of the same mood). The transfer starts only
            # after the last CONVERSATION_DATA frame so nothing interleaves.
            music_meta = self.conv_manager.get_meta('music')
            if music_meta and self.music.available:
                tune = (self.music.find(music_meta.get('tune'))
                        or self.music.pick(music_meta.get('mood', '')))
                if tune:
                    self._spawn_media(self._resume_tune(tune, len(frames)))
        else:
            error = b"Conversation not found\x00"
            await self.send_message(MessageType.CHAT_ERROR, error)

    async def handle_new_conversation(self):
        """Start a new conversation"""
        self.logger.info("New conversation request")
        self.conv_manager.new_conversation()
        await self.send_ack()
