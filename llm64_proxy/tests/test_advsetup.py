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


def pick(s, text):
    """Choose a listed race/class and clear the (Y/n) gate it now shows.
    A custom (typed) answer sets no gate, so the extra 'y' is skipped."""
    reply, act = s.feed(text)
    if s.confirm is not None:
        reply, act = s.feed('y')
    return reply, act


def gear(s, *shelves):
    """Walk the kit shop, which is a browser rather than one answer.

    Each argument is "shelf item item...": open that shelf by number,
    toggle those items, come back. No arguments at all travels light.
    Always ends by approving, so the caller lands on the next stage."""
    for spec in shelves:
        toks = spec.split()
        s.feed(toks[0])
        if toks[1:]:
            s.feed(' '.join(toks[1:]))
        s.feed('b')
    s.feed('d')
    s.feed('y')


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
pick(s, '3')
check("race by number", s.answers['race'], 'Dwarf')
s2 = to_race(fresh())
pick(s2, 'dwarf')
check("race by name, any case", s2.answers['race'], 'Dwarf')
s3 = to_race(fresh())
reply, act = s3.feed('99')
check("a bad number is refused", 'race' in s3.answers, False)

# --- your own race and class ------------------------------------------
# The rules cannot list every race anyone will want to be, so anything
# that is not a number is taken as the player's own answer. A bad NUMBER
# is still refused (above): silently recording "99" as a race would be
# worse than saying no.
s = to_race(fresh())
n_custom = len(s.options()) + 1
reply, _ = s.feed(str(n_custom))
check("the custom line asks for words", 'race' in s.answers, False)
if 'your own race' not in reply.lower():
    failures.append(f"custom prompt does not ask for the race: {reply!r}")
reply, _ = s.feed('Automaton, a clockwork person of brass')
check("free text becomes the race",
      s.answers['race'], 'Automaton, a clockwork person of brass')
if 'no rule modifiers' not in reply.lower():
    failures.append("a custom race must say no modifiers were applied")
check("...and it survives into the character block",
      'Automaton' in s.character_block(), True)
check("...without racial modifiers",
      chargen.final_scores(RULES, {'CON': 10}, 'Automaton')['CON'], 10)

# A custom CLASS has no skill list, so that stage must not appear at all
s.feed('Vault Technician')
check("a custom class skips skills, having none",
      STAGES[s.stage]['key'], 'gear')

# Listed choices come with a line of flavour, and choosing one says it
s = to_race(fresh())
screen = s.stage_screen()
for want in ('Catfolk', 'Lizardfolk', 'Birdfolk', 'Kobold'):
    if want not in screen:
        failures.append(f"race list missing {want}")
reply, _ = s.feed('Dwarf')
if chargen.blurb(RULES, 'race', 'Dwarf') not in reply:
    failures.append(f"choosing a race does not describe it: {reply!r}")

# --- the kit shop ------------------------------------------------------
# Browsable now, not one flat list: an overview of category shelves, a
# toggle list behind each, a shelf for things the player invents, and an
# approve screen. Every screen is canned text and numbered replies.
GEAR = RULES['equipment']


def to_gear(cls='Fighter', skills='1 2'):
    st = to_race(fresh())
    st.answers['scores'] = {a: 15 for a in RULES['abilities']}
    pick(st, '1')
    pick(st, cls)
    st.feed(skills)
    return st


s = to_gear()
check("gear comes after skills", STAGES[s.stage]['key'], 'gear')

# The overview lists shelves, not items
screen = s.stage_screen()
for want in ('Weapons', 'Armor', 'Creature Comforts', 'Your own things'):
    if want not in screen:
        failures.append(f"kit overview missing {want!r}")
if 'Longsword' in screen:
    failures.append("the overview must list shelves, not items")

cats = s._gear_cats()
weapons = next(i for i, (c, _) in enumerate(cats, 1) if c['slug'] == 'weapon')
own_n = len(cats) + 1

