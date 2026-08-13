"""Render a Home Assistant view as C64 soft-80 cells.

Pure: no network, no entity ids. The screen is derived from the
Lovelace config plus each entity's domain, device_class and unit, so
the same code renders any instance. The client blits what it gets.
"""

import asyncio
import collections
import json
import logging
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# --- the C64's palette, by the names profiles.py already uses ---------
BLACK, WHITE, RED, CYAN = 0, 1, 2, 3
PURPLE, GREEN, BLUE, YELLOW = 4, 5, 6, 7
ORANGE, BROWN, PINK, DKGREY = 8, 9, 10, 11
GRAY, LTGREEN, LTBLUE, LTGREY = 12, 13, 14, 15

COLS = 80          # soft-80 cells per row
PAIRS = 40         # color matrix entries per row (one per 8x8 cell)
ROWS = 25

# Ink by meaning, not by domain.
INK = {
    'good': GREEN,     # nothing to see here
    'bad': RED,        # a door is open
    'warn': YELLOW,    # motion, right now
    'dim': DKGREY,     # off, or unavailable
    'num': LTGREEN,    # a reading
    'info': CYAN,      # a mode or a word
    'name': LTGREY,
    'key': YELLOW,
    'head': CYAN,
}


def cell(ch: str, reverse: bool = False) -> int:
    """One soft-80 cell: ASCII, bit 7 = reverse video."""
    c = ord(ch)
    if c < 0x20 or c > 0x7E:
        c = 0x20
    return c | (0x80 if reverse else 0)


def mat(fg: int, bg: int = BLACK) -> int:
    """Matrix byte: fg high nibble, bg low.

    A cell is 8x8 and holds two 4x8 glyphs, so color changes every two
    columns. Both an ink and a ground, as mod_menu uses.
    """
    return ((fg & 15) << 4) | (bg & 15)


# Card type -> how to draw it. The only place card knowledge lives.
# Unknown types fall through to ROWS and list their entities.
CARD_KIND = {
    'thermostat': 'EDIT_CLIMATE',
    'humidifier': 'EDIT_CLIMATE',
    'light': 'EDIT_LIGHT',
    'history-graph': 'PLOT',
    'statistics-graph': 'PLOT',
    'sensor': 'PLOT',
    'gauge': 'BAR',
    'heading': 'HEADING',
    'map': 'SKIP',
    # No room for the history, but the entities still belong on screen.
    'logbook': 'ROWS',
    'iframe': 'SKIP',
    'picture-elements': 'SKIP',
    'picture-glance': 'ROWS',
    'media-control': 'ROWS',
    'weather-forecast': 'ROWS',
}
DEFAULT_KIND = 'ROWS'

# device_class -> (text when on, text when off, on_is_alarming)
BINARY = {
    'door': ('OPEN', 'shut', True),
    'garage_door': ('OPEN', 'shut', True),
    'window': ('OPEN', 'shut', True),
    'opening': ('OPEN', 'shut', True),
    'lock': ('UNLKD', 'locked', True),
    'motion': ('MOTION', 'clear', None),
    'moving': ('MOVING', 'still', None),
    'occupancy': ('HERE', 'empty', None),
    'presence': ('HOME', 'away', None),
    'moisture': ('WET', 'dry', True),
    'smoke': ('SMOKE', 'clear', True),
    'gas': ('GAS', 'clear', True),
    'carbon_monoxide': ('CO!', 'clear', True),
    'problem': ('FAULT', 'ok', True),
    'safety': ('UNSAFE', 'safe', True),
    'tamper': ('TAMPER', 'ok', True),
    'battery': ('LOW', 'ok', True),
    'connectivity': ('up', 'DOWN', False),
    'running': ('RUN', 'idle', None),
    'update': ('UPDATE', 'current', None),
    None: ('ON', 'off', None),
}

# Domains whose primary verb is not "toggle".
EDITOR_FOR = {
    'number': 'EDIT_NUMBER',
    'input_number': 'EDIT_NUMBER',
    'climate': 'EDIT_CLIMATE',
    'water_heater': 'EDIT_CLIMATE',
    'light': 'EDIT_LIGHT',
    'cover': 'CONFIRM',
    'lock': 'CONFIRM',
    'vacuum': 'CONFIRM',
}
TOGGLE_DOMAINS = {
    'switch', 'input_boolean', 'automation', 'script', 'fan', 'siren',
    'humidifier', 'media_player', 'button', 'scene',
}
# A second keypress does not undo these, so they ask first.
CONFIRM_DOMAINS = {'cover', 'lock', 'vacuum'}


# =====================================================================
# Naming
# =====================================================================

