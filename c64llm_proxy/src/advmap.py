"""The adventure map: a real graph the proxy keeps for itself.

See docs/10-adventure-map.md. A PURE module in the manner of
advsetup.py - no network, no model, no conversation, no asyncio. It
takes and returns plain dicts, which is what makes the interesting
behaviour (ingest and layout) testable without a model or an emulator.

The map is built from three signals, in priority order:

  1. [[STATE]].location changing - already shipping, and measured
     reliable (docs/10 section 0.1)
  2. the player's typed command - a null-filler only, never load-bearing
  3. the [[MAP: ...]] directive - cardinal direction and flavour

Only the third is new, and the feature works without it: a move is
recorded whenever the location changes, carrying dir=None when nothing
said which way. dir=None is normal, not an error, and every consumer
here handles it.

Nothing is ever deleted from a map. Pruning is a RENDERING decision -
deleting is what breaks "how do I get back to X", the one thing this
exists to do.
"""

import re

# The whole direction vocabulary. 'u', 'd', 'in' and 'out' are real
# directions with no place on a flat grid: they route and they appear in
# the legend, they are simply never drawn as connectors.
DIRS = ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw', 'u', 'd', 'in', 'out')

OPPOSITE = {'n': 's', 's': 'n', 'e': 'w', 'w': 'e',
            'ne': 'sw', 'sw': 'ne', 'nw': 'se', 'se': 'nw',
            'u': 'd', 'd': 'u', 'in': 'out', 'out': 'in'}

# Grid offsets, y growing downward (screen order). Only these eight are
# drawable; everything else is legend-only (section 5.2 step 7).
DELTA = {'n': (0, -1), 's': (0, 1), 'e': (1, 0), 'w': (-1, 0),
         'ne': (1, -1), 'nw': (-1, -1), 'se': (1, 1), 'sw': (-1, 1)}

_DIR_WORDS = {
    'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
    'northeast': 'ne', 'north-east': 'ne', 'northwest': 'nw',
    'north-west': 'nw', 'southeast': 'se', 'south-east': 'se',
    'southwest': 'sw', 'south-west': 'sw',
    'up': 'u', 'upward': 'u', 'down': 'd', 'downward': 'd',
    'inside': 'in', 'enter': 'in', 'outside': 'out', 'exit': 'out',
}

# Dropped when two names are compared as token sets (section 1.3)
_STOP = frozenset(('of', 'in', 'at', 'the', 'a', 'an'))

MAX_NAME = 40
MAX_NOTE = 60
MAX_VIA = 40


# --- names and slugs --------------------------------------------------

def slug(name: str) -> str:
    """Room identity, derived by the proxy and never by the model.

    Models write "The Sunken Gate" one turn and "Sunken Gate" the next;
    slugging kills most of that for nothing. The leading 'location:' is
    a real decoration seen in the field ("Location: The Sunken Sanctum").
    """
    s = (name or '').strip().lower()
    s = re.sub(r'^location\s*:\s*', '', s)
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:MAX_NAME]


def display_name(name: str) -> str:
    """What the model and the player see. Keeps the article.

    '*' and brackets are stripped: a room the model called "The
    **Void**" would otherwise turn into bold markers on the way to the
    C64 (markup.colorize_for_wire), and brackets in a name would read
    as a directive on the way back to the model.
    """
    s = re.sub(r'^\s*location\s*:\s*', '', (name or '').strip(),
               flags=re.IGNORECASE)
    s = re.sub(r'[*\[\]]', '', s)
    return re.sub(r'\s+', ' ', s).strip()[:MAX_NAME]


def short_name(name: str) -> str:
    """Display name without its article - for the dense listings where
    a column of "The " costs more than it says."""
    return re.sub(r'^(the|a|an)\s+', '', name or '', flags=re.IGNORECASE)