# Opening a shelf shows its items, and a number toggles one on then off
s.feed(str(weapons))
screen = s.stage_screen()
if 'Longsword' not in screen:
    failures.append("the weapons shelf lists no weapons")
items = [it for it in s._gear_items() if it['kind'] == 'weapon']
s.feed('1')
check("a number takes the item", s.kit, [items[0]['name']])
if '+ ' not in s.stage_screen():
    failures.append("a taken item is not marked")
s.feed('1')
check("the same number puts it back", s.kit, [])

# Numbers batch, because every screen is a repaint down the wire
s.feed('1 2')
check("numbers batch", len(s.kit), 2)
reply, _ = s.feed('b')
check("b returns to the overview", s.cat, None)
if 'Weapons' not in s.stage_screen():
    failures.append("back did not land on the overview")

# The purse binds. Taking everything on a shelf must stop at the budget
# rather than going negative, and the refusal has to name what it could
# not fit - a batch that silently half-applies is worse than one that
# explains itself.
s = to_gear()
s.feed(str(weapons))
n_weapons = len([it for it in s._gear_items() if it['kind'] == 'weapon'])
reply, _ = s.feed(' '.join(str(i) for i in range(1, n_weapons + 1)))
if s._gear_spent() > GEAR['points']:
    failures.append(f"the purse went over: {s._gear_spent()}")
if s._gear_left() < 0:
    failures.append("negative points remaining")
if 'No room for' not in reply:
    failures.append(f"an over-budget batch says nothing: {reply!r}")
# ...and what DID fit is still in the kit
if not s.kit:
    failures.append("an over-budget batch took nothing at all")

# Your own things: several at once, each its own item, each priced
s = to_gear()
s.feed(str(own_n))
check("the own shelf opens", s.cat, 'own')
s.feed('a lucky coin + my fathers ring + a stolen key')
check("each + is its own item", s.kit_own,
      ['a lucky coin', 'my fathers ring', 'a stolen key'])
check("...each priced separately",
      chargen.gear_cost(RULES, s.kit_own), 3 * GEAR['custom_cost'])
s.feed('a lucky coin')
check("duplicates do not double up", len(s.kit_own), 3)
s.feed('2')
check("a number leaves one behind", s.kit_own,
      ['a lucky coin', 'a stolen key'])

# The cap binds
s = to_gear()
s.feed(str(own_n))
s.feed(' + '.join(f"thing {i}" for i in range(GEAR['custom_max'] + 3)))
if len(s.kit_own) > GEAR['custom_max']:
    failures.append("more custom items than custom_max allows")

# x puts everything back
s = to_gear()
s.feed(str(weapons)); s.feed('1'); s.feed('b')
s.feed('x')
check("x clears the kit", (s.kit, s.kit_own), ([], []))

# d shows the approve screen; b returns; y commits
s = to_gear()
s.feed(str(weapons)); s.feed('1'); s.feed('b')
taken = list(s.kit)
reply, _ = s.feed('d')
check("d opens the approve screen", s.cat, 'done')
if taken[0] not in reply:
    failures.append("the approve screen does not list what you carry")
s.feed('b')
check("b leaves the approve screen", s.cat, None)
check("...without committing", 'gear' in s.answers, False)
s.feed('d')
s.feed('y')
check("y commits the kit", s.answers['gear'], taken)
check("gear reaches the character block",
      'carrying' in s.character_block(), True)

# Travelling light is legal - approve an empty kit
s = to_gear()
s.feed('d'); s.feed('y')
check("an empty kit is allowed", s.answers['gear'], [])

# Editing gear from the review RESUMES the kit rather than starting bare
s = to_gear()
s.feed(str(weapons)); s.feed('1'); s.feed('b'); s.feed('d'); s.feed('y')
kept = list(s.answers['gear'])
while s.state == 'stage':
    s.feed('?')
