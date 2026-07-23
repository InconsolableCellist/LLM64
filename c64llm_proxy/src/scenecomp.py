"""Build the illustrator's composition question (docs/13).

Every scene illustration - whether the player typed /pic <text>, the
narrator emitted an [[IMAGE:]] directive, or the player asked for a bare
/pic - is written by the chat model in one composition step, so the
prompt is always anchored to game state rather than taken verbatim. This
module builds that step's question from whatever context is in hand,
kept as a pure function so it is testable without an event loop or a
model (tests/test_scenecomp.py).
"""


# Image models obey "the scene contains only X" far better than "do not
# include Z" - negations are weak in image prompting - so the composed
# sentence must END with an explicit roster of who is present.
_ROSTER_EXAMPLE = ('e.g. "- the only figure present is the kobold in a '
                   'maid outfit", or for an empty room "- an empty, '
                   'unpeopled scene"')


def compose_question(convo: str, priors, adv_state, room, character,
                     instructions: str = '', directive: str = '') -> str:
    """The one-shot question handed to the chat model to write an image
    prompt.

    convo        - the joined recent transcript (already trimmed).
    priors       - list of the last few illustration prompt strings.
    adv_state    - the [[STATE]] JSON string, or None.
    room         - the current map room dict (name/note), or None.
    character    - the chargen character sheet text, or ''.
    instructions - the player's /pic text, a request TO the illustrator.
    directive    - the narrator's [[IMAGE:]] text, a suggested shot.

    Every game-state argument is optional, so roleplay mode (transcript
    only) still yields a well-formed question with no leftover
    placeholders.
    """
    parts = []

    # Task and any steering come first, so a small model reads them
    # before the wall of transcript.
    parts.append(
        "Below is the latest part of a text adventure. Write a vivid "
        "visual description of the CURRENT scene for an illustrator, in "
        "ONE sentence (two only if the scene is genuinely busy). Include "
        "the established appearance of the characters and setting "
        "(clothing, hair, architecture, lighting) from earlier in the "
        "story. Reply with only that description, no preamble.")

    if instructions:
        parts.append(
            "The player asked the illustrator for: " + instructions +
            "\nHonor this request: find what it refers to in the "
            "transcript and describe THAT. It may name a single detail "
            "rather than the whole room; if so, illustrate the detail. "
            "This request outranks the default framing above.")
    if directive:
        parts.append(
            "The narrator suggested this shot: " + directive +
            "\nUse it as the basis, corrected to obey the cast rules "
            "below.")

    if priors:
        parts.append(
            "Earlier illustrations in this story showed:\n"
            + "\n".join(f"- {p}" for p in priors)
            + "\nKeep characters and places visually consistent with "
              "those.")

    if character:
        parts.append(
            "The player character's fixed visual identity:\n" + character +
            "\nIf the player character is shown, they MUST match this "
            "(race, clothing, what they carry).")

    if adv_state:
        parts.append("Authoritative game state: " + adv_state)

    if room:
        loc = []
        if room.get('name'):
            loc.append("The player is currently in: " + room['name'] + ".")
        if room.get('note'):
            loc.append("Notable here: " + room['note'] +
                       " - include such props when the scene calls for "
                       "them.")
        if loc:
            parts.append(" ".join(loc))

    parts.append(
        "Cast rules for the illustration:\n"
        "- Show the current location as described in the transcript.\n"
        "- The player character may appear ONLY if the request asks for "
        "them or the current scene is about them; if shown, they must "
        "match the identity and game state above.\n"
        "- Companions listed in the game state may appear.\n"
        "- Include NO other people, creatures, or monsters unless the "
        "current scene in the transcript explicitly puts them there.\n"
        "- Include props and scenery named in the scene or the location "
        "note (keys, doors, altars).\n"
        "- End your description with an explicit list of who is present, "
        + _ROSTER_EXAMPLE + ".")

    parts.append("Transcript:\n" + convo)

    return "\n\n".join(parts)
