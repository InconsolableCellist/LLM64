#!/usr/bin/env python3
"""The /adventure front door. Run: python3 tests/test_advsetup.py

The state machine is pure on purpose (docs/09-adventure-setup.md), so
the interesting rules - edit returns to the review, and an edit
invalidates what was built on top of it - are testable with no model,
no proxy and no emulator.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.advsetup import (AdventureSetup, STAGES, STAGE_KEYS,
                          ACT_QUICK, ACT_THEME, ACT_BEGIN, ACT_CANCEL,
                          ACT_SUGGEST, ACT_NONE)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def walk(s, answers):
    """Answer every remaining stage in order."""
    out = []
    for a in answers:
        out.append(s.feed(a))
    return out


# --- the chooser ------------------------------------------------------
s = AdventureSetup()
if '4  Load a saved world' in s.opening_screen():
    failures.append("option 4 offered with no templates saved")
s2 = AdventureSetup(templates=['The Sunken Sanctum'])
if '4  Load a saved world' not in s2.opening_screen():
    failures.append("option 4 missing when templates exist")

check("option 1 starts immediately", AdventureSetup().feed('1')[1], ACT_QUICK)
s = AdventureSetup()
s.feed('2')
check("option 2 asks for a line", s.state, 'theme')
check("theme is captured", s.set_theme('a heist on a sky-barge')[1], ACT_THEME)

s = AdventureSetup()
_, act = s.feed('9')
check("a bad pick re-shows the menu", act, ACT_NONE)
check("...and does not advance", s.state, 'choose')

# --- the four stages --------------------------------------------------
s = AdventureSetup()
s.feed('3')
check("option 3 enters stage 1", (s.state, s.stage), ('stage', 0))
reply, act = s.feed('a drowned temple city')
check("stage 1 answered advances to stage 2", s.stage, 1)
check("...and invites suggestions", act, ACT_SUGGEST)
walk(s, ['grim and wet', 'a salvage diver', 'waking in the nave'])
check("all four answered lands on review", s.state, 'review')
check("answers kept in order", [s.answers[k] for k in STAGE_KEYS],
      ['a drowned temple city', 'grim and wet', 'a salvage diver',
       'waking in the nave'])

# '?' means surprise me, and must NOT reach the prep pass as a literal
s2 = AdventureSetup()
s2.feed('3')
walk(s2, ['?', 'grim', '?', '?'])
check("surprise answers are dropped from the bundle",
      sorted(s2.bundle()), ['tone'])

# --- the review loop --------------------------------------------------
s = AdventureSetup()
s.feed('3')
walk(s, ['a drowned city', 'grim', 'a diver', 'the nave'])
review = s.review_screen()
for label in ('World', 'Tone', 'Character', 'Opening'):
    if label not in review:
        failures.append(f"review screen missing {label}")

# Editing line 2 (Tone) has no dependents, so it returns clean...
s.feed('2')
check("picking a number edits that line", (s.state, s.stage), ('stage', 1))
check("...and is marked as an edit", s.editing, True)
s.feed('hopeful')
check("an edit returns to the review, not to stage 3", s.state, 'review')
check("the edited value stuck", s.answers['tone'], 'hopeful')
check("nothing else was disturbed", s.answers['opening'], 'the nave')
check("no dependents flagged", s.invalid, set())
check("y begins", s.feed('y')[1], ACT_BEGIN)

# --- the cascade: editing World invalidates what was built on it ------
s = AdventureSetup()
s.feed('3')
walk(s, ['a drowned city', 'grim', 'a diver', 'the nave'])
s.feed('1')                       # edit World
s.feed('a desert of glass')
check("editing world flags its dependents",
      s.invalid, {'character', 'opening'})
reply, act = s.feed('y')
check("...and begin is refused while flagged", act, ACT_NONE)
if 'needs a look' not in reply:
    failures.append("flagged lines are not called out on the review")
s.feed('3')
s.feed('a glassmaker')            # re-answer Character
check("re-answering clears its own flag", 'character' in s.invalid, False)
check("but Opening is still flagged", 'opening' in s.invalid, True)
check("still refused", s.feed('y')[1], ACT_NONE)
s.feed('4')
s.feed('at the kiln')
check("all clear now", s.invalid, set())
check("and y begins", s.feed('y')[1], ACT_BEGIN)

# A junk reply at the review must not lose the work
s = AdventureSetup()
s.feed('3')
walk(s, ['w', 't', 'c', 'o'])
before = dict(s.answers)
s.feed('banana')
check("junk at the review changes nothing", s.answers, before)
check("...and stays on the review", s.state, 'review')

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all adventure-setup tests pass")
