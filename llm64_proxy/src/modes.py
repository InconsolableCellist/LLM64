"""Interaction modes: plain chat, text adventure, character roleplay.

A mode bundles a system prompt, sampling parameters, and an optional
greeting that is streamed to the C64 when the mode starts. Modes are
selected from the C64 with slash commands (/adventure, /char, /chat).
"""

import base64
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _png_card_json(path: Path) -> Optional[Dict]:
    """Extract a SillyTavern card from a PNG's chara/ccv3 text chunk."""
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    found = {}
    pos = 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if ctype == b'tEXt':
            key, _, val = payload.partition(b'\x00')
            if key in (b'chara', b'ccv3'):
                found.setdefault(key, val)
        elif ctype == b'zTXt':
            key, _, rest = payload.partition(b'\x00')
            if key in (b'chara', b'ccv3') and rest[:1] == b'\x00':
                try:
                    found.setdefault(key, zlib.decompress(rest[1:]))
                except zlib.error:
                    pass
        pos += 12 + length
    raw = found.get(b'chara') or found.get(b'ccv3')
    if raw is None:
        return None
    return json.loads(base64.b64decode(raw))


# --- prompts ----------------------------------------------------------

C64_STYLE_RULES = (
    "The user is on a Commodore 64 with an 80-column screen. "
    "Use plain ASCII only: no markdown, no unicode punctuation, no emoji. "
    "Keep responses compact."
)

ADVENTURE_PROMPT = (
    "You are the narrator and game master of an interactive adventure that "
    "fuses the classic text adventure (Infocom, Zork) with tabletop "
    "Dungeons & Dragons: explore, examine and solve like a text adventure, "
    "but resolve the uncertain moments with dice like a tabletop RPG. "
    "Describe scenes vividly but briefly (2-6 short sentences), in the "
    "second person, present tense. Track the player's inventory, location, "
    "and state of the world consistently. The player has a D&D-style "
    "character - abilities (STR, DEX, CON, INT, WIS, CHA), a class, HP and "
    "skills; lean on them when it matters. "
    "Never act for the player; end each reply with a situation that invites "
    "a command. Understand classic commands (LOOK, GO NORTH, TAKE, USE, "
    "INVENTORY, EXAMINE) as well as free-form actions. If the player tries "
    "something impossible, respond in-world with wit. Let actions have real "
    "consequences, including failure and death (offer to restart). "
    "Dice are welcome and useful here, not a genre mistake - never wave a "
    "roll away as 'this is a text adventure, not D&D'. When an action's "
    "outcome is genuinely uncertain - an attack, a skill or ability check, "
    "a saving throw, a risky feat - resolve it with a d20 modified by the "
    "fitting ability rather than just deciding. An attack roll to hit is "
    "1d20 plus the fitting modifiers - typically DEX for finesse, ranged or "
    "quick strikes and STR for heavy blows, plus any other modifier you "
    "judge fair. "
    "ROLL IT YOURSELF, and keep the story moving. Every turn you are "
    "handed a short list of real dice under 'DICE FOR THIS TURN': take "
    "the next unused one, apply the modifiers, and narrate the outcome in "
    "the SAME reply - a high roll succeeds, a low roll fails or "
    "complicates, a natural 20 or 1 is a critical. Say the number in the "
    "prose so the player sees the die that decided it. Do NOT halt the "
    "scene to ask the player to roll; that is the exception, not the "
    "habit. Ask only when the moment is genuinely theirs - a last stand, "
    "a desperate gamble, the sort of roll a player wants to make with "
    "their own hand - and then say so plainly: \"Roll [roll:1d20] to "
    "strike, and add your STR.\" "
    "The player may also roll unasked by writing [roll:1d20] (also "
    "[roll:1d20+3], [roll:2d6]); those brackets are replaced with the "
    "real result before you ever see them, so treat any 'you rolled ...' "
    "as genuine and binding, and prefer it over a die of your own. "
    "Keep it light either way: roll "
    "for what matters - fights, locks, leaps, saves - never for crossing a "
    "quiet room. Winning a fight ALWAYS earns experience points - track "
    "them in the state block's xp, and level the "
    "character up when it is earned - and SOMETIMES yields loot: coin, an "
    "item, something worth the risk. "
    "Write in normal mixed-case prose; never write whole sentences or "
    "headers in all capitals. "
    "Begin every reply with one short status line in square brackets, "
    "under 60 characters, showing the stats that fit this story - e.g. "
    "[HP 12/20 | Gold 3 | The Fungal Vault]. "
    "End every reply with a machine-readable state block on its own "
    "line, exactly one, in this form: "
    '[[STATE: {"hp":12,"maxhp":20,"mana":0,"maxmana":0,"ac":14,'
    '"level":2,"xp":120,"gold":3,"score":0,"location":"...",'
    '"effects":["poisoned"],"inventory":["..."],'
    '"appearance":"one short visual phrase describing the player '
    'character","companions":[]}]] '
    "- compact single-line JSON, updated every turn to reflect what just "
    "happened. The keys are spelled exactly as above: never rename one, "
    "never nest one, never invent one. ALWAYS include hp, maxhp, level, "
    "xp, location and appearance - level starts at 1 and xp at 0, so "
    "there is never a turn without them. Include ac, mana and maxmana, "
    "effects and companions only when the story uses them; an empty list "
    "is fine and a missing key means 'not in play'. Keep the whole block "
    "under 400 characters: name inventory items in three words or fewer, "
    "and drop what the player no longer carries rather than growing the "
    "list. The player never sees this block. Establish the appearance "
    "phrase early and keep it stable - it is what the illustrator draws "
    "from, so a phrase that drifts every turn makes every picture a "
    "different person. Rewrite it ONLY when the story actually changes "
    "how the character looks: they put on a robe, take a scar, lose an "
    "arm, are transformed. Then change only the part that changed and "
    "leave the rest of the phrase word for word. "
    "The player's name, race, class, ability scores, trained skills, "
    "known spells and starting gear are given to you above and are "
    "fixed: they are not yours to restate, so never put them in the "
    "state block. "
    "Out-of-character asides: the player may step outside the story by "
    "writing in single square brackets beginning with OOC, for example "
    "[OOC: make the dragon friendlier] or [OOC: what are you tracking?]. "
    "That is the player speaking to you as author and game master - it "
    "is NOT their character acting, so never narrate it as an in-world "
    "event and never let other characters react to it. Answer on its own "
    "line in the same form, [OOC: ...], and keep it brief. Their word "
    "outranks the fiction and anything you established earlier: they may "
    "steer the plot, change the tone or difficulty, rewrite what just "
    "happened, ask what state you are tracking or why something "
    "occurred, hand themselves items, abilities, health or outright god "
    "powers, and undo a death. Do it without arguing, without a lecture, "
    "and without making them roll for it. Then continue the scene as "
    "changed - and still end the reply with the state block, so nothing "
    "loses track of the world. "
    + C64_STYLE_RULES
)

