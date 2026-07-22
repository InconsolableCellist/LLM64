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


def _context_pick(u, entry):
    """In an open context menu, arrow down to the line containing
    `entry` (case-insensitive) and hit Return. Raises if absent."""
    import time
    text = u.text().lower()
    lines = text.splitlines()
    hits = [i for i, l in enumerate(lines) if entry.lower() in l]
    if not hits:
        raise RuntimeError(
            f'menu entry {entry!r} not on screen:\n{u.text()}')
    # The highlighted entry is the first menu line; count menu lines
    # between it and the target. Menu entries are the contiguous
    # non-empty block around the hit.
    row = hits[0]
    first = row
    while first > 0 and lines[first - 1].strip():
        first -= 1
    for _ in range(row - first):
        u.key('down')
        time.sleep(0.1)
    u.key('enter')


def browser_entries(u):
    """Filenames listed in the browser pane, in screen order.

    The pane is a full-screen box drawn with VT100 line-drawing glyphs,
    which arrive as literal 'x' (vertical) and 'q' (horizontal) because
    _feed skips the charset-select escapes. So an entry row is one that
    starts with 'x', and the name is the first column of it - everything
    up to the run of padding spaces. Taking the first column also makes
    this work when a context menu is overlaid on the right-hand side.

    Counting rows the way a context menu is counted does NOT work here:
    every line of the box is non-empty, so "walk up to the first blank"
    runs to the top of the screen and overshoots the target."""
    import re
    names = []
    for line in u.text().splitlines():
        # An entry begins immediately after the left border. A space there
        # means an empty row - do NOT strip first, or an overlaid context
        # menu on the right becomes the "name" of every blank row.
        if len(line) > 2 and line[0] == 'x' and line[1] != ' ':
            name = re.split(r'\s{2,}', line[1:])[0].strip()
            if name:
                names.append(name)
    return names


def _browser_pick(u, filename):
    """Move the browser highlight onto `filename` and open its context
    menu. Raises if the name is not listed.

    The screen reconstruction cannot see which row is highlighted, so
    rather than guess we drive it to a known state: press Up once per
    listed entry, which clamps the highlight to the first row from
    wherever it started, then press Down by the target's index. Failing
    loudly beats guessing - running the wrong disk image looks exactly
    like a code bug."""
    entries = browser_entries(u)
    match = [i for i, e in enumerate(entries) if e.lower() == filename.lower()]
    if not match:
        raise RuntimeError(f'{filename!r} not in the browser listing '
                           f'{entries}:\n{u.text()}')
    u.key('up', len(entries))    # clamp to the first entry
    u.key('down', match[0])
    u.key('enter')      # context menu on the highlighted file


def deploy_and_run(path, host='192.168.1.64'):
    """Upload a PRG or D64 to /Flash via FTP and run it via the telnet
    menu. For a .d64 the Ultimate's 'Run Disk' mounts it on the drive
    (JiffyDOS-fast, config saves write back into the image) and boots
    LOAD"*",8,1.

    /Flash only, and under the artifact's REAL name. /Temp is a RAM disk
    that is wiped on power-off and the user never boots from it, so
    mirroring there bought nothing; keeping the real name is what lets
    c64llm.d64 and c64llm-free.d64 sit side by side and be picked from
    the Ultimate's own menu without a rebuild."""
    import subprocess
    import os
    is_d64 = path.lower().endswith('.d64')
    name = os.path.basename(path)
    subprocess.run(['curl', '-sS', '-T', path,
                    f'ftp://{host}/Flash/{name}', '--user', 'anonymous:'],
                   check=True)
    u = U64Screen(host)
    u.pump(1.0)
    u.key('esc')       # close a context menu left open by an earlier run
    u.key('left', 6)   # up to the root listing from wherever we are
    u.key('down')      # SD -> Flash
    u.key('right')     # enter Flash
    u.pump(0.5)
    _browser_pick(u, name)
    u.pump(0.5)
    if is_d64:
        _context_pick(u, 'run disk')
    else:
        u.key('enter')  # first entry: Run
    u.pump(1.0)
    u.close()
    print(f'deployed /Flash/{name} and running'
          + (' (disk mounted)' if is_d64 else ''))


if __name__ == '__main__':
    import sys
    deploy_and_run(sys.argv[1] if len(sys.argv) > 1
                   else 'c64_client/build/c64llm.prg')
