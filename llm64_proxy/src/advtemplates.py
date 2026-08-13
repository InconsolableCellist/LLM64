"""Saved adventure worlds.

docs/09-adventure-setup.md §5. The moment an adventure begins, what the
player built - the answers, the character sheet and the campaign bible -
is written to data/adventures/. A world you liked can then be replayed
with a different character, and an expensive prep pass is never paid for
twice.

Server-side for the same reason favorites are: the C64 cannot hold
them, and they should outlive a disk swap.
"""

import json
import re
import time
from pathlib import Path

MAX_LISTED = 12          # the C64 shows a numbered list; keep it readable


def _slugify(text: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return (s[:40] or 'world')


def _name_from(bundle: dict, bible: str) -> str:
    """A title without spending an API call. The world answer is the
    player's own words and almost always the best name; failing that the
    bible's opening clause usually names the place outright."""
    world = (bundle.get('world') or '').strip()
    if world:
        return world[:60]
    first = (bible or '').strip().split('\n')[0]
    first = re.split(r'[.!?]', first)[0].strip()
    return (first[:60] or 'Unnamed world')


class TemplateStore:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / 'adventures'

    def _paths(self):
        try:
            return sorted(self.dir.glob('*.json'),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []

    def list(self):
        """[(name, slug)] newest first. A corrupt file is skipped rather
        than breaking the menu."""
        out = []
        for p in self._paths()[:MAX_LISTED]:
            try:
                d = json.loads(p.read_text())
                out.append((d.get('name') or p.stem, p.stem))
            except (OSError, ValueError):
                continue
        return out

    def load(self, slug):
        try:
            return json.loads((self.dir / f'{slug}.json').read_text())
        except (OSError, ValueError):
            return None

    def save(self, bundle: dict, bible: str, character: str,
             model: str = '') -> str:
        """Returns the slug, or '' if it could not be written - losing a
        template must never stop an adventure starting."""
        name = _name_from(bundle, bible)
        rec = {
            'name': name,
            'created': int(time.time()),
            'model': model,
            'bundle': bundle,
            'bible': bible,
            'character': character,
        }
        slug = f"{_slugify(name)}-{rec['created']}"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f'{slug}.json').write_text(
                json.dumps(rec, indent=1))
        except OSError:
            return ''
        return slug