def _clean(friendly: Optional[str], entity_id: str) -> str:
    """Drop integration boilerplate, keep every word that informs."""
    s = friendly or entity_id.split('.', 1)[-1].replace('_', ' ')
    s = re.sub(r'\b(window|door)\s*/\s*(door|window)\s+is\s+open\b', '', s, flags=re.I)
    s = re.sub(r'\bis\s+open\b', '', s, flags=re.I)
    s = re.sub(r'\bopening\b\s*$', '', s, flags=re.I)
    s = re.sub(r'\bmotion\s+detect(ion)?\b', 'Motion', s, flags=re.I)
    s = re.sub(r'\bv?\d+\.\d+\b', '', s)                  # "v1.0"
    s = re.sub(r'\b[0-9A-F]{6,}\b', '', s, flags=re.I)    # hex device ids
    s = re.sub(r'[_]+', ' ', s)
    s = re.sub(r'\s*_\d+\s*$', '', s)
    s = re.sub(r'\b(\w+)(\s+\1\b)+', r'\1', s, flags=re.I)  # "Temp Temp"
    return re.sub(r'\s{2,}', ' ', s).strip(' ,-')


def _ladder(friendly: Optional[str], entity_id: str, area: Optional[str]) -> List[str]:
    """Candidate names, shortest first, padded to a fixed length.

    No rung is safe alone: dropping the area turns three doors into
    three rows called "Door". name_screen picks the rung.
    """
    base = _clean(friendly, entity_id)
    cands = []
    a = base
    if area:
        a = re.sub(r'^' + re.escape(area) + r'\s+', '', a, flags=re.I)
    a = re.sub(r'\b(sensor|switch|light|controller|opener)\b\s*$', '', a, flags=re.I).strip()
    cands.append(a)
    cands.append(re.sub(r'\b(sensor|switch|light|controller)\b\s*$', '', base, flags=re.I).strip())
    cands.append(base)
    fallback = entity_id.split('.', 1)[-1].replace('_', ' ')

    out: List[str] = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    # Pad so every ladder has the same rungs; otherwise clamping drops
    # a short ladder to the entity id while its siblings keep a name.
    while len(out) < 3:
        out.append(out[-1] if out else fallback)
    out.append(fallback)
    return out


def _fits(s: str, width: int) -> List[str]:
    """Both truncations. Dropping the tail keeps "Doorbell on door";
    dropping the head keeps "Garage Door Single". Caller picks.
    """
    if len(s) <= width:
        return [s]
    head = s[:width]
    if ' ' in head:
        head = head.rsplit(' ', 1)[0]
    tail = s[-width:]
    if ' ' in tail:
        tail = tail.split(' ', 1)[1]
    out = []
    for c in (head, tail):
        if c and c not in out:
            out.append(c)
    return out or [s[:width]]


def _group_key(entity_id: str, attrs: dict) -> tuple:
    dc = attrs.get('device_class')
    if dc is not None:
        return (entity_id.split('.')[0], dc)
    # With no device_class, siblings are the ones measuring the same
    # thing: the tail of the name. Grouping all sensors together drags
    # them to the entity id.
    words = _clean(attrs.get('friendly_name'), entity_id).split()
    return (entity_id.split('.')[0], None, words[-1].lower() if words else '')


