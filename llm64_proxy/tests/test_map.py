#!/usr/bin/env python3
"""advmap: ingest, routing and layout (docs/10-adventure-map.md).

The case that must never regress is the first one: a location change
with NO directive still creates the room and a dir=null edge. That is
the whole safety argument of the design - the map is built from the
[[STATE]].location signal, which is already shipping and measured
reliable, and the [[MAP:]] directive only decorates it.

Run: python3 tests/test_map.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import advmap
from src.markup import colorize_for_wire
from src.music import MusicDirectiveFilter

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def ok(name, cond, detail=''):
    if not cond:
        failures.append(f"{name}: {detail or 'false'}")


def move(m, location, directive=None, player_text=''):
    """One reply's worth of ingest."""
    return advmap.ingest(m, location=location,
                         directives=(directive,) if directive else (),
                         player_text=player_text)


def nums(m):
    return {s: r['num'] for s, r in m['rooms'].items()}


# --- 1. the load-bearing case: no directive at all --------------------

m = advmap.new_map()
move(m, "The Whispering Woods")
move(m, "The Sunken Gate")
check("no directive: rooms", sorted(m['rooms']),
      ['sunken-gate', 'whispering-woods'])
check("no directive: edge", m['edges'],
      [{'a': 'whispering-woods', 'b': 'sunken-gate', 'dir': None,
        'via': None, 'oneway': False}])
check("no directive: at", m['at'], 'sunken-gate')
check("no directive: turn", m['turn'], 2)
check("no directive: names", m['rooms']['sunken-gate']['name'],
      'The Sunken Gate')

# Every turn leaves a log line naming the location that arrived: that
# line is the instrument the phase-1 playtest reads.
log = move(m, "The Sunken Gate")
ok("every turn is logged", any(x.startswith('turn ') for x in log), log)

# The first room of a game has nowhere to have come from.
m1 = advmap.new_map()
move(m1, "The Cell")
check("first room makes no edge", m1['edges'], [])

# A missing or empty location leaves the map entirely alone.
m2 = advmap.new_map()
move(m2, "The Cell")
before = (dict(m2['rooms']), list(m2['edges']), m2['at'])
move(m2, None)
move(m2, "   ")
check("empty location changes nothing",
      (m2['rooms'], m2['edges'], m2['at']), before)


# --- 2. directives ----------------------------------------------------

m = advmap.new_map()
move(m, "The Whispering Woods", "exits=n,e,d,w | note=a boarded door west")
move(m, "The Sunken Gate", "dir=n | via=through the iron door | exits=s,e")
check("directive edge", m['edges'],
      [{'a': 'whispering-woods', 'b': 'sunken-gate', 'dir': 'n',
        'via': 'through the iron door', 'oneway': False}])
check("exits stored", m['rooms']['whispering-woods']['exits'],
      ['n', 'e', 'd', 'w'])
check("note stored", m['rooms']['whispering-woods']['note'],
      'a boarded door west')
check("unexplored from woods", advmap.unexplored(m, 'whispering-woods'),
      ['e', 'd', 'w'])

# Long-form directions, and a direction the vocabulary does not know.
m = advmap.new_map()
move(m, "Hall")
move(m, "Attic", "dir=upward")
check("long-form dir", m['edges'][0]['dir'], 'u')
log = move(m, "Cellar", "dir=widdershins")
check("bad dir keeps the edge", m['edges'][1]['dir'], None)
ok("bad dir is logged", any('widdershins' in x for x in log), log)

# dir= without a move describes, it does not travel.
m = advmap.new_map()
move(m, "Hall")
move(m, "Hall", "dir=n | exits=n,s")
check("dir without a move makes no edge", m['edges'], [])
check("...but exits still land", m['rooms']['hall']['exits'], ['n', 's'])

# from= naming an unknown room: the edge is dropped, the move is not.
m = advmap.new_map()
move(m, "Hall")
log = move(m, "Crypt", "from=The Sunless Deep | dir=d")
check("unknown from: no edge", m['edges'], [])
check("unknown from: still moved", m['at'], 'crypt')
ok("unknown from is logged", any('from=' in x for x in log), log)

# Two moves in one reply: each subsequent from= defaults to the `at`
# the previous one produced.
m = advmap.new_map()
move(m, "Hall")
advmap.ingest(m, location="Yard",
              directives=("dir=n | to=The Stair", "dir=e | to=The Yard"))
check("two directives: rooms", sorted(m['rooms']), ['hall', 'stair', 'yard'])
check("two directives: at", m['at'], 'yard')
check("two directives: edges",
      [(e['a'], e['dir'], e['b']) for e in m['edges']],
      [('hall', 'n', 'stair'), ('stair', 'e', 'yard')])


