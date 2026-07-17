"""Minimal C64U telnet menu driver: VT100 screen reconstruction + keys."""
import re, socket, time

ROWS, COLS = 24, 80

class U64Screen:
    def __init__(self, host='192.168.1.64'):
        self.s = socket.create_connection((host, 23), timeout=5)
        self.s.settimeout(0.4)
        self.grid = [[' '] * COLS for _ in range(ROWS)]
        self.r = self.c = 0

    def _feed(self, data):
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == 0x1b:
                m = re.match(rb'\x1b\[([0-9;]*)([A-Za-z])', data[i:])
                if m:
                    params, cmd = m.group(1).decode(), m.group(2)
                    if cmd == b'H':
                        p = [int(x) for x in params.split(';') if x] or [1, 1]
                        self.r = min(max(p[0] - 1, 0), ROWS - 1)
                        self.c = min(max((p[1:] or [1])[0] - 1, 0), COLS - 1)
                    elif cmd == b'K':
                        for x in range(self.c, COLS):
                            self.grid[self.r][x] = ' '
                    elif cmd == b'J':
                        self.grid = [[' '] * COLS for _ in range(ROWS)]
                    i += m.end()
                    continue
                if data[i:i+2] in (b'\x1b(', b'\x1b)'):
                    i += 3
                    continue
                if data[i:i+2] == b'\x1bc':
                    self.grid = [[' '] * COLS for _ in range(ROWS)]
                    self.r = self.c = 0
                    i += 2
                    continue
                i += 1
                continue
            if ch == 0xff:  # telnet IAC
                i += 3 if len(data) > i + 2 else len(data) - i
                continue
            if ch == 0x0d: self.c = 0
            elif ch == 0x0a: self.r = min(self.r + 1, ROWS - 1)
            elif 0x20 <= ch < 0x7f:
                self.grid[self.r][self.c] = chr(ch)
                if self.c < COLS - 1: self.c += 1
            i += 1

    def pump(self, wait=0.8):
        end = time.time() + wait
        while time.time() < end:
            try:
                d = self.s.recv(8192)
                if not d: break
                self._feed(d)
            except socket.timeout:
                pass

    def send(self, data):
        self.s.sendall(data)
        self.pump()

    def key(self, name, n=1):
        codes = {'up': b'\x1b[A', 'down': b'\x1b[B', 'right': b'\x1b[C',
                 'left': b'\x1b[D', 'enter': b'\r', 'esc': b'\x1b\x1b',
                 'f1': b'\x1bOP', 'f3': b'\x1bOR', 'f5': b'\x1b[15~'}
        for _ in range(n):
            self.send(codes.get(name, name.encode()))
            time.sleep(0.15)

    def text(self):
        return '\n'.join(''.join(row).rstrip() for row in self.grid)

    def close(self):
        try: self.s.close()
        except OSError: pass


def deploy_and_run(prg_path, host='192.168.1.64'):
    """Upload a PRG to /Temp via FTP and run it through the telnet menu."""
    import subprocess
    subprocess.run(['curl', '-sS', '-T', prg_path,
                    f'ftp://{host}/Temp/c64llm.prg', '--user', 'anonymous:'],
                   check=True)
    u = U64Screen(host)
    u.pump(1.0)
    u.key('left', 6)   # up to the root listing from wherever we are
    u.key('down', 2)   # SD -> Flash -> Temp
    u.key('right')     # enter Temp
    u.key('enter')     # context menu on c64llm.prg
    u.key('enter')     # Run
    u.pump(1.0)
    u.close()
    print('deployed and running')


if __name__ == '__main__':
    import sys
    deploy_and_run(sys.argv[1] if len(sys.argv) > 1
                   else 'c64_client/build/c64llm.prg')
