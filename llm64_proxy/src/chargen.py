"""Character creation mechanics for /adventure.

docs/09-adventure-setup.md §4c: the proxy owns the mechanics, the model
owns the flavour. Dice are real (a model asked to roll invents a
flattering number), racial modifiers are applied here (it would fudge
the arithmetic), and the legal options are computed here and handed to
the player - so the model never has to remember the rules.

Everything here is pure: pass an rng in and the results are exact,
which is what makes the tests worth having.
"""

import json
import random
from pathlib import Path

from .respath import resource_dir

RULES_PATH = resource_dir() / 'adventure_rules.json'


def load_rules(path=None) -> dict:
    return json.loads(Path(path or RULES_PATH).read_text())


def roll_scores(rules: dict, rng=None) -> dict:
    """4d6 drop lowest, once per ability. The classic, and the reason
    the roll is shown rather than hidden."""
    rng = rng or random
    out = {}
    for ab in rules['abilities']:
        dice = sorted(rng.randint(1, 6) for _ in range(4))
        out[ab] = sum(dice[1:])          # drop the lowest
    return out


def find_race(rules: dict, name: str):
    for r in rules['races']:
        if r['name'].lower() == (name or '').lower():
            return r
    return None


def find_class(rules: dict, name: str):
    for c in rules['classes']:
        if c['name'].lower() == (name or '').lower():
            return c
    return None


def blurb(rules: dict, key: str, name: str) -> str:
    """The one-line description of a race or class. Empty for anything
    the player typed themselves - the rules have never heard of it, and
    inventing a description here would be the model's job, not ours."""
    found = (find_race if key == 'race' else find_class)(rules, name)
    return (found or {}).get('blurb', '')


def gear_options(rules: dict, class_name: str) -> list:
    """Starting equipment this class may take: everything unrestricted,
    plus whatever names it.

    A class the rules have never heard of (the player typed their own)
    gets the WHOLE catalogue. The old reading - give it only the
    unrestricted items - sounded like honesty and played like a
    punishment: exactly one weapon in the whole book is unrestricted, so
    a custom class opened the weapon shelf and found a dagger and
    nothing else. We cannot know what an Automaton is proficient with,
    and that cuts both ways: offering everything and letting the player
    choose is the honest answer, not offering nothing."""
    items = (rules.get('equipment') or {}).get('items', [])
    if not find_class(rules, class_name):
        return list(items)
    out = []
    for item in items:
        allowed = item.get('classes')
        if not allowed or (class_name or '').lower() in [
                a.lower() for a in allowed]:
            out.append(item)
    return out


def gear_cost(rules: dict, names) -> int:
    """What a kit costs. Anything not in the catalogue is the player's
    own custom item and costs the custom price."""
    gear = rules.get('equipment') or {}
    by_name = {it['name']: it['cost'] for it in gear.get('items', [])}
    return sum(by_name.get(n, gear.get('custom_cost', 2)) for n in names)


def final_scores(rules: dict, base: dict, race_name: str) -> dict:
    """Base roll plus racial modifiers. Kept separate from the roll so
    changing race re-derives rather than re-rolls - losing your scores
    because you changed your mind about being a dwarf would be rude."""
    race = find_race(rules, race_name)
    out = dict(base)
    for ab, mod in (race or {}).get('mods', {}).items():
        out[ab] = out.get(ab, 10) + mod
    return out


def eligible_classes(rules: dict, scores: dict) -> list:
    """Classes whose requirements the scores meet. 'Wanderer' has no
    requirements on purpose: bad dice must never leave a player with
    nothing to be."""
    ok = []
    for c in rules['classes']:
        if all(scores.get(ab, 0) >= need
               for ab, need in c.get('requires', {}).items()):
            ok.append(c)
    return ok


def fmt_scores(rules: dict, scores: dict) -> str:
    return "  ".join(f"{ab} {scores.get(ab, 0):2}"
                     for ab in rules['abilities'])


def sheet(rules: dict, answers: dict) -> dict:
    """The immutable half of the character - what rides the cached head
    of the system prompt (docs/09 §4b). Mutable stats (hp, gold,
    location) are NOT here; they live in adv_state."""
    base = answers.get('scores') or {}
    race = answers.get('race', '')
    cls = find_class(rules, answers.get('class', '')) or {}
    return {
        'race': race,
        # A class the player typed themselves is not in the rules, so
        # fall back to what they wrote - dropping it here would quietly
        # delete the whole idea from the character.
        'class': cls.get('name') or answers.get('class', ''),
        'scores': final_scores(rules, base, race),
        'skills': list(answers.get('skills') or []),
        'spells': list(answers.get('spells') or []),
        'gear': list(answers.get('gear') or []),
        'name': answers.get('name', ''),
        'hit_die': cls.get('hit_die'),
    }


def describe(rules: dict, answers: dict) -> str:
    """One block for the system prompt. Plain prose beats JSON here -
    it is background the model reads, not data it has to update."""
    return describe_sheet(rules, sheet(rules, answers))


