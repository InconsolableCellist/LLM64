"""Build the illustrator's composition question (docs/13).

Every scene illustration - whether the player typed /pic <text>, the
narrator emitted an [[IMAGE:]] directive, or the player asked for a bare
/pic - is written by the chat model in one composition step, so the
prompt is always anchored to game state rather than taken verbatim. This
module builds that step's question from whatever context is in hand,
kept as a pure function so it is testable without an event loop or a
model (tests/test_scenecomp.py).
"""

import re


# Image models obey "the scene contains only X" far better than "do not
# include Z" - negations are weak in image prompting - so the composed
# sentence must END with an explicit roster of who is present.
_ROSTER_EXAMPLE = ('e.g. "- the only figure present is the kobold in a '
                   'maid outfit", or for an empty room "- an empty, '
                   'unpeopled scene"')

# --- tag mode ---------------------------------------------------------
#
# A Danbooru/e621-lineage checkpoint (Illustrious, NoobAI, Pony) was
# trained on tag strings, and a well-formed prose sentence is the wrong
# shape for it in ways that are not subtle: prose "the only figure
# present is Bruc" gets a second figure anyway, while the single token
# `solo` does not, and prose camera language ("a close view of") is
# ignored where `close-up` is obeyed. So for those models the
# COMPOSITION step writes tags instead, and the two failures that
# survived every prompt-prefix attempt - an extra character, and a
# subject the size of an ant in a wide establishing shot - become one
# tag each.
#
# The vocabulary below is deliberately small and fixed. A free-for-all
# tag list from a 26B model drifts into invented tags the checkpoint has
# never seen; naming the allowed values makes the two that matter -
# cast and framing - land every time.

# Danbooru cast tags. `no humans` is not a negation here: it is the tag
# for "the characters in this picture are not human", which is what an
# all-anthro cast is, and it is the single most effective way to stop a
# beast-person from being drawn with a human face.
TAG_CAST = ("solo", "duo", "no humans, scenery")

# Framing, weakest to strongest crop. One of these is mandatory: the
# model picks a default composition when none is named, and its default
# is a wide shot with the subject at the horizon.
TAG_FRAMING = ("close-up", "portrait", "upper body", "cowboy shot",
               "full body", "wide shot", "scenery")

# How long a composed tag list may be. Around 30-40 tags is what the
# task below asks for, and that runs 400-600 characters - past the
# 400-char cap a prose sentence lives under, which is why this is its
# own number rather than a shared one.
TAG_SCENE_LIMIT = 700

_TAG_EXAMPLE = (
    "solo, 1girl, no humans, anthro, kobold, adult, small build, red "
    "scales, cream underbelly, chipped horn, maid outfit, holding a "
    "lantern up in one hand, looking back over her shoulder, upper "
    "body, standing in a narrow stone corridor, wet flagstones, ivy on "
    "the walls, lantern light, deep shadow, dark fantasy")

# The vocabulary rule, and the reason it needs saying. A narrator writes
# "a bruised sky above the Hollow Chapel"; the checkpoint has been
# trained on neither of those phrases, so it renders roughly nothing
# from them. It HAS seen "twilight sky", "purple clouds" and "ruined
# stone chapel" tens of thousands of times. The composition step is the
# only place this translation can happen - by the time the prompt
# reaches the backend it is too late.
_PLAIN_WORDS = (
    "Write it in words an image model knows: plain concrete nouns and "
    "ordinary adjectives, the vocabulary of a photo caption. Two rules "
    "that matter more than they look:\n"
    "- NO PROPER NOUNS. A name means nothing to an image model. "
    "\"Bruc\" is nothing; \"grey wolf in ring mail\" is a picture. "
    "\"The Hollow Chapel\" is nothing; \"ruined stone chapel\" is a "
    "picture.\n"
    "- NO LITERARY PHRASING. Translate the narrator's language into "
    "what a camera would see: \"a bruised sky\" becomes \"twilight sky, "
    "purple clouds\"; \"a spiral of teeth\" becomes \"spiral carved in "
    "stone, rows of teeth\"; \"something drags itself upright\" becomes "
    "whatever the thing actually is.")