gear_row = [i for i, st in enumerate(s._visible(), 1)
            if st['key'] == 'gear'][0]
s.feed(str(gear_row))
check("editing gear reopens the shelves", s.cat, None)
check("...holding what was already chosen", s.kit, kept)

# Every class must be able to kit itself out inside the budget
for c in RULES['classes']:
    items = chargen.gear_options(RULES, c['name'])
    if len(items) < 6:
        failures.append(f"{c['name']} sees only {len(items)} items")
    if min(i['cost'] for i in items) > GEAR['points']:
        failures.append(f"{c['name']} cannot afford anything")

# Every catalogue item must live on a shelf, or it is unreachable
_kinds = set()
for c in GEAR['categories']:
    _kinds.update(c['kinds'])
for it in GEAR['items']:
    if it['kind'] not in _kinds:
        failures.append(f"{it['name']} has kind {it['kind']!r}, no category")

# --- the step counter must not skip -----------------------------------
# Field complaint: a non-caster went "step 6" then "step 8", which reads
# as a setup that lost its place.
for cls, feeds in (('Wizard', ['1 2', '1 2 3']), ('Fighter', ['1 2'])):
    s = to_race(fresh())
    s.answers['scores'] = {a: 15 for a in RULES['abilities']}
    pick(s, '1')                              # race (Human), confirmed
    seen = []

    def note(reply):
        for line in reply.splitlines():
            if line.startswith('[step '):
                seen.append(int(line.split()[1]))

    for text in [cls] + feeds:
        reply, _ = s.feed(text)
        if s.confirm is not None:             # a class pick hits the Y/n gate
            reply, _ = s.feed('y')
        note(reply)
    # The kit shop repaints its own step on every screen (overview,
    # shelf, approve). Standing still is not a skip, so collapse runs of
    # the same number before checking - what must never happen is a
    # number being jumped over.
    for text in ('1', '1', 'b', 'd', 'y'):
        note(s.feed(text)[0])
    seen = [n for i, n in enumerate(seen) if i == 0 or n != seen[i - 1]]
    if seen != sorted(seen) or any(b - a != 1 for a, b in zip(seen, seen[1:])):
        failures.append(f"{cls} step numbers jump: {seen}")

# Only eligible classes are offered
s = to_race(fresh())
s.answers['scores'] = {a: 3 for a in RULES['abilities']}
pick(s, '1')                                  # Human
check("class list respects the scores",
      s.options(), ['Wanderer'])

# --- multi-pick enforces the count ------------------------------------
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
pick(s, '1')                                  # Human
pick(s, 'Wizard')
want = s.picks_allowed()
reply, _ = s.feed('1')
check("too few picks is refused", 'skills' in s.answers, False)
if f"Pick exactly {want}" not in reply:
    failures.append("refusal does not say how many are needed")
s.feed(' '.join(str(i) for i in range(1, want + 1)))
check("the right count is accepted", len(s.answers['skills']), want)


# --- naming your own skills (CUSTOM_MULTI_OK) -------------------------
def to_skills(cls='Fighter'):
    """A setup parked on the skills stage for the given class."""
    st = to_race(fresh())
    st.answers['scores'] = {a: 15 for a in RULES['abilities']}
    pick(st, '1')
    pick(st, cls)
    return st


s = to_skills()
n_opts = len(s.options())
want = s.picks_allowed()
screen = s.stage_screen()
if f"{n_opts + 1}  Something else" not in screen:
    failures.append("skills screen offers no 'name your own' line")
if "Trained means" not in screen:
    failures.append("skills screen does not explain what trained means")

# The custom line by number prompts rather than recording a skill
reply, _ = s.feed(str(n_opts + 1))
check("custom line does not record", 'skills' in s.answers, False)
if "name them" not in reply:
    failures.append("custom skills prompt does not ask for names")
s.feed('haggling, forgery')
check("own skills recorded", s.answers['skills'], ['haggling', 'forgery'])

