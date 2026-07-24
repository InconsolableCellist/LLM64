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