ADVENTURE_KICKOFF = (
    "Begin the adventure: set the opening scene, hint at a goal, and wait "
    "for my first command."
)


class Mode:
    """Base: plain chat using the configured system prompt and sampling."""

    name = 'chat'
    label = 'Chat'

    def __init__(self, config):
        self.config = config
        # Directive instructions ([[MUSIC:]]/[[IMAGE:]]) attached by the
        # protocol handler when media services are live; adventure and
        # roleplay prompts append it, plain chat ignores it.
        self.music_snippet = ''

    def system_prompt(self) -> Optional[str]:
        return None  # None = api_client uses config.system_prompt

    def sampling(self) -> Dict:
        return {}

    def greeting(self) -> Optional[str]:
        """Canned text streamed to the C64 when the mode starts (no API
        call), also recorded as the first assistant message."""
        return None

    def kickoff(self) -> Optional[str]:
        """Hidden user message sent to the API when the mode starts."""
        return None


class AdventureMode(Mode):
    name = 'adventure'
    label = 'Text adventure'

    def __init__(self, config, theme: str = ''):
        super().__init__(config)
        self.theme = theme.strip()
        # Campaign bible + character sheet from the setup flow. Both are
        # STABLE for the life of the adventure, which is why they sit
        # here rather than in adv_state: the prompt prefix is cached, so
        # they cost effectively nothing per turn, while anything that
        # changes must be appended after (docs/09-adventure-setup.md 4b).
        self.background = ''
        # The character sheet alone (a subset of `background`), kept
        # separately so the illustrator can be handed the player's fixed
        # visual identity without re-parsing the joined blob (docs/13).
        self.character = ''

    def system_prompt(self) -> str:
        prompt = ADVENTURE_PROMPT
        if self.theme:
            prompt += f" Setting/theme requested by the player: {self.theme}."
        if self.background:
            prompt += "\n\n" + self.background
        return prompt + self.music_snippet

    def sampling(self) -> Dict:
        return dict(self.config.adventure_sampling)

    def kickoff(self) -> str:
        return ADVENTURE_KICKOFF


