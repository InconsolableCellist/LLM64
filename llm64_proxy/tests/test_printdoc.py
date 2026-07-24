#!/usr/bin/env python3
"""printdoc: the document /print puts on paper (docs/14).

What a page looks like is decided entirely on the proxy - the C64 only
opens device 4 and writes bytes - so these assertions pin the layout
exactly rather than pattern-matching it: header, wrap width, the
deterministic character sheet, and the guarantees the client's PETSCII
mapping depends on (pure ASCII, \\n line ends, no directives).

Run: python3 tests/test_printdoc.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import printdoc

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def check_in(name, needle, hay):
    if needle not in hay:
        failures.append(f"{name}: {needle!r} missing from {hay!r:.300}")


STATE = ('{"hp":12,"maxhp":20,"gold":3,"location":"The Vault",'
         '"appearance":"a traveler in tattered linen robes",'
         '"inventory":["rope","an iron key"],"companions":[]}')
CHARACTER = "The player character is Kob, a kobold tinker."

MSGS = [
    {'role': 'user', 'content': 'how do I make the stew?'},
    {'role': 'assistant', 'content': 'Brown the pork, then simmer it.'},
    {'role': 'user', 'content': 'thanks'},
]

# --- the character sheet: exact text, no model involved ---------------

check('sheet', printdoc.render_sheet(STATE, CHARACTER),
      "Location: The Vault\n"
      "HP 12/20   Gold 3\n"
      "\n"
      "Appearance:\n"
      "  a traveler in tattered linen robes\n"
      "\n"
      "Inventory:\n"
      "  - rope\n"
      "  - an iron key\n"
      "\n"
      "Companions:\n"
      "  (alone)\n"
      "\n"
      "Character:\n"
      "  The player character is Kob, a kobold tinker.")

# Stats the adventure does not track are simply absent - the model is
# told to omit them, and a half-empty sheet reads worse than a short one
check('sheet minimal', printdoc.render_sheet('{"hp":5,"location":"Bog"}'),
      "Location: Bog\nHP 5")

# A malformed state block must not take the whole command down: the
# stored JSON is model-written and has arrived broken in the field
check('sheet from junk', printdoc.render_sheet('{"hp":', 'Kob the tinker'),
      "Character:\n  Kob the tinker")
check('sheet from nothing', printdoc.render_sheet(None), '')

# background is the fallback when setup produced no separate sheet
check('sheet background fallback',
      printdoc.render_sheet('{}', '', 'A cold campaign in the north.'),
      "Character:\n  A cold campaign in the north.")

# --- what a bare /print prints ----------------------------------------

check('last reply', printdoc.last_reply(MSGS),
      'Brown the pork, then simmer it.')
check('last reply, none yet', printdoc.last_reply(
    [{'role': 'user', 'content': 'hello'}]), '')
check('last reply, empty conversation', printdoc.last_reply([]), '')

# --- the sheet fast path's trigger ------------------------------------

for arg in ('my inventory', 'the character sheet', 'CHAR', 'stats'):
    if not printdoc.wants_sheet(arg):
        failures.append(f"wants_sheet({arg!r}) should be true")
for arg in ('', 'the complete recipe', 'characteristics of steel'):
    if printdoc.wants_sheet(arg):
        failures.append(f"wants_sheet({arg!r}) should be false")

# The sheet path is DETERMINISTIC - it renders stored state and never
# reads the conversation - so claiming a document request is a silent,
# total failure: the player asked for a history and got a stat block in
# the same second, with no model call (2026-07-24, docs/14 13.13).
# These name a character AND ask for a document; the document wins.
for arg in ('summary of the story with critical character details',
            'a detailed history of my character',
            'the story so far and my stats',
            'an account of what happened to the characters',
            'a recap with character notes',
            'the adventure log with inventory changes'):
    if printdoc.wants_sheet(arg):
        failures.append(f"wants_sheet({arg!r}) must not take the fast path")
# ...while the plain asks still do, or the fast path is pointless
for arg in ('my inventory', 'character sheet', 'char', 'my stats',
            'character info', 'the sheet'):
    if not printdoc.wants_sheet(arg):
        failures.append(f"wants_sheet({arg!r}) should still be true")

# --- how much conversation the composer sees -------------------------

# A fixed message count was the bug: "a detailed history" of a long
# adventure saw six turns of it (13.13). The budget is characters, from
# the model's context window.
many = [{'role': 'user' if i % 2 == 0 else 'assistant',
         'content': f'turn {i} ' + 'x' * 90} for i in range(200)]
big = printdoc.transcript(many, 1_000_000)
check('a large budget reads the whole conversation',
      big.count('\n') + 1, 200)
check('and keeps it in order', big.startswith('user: turn 0'), True)

small = printdoc.transcript(many, 1000)
check('a small budget keeps the NEWEST turns',
      small.strip().endswith('x' * 90) and 'turn 199' in small, True)
if 'turn 0 ' in small:
    failures.append('a small budget should have dropped the oldest turns')
check('the budget is respected', len(small) <= 1000, True)

# Whole messages only - half a turn reads as the model being confused
for line in printdoc.transcript(many, 1000).split('\n'):
    if line and not line.startswith(('user: ', 'assistant: ')):
        failures.append(f'transcript cut a message in half: {line[:40]!r}')

# One message longer than the whole budget still gets printed, clipped
# to per_msg: better a long turn than an empty document.
huge = [{'role': 'user', 'content': 'y' * 50_000}]
check('a single overlong turn survives',
      len(printdoc.transcript(huge, 100)) <= 4000 + 6, True)
check('transcript of nothing', printdoc.transcript([], 5000), '')

# --- the composed question --------------------------------------------

q = printdoc.compose_question('the complete recipe', 'user: hi')
check_in('question carries the marker', 'PRINTABLE DOCUMENT', q)
check_in('question carries the request', 'the complete recipe', q)
check_in('question carries the transcript', 'user: hi', q)
check_in('question forbids markdown', 'no markdown', q)

# --- what the player's words steer ------------------------------------

# Synthesis is opt-in: a printed page is easy to mistake for a record,
# so the default stays a faithful extraction.
for arg in ('please complete this recipe', 'fill in the missing steps',
            'flesh out the plan', 'expand the notes', 'complete the recipe',
            'collate what we discussed', 'the recipe, elaborated'):
    if not printdoc.wants_synthesis(arg):
        failures.append(f"wants_synthesis({arg!r}) should be true")
# 'the complete recipe' is the ADJECTIVE - "leave nothing out", not
# "invent what is absent". The determiner after the verb tells them apart.
for arg in ('', 'the recipe', 'the complete recipe',
            'a detailed one-page recipe with ingredients and steps'):
    if printdoc.wants_synthesis(arg):
        failures.append(f"wants_synthesis({arg!r}) should be false")

check('no length asked for', printdoc.target_lines('the recipe'), None)
check('detailed asks for a page',
      printdoc.target_lines('detailed one-page recipe'), printdoc.FULL_LINES)
check('brief asks for a note',
      printdoc.target_lines('a brief summary'), printdoc.BRIEF_LINES)
# Asking for both is asking for the short version of something detailed
check('brief beats detailed',
      printdoc.target_lines('a short version of the detailed plan'),
      printdoc.BRIEF_LINES)

# The extract-only clause must survive verbatim on the default path -
# it is what keeps an unqualified /print honest.
plain = printdoc.compose_question('the recipe', 'user: hi')
check_in('plain document may not invent', 'do not invent any', plain)
if 'lines' in plain:
    failures.append('plain document should carry no length target')

synth = printdoc.compose_question('please complete this recipe', 'user: hi')
if 'do not invent any' in synth:
    failures.append('a completion request must lift the no-invention rule')
check_in('completion fills the gaps', 'supply what is missing', synth)
check_in('completion stays anchored', 'Do not contradict', synth)

full = printdoc.compose_question('a detailed recipe', 'user: hi')
check_in('detailed states the target', '55 lines', full)
check_in('detailed forbids padding', 'never padding', full)
brief = printdoc.compose_question('a brief recipe', 'user: hi')
check_in('brief states the target', 'under 20 lines', brief)

# The compose call must not run as the chat persona: stream_chat reads
# system_prompt=None as "use the configured one", and the configured one
# says to keep replies short for a 40-column screen. That produced
# one-paragraph recipes on the live proxy no matter the token budget.
check_in('a document is not a chat message', 'not a chat message',
         printdoc.SYSTEM)
check_in('length comes from the request', 'brevity is not a virtue',
         printdoc.SYSTEM)
if '40-column' not in printdoc.SYSTEM:
    failures.append('the document prompt should say what it is NOT '
                    'writing for')

# --- picture vs document ----------------------------------------------

# /print the picture takes the illustration path, not the composer.
for arg in ('the picture', 'pic', 'this image', 'the illustration',
            'a picture of my character'):
    if not printdoc.wants_pic(arg):
        failures.append(f"wants_pic({arg!r}) should be true")
for arg in ('', 'the recipe', 'my inventory', 'the story so far'):
    if printdoc.wants_pic(arg):
        failures.append(f"wants_pic({arg!r}) should be false")
# Both match here, and the picture has to win - otherwise "print a
# picture of my character" renders a text character sheet.
if not (printdoc.wants_pic('a picture of my character')
        and printdoc.wants_sheet('a picture of my character')):
    failures.append('the pic/sheet overlap case changed shape')

# The natural-language surface: these are what a player actually types.
for arg in ('the last image', 'that pic', 'the artwork', 'what you drew',
            'the drawing', 'a sketch of the room', 'the painting',
            'picture 2', 'the last picture'):
    if not printdoc.wants_pic(arg):
        failures.append(f"wants_pic({arg!r}) should be true")

check('bare ask means the latest', printdoc.pic_index('the last image'), None)
check('a number picks one', printdoc.pic_index('picture 2'), 2)
check('no number, no index', printdoc.pic_index('the picture'), None)

# The map is text, so unlike a picture it prints on either backend.
for arg in ('the map', 'my map', 'maps'):
    if not printdoc.wants_map(arg):
        failures.append(f"wants_map({arg!r}) should be true")
for arg in ('', 'the recipe', 'the picture'):
    if printdoc.wants_map(arg):
        failures.append(f"wants_map({arg!r}) should be false")

# wrap=False is for art: a grid must be clipped, never reflowed, or a
# line that overran the paper returns as a second corridor.
grid = "+---+   +---+\n| 1 |---| 2 |\n+---+   +---+"
out = printdoc.finish('', grid, width=13, date='2026-07-24', wrap=False)
check('art keeps its rows', out.splitlines()[2:5], grid.splitlines())
long_art = 'x' * 40
clipped = printdoc.finish('', long_art, width=20, date='d', wrap=False)
check('art is clipped, not folded',
      [ln for ln in clipped.splitlines() if ln.startswith('x')], ['x' * 20])
# ...while prose still wraps rather than losing its tail. (A single
# unbroken 40-char token would NOT fold - _wrap keeps break_long_words
# off on purpose - so this asserts with real words.)
prose = 'the quick brown fox jumps over the lazy dog again'
folded = printdoc.finish('', prose, width=20, date='d')
body = [ln for ln in folded.splitlines()[2:-1]]
check('prose still wraps', len(body) > 1, True)
check('prose stays inside the page', [ln for ln in body if len(ln) > 20], [])

check('split_title', printdoc.split_title("Fire Stew\n\n1. Brown it.\n"),
      ('Fire Stew', '1. Brown it.'))
check('split_title strips a markdown heading',
      printdoc.split_title("# Fire Stew\nSimmer."), ('Fire Stew', 'Simmer.'))
# A one-line document is a body, not a lonely heading over nothing
check('split_title one line', printdoc.split_title("Just this."),
      ('', 'Just this.'))
check('split_title empty', printdoc.split_title('  '), ('', ''))

# --- finish(): header, wrapping, and the client's guarantees ----------

doc = printdoc.finish('Fire Stew', 'Brown the pork.', width=40,
                      date='2026-07-23')
check('finish layout', doc,
      "Fire Stew\n"
      "2026-07-23\n"
      + '-' * 40 + "\n"
      "Brown the pork.\n"
      + '-' * 40 + "\n")

check('finish with no title',
      printdoc.finish('', 'Brown the pork.', width=20, date='2026-07-23'),
      "2026-07-23\n" + '-' * 20 + "\nBrown the pork.\n" + '-' * 20 + "\n")

check('finish with nothing to print',
      printdoc.finish('Title', '   \n\n', width=78, date='x'), '')

long_line = 'the quick brown fox jumps over the lazy dog ' * 4
for width in (78, 40):
    out = printdoc.finish('T', long_line, width=width, date='2026-07-23')
    over = [ln for ln in out.split('\n') if len(ln) > width]
    check(f'wrapped to {width}', over, [])
    # rules are exactly the width, so the header is the width check too
    check(f'rule at {width}', out.split('\n')[2], '-' * width)

# The state block is the one text the player was never meant to read
stripped = printdoc.finish(
    'T', 'You take the key. [[STATE: {"hp":12}]]\nOnward.',
    width=78, date='x')
check_in('directive stripped', 'You take the key.', stripped)
if 'STATE' in stripped or '[[' in stripped:
    failures.append(f"directive survived: {stripped!r}")

# Typography the LLM emits must become printable ASCII, not '?'
smart = printdoc.finish('T', "“don’t” — 5° "
                             "… café", width=78, date='x')
check_in('smart quotes folded', '"don\'t" - 5 deg ... caf?', smart)
if any(ord(c) > 127 for c in smart):
    failures.append(f"non-ASCII survived: {smart!r}")
if '\r' in smart:
    failures.append("finish() emitted CR - the client maps \\n itself")

# Indented list lines keep their shape when they wrap (the sheet's
# one-item-per-line layout is the whole point of the fast path)
wrapped_item = printdoc.finish(
    '', '  - ' + 'a very long item name ' * 5, width=40, date='x')
for ln in wrapped_item.split('\n')[3:-2]:
    if ln and not ln.startswith('  '):
        failures.append(f"list continuation lost its indent: {ln!r}")

# --- block split -------------------------------------------------------

check('split, exact multiple',
      printdoc.blocks(b'x' * 480, 240), [b'x' * 240, b'x' * 240])
check('split, one over',
      [len(b) for b in printdoc.blocks(b'x' * 481, 240)], [240, 240, 1])
check('split, one under',
      [len(b) for b in printdoc.blocks(b'x' * 239, 240)], [239])
check('split, empty', printdoc.blocks(b'', 240), [])

# --- report ------------------------------------------------------------

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print('  ' + f)
    sys.exit(1)
print("printdoc: all checks passed")