def name_screen(entities: Sequence[str], states: Dict[str, dict],
                area_of: Callable[[str], Optional[str]],
                width: int = 24,
                overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Shorten names for one screen: unique and consistent.

    The rung is chosen per group, not per entity. Greedy per-entity
    picking lets one door take "Door" while its siblings keep "Garage
    Door" - unique, but unreadable as a list.
    """
    overrides = overrides or {}
    groups: 'collections.OrderedDict[tuple, List[str]]' = collections.OrderedDict()
    todo = [e for e in entities if e not in overrides]
    for e in todo:
        groups.setdefault(_group_key(e, states.get(e, {}).get('attributes', {})), []).append(e)

    ladders = {e: _ladder(states.get(e, {}).get('attributes', {}).get('friendly_name'),
                          e, area_of(e)) for e in todo}
    chosen: Dict[str, str] = {}
    used = set()
    for e, lab in overrides.items():
        if e in entities:
            chosen[e] = lab[:width]
            used.add(chosen[e].lower())

    for members in groups.values():
        # Words every sibling shares carry no information. Dropping them
        # gives "Basement Humidity" / "Family Room Humidity". Never drop
        # the last word: it is the measurement.
        if len(members) > 1:
            wordsets = [_clean(states.get(e, {}).get('attributes', {}).get('friendly_name'),
                               e).split() for e in members]
            if all(len(ws) > 1 for ws in wordsets):
                common = set.intersection(*[{w.lower() for w in ws[:-1]} for ws in wordsets])
                if common:
                    for e, ws in zip(members, wordsets):
                        trimmed = ' '.join(w for i, w in enumerate(ws)
                                           if i == len(ws) - 1 or w.lower() not in common)
                        if trimmed and len(trimmed.split()) >= 2:
                            ladders[e] = [trimmed] + ladders[e]

        nrungs = max(len(ladders[e]) for e in members)
        placed = False
        # One truncation direction per group, or two garage doors come out
        # "Door Opener Garage Door" and "Garage Door Double".
        for rung in range(nrungs):
            for cut in (0, 1):
                trial: Dict[str, str] = {}
                ok = True
                for e in members:
                    cands = ladders[e]
                    variants = _fits(cands[min(rung, len(cands) - 1)], width)
                    pick = variants[min(cut, len(variants) - 1)]
                    low = pick.lower()
                    if low in used or low in {v.lower() for v in trial.values()}:
                        ok = False
                        break
                    trial[e] = pick
                if ok and len(members) > 1:
                    # One-word labels only if the full name was one word.
                    for e, v in trial.items():
                        full = _clean(states.get(e, {}).get('attributes', {}).get('friendly_name'), e)
                        if len(v.split()) < 2 and len(full.split()) > 1:
                            ok = False
                            break
                if ok:
                    chosen.update(trial)
                    used.update(v.lower() for v in trial.values())
                    placed = True
                    break
            if placed:
                break
        if not placed:
            for e in members:
                lab = _fits(ladders[e][-1], width)[0]
                n = 2
                while lab.lower() in used:
                    lab = f'{lab[:width - 2]} {n}'
                    n += 1
                chosen[e] = lab
                used.add(lab.lower())
    return chosen


# =====================================================================
# Values
# =====================================================================

def fmt_state(state: Optional[str], attrs: dict, domain: str,
              width: int = 8) -> Tuple[str, str]:
    """(text, ink-role). Never longer than width."""
    dc = attrs.get('device_class')
    if state in (None, 'unavailable', 'unknown'):
        return 'n/a', 'dim'
    if domain == 'binary_sensor':
        # alarm: True = on is bad (door), False = off is bad
        # (connectivity), None = neither (motion).
        on, off, alarm = BINARY.get(dc, BINARY[None])
        if state == 'on':
            ink = 'bad' if alarm is True else ('good' if alarm is False else 'warn')
            return on[:width], ink
        ink = 'good' if alarm is True else ('bad' if alarm is False else 'dim')
        return off[:width], ink
    if domain in ('switch', 'light', 'input_boolean', 'automation', 'script',
                  'fan', 'siren', 'humidifier'):
        return ('ON' if state == 'on' else 'OFF'), ('good' if state == 'on' else 'dim')
    if domain == 'cover':
        shut = state == 'closed'
        return ('shut' if shut else state.upper()[:width]), ('good' if shut else 'bad')
    if domain == 'lock':
        return ('locked' if state == 'locked' else 'UNLKD'), ('good' if state == 'locked' else 'bad')
    if domain == 'climate':
        t = attrs.get('temperature')
        if isinstance(t, (int, float)):
            return f'{t:.0f}'[:width], 'info'
        return state.upper()[:width], 'info'
    unit = (attrs.get('unit_of_measurement') or '')
    unit = unit.replace('°', '').replace('µg/m³', 'ug')
    try:
        v = float(state)
    except (TypeError, ValueError):
        return str(state)[:width], 'info'
    for dec in (1, 0):
        s = f'{v:.{dec}f}' if abs(v) < 10 else f'{v:.0f}'
        if len(s) + len(unit) <= width:
            return s + unit, 'num'
    return f'{v:.0f}'[:width], 'num'


def action_for(domain: str) -> Optional[str]:
    if domain in EDITOR_FOR:
        return EDITOR_FOR[domain]
    if domain in TOGGLE_DOMAINS:
        return 'TOGGLE'
    return None


# =====================================================================
# History
# =====================================================================

def resample(points: Sequence[Tuple[float, float]], ncols: int) -> List[float]:
    """Bin (epoch_seconds, value) onto a uniform time grid.

    History is event-driven, so samples cluster where the value moved.
    Binning by sample index instead of time makes solar generate at
    midnight. An empty bin holds the last value.
    """
    if ncols <= 0:
        return []
    rows = sorted(points, key=lambda r: r[0])
    if not rows:
        return [0.0] * ncols
    t0, t1 = rows[0][0], rows[-1][0]
    span = (t1 - t0) or 1.0
    out: List[float] = []
    i = 0
    last = rows[0][1]
    for c in range(ncols):
        edge = t0 + span * (c + 1) / ncols
        acc = []
        while i < len(rows) and rows[i][0] <= edge:
            acc.append(rows[i][1])
            i += 1
        if acc:
            last = sum(acc) / len(acc)
        out.append(last)
    return out


def rasterize(ys: Sequence[int], cell0: int, ncells: int,
              rows: int) -> List[bytes]:
    """Trace -> finished hires bytes, one block per text row.

    Only the cells the graph owns are sent, so a label beside it is not
    overwritten. The client cannot rasterize itself: the bitmap lives
    under the KERNAL, where writes reach RAM but reads return ROM.

    Within a row, byte (x>>3)*8 + (y&7), bit 7-(x&7).
    """
    x0 = cell0 * 8
    blocks = [bytearray(ncells * 8) for _ in range(rows)]
    prev = ys[0] if ys else 0
    for i, y in enumerate(ys):
        x = i
        if x >= ncells * 8:
            break
        lo, hi = (prev, y) if y > prev else (y, prev)
        for yy in range(lo, hi + 1):
            if 0 <= yy < rows * 8:
                blocks[yy >> 3][((x >> 3) << 3) + (yy & 7)] |= 0x80 >> (x & 7)
        prev = y
    return [bytes(b) for b in blocks]


def scale_to_band(values: Sequence[float], height_px: int,
                  lo: Optional[float] = None,
                  hi: Optional[float] = None) -> List[int]:
    """Values -> y offsets from the band top, ready to plot."""
    if not values or height_px <= 0:
        return []
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = (hi - lo) or 1.0
    out = []
    for v in values:
        v = max(lo, min(hi, v))
        y = int(round((v - lo) / span * (height_px - 1)))
        out.append(height_px - 1 - y)
    return out


# =====================================================================
# Screen model
# =====================================================================

class Row:
    """One 80-cell text row plus its 40-entry color matrix."""

    __slots__ = ('cells', 'colors')

    def __init__(self):
        self.cells = [0x20] * COLS
        self.colors = [mat(LTGREY)] * PAIRS

    def put(self, col: int, text: str, reverse: bool = False) -> None:
        for i, ch in enumerate(text):
            c = col + i
            if 0 <= c < COLS:
                self.cells[c] = cell(ch, reverse)

    def ink(self, col: int, ncols: int, fg: int, bg: int = BLACK) -> None:
        """Color a span, snapped to the 2-column matrix grid."""
        start, end = max(0, col) // 2, min(COLS, col + ncols + 1) // 2
        for p in range(start, end):
            self.colors[p] = mat(fg, bg)

    def to_bytes(self) -> bytes:
        return bytes(self.colors) + bytes(self.cells)


class Screen:
    def __init__(self):
        self.rows = [Row() for _ in range(ROWS)]
        self.plots: List[dict] = []      # {'row','rows','x0','ys'}
        self.keymap: Dict[str, dict] = {}   # hotkey -> {'entity','action'}
        self.labels: Dict[str, str] = {}    # entity -> shortened name
        self.page = 0
        self.npages = 1

    def row(self, n: int) -> Row:
        return self.rows[n]

    def frames(self, max_payload: int = 512) -> List[Tuple[int, bytes]]:
        """Split the screen into HA_ROWS payloads that fit the wire."""
        per = 120
        cap = max(1, (max_payload - 2) // per)
        out = []
        for start in range(0, ROWS, cap):
            chunk = self.rows[start:start + cap]
            body = b''.join(r.to_bytes() for r in chunk)
            out.append((start, bytes([start, len(chunk)]) + body))
        return out

    def plot_frames(self) -> List[bytes]:
        out = []
        for p in self.plots:
            if not p.get('ys'):
                continue          # the band is filled later, from history
            ys = bytes(max(0, min(255, y)) for y in p['ys'])
            out.append(bytes([p['row'], p['rows'], p['x0'], len(ys)]) + ys)
        return out


# =====================================================================
# Deriving a view
# =====================================================================

def entities_of(card: dict) -> List[str]:
    out: List[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == 'entity' and isinstance(v, str):
                    out.append(v)
                elif k == 'entities' and isinstance(v, list):
                    for e in v:
                        if isinstance(e, str):
                            out.append(e)
                        elif isinstance(e, dict) and isinstance(e.get('entity'), str):
                            out.append(e['entity'])
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(card)
    seen = set()
    return [e for e in out if not (e in seen or seen.add(e))]


def build_blocks(view: dict) -> List[tuple]:
    """View -> [('SECTION', (heading, [eid, ...])), ('PLOT', eid), ...]"""
    blocks: List[tuple] = []
    containers = view.get('sections') or [{'cards': view.get('cards') or []}]
    for sec in containers:
        heading = sec.get('title')
        rows: List[str] = []
        for card in (sec.get('cards') or []):
            kind = CARD_KIND.get(card.get('type'), DEFAULT_KIND)
            if kind == 'HEADING':
                heading = card.get('heading') or card.get('title') or heading
                continue
            if kind == 'SKIP':
                continue
            # A graph or editor is extra, not a replacement: the entity
            # still gets a row carrying its current value.
            if kind in ('PLOT', 'BAR', 'EDIT_CLIMATE', 'EDIT_LIGHT'):
                ents = entities_of(card)
                for e in ents:
                    blocks.append((kind, e))
                rows.extend(ents)
                continue
            rows.extend(entities_of(card))
        seen = set()
        rows = [e for e in rows if not (e in seen or seen.add(e))]
        if rows or heading:
            blocks.append(('SECTION', (heading, rows)))
    return [b for b in blocks if b[0] != 'SECTION' or b[1][1]]


HOTKEYS = 'abcdefghijklmnopqrstuvwxyz'


def render_view(view: dict, states: Dict[str, dict],
                area_of: Callable[[str], Optional[str]],
                title: str = '',
                overrides: Optional[Dict[str, str]] = None,
                confirm_domains: Optional[set] = None,
                plot_label: Optional[tuple] = None,
                page: int = 0) -> Screen:
    """Overview screen for one view: two 38-column panes, sections kept
    whole where they fit."""
    confirm_domains = CONFIRM_DOMAINS if confirm_domains is None else confirm_domains
    blocks = build_blocks(view)
    plots = [b[1] for b in blocks if b[0] == 'PLOT']

    # One entity, one row. Lovelace repeats entities across cards; fed
    # the same id twice the namer collides it with itself.
    seen: set = set()
    sections = []
    for kind, payload in blocks:
        if kind != 'SECTION':
            continue
        heading, rows = payload
        fresh = [e for e in rows if not (e in seen or seen.add(e))]
        if fresh:
            sections.append((heading, fresh))

    all_rows = [e for _, rows in sections for e in rows]
    labels = name_screen(all_rows, states, area_of, overrides=overrides)

    sc = Screen()
    sc.labels = labels
    head = sc.row(0)
    head.put(0, (' LLM64 . HOME ASSISTANT . ' + title)[:56].ljust(58), reverse=True)
    head.put(58, 'F7 VIEWS  F8 EXIT '.rjust(22), reverse=True)
    head.ink(0, COLS, CYAN)

    # Break to the second pane when the first cannot hold a whole
    # section.
    TOP, BOTTOM = 1, 21
    PER_PANE = BOTTOM - TOP + 1
    PER_PAGE = PER_PANE * 2

    # Flatten to lines, then flow. A section bigger than one pane has to
    # continue into the next rather than jump to it whole: Downstairs is
    # thirty entities under one heading, and keeping it together left
    # the left pane empty and dropped everything past row 21.
    lines: List[tuple] = []
    for heading, ents in sections:
        if heading:
            lines.append(('head', heading))
        lines.extend(('ent', e) for e in ents)

    # A heading alone at the foot of a pane belongs to the next one.
    slots: List[Optional[tuple]] = []
    for ln in lines:
        if ln[0] == 'head' and (len(slots) % PER_PANE) == PER_PANE - 1:
            slots.append(None)
        slots.append(ln)

    npages = max(1, (len(slots) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, npages - 1))
    window = slots[page * PER_PAGE:(page + 1) * PER_PAGE]

    keys = iter(HOTKEYS)
    for i, ln in enumerate(window):
        if ln is None:
            continue
        base = 0 if i < PER_PANE else 40
        y = TOP + (i % PER_PANE)
        r = sc.row(y)
        if ln[0] == 'head':
            r.put(base, (' ' + ln[1])[:38].ljust(38), reverse=True)
            r.ink(base, 38, INK['head'])
            continue
        e = ln[1]
        st = states.get(e, {})
        attrs = st.get('attributes', {})
        domain = e.split('.')[0]
        act = action_for(domain)
        if act:
            k = next(keys, None)
            if k:
                r.put(base, f'{k})')
                r.ink(base, 2, INK['key'])
                sc.keymap[k] = {'entity': e, 'action': act,
                                'confirm': domain in confirm_domains}
        r.put(base + 4, labels.get(e, e)[:24])
        r.ink(base + 4, 24, INK['name'])
        text, role = fmt_state(st.get('state'), attrs, domain)
        r.put(base + 30, text.rjust(8))
        r.ink(base + 30, 8, INK[role])

    sc.page, sc.npages = page, npages

    foot = sc.row(24)
    hint = ' a-z act   R refresh   F7 views   F8 exit'
    if npages > 1:
        hint += f'   F4/F6 page {page + 1}/{npages}'
    foot.put(0, hint.ljust(COLS), reverse=True)
    foot.ink(0, COLS, GRAY)
    # The graph occupies cells 8-39 of rows 22-23; cells 0-7 carry its
    # label, so the plot frame must not overwrite them.
    sc.plots = []
    if plots:
        eid = plots[0]
        sc.plots.append({'row': 22, 'rows': 2, 'cell0': 8, 'ncells': 32,
                         'entity': eid})
        name, lo, hi, hours = (plot_label or (labels.get(eid, eid), None, None, 24))
        unit = (states.get(eid, {}).get('attributes', {})
                .get('unit_of_measurement') or '')
        unit = unit.replace('\u00b0', '')
        r22, r23 = sc.row(22), sc.row(23)
        r22.put(0, str(name)[:15])
        r22.ink(0, 16, GRAY)
        if lo is None:
            r23.put(0, f'{hours}h')
        else:
            r23.put(0, f'{hours}h {lo:.0f}-{hi:.0f}{unit}'[:15])
        r23.ink(0, 16, DKGREY)
    return sc


# =====================================================================
# Editors
# =====================================================================
#
# Built from cells, not pixels: a reverse space is a solid 4x8 block, so
# these travel in the ordinary HA_ROWS frame.

SEGMENTS = {
    '0': 'abcdef', '1': 'bc', '2': 'abdeg', '3': 'abcdg', '4': 'bcfg',
    '5': 'acdfg', '6': 'acdefg', '7': 'abc', '8': 'abcdefg', '9': 'abcdfg',
    '-': 'g', ' ': '',
}
# 4 cells of glyph + 2 of gap, so every digit starts on an even column
# and can be inked without bleeding into its neighbour.
DIGIT_W = 6
DIGIT_H = 5


def big_digits(screen: 'Screen', top: int, left: int, text: str,
               fg: int, bg: int = BLACK) -> int:
    """Paint text as seven-segment blocks. Returns width used."""
    x = left
    for ch in text:
        segs = SEGMENTS.get(ch, '')
        if ch == '.':                      # a full cell is too wide
            screen.row(top + DIGIT_H - 1).put(x, ' ', reverse=True)
            screen.row(top + DIGIT_H - 1).ink(x, 1, fg, bg)
            x += 2
            continue
        for r in range(DIGIT_H):
            row = screen.row(top + r)
            on = []
            # Horizontals span all four cells so they meet the
            # verticals; narrower, the digit reads as loose blocks.
            if r == 0 and 'a' in segs:
                on += [x, x + 1, x + 2, x + 3]
            if r == 2 and 'g' in segs:
                on += [x, x + 1, x + 2, x + 3]
            if r == 4 and 'd' in segs:
                on += [x, x + 1, x + 2, x + 3]
            if r in (0, 1) and 'f' in segs:
                on.append(x)
            if r in (0, 1) and 'b' in segs:
                on.append(x + 3)
            if r in (2, 3) and 'e' in segs:
                on.append(x)
            if r in (2, 3) and 'c' in segs:
                on.append(x + 3)
            for cx in on:
                row.put(cx, ' ', reverse=True)
            if on:
                row.ink(min(on), max(on) - min(on) + 1, fg, bg)
        x += DIGIT_W
    return x - left


def _chrome(sc: 'Screen', title: str, keys: str, right: str = '') -> None:
    head = sc.row(0)
    head.put(0, (' LLM64 . ' + title)[:58].ljust(58), reverse=True)
    head.put(58, 'F7 VIEWS  F8 EXIT '.rjust(22), reverse=True)
    head.ink(0, COLS, CYAN)
    foot = sc.row(24)
    foot.put(0, (' ' + keys).ljust(COLS - len(right)) + right, reverse=True)
    foot.ink(0, COLS, GRAY)


def render_climate(entity_id: str, states: Dict[str, dict],
                   label: str = '', pending: Optional[float] = None) -> Screen:
    """Setpoint screen.

    `pending` is dialled but not sent, and is drawn in a different ink
    so it cannot be mistaken for the committed value.
    """
    st = states.get(entity_id, {})
    a = st.get('attributes', {})
    target = a.get('temperature')
    current = a.get('current_temperature')
    shown = pending if pending is not None else target
    sc = Screen()
    _chrome(sc, label or entity_id,
            '+ / -  setpoint   RET apply   M mode   STOP back   F8 exit',
            f'{shown:.0f} ' if isinstance(shown, (int, float)) else '')

    sc.row(2).put(2, 'TARGET')
    sc.row(2).ink(2, 8, YELLOW if pending is None else WHITE)
    if isinstance(shown, (int, float)):
        big_digits(sc, 4, 2, f'{shown:.0f}', YELLOW if pending is None else WHITE)
    if pending is not None:
        sc.row(9).put(2, 'not sent yet')
        sc.row(9).ink(2, 14, WHITE)

    sc.row(2).put(24, 'NOW')
    sc.row(2).ink(24, 4, GRAY)
    if isinstance(current, (int, float)):
        big_digits(sc, 4, 24, f'{current:.0f}', LTGREEN)

    x = 46
    sc.row(2).put(x, 'STATE')
    sc.row(2).ink(x, 6, GRAY)
    lines = [
        (4, f"{a.get('hvac_action') or st.get('state') or ''}"),
        (5, f"fan  {a.get('fan_mode') or '-'}"),
        (6, f"humidity {a.get('current_humidity', '-')}%"),
        (8, f"range {a.get('min_temp', '?'):.0f}-{a.get('max_temp', '?'):.0f}"
            if isinstance(a.get('min_temp'), (int, float)) else 'range ?'),
    ]
    for r, text in lines:
        sc.row(r).put(x, str(text)[:30])
        sc.row(r).ink(x, 30, LTGREEN if r == 4 else DKGREY)

    modes = a.get('hvac_modes') or []
    cur_mode = st.get('state')
    sc.row(11).put(2, 'MODE')
    sc.row(11).ink(2, 6, GRAY)
    mx = 10
    for m in modes[:6]:
        lab = f' {m.upper()[:8]} '
        active = (m == cur_mode)
        sc.row(11).put(mx, lab, reverse=active)
        sc.row(11).ink(mx, len(lab), CYAN if active else DKGREY)
        mx += len(lab) + 2

    sc.keymap = {
        '+': {'entity': entity_id, 'action': 'STEP', 'delta': 1},
        '-': {'entity': entity_id, 'action': 'STEP', 'delta': -1},
        '\r': {'entity': entity_id, 'action': 'APPLY'},
        'm': {'entity': entity_id, 'action': 'MODE'},
    }
    return sc


def render_number(entity_id: str, states: Dict[str, dict],
                  label: str = '', pending: Optional[float] = None) -> Screen:
    """A slider, as a number you nudge.

    Same contract as the setpoint: `pending` is dialled but not sent, and
    is drawn in a different ink so it cannot be mistaken for the value
    the entity actually holds.
    """
    st = states.get(entity_id, {})
    a = st.get('attributes', {})
    unit = (a.get('unit_of_measurement') or '').replace('\u00b0', '')
    lo = float(a.get('min', 0))
    hi = float(a.get('max', 100))
    step = float(a.get('step', 1) or 1)
    try:
        cur = float(st.get('state'))
    except (TypeError, ValueError):
        cur = lo
    shown = cur if pending is None else pending

    sc = Screen()
    _chrome(sc, label or entity_id,
            '+ / -  adjust   RET apply   STOP back   F8 exit',
            f'{shown:g}{unit} ')

    sc.row(2).put(2, 'VALUE')
    sc.row(2).ink(2, 8, YELLOW if pending is None else WHITE)
    txt = f'{shown:g}'
    big_digits(sc, 4, 2, txt, YELLOW if pending is None else WHITE)
    if unit:
        sc.row(8).put(2 + len(txt) * DIGIT_W, unit[:6])
        sc.row(8).ink(2 + len(txt) * DIGIT_W, 8, DKGREY)
    if pending is not None:
        sc.row(9).put(2, f'not sent yet (now {cur:g})')
        sc.row(9).ink(2, 28, WHITE)

    # where the value sits in its range, as a bar of reverse cells
    sc.row(11).put(2, 'RANGE')
    sc.row(11).ink(2, 8, GRAY)
    span = (hi - lo) or 1.0
    filled = int(round((shown - lo) / span * 60))
    bar = sc.row(12)
    for i in range(60):
        bar.put(2 + i, ' ', reverse=(i < filled))
    bar.ink(2, 60, YELLOW if pending is None else WHITE)
    sc.row(13).put(2, f'{lo:g}')
    sc.row(13).put(56, f'{hi:g}'.rjust(6))
    sc.row(13).ink(0, COLS, DKGREY)
    sc.row(15).put(2, f'step {step:g}')
    sc.row(15).ink(2, 20, DKGREY)

    sc.keymap = {
        '+': {'entity': entity_id, 'action': 'STEP_NUM', 'delta': 1},
        '-': {'entity': entity_id, 'action': 'STEP_NUM', 'delta': -1},
        '\r': {'entity': entity_id, 'action': 'APPLY_NUM'},
    }
    return sc


# The C64's sixteen as light colors: the (x, y) each one asks for.
PALETTE_XY = [
    (0.000, 0.000), (0.313, 0.329), (0.640, 0.330), (0.170, 0.340),
    (0.280, 0.130), (0.170, 0.700), (0.150, 0.060), (0.420, 0.505),
    (0.560, 0.410), (0.520, 0.400), (0.410, 0.250), (0.313, 0.329),
    (0.313, 0.329), (0.270, 0.560), (0.180, 0.180), (0.313, 0.329),
]
PALETTE_NAMES = ['blk', 'wht', 'red', 'cyn', 'pur', 'grn', 'blu', 'yel',
                 'org', 'brn', 'pnk', 'dgy', 'gry', 'lgn', 'lbl', 'lgy']
# Warm to cool.
TEMP_RAMP = [BROWN, ORANGE, ORANGE, YELLOW, YELLOW, LTGREY, WHITE, WHITE,
             LTBLUE, CYAN]


def render_light(entity_id: str, states: Dict[str, dict],
                 label: str = '', presets: Optional[Sequence[dict]] = None) -> Screen:
    """Brightness, white temperature and color. Which appear is read
    from the light's supported_color_modes."""
    st = states.get(entity_id, {})
    a = st.get('attributes', {})
    on = st.get('state') == 'on'
    bri = a.get('brightness')
    kelvin = a.get('color_temp_kelvin')
    kmin = a.get('min_color_temp_kelvin') or 2000
    kmax = a.get('max_color_temp_kelvin') or 6535
    modes = a.get('supported_color_modes') or []
    presets = presets or []

    sc = Screen()
    _chrome(sc, label or entity_id,
            '[ ] bright   < > temp   0-F color   1-9 preset   STOP back',
            'ON ' if on else 'OFF ')

    grp = a.get('group_entities') or []
    sc.row(2).put(2, (f'{len(grp)} controllers, ' if grp else '') + ('on' if on else 'off'))
    sc.row(2).ink(2, 24, GREEN if on else DKGREY)

    y = 4
    if bri is not None or 'brightness' in str(modes):
        pct = int(round((bri or 0) / 255 * 100))
        sc.row(y).put(2, 'BRIGHTNESS')
        sc.row(y).ink(2, 12, YELLOW)
        filled = int(round((bri or 0) / 255 * 60))
        bar = sc.row(y + 1)
        for i in range(60):
            bar.put(2 + i, ' ', reverse=(i < filled))
        bar.ink(2, 60, YELLOW)
        bar.put(64, f'{pct}%')
        bar.ink(64, 6, WHITE)
        y += 3

    if 'color_temp' in modes:
        sc.row(y).put(2, 'WHITE TEMPERATURE')
        sc.row(y).ink(2, 18, YELLOW)
        ramp = sc.row(y + 1)
        for i, c in enumerate(TEMP_RAMP):
            ramp.put(2 + i * 6, ' ' * 6, reverse=True)
            ramp.ink(2 + i * 6, 6, c)
        if kelvin:
            frac = (kelvin - kmin) / float(max(1, kmax - kmin))
            mark = 2 + int(round(frac * (len(TEMP_RAMP) * 6 - 2)))
            sc.row(y + 2).put(mark, '^')
            sc.row(y + 2).ink(mark, 2, WHITE)
        sc.row(y + 3).put(2, f'{kmin:.0f}K warm')
        sc.row(y + 3).put(48, f'cool {kmax:.0f}K' + (f'  now {kelvin:.0f}K' if kelvin else ''))
        sc.row(y + 3).ink(0, COLS, DKGREY)
        sc.row(y + 3).ink(48, 30, WHITE)
        y += 5

    if any(m in modes for m in ('xy', 'hs', 'rgb', 'rgbw', 'rgbww')):
        sc.row(y).put(2, 'COLOR')
        sc.row(y).ink(2, 8, YELLOW)
        sw, nm, hx = sc.row(y + 1), sc.row(y + 2), sc.row(y + 3)
        for i in range(16):
            x = 2 + i * 4
            sw.put(x, ' ' * 4, reverse=True)
            sw.ink(x, 4, i)
            nm.put(x, PALETTE_NAMES[i])
            nm.ink(x, 4, DKGREY)
            hx.put(x, f'{i:X}')
            hx.ink(x, 4, YELLOW)
        y += 5

    if presets:
        sc.row(y).put(2, 'PRESETS')
        sc.row(y).ink(2, 8, YELLOW)
        for i, p in enumerate(presets[:3]):
            x = 2 + i * 26
            sc.row(y + 1).put(x, f'{i + 1})')
            sc.row(y + 1).ink(x, 2, WHITE)
            sc.row(y + 1).put(x + 4, str(p.get('name', ''))[:20])
            sc.row(y + 1).ink(x + 4, 20, LTGREY)

    keymap = {'[': {'entity': entity_id, 'action': 'BRIGHT', 'delta': -10},
              ']': {'entity': entity_id, 'action': 'BRIGHT', 'delta': 10},
              '<': {'entity': entity_id, 'action': 'TEMP', 'delta': -300},
              '>': {'entity': entity_id, 'action': 'TEMP', 'delta': 300}}
    for i in range(16):
        keymap[f'{i:x}'] = {'entity': entity_id, 'action': 'COLOR', 'index': i}
    for i, p in enumerate(presets[:9]):
        keymap[str(i + 1)] = {'entity': entity_id, 'action': 'PRESET', 'preset': p}
    sc.keymap = keymap
    return sc


def render_views(dashboards: Sequence[dict], views: Sequence[dict],
                 current: int = 0) -> Screen:
    """Views and dashboards, from lovelace/dashboards/list."""
    sc = Screen()
    _chrome(sc, 'HOME ASSISTANT . choose a view',
            '1-9 view   a-z dashboard   STOP back   F8 exit')
    sc.row(2).put(0, ' Views'.ljust(38), reverse=True)
    sc.row(2).ink(0, 38, CYAN)
    for i, v in enumerate(views[:18]):
        r = sc.row(3 + i)
        r.put(0, f'{i + 1})')
        r.ink(0, 2, YELLOW)
        r.put(4, str(v.get('title') or v.get('path') or f'view {i}')[:24],
              reverse=(i == current))
        r.ink(4, 24, WHITE if i == current else LTGREY)
    sc.row(2).put(40, ' Dashboards'.ljust(38), reverse=True)
    sc.row(2).ink(40, 38, CYAN)
    keymap: Dict[str, dict] = {}
    for i, v in enumerate(views[:18]):
        keymap[str(i + 1)] = {'action': 'VIEW', 'index': i}
    for i, d in enumerate(dashboards[:18]):
        r = sc.row(3 + i)
        k = HOTKEYS[i]
        r.put(40, f'{k})')
        r.ink(40, 2, YELLOW)
        r.put(44, str(d.get('title') or d.get('url_path'))[:24])
        r.ink(44, 24, LTGREY)
        keymap[k] = {'action': 'DASHBOARD', 'url_path': d.get('url_path')}
    sc.keymap = keymap
    return sc


def render_confirm(base: Screen, question: str, keys: str = 'Y = yes    N = no') -> Screen:
    """Modal box over whatever is behind it.

    A cell has an ink and a ground, so white-on-red works; two inks in
    one cell do not, hence a colored ground rather than reverse video.
    """
    top, left, width = 8, 18, 44
    for r in range(top, top + 7):
        row = base.row(r)
        row.put(left, ' ' * width)
        row.ink(left, width, WHITE, RED)
    base.row(top + 1).put(left + 2, 'CONFIRM')
    base.row(top + 1).ink(left, width, WHITE, RED)
    base.row(top + 3).put(left + 2, question[:width - 4])
    base.row(top + 3).ink(left, width, WHITE, RED)
    base.row(top + 5).put(left + 2, keys)
    base.row(top + 5).ink(left, width, YELLOW, RED)
    return base
