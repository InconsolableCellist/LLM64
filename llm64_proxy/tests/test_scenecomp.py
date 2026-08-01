#!/usr/bin/env python3
"""compose_question: the illustrator's composition prompt (docs/13).

The prompt for every scene illustration is built here from whatever game
state is in hand. These assertions pin the elements the handoff requires
- the cast rules, the character/room/state anchors, the /pic steering,
and clean degradation to a transcript-only question in roleplay mode.
No event loop, no model, no PIL.

Run: python3 tests/test_scenecomp.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.scenecomp import (compose_question, trim_scene, cast_tags,
                           SCENE_CHARS)

CONVO = ("user: I push open the door.\n"
         "assistant: The hinges shriek and dust drifts down.")
STATE = ('{"location":"The Vault","appearance":"a traveler in tattered '
         'linen robes with slimy hands","companions":[],'
         '"inventory":["rope"]}')
ROOM = {'name': 'The Vault', 'note': 'an iron key hangs on a hook'}
CHARACTER = ("The player character is ?, a Kobold Femboy maid. "
             "Carrying: gun, french maid outfit, cigarettes.")

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def has(q, *subs):
    """Every substring present (case-insensitive)."""
    low = q.lower()
    return all(s.lower() in low for s in subs)


def test_full():
    print("full context (adventure)")
    q = compose_question(CONVO, ['a ruined tower at dusk'], STATE, ROOM,
                         CHARACTER)
    # adv_state present -> appearance phrase, companions rule, no-extras.
    check("appearance phrase from state present",
          "a traveler in tattered linen robes with slimy hands" in q)
    check("companions rule present", has(q, "companions", "may appear"))
    check("no-extra-creatures rule present",
          has(q, "no other people, creatures, or monsters"))
    # room note -> the prop text is carried through.
    check("room note present", "an iron key hangs on a hook" in q)
    check("room name present", "The Vault" in q)
    # character sheet -> present verbatim.
    check("character sheet present", "a Kobold Femboy maid" in q)
    # the positive-roster requirement (negations are weak for image models).
    check("roster requirement present",
          has(q, "explicit list of who is present"))
    # prior illustration carried for consistency.
    check("prior illustration present", "a ruined tower at dusk" in q)
    # output contract.
    check("output contract present", has(q, "one sentence", "no preamble"))
    check("transcript present", CONVO.split("\n")[0] in q)


def test_instructions():
    print("player instructions steer and outrank")
    q = compose_question(CONVO, [], STATE, ROOM, CHARACTER,
                         instructions='the footprints in the sand')
    check("instructions text present", "the footprints in the sand" in q)
    check("steering framed as a request to the illustrator",
          has(q, "asked the illustrator for"))
    check("instructions marked as outranking the default",
          has(q, "outranks the default"))
    # A detail request must say it may be a detail, not the whole room.
    check("detail-not-room hint present", has(q, "single detail"))


def test_directive():
    print("narrator directive as suggestion")
    q = compose_question(CONVO, [], STATE, ROOM, CHARACTER,
                         directive='A dragon looms over the party')
    check("directive text present", "A dragon looms over the party" in q)
    check("directive framed as a suggestion",
          has(q, "narrator suggested this shot"))
    check("directive still subject to the rules",
          has(q, "corrected to obey"))


def test_degradation():
    print("roleplay degradation (no state/room/character)")
    q = compose_question(CONVO, [], None, None, '')
    # Still well-formed: transcript + output contract + cast rules.
    check("transcript still present", CONVO.split("\n")[0] in q)
    check("output contract still present", has(q, "one sentence"))
    check("cast rules still present", has(q, "cast rules"))
    # No leftover placeholder / empty-label text from missing sections.
    check("no empty 'game state' label",
          "authoritative game state:" not in q.lower())
    check("no empty character label",
          "fixed visual identity" not in q.lower())
    check("no empty instructions label",
          "asked the illustrator" not in q.lower())
    check("no None leaked into the text", "None" not in q)


def test_partial_room():
    print("room with a name but no note")
    q = compose_question(CONVO, [], None, {'name': 'A Cold Cellar'}, '')
    check("room name present", "A Cold Cellar" in q)
    check("no dangling note phrase", "Notable here" not in q)


def test_canon():
    print("visual canon injection (docs/17)")
    from src.scenecomp import normalize_canon
    canon = normalize_canon({
        'player': 'a wiry kobold in a patched gray cloak',
        'npcs': {'Mara': 'a stout innkeeper in grease-stained leathers'},
        'places': {}})
    q = compose_question(CONVO, [], STATE, ROOM, CHARACTER, canon=canon)
    check("authority heading present",
          "AUTHORITATIVE VISUAL CANON" in q)
    check("player entry injected verbatim",
          "a wiry kobold in a patched gray cloak" in q)
    check("npc entry injected verbatim",
          "Mara: a stout innkeeper in grease-stained leathers" in q)
    check("outranking wording present",
          has(q, "outrank the transcript"))
    check("character sheet still rides along (mechanics, not looks)",
          "a Kobold Femboy maid" in q)
    # Absent canon must leave no trace - the pre-canon question exactly.
    q0 = compose_question(CONVO, [], STATE, ROOM, CHARACTER)
    check("no canon, no heading",
          "AUTHORITATIVE VISUAL CANON" not in q0)
    check("no canon, question unchanged",
          q0 == compose_question(CONVO, [], STATE, ROOM, CHARACTER,
                                 canon=None))


def test_trim_scene():
    """The cap must not eat the roster - it used to, every time.

    The 400-char cap this replaces cut the composed sentence mid-word
    and took the who-is-present line with it, which is precisely the
    line that keeps extra creatures out of the picture."""
    print("trim_scene")
    body = ("A narrow spiral stone stair descends into darkness, rough-cut "
            "walls closing in, a cracked funeral urn with dusty bones in a "
            "wall niche, cold air and a faint damp gleam on the steps, lit "
            "by the warm orange glow of a torch held by a young Khajiit "
            "woman in a worn leather jerkin over a simple tunic, a holy "
            "symbol on a cord around her neck, a shortsword and pouch at "
            "her belt, her short golden fur striped with darker markings, "
            "her tail low and still, breath faintly visible in the cold, "
            "the stair curving away below her into a blackness the torch "
            "cannot reach, salt crusting the mortar between the stones, "
            "the air tasting of brine and wet lime, and somewhere far "
            "below the slow drip of water into standing water.")
    roster = "- the only figure present is the young Khajiit woman"
    out = trim_scene(body + "\n" + roster)
    check("over-budget scene is capped", len(out) <= SCENE_CHARS,
          f"got {len(out)}")
    check("the roster survives the cap", out.endswith(roster))
    check("the prose is what gave way", len(out.rpartition('\n')[0])
          < len(body))
    check("no mid-word cut", not out.rpartition('\n')[0].endswith(" "))

    short = "A cold stair.\n" + roster
    check("a scene inside budget is untouched", trim_scene(short) == short)
    # No roster to protect: cap the whole thing, still on a word boundary.
    plain = trim_scene(body, 100)
    check("rosterless scene still capped", len(plain) <= 100)
    check("rosterless cut lands on a word", not plain.endswith("rough-c"))


def test_cast_tags():
    """The roster, once it survives, in the vocabulary image models obey."""
    print("cast_tags")
    solo_pos, solo_neg = cast_tags(
        "A stair.\n- the only figure present is the Khajiit woman")
    check("one figure -> solo", "solo" in solo_pos)
    check("one figure pushes crowds away", "multiple girls" in solo_neg)
    check("one figure pushes beasts away",
          has(solo_neg, "monster", "macro", "giant"))

    empty_pos, empty_neg = cast_tags(
        "A cold crypt corridor.\n- an empty, unpeopled scene")
    check("empty scene -> no humans", "no humans" in empty_pos)
    check("empty scene pushes people away", has(empty_neg, "1girl", "solo"))
    check("empty scene pushes creatures away",
          has(empty_neg, "creature", "monster"))

    # Anything it cannot read confidently must change nothing at all.
    check("a crowded roster claims nothing",
          cast_tags("The hall.\n- present are the player, Marja and the "
                    "drowned priest") == ('', ''))
    check("no roster claims nothing",
          cast_tags("just some prose about a room") == ('', ''))
    check("empty input claims nothing", cast_tags('') == ('', ''))


if __name__ == "__main__":
    test_full()
    test_instructions()
    test_directive()
    test_degradation()
    test_partial_room()
    test_canon()
    test_trim_scene()
    test_cast_tags()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all scenecomp tests passed")
