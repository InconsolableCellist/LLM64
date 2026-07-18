"""SID music library: mood-based tune selection and the stream directive.

The library is moods.json (built by tools/sid_makedb.py) plus sidreloc-
relocated .sid files whose C64 payload gets streamed into the client's
$B000 window. The LLM requests music with [[MUSIC: mood]] in its output;
MusicDirectiveFilter strips that from the stream (holding back partial
matches so nothing leaks to the C64) and the proxy acts on it after the
response completes.
"""

import json
import random
import re
import struct
import time
from collections import deque
from pathlib import Path

DIRECTIVE_RE = re.compile(
    r"\[\[\s*(MUSIC|IMAGE)\s*:\s*(.*?)\s*\]\]", re.IGNORECASE | re.DOTALL)

# Never repeat any of the last N tunes (demo library is small; keep this
# below library size or selection starves)
RECENT_N = 3


class MusicLibrary:
    def __init__(self, db_path: Path, min_interval_s: float = 90.0):
        self.db_path = Path(db_path)
        self.min_interval_s = min_interval_s
        self.tunes = []
        self.moods = []
        self._recent = deque(maxlen=RECENT_N)
        self._last_change = 0.0
        self.tune_started = None  # monotonic time the current tune began
        try:
            db = json.loads(self.db_path.read_text())
            self.tunes = db["tunes"]
            self.moods = db["moods"]
        except (OSError, KeyError, json.JSONDecodeError):
            pass  # library optional: proxy runs fine without music

    @property
    def available(self) -> bool:
        return bool(self.tunes)

    def prompt_snippet(self) -> str:
        """Instruction block appended to the adventure system prompt."""
        return (
            "\nBackground music: you control the soundtrack. To change it, "
            "output [[MUSIC: mood]] on its own at the START of a reply, "
            "picking mood from: " + ", ".join(self.moods) + ". "
            "Use it sparingly - only when the scene's emotional tone truly "
            "shifts (a new area, combat starting, victory). Most replies "
            "should have no music directive."
        )

    def rate_limited(self) -> bool:
        """Limits LLM-directed changes only; manual /music never counts."""
        return time.monotonic() - self._last_change < self.min_interval_s

    def mark_changed(self):
        self._last_change = time.monotonic()

    def stale(self, after_s: float = 300.0) -> bool:
        """True when one tune has looped long enough that the LLM should
        be nudged toward a change."""
        return (self.tune_started is not None
                and time.monotonic() - self.tune_started > after_s)

    def find(self, tune_id):
        for t in self.tunes:
            if t["id"] == tune_id:
                return t
        return None

    def pick(self, mood: str):
        """Best tune for a mood, avoiding recent repeats. None if no fit."""
        mood = mood.lower()
        pool = [t for t in self.tunes if t["id"] not in self._recent]
        pool = [t for t in pool if t["moods"].get(mood, 0) > 0]
        # Instantly recognizable themes (Pac-Man...) feel cheesy in-game
        pool = [t for t in pool if (t.get("iconic") or 0) < 0.85]
        # Low tagger confidence = mood is a guess from a bare filename;
        # skip unless that would empty the bucket (tiny demo libraries)
        confident = [t for t in pool
                     if (t.get("confidence") or 0) >= 0.5]
        if confident:
            pool = confident
        if not pool:
            return None
        # Weight by mood fit, damped by arcadey and iconic scores
        weights = [t["moods"][mood]
                   * (1.0 - 0.5 * (t.get("arcadey") or 0))
                   * (1.0 - 0.6 * (t.get("iconic") or 0))
                   for t in pool]
        tune = random.choices(pool, weights=weights, k=1)[0]
        self._recent.append(tune["id"])
        return tune

    def payload(self, tune) -> bytes:
        """C64 memory image of the relocated tune (PSID header stripped)."""
        p = Path(tune["file"])
        if not p.is_absolute():
            p = self.db_path.parent / p
        data = p.read_bytes()
        data_offset = struct.unpack(">H", data[6:8])[0]
        payload = data[data_offset:]
        if struct.unpack(">H", data[8:10])[0] == 0:  # load addr in payload
            payload = payload[2:]
        return payload


class MusicDirectiveFilter:
    """Strips [[MUSIC: x]] / [[IMAGE: desc]] from streamed text without
    leaking partials.

    feed() returns text safe to forward; anything that could be the start
    of a directive is held back until it resolves either way. flush()
    returns whatever is still held at end of stream. Parsed directives
    accumulate in .moods (music) and .images (image descriptions).
    """

    # Image descriptions can be a sentence; hold generously before
    # concluding a '[[' wasn't a directive after all
    MAX_HOLD = 300

    def __init__(self):
        self.held = ""
        self.moods = []
        self.images = []

    def _extract(self, text: str) -> str:
        def grab(m):
            if m.group(1).upper() == "MUSIC":
                self.moods.append(m.group(2).lower())
            else:
                self.images.append(m.group(2))
            return ""
        return DIRECTIVE_RE.sub(grab, text)

    @staticmethod
    def _could_become_directive(tail: str) -> bool:
        t = tail.upper().replace(" ", "")
        for p in ("[[MUSIC:", "[[IMAGE:"):
            if p.startswith(t) if len(t) <= len(p) else t.startswith(p):
                return True
        return False

    def feed(self, chunk: str) -> str:
        text = self._extract(self.held + chunk)
        self.held = ""
        idx = text.rfind("[[")
        if idx != -1:
            tail = text[idx:]
            if (len(tail) <= self.MAX_HOLD and "]]" not in tail
                    and self._could_become_directive(tail)):
                self.held = tail
                return text[:idx]
        if text.endswith("["):  # lone '[' could become '[['
            self.held = "["
            return text[:-1]
        return text

    def flush(self) -> str:
        text = self._extract(self.held)
        self.held = ""
        return text