def _tokens(text: str) -> frozenset:
    return frozenset(t for t in re.split(r'[^a-z0-9]+', (text or '').lower())
                     if t and t not in _STOP)


def norm_dir(text: str):
    """Canonical direction, or None. None is a normal outcome."""
    t = (text or '').strip().lower().strip('.')
    if t in DIRS:
        return t
    return _DIR_WORDS.get(t)


# A message that is ENTIRELY a movement command. Never load-bearing: it
# only fills a dir that would otherwise be None, because this user plays
# free-form ("sniff deeper", "scream for the guards"), not compass.
_MOVE_RE = re.compile(
    r'^\s*(?:go|walk|run|head|move|travel|climb)?\s*(?:to\s+the\s+)?'
    r'([a-z-]+)\s*$', re.IGNORECASE)


def parse_move(text: str):
    m = _MOVE_RE.match(text or '')
    return norm_dir(m.group(1)) if m else None


# --- the map ----------------------------------------------------------

def new_map() -> dict:
    return {'at': None, 'turn': 0, 'rooms': {}, 'edges': []}


def _next_num(m) -> int:
    """Monotonic and never reused - rooms are never deleted, so max+1
    is enough. `num` is what the player types into '/map 4', so
    stability matters more than tidiness: never renumber."""
    return 1 + max([r.get('num', 0) for r in m['rooms'].values()] or [0])


def _match(m, s: str):
    """The known room for a slug: exact, else an exact token-set match.

    ONE fuzzy repair and only one. Edit distance or substring matching
    would merge "North Tower" into "Tower" and silently destroy the
    geography this feature exists to preserve.
    """
    rooms = m['rooms']
    if s in rooms:
        return s
    want = _tokens(s)
    if not want:
        return None
    for known, room in rooms.items():
        if _tokens(known) == want or _tokens(room.get('name', '')) == want:
            return known
    return None


def _create(m, s: str, name: str, turn: int, visited: bool = True) -> dict:
    room = {'num': _next_num(m), 'name': display_name(name) or s,
            'seen': turn, 'visited': visited}
    m['rooms'][s] = room
    return room


def _neighbours(m, s: str):
    """(other_slug, direction_from_s) over every usable edge. Edges are
    stored ONCE and walked from both ends (section 1.2); `oneway`
    suppresses the reverse traversal."""
    for e in m['edges']:
        if e['a'] == s:
            yield e['b'], e.get('dir')
        elif e['b'] == s and not e.get('oneway'):
            d = e.get('dir')
            yield e['a'], (OPPOSITE.get(d) if d else None)


def _find_edge(m, a: str, b: str):
    for e in m['edges']:
        if (e['a'] == a and e['b'] == b) or (e['a'] == b and e['b'] == a):
            return e
    return None


def _room_in_dir(m, src: str, d: str):
    for other, od in _neighbours(m, src):
        if od and od == d:
            return other
    return None


def dir_from(m, e, s: str):
    """An edge's direction as seen from one of its ends."""
    d = e.get('dir')
    if not d:
        return None
    return d if e['a'] == s else OPPOSITE.get(d)


def _add_edge(m, a, b, d, via, oneway, log):
    if d:
        other = _room_in_dir(m, a, d)
        if other and other != b:
            # Keep the first, drop the new direction. Do NOT correct the
            # model: next turn's injected block restates the truth, and a
            # nag every turn buys nothing.
            log.append("%s already lies %s of %s; edge to %s kept with "
                       "no direction" % (other, d, a, b))
            d = None
    via = (via or '').strip()[:MAX_VIA] or None
    e = _find_edge(m, a, b)
    if e is None:
        m['edges'].append({'a': a, 'b': b, 'dir': d, 'via': via,
                           'oneway': bool(oneway)})
        log.append("edge %s -%s-> %s" % (a, d or '?', b))
        return
    # Upgrade in place: fill a null field, never overwrite a known one.
    if d and not e.get('dir'):
        e['dir'] = d if e['a'] == a else OPPOSITE.get(d)
        log.append("edge %s-%s learned direction %s" % (e['a'], e['b'], d))
    if via and not e.get('via'):
        e['via'] = via
    if oneway:
        e['oneway'] = True


