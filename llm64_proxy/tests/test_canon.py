#!/usr/bin/env python3
"""The visual canon's pure half (docs/17): shape clamping, lenient
parsing, the staleness trigger, and the load-bearing phrases in the
build/update questions. Run: python3 tests/test_canon.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.scenecomp import (normalize_canon, parse_canon_reply, canon_stale,
                           canon_block, canon_build_question,
                           canon_update_question, CANON_MARKER,
                           CANON_PLAYER_MAX, CANON_ENTRY_MAX,
                           CANON_ENTITIES_MAX)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


# --- normalize: the caps are the contract -----------------------------

c = normalize_canon({'player': 'x' * 1000,
                     'npcs': {f'n{i}': 'd' * 500 for i in range(20)},
                     'places': {'Inn': ' padded '}})
check("player clamped", len(c['player']), CANON_PLAYER_MAX)
check("npc count clamped", len(c['npcs']), CANON_ENTITIES_MAX)
check("npc entry clamped", len(c['npcs']['n0']), CANON_ENTRY_MAX)
check("place whitespace stripped", c['places']['Inn'], 'padded')
check("first version is 1", c['version'], 1)

check("junk types refused", normalize_canon('not a dict'), None)
check("an empty ledger is no ledger",
      normalize_canon({'player': '', 'npcs': {}, 'places': {}}), None)
check("non-string entries dropped",
      normalize_canon({'player': 'p', 'npcs': {'a': 3, 'b': 'ok'}})
      ['npcs'], {'b': 'ok'})

prev = dict(c, version=4, built_at_msg=2)
check("version carries and bumps",
      normalize_canon({'player': 'p'}, prev)['version'], 5)
check("built_at carries",
      normalize_canon({'player': 'p'}, prev)['built_at_msg'], 2)

# --- lenient parsing --------------------------------------------------

j = ('{"player": "a wiry kobold", "npcs": {"Mara": "stout innkeeper"},'
     ' "places": {}}')
check("clean json parses", parse_canon_reply(j)['player'], 'a wiry kobold')
check("json inside prose parses",
      parse_canon_reply("Here you go!\n" + j + "\nHope that helps.")
      ['npcs'], {'Mara': 'stout innkeeper'})
check("prose-only falls back to the player entry",
      parse_canon_reply('A tall knight in dented plate.')['player'],
      'A tall knight in dented plate.')
check("broken json still salvages the text as prose",
      parse_canon_reply('{"player": broken')['player'],
      '{"player": broken')
check("empty reply is None", parse_canon_reply('   '), None)
check("None reply is None", parse_canon_reply(None), None)

# --- staleness: exact, cheap, whitespace-blind ------------------------

canon = normalize_canon({'player': 'p'})
canon['appearance_seen'] = 'a wiry traveler in a patched gray cloak'
check("same appearance is fresh",
      canon_stale(canon, 'a wiry traveler in a patched gray cloak'), False)
check("case and spacing do not trip it",
      canon_stale(canon, '  A Wiry  Traveler in a patched gray cloak '),
      False)
check("a rewritten appearance trips it",
      canon_stale(canon, 'a scarred knight in dented plate'), True)
check("no canon is never stale", canon_stale(None, 'anything'), False)
check("no appearance is never stale (nothing to compare)",
      canon_stale(canon, ''), False)

# --- the injection block ----------------------------------------------

full = normalize_canon({'player': 'a wiry kobold',
                        'npcs': {'Mara': 'stout innkeeper'},
                        'places': {'The Vault': 'iron-doored cellar'}})
b = canon_block(full)
check("block carries the authority heading",
      'AUTHORITATIVE VISUAL CANON' in b, True)
check("player entry verbatim", 'a wiry kobold' in b, True)
check("npc entry verbatim", 'Mara: stout innkeeper' in b, True)
check("place labeled as one", 'The Vault (place): iron-doored cellar' in b,
      True)
check("no canon, no block", canon_block(None), '')
check("marker stays out of the injected block (mock keys on it)",
      CANON_MARKER in b, False)

# --- the questions' load-bearing phrases ------------------------------

q = canon_build_question('user: hello', 'a wiry traveler', 'sheet text')
check("build question carries the marker", CANON_MARKER in q, True)
check("build sees the state appearance", 'a wiry traveler' in q, True)
check("build sees the character sheet", 'sheet text' in q, True)
check("build forbids invention", 'never invent' in q, True)

u = canon_update_question(full, 'user: the cloak burned away',
                          'a singed kobold')
check("update carries the marker", CANON_MARKER in u, True)
check("update shows the old ledger",
      'a wiry kobold' in u, True)
check("update names the new appearance", 'a singed kobold' in u, True)
check("update says the story wins", 'authoritative' in u, True)
check("update says keep the rest verbatim", 'VERBATIM' in u, True)
check("old ledger travels as valid json",
      json.loads(u.split('Current ledger:\n')[1]
                 .split('\n\n')[0])['player'], 'a wiry kobold')

if failures:
    print(f"{len(failures)} FAILURES")
    for f in failures:
        print("-", f)
    sys.exit(1)
print("test_canon: all checks passed")