# --- 3. the player's typed command as a null-filler --------------------

m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", player_text="go north")
check("typed command fills dir", m['edges'][0]['dir'], 'n')

m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", "dir=e", player_text="n")
check("typed command never overrides", m['edges'][0]['dir'], 'e')

m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", player_text="check the cell door, maybe north is open")
check("free-form play is not a command", m['edges'][0]['dir'], None)


# --- 4. slugs and the one fuzzy repair ---------------------------------

m = advmap.new_map()
move(m, "The Sunken Gate")
move(m, "Sunken Gate")
move(m, "sunken gate")
move(m, "Location: The Sunken Gate")
check("aliases are one room", list(m['rooms']), ['sunken-gate'])
check("aliases make no edges", m['edges'], [])

m = advmap.new_map()
move(m, "The Ruins of the Sunken Gate")
move(m, "Sunken Gate Ruins")
check("token-set match merges", len(m['rooms']), 1)

m = advmap.new_map()
move(m, "The North Tower")
move(m, "The Tower")
check("near-miss does NOT merge", len(m['rooms']), 2)

# num is assigned once and never changes, even as rooms come and go
# from the render.
m = advmap.new_map()
for name in ("Hall", "Yard", "Crypt"):
    move(m, name)
first = nums(m)
move(m, "Hall")
move(m, "Yard")
move(m, "Cellar")
check("num is stable", {k: v for k, v in nums(m).items() if k in first},
      first)
check("num is not reused", nums(m)['cellar'], 4)


# --- 5. edges ---------------------------------------------------------

# A second edge in the same direction from the same room keeps the
# first and stores the new one with no direction.
m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", "dir=n")
move(m, "Hall")
log = move(m, "Crypt", "dir=n")
check("first n edge untouched", m['edges'][0]['dir'], 'n')
check("second n edge has no dir", m['edges'][1]['dir'], None)
check("collision made no extra edge", len(m['edges']), 2)

# ...and the same claim arriving from the FAR end must collide too.
# Field case (conversation 1784706552, 2026-07-22): the model put the
# Iron Corridor west of the Great Hall, was refused the Iron Gateway in
# the same direction, then said "east" while walking back FROM the
# gateway - which is the identical claim seen from the other side. It
# was stored, and the Great Hall ended up with two rooms to its west.
m = advmap.new_map()
move(m, "The Iron Corridor")
move(m, "The Great Hall", "dir=e")            # corridor is w of the hall
move(m, "The Iron Gateway")                   # dir unknown, no conflict
log = move(m, "The Great Hall", "dir=e")      # ...says the gateway is w too
west = [e for e in m['edges']
        if advmap.dir_from(m, e, 'great-hall') == 'w']
check("only one room lies west of the hall", len(west), 1)
ok("the far-end collision is logged",
   any('already lies' in x for x in log), log)
ok("collision is logged", any('already lies' in x for x in log), log)

# An existing edge is upgraded in place, never duplicated, and a known
# field is never overwritten.
m = advmap.new_map()
move(m, "Hall")
move(m, "Yard")                       # dir=null
move(m, "Hall", "dir=s | via=back down the path")
check("no duplicate edge", len(m['edges']), 1)
check("null dir is filled", m['edges'][0]['dir'], 'n')
check("via is filled", m['edges'][0]['via'], 'back down the path')
move(m, "Yard", "dir=e | via=some other way")
check("known dir is not overwritten", m['edges'][0]['dir'], 'n')
check("known via is not overwritten", m['edges'][0]['via'],
      'back down the path')

# Edges are stored once and walked from both ends.
m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", "dir=n")
check("stored once", len(m['edges']), 1)
check("reverse traversal", advmap.route(m, 'hall'), ['s'])

m = advmap.new_map()
move(m, "Hall")
move(m, "Yard", "dir=n | oneway=1")
check("oneway blocks the reverse", advmap.route(m, 'hall'), None)
check("oneway still routes forward", advmap.route(m, 'yard'), [])


# --- 6. routing -------------------------------------------------------

def sixroom():
    """The map from docs/10 section 4."""
    m = advmap.new_map()
    move(m, "The Whispering Woods", "exits=n,e,d,w")
    move(m, "The Sunken Gate", "dir=n | via=through the iron door")
    move(m, "The Tower Entrance", "dir=e")
    move(m, "The Sunken Gate", "dir=w")
    move(m, "The Whispering Woods", "dir=s")
    move(m, "The Sunlit Meadow", "dir=e | via=along the deer track")
    move(m, "The Foothills", "dir=n")
    move(m, "The Sunlit Meadow", "dir=s")
    move(m, "The Whispering Woods", "dir=w")
    move(m, "The Rotted Cellar", "dir=d")
    move(m, "The Whispering Woods", "dir=u")
    return m


