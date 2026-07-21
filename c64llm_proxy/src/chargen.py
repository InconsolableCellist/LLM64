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

RULES_PATH = Path(__file__).resolve().parent / 'adventure_rules.json'


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
        'class': cls.get('name', ''),
        'scores': final_scores(rules, base, race),
        'skills': list(answers.get('skills') or []),
        'spells': list(answers.get('spells') or []),
        'name': answers.get('name', ''),
        'hit_die': cls.get('hit_die'),
    }


def describe(rules: dict, answers: dict) -> str:
    """One block for the system prompt. Plain prose beats JSON here -
    it is background the model reads, not data it has to update."""
    s = sheet(rules, answers)
    bits = []
    who = " ".join(x for x in (s['race'], s['class']) if x)
    if s['name']:
        bits.append(f"The player character is {s['name']}, a {who}.")
    elif who:
        bits.append(f"The player character is a {who}.")
    if s['scores']:
        bits.append("Ability scores: " + fmt_scores(rules, s['scores']) + ".")
    if s['skills']:
        bits.append("Trained skills: " + ", ".join(s['skills']) + ".")
    if s['spells']:
        bits.append("Known spells: " + ", ".join(s['spells']) + ".")
    return " ".join(bits)