def parse_directive(value: str) -> dict:
    """'dir=n | via=the iron door' -> {'dir': 'n', 'via': '...'}.

    Case-insensitive keys, trimmed values, unknown keys ignored.
    """
    out = {}
    for part in (value or '').split('|'):
        key, sep, val = part.partition('=')
        key = key.strip().lower()
        if sep and key:
            out[key] = val.strip()
    return out


def _apply_fields(m, at, d, log):
    """exits= and note= describe the room you are IN. They never move
    you, and they are the only part of a directive that survives a
    directive with nowhere to go."""
    room = m['rooms'].get(at)
    if room is None:
        return
    if 'exits' in d:
        ex = []
        for tok in re.split(r'[,;/\s]+', d['exits']):
            v = norm_dir(tok)
            if v and v not in ex:
                ex.append(v)
        if ex:
            room['exits'] = ex
    if d.get('note'):
        room['note'] = d['note'][:MAX_NOTE]


def ingest(m, *, location=None, directives=(), player_text='') -> list:
    """Fold one assistant reply into the map. Returns log lines for the
    caller to log - keeping the logger out of here is what makes
    rejection assertable in tests.

    Called once per reply, after it completes: both signals are in hand
    by then, so ordering WITHIN the reply is irrelevant and the model
    may put [[MAP:]] anywhere.
    """
    log = []
    m['turn'] = int(m.get('turn') or 0) + 1
    turn = m['turn']

    steps = []
    for raw in directives or ():
        d = parse_directive(raw)
        if d:
            steps.append(d)
        else:
            log.append("directive ignored (nothing parsed): %.60s" % raw)
    if not steps:
        steps = [{}]        # the location signal alone is a whole move

    typed = parse_move(player_text)

    for d in steps:
        dest_name = d.get('to') or location
        dest = slug(dest_name)
        if not dest:
            # No destination anywhere: leave the map alone, but a bare
            # exits=/note= still describes where we already are.
            _apply_fields(m, m.get('at'), d, log)
            continue

        known = _match(m, dest)
        if known is None:
            _create(m, dest, dest_name, turn)
            log.append("new room %d: %s" % (m['rooms'][dest]['num'],
                                            m['rooms'][dest]['name']))
        else:
            if known != dest:
                # Worth a line of its own: a model that renames a place
                # cosmetically every turn is the failure mode to watch
                # for, and this is where it shows.
                log.append("%s taken as the known room %s" % (dest, known))
            dest = known

        src = m.get('at')
        if d.get('from'):
            asked = _match(m, slug(d['from']))
            if asked is None:
                # A move must start somewhere real; the destination is
                # still true, so drop the edge and keep the move.
                log.append("unknown from=%.40s; edge dropped, still "
                           "moving to %s" % (d['from'], dest))
                src = None
            else:
                src = asked

        if src and src != dest:
            d_raw = d.get('dir')
            step = norm_dir(d_raw) if d_raw else None
            if d_raw and step is None:
                # Never discard geography over a vocabulary slip.
                log.append("unknown dir=%.20s; edge kept with no "
                           "direction" % d_raw)
            if step is None and typed:
                step = typed
                log.append("direction %s taken from the player's command"
                           % step)
            _add_edge(m, src, dest, step, d.get('via'),
                      d.get('oneway') not in (None, '', '0', 'false'), log)
        elif d.get('dir') and src == dest:
            log.append("dir given but the location did not change; "
                       "describing, not moving")

        m['at'] = dest
        room = m['rooms'][dest]
        room['seen'] = turn
        room['visited'] = True
        _apply_fields(m, dest, d, log)
        # Every turn, moved or not. This line IS the phase-1 playtest
        # (docs/10 section 0.1): per-turn stability of `location` is
        # inferred, not observed, and the only way to observe it is to
        # log what arrived and read the log.
        log.append("turn %d: location %r -> at %s (%d rooms)"
                   % (turn, location, dest, len(m['rooms'])))

    return log