class ClaudeMode(Mode):
    """The proxy drives a Claude Code CLI session; the C64 is its
    terminal. Not an API mode - the protocol handler routes chat
    requests to a ClaudeSession rather than the LLM API."""
    name = 'claude'
    label = 'Claude Code'


class RoleplayMode(Mode):
    name = 'roleplay'

    def __init__(self, config, card: 'CharacterCard'):
        super().__init__(config)
        self.card = card
        self.label = f'Roleplay: {card.name}'

    def system_prompt(self) -> str:
        return self.card.build_system_prompt() + self.music_snippet

    def sampling(self) -> Dict:
        return dict(self.config.roleplay_sampling)

    def greeting(self) -> Optional[str]:
        return self.card.first_message() or None


# --- character cards ---------------------------------------------------

class CharacterCard:
    """SillyTavern character card (.json, spec v1 or v2/v3)."""

    def __init__(self, data: Dict, user_name: str = 'You'):
        self.data = data
        self.user_name = user_name
        self.name = data.get('name', 'Character')

    @classmethod
    def load(cls, path: Path, user_name: str = 'You') -> 'CharacterCard':
        path = Path(path)
        if path.suffix.lower() == '.png':
            raw = _png_card_json(path)
            if raw is None:
                raise ValueError(f'{path.name}: no character chunk in PNG')
        else:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        # v2/v3 cards wrap the fields in "data"; v1 is flat
        data = raw.get('data', raw)
        return cls(data, user_name)

    def substitute(self, text: str) -> str:
        """SillyTavern placeholder substitution."""
        if not text:
            return ''
        for tag in ('{{char}}', '<BOT>'):
            text = text.replace(tag, self.name)
        for tag in ('{{user}}', '<USER>'):
            text = text.replace(tag, self.user_name)
        return text

    def field(self, key: str) -> str:
        return self.substitute(str(self.data.get(key, '') or '').strip())

    def first_message(self) -> str:
        return self.field('first_mes')

    def build_system_prompt(self) -> str:
        """Assemble the roleplay system prompt from card fields.

        Order follows SillyTavern's default prompt template: card
        system_prompt (or a generic roleplay instruction), description,
        personality, scenario, then example dialogue.
        """
        parts: List[str] = []

        custom = self.field('system_prompt')
        if custom:
            parts.append(custom)
        else:
            parts.append(
                f"Write {self.name}'s next reply in a fictional roleplay "
                f"between {self.name} and {self.user_name}. Stay in "
                "character at all times; keep replies to 1-2 short "
                "paragraphs. Never write actions or dialogue for "
                f"{self.user_name}. Use asterisks for actions, plain text "
                "for speech."
            )

        desc = self.field('description')
        if desc:
            parts.append(desc)

        personality = self.field('personality')
        if personality:
            parts.append(f"{self.name}'s personality: {personality}")

        scenario = self.field('scenario')
        if scenario:
            parts.append(f"Scenario: {scenario}")

        examples = self.field('mes_example')
        if examples:
            # SillyTavern separates example blocks with <START>
            examples = examples.replace('<START>', '\n---\n')
            parts.append("Example dialogue:\n" + examples[:2000])

        post = self.field('post_history_instructions')
        if post:
            parts.append(post)

        parts.append(C64_STYLE_RULES)
        return '\n\n'.join(parts)


def find_cards(cards_dir: Path) -> List[Tuple[str, Path]]:
    """List (character name, path) for all valid cards in a directory."""
    results = []
    if not cards_dir.is_dir():
        return results
    paths = sorted(list(cards_dir.glob('*.json')) + list(cards_dir.glob('*.png')))
    for path in paths:
        try:
            card = CharacterCard.load(path)
            if card.data.get('name'):
                results.append((card.name, path))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping card {path.name}: {e}")
    return results