def _tag_task(fmt_note: str = '') -> str:
    """The tag-mode task block: what to write, in what order."""
    return (
        "Below is the latest part of a text adventure. Describe the "
        "CURRENT scene for an image model as a TAG LIST - lowercase, "
        "comma-separated, in the Danbooru/e621 style, no preamble. "
        "Write it in this order:\n"
        "1. CAST, first and exactly one of: " + ", ".join(TAG_CAST)
        + ". Add `1girl` or `1boy` for the main character when the story "
        "gives them a gender, and add `no humans` as well whenever every "
        "character in the shot is an anthro, beast or monster rather "
        "than a human.\n"
        "2. WHO THEY ARE: for each character actually in the shot, their "
        "settled look as tags - anthro, the species, muzzle or snout, "
        "ears, fur or scales and their colours, tail, clothing. Copy "
        "these from the descriptions given below rather than inventing "
        "them, and keep it to the SIX or so most recognisable tags per "
        "character: a long costume inventory crowds out the scene. "
        "Include `adult` and a build (`small build`, `athletic build`, "
        "`towering`) for every grown character - a small species with "
        "no age tag comes out looking like a child.\n"
        "3. WHAT IS HAPPENING: the pose and the action. If the request "
        "or the transcript names an action, it belongs here; a picture "
        "of someone standing still when the story has them drawing a "
        "blade is the wrong picture. Say what they are doing WITH the "
        "place too - `walking in through a broken doorway` rather than "
        "`standing`, or two characters end up facing each other in the "
        "middle of the frame doing nothing.\n"
        "4. FRAMING, exactly one of: " + ", ".join(TAG_FRAMING)
        + ". Choose the one that makes the subject readable: a character "
        "the scene is about wants upper body or cowboy shot, a room or "
        "a view wants wide shot or scenery.\n"
        "5. WHERE: the place, its architecture, and the props the scene "
        "names.\n"
        "6. LIGHT AND MOOD: the light source and the atmosphere.\n"
        + _PLAIN_WORDS + "\n"
        # The hybrid: these checkpoints were captioned with tag lists
        # that carry short natural phrases inside them, which is the
        # only way to express a relationship ("holding a lantern up",
        # "rain through the window") that no single tag covers.
        "Slots 3 and 5 may be short plain phrases as well as single "
        "tags - `holding a lantern up in one hand`, `rain visible "
        "through a broken window` - which is how these models were "
        "captioned. Slots 1 and 4 must be the exact tags listed above, "
        "nothing else.\n"
        "Around 30 tags in total, never more than 40 - past that the "
        "image model reads the tail of the list as noise.\n"
        "Example of the shape wanted:\n" + _TAG_EXAMPLE
        + (("\n" + fmt_note) if fmt_note else ""))


def tidy_tags(text, truncated=False):
    """A tag-list reply, normalised.

    One line (models like to wrap), no empty or duplicate tags, and -
    when the reply hit the caller's length cap - no half-written tag
    dangling off the end. That last one is not cosmetic: a truncated
    "dark atmos" is a token the checkpoint has never seen sitting in
    the highest-weight position of the list, the end.
    """
    tags, seen = [], set()
    for raw in (text or '').replace('\n', ',').split(','):
        tag = ' '.join(raw.split())
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    if truncated and len(tags) > 1:
        tags.pop()
    return ', '.join(tags)


