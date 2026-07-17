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


class ViceMonitorError(Exception):
    pass


class ViceMonitor:
    def __init__(self, host='127.0.0.1', port=6502, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._req_id = 0

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

    def read_memory(self, start, end):
        body = struct.pack('<BHHBH', 0, start, end, 0, 0)
        resp_type, error, rbody = self._transact(CMD_MEM_GET, body)
        if error:
            raise ViceMonitorError(f'memory read failed: error {error}')
        (length,) = struct.unpack('<H', rbody[:2])
        data = rbody[2:2 + length]
        self.resume()
        return data

    def screen_rows(self):
        """Return the 25 screen rows as decoded ASCII-ish strings."""
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
        """Type text into the emulated keyboard buffer (PETSCII bytes)."""
        encoded = text.encode('ascii')
        self._transact(CMD_KEYBOARD_FEED, bytes([len(encoded)]) + encoded)
        self.resume()

    def quit(self):
        """Ask VICE to exit (triggers -exitscreenshot if configured)."""
        try:
            self._send(CMD_QUIT)
        except OSError:
            pass