# --- routing ----------------------------------------------------------

def _bfs(m, start: str) -> dict:
    """slug -> (distance, previous_slug, direction_taken)."""
    seen = {start: (0, None, None)}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        dist = seen[cur][0]
        for other, d in sorted(
                _neighbours(m, cur),
                key=lambda t: m['rooms'].get(t[0], {}).get('num', 0)):
            if other in seen or other not in m['rooms']:
                continue
            seen[other] = (dist + 1, cur, d)
            queue.append(other)
    return seen


def _path(m, dest: str, start=None):
    """[(direction_or_None, slug), ...] from `start` to `dest`, or None
    if there is no way through. Empty list = you are already there."""
    start = start or m.get('at')
    if not start or start not in m['rooms'] or dest not in m['rooms']:
        return None
    seen = _bfs(m, start)
    if dest not in seen:
        return None
    out = []
    cur = dest
    while cur != start:
        _, prev, d = seen[cur]
        out.append((d, cur))
        cur = prev
    out.reverse()
    return out


def route(m, dest_slug: str):
    """The directions to walk from where you are to dest_slug, '?' where
    the direction was never learned. None if unreachable."""
    p = _path(m, dest_slug)
    return None if p is None else [d or '?' for d, _ in p]


def find_room(m, query: str):
    """A room by '/map 4' number or by name. Fuzzier than _match on
    purpose: this is a player's query, not room identity."""
    q = (query or '').strip()
    if not q:
        return None
    rooms = m.get('rooms') or {}
    if q.isdigit():
        n = int(q)
        for s, room in rooms.items():
            if room.get('num') == n:
                return s
    s = slug(q)
    hit = _match(m, s)
    if hit:
        return hit
    want = _tokens(q)
    for known, room in rooms.items():
        name = (room.get('name') or '').lower()
        if q.lower() in name or (want and want <= _tokens(name)):
            return known
    return None


# --- what goes back into the prompt -----------------------------------

def prompt_snippet() -> str:
    """Teaches the directive. Deliberately short: ADVENTURE_PROMPT is
    already long and every instruction competes for compliance."""
    return (
        "\nMap: when the player MOVES to a different place, add "
        "[[MAP: dir=n | via=through the iron door | exits=n,e,w]] to that "
        "reply - dir is the compass direction they went (n s e w ne nw se "
        "sw u d in out), via is a short phrase for how, exits lists every "
        "way out of the place they have ARRIVED in. Omit any field you "
        "are unsure of. The player never sees this. Keep the \"location\" "
        "in your STATE block exactly consistent with the place you just "
        "described - that is what the map is keyed on."
    )


def _exit_list(m, s: str):
    """[(direction_or_None, other_slug, via)] out of a room, in a stable
    order: known directions in vocabulary order, then the unknown."""
    out = []
    for e in m['edges']:
        if e['a'] == s:
            other = e['b']
        elif e['b'] == s and not e.get('oneway'):
            other = e['a']
        else:
            continue
        if other in m['rooms']:
            out.append((dir_from(m, e, s), other, e.get('via')))
    return sorted(out, key=lambda t: (DIRS.index(t[0]) if t[0] in DIRS
                                      else len(DIRS),
                                      m['rooms'][t[1]]['num']))


def unexplored(m, s: str):
    """What the model SAYS leads out of a room, minus the edges we
    know: the single line that makes the map feel like a real text
    adventure."""
    room = m['rooms'].get(s) or {}
    known = {d for d, _, _ in _exit_list(m, s) if d}
    return [d for d in (room.get('exits') or []) if d not in known]


