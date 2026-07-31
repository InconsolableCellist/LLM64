#!/usr/bin/env python3
"""The two sheet frames a windowed client draws from, and the staleness
stamp that goes with them.

CHAR_SHEET (0x6B) carries the half of the character the PROXY owns -
chargen rolled it, so race, class and the ability scores are not the
narrator's to restate. MAP_DATA (0x6C) carries the map as structure
rather than as the ASCII art /print and the C64 get. Both are
capability-gated, both are budgeted, and both are wire contracts with a
16-bit client that will never be recompiled against this code: field
order, separators and the flag bits are what this file pins.

ProtocolHandler is built without __init__ (it wants a socket, an
api_client and a conversation store); send_message and the conversation
meta are recorders.

Run: .venv/bin/python tests/test_sheetwire.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.protocol import ProtocolHandler, MessageType
from src.profiles import (from_hello, WIN16, C64,
                          CAP_CHAR_SHEET, CAP_MAP_DATA, CAP_STATE_JSON)
from src.music import MusicDirectiveFilter
from src import advmap, printdoc

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


class FakeConv:
    """Just the meta store, which is all these paths touch."""

    def __init__(self, meta=None, msgs=None):
        self.meta = dict(meta or {})
        self.msgs = list(msgs or [])
        self.saves = 0

    def get_messages(self):
        return self.msgs

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def save(self):
        self.saves += 1


class FakeMode:
    name = 'adventure'


def handler(profile, meta=None, mode='adventure', msgs=None):
    h = ProtocolHandler.__new__(ProtocolHandler)
    h.profile = profile
    h.conv_manager = FakeConv(meta, msgs)
    h.mode = FakeMode()
    h.mode.name = mode
    h.sent = []

    import logging
    h.logger = logging.getLogger('test')

    async def send_message(mtype, data):
        h.sent.append((mtype, data))

    h.send_message = send_message
    return h


def hello(caps, name=b'win16'):
    return bytes([1, 80, 0, 8, caps & 0xFF, caps >> 8, len(name)]) + name


WIN = from_hello(hello(CAP_CHAR_SHEET | CAP_MAP_DATA | CAP_STATE_JSON))[0]

# --- the capability gate ----------------------------------------------

check("claiming the bit turns the sheet on", WIN.char_sheet, True)
check("...and the map", WIN.map_data, True)
check("a client that claims neither gets neither",
      (from_hello(hello(CAP_STATE_JSON))[0].char_sheet,
       from_hello(hello(CAP_STATE_JSON))[0].map_data), (False, False))
check("the table row never grants them",
      (WIN16.char_sheet, WIN16.map_data, C64.char_sheet, C64.map_data),
      (False, False, False, False))

SHEET = {
    'name': 'Kob Ashfoot', 'race': 'Kobold', 'class': 'Rogue',
    'scores': {'STR': 9, 'DEX': 16, 'CON': 11, 'INT': 13, 'WIS': 10,
               'CHA': 12},
    'skills': ['Stealth', 'Locks'], 'spells': [],
    'gear': ['dagger', 'lantern'], 'hit_die': 8,
}

h = handler(C64, {'character_sheet': SHEET})
asyncio.run(h._send_char_sheet())
check("a c64 is sent no CHAR_SHEET frame", h.sent, [])
h = handler(C64, {'adv_map': {'at': 'hall', 'turn': 3, 'rooms': {}, 'edges': []}})
asyncio.run(h._send_map_data())
check("...and no MAP_DATA frame", h.sent, [])

# --- CHAR_SHEET -------------------------------------------------------

h = handler(WIN, {'character_sheet': SHEET})
asyncio.run(h._send_char_sheet())
check("one CHAR_SHEET frame", [t for t, _ in h.sent],
      [MessageType.CHAR_SHEET])
data = h.sent[0][1]
check("NUL-terminated", data[-1], 0)
obj = json.loads(data[:-1].decode('ascii'))
check("the keys the client parses",
      sorted(obj), ['abil', 'class', 'gear', 'hd', 'name', 'race',
                    'skills', 'spells'])
check("scores are flattened to one drawable line",
      obj['abil'], "STR 9  DEX 16  CON 11  INT 13  WIS 10  CHA 12")
check("name, race and class travel as themselves",
      (obj['name'], obj['race'], obj['class']),
      ('Kob Ashfoot', 'Kobold', 'Rogue'))
check("lists stay lists", (obj['skills'], obj['spells'], obj['gear']),
      (['Stealth', 'Locks'], [], ['dagger', 'lantern']))
check("the hit die rides along", obj['hd'], 8)
check("depth 1 only - nothing nested",
      any(isinstance(v, dict) for v in obj.values()), False)

# No adventure: an empty object, which is what clears the window.
h = handler(WIN, {})
asyncio.run(h._send_char_sheet())
check("no character means an empty object", h.sent[0][1], b'{}\x00')

# The budget. A setup with a hundred items gives back kit first, then
# spells, then skills - and never overflows the frame.
big = dict(SHEET)
big['gear'] = [f"item number {i}" for i in range(120)]
big['spells'] = [f"spell {i}" for i in range(40)]
h = handler(WIN, {'character_sheet': big})
asyncio.run(h._send_char_sheet())
body = h.sent[0][1][:-1].decode('ascii')
check("the sheet stays inside its budget",
      len(body) + 1 <= ProtocolHandler.CHAR_SHEET_MAX, True)
trimmed = json.loads(body)
check("...by trimming kit before skills",
      (len(trimmed['gear']) < 120, trimmed['skills']),
      (True, ['Stealth', 'Locks']))
check("...and it is still valid JSON with the identity intact",
      trimmed['name'], 'Kob Ashfoot')

# --- MAP_DATA ---------------------------------------------------------

# A hand-built map: three rooms in a row, one of them only heard about,
# one edge with no known direction, one one-way.
MAP = {
    'at': 'hall',
    'turn': 7,
    'rooms': {
        'hall':   {'num': 1, 'name': 'The Hall',   'visited': True},
        'cellar': {'num': 2, 'name': 'Cellar',     'visited': True},
        'tower':  {'num': 3, 'name': 'North Tower', 'visited': False},
    },
    'edges': [
        {'a': 'hall', 'b': 'cellar', 'dir': 'd', 'via': None,
         'oneway': False},
        {'a': 'hall', 'b': 'tower', 'dir': None, 'via': None,
         'oneway': True},
    ],
}

h = handler(WIN, {'adv_map': MAP})
asyncio.run(h._send_map_data())
check("one MAP_DATA frame", [t for t, _ in h.sent], [MessageType.MAP_DATA])
raw = h.sent[0][1]
check("NUL-terminated", raw[-1], 0)
text = raw[:-1].decode('ascii')
lines = [ln for ln in text.split('\n') if ln]

check("the header comes first and carries the turn",
      lines[0].split('\t')[0], 'M7')
pos = advmap.layout(MAP)
cols = max(p[0] for p in pos.values()) + 1
rows = max(p[1] for p in pos.values()) + 1
check("...and the grid extents", lines[0], f"M7\t{cols}\t{rows}")

rooms = {}
for ln in lines:
    if ln[0] != 'R':
        continue
    f = ln[1:].split('\t')
    rooms[int(f[0])] = f
check("every room is on the wire", sorted(rooms), [1, 2, 3])
check("you are here is flagged, and visited with it",
      rooms[1][3], '3')
check("a visited room you are not in is 1", rooms[2][3], '1')
check("somewhere only heard about is 0", rooms[3][3], '0')
check("names travel, capped at 20 characters",
      (rooms[1][4], rooms[3][4]), ('The Hall', 'North Tower'))

edges = [ln[1:].split('\t') for ln in lines if ln[0] == 'E']
check("both edges travel", len(edges), 2)
byends = {(e[0], e[1]): e for e in edges}
check("a known direction travels as one letter",
      byends[('1', '2')][2], 'd')
check("an unknown one travels as a dash", byends[('1', '3')][2], '-')
check("one-way is flag bit 0",
      (byends[('1', '2')][3], byends[('1', '3')][3]), ('0', '1'))
check("no X line when everything fits",
      [ln for ln in lines if ln[0] == 'X'], [])

# Not in adventure mode: an empty map, so the window clears rather than
# keeping the last game's geography.
h = handler(WIN, {'adv_map': MAP}, mode='chat')
asyncio.run(h._send_map_data())
check("chat mode sends an empty map", h.sent[0][1], b"M0\t0\t0\n\x00")

# The budget: fifty rooms in a line cannot fit, and the ones that are
# dropped are the ones FARTHEST from the player - with a count, so the
# window can say so.
big_rooms = {}
big_edges = []
for i in range(1, 51):
    slug = f"room{i}"
    big_rooms[slug] = {'num': i, 'name': f"Chamber {i} of the Deep",
                       'visited': True}
    if i > 1:
        big_edges.append({'a': f"room{i - 1}", 'b': slug, 'dir': 'e',
                          'via': None, 'oneway': False})
BIG = {'at': 'room1', 'turn': 40, 'rooms': big_rooms, 'edges': big_edges}
h = handler(WIN, {'adv_map': BIG})
asyncio.run(h._send_map_data())
text = h.sent[0][1][:-1].decode('ascii')
lines = [ln for ln in text.split('\n') if ln]
nums = sorted(int(ln[1:].split('\t')[0]) for ln in lines if ln[0] == 'R')
hidden = [ln for ln in lines if ln[0] == 'X']
check("the map stays inside its budget",
      len(text) + 1 <= ProtocolHandler.MAP_DATA_MAX + 8, True)
check("it did not fit, and says how much did not", len(hidden), 1)
check("the count matches what was dropped",
      int(hidden[0][1:]), 50 - len(nums))
check("what survives is the near geography",
      nums == list(range(1, len(nums) + 1)), True)
check("no edge dangles off a dropped room",
      [e for e in (ln[1:].split('\t') for ln in lines if ln[0] == 'E')
       if int(e[0]) not in nums or int(e[1]) not in nums], [])

# --- the staleness stamp ----------------------------------------------

STATE = '{"hp":12,"maxhp":20,"location":"The Hall"}'

h = handler(WIN, {'adv_map': {'turn': 9, 'rooms': {}, 'edges': []},
                  'adv_state': STATE, 'adv_state_turn': 9})
asyncio.run(h._send_state_json(STATE))
obj = json.loads(h.sent[0][1][:-1].decode('ascii'))
check("a fresh block is nought turns old", obj['_age'], 0)
check("...and the narrator's own fields are untouched",
      (obj['hp'], obj['maxhp'], obj['location']), (12, 20, 'The Hall'))

# Three turns where the narrator gave us nothing: the block is the same,
# the age is not.
h = handler(WIN, {'adv_map': {'turn': 12, 'rooms': {}, 'edges': []},
                  'adv_state': STATE, 'adv_state_turn': 9})
asyncio.run(h._send_state_json(STATE))
obj = json.loads(h.sent[0][1][:-1].decode('ascii'))
check("a stale block reports its age", obj['_age'], 3)
check("meta itself is never stamped",
      '_age' in h.conv_manager.get_meta('adv_state'), False)

# No stamp recorded yet (a conversation from before this existed): no
# claim about age rather than a wrong one.
h = handler(WIN, {'adv_map': {'turn': 4, 'rooms': {}, 'edges': []}})
asyncio.run(h._send_state_json(STATE))
check("no stamp means no _age key",
      '_age' in h.sent[0][1].decode('ascii'), False)

# An empty object stays empty - it is what clears the windows.
h = handler(WIN, {'adv_state_turn': 2,
                  'adv_map': {'turn': 5, 'rooms': {}, 'edges': []}})
asyncio.run(h._send_state_json('{}'))
check("the clearing frame is not decorated", h.sent[0][1], b'{}\x00')

# --- the free refresh -------------------------------------------------

h = handler(WIN, {'character_sheet': SHEET, 'adv_state': STATE,
                  'adv_state_turn': 3, 'adv_map': MAP})
asyncio.run(h.handle_get_sheet())
check("GET_SHEET answers with all three, out of storage alone",
      [t for t, _ in h.sent],
      [MessageType.CHAR_SHEET, MessageType.STATE_JSON,
       MessageType.MAP_DATA])

# --- the hold that used to eat long state blocks ----------------------

# A twelve-item inventory with a described companion: 600 characters was
# not enough, and the failure printed raw JSON at the player.
inv = [f"a well-made item number {i}" for i in range(12)]
block = json.dumps({
    'hp': 12, 'maxhp': 20, 'mana': 0, 'maxmana': 0, 'ac': 14, 'level': 2,
    'xp': 120, 'gold': 3, 'score': 0, 'location': 'The Fungal Vault',
    'effects': ['poisoned'], 'inventory': inv,
    'appearance': 'a soot-streaked kobold in a patched leather coat',
    'companions': ['Wren, a tall archer in green'],
}, separators=(',', ':'))
check("the block this exercises really is over the old limit",
      len(block) > 600, True)
f = MusicDirectiveFilter()
shown = f.feed(f"You step into the vault.\n[[STATE: {block}]]")
shown += f.flush()
check("a long state block is captured", len(f.states), 1)
check("...and none of it leaks onto the screen",
      'hp' in shown or '{' in shown, False)
check("...and it parses", json.loads(f.states[0].strip())['xp'], 120)

# --- back-filling the static half -------------------------------------
#
# An adventure started with /adventure <theme> never ran chargen, so
# 'character_sheet' meta is absent and the state block is forbidden to
# carry those fields. This is the one path that can ever fill them.

STORY = [
    {'role': 'user', 'content': 'i push the vestry door'},
    {'role': 'assistant',
     'content': 'Kesh, half-elf and wizard, shoulders it open. Her oak '
                'staff throws light across the flooded nave.'},
]


def backfill_handler(answer, meta=None, msgs=STORY, mode='adventure'):
    h = handler(WIN, meta, mode=mode, msgs=msgs)
    h.asked = []
    h.status = []

    async def ask(question, **kw):
        h.asked.append(question)
        return answer

    async def status(text):
        h.status.append(text)

    h._ask_model = ask
    h.send_status = status
    h.mode.character = ''
    h.mode.background = ''
    return h


GOOD = ('{"name":"Kesh","race":"Half-elf","class":"Wizard",'
        '"skills":["Lore"],"spells":["Light"],"gear":["oak staff"]}')

h = backfill_handler(GOOD)
check("a missing sheet is back-filled",
      asyncio.run(h._backfill_character_sheet()), True)
check("...with one model call", len(h.asked), 1)
check("...that carries the transcript", 'half-elf and wizard' in h.asked[0],
      True)
check("...and it is stored", h.conv_manager.meta['character_sheet']['name'],
      'Kesh')
check("...marked as told, not rolled",
      h.conv_manager.meta['character_sheet']['derived'], True)
check("...with no scores it never rolled",
      h.conv_manager.meta['character_sheet']['scores'], {})
check("...and a CHAR_SHEET frame goes out",
      [t for t, _ in h.sent], [MessageType.CHAR_SHEET])
check("the derived flag never reaches the wire",
      'derived' in json.loads(h.sent[0][1][:-1].decode('ascii')), False)

# The prose has to land where a rolled character's would: the illustrator
# reads 'character', the system prompt reads 'background'.
check("the illustrator gets the identity",
      'Kesh' in h.conv_manager.meta['character'], True)
check("...on the mode too", h.mode.character,
      h.conv_manager.meta['character'])
check("the system prompt's head gets it",
      'Kesh' in h.conv_manager.meta['background'], True)
check("...on the mode too", h.mode.background,
      h.conv_manager.meta['background'])
check("it was persisted", h.conv_manager.saves > 0, True)

# Existing prep notes are joined, never replaced.
h = backfill_handler(GOOD, meta={'background': 'Your prep notes: a flood.'})
asyncio.run(h._backfill_character_sheet())
check("prior background survives",
      'a flood.' in h.conv_manager.meta['background'], True)
check("...with the character after it",
      h.conv_manager.meta['background'].endswith(
          h.conv_manager.meta['character']), True)

# A sheet that already exists is never overwritten, and costs nothing.
h = backfill_handler(GOOD, meta={'character_sheet': SHEET})
check("a rolled sheet is left alone",
      asyncio.run(h._backfill_character_sheet()), False)
check("...with no model call", h.asked, [])
check("...and the rolled sheet stands",
      h.conv_manager.meta['character_sheet'], SHEET)

# Nothing to read it off yet.
h = backfill_handler(GOOD, msgs=[])
check("an empty conversation is not guessed at",
      asyncio.run(h._backfill_character_sheet()), False)
check("...with no model call", h.asked, [])

# A useless answer stores NOTHING - so the next /sheet tries again rather
# than finding a meta key and giving up forever.
for bad, label in ((' ', 'an empty answer'),
                   ('I could not say.', 'a prose answer'),
                   ('{"name":"","race":"","class":""}', 'an all-blank answer')):
    h = backfill_handler(bad)
    check(f"{label} stores nothing",
          (asyncio.run(h._backfill_character_sheet()),
           'character_sheet' in h.conv_manager.meta), (False, False))
    check(f"...{label} sends no frame", h.sent, [])

# --- the printed sheet ------------------------------------------------

paper = printdoc.render_sheet(
    '{"hp":9,"maxhp":20,"mana":2,"maxmana":6,"ac":14,"level":3,"xp":420,'
    '"gold":7,"score":5,"location":"The Hall","effects":["poisoned"],'
    '"inventory":["dagger"],"companions":[]}',
    character='The player character is Kob Ashfoot, a Kobold Rogue.')
for want in ('Level 3', 'XP 420', 'AC 14', 'Mana 2/6', 'HP 9/20',
             'Afflictions', 'Kob Ashfoot'):
    if want not in paper:
        failures.append(f"the printed sheet is missing {want!r}")

if failures:
    print(f"\n{len(failures)} FAILURES")
    for f in failures:
        print('- ' + f)
    sys.exit(1)
print("test_sheetwire: all checks passed")