def describe_sheet(rules: dict, s: dict) -> str:
    """The same block, from an already-built sheet.

    Split out because a back-filled character never had any `answers` to
    build from - the story is where it came from - and the system prompt
    still wants the same prose either way.
    """
    bits = []
    # A race the player typed themselves can be a whole sentence
    # ("Automaton, a clockwork person of brass gears"), which reads as
    # gibberish jammed in front of the class. Keep it as its own
    # sentence when it is not a plain one-word race.
    race, aside = s['race'], ''
    if len(race) > 18 or ',' in race:
        race, aside = '', s['race']
    who = " ".join(x for x in (race, s['class']) if x)
    article = 'an' if who[:1].lower() in 'aeiou' else 'a'
    if s['name'] and who:
        bits.append(f"The player character is {s['name']}, "
                    f"{article} {who}.")
    elif s['name']:
        bits.append(f"The player character is {s['name']}.")
    elif who:
        bits.append(f"The player character is {article} {who}.")
    if aside:
        bits.append(f"Their kind: {aside}.")
    if s['scores']:
        bits.append("Ability scores: " + fmt_scores(rules, s['scores']) + ".")
    # The hit die is a rules value the proxy has and the model was
    # inventing: it rolled the class, so it owns the HP that class starts
    # with. Said out loud, level 1 maxhp stops being a coin flip.
    if s.get('hit_die'):
        bits.append(f"Hit die: d{s['hit_die']}, so their maximum HP at "
                    f"level 1 is {s['hit_die']} plus their CON modifier.")
    if s['skills']:
        bits.append("Trained skills: " + ", ".join(s['skills']) + ".")
    if s['spells']:
        bits.append("Known spells: " + ", ".join(s['spells']) + ".")
    if s['gear']:
        # Named as STARTING equipment, not as a live inventory: what the
        # player is carrying now belongs in adv_state, which the model
        # updates every turn. This block is the stable head of the
        # prompt and must never contradict it.
        bits.append(("Has carried since early in the story: "
                     if s.get('derived') else
                     "Started the adventure carrying: ")
                    + ", ".join(s['gear']) + ".")
    return " ".join(bits)


# --- back-filling a sheet the dice never rolled ------------------------
#
# /adventure <theme> and the 'surprise me' path start play without ever
# running the setup flow, so nothing writes the static half of the sheet
# and the narrator is forbidden to put those fields in the state block
# (modes.py). The window is then blank for the life of the game and no
# refresh can ever fill it. The story does establish who the character
# is - it just does it in prose. These two pure functions are how that
# prose is turned back into a sheet.

BACKFILL_SCHEMA = ('{"name":"...","race":"...","class":"...",'
                   '"skills":["..."],"spells":["..."],"gear":["..."]}')

BACKFILL_LIST_MAX = 6
BACKFILL_ITEM_MAX = 40
BACKFILL_STR_MAX = 40


def backfill_question(convo: str, state: str = '') -> str:
    """The one-shot that asks the narrator who the player turned out to
    be.

    The instruction that matters is the refusal: a field the story has
    not established comes back empty. A blank line in the window is
    honest; an invented race is a fact the game will then defend for the
    rest of the adventure.
    """
    parts = [
        "Below is a text adventure in progress. This character was "
        "never formally created, so the game holds no record of who "
        "they are - only what the story itself has established.",

        "Recover that record. Reply with ONLY a JSON object of this "
        "shape, on one line, and nothing else:\n" + BACKFILL_SCHEMA,

        "How to fill it in:\n"
        "- Use ONLY what the story has actually established. Leave a "
        "string empty, or a list empty, where it has not. An empty "
        "field is the correct answer; an invented one is not.\n"
        "- skills and spells are abilities the character has used or "
        "been said to have. gear is what they have carried since early "
        "on, not what they picked up last turn.\n"
        "- Do NOT report ability scores, hit points, gold, or where "
        "they are. Those are tracked elsewhere and are not yours here.\n"
        "- At most six entries per list, three words or fewer each.",
    ]
    if state:
        parts.append("The game state currently tracked: " + state)
    parts.append("Transcript:\n" + convo)
    return "\n\n".join(parts)


def parse_backfill(text: str, rules: dict = None) -> dict:
    """A back-fill reply -> a sheet dict shaped exactly like sheet()'s,
    or {} when there is nothing usable in it.

    Ability scores are not accepted from the model at any price: a score
    means four dice were rolled, and here none were - so `scores` comes
    back empty and the window's ability line stays blank rather than
    showing numbers no one rolled. The hit die is left out for the same
    reason, and because describe_sheet() would otherwise announce a
    level-1 maximum HP to a character who is already level 3.

    `derived` marks the sheet as told rather than rolled. It is for this
    side of the wire only - _char_sheet_payload names the keys it sends,
    so the flag never reaches a client.
    """
    if not text:
        return {}
    lo, hi = text.find('{'), text.rfind('}')
    if lo < 0 or hi <= lo:
        return {}
    try:
        obj = json.loads(text[lo:hi + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}

    def one(key):
        v = obj.get(key)
        return v.strip()[:BACKFILL_STR_MAX] if isinstance(v, str) else ''

    def many(key):
        v = obj.get(key)
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if len(out) >= BACKFILL_LIST_MAX:
                break
            if isinstance(item, str) and item.strip():
                out.append(item.strip()[:BACKFILL_ITEM_MAX])
        return out

    s = {
        'name': one('name'),
        'race': one('race'),
        'class': one('class'),
        'scores': {},
        'skills': many('skills'),
        'spells': many('spells'),
        'gear': many('gear'),
        'hit_die': None,
        'derived': True,
    }
    if not any((s['name'], s['race'], s['class'],
                s['skills'], s['spells'], s['gear'])):
        return {}
    # Spell a known class the way the rules spell it, so a back-filled
    # 'wizard' and a rolled 'Wizard' are the same word downstream.
    found = find_class(rules, s['class']) if rules else None
    if found:
        s['class'] = found.get('name') or s['class']
    return s