def prompt_block(m, budget: int = 2000) -> str:
    """The map as the model sees it. Display names only - the model
    never sees a slug, and showing it two names for one room invites it
    to use the wrong one."""
    rooms = (m or {}).get('rooms') or {}
    at = (m or {}).get('at')
    if not rooms or at not in rooms:
        return ''

    head = ["MAP - %d place%s known. You are at: %s."
            % (len(rooms), '' if len(rooms) == 1 else 's',
               rooms[at]['name'])]

    exits = _exit_list(m, at)
    if exits:
        head.append("Exits from here: " + "; ".join(
            "%s -> %s%s" % (d or '?', rooms[o]['name'],
                            " (%s)" % via if via else '')
            for d, o, via in exits) + ".")
    else:
        head.append("Exits from here: none discovered yet.")
    un = unexplored(m, at)
    if un:
        head.append("Unexplored from here: " + ", ".join(un) + ".")
    note = rooms[at].get('note')
    if note:
        head.append("Note: " + note)

    # Routes: models are poor at graph traversal and good at reading a
    # table, and "how do I get back to the gate" is the requirement.
    routes = ''
    if len(rooms) > 4:
        reach = _bfs(m, at)
        near = sorted((v[0], rooms[s]['num'], s)
                      for s, v in reach.items() if s != at)[:6]
        parts = []
        for _, _, s in near:
            p = _path(m, s)
            if not p:
                continue
            parts.append("%s = %s" % (
                rooms[s]['name'],
                " then ".join(d or ("to " + short_name(rooms[x]['name']))
                              for d, x in p)))
        if parts:
            routes = "Routes from here: " + "; ".join(parts)

    # Known places, most-recently-seen first, until the budget bites.
    # The head and the routes are never dropped, so they are spent
    # before anything is offered a place - along with room for the
    # "(+N older places not shown)" line the truncation owes the model.
    used = (len("\n".join(head)) + (len(routes) + 1 if routes else 0)
            + len("\nKnown places: ") + 32)
    others = sorted((s for s in rooms if s != at),
                    key=lambda s: (-(rooms[s].get('seen') or 0),
                                   rooms[s]['num']))
    shown = []
    for s in others:
        entry = "%s: %s" % (
            rooms[s]['name'],
            ", ".join("%s>%s" % (d or '?', short_name(rooms[o]['name']))
                      for d, o, _ in _exit_list(m, s)) or "no known exits")
        if used + len(entry) + 3 > budget:
            break
        used += len(entry) + 3
        shown.append(entry)
    dropped = len(others) - len(shown)

    out = "\n".join(head)
    if shown:
        out += "\nKnown places: " + " | ".join(shown)
    if dropped:
        # Nothing is deleted - this is a rendering decision only. The
        # proxy still routes through what it is not showing.
        out += "\n(+%d older place%s not shown)" % (
            dropped, '' if dropped == 1 else 's')
    if routes:
        out += "\n" + routes
    return out


# --- the drawn map ----------------------------------------------------

GRID_W = 8      # characters per grid column
GRID_H = 2      # text rows per grid row

# Candidate offsets when a cell is taken, nearest first (section 5.2).
_SPIRAL = sorted(((dx, dy) for dx in range(-3, 4) for dy in range(-3, 4)
                  if abs(dx) + abs(dy) <= 3),
                 key=lambda t: (abs(t[0]) + abs(t[1]), t[1], t[0]))