m = sixroom()
check("six rooms", len(m['rooms']), 6)
check("route to the foothills", advmap.route(m, 'foothills'), ['e', 'n'])
check("route to the tower", advmap.route(m, 'tower-entrance'), ['n', 'e'])
check("route to here", advmap.route(m, 'whispering-woods'), [])

m['rooms']['island'] = {'num': 99, 'name': 'The Island', 'seen': 0,
                        'visited': False}
check("unreachable routes to None", advmap.route(m, 'island'), None)
check("unknown room routes to None", advmap.route(m, 'nowhere'), None)

# A route over an edge whose direction was never learned still routes;
# the unknown step shows as '?' rather than being hidden.
m2 = advmap.new_map()
move(m2, "Hall")
move(m2, "Yard")
check("route with a null dir", advmap.route(m2, 'hall'), ['?'])

# find_room: by number, by name, by fragment.
m = sixroom()
check("find by number", advmap.find_room(m, '5'), 'foothills')
check("find by name", advmap.find_room(m, 'The Foothills'), 'foothills')
check("find by fragment", advmap.find_room(m, 'foot'), 'foothills')
check("find nothing", advmap.find_room(m, 'zzz'), None)


# --- 7. the prompt block ----------------------------------------------

m = sixroom()
block = advmap.prompt_block(m)
ok("prompt block names the current room",
   "You are at: The Whispering Woods." in block, block)
ok("prompt block has exits", "Exits from here:" in block, block)
ok("prompt block has a via", "(through the iron door)" in block, block)
ok("prompt block routes", "Routes from here:" in block, block)
ok("prompt block never shows a slug",
   'whispering-woods' not in block, block)

ok("a generous budget drops nothing", "not shown)" not in block, block)
tight = advmap.prompt_block(m, budget=len(block) - 120)
ok("budget keeps the head",
   tight.startswith("MAP - 6 places known."), tight)
ok("budget keeps the routes", "Routes from here:" in tight, tight)
ok("budget drops older places", "not shown)" in tight, tight)
ok("budget is honoured", len(tight) <= len(block) - 120,
   f"{len(tight)} vs {len(block) - 120}")
ok("nothing is deleted, only unrendered", len(m['rooms']) == 6, m['rooms'])

# A dir=null edge renders, it is never hidden.
m2 = advmap.new_map()
move(m2, "Hall")
move(m2, "Yard")
ok("null dir renders as ?", "?>" in advmap.prompt_block(m2, budget=2000),
   advmap.prompt_block(m2))

check("no map, no block", advmap.prompt_block(advmap.new_map()), '')


# --- 8. the drawn map --------------------------------------------------

m = sixroom()
lines = advmap.render_ascii(m)
check("layout is deterministic", lines, advmap.render_ascii(sixroom()))

for ln in lines:
    ok("line fits 77 columns", len(ln) <= 77, f"{len(ln)}: {ln!r}")
    ok("no bold markup in the art", '**' not in ln, ln)

# What the client actually receives, one colour tag per line as the
# proxy sends it: the tag costs one column, the close costs one more.
wire = ["[color=cyan]" + ln for ln in lines]
wire[-1] += "[/color]"
for ln in wire:
    cols = len(colorize_for_wire(ln))
    ok("wire line fits 78 columns", cols <= 78, f"{cols}: {ln!r}")
    ok("no line is exactly 80", cols != 80, ln)
ok("exactly one colour run is opened",
   sum(x.count('[color=') for x in wire) == len(wire), wire)
ok("the colour run is closed exactly once",
   sum(x.count('[/color]') for x in wire) == 1, wire)

body = "\n".join(lines)
ok("legend lists every room",
   all(r['name'][:34] in body for r in m['rooms'].values()), body)
ok("you are here is marked", "<- you are here" in body, body)
ok("the footer teaches /map n", "/map " in body, body)

# Connectors are drawn only where the direction matches the placement.
pos = advmap.layout(m)
check("current room anchors the layout",
      pos['whispering-woods'][0] - pos['sunken-gate'][0], 0)
ok("the gate is north of the woods",
   pos['sunken-gate'][1] == pos['whispering-woods'][1] - 1, pos)
ok("the meadow is east of the woods",
   pos['sunlit-meadow'] == (pos['whispering-woods'][0] + 1,
                            pos['whispering-woods'][1]), pos)
