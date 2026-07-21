#!/usr/bin/env python3
"""The /adventure front door. Run: python3 tests/test_advsetup.py

The state machine is pure on purpose (docs/09-adventure-setup.md), so
the rules worth testing - edit returns to the review, the dependency
cascade, and the character mechanics the proxy owns rather than the
model - need no emulator and no API call.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import chargen
from src.advsetup import (AdventureSetup, STAGES, STAGE_KEYS,
                          ACT_QUICK, ACT_THEME, ACT_BEGIN, ACT_NONE)

failures = []
RULES = chargen.load_rules()


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def fresh(seed=7):
    return AdventureSetup(rng=random.Random(seed))


def to_race(s):
    """Answer world/tone/scores and stop on the Race stage."""
    s.feed('3')
    s.feed('a drowned temple city')
    s.feed('grim and wet')
    s.feed('')                      # keep the rolled scores
    return s


# --- dice are the proxy's job, and they are real ----------------------
rng = random.Random(1)
vals = [v for _ in range(2000) for v in chargen.roll_scores(RULES, rng).values()]
mean = sum(vals) / len(vals)
if not (12.0 < mean < 12.5):
    failures.append(f"4d6-drop-lowest mean {mean:.2f}, expected ~12.24")
if min(vals) < 3 or max(vals) > 18:
    failures.append(f"scores out of range: {min(vals)}..{max(vals)}")

check("racial modifiers apply",
      chargen.final_scores(RULES, {'CON': 10}, 'Dwarf')['CON'], 12)
check("...and are not baked into the roll",
      chargen.final_scores(RULES, {'CON': 10}, 'Elf')['CON'], 9)

# Requirements gate classes, and nobody is ever left with nothing
weak = {a: 3 for a in RULES['abilities']}
names = [c['name'] for c in chargen.eligible_classes(RULES, weak)]
check("terrible scores still leave a class", names, ['Wanderer'])
strong = {a: 18 for a in RULES['abilities']}
if len(chargen.eligible_classes(RULES, strong)) != len(RULES['classes']):
    failures.append("great scores should qualify for everything")

# --- the chooser ------------------------------------------------------
check("option 1 starts immediately", fresh().feed('1')[1], ACT_QUICK)
s = fresh(); s.feed('2')
check("option 2 captures the line", s.set_theme('a sky-barge heist')[1],
      ACT_THEME)
s = fresh(); _, act = s.feed('9')
check("a bad pick re-shows the menu", (act, s.state), (ACT_NONE, 'choose'))

# --- the roll stage ---------------------------------------------------
s = fresh(); s.feed('3'); s.feed('w'); s.feed('t')
first = dict(s.answers['scores'])
s.feed('r')
check("r re-rolls", s.answers['scores'] != first, True)
check("...and stays on the roll", s.stage, STAGE_KEYS.index('scores'))
rolled = dict(s.answers['scores'])
s.feed('')
check("Return keeps the roll", s.answers['scores'], rolled)
check("...and moves on", s.stage, STAGE_KEYS.index('race'))

# --- choices are by number or by name ---------------------------------
s = to_race(fresh())
s.feed('3')
check("race by number", s.answers['race'], 'Dwarf')
s2 = to_race(fresh())
s2.feed('dwarf')
check("race by name, any case", s2.answers['race'], 'Dwarf')
s3 = to_race(fresh())
reply, act = s3.feed('banana')
check("a bad choice is refused", 'race' in s3.answers, False)

# Only eligible classes are offered
s = to_race(fresh())
s.answers['scores'] = {a: 3 for a in RULES['abilities']}
s.feed('1')                                   # Human
check("class list respects the scores",
      s.options(), ['Wanderer'])

# --- multi-pick enforces the count ------------------------------------
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
s.feed('1')                                   # Human
s.feed('Wizard')
want = s.picks_allowed()
reply, _ = s.feed('1')
check("too few picks is refused", 'skills' in s.answers, False)
if f"Pick exactly {want}" not in reply:
    failures.append("refusal does not say how many are needed")
s.feed(' '.join(str(i) for i in range(1, want + 1)))
check("the right count is accepted", len(s.answers['skills']), want)

# A caster reaches the spell stage...
check("wizard is asked for spells", STAGES[s.stage]['key'], 'spells')
s.feed('1 2 3')
check("spells recorded", len(s.answers['spells']), 3)

# ...and a non-caster skips it entirely
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
s.feed('1')
s.feed('Fighter')
s.feed('1 2')
check("fighter skips spells", STAGES[s.stage]['key'], 'name')

# --- the cascade ------------------------------------------------------
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
s.feed('1'); s.feed('Wizard'); s.feed('1 2'); s.feed('1 2 3')
s.feed('Bruni'); s.feed('the flooded nave')
check("all stages answered lands on review", s.state, 'review')

vis = [st['label'] for st in STAGES if s._applies(st)]
review = s.review_screen()
for label in vis:
    if label not in review:
        failures.append(f"review missing {label}")

# Editing race puts class (and so skills/spells) back in question
n_race = vis.index('Race') + 1
s.feed(str(n_race))
check("picking a number edits that line", STAGES[s.stage]['key'], 'race')
s.feed('Elf')
check("an edit returns to the review", s.state, 'review')
check("class is flagged", 'class' in s.invalid, True)
check("begin is refused while flagged", s.feed('y')[1], ACT_NONE)

# Changing to a class with no spells must not leave a stale spell list
n_class = vis.index('Class') + 1
s.feed(str(n_class)); s.feed('Wanderer')
check("stale spells are dropped with the class",
      'spells' in s.answers, False)
check("...and the spell line vanishes from the review",
      'Spells' in s.review_screen(), False)

# --- surprise answers never reach the prep pass -----------------------
s = fresh(); s.feed('3')
s.feed('?'); s.feed('?'); s.feed('')
s.feed('1'); s.feed('Wanderer'); s.feed('1 2'); s.feed('?'); s.feed('?')
b = s.bundle()
check("'?' answers are dropped from the bundle",
      [k for k in ('world', 'tone', 'name', 'opening') if k in b], [])
check("...but real answers survive", 'race' in b and 'scores' in b, True)

# The character block is prose for the prompt, not JSON
s = to_race(fresh())
s.answers['scores'] = {a: 14 for a in RULES['abilities']}
s.feed('3')                                   # Dwarf
s.feed('Cleric'); s.feed('1 2'); s.feed('1 2'); s.feed('Bruni Ashvein')
block = s.character_block()
for bit in ('Dwarf', 'Cleric', 'Bruni Ashvein', 'CON'):
    if bit not in block:
        failures.append(f"character block missing {bit}: {block!r}")

# --- saved worlds -----------------------------------------------------
import tempfile
from src.advtemplates import TemplateStore
from src.advsetup import ACT_LOAD

with tempfile.TemporaryDirectory() as tmp:
    store = TemplateStore(tmp)
    check("no worlds yet", store.list(), [])
    slug = store.save({'world': 'The Sunken Sanctum', 'tone': 'grim'},
                      'Oakhaven is a drowned city.', 'A Dwarf Cleric.')
    check("saving returns a slug", bool(slug), True)
    listed = store.list()
    check("the world is listed by name",
          [n for n, _ in listed], ['The Sunken Sanctum'])
    got = store.load(listed[0][1])
    check("and round-trips", (got['bundle']['tone'], got['bible'][:8]),
          ('grim', 'Oakhaven'))
    check("an unknown slug is None, not a crash",
          store.load('nope'), None)

    # Named from the bible when the player left the world to the story
    s2 = TemplateStore(tmp)
    s2.save({}, 'Oakhaven is a skeletal cathedral-city. More text.', '')
    names = [n for n, _ in s2.list()]
    if 'Oakhaven is a skeletal cathedral-city' not in names:
        failures.append(f"bible-derived name missing: {names}")

    # A corrupt file must not break the menu
    (Path(tmp) / 'adventures' / 'broken.json').write_text('{not json')
    check("corrupt files are skipped", len(TemplateStore(tmp).list()), 2)

# Option 4 only appears with saved worlds, and picking one offers both
s = AdventureSetup(templates=[('The Sunken Sanctum', 'sunken-1')],
                   rng=random.Random(5))
if '4  Load a saved world' not in s.opening_screen():
    failures.append("option 4 missing when a world exists")
reply, _ = s.feed('4')
if 'Sunken Sanctum' not in reply:
    failures.append("world list does not name the world")
reply, _ = s.feed('1')
for want in ('Play it as it was', 'roll a new character'):
    if want not in reply:
        failures.append(f"load menu missing {want!r}")
check("replay is a load", s.feed('1')[1], ACT_LOAD)
check("...and remembers which world", s.template_slug, 'sunken-1')

# Re-roll keeps the world and walks only the character
s = AdventureSetup(templates=[('The Sunken Sanctum', 'sunken-1')],
                   rng=random.Random(5))
s.feed('4'); s.feed('1'); s.feed('2')
saved = {'bundle': {'world': 'The Sunken Sanctum', 'tone': 'grim',
                    'opening': 'the flooded nave'}}
s.start_reroll(saved)
check("re-roll starts at the dice", STAGES[s.stage]['key'], 'scores')
check("...with the world kept", s.answers['world'], 'The Sunken Sanctum')
s.feed('k'); s.feed('Dwarf'); s.feed('Wanderer'); s.feed('1 2'); s.feed('Bruni')
check("...and does not re-ask the kept world", s.state, 'review')
check("the kept world is still on the review",
      'Sunken Sanctum' in s.review_screen(), True)

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all adventure-setup tests pass")