def compose_question(convo: str, priors, adv_state, room, character,
                     instructions: str = '', directive: str = '',
                     canon=None, fmt: str = 'prose') -> str:
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
    fmt          - 'prose' (the default: one sentence, what Flux and the
                   API backends want) or 'tags' (a Danbooru-style tag
                   list, for Illustrious/Pony/NoobAI checkpoints). Set
                   by [images] prompt_format, which a style preset can
                   carry - see imgstyles.py.

    Every game-state argument is optional, so roleplay mode (transcript
    only) still yields a well-formed question with no leftover
    placeholders.
    """
    tags = fmt == 'tags'
    # Which canon characters the request names, if any. Decided up front
    # because three separate blocks below depend on it: the request's
    # own framing rule, how much of the ledger is injected, and the
    # final word on the cast.
    focus = focus_names(instructions, canon)
    parts = []

    # Task and any steering come first, so a small model reads them
    # before the wall of transcript.
    parts.append(_tag_task() if tags else (
        "Below is the latest part of a text adventure. Write a vivid "
        "visual description of the CURRENT scene for an illustrator, in "
        "ONE sentence (two only if the scene is genuinely busy). Include "
        "the established appearance of the characters and setting "
        "(clothing, hair, architecture, lighting) from earlier in the "
        "story. Reply with only that description, no preamble.\n"
        # The narrator's prose is written for a reader; the illustrator
        # is a diffusion model, which renders concepts it has seen and
        # nothing else. Same rule in both modes, for the same reason.
        + _PLAIN_WORDS))

    if instructions:
        # A checklist, not a paragraph: a 20-30B model follows short
        # imperatives far more reliably, and every line here is a
        # failure that was observed - the whole party turning up for a
        # request naming one character, and a named action ("drawing
        # his sword") quietly becoming a standing portrait.
        parts.append(
            "The player asked the illustrator for: " + instructions +
            "\nObey the request exactly:\n"
            "- What it names is the SUBJECT of the picture; find it in "
            "the transcript and describe THAT.\n"
            "- If it names ONE character, that character is alone in "
            "the picture and nobody else appears"
            + (" - cast tag `solo`" if tags else "") + ".\n"
            "- If it names an ACTION, that action must be in the "
            "description; a still portrait instead is the wrong "
            "picture.\n"
            "- It may name a detail rather than a whole room; then "
            "illustrate the detail.\n"
            # A request for a character that comes back as a wide shot
            # of the room they are in is a picture of the wrong subject
            # - and at 160x200 the character is then four pixels tall.
            + ("- This is a picture OF that character, so the framing "
               "tag is one of: portrait, upper body, cowboy shot, full "
               "body. Not wide shot, not scenery.\n"
               if tags and focus else
               ("- This is a picture OF that character: frame it on "
                "them, close enough to read their face, not as a wide "
                "view of the room.\n" if focus else "")) +
            "This request outranks everything below it.")
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
    #
    # Narrowed when the request names characters, because a ledger entry
    # is an invitation to draw someone: telling the model what Vess
    # looks like while asking for a portrait of Bruc is most of why she
    # kept turning up in it.
    if canon:
        block = canon_block(canon, only=focus or None)
        if block:
            parts.append(block)

    # Same reason: the chargen sheet is the PLAYER's identity, so a
    # request naming somebody else must not carry it.
    if character and (not focus or canon_player_name(canon) in focus):
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

    # The same cast policy either way; only the last line differs,
    # because in tag mode the roster IS the cast tag.
    cast = [
        "Cast rules for the illustration:",
        "- Show the current location as described in the transcript.",
        "- The player character may appear ONLY if the request asks for "
        "them or the current scene is about them; if shown, they must "
        "match the identity and game state above.",
        # This bullet is why a request for one character used to come
        # back with the whole party: it is the LAST thing said about the
        # cast before the transcript, so it beat the request block far
        # above it. When the player has named a subject, the request
        # owns the cast and this rule must not re-open it.
        (("- The picture contains exactly "
          + ", ".join(sorted(focus)) + " and nobody else.")
         if focus else
         ("- The player's request above has already settled who is in "
          "the picture. The game state does not add anyone to it."
          if instructions else
          "- Companions listed in the game state may appear.")),
        "- Include NO other people, creatures, or monsters unless the "
        "current scene in the transcript explicitly puts them there.",
        "- Include props and scenery named in the scene or the location "
        "note (keys, doors, altars).",
    ]
    if tags:
        cast.append(
            "- The cast tag must match that count exactly: `solo` when "
            "one character is in the shot, `duo` when two, "
            "`no humans, scenery` when the shot is a place with nobody "
            "in it. Getting this tag wrong is how an extra figure "
            "appears in the picture.")
    else:
        cast.append(
            "- End your description with an explicit list of who is "
            "present, " + _ROSTER_EXAMPLE + ".")
    parts.append("\n".join(cast))

    parts.append("Transcript:\n" + convo)

    return "\n\n".join(parts)


# --- keeping the prose roster -----------------------------------------
#
# Tag mode ends on the cast tag and tidy_tags looks after it. Prose ends
# on the roster line the rule above asks for - the "who is present" list
# that is the only thing standing between an empty crypt and a monster -
# and that line is LAST, which is exactly where a length cap bites. At
# 400 chars it was cut off every time the description ran long, mid-word
# ("...her short golden f"), taking the cast list with it.

# Two sentences of scene plus a roster fit inside this; more than this
# is prose the image model dilutes itself with anyway. Deliberately the
# same number as TAG_SCENE_LIMIT, for a different reason.
PROSE_SCENE_LIMIT = 700


def _cut_words(text, limit):
    """`text` capped at `limit`, on a word boundary when there is one to
    find in the back half."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(' ')
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip().rstrip(',;:-').rstrip()


