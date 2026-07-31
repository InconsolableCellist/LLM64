#!/usr/bin/env python3
"""Back-filling the static half of a sheet for an adventure that never
ran chargen.

The thing under test is a refusal as much as a parse: the model is asked
for what the STORY established, and everything it is not entitled to
supply - ability scores above all - has to be dropped no matter how
confidently it offers it. A rolled score means four dice; a back-filled
character had none, and a window showing invented numbers is worse than
a window showing a blank.

Run: python3 tests/test_backfill.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import chargen
from src.chargen import (backfill_question, parse_backfill, describe_sheet,
                         BACKFILL_SCHEMA, BACKFILL_LIST_MAX,
                         BACKFILL_ITEM_MAX, BACKFILL_STR_MAX)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


RULES = chargen.load_rules()


# --- the question -----------------------------------------------------

q = backfill_question("user: i draw my sword\nnarrator: Kesh the elf...",
                      '{"hp":14,"location":"The Vestry"}')
check("question carries the schema", BACKFILL_SCHEMA in q, True)
check("question forbids invention", 'invented one is not' in q, True)
check("question refuses ability scores",
      'Do NOT report ability scores' in q, True)
check("question carries the state", 'The Vestry' in q, True)
check("question carries the transcript", 'Kesh the elf' in q, True)

# No state is the common case for a game that never had a sheet; the
# question must still be well-formed rather than carrying an empty label.
q2 = backfill_question("user: hello")
check("stateless question omits the label",
      'game state currently tracked' in q2, False)


# --- the parse: what comes back ---------------------------------------

s = parse_backfill(
    'Sure! Here you go:\n'
    '{"name":"Kesh","race":"Half-elf","class":"wizard",'
    '"skills":["stealth","lore"],"spells":["light"],'
    '"gear":["oak staff","satchel"]}\nHope that helps.')
check("name read", s['name'], 'Kesh')
check("race read", s['race'], 'Half-elf')
check("lists read", s['skills'], ['stealth', 'lore'])
check("marked as derived", s['derived'], True)
check("scores stay empty", s['scores'], {})
check("no hit die is claimed", s['hit_die'], None)

# The rules spell the class, so a back-filled character and a rolled one
# are the same word everywhere downstream.
sr = parse_backfill('{"class":"wizard"}', RULES)
check("known class canonicalised", sr['class'],
      (chargen.find_class(RULES, 'wizard') or {}).get('name'))
check("unknown class kept as written",
      parse_backfill('{"class":"Bone Sommelier"}', RULES)['class'],
      'Bone Sommelier')


# --- the parse: what must NOT come back --------------------------------

greedy = parse_backfill(
    '{"name":"Kesh","scores":{"STR":18,"DEX":17,"CON":16,"INT":18},'
    '"hit_die":12,"hp":40,"gold":500,"location":"anywhere"}')
check("offered scores refused", greedy['scores'], {})
check("offered hit die refused", greedy['hit_die'], None)
check("state keys never leak into the sheet",
      [k for k in ('hp', 'gold', 'location') if k in greedy], [])

check("no json is no sheet", parse_backfill('I am not sure who they are.'),
      {})
check("empty text is no sheet", parse_backfill(''), {})
check("malformed json is no sheet", parse_backfill('{"name": }'), {})
check("a non-object is no sheet", parse_backfill('[1,2,3]'), {})
# Every field empty means the story established nothing. Storing that
# would be worse than storing nothing: the meta key exists afterward, so
# the next /sheet would stop trying.
check("an empty answer stores nothing",
      parse_backfill('{"name":"","race":"","class":"",'
                     '"skills":[],"spells":[],"gear":[]}'), {})
check("whitespace-only fields are empty too",
      parse_backfill('{"name":"  ","skills":["  ", ""]}'), {})


# --- the clamps -------------------------------------------------------

big = parse_backfill(
    '{"name":"' + 'N' * 200 + '","race":"elf",'
    '"skills":' + str(['s%d' % i for i in range(30)]).replace("'", '"') + ','
    '"gear":["' + 'G' * 200 + '"]}')
check("strings clamped", len(big['name']), BACKFILL_STR_MAX)
check("lists clamped", len(big['skills']), BACKFILL_LIST_MAX)
check("list items clamped", len(big['gear'][0]), BACKFILL_ITEM_MAX)
check("non-string list entries dropped",
      parse_backfill('{"race":"elf","gear":["rope", 7, null, "lamp"]}')['gear'],
      ['rope', 'lamp'])
check("a non-list where a list belongs is empty",
      parse_backfill('{"race":"elf","skills":"stealth and lore"}')['skills'],
      [])


# --- the prose it turns into ------------------------------------------

text = describe_sheet(RULES, s)
check("prose names the character", 'Kesh' in text, True)
check("prose gives race and class", 'Half-elf' in text, True)
# A derived sheet has no scores, so the ability line must be absent
# rather than present and empty.
check("prose omits absent scores", 'Ability scores' in text, False)
# "Started the adventure carrying" is a claim about turn one, which is
# exactly what nobody knows about a back-filled character.
check("derived gear is not claimed as starting kit",
      'Started the adventure' in text, False)
check("derived gear is still listed", 'oak staff' in text, True)

# The rolled path must be untouched by the split.
rolled = chargen.sheet(RULES, {
    'name': 'Bram', 'race': 'Dwarf', 'class': 'Wanderer',
    'scores': {ab: 12 for ab in RULES['abilities']},
    'gear': ['dagger']})
rq = chargen.describe(RULES, rolled and {
    'name': 'Bram', 'race': 'Dwarf', 'class': 'Wanderer',
    'scores': {ab: 12 for ab in RULES['abilities']},
    'gear': ['dagger']})
check("rolled kit still reads as starting kit",
      'Started the adventure carrying' in rq, True)
check("rolled sheet still states its scores", 'Ability scores' in rq, True)
check("describe still agrees with describe_sheet",
      rq, describe_sheet(RULES, rolled))

if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("-", f)
    sys.exit(1)
print("test_backfill: all checks passed")