def layout(m) -> dict:
    """slug -> (gx, gy). Deterministic, integer-only, no cleverness: BFS
    from where you are, neighbours in `num` order so the output is
    stable and testable. A room that cannot be placed is simply absent -
    the legend is complete and does not need it."""
    rooms = (m or {}).get('rooms') or {}
    if not rooms:
        return {}
    start = m.get('at')
    if start not in rooms:
        start = min(rooms, key=lambda s: rooms[s]['num'])
    pos = {start: (0, 0)}
    taken = {(0, 0): start}
    queue = [start]
    seen = {start}
    while queue:
        cur = queue.pop(0)
        cx, cy = pos[cur]
        for other, d in sorted(
                _neighbours(m, cur),
                key=lambda t: rooms.get(t[0], {}).get('num', 0)):
            if other in seen or other not in rooms:
                continue
            seen.add(other)
            dx, dy = DELTA.get(d or '', (0, 0))
            spot = None
            for ox, oy in _SPIRAL:
                p = (cx + dx + ox, cy + dy + oy)
                if p not in taken:
                    spot = p
                    break
            if spot is None:
                continue
            taken[spot] = other
            pos[other] = spot
            queue.append(other)
    # Normalise so min x and min y are 0
    minx = min(p[0] for p in pos.values())
    miny = min(p[1] for p in pos.values())
    return {s: (x - minx, y - miny) for s, (x, y) in pos.items()}


def _label(room) -> str:
    """Unvisited rooms - seeded from the prep notes, never entered -
    render parenthesised."""
    return ("[%d]" if room.get('visited', True) else "(%d)") % room['num']


def _draw(m, pos, cols, rows, at):
    """The picture. Returns (lines, hidden_count)."""
    rooms = m['rooms']
    if not pos:
        return [], 0
    # Window: centred on where you are, so the crop keeps the part of
    # the map the player is actually standing in.
    ax, ay = pos.get(at, (0, 0))
    x0 = max(0, min(ax - cols // 2,
                    max(p[0] for p in pos.values()) - cols + 1))
    y0 = max(0, min(ay - rows // 2,
                    max(p[1] for p in pos.values()) - rows + 1))
    view = {s: (x - x0, y - y0) for s, (x, y) in pos.items()
            if x0 <= x < x0 + cols and y0 <= y < y0 + rows}
    hidden = len(rooms) - len(view)
    if not view:
        return [], hidden

    gh = max(p[1] for p in view.values()) + 1
    gw = max(p[0] for p in view.values()) + 1
    grid = [[' '] * (gw * GRID_W) for _ in range(gh * GRID_H - 1)]

    for s, (gx, gy) in view.items():
        text = _label(rooms[s])
        for i, ch in enumerate(text):
            col = gx * GRID_W + i
            if col < len(grid[0]):
                grid[gy * GRID_H][col] = ch

    # A connector is drawn ONLY when the edge's direction matches the
    # actual placement. Drawing a horizontal line for a "down" edge is a
    # lie, and a map that lies is worse than a map that is sparse.
    for e in m['edges']:
        a, b = e['a'], e['b']
        if a not in view or b not in view:
            continue
        d = e.get('dir')
        if d in ('e', 'w'):
            west, east = (a, b) if d == 'e' else (b, a)
            wx, wy = view[west]
            ex, ey = view[east]
            if wy != ey or ex != wx + 1:
                continue
            start = wx * GRID_W + len(_label(rooms[west]))
            for col in range(start, ex * GRID_W):
                if grid[wy * GRID_H][col] == ' ':
                    grid[wy * GRID_H][col] = '-'
        elif d in ('n', 's'):
            north, south = (a, b) if d == 's' else (b, a)
            nx, ny = view[north]
            sx, sy = view[south]
            if nx != sx or sy != ny + 1:
                continue
            row = ny * GRID_H + 1
            if grid[row][nx * GRID_W + 1] == ' ':
                grid[row][nx * GRID_W + 1] = '|'

    return ["".join(r).rstrip() for r in grid], hidden