# Typed straight in, no number first; 'and' separates as well as commas
s = to_skills()
s.feed('smuggling and cartography')
check("own skills without the number",
      s.answers['skills'], ['smuggling', 'cartography'])

# Listed names still beat the custom path - a skill that exists is a pick
s = to_skills()
s.feed('Athletics Riding')
check("listed names stay a listed pick",
      s.answers['skills'], ['Athletics', 'Riding'])

# A misfired number is a typo, not a skill called '1 9'
s = to_skills()
reply, _ = s.feed('1 %d' % (n_opts + 5))
check("a bad number is refused, not taken as custom",
      'skills' in s.answers, False)
if f"Pick exactly {want}" not in reply:
    failures.append("bad-number refusal lost its count")

# The count is enforced on custom names too
s = to_skills()
reply, _ = s.feed('haggling')
check("one custom name is refused", 'skills' in s.answers, False)
if f"Name exactly {want}" not in reply:
    failures.append("custom refusal does not say how many are needed")

# Spells are deliberately NOT custom-able
s = to_skills('Wizard')
s.feed('1 2')
check("wizard reached spells", STAGES[s.stage]['key'], 'spells')
n_spells = len(s.options())
if "Something else" in s.stage_screen():
    failures.append("spells must not offer a custom line")
reply, _ = s.feed(str(n_spells + 1))
check("a custom spell number is refused", 'spells' in s.answers, False)

# A caster reaches the spell stage...
check("wizard is asked for spells", STAGES[s.stage]['key'], 'spells')
s.feed('1 2 3')
check("spells recorded", len(s.answers['spells']), 3)

# ...and a non-caster skips it entirely
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
pick(s, '1')
pick(s, 'Fighter')
s.feed('1 2')
check("fighter skips spells", STAGES[s.stage]['key'], 'gear')

# --- the cascade ------------------------------------------------------
s = to_race(fresh())
s.answers['scores'] = {a: 15 for a in RULES['abilities']}
pick(s, '1'); pick(s, 'Wizard'); s.feed('1 2'); s.feed('1 2 3')
gear(s, '1 1'); s.feed('Bruni'); s.feed('the flooded nave')
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
pick(s, 'Elf')
check("an edit returns to the review", s.state, 'review')
check("class is flagged", 'class' in s.invalid, True)
check("begin is refused while flagged", s.feed('y')[1], ACT_NONE)

# Changing to a class with no spells must not leave a stale spell list
n_class = vis.index('Class') + 1
s.feed(str(n_class)); pick(s, 'Wanderer')
check("stale spells are dropped with the class",
      'spells' in s.answers, False)
check("...and the spell line vanishes from the review",
      'Spells' in s.review_screen(), False)

# --- surprise answers never reach the prep pass -----------------------
s = fresh(); s.feed('3')
s.feed('?'); s.feed('?'); s.feed('')
pick(s, '1'); pick(s, 'Wanderer'); s.feed('1 2'); gear(s, '1 1')
s.feed('?'); s.feed('?')
b = s.bundle()
check("'?' answers are dropped from the bundle",
      [k for k in ('world', 'tone', 'name', 'opening') if k in b], [])
check("...but real answers survive", 'race' in b and 'scores' in b, True)

# The character block is prose for the prompt, not JSON
s = to_race(fresh())
s.answers['scores'] = {a: 14 for a in RULES['abilities']}
pick(s, '3')                                  # Dwarf
pick(s, 'Cleric'); s.feed('1 2'); s.feed('1 2'); gear(s, '1 1')
s.feed('Bruni Ashvein')
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
s.feed('k'); s.feed('Dwarf'); s.feed('Wanderer'); s.feed('1 2')
gear(s, '1 1'); s.feed('Bruni')
check("...and does not re-ask the kept world", s.state, 'review')
check("the kept world is still on the review",
      'Sunken Sanctum' in s.review_screen(), True)

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all adventure-setup tests pass")

