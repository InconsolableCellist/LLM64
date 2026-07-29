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
                     instructions: str = '', directive: str = '',
                     canon=None) -> str:
    """The one-shot question handed to the chat model to write an image
    prompt.

    convo        - the joined recent transcript (already trimmed).
    priors       - list of the last few illustration prompt strings.
    adv_state    - the [[STATE]] JSON string, or None.
    room         - the current map room dict (name/note), or None.
    character    - the chargen character sheet text, or ''.
    instructions - the player's /pic text, a request TO the illustrator.
    directive    - the narrator's [[IMAGE:]] text, a suggested shot.
    canon        - the visual_canon META dict (docs/17), or None.

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

    # The settled ledger outranks everything above it except the
    # player's explicit request: priors describe scenes, the canon
    # describes the people - and copying it verbatim is precisely what
    # keeps illustration N looking like illustration N-1 (docs/17).
    if canon:
        block = canon_block(canon)
        if block:
            parts.append(block)

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


# --- the visual canon (docs/17) ---------------------------------------
#
# A settled ledger of how the player, recurring NPCs and places LOOK,
# kept in conversation META and injected verbatim into every composition
# above. Everything here is pure - building the questions, parsing the
# replies, clamping the shape, deciding staleness - so the whole
# lifecycle tests without an event loop or a model. The one thing that
# is not here is the model call itself (protocol._ensure_canon).

# Hard caps, enforced on every write: a canon that cannot bloat is a
# canon that can ride along on every composition without budget anxiety.
CANON_PLAYER_MAX = 400
CANON_ENTRY_MAX = 200
CANON_ENTITIES_MAX = 8

# The marker the mock (and only the mock) keys on. It appears in the
# build/update QUESTIONS and nowhere else - deliberately distinct from
# the "AUTHORITATIVE VISUAL CANON" heading injected into compose
# questions, so a mock branch on it can never shadow the scene branch.
CANON_MARKER = "VISUAL CANON LEDGER"


def normalize_canon(obj, prev=None):
    """Clamp a parsed ledger into the canonical shape, or None if there
    is nothing usable in it. `prev` carries version/built_at forward."""
    if not isinstance(obj, dict):
        return None
    player = obj.get('player')
    player = player.strip()[:CANON_PLAYER_MAX] \
        if isinstance(player, str) else ''

    def take(d):
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                if len(out) >= CANON_ENTITIES_MAX:
                    break
                if isinstance(k, str) and isinstance(v, str) \
                        and k.strip() and v.strip():
                    out[k.strip()[:64]] = v.strip()[:CANON_ENTRY_MAX]
        return out

    npcs = take(obj.get('npcs'))
    places = take(obj.get('places'))
    if not player and not npcs and not places:
        return None
    prev = prev or {}
    return {
        'player': player,
        'npcs': npcs,
        'places': places,
        'appearance_seen': '',      # caller stamps what it built against
        'built_at_msg': prev.get('built_at_msg', 0),
        'updated_at_msg': prev.get('updated_at_msg', 0),
        'version': (prev.get('version') or 0) + 1,
    }


def parse_canon_reply(text, prev=None):
    """A model's ledger reply -> canon dict, leniently.

    JSON first (the first {...} span, so prose around it is fine); a
    model that answers entirely in prose gets its reply stored as the
    player entry - an imprecise canon still beats none. Returns None
    only for empty/unusable replies."""
    import json
    import re
    if not text or not text.strip():
        return None
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            got = normalize_canon(json.loads(m.group(0)), prev)
            if got:
                return got
        except (ValueError, TypeError):
            pass
    return normalize_canon({'player': text.strip()}, prev)


def _canon_norm(s):
    return ' '.join((s or '').lower().split())


def canon_stale(canon, appearance):
    """Does the STATE block's appearance contradict the ledger?

    Deliberately the cheapest heuristic that matters: exact (whitespace-
    and case-insensitive) comparison against the appearance the canon
    was built against. The narrator rewriting that string is the one
    authoritative signal that the character's look changed; anything
    fuzzier costs a model call per turn (docs/17 section 4)."""
    if not canon or not appearance:
        return False
    return _canon_norm(appearance) != _canon_norm(
        canon.get('appearance_seen', ''))


def canon_block(canon):
    """The ledger as the injection block compose_question carries."""
    if not canon:
        return ''
    lines = ["AUTHORITATIVE VISUAL CANON - these descriptions are "
             "settled. Wherever these people or places appear, repeat "
             "them precisely; they outrank the transcript's phrasing:"]
    if canon.get('player'):
        lines.append("Player character: " + canon['player'])
    for name, desc in (canon.get('npcs') or {}).items():
        lines.append(name + ": " + desc)
    for name, desc in (canon.get('places') or {}).items():
        lines.append(name + " (place): " + desc)
    return "\n".join(lines) if len(lines) > 1 else ''


def canon_build_question(convo, appearance, character):
    """The one-shot that writes the ledger the first time a conversation
    illustrates (docs/17 section 2)."""
    parts = [
        "Below is the opening of a text adventure. Write its "
        + CANON_MARKER + ": the settled visual description of the "
        "player character, and of any recurring characters or places "
        "whose look the story has already established. Be precise and "
        "concrete (build, hair, clothing, colors, what they carry) - "
        "an illustrator will repeat these verbatim for the rest of the "
        "story. Reply with ONLY a JSON object of the form "
        '{"player": "...", "npcs": {"Name": "..."}, '
        '"places": {"Name": "..."}} - omit npcs/places entries you '
        "cannot yet describe; never invent details the story does not "
        "support."]
    if appearance:
        parts.append("The game state currently describes the player "
                     "as: " + appearance)
    if character:
        parts.append("The player's character sheet:\n" + character)
    parts.append("Transcript:\n" + convo)
    return "\n\n".join(parts)


def canon_update_question(canon, convo, appearance):
    """The amendment question, fired only when canon_stale says the
    narrative contradicts the ledger. The instruction that matters:
    the NARRATIVE wins, and everything it did not contradict stays
    word for word."""
    import json
    return "\n\n".join([
        "A text adventure's " + CANON_MARKER + " below has been "
        "contradicted by the story. The story is authoritative: "
        "rewrite ONLY the entries the story has changed, and keep "
        "every other entry VERBATIM, word for word. Reply with ONLY "
        "the full corrected JSON object, same shape as the original.",
        "Current ledger:\n" + json.dumps(
            {'player': canon.get('player', ''),
             'npcs': canon.get('npcs') or {},
             'places': canon.get('places') or {}}),
        "The game state now describes the player as: " + appearance,
        "Recent story:\n" + convo,
    ])
