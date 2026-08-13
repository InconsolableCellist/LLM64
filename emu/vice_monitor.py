"""Minimal client for the VICE binary monitor protocol (VICE 3.x).

Enough to read screen RAM, inject keystrokes, and quit the emulator —
the primitives the automated test harness needs. Protocol reference:
https://vice-emu.sourceforge.io/vice_13.html
"""

import socket
import struct
import time

STX = 0x02
API_VERSION = 0x02

CMD_MEM_GET = 0x01
CMD_MEM_SET = 0x02
CMD_KEYBOARD_FEED = 0x72
CMD_PING = 0x81
CMD_EXIT = 0xAA        # resume emulation
CMD_QUIT = 0xBB

SCREEN_RAM = 0x0400
SCREEN_COLS = 40
SCREEN_ROWS = 25


def screen_code_to_char(value):
    """Decode a C64 screen code to approximate ASCII (case-insensitive use).

    Codes 1-26 are letters in both charsets; 65-90 are letters in the
    shifted charset (graphics in the default one) — decode both as letters
    since assertions compare case-insensitively. Graphics map to '?'.
    """
    c = value & 0x7F  # strip reverse-video bit
    if c == 0:
        return '@'
    if 1 <= c <= 26:
        return chr(ord('a') + c - 1)
    if 27 <= c <= 31:
        return {27: '[', 28: '#', 29: ']', 30: '^', 31: '_'}[c]
    if 32 <= c <= 63:
        return chr(c)
    if 65 <= c <= 90:
        return chr(ord('A') + c - 65)
    return '?'


def ascii_to_petscii(c):
    """ASCII byte -> PETSCII byte for keyboard input."""
    if 0x61 <= c <= 0x7A:      # a-z
        return c - 0x20
    if 0x41 <= c <= 0x5A:      # A-Z
        return c + 0x80
    return c


class ViceMonitorError(Exception):
    pass


class ViceMonitor:
    def __init__(self, host='127.0.0.1', port=6502, timeout=10.0,
                 cols80=False):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._req_id = 0
        # In soft-80 mode the visible screen is a bitmap; the client keeps
        # an ASCII shadow of all 80x25 cells at $C000 for us.
        self.cols80 = cols80

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, command, body=b''):
        self._req_id += 1
        header = (bytes([STX, API_VERSION])
                  + struct.pack('<I', len(body))
                  + struct.pack('<I', self._req_id)
                  + bytes([command]))
        self.sock.sendall(header + body)
        return self._req_id

    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ViceMonitorError('monitor connection closed')
            buf += chunk
        return buf

    def _read_frame(self):
        header = self._recv_exact(12)
        if header[0] != STX:
            raise ViceMonitorError(f'bad frame start: {header[0]:#x}')
        body_len = struct.unpack('<I', header[2:6])[0]
        resp_type = header[6]
        error = header[7]
        req_id = struct.unpack('<I', header[8:12])[0]
        body = self._recv_exact(body_len) if body_len else b''
        return resp_type, error, req_id, body

    def _transact(self, command, body=b''):
        """Send a command and wait for its response, skipping async events."""
        req_id = self._send(command, body)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            resp_type, error, rid, rbody = self._read_frame()
            if rid == req_id:
                return resp_type, error, rbody
            # rid 0xffffffff = unsolicited event (stopped/resumed/etc); skip
        raise ViceMonitorError(f'timeout waiting for response to {command:#x}')

    def resume(self):
        """Resume emulation (commands leave the machine paused in monitor)."""
        try:
            self._transact(CMD_EXIT)
        except ViceMonitorError:
            pass

    def read_memory(self, start, end, bank=0):
        """bank 0 = CPU view (ROMs visible); bank 1 = RAM (needed to see
        writes under the KERNAL, e.g. the bitmap at $E000)."""
        body = struct.pack('<BHHBH', 0, start, end, 0, bank)
        resp_type, error, rbody = self._transact(CMD_MEM_GET, body)
        if error:
            raise ViceMonitorError(f'memory read failed: error {error}')
        (length,) = struct.unpack('<H', rbody[:2])
        data = rbody[2:2 + length]
        self.resume()
        return data

    def write_memory(self, start, data, bank=0):
        """Write bytes into the emulated machine's memory."""
        end = start + len(data) - 1
        body = struct.pack('<BHHBH', 0, start, end, 0, bank) + bytes(data)
        resp_type, error, rbody = self._transact(CMD_MEM_SET, body)
        if error:
            raise ViceMonitorError(f'memory write failed: error {error}')
        self.resume()

    def press_key(self, petscii):
        """Put one key straight into the KERNAL buffer.

        keyboard_feed goes through a PETSCII-to-matrix translation that
        drops the function keys, and this client scans the matrix itself
        anyway - it appends to the same buffer, so writing it directly is
        what a keypress would have produced.
        """
        self.write_memory(0x0277, bytes([petscii]))   # KBUF
        self.write_memory(0x00C6, bytes([1]))         # NDX

    def screen_rows(self):
        """Return the 25 screen rows as decoded ASCII-ish strings."""
        if self.cols80:
            data = self.read_memory(0xC000, 0xC000 + 80 * 25 - 1)
            return [
                ''.join(chr(b) if 0x20 <= b < 0x7F else '?'
                        for b in data[r * 80:(r + 1) * 80])
                for r in range(SCREEN_ROWS)
            ]
        data = self.read_memory(SCREEN_RAM,
                                SCREEN_RAM + SCREEN_COLS * SCREEN_ROWS - 1)
        return [
            ''.join(screen_code_to_char(b)
                    for b in data[r * SCREEN_COLS:(r + 1) * SCREEN_COLS])
            for r in range(SCREEN_ROWS)
        ]

    def screen_text(self):
        return '\n'.join(self.screen_rows())

    def keyboard_feed(self, text):
        """Type ASCII text into the emulated keyboard (converted to PETSCII)."""
        self.keyboard_feed_petscii(bytes(ascii_to_petscii(c)
                                         for c in text.encode('ascii')))

    def keyboard_feed_petscii(self, data):
        """Feed raw PETSCII bytes (e.g. b'\\x88' for F7). The command's
        length field is one byte, so long inputs are sent in chunks."""
        for i in range(0, len(data), 32):
            piece = data[i:i + 32]
            self._transact(CMD_KEYBOARD_FEED, bytes([len(piece)]) + piece)
            self.resume()
            if len(data) > 32:
                time.sleep(0.35)  # let the emulated KERNAL drain the queue

    def quit(self):
        """Ask VICE to exit (triggers -exitscreenshot if configured)."""
        try:
            self._send(CMD_QUIT)
        except OSError:
            pass
