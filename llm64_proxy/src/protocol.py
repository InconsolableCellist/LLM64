"""Protocol message encoding/decoding and handling"""

import json
import struct
import asyncio
import time
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional
import logging

from .modes import (Mode, AdventureMode, RoleplayMode, ClaudeMode,
                    CharacterCard, find_cards)
from .claude_session import ClaudeSession
from .music import MusicLibrary, MusicDirectiveFilter
from .dice import expand as expand_dice
from .advsetup import (AdventureSetup, STAGES, ACT_QUICK,
                       ACT_THEME, ACT_BEGIN, ACT_LOAD)
from .advtemplates import TemplateStore
from . import advmap
from . import printdoc
from . import printcups
from .scenecomp import compose_question
from .markup import colorize_for_wire, split_safe, UNICODE_TO_ASCII
from .markup import prompt_snippet as color_prompt_snippet
from .images import ImageService
from .imagegen import make_backend


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
    DELETE_CONVERSATION = 0x39  # '9' - id(4); ACK/NAK
    STAR_CONVERSATION = 0x3A  # ':' - id(4); toggles starred, ACK/NAK
    GET_MENU = 0x3B  # ';' - request the server-fed menu
    GET_NOWPLAYING = 0x3C  # '<' - jukebox asks what is playing
    FAV_TUNE = 0x3D  # '=' - toggle favorite on the current tune
    SET_BAUD = 0x3E  # '>' - client's wire rate: 2 bytes LE, nominal/100
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
    HINT = 0x5D  # ']' - [flags(bit0: pic)][pics][chrome\0]; chrome is
    #                    the right-hand status text (place, music),
    #                    composed here so it can change with no client
    #                    rebuild. Soft-80 only on the client side.
    NOTICE = 0x60  # '`' - out-of-band system line (dice results)
    NOWPLAYING = 0x5F  # '_' - [flags][elapsed:2][secs:2] then
    #                     title\0 author\0 mood\0 (jukebox module)
    MENU_LIST = 0x5E  # '^' - menu entries: [n][more] then
    #                   [key][label\0][cmd\0] each; cmd "!x" = client-local
    MUSIC_STOP = 0x61  # 'a' - silence a streamed SID (no ACK)
    PRINT_BEGIN = 0x62  # 'b' - [flags][nblocks]; open IEC device 4.
    #                     flags bit0 = business charset (secondary
    #                     address 7), bit1 = form feed before close
    PRINT_DATA = 0x63  # 'c' - one block of ASCII text (<= 240 bytes);
    #                    the client ACKs each one, and nothing else is
    #                    on the wire while it prints (docs/14 §3)
    PRINT_END = 0x64  # 'd' - close the channel


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
        # Wire baud this session is pacing to. None until the client
        # announces it via SET_BAUD; falls back to config.wire_baud (the
        # old-client default). Only bulk pacing reads it.
        self._wire_baud = None
        # BEGIN handshake event: set when the client ACKs a SID/IMG
        # BEGIN (music silenced, rendering drained - clear to stream)
        self._begin_ack = None
        # Window flow-control event (see _send_bulk_stream)
        self._flow_ack = None
        # An IEC print job owns the wire: routes NAKs to the job (which
        # aborts it) instead of into the image/SID retry logic
        self._print_active = False
        self._print_refused = False
        # A composed /print is being delivered to SOME backend - the
        # re-entry guard, a superset of _print_active (docs/14 13): the
        # cups backend sends no frames, so it has no wire to own
        self._print_busy = False
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
        # Music the user chose themselves: the LLM stops overriding it
        # until /auto hands control back. The notice is sent once per
        # manual stretch, not every time a directive is ignored.
        self._music_manual = False
        self._manual_notice_sent = False
        # The player silenced the music (/music stop, or the jukebox's
        # stop key). Kept apart from _music_manual so the status chrome
        # can stop naming a tune that is no longer audible.
        self._music_stopped = False
        # One setup tune per trip through the front door
        self._adv_music = False
        self._wire_hold = ''
        # What the player typed this turn, for the map's direction
        # fallback ("n", "go north"). Never load-bearing - it only
        # fills a direction the model did not give (docs/10 section 0).
        self._last_user_text = ''
        self._adv_setup = None
        self._templates = TemplateStore(self.config.data_dir)
        self._img_sent = False
        self._sid_retry = None
        self._claude = None          # ClaudeSession when in code mode
        self._claude_model = None    # remembered Claude Code model

        # SID music library (optional: absent moods.json disables music)
        self.music = MusicLibrary(
            Path(self.config.data_dir) / 'sids' / 'moods.json')
        if self.music.available:
            self.logger.info(
                f"Music library: {len(self.music.tunes)} tunes")

        # Scene illustrations (optional: needs Pillow + a configured
        # backend, or the LLM64_IMG_FIXTURE test hook)
        images_cfg = getattr(self.config, 'images_cfg', {})
        self.images = ImageService(
            Path(self.config.data_dir),
            mode=getattr(self.config, 'images_mode', 'ask'),
            backend=make_backend(images_cfg, self.config.data_dir,
                                 getattr(self.config, 'config_dir', '.')),
            style_prefix=images_cfg.get('style_prefix'))
        if self.images.available:
            self.logger.info(f"Images enabled (backend: "
                             f"{self.images.backend.name}, "
                             f"mode: {self.images.mode})")

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
            elif msg_type == MessageType.DELETE_CONVERSATION:
                await self.handle_delete_conversation()
            elif msg_type == MessageType.STAR_CONVERSATION:
                await self.handle_star_conversation()
            elif msg_type == MessageType.GET_MENU:
                await self.handle_get_menu()
            elif msg_type == MessageType.GET_NOWPLAYING:
                await self.handle_get_nowplaying()
            elif msg_type == MessageType.FAV_TUNE:
                await self.handle_fav_tune()
            elif msg_type == MessageType.SET_BAUD:
                self.handle_set_baud()
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
                    # Transfer-complete ACK: disarm BOTH retry slots
                    # (leaving _img_sent set misrouted the next NAK
                    # into the image branch, suppressing a SID retry)
                    self._sid_retry = None
                    self._img_sent = False
            elif msg_type == MessageType.NAK:
                # A NAK often means the client was still digesting the
                # previous bulk send (post-load rendering backlog) when
                # this transfer arrived - retrying instantly hits the
                # same congestion. Wait it out first, and allow two
                # attempts (seen in the field: back-to-back retries
                # failed identically).
                if self._print_active:
                    # A print NAK is the client refusing the job (no
                    # printer on the bus, or a write error mid-page).
                    # Checked FIRST so it can never be misrouted into
                    # the image/SID retry paths; the job's own wait is
                    # released here and turns this into a status line.
                    self._print_refused = True
                    ev = self._begin_ack or self._flow_ack
                    if ev is not None and not ev.is_set():
                        ev.set()
                elif getattr(self, '_img_sent', False):
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
        # The CLIENT's buffer is the binding limit: an oversized frame
        # desyncs its parser (field bug: 1000-char load messages)
        if len(payload) > 512:
            self.logger.error(
                f"Payload {len(payload)} exceeds client buffer (512) - "
                f"dropping {msg_type.name} frame")
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

        # A re-sent request (client watchdog recovery) must not leave
        # two streams interleaving their CHAT_CHUNK seq counters
        self._cancel_stream()

        if text.startswith('/'):
            await self.handle_command(text)
            return

        # Claude Code mode: route to the CLI session, not the LLM API.
        # A pending tool-permission question is answered by this very
        # message (y/n); anything else is a new instruction.
        if self.mode.name == 'claude':
            self.conv_manager.add_message('user', text)
            self.conv_manager.save()
            if self._claude_pending():
                await self._claude_answer(text)
            else:
                self.stream_task = asyncio.create_task(
                    self._claude_turn(text))
            return

        # A setup in progress owns plain messages until it finishes or
        # is cancelled (/chat). Commands were handled above, so this
        # cannot swallow one.
        if self._adv_setup is not None:
            await self._adv_setup_input(text)
            return

        # Dice macros are rolled HERE, before the model ever sees the
        # message: [roll:1d20] becomes [you rolled 1d20: 14] in the text
        # that gets stored, sent and replied to. Asking a model to roll
        # gets you an invented - and suspiciously generous - number.
        text, rolls = expand_dice(text)
        for r in rolls:
            # The C64 echoed the raw macro locally when it was typed, so
            # the result has to come back or the player never sees what
            # they got. Not the status bar: _stream_response overwrites
            # it with "Contacting API..." a moment later. This lands in
            # the scrollback, where it stays.
            await self._send_bulk(
                MessageType.NOTICE,
                ('* ' + r + ' *').encode('ascii', errors='replace')
                + b'\x00')

        # Add user message to conversation
        self.conv_manager.add_message('user', text)

        # Post-dice-expansion, which is fine: a dice macro never looks
        # like a movement command.
        self._last_user_text = text

        # Start streaming task
        self.stream_task = asyncio.create_task(self._stream_response())

    async def shutdown(self):
        """Release everything bound to this connection (called on close)."""
        self._cancel_stream()
        for t in list(self._media_tasks):
            t.cancel()
        await self._stop_claude()

    def _cancel_stream(self):
        """Quietly cancel any in-flight response stream. Quiet = no
        partial CHAT_DONE: the client has moved on (loading, new
        conversation, or re-sent request), and a stray DONE would flip
        it idle mid-load and disarm its watchdog. The client's explicit
        F3 cancel keeps the loud path (it awaits the DONE)."""
        if self.stream_task and not self.stream_task.done():
            self.stream_task.quiet_cancel = True
            self.stream_task.cancel()

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
                "/code [model] - Claude Code (drive a coding agent)\n"
                "/adventure [theme] - text adventure\n"
                "/chars - list character cards\n"
                "/char <name> - roleplay with a card\n"
                "/assist - talk to the AI assistant\n"
                "/models - list models, /model <name> - switch\n"
                "/music <mood> - play a tune (/music for moods)\n"
                "/auto - let the narrator choose the music again\n"
                "[roll:1d20] in a message - roll dice for real\n"
                "/pic [request|n] - illustrate scene / re-show pic n\n"
                "/pics - list this conversation's pictures\n"
                "/map [n|name] - the map / how to get there\n"
                "/print [what] - hardcopy on the printer\n"
                "/save [name] - checkpoint this conversation\n"
                "/saves - list, /restore <n> - roll back\n"
                "/history [page] - browse the full conversation\n"
                "/find <text> - search this conversation\n"
                "/findall <text> - search all conversations\n"
                "/stats - server statistics\n"
                "/mode - show current mode\n"
                "\n"
                "Keys: F1 menu, F2 new, F3 cancel, F5 conversations,\n"
                "F4/F6 scroll a page up/down, crsr up/down one line.\n"
                "In an adventure, [OOC: ...] talks to the narrator.")

        elif cmd == 'mode':
            await self._send_canned(f"Current mode: {self.mode.label}")

        elif cmd == 'chat':
            if self._adv_setup is not None:
                self._adv_setup = None
                await self._send_canned("Adventure setup cancelled.")
                return
            await self._stop_claude()
            self._switch_mode(Mode(self.config))
            await self._send_hint()   # drop the place from the row
            await self._send_canned("Chat mode. New conversation started.")

        elif cmd in ('code', 'claude'):
            await self._start_claude(arg)

        elif cmd in ('adventure', 'adv'):
            if arg:
                # /adventure <theme> is unchanged - anyone who learned it
                # keeps it, and it is option 2 without the asking.
                await self._start_adventure(arg)
            else:
                self._adv_music = False
                self._adv_setup = AdventureSetup(
                    templates=self._templates.list())
                await self._send_canned(self._adv_setup.opening_screen())

        elif cmd == 'chars':
            cards = self._all_cards()
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

        elif cmd == 'assist':
            # One-keystroke "talk to the AI assistant" from the F1 menu.
            # The wire's command field caps at 10 characters, so
            # '/char Assistant' cannot be a menu entry - this alias can.
            # _start_roleplay -> _switch_mode already opens a fresh
            # conversation, so there is nothing to reset first.
            await self._start_roleplay(arg or 'Assistant')

        elif cmd in ('pic', 'pics'):
            if not self.images.available:
                await self._send_canned("Images not enabled on this server.")
            elif arg.isdigit():
                await self._resend_pic(int(arg))
            elif cmd == 'pics':
                await self._list_pics()
            else:
                # Everything (including the derive LLM call) runs off
                # the reader task: a stalled model must not deafen the
                # proxy to CANCEL/ACK/NAK for minutes. Typed text is a
                # request TO the illustrator; a parked suggestion is the
                # narrator's directive - the composition step treats them
                # differently (docs/13).
                directive = '' if arg else (self.images.pending_prompt or '')
                self.images.pending_prompt = None
                self._spawn_media(self._illustrate(instructions=arg,
                                                   directive=directive))

        elif cmd == 'map':
            await self._show_map(arg)

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
                    + "\nUse: /music <mood>, /music next for another of "
                      "the same,\n/auto to give the soundtrack back to "
                      "the narrator.")
            else:
                # 'next' is a skip, not a takeover: it keeps whatever
                # mood is playing and does NOT claim manual control, so
                # the narrator still gets the soundtrack at the next
                # scene change. (The jukebox panel's 'n' key sends
                # exactly this shape of command.)
                mood = arg.lower()
                if mood in ('stop', 'off', 'silence'):
                    await self._stop_music()
                    return
                if mood == 'next':
                    mood = (self.conv_manager.get_meta('music')
                            or {}).get('mood', '')
                    if not mood:
                        await self._send_canned(
                            "Nothing is playing. /music <mood> starts "
                            "something.")
                        return
                tune = self.music.pick(mood)
                if tune:
                    if arg.lower() != 'next':
                        self._music_manual = True
                        self._manual_notice_sent = False
                    await self._send_canned(
                        f"Playing: {tune['title']} ({tune['author']})")
                    self._spawn_media(self.send_sid(tune))
                    self.conv_manager.set_meta('music', {
                        'mood': mood, 'tune': tune['id']})
                    self.conv_manager.save()
                else:
                    await self._send_canned(
                        f"No tune fits '{arg[:20]}'. /music lists moods.")

        elif cmd == 'auto':
            if self._music_manual:
                self._music_manual = False
                self._manual_notice_sent = False
                self._music_stopped = False
                await self._send_canned(
                    "The narrator picks the music again.")
            else:
                await self._send_canned(
                    "The narrator is already choosing the music.")

        elif cmd == 'models':
            try:
                names = await self.api_client.list_models()
            except Exception:
                names = []
            if names:
                self._model_names = names
                await self._send_canned(
                    "Models:\n"
                    + "\n".join(f"{i}. {n}"
                                for i, n in enumerate(names, 1))
                    + "\nSwitch with: /model <n> or a name prefix")
            else:
                await self._send_canned("Could not fetch the model list.")

        elif cmd == 'model':
            # In Claude Code mode /model targets the CLI's model, which
            # means restarting the session (Claude Code can't hot-swap
            # models mid-session). Elsewhere it's the API chat model.
            if self.mode.name == 'claude':
                if arg:
                    await self._send_canned(
                        f"Restarting Claude Code with {arg}...")
                    await self._start_claude(arg)
                else:
                    await self._send_canned(
                        f"Claude Code model: "
                        f"{self._claude_model or 'default'}. "
                        "/model <opus|sonnet|haiku> to switch.")
            elif arg:
                match = await self._set_model(arg)
                await self._send_canned(f"Now using: {match}")
            else:
                current = self.model_override or self.config.model
                await self._send_canned(f"Current model: {current}")

        elif cmd == 'print':
            await self._print_command(arg)

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
            # Wire-time pacing like every multi-frame send (a flat 50ms
            # under-paced ~300-byte frames into the modem's queue)
            await self._send_bulk(MessageType.MODEL_LIST, bytes(payload))

    async def handle_set_model(self):
        name = self.payload.rstrip(b'\x00').decode('ascii', errors='replace')
        await self.send_ack()
        await self._set_model(name)

    async def _set_model(self, query: str):
        """Resolve a model by /models list number, name prefix, or
        substring, and switch to it."""
        try:
            names = await self.api_client.list_models()
        except Exception:
            names = []
        match = query
        # '/model 2' picks by number from the last /models listing
        # (falling back to the fresh list, same order)
        if query.isdigit():
            pool = getattr(self, '_model_names', None) or names
            n = int(query)
            if 1 <= n <= len(pool):
                match = pool[n - 1]
        else:
            q = query.lower()
            for name in names:
                if name.lower().startswith(q) or q in name.lower():
                    match = name
                    break
        self.model_override = match
        self.logger.info(f"Model -> {match}")
        await self.send_status(f"Model: {match[:32]}")
        return match

    def _attach_snippets(self, mode):
        """Give an AdventureMode the directive instructions for whichever
        media services are live on this server."""
        snippet = ''
        if self.music.available:
            snippet += self.music.prompt_snippet()
        if self.images.available:
            snippet += self.images.prompt_snippet()
        # The map is adventure-only: nothing else has a geography to
        # keep. It needs no server capability either - the graph is
        # built by the proxy from the state block whether or not the
        # model ever emits a MAP directive.
        if getattr(mode, 'name', '') == 'adventure':
            snippet += advmap.prompt_snippet()
        # Colour needs no server capability - the client renders it - so
        # it is unconditional, unlike music and images.
        snippet += color_prompt_snippet()
        mode.music_snippet = snippet

    def _switch_mode(self, mode):
        self.mode = mode
        # A fresh experience gets its soundtrack chosen for it again
        self._music_manual = False
        self._manual_notice_sent = False
        # A parked image suggestion belongs to the old conversation
        self.images.pending_prompt = None
        # ...as does the last thing typed: the kickoff turn has no
        # player command, and a stray one must not fill a map direction
        self._last_user_text = ''
        self.conv_manager.new_conversation()
        # Record the mode on the conversation so loading it later can
        # restore the same experience (prompt, sampling, music directives)
        self.conv_manager.set_meta('mode', mode.name)
        if getattr(mode, 'theme', ''):
            self.conv_manager.set_meta('theme', mode.theme)
        self.logger.info(f"Mode -> {mode.label}")

    def _all_cards(self):
        """Every available card: the user's own folder plus the ones
        bundled with the proxy. A user card of the same name shadows a
        bundled one, so shipping a default 'Assistant' never blocks
        someone from replacing it with their own."""
        cards = find_cards(Path(self.config.cards_dir))
        seen = {name.lower() for name, _ in cards}
        for name, path in find_cards(Path(self.config.default_cards_dir)):
            if name.lower() not in seen:
                cards.append((name, path))
        return sorted(cards, key=lambda c: c[0].lower())

    def _find_card(self, query: str):
        """Match a card by name prefix/substring: (name, path) or None."""
        q = query.lower()
        for name, path in self._all_cards():
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
    # Derived rather than magic: 10 bits per byte over the wire, plus a
    # 15% margin. At the 9600 default this is exactly the 0.0012 that
    # ran for months; config [serial] wire_baud retunes it when the
    # client is rebuilt with BAUD38400.
    BULK_PACE_MARGIN = 1.15

    @property
    def bulk_pace_per_byte(self) -> float:
        baud = self._wire_baud or self.config.wire_baud
        return (10.0 / baud) * self.BULK_PACE_MARGIN

    def handle_set_baud(self):
        """Client announced its wire rate (SET_BAUD): 2 bytes LE, nominal
        baud / 100. Retune this session's bulk pacing to match; leave
        config.wire_baud as the fallback for clients that never send it.
        Fire-and-forget - no reply, matching the client. An out-of-range
        value is ignored rather than trusted."""
        if len(self.payload) < 2:
            self.logger.warning("SET_BAUD: short payload, ignored")
            return
        nominal = (self.payload[0] | (self.payload[1] << 8)) * 100
        if nominal < 1200 or nominal > 115200:
            self.logger.warning(f"SET_BAUD: implausible {nominal}, ignored")
            return
        self._wire_baud = nominal
        self.logger.info(
            f"SET_BAUD: pacing bulk to {nominal} baud "
            f"({self.bulk_pace_per_byte*1000:.3f} ms/byte)")

    async def _send_bulk(self, msg_type: MessageType, payload: bytes):
        """Send one bulk frame and sleep out its wire time."""
        await self.send_message(msg_type, payload)
        await asyncio.sleep(self.BULK_PACE_BASE
                            + len(payload) * self.bulk_pace_per_byte)

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

        def _done(t):
            self._media_tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.logger.error("Media task failed",
                                  exc_info=t.exception())
        task.add_done_callback(_done)

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
        if len(data) > 0x1000:
            self.logger.error(f"SID {tune['id']} is {len(data)} bytes - "
                              "exceeds the $B000 window, not sending")
            return
        name = tune['title'][:24].encode('ascii', errors='replace')
        if not is_retry:
            # Arm BEFORE sending: arming after the lock released raced
            # the completion ACK and could leave a stale retry armed
            self._sid_retry = tune
            self._sid_tries = 2
        async with self._media_lock:
            if not await self._send_begin(MessageType.SID_BEGIN,
                                          head + name + b'\x00'):
                self.logger.error("SID_BEGIN never ACKed - aborting send")
                return
            await self._send_bulk_stream(MessageType.SID_DATA, data)
            await self._send_bulk(MessageType.SID_END, b'')
        self._tunes_sent += 1
        self._music_stopped = False
        self.music.tune_started = time.monotonic()
        await self._send_hint()
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

    async def _ask_model(self, question: str, limit: int = 300,
                         sampling: dict = None) -> str:
        """One-shot utility question to the chat model, plain text back.

        `sampling` overrides the configured generation settings for this
        one call - /print needs a bigger max_tokens than a chat turn
        wants to wait for."""
        out = ""
        try:
            async for kind, chunk in self.api_client.stream_chat(
                    [{'role': 'user', 'content': question}],
                    system_prompt=None, sampling=sampling or {},
                    model=self.model_override):
                if kind != 'reasoning' and chunk:
                    out += chunk
        except Exception as e:
            self.logger.error(f"Utility query failed: {e}")
            return ''
        return out.strip()[:limit]

    async def _derive_scene_prompt(self, instructions: str = '',
                                   directive: str = '') -> str:
        """Compose the illustrator's prompt for the current scene. Every
        /pic and every [[IMAGE:]] directive comes through here so the
        prompt is anchored to game state rather than taken verbatim
        (docs/13). `instructions` is the player's /pic <text>, a request
        TO the illustrator; `directive` is the narrator's suggested shot.
        Reads well back into the transcript, the prompts of earlier
        illustrations, the state block, the current room, and the
        character sheet so recurring people and places keep their look."""
        msgs = self.conv_manager.get_messages()[-12:]
        if not msgs:
            return ''
        convo = "\n".join(f"{m['role']}: {m['content'][:500]}"
                          for m in msgs)
        priors = [p['prompt'] for p in
                  self.conv_manager.get_meta('images', [])[-3:]]
        adv_state = self.conv_manager.get_meta('adv_state')
        # The room the player is standing in, whose note carries props
        # ("an iron key on a hook") the scene should include.
        room = None
        m = self.conv_manager.get_meta('adv_map')
        if m and m.get('at') is not None:
            room = m.get('rooms', {}).get(m['at'])
        character = getattr(self.mode, 'character', '')
        question = compose_question(
            convo, priors, adv_state, room, character,
            instructions=instructions, directive=directive)
        return await self._ask_model(question, limit=400)

    async def _make_caption(self, prompt: str) -> str:
        """A short atmospheric caption, burned into the picture and
        echoed into the chat. Falls back to the prompt itself."""
        out = await self._ask_model(
            "Write ONE short atmospheric caption (at most 10 words, no "
            "quotation marks) for this adventure-game illustration, "
            "addressed to the player. Reply with only the caption. "
            "Scene: " + prompt, limit=100)
        return out.strip('"').strip() or prompt[:60]

    # --- hardcopy: /print (docs/14) ------------------------------------

    # 240 bytes fits the 512-byte buffers with room to spare and is ~4s
    # of paper on a 60cps MPS-803 - well inside the client's ~43s
    # watchdog, which its per-block ACK also keeps resetting.
    PRINT_BLOCK = 240
    PRINT_ACK_TIMEOUT = 30.0

    async def _print_command(self, arg: str):
        """/print [what]: compose a document and put it on paper.

        Three sources, cheapest first (printdoc.py): a bare /print
        reflows the last reply, an argument naming the character sheet
        renders it from stored state, anything else asks the model to
        extract the document from the conversation."""
        arg = (arg or '').strip()
        if self._print_busy:
            await self._send_canned("A print job is already running.")
            return

        msgs = self.conv_manager.get_messages()
        adv_state = self.conv_manager.get_meta('adv_state')
        title, body = '', ''
        if not arg:
            # No title: a chat reply's first line is prose, not a heading
            body = printdoc.last_reply(msgs)
        elif printdoc.wants_sheet(arg) and adv_state:
            title = 'Character sheet'
            body = printdoc.render_sheet(
                adv_state, getattr(self.mode, 'character', ''),
                getattr(self.mode, 'background', ''))
        elif msgs:
            await self.send_status("Composing the document...")
            # 4000 chars a message, not the 800 the scene prompt uses:
            # the recipe being asked for IS one of these messages, and
            # clipping it at 800 (about 10 printed lines) loses the tail
            # of the document before the model can even see it.
            convo = "\n".join(f"{m['role']}: {m['content'][:4000]}"
                              for m in msgs[-12:])
            # limit must stay clear of printer_max_tokens or it would
            # silently behead a page the model finished properly.
            title, body = printdoc.split_title(await self._ask_model(
                printdoc.compose_question(arg, convo), limit=12000,
                sampling={'max_tokens': self.config.printer_max_tokens}))

        doc = printdoc.finish(title, body, self.config.printer_width)
        if not doc:
            await self._send_canned("Nothing to print.")
            return
        # Off the reader task: the job waits on ACKs only the reader can
        # dispatch (see _media_tasks)
        self._spawn_media(self._print_job(doc))

    async def _print_job(self, doc: str):
        """Deliver one composed document to every configured backend
        (docs/14 13): the C64's IEC printer, a CUPS queue on the proxy
        side, or both.

        The two deliveries run one after the other rather than as
        parallel tasks. While the IEC job runs the client masks its
        serial RX for every write, so the wire has to stay silent (13 3)
        - a STATUS from a concurrent cups task would be dropped, or
        worse, land inside a PRINT_DATA frame. Going first also makes
        "cups status before the IEC one" exact instead of a race, and lp
        only spools (sub-second), so the paper leg barely delays the C64
        leg. Neither leg's failure stops the other: _print_cups reports
        its own outcome and returns.

        _print_busy (this whole job, either backend) is deliberately not
        _print_active (an IEC job owns the wire, which is what routes a
        NAK into the print path)."""
        backend = self.config.printer_backend
        self._print_busy = True
        try:
            if backend in ('cups', 'both'):
                await self._print_cups(doc)
            if backend in ('c64', 'both'):
                await self._send_print(doc)
        finally:
            self._print_busy = False

    async def _print_cups(self, doc: str):
        """The paper-printer leg (docs/14 13): the composed document into
        lp, no PRINT frames, the C64 uninvolved. Short line on the C64,
        the full reason in the log - the person who can fix a CUPS queue
        is at the proxy, not at the C64.

        The outcome goes out as a canned REPLY, not a STATUS. /print is
        an answer to something the user typed, so the client is sitting
        in ST_WAITING; the IEC leg leaves that state by taking
        ST_LOADING for the job (12), but this leg sends no frames at all
        and a STATUS is not an end-of-reply - the client waited out its
        timeout and reported the message lost while the page was already
        spooled (caught by make test-emu-print-cups). A canned reply
        carries CHAT_DONE, which ends the turn, and leaves the outcome in
        the transcript instead of a status row that scrolls away."""
        queue = self.config.printer_cups_queue
        res = await printcups.send(
            doc, queue,
            server=self.config.printer_cups_server,
            options=self.config.printer_cups_options)
        if res.ok:
            await self._send_canned("Sent to the paper printer.")
            self.logger.info(
                f"Spooled {len(doc)} chars to CUPS queue {queue!r}"
                + (f": {res.detail}" if res.detail else ''))
        else:
            await self._send_canned(f"Paper print failed: {res.reason}")
            self.logger.error(f"CUPS print to {queue!r} failed: {res.detail}")

    async def _send_print(self, doc: str):
        """Drive one print job: BEGIN, a block at a time, END. Strictly
        one frame in flight - the client masks serial RX for every IEC
        write, so the wire must be silent while it prints (docs/14 3)."""
        data = doc.encode('ascii', 'replace')
        blocks = printdoc.blocks(data, self.PRINT_BLOCK)
        # bit0: business charset (SA 7), so mixed case prints as chatted
        flags = 0x01 | (0x02 if self.config.printer_formfeed else 0)
        head = bytes([flags, min(len(blocks), 255)])
        self._print_active = True
        self._print_refused = False
        try:
            async with self._media_lock:
                if not await self._send_begin(MessageType.PRINT_BEGIN,
                                              head):
                    await self.send_status("Printer not responding.")
                    return
                if self._print_refused:
                    await self.send_status("No printer on the bus (dev 4).")
                    return
                for b in blocks:
                    if not await self._print_block(MessageType.PRINT_DATA,
                                                   b):
                        return
                if await self._print_block(MessageType.PRINT_END, b''):
                    await self.send_status(
                        f"Printed {doc.count(chr(10))} lines.")
                    self.logger.info(
                        f"Printed {len(data)} bytes in {len(blocks)} blocks")
        finally:
            self._print_active = False

    async def _print_block(self, msg_type: MessageType,
                           payload: bytes) -> bool:
        """One frame, then silence until the client ACKs it. A timeout
        or a NAK ABORTS the job rather than continuing (the bulk
        stream's warn-and-continue would transmit into a client that is
        printing with its serial RX masked)."""
        self._flow_ack = asyncio.Event()
        try:
            await self._send_bulk(msg_type, payload)
            await asyncio.wait_for(self._flow_ack.wait(),
                                   self.PRINT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.warning(f"{msg_type.name} not ACKed - job aborted")
            await self.send_status("Printer stalled - job cancelled.")
            return False
        finally:
            self._flow_ack = None
        if self._print_refused:
            await self.send_status("Printer error - job cancelled.")
            return False
        return True

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

    # The map is drawn into the SCROLLBACK rather than over the screen:
    # a map is a reference you glance at, not an event you stage. 78
    # columns, not 79 - the colour tag each line opens with takes a
    # column of its own (docs/10 section 5.3).
    MAP_WIDTH = 78

    async def _show_map(self, arg: str):
        """/map - the graph the proxy has been keeping (docs/10)."""
        if self.mode.name != 'adventure':
            await self._send_canned(
                "The map only exists in adventure mode.")
            return
        m = self.conv_manager.get_meta('adv_map') or {}
        if not m.get('rooms'):
            await self._send_canned(
                "No map yet - the story has not moved you anywhere.")
            return
        if arg:
            # No model call: the cheapest correct answer in the design.
            await self._send_canned(self._map_route(m, arg))
            return
        lines = advmap.render_ascii(m, width=self.MAP_WIDTH)
        # Every line opens with a colour tag. It becomes a marker cell,
        # so cur_len > 0 by the time the first space of the art arrives
        # and the indentation survives (the client drops a leading space
        # otherwise). The run carries across line breaks, so it must be
        # closed once at the end or it tints the rest of the chat.
        await self._send_canned(
            "\n".join("[color=cyan]" + ln for ln in lines) + "[/color]")

    @staticmethod
    def _map_route(m, arg: str) -> str:
        dest = advmap.find_room(m, arg)
        if not dest:
            return (f'No place like "{arg[:30]}" on the map. '
                    "/map lists them.")
        name = m['rooms'][dest]['name']
        if dest == m.get('at'):
            return f"You are already at {name}."
        steps = advmap.route(m, dest)
        if steps is None:
            return f"{name}: the map knows no way there from here."
        return "%s: %s. (%d place%s away.)" % (
            name, ", then ".join(steps), len(steps),
            '' if len(steps) == 1 else 's')

    async def _list_pics(self):
        """This conversation's generated pictures, newest first."""
        pics = self.conv_manager.get_meta('images', [])
        if not pics:
            await self._send_canned("No pictures in this conversation "
                                    "yet. /pic makes one.")
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
        path = self.images.blob_path(pics[n - 1]['stem'])
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

    async def _illustrate(self, instructions: str = '',
                          directive: str = ''):
        """Full /pic flow off the reader task: compose the scene prompt
        (always - from the transcript and game state, steered by the
        player's request or the narrator's directive, docs/13), announce
        it, then generate + send. The heartbeat covers the compose call
        for every trigger, not just the bare one."""
        await self.send_status("Studying the scene...")
        hb = asyncio.create_task(self._heartbeat("Studying..."))
        try:
            prompt = await self._derive_scene_prompt(
                instructions=instructions, directive=directive)
        finally:
            hb.cancel()
        if not prompt:
            await self._send_canned(
                "Couldn't picture the scene - try /pic <description>.")
            return
        await self._send_hint(0)
        # Complete the chat round-trip first: the client must return
        # to idle before the image transfer starts
        await self._send_canned(f"Illustrating: {prompt[:300]}")
        await self._generate_and_send_image(prompt, instructions=instructions,
                                            directive=directive)

    async def _generate_and_send_image(self, prompt: str,
                                       instructions: str = '',
                                       directive: str = ''):
        await self.send_status("Illustrating... (10-20s)")
        hb = asyncio.create_task(self._heartbeat("Illustrating..."))
        try:
            caption = await self._make_caption(prompt)
            # The trigger context rides along into the JSON sidecar so a
            # later playtest can compare request against composed scene.
            meta = {'instructions': instructions, 'directive': directive,
                    'caption': caption[:100],
                    'conv_id': str(self.conv_manager.current_id),
                    'at_msg': len(self.conv_manager.get_messages())}
            blob, stem, bg = await self.images.generate_blob(
                prompt, self.conv_manager.current_id, caption, meta=meta)
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
        await self._send_hint(0)      # the tally just went up
        # The caption lands in the scrollback before the screen freezes,
        # so it's also the last chat line when the picture is dismissed
        await self._send_canned(f'"{caption}"')
        await self.send_image_blob(blob, bg)

    async def _send_text(self, seq: int, text: str) -> int:
        """Every path that streams prose to the C64 goes through here.

        This is where colour markup becomes in-band marker cells
        (docs/08-inline-color.md): tags stay in stored history and in the
        model's context, and only the client-bound bytes carry markers.
        Splitting the result into frames is safe at any offset - a marker
        is a single byte and the client consumes the stream in order.
        """
        # Hold back any tail that could still become markup - a partial
        # tag, an open **bold**, or a space an incoming tag will swallow.
        # The transform works on whole strings; a stream hands it slices,
        # and markup cut across one matches nothing and prints literally.
        emit, self._wire_hold = split_safe(
            self._wire_hold + text.translate(UNICODE_TO_ASCII))
        data = colorize_for_wire(emit)
        for i in range(0, len(data), self.CHUNK_TEXT_MAX):
            seq = await self._send_text_chunk(
                seq, data[i:i + self.CHUNK_TEXT_MAX])
        return seq

    async def _flush_text(self, seq: int) -> int:
        """Emit whatever split_safe held back. Must run before CHAT_DONE
        or a reply ending mid-markup loses its tail."""
        if not self._wire_hold:
            return seq
        held, self._wire_hold = self._wire_hold, ''
        data = colorize_for_wire(held)
        for i in range(0, len(data), self.CHUNK_TEXT_MAX):
            seq = await self._send_text_chunk(
                seq, data[i:i + self.CHUNK_TEXT_MAX])
        return seq

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

    # Heartbeats keep the client's ~43s response watchdog fed during a
    # long silence. The cap must OUTLIVE the API's own read timeout
    # (api_client, 600s) or the C64 gives up first and the real error -
    # the one that would say what went wrong - never arrives. A slow GPU
    # doing a cold prompt-eval on a long conversation is minutes.
    HEARTBEAT_BEATS = 63

    async def _heartbeat(self, label: str, interval: float = 10.0,
                         beats: int = HEARTBEAT_BEATS):
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

    # The client reserves 40 columns for it, right-aligned before the
    # !P/tally corner (c64_client/include/ui.h, UI_CHROME_MAX).
    CHROME_MAX = 40
    NOTE = '\x7f'          # the 4x8 music note (tools/make_font.py)

    def _chrome(self) -> str:
        """The right-hand status row, composed HERE so its contents can
        change without touching the client: where you are, and what is
        playing. Place first - it is the thing you look at between
        turns; the tune is the thing you glance at once."""
        parts = []
        if self.mode.name == 'adventure':
            m = self.conv_manager.get_meta('adv_map') or {}
            room = (m.get('rooms') or {}).get(m.get('at') or '')
            if room:
                parts.append(room['name'][:24])
        tune, _mood = self._current_tune()
        if tune and not self._music_stopped:
            parts.append(self.NOTE + tune['title'][:20])
        line = "  ".join(parts)
        return line[:self.CHROME_MAX]

    async def _send_hint(self, pending: int = None):
        """Status-row indicators: '!P' when a scene is waiting, the
        running picture count, and the composed chrome. One frame
        carries all three so they can never disagree, and the count is
        read from meta rather than tracked separately - a loaded
        conversation then shows its own tally.

        `pending` defaults to whatever is actually parked, so a call
        made only to refresh the chrome cannot silently clear a waiting
        scene suggestion."""
        if pending is None:
            pending = 1 if self.images.pending_prompt else 0
        pics = len(self.conv_manager.get_meta('images', []) or [])
        payload = bytes([pending & 1, min(pics, 255)])
        payload += self._chrome().encode('ascii', errors='replace') + b'\x00'
        await self.send_message(MessageType.HINT, payload)

    async def _send_notice(self, text: str):
        """An out-of-band system line in the scrollback (the dice path's
        message type). Unlike _send_canned it sends no CHAT_DONE, so it
        cannot disturb the reply state machine - but the client IGNORES
        it while streaming, so only send it once a reply has finished."""
        await self._send_bulk(
            MessageType.NOTICE,
            text.encode('ascii', errors='replace')[:400] + b'\x00')

    async def _send_canned(self, text: str):
        """Stream local text to the C64 as a normal reply (no API call)."""
        seq = await self._send_text(0, text)
        seq = await self._flush_text(seq)
        await self.send_message(MessageType.CHAT_DONE,
                                struct.pack('<BH', seq, len(text)))

    # --- Claude Code mode ---------------------------------------------

    # Claude Code has its own model namespace, distinct from the API
    # chat model (/model). Aliases the CLI accepts; a full model id is
    # also allowed and passed through.
    CLAUDE_MODELS = ('opus', 'sonnet', 'haiku')

    async def _start_claude(self, model_arg: str = ''):
        """Enter Claude Code mode: spin up the CLI session. Optional
        model alias (opus/sonnet/haiku) or full id; else the config
        default, else the CLI's own default."""
        model = (model_arg.strip() or self._claude_model
                 or self.config.claude_model or None)
        self._claude_model = model
        await self._stop_claude()
        self._switch_mode(ClaudeMode(self.config))
        try:
            self._claude = ClaudeSession(
                self.config.claude_command,
                self.config.claude_workdir,
                model=model)
            await self._claude.start()
        except Exception as e:
            self.logger.error(f"Claude Code start failed: {e}")
            self._claude = None
            self._switch_mode(Mode(self.config))
            await self._send_canned(
                "Couldn't start Claude Code on the server.")
            return
        await self._send_canned(
            f"Claude Code ready ({model or 'default model'}). Tell me "
            "what to build; I'll ask before running tools (reply y or "
            "n). /model <opus|sonnet|haiku> switches, /chat exits.\n"
            f"Working in: {self.config.claude_workdir}")

    async def _stop_claude(self):
        if self._claude:
            await self._claude.stop()
            self._claude = None

    def _claude_pending(self):
        return bool(self._claude and self._claude.pending_permission)

    async def _claude_answer(self, text: str):
        """Resolve a parked tool-permission question with this message."""
        rid = self._claude.pending_permission
        allow = text.strip().lower()[:1] in ('y', 'a', 'o')  # y/allow/ok
        await self._claude.resolve_permission(rid, allow)
        await self.send_status("Approved." if allow else "Denied.")
        # Keep rendering the same turn's remaining events
        self.stream_task = asyncio.create_task(self._claude_drain())

    async def _claude_turn(self, text: str):
        await self._claude.send_user_turn(text)
        await self._claude_drain()

    async def _claude_drain(self):
        """Render the session's event stream until the turn ends or a
        permission question needs the user."""
        seq = 0
        hb = asyncio.create_task(self._heartbeat("Working..."))
        try:
            async for ev in self._claude.events():
                kind = ev.get("kind")
                if kind == "text":
                    seq = await self._send_text(seq, ev["text"])
                elif kind == "tool":
                    line = self._describe_tool(ev["name"], ev["input"])
                    seq = await self._send_text(seq, "\n> " + line + "\n")
                elif kind == "permission":
                    q = (f"Allow {ev['name']}"
                         f"{' ' + ev['description'] if ev['description'] else ''}"
                         "? (y/n)")
                    seq = await self._send_text(seq, "\n" + q)
                    seq = await self._flush_text(seq)
                    await self.send_message(
                        MessageType.CHAT_DONE,
                        struct.pack('<BH', seq, 0))
                    return  # wait for the user's y/n
                elif kind == "result":
                    break
                elif kind == "exit":
                    await self._send_canned(
                        "Claude Code session ended. /code to restart.")
                    self._claude = None
                    self._switch_mode(Mode(self.config))
                    return
            await self.send_message(MessageType.CHAT_DONE,
                                    struct.pack('<BH', seq, 0))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"Claude drain error: {e}")
            await self.send_message(MessageType.CHAT_DONE,
                                    struct.pack('<BH', seq, 0))
        finally:
            hb.cancel()

    @staticmethod
    def _describe_tool(name, inp):
        """A one-line, C64-friendly summary of a tool call."""
        if name in ('Bash',) and 'command' in inp:
            return f"Bash: {inp['command'][:60]}"
        for k in ('file_path', 'path', 'pattern', 'url', 'query'):
            if k in inp:
                return f"{name}: {str(inp[k])[:60]}"
        return name

    def _ingest_map(self, directives):
        """One reply's worth of movement, folded into adv_map."""
        # Read the location back from META rather than from the local
        # `state`: that name only exists on a turn that carried a state
        # block, and meta is the value that actually survived
        # validation. The consequence is right and free - on a turn
        # whose state block was dropped, `location` is last turn's
        # value, so ingest correctly sees no move.
        loc = None
        try:
            loc = (json.loads(
                self.conv_manager.get_meta('adv_state') or '{}')
                or {}).get('location')
        except (ValueError, TypeError):
            pass
        m = self.conv_manager.get_meta('adv_map') or advmap.new_map()
        for line in advmap.ingest(m, location=loc, directives=directives,
                                  player_text=self._last_user_text):
            self.logger.info("map: %s", line)
        self.conv_manager.set_meta('adv_map', m)
        self.conv_manager.save()

    async def _stream_response(self, hidden_user_msg: str = None):
        """Stream API response to C64"""
        hb = None
        # Bound before any await: a cancel landing on the first status
        # send must not NameError in the CancelledError handler
        seq = 0
        full_response = ""
        done_sent = False
        try:
            await self.send_status("Contacting API...")

            messages = self.conv_manager.get_messages()
            if hidden_user_msg:
                # Mode kickoff: sent to the API but not persisted
                messages = messages + [
                    {'role': 'user', 'content': hidden_user_msg}]

            # In adventure/roleplay the model may emit directives
            # ([[MUSIC:]], [[IMAGE:]], [[STATE:]]); strip them from what
            # the C64 sees (and from saved history) and act on them
            # after the response completes. Always on for these modes -
            # the state block flows even with music/images disabled.
            # Always on, in every mode. It was adventure/roleplay only
            # while its whole job was directives, but it is also what
            # holds back markup split across SSE chunk boundaries - and
            # colour tags can appear in any mode. Without it a tag cut in
            # half by the API's chunking matches nothing and both halves
            # print literally. Directives simply never appear in chat, so
            # extracting them there costs nothing.
            mfilter = MusicDirectiveFilter()

            # Nudge, don't nag: after ~5 minutes of one tune looping,
            # remind the narrator it owns the soundtrack
            sys_prompt = self.mode.system_prompt()

            # Re-inject the adventure's authoritative state each turn:
            # it survives even when early messages fall out of the
            # context window, so stats/inventory/appearance stay
            # consistent across a long game.
            if self.mode.name == 'adventure' and sys_prompt:
                adv_state = self.conv_manager.get_meta('adv_state')
                if adv_state:
                    sys_prompt += (
                        "\n\nAUTHORITATIVE GAME STATE from your previous "
                        "turn - trust it over your reading of the "
                        "transcript, and update it in this reply's "
                        "[[STATE: ...]] block: " + adv_state)
                # The map, restated: the model never has to FIND the
                # current node in a graph, it is told, with its exits and
                # its routes. Whatever it believed last turn is silently
                # overwritten by the truth this turn. After adv_state,
                # because everything that changes must come after
                # everything that does not (llama.cpp prefix cache).
                block = advmap.prompt_block(
                    self.conv_manager.get_meta('adv_map') or {})
                if block:
                    sys_prompt += "\n\n" + block
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
                    seq = await self._send_text(seq, chunk)

            if mfilter:
                tail = mfilter.flush()
                if tail:
                    full_response += tail
                    seq = await self._send_text(seq, tail)

            seq = await self._flush_text(seq)

            # Send completion
            payload = struct.pack('<BH', seq,
                                  min(len(full_response), 0xFFFF))
            await self.send_message(MessageType.CHAT_DONE, payload)
            done_sent = True

            # Save assistant response to conversation
            self.conv_manager.add_message('assistant', full_response)
            self.conv_manager.save()

            # Adventure state block: persist the newest one in meta
            # (normalized if it parses; kept raw otherwise - still
            # useful as context next turn)
            if mfilter and mfilter.states:
                state = mfilter.states[-1].strip()
                try:
                    state = json.dumps(json.loads(state),
                                       separators=(',', ':'))
                except (ValueError, TypeError):
                    # Do NOT store it. adv_state is re-injected into the
                    # system prompt every turn as authoritative, so
                    # keeping malformed JSON teaches the model that
                    # malformed JSON is acceptable and the damage
                    # compounds. A slightly stale but valid state is
                    # strictly better. (Field: a block arrived with
                    # "companions:[] - one missing quote.)
                    self.logger.warning(
                        "STATE block is not valid JSON, keeping the "
                        "previous state: %.120s", state)
                    state = None
                if state is not None:
                    self.conv_manager.set_meta('adv_state', state)
                    self.conv_manager.save()

            # Fold this reply into the map (docs/10). Both signals are in
            # hand: the filter has every directive from the whole stream
            # and the state block has been parsed and validated.
            if self.mode.name == 'adventure' and mfilter:
                self._ingest_map(mfilter.maps)
                # The place in the status row is only as current as the
                # last ingest, so refresh it in the same breath.
                await self._send_hint()

            self.logger.info(f"Response complete: {len(full_response)} bytes")

            # Model requested a scene illustration: generate now (auto,
            # rate-limited) or park it as a /pic suggestion (ask)
            if mfilter and mfilter.images and self.images.available:
                directive = mfilter.images[0]
                if self.images.auto_ok():
                    self.images.mark_auto()
                    # Compose from state rather than firing the directive
                    # verbatim - the narrator's text is a suggestion for
                    # the shot (docs/13).
                    await self._illustrate(directive=directive)
                elif self.images.mode == 'ask':
                    self.images.pending_prompt = directive
                    await self.send_status(
                        "Scene available - /pic to illustrate")
                    await self._send_hint(1)

            # Model asked for a music change: honor at most one, after the
            # text is fully delivered (the client is idle again), unless a
            # change happened too recently
            if mfilter and mfilter.moods:
                if self._music_manual:
                    # Stay in the fiction rather than posting a mode
                    # banner, and say it once - a reminder every turn
                    # would be worse than the override it replaced.
                    self.logger.info(
                        f"Music directive ignored (manual): {mfilter.moods}")
                    if not self._manual_notice_sent:
                        self._manual_notice_sent = True
                        await self._send_canned(
                            "(The scene calls for different music, but "
                            "you have chosen your own. /auto gives the "
                            "soundtrack back to the narrator.)")
                elif self.music.rate_limited():
                    self.logger.info(
                        f"Music directive rate-limited: {mfilter.moods}")
                else:
                    tune = self.music.pick(mfilter.moods[0])
                    if tune:
                        # Say WHY the music changed. Until now a tune
                        # simply appeared, with the title flashing past
                        # in the status row; the scrollback keeps this.
                        await self._send_notice(
                            f"The narrator chose \"{tune['title']}\" by "
                            f"{tune['author']} to fit the mood: "
                            f"{mfilter.moods[0]}.\n"
                            "(/music next for another, /music <mood> to "
                            "choose your own.)")
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
            # A cancel landing in the post-DONE media phase must not
            # re-send DONE or double-save the assistant message; a
            # quiet cancel (client moved on) sends nothing at all
            if not done_sent and not getattr(
                    asyncio.current_task(), 'quiet_cancel', False):
                payload = struct.pack('<BH', seq,
                                      min(len(full_response), 0xFFFF))
                await self.send_message(MessageType.CHAT_DONE, payload)
                if full_response:
                    self.conv_manager.add_message('assistant',
                                                  full_response)
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

    # One page of the conversation manager's list; also the cap for the
    # legacy no-payload request (the client stores 17 entries max)
    LIST_PAGE = 16

    def _resolve_masked_id(self, masked_id: int) -> int:
        """The wire id is 32-bit; stored ids may be wider (ms
        timestamps), so resolve by masked comparison."""
        for conv in self.conv_manager.list_conversations():
            if int(conv['id']) & 0xFFFFFFFF == masked_id:
                return int(conv['id'])
        return masked_id

    async def handle_list_conversations(self):
        """Send one page of the conversation list to the C64. Optional
        payload byte = page number (0-based; absent = page 0, which
        keeps old clients working). Starred conversations sort first
        and get a '*' title prefix - the client renders titles as-is,
        so starring costs zero client bytes."""
        page = self.payload[0] if len(self.payload) >= 1 else 0
        self.logger.info(f"List conversations request (page {page})")
        await self.send_ack()

        conversations = self.conv_manager.list_conversations()
        for conv in conversations:
            if conv.get('starred'):
                conv['title'] = '*' + str(conv['title'])
        start = page * self.LIST_PAGE
        window = conversations[start:start + self.LIST_PAGE]
        more_pages = 1 if len(conversations) > start + self.LIST_PAGE else 0
        self.logger.info(f"Found {len(conversations)} conversations, "
                         f"sending {len(window)}")

        # Zero frames would leave the browser at 'loading...' forever
        # (fresh install or past-the-end page): an explicit empty frame
        if not window:
            await self.send_message(MessageType.CONVERSATION_LIST,
                                    bytes([0, 0]))
            return

        # Send in chunks (max 5 per message to keep under size limit).
        # 'more' is a bitfield: bit0 = more frames in this response,
        # bit1 = more pages exist beyond this one.
        chunk_size = 5
        for i in range(0, len(window), chunk_size):
            chunk = window[i:i+chunk_size]
            more = 1 if (i + chunk_size) < len(window) else (more_pages << 1)

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

    def _menu_entries(self):
        """The F1 menu, mode-aware. (key, label, command) triples;
        commands starting with '!' run CLIENT-side (config editor,
        disk copy, ...) - the server still owns their labels, so the
        menu has exactly one source of truth. New slash commands
        added here appear on the C64 with zero client bytes."""
        mode = self.mode.name
        entries = [
            ('n', 'New conversation', '!n'),
            ('c', 'Conversations...', '!c'),
        ]
        if mode == 'claude':
            entries.append(('q', 'Leave code mode', '/chat'))
        elif mode in ('adventure', 'roleplay'):
            entries += [
                ('p', 'Picture of this scene', '/pic'),
                ('v', 'Past pictures', '/pics'),
                ('t', 'Save checkpoint', '/save'),
                ('q', 'Back to chat mode', '/chat'),
                # The panel caps at 13 and this branch already fills it,
                # so the map costs the /models entry - in ADVENTURE
                # only, the one mode that has a geography. Switching
                # models mid-adventure is rare (and /models is still
                # typeable); a map is something you reach for
                # constantly. Roleplay keeps Models: /map there would
                # only ever answer "not in this mode", and the e2e
                # drives the model list through this very key.
                ('m', 'Map', '/map') if mode == 'adventure'
                else ('m', 'Models', '/models'),
            ]
        else:
            # The two quick-starts lead: "new conversation, then type
            # /adventure" was the buried first-run flow. Both commands
            # switch mode AND open a fresh conversation (_switch_mode),
            # so one keystroke is genuinely the whole journey. Labels cap
            # at 26 chars and commands at 10 - '/char Assistant' would
            # not fit the wire, which is why /assist exists.
            entries += [
                ('a', 'Start an adventure', '/adventure'),
                # 'i' rather than the more obvious 't': 't' is already
                # "Save checkpoint" in adventure/roleplay mode. The two
                # never render together, but a key that means different
                # things in different modes would eventually cost
                # someone a conversation, since this one starts a new.
                ('i', 'Talk to the AI assistant', '/assist'),
                ('r', 'Roleplay characters', '/chars'),
                ('m', 'Models', '/models'),
            ]
        if self.music.available:
            entries.append(('j', 'Jukebox / now playing', '!j'))
        # No 'Music: next / stop' entry. It was the only way to stop a
        # streamed SID, so removing it needed somewhere else for stop to
        # live first: the jukebox panel's own 's' key and /music stop
        # (both land on MUSIC_STOP). Two music entries side by side read
        # as two different features, which they were not.
        entries += [
            ('x', 'Cancel reply', '!x'),
            ('e', 'Server config', '!e'),
            ('d', 'Copy client disk', '!d'),
            ('h', 'Help', '/help'),
        ]
        return entries[:13]  # the client panel caps at MAX_MENU

    async def handle_get_menu(self):
        """Send the server-fed menu: [count][more] then
        [key][label\\0][cmd\\0] per entry, single frame."""
        payload = bytearray([0, 0])
        count = 0
        for key, label, cmd in self._menu_entries():
            rec = bytearray()
            rec.append(ord(key))
            rec.extend(label[:26].encode('ascii', errors='replace'))
            rec.append(0)
            rec.extend(cmd[:10].encode('ascii', errors='replace'))
            rec.append(0)
            if len(payload) + len(rec) > 500:  # client MAX_PAYLOAD 512
                self.logger.warning("Menu truncated at %d entries", count)
                break
            payload.extend(rec)
            count += 1
        payload[0] = count
        await self._send_bulk(MessageType.MENU_LIST, bytes(payload))

    # One thinking-enabled call, once per adventure. Needs a real budget:
    # thinking emits reasoning BEFORE content, so a small max_tokens is
    # spent entirely on reasoning and the answer never lands (measured,
    # docs/09-adventure-setup.md section 1).
    PREP_MAX_TOKENS = 3000

    PREP_SYSTEM = (
        "You are a game master preparing a short text adventure before "
        "the first scene. Think it through, then answer with the prep "
        "notes only - no preamble, no headings longer than a few words. "
        "Be concrete and brief: a paragraph of setting, 3-5 named "
        "locations with how they connect, 2-3 people worth meeting, one "
        "secret the player could uncover, and the situation the player "
        "opens in. This is YOUR notes, never shown to the player. "
        "Finish with a machine-readable index of the geography: a line "
        "reading exactly MAP: and then one line per place, in the form "
        "- The Flooded Nave | n: The Choir Stair | e: The Salt Cloister "
        "- using only the directions n s e w ne nw se sw u d in out.")

    async def _prep_world(self, bundle: dict, character: str) -> str:
        """The 'DM prepares a campaign' pass. Returns the bible, or ''
        if anything goes wrong - a failed prep must degrade to an
        ordinary adventure, never block one."""
        asked = "\n".join(f"{k}: {v}" for k, v in bundle.items()
                           if k not in ('scores', 'skills', 'spells'))
        ask = "Prepare an adventure.\n" + (asked or "No preferences given.")
        if character:
            ask += "\n\nThe player character: " + character
        text = ''
        try:
            async for kind, chunk in self.api_client.stream_chat(
                    [{'role': 'user', 'content': ask}],
                    system_prompt=self.PREP_SYSTEM,
                    sampling={'max_tokens': self.PREP_MAX_TOKENS},
                    think=True):
                if kind != 'reasoning':
                    text += chunk
        except Exception as e:
            self.logger.warning("World prep failed (%s); starting without "
                                "it", type(e).__name__)
            return ''
        return text.strip()

    @staticmethod
    def _theme_from(bundle: dict) -> str:
        labels = {st['key']: st['label'] for st in STAGES}
        return ". ".join(f"{labels[k]}: {v}" for k, v in bundle.items()
                         if k in ('world', 'tone', 'opening'))

    @staticmethod
    def _background(bible: str, character: str) -> str:
        """Prep notes and character sheet, both STABLE for the life of
        the adventure - so they ride the cached head of the system
        prompt rather than the per-turn append."""
        return "\n\n".join(x for x in (
            ("Your prep notes for this adventure:\n" + bible)
            if bible else '', character) if x)

    async def _start_adventure(self, theme: str, background: str = '',
                               character: str = ''):
        """Shared by /adventure <theme> and the setup flow, so both take
        exactly the same path into play."""
        mode = AdventureMode(self.config, theme=theme)
        mode.background = background
        # The character sheet is a slice of `background`; keep it whole so
        # the illustrator gets the player's visual identity (docs/13).
        mode.character = character
        self._attach_snippets(mode)
        self._switch_mode(mode)
        # Seed the map from the prep notes' MAP: section, so the first
        # /map is not empty and - more valuable - the model is anchored
        # to place names it already committed to. Best-effort: a bad
        # parse must never block an adventure starting.
        if background:
            seeded = advmap.new_map()
            n = advmap.seed_from_notes(seeded, background)
            if n:
                self.conv_manager.set_meta('adv_map', seeded)
                self.logger.info("map: seeded %d places from the prep "
                                 "notes", n)
        # How to play, once, before the first scene. Everything here is
        # otherwise only discoverable through /help, which a player in
        # the middle of a story has no reason to type.
        await self._send_notice(
            "How to play: say what you want to do, in your own words -"
            " LOOK, TAKE THE LAMP, or just \"ask her about the gate\"."
            "\n/map draws the map as you explore it (/map <n> routes you"
            " back). /pic illustrates this scene, /pics lists the ones"
            " you have. /save makes a checkpoint. F1 is the menu.")
        await self.send_status("Generating your adventure...")
        self.stream_task = asyncio.create_task(
            self._stream_response(hidden_user_msg=self.mode.kickoff()))

    # Character creation is several minutes of menus before a word of
    # story, and silence makes it feel like filling in a form.
    # 'adventurous' first - the mood of setting out - with fallbacks in
    # case a library has that bucket empty.
    SETUP_MOODS = ('adventurous', 'heroic', 'mysterious', 'serene')

    async def _setup_music(self):
        """A tune for the character-creation stages. Once per setup, and
        never over a manual choice. It does NOT claim manual control
        itself: the narrator must be able to take the soundtrack the
        moment the story opens, which is the whole point of starting one
        here rather than making the player ask."""
        if (self._adv_music or self._music_manual
                or not self.music.available):
            return
        self._adv_music = True
        for mood in self.SETUP_MOODS:
            tune = self.music.pick(mood)
            if not tune:
                continue
            self.music.mark_changed()
            self.conv_manager.set_meta('music', {'mood': mood,
                                                 'tune': tune['id']})
            # Spawned, never awaited: send_sid waits for an ACK that only
            # the reader task can dispatch, and this runs ON that task.
            self._spawn_media(self.send_sid(tune))
            self.logger.info("setup music: %s (%s)", tune['title'], mood)
            return

    async def _adv_setup_input(self, text: str):
        """One turn of the front door (docs/09-adventure-setup.md). The
        state machine decides what to show; this only performs what it
        asks for."""
        setup = self._adv_setup
        reply, act = setup.feed(text)
        if act == ACT_LOAD:
            saved = self._templates.load(setup.template_slug)
            if not saved:
                self._adv_setup = None
                await self._send_canned("That world could not be read.")
                return
            if text.strip().startswith('2'):
                # Keep the world, roll a new character: the prep notes
                # are reused, so the expensive pass is not paid again.
                reply, _ = setup.start_reroll(saved)
                await self._send_canned(reply)
                await self._setup_music()
                return
            self._adv_setup = None
            await self._start_adventure(
                self._theme_from(saved.get('bundle') or {}),
                background=self._background(saved.get('bible') or '',
                                           saved.get('character') or ''),
                character=saved.get('character') or '')
            return
        if act in (ACT_QUICK, ACT_THEME, ACT_BEGIN):
            self._adv_setup = None
            if act in (ACT_QUICK, ACT_THEME):
                theme = '' if act == ACT_QUICK else setup.theme
                await self._start_adventure(theme)
                return
            # ACT_BEGIN: prepare the world before the first scene.
            bundle = setup.bundle()
            theme = self._theme_from(bundle)
            character = setup.character_block()
            # A re-roll reuses the saved prep notes rather than paying
            # for a second thinking pass over the same world.
            saved = (self._templates.load(setup.template_slug)
                     if setup.template_slug else None)
            if saved and saved.get('bible'):
                bible = saved['bible']
            else:
                await self.send_status("Preparing the world... (20-30s)")
                hb = asyncio.create_task(
                    self._heartbeat("Preparing the world..."))
                try:
                    bible = await self._prep_world(bundle, character)
                finally:
                    hb.cancel()
                # Keep what was built. A failed save must never stop an
                # adventure starting.
                self._templates.save(bundle, bible, character,
                                     model=self.model_override or '')
            await self._start_adventure(
                theme, background=self._background(bible, character),
                character=character)
            return
        if reply:
            await self._send_canned(reply)
            # Music once the player commits to BUILDING a character.
            # Not at the chooser: options 1 and 2 go straight into play,
            # where the narrator picks the soundtrack itself, and a SID
            # in flight makes the client swallow Return as 'Busy' - no
            # reason to spend that on someone who chose 'surprise me'.
            if self._adv_setup and self._adv_setup.state == 'stage':
                await self._setup_music()

    async def _stop_music(self):
        """Silence the client and make it STAY silent. Stopping is a
        manual act, so it also takes the soundtrack off the narrator -
        otherwise the next [[MUSIC:]] directive restarts what the player
        just switched off, which is the most annoying possible outcome.
        /auto hands it back."""
        self._music_stopped = True
        self._music_manual = True
        self._manual_notice_sent = False
        self.music.tune_started = None
        await self.send_message(MessageType.MUSIC_STOP, b'')
        await self._send_hint()
        await self._send_canned(
            "Music off. /music <mood> starts another, /auto gives the "
            "soundtrack back to the narrator.")

    def _current_tune(self):
        """(tune, mood) for whatever is playing, or (None, '')."""
        meta = self.conv_manager.get_meta('music') or {}
        tune = self.music.find(meta.get('tune')) if meta.get('tune') else None
        return tune, meta.get('mood', '')

    async def handle_get_nowplaying(self):
        """Jukebox module (#5) asking what is playing.

        The client cannot work any of this out for itself: a SID file
        carries no title, no author and no duration, and the tune it is
        playing arrived as a bare memory image. So the server answers
        with everything the panel needs, including elapsed time already
        worked out - the module only has to count seconds forward from
        here off its own 60Hz tick.
        """
        tune, mood = self._current_tune()
        elapsed = 0
        flags = 0
        secs = 0
        if tune and self.music.tune_started is not None:
            flags |= 1
            elapsed = int(time.monotonic() - self.music.tune_started)
            secs = int(tune.get('secs') or 0)
            if secs:
                # A tune that has looped shows position within the loop
                # rather than a bar pinned at the end
                elapsed %= secs
            if self.music.is_favorite(tune['id']):
                flags |= 2
        if self._music_manual:
            flags |= 4      # jukebox shows who is choosing

        def field(text, limit):
            return text[:limit].encode('ascii', errors='replace') + b'\x00'

        payload = (bytes([flags]) + struct.pack('<HH', min(elapsed, 0xFFFF),
                                                min(secs, 0xFFFF))
                   + field(tune['title'] if tune else '', 36)
                   + field(tune['author'] if tune else '', 24)
                   + field(mood, 12))
        await self._send_bulk(MessageType.NOWPLAYING, payload)

    async def handle_fav_tune(self):
        """Toggle favorite on the current tune. The module has already
        flipped its own star optimistically; ACK confirms, NAK means
        there was nothing playing to favorite."""
        tune, _ = self._current_tune()
        if not tune:
            await self.send_nak()
            return
        now = self.music.toggle_favorite(tune['id'])
        self.logger.info("%s favorite: %s",
                         "Added" if now else "Removed", tune['id'])
        await self.send_ack()

    async def handle_delete_conversation(self):
        """Delete a conversation (id in payload). The manager module
        confirms client-side; this is the point of no return."""
        if len(self.payload) < 4:
            await self.send_nak()
            return
        conv_id = self._resolve_masked_id(
            struct.unpack('<I', self.payload[:4])[0])
        if self.conv_manager.delete_conversation(conv_id):
            self.logger.info(f"Deleted conversation {conv_id}")
            await self.send_ack()
        else:
            await self.send_nak()

    async def handle_star_conversation(self):
        """Toggle a conversation's starred flag (id in payload)."""
        if len(self.payload) < 4:
            await self.send_nak()
            return
        conv_id = self._resolve_masked_id(
            struct.unpack('<I', self.payload[:4])[0])
        starred = self.conv_manager.toggle_star(conv_id)
        if starred is None:
            await self.send_nak()
        else:
            self.logger.info(f"Conversation {conv_id} starred={starred}")
            await self.send_ack()

    async def handle_load_conversation(self):
        """Load a conversation"""
        if len(self.payload) < 4:
            self.logger.error("Invalid LOAD_CONVERSATION payload")
            await self.send_nak()
            return
        # An in-flight stream would save its response into the newly
        # loaded conversation and interleave frames with the load
        self._cancel_stream()
        self.images.pending_prompt = None

        masked_id = struct.unpack('<I', self.payload[:4])[0]
        self.logger.info(f"Load conversation: {masked_id}")

        await self.send_ack()

        conv_id = self._resolve_masked_id(masked_id)

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

            # The client's frame buffer is 512 bytes (MAX_PAYLOAD): a
            # long message is SPLIT across frames, continuations marked
            # with bit 7 of the role byte so the client appends instead
            # of starting a new chat block.
            FRAME_TEXT = 380
            # Colour markup becomes marker cells HERE, before slicing:
            # stored history keeps its tags, and colorizing a frame at a
            # time could cut a tag across the boundary.
            frames = []
            if omitted > 0:
                frames.append((2, colorize_for_wire(
                    f'(... {omitted} earlier messages '
                    f'not shown - /history has them ...)')))
            for m in window:
                role = 0 if m['role'] == 'user' else 1
                data = colorize_for_wire(clip(m['content']))
                for i in range(0, len(data), FRAME_TEXT):
                    frames.append((role if i == 0 else role | 0x80,
                                   data[i:i + FRAME_TEXT]))
            # Zero frames would leave the client waiting forever: the
            # 'load done' signal is the final more=0 frame
            if not frames:
                frames.append((2, colorize_for_wire('(empty conversation)')))

            # One message per frame: the C64 client's payload buffer is
            # small (512 bytes), so keep each frame well under that.
            for i, (role, text) in enumerate(frames):
                more = 1 if i + 1 < len(frames) else 0

                payload = bytearray()
                payload.append(1)
                payload.append(more)
                payload.append(role)
                payload.extend(text)        # already colorized bytes
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
            # Rebuild the mode unconditionally: same-NAME loads still
            # need the right theme/character (adventure theme B loaded
            # while in theme A kept A's prompt), and loading a plain
            # chat while in adventure kept narrating in adventure voice
            if meta_mode == 'adventure':
                mode = AdventureMode(
                    self.config, theme=self.conv_manager.get_meta('theme', ''))
                self._attach_snippets(mode)
                self.mode = mode  # not _switch_mode: keep the conversation
                self.logger.info("Restored adventure mode from conversation")
            elif meta_mode == 'roleplay':
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
            elif meta_mode == 'chat' and self.mode.name != 'chat':
                self.mode = Mode(self.config)
                self.logger.info("Restored chat mode from conversation")

            # ...and the soundtrack (exact tune if still in the library,
            # else another tune of the same mood). The transfer starts only
            # after the last CONVERSATION_DATA frame so nothing interleaves.
            music_meta = self.conv_manager.get_meta('music')
            if music_meta and self.music.available:
                tune = (self.music.find(music_meta.get('tune'))
                        or self.music.pick(music_meta.get('mood', '')))
                if tune:
                    self._spawn_media(self._resume_tune(tune, len(frames)))

            # The tally belongs to the conversation, so a load has to
            # restate it - otherwise the corner keeps showing the count
            # from whatever was open before.
            await self._send_hint(0)
        else:
            error = b"Conversation not found\x00"
            await self.send_message(MessageType.CHAT_ERROR, error)

    async def handle_new_conversation(self):
        """Start a new conversation"""
        self.logger.info("New conversation request")
        self._cancel_stream()
        self.images.pending_prompt = None
        self._music_manual = False
        self._manual_notice_sent = False
        self.conv_manager.new_conversation()
        await self.send_ack()
        await self._send_hint(0)      # fresh conversation, empty tally