def render_ascii(m, width: int = 78, rows: int = None) -> list:
    """The whole /map screen, without colour tags - the caller adds
    those (section 5.3). The LEGEND is the truth and the picture is
    ornament, so if the drawing is ever wrong nothing important is lost.

    Every line is at most `width` - 1 characters: the caller's colour
    tag owns the first column, which is also what stops the client
    dropping the leading spaces of the art.
    """
    rooms = (m or {}).get('rooms') or {}
    if not rooms:
        return []
    inner = width - 1
    cols = max(1, inner // GRID_W)
    rows = rows or 8
    at = m.get('at') if m.get('at') in rooms else None

    out = ["THE MAP - %d place%s%s" % (
        len(rooms), '' if len(rooms) == 1 else 's',
        ", you are at %d" % rooms[at]['num'] if at else '')]

    pos = layout(m)
    art, hidden = _draw(m, pos, cols, rows, at)
    if art:
        out.append("")
        out.extend(line[:inner] for line in art)
        if hidden:
            out.append("(+%d place%s off this view)"
                       % (hidden, '' if hidden == 1 else 's'))
    out.append("")

    for s in sorted(rooms, key=lambda s: rooms[s]['num']):
        room = rooms[s]
        exits = "  ".join("%s>%d" % (d or '?', rooms[o]['num'])
                          for d, o, _ in _exit_list(m, s))
        un = unexplored(m, s)
        if un:
            exits += "  (%s unexplored)" % ",".join(un)
        # Unvisited rooms - seeded from the prep notes, never entered -
        # are parenthesised here exactly as they are in the picture.
        tag = "%d" % room['num'] if room.get('visited', True) \
            else "(%d)" % room['num']
        name = room['name'][:34]
        if s == at:
            out.append("%s %s  <- you are here" % (tag, name))
            if exits:
                out.append("    " + exits[:inner - 4])
        else:
            out.append((("%s %s" % (tag, name)).ljust(26) + " "
                        + exits).rstrip()[:inner])

    # The footer teaches the one command that answers "how do I get
    # back": the farthest place is the one worth asking about.
    if at and len(rooms) > 1:
        reach = _bfs(m, at)
        far = sorted(((v[0], -rooms[s]['num'], s)
                      for s, v in reach.items() if s != at), reverse=True)
        target = far[0][2] if far else None
        if target:
            out.append("")
            out.append("/map %d - how to get to %s"
                       % (rooms[target]['num'], rooms[target]['name'][:34]))
    return [line[:inner] for line in out]


# --- seeding from the prep notes --------------------------------------

def seed_from_notes(m, text: str) -> int:
    """Parse the machine-readable tail PREP_SYSTEM asks for:

        MAP:
        - The Flooded Nave | n: The Choir Stair | e: The Salt Cloister

    Rooms land unvisited. Best-effort and SILENT on failure: a bad
    parse must never block an adventure starting.
    """
    added = 0
    try:
        body = re.split(r'^\s*MAP\s*:\s*$', text or '',
                        flags=re.IGNORECASE | re.MULTILINE)
        if len(body) < 2:
            return 0
        pending = []
        for raw in body[-1].splitlines():
            line = raw.strip()
            if not line:
                continue
            if not line.startswith(('-', '*')):
                if pending:
                    break       # the section has ended
                continue
            parts = [p.strip() for p in line.lstrip('-* ').split('|')]
            if not parts or not parts[0]:
                continue
            here = slug(parts[0])
            if not here:
                continue
            if _match(m, here) is None:
                _create(m, here, parts[0], m.get('turn') or 0,
                        visited=False)
                added += 1
            else:
                here = _match(m, here)
            for link in parts[1:]:
                d, sep, dest = link.partition(':')
                if not sep:
                    continue
                pending.append((here, norm_dir(d), dest.strip()))
        # Edges last: a link may name a place listed further down.
        for here, d, dest in pending:
            s = slug(dest)
            if not s:
                continue
            known = _match(m, s)
            if known is None:
                _create(m, s, dest, m.get('turn') or 0, visited=False)
                added += 1
                known = s
            if known != here:
                _add_edge(m, here, known, d, None, False, [])
    except Exception:
        return added
    return added
