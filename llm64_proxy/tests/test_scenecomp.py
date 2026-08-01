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
from src.scenecomp import compose_question

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
    check("instructions marked as outranking the rest",
          has(q, "outranks everything below"))
    # A detail request must say it may be a detail, not the whole room.
    check("detail-not-room hint present", has(q, "detail rather than a"))
    # Two rules that exist because both failures were seen in real
    # compositions: the whole party turning up for a request naming one
    # character, and a named action becoming a standing portrait.
    check("one-character requests settle the cast",
          has(q, "alone in the picture"))
    check("named actions must survive composition", has(q, "names an ACTION"))


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


def test_focus():
    """A /pic naming one character narrows the ledger to that character.

    Not a prompt-politeness matter: every canon entry in the question is
    an invitation to draw that person, and describing the rest of the
    party in a question that asks for a portrait of one of them is how
    the party ends up in the portrait. Deterministic here rather than
    asked of the model, which was measured refusing to drop them."""
    print("a named subject narrows the cast (docs/17)")
    from src.scenecomp import (normalize_canon, focus_names, canon_block,
                               canon_player_name)
    canon = normalize_canon({
        'player': 'Vess Kolvar: a wiry kobold in a patched maid outfit',
        'npcs': {'Bruc': 'a grey wolf mercenary in battered ring mail',
                 'Mara': 'a stout innkeeper in grease-stained leathers'},
        'places': {'The Vault': 'a flooded stone cellar'}})

    check("the player's name comes out of the ledger entry",
          canon_player_name(canon) == 'Vess Kolvar',
          canon_player_name(canon))
    check("a first name matches the full name",
          focus_names('vess', canon) == {'Vess Kolvar'})
    check("an npc matches",
          focus_names('bruc drawing his sword', canon) == {'Bruc'})
    check("two named characters both match",
          focus_names('vess and bruc', canon) == {'Vess Kolvar', 'Bruc'})
    check("a request naming nobody narrows nothing",
          focus_names('the footprints in the sand', canon) == set())
    check("no request, no narrowing", focus_names('', canon) == set())
    # Word boundaries: "Mara" must not be found inside "marauder".
    check("substrings do not count as names",
          focus_names('the marauders outside', canon) == set(),
          focus_names('the marauders outside', canon))

    q = compose_question(CONVO, [], STATE, ROOM, CHARACTER, canon=canon,
                         instructions='bruc drawing his sword')
    check("the named character is described", "grey wolf mercenary" in q)
    check("the others are not", "grease-stained leathers" not in q
          and "patched maid outfit" not in q)
    check("the place survives narrowing", "a flooded stone cellar" in q)
    check("the chargen sheet goes too - it is the PLAYER's identity",
          "a Kobold Femboy maid" not in q)
    check("and the cast is stated outright", has(q, "contains exactly Bruc"))

    # Naming the player keeps the player's sheet and entry.
    qp = compose_question(CONVO, [], STATE, ROOM, CHARACTER, canon=canon,
                          instructions='vess')
    check("naming the player keeps her ledger entry",
          "patched maid outfit" in qp)
    check("...and her sheet", "a Kobold Femboy maid" in qp)
    check("...and drops the NPCs", "battered ring mail" not in qp)

    # A request that names nobody leaves the question exactly as it was.
    q0 = compose_question(CONVO, [], STATE, ROOM, CHARACTER, canon=canon,
                          instructions='the altar')
    check("no narrowing means the whole ledger",
          "battered ring mail" in q0 and "patched maid outfit" in q0)


