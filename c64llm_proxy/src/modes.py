"""Interaction modes: plain chat, text adventure, character roleplay.

A mode bundles a system prompt, sampling parameters, and an optional
greeting that is streamed to the C64 when the mode starts. Modes are
selected from the C64 with slash commands (/adventure, /char, /chat).
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --- prompts ----------------------------------------------------------

C64_STYLE_RULES = (
    "The user is on a Commodore 64 with a 40-column screen. "
    "Use plain ASCII only: no markdown, no unicode punctuation, no emoji. "
    "Keep responses compact."
)

ADVENTURE_PROMPT = (
    "You are the narrator and game master of an interactive text adventure "
    "in the classic Infocom style. Describe scenes vividly but briefly "
    "(2-6 short sentences), in the second person, present tense. Track the "
    "player's inventory, location, and state of the world consistently. "
    "Never act for the player; end each reply with a situation that invites "
    "a command. Understand classic commands (LOOK, GO NORTH, TAKE, USE, "
    "INVENTORY, EXAMINE) as well as free-form actions. If the player tries "
    "something impossible, respond in-world with wit. Let actions have real "
    "consequences, including failure and death (offer to restart). "
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

    def system_prompt(self) -> str:
        prompt = ADVENTURE_PROMPT
        if self.theme:
            prompt += f" Setting/theme requested by the player: {self.theme}."
        return prompt

    def sampling(self) -> Dict:
        return dict(self.config.adventure_sampling)

    def kickoff(self) -> str:
        return ADVENTURE_KICKOFF


class RoleplayMode(Mode):
    name = 'roleplay'

    def __init__(self, config, card: 'CharacterCard'):
        super().__init__(config)
        self.card = card
        self.label = f'Roleplay: {card.name}'

    def system_prompt(self) -> str:
        return self.card.build_system_prompt()

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
    for path in sorted(cards_dir.glob('*.json')):
        try:
            card = CharacterCard.load(path)
            if card.data.get('name'):
                results.append((card.name, path))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping card {path.name}: {e}")
    return results