def _is_roster(line):
    """Does this last line look like the who-is-present list?"""
    low = line.strip().lower()
    return bool(low) and (low.startswith('-')
                          or 'present' in low
                          or 'figure' in low)


def trim_scene(text, limit=PROSE_SCENE_LIMIT):
    """A composed prose scene, capped WITHOUT losing the roster.

    Over budget, the description gives up the tail of its PROSE, not its
    cast list; the roster is dropped only when there is no room for it
    at all."""
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    body, sep, last = text.rpartition('\n')
    roster = last.strip()
    if sep and _is_roster(roster) and len(roster) <= limit // 2:
        return _cut_words(body, limit - len(roster) - 1) + "\n" + roster
    return _cut_words(text, limit)


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
    # A reply that opened a JSON object and never closed it - cut short
    # by a length cap, or by the model simply stopping. Salvage the one
    # field that matters rather than storing the scaffolding itself as
    # the player's description, which is what the prose fallback below
    # would otherwise do (and did: a live conversation's ledger began
    # '{\n  "player": "').
    m = re.search(r'"player"\s*:\s*"(.+?)(?:"\s*[,}]|$)', text, re.DOTALL)
    if m:
        salvaged = m.group(1).replace('\\"', '"').replace('\\n', ' ').strip()
        if salvaged:
            return normalize_canon({'player': salvaged}, prev)
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


def canon_player_name(canon):
    """The player's name as the ledger records it ("Vess Kolvar: small
    anthro kobold, ..." -> "Vess Kolvar"), or ''. The build question
    asks for that shape but does not enforce it, so a player entry
    written as bare prose yields no name and simply never matches."""
    player = (canon or {}).get('player') or ''
    head = player.split(':', 1)[0].strip() if ':' in player else ''
    # A whole sentence before the colon is prose, not a name.
    return head if 0 < len(head) <= 40 and len(head.split()) <= 4 else ''


def focus_names(instructions, canon):
    """The canon characters a /pic request actually names.

    Empty when the request names none of them (or there is no request),
    which means "no narrowing" - a bare /pic still gets the whole cast
    to choose from. Matching is per word so "/pic vess" finds "Vess
    Kolvar", with a word boundary so "/pic the altar" does not find an
    NPC called "Al".
    """
    text = (instructions or '').lower()
    if not text or not canon:
        return set()
    hits = set()
    names = [canon_player_name(canon)] + list(canon.get('npcs') or {})
    for name in [n for n in names if n]:
        for word in name.replace(',', ' ').split():
            if len(word) >= 3 and re.search(
                    r'\b' + re.escape(word.lower()) + r'\b', text):
                hits.add(name)
                break
    return hits


def canon_block(canon, only=None):
    """The ledger as the injection block compose_question carries.

    `only` narrows it to a set of character names - what a request
    naming one character needs. Everything the block still lists is a
    character the model is being invited to draw, so the reliable way
    to keep the rest of the party out of a portrait is not to mention
    them here. Places are never narrowed: the room is where the shot
    happens whoever is standing in it.
    """
    if not canon:
        return ''
    lines = ["AUTHORITATIVE VISUAL CANON - these descriptions are "
             "settled. Wherever these people or places appear, repeat "
             "them precisely; they outrank the transcript's phrasing:"]
    if canon.get('player') and (
            only is None or canon_player_name(canon) in only):
        lines.append("Player character: " + canon['player'])
    for name, desc in (canon.get('npcs') or {}).items():
        if only is None or name in only:
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