ok("a vertical connector is drawn", any('|' in ln for ln in lines), body)
ok("a horizontal connector is drawn", any('-[' in ln for ln in lines), body)

# 'd' has no delta, so the cellar is placed but never connected by a
# drawn line - it lives in the legend, which is complete.
ok("the cellar is in the legend", "The Rotted Cellar" in body, body)

# An unvisited (seeded) room renders parenthesised.
m['rooms']['salt-cloister'] = {'num': 7, 'name': 'The Salt Cloister',
                               'seen': 0, 'visited': False}
ok("unvisited rooms are parenthesised",
   any('(7)' in ln for ln in advmap.render_ascii(m)),
   advmap.render_ascii(m))

check("no rooms, no drawing", advmap.render_ascii(advmap.new_map()), [])

# A map too wide for the window crops around where you are and says so.
wide = advmap.new_map()
move(wide, "Room 0")
for i in range(1, 14):
    move(wide, f"Room {i}", "dir=e")
drawn = advmap.render_ascii(wide, rows=4)
ok("a cropped view says what it dropped",
   any('off this view' in ln for ln in drawn), drawn)
for ln in drawn:
    ok("cropped line fits", len(ln) <= 77, f"{len(ln)}: {ln!r}")


# --- 9. through the real stream filter ---------------------------------

def stream(text, chunked):
    f = MusicDirectiveFilter()
    if chunked:
        out = "".join(f.feed(c) for c in text) + f.flush()
    else:
        out = f.feed(text) + f.flush()
    return out, f


REPLY = ("[HP 15/20 | Gold 0 | The Sunken Gate]\n"
         "You step through the iron door.\n"
         "[[MAP: dir=n | via=through the iron door | exits=s,e]]\n")

for chunked in (False, True):
    how = "chunked" if chunked else "whole"
    out, f = stream(REPLY, chunked)
    check(f"[{how}] MAP is stripped from the text", out,
          "[HP 15/20 | Gold 0 | The Sunken Gate]\n"
          "You step through the iron door.\n\n")
    check(f"[{how}] MAP is captured", f.maps,
          ["dir=n | via=through the iron door | exits=s,e"])

    # The single-bracket shape, which is what the status line teaches.
    out, f = stream("[MAP: dir=e]\nYou go east.", chunked)
    check(f"[{how}] single-bracket MAP", out, "\nYou go east.")
    check(f"[{how}] single-bracket captured", f.maps, ["dir=e"])

# End to end: the filter's output feeds ingest.
_, f = stream(REPLY, True)
m = advmap.new_map()
move(m, "The Whispering Woods")
advmap.ingest(m, location="The Sunken Gate", directives=f.maps)
check("filter -> ingest", [(e['a'], e['dir'], e['b']) for e in m['edges']],
      [('whispering-woods', 'n', 'sunken-gate')])
check("filter -> ingest exits", m['rooms']['sunken-gate']['exits'],
      ['s', 'e'])


# --- 10. seeding from the prep notes -----------------------------------

BIBLE = """The drowned abbey sits in a salt marsh.

MAP:
- The Flooded Nave | n: The Choir Stair | e: The Salt Cloister
- The Choir Stair | u: The Bell Loft
"""

m = advmap.new_map()
check("seeded room count", advmap.seed_from_notes(m, BIBLE), 4)
check("seeded rooms", sorted(m['rooms']),
      ['bell-loft', 'choir-stair', 'flooded-nave', 'salt-cloister'])
ok("seeded rooms are unvisited",
   not any(r['visited'] for r in m['rooms'].values()), m['rooms'])
check("seeded edges",
      sorted((e['a'], e['dir'], e['b']) for e in m['edges']),
      [('choir-stair', 'u', 'bell-loft'),
       ('flooded-nave', 'e', 'salt-cloister'),
       ('flooded-nave', 'n', 'choir-stair')])

# Arriving at a seeded room adopts it rather than duplicating it.
move(m, "The Flooded Nave")
check("arrival adopts the seed", len(m['rooms']), 4)
ok("arrival marks it visited", m['rooms']['flooded-nave']['visited'],
   m['rooms']['flooded-nave'])
check("seeded route", advmap.route(m, 'bell-loft'), ['n', 'u'])

# Notes without the section, and notes that are nonsense, are silent.
check("no MAP section", advmap.seed_from_notes(advmap.new_map(),
                                               "just prose"), 0)
check("empty notes", advmap.seed_from_notes(advmap.new_map(), ''), 0)
check("garbage section",
      advmap.seed_from_notes(advmap.new_map(), "MAP:\n- | | |\n"), 0)


if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all map tests pass")