def test_tags():
    """[images] prompt_format = "tags": the composition writes Danbooru
    tags instead of a sentence, because that is what an Illustrious /
    Pony / NoobAI checkpoint was trained on. The two tags that carry
    the weight are the cast tag (an extra figure otherwise) and the
    framing tag (a subject at the horizon otherwise)."""
    print("tag-mode composition")
    from src.scenecomp import TAG_CAST, TAG_FRAMING
    q = compose_question(CONVO, [], STATE, ROOM, CHARACTER, fmt='tags')
    check("asks for tags, not a sentence", has(q, "TAG LIST"))
    check("no prose contract left over", not has(q, "ONE sentence"))
    for tag in TAG_CAST:
        check(f"cast vocabulary offered: {tag}", tag in q)
    for tag in TAG_FRAMING:
        check(f"framing vocabulary offered: {tag}", tag in q)
    check("framing is mandatory, not optional", has(q, "exactly one of"))
    check("an action slot exists", has(q, "WHAT IS HAPPENING"))
    check("the tag budget is stated", has(q, "never more than 40"))
    check("the cast tag is tied to the count",
          has(q, "cast tag must match that count"))
    check("the prose roster is gone",
          not has(q, "explicit list of who is present"))
    check("cast rules still apply", has(q, "cast rules"))
    check("transcript still present", CONVO.split("\n")[0] in q)

    # The reply cap: a 30-tag list does not fit in the 400 chars a prose
    # sentence lives under, and what a cut leaves behind is a made-up
    # token in the list's highest-weight position.
    from src.scenecomp import tidy_tags, TAG_SCENE_LIMIT
    check("tag lists get their own, larger cap", TAG_SCENE_LIMIT > 400)
    check("a wrapped reply becomes one line",
          tidy_tags("solo, 1girl,\nanthro,\n  kobold ")
          == "solo, 1girl, anthro, kobold")
    check("duplicates collapse",
          tidy_tags("anthro, kobold, anthro") == "anthro, kobold")
    check("empty tags from a trailing comma go",
          tidy_tags("solo, kobold, ,") == "solo, kobold")
    check("a truncated reply loses its half-written tag",
          tidy_tags("solo, kobold, dark atmos", truncated=True)
          == "solo, kobold")
    check("an untruncated reply keeps every tag",
          tidy_tags("solo, kobold, dark atmosphere")
          == "solo, kobold, dark atmosphere")
    check("a one-tag reply is never emptied",
          tidy_tags("solo", truncated=True) == "solo")
    check("nothing in, nothing out", tidy_tags("") == "")

    # Prose stays the default: every existing config keeps its behaviour.
    check("prose is what an unset format gets",
          compose_question(CONVO, [], STATE, ROOM, CHARACTER)
          == compose_question(CONVO, [], STATE, ROOM, CHARACTER,
                              fmt='prose'))
    check("prose mode is untouched by tag mode existing",
          has(compose_question(CONVO, [], STATE, ROOM, CHARACTER),
              "ONE sentence"))


def test_trim_scene():
    """The prose cap must not eat the roster - it used to, every time.

    Tag mode ends on the cast tag, which tidy_tags protects; prose ends
    on the who-is-present line, and the 400-char cap this replaces cut
    it off mid-word along with the description's tail."""
    print("trim_scene (prose)")
    from src.scenecomp import trim_scene, PROSE_SCENE_LIMIT
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
    check("over-budget scene is capped", len(out) <= PROSE_SCENE_LIMIT,
          f"got {len(out)}")
    check("the roster survives the cap", out.endswith(roster))
    check("the prose is what gave way",
          len(out.rpartition('\n')[0]) < len(body))
    check("no mid-word cut", not out.rpartition('\n')[0].endswith(" "))

    short = "A cold stair.\n" + roster
    check("a scene inside budget is untouched", trim_scene(short) == short)
    # No roster to protect: cap the whole thing, still on a word boundary.
    plain = trim_scene(body, 100)
    check("rosterless scene still capped", len(plain) <= 100)
    check("rosterless cut lands on a word", not plain.endswith("rough-c"))
    check("empty input survives", trim_scene('') == '')


if __name__ == "__main__":
    test_full()
    test_instructions()
    test_directive()
    test_degradation()
    test_partial_room()
    test_canon()
    test_focus()
    test_tags()
    test_trim_scene()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all scenecomp tests passed")
