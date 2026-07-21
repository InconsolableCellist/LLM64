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
    # Canonical form. DOTALL so a STATE block's JSON may wrap, and ']]'
    # as the terminator so the JSON's own ']' cannot close it early.
    r"\[\[\s*(MUSIC|IMAGE|STATE)\s*:\s*(.*?)\s*\]\]"
    r"|"
    # Single-bracket fallback. Adventure replies open with a status line
    # that is itself single-bracketed - "[HP 15/20 | Gold 0 | ...]" - so
    # models copy that shape and emit "[MUSIC: eerie]". Those used to
    # leak onto the screen as text and silently do nothing.
    # MUSIC/IMAGE only, and the value may not span ']' or a newline:
    # STATE's JSON contains ']', so it has no safe single-bracket form.
    # The lookbehind matters mid-stream: while "[[MUSIC: calm]]" is still
    # arriving it briefly reads as "[[MUSIC: calm]", and without it this
    # alternative would match the inner "[MUSIC: calm]" and leave a
    # stray "[]" on the screen.
    r"(?<!\[)\[\s*(MUSIC|IMAGE)\s*:\s*([^\]\n]*?)\s*\]",
    re.IGNORECASE | re.DOTALL)

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
        self.favorites = self._load_favorites()

    @property
    def available(self) -> bool:
        return bool(self.tunes)

    # --- favorites (jukebox 'f' key) ---------------------------------
    # Kept beside the database as a plain id list. Server-side on
    # purpose: the C64 has no room to remember 10k tunes, and a
    # favorite should survive reboots and disk swaps.

    def _fav_path(self) -> Path:
        return self.db_path.parent / "favorites.json"

    def _load_favorites(self) -> set:
        try:
            return set(json.loads(self._fav_path().read_text()))
        except (OSError, ValueError):
            return set()

    def is_favorite(self, tune_id) -> bool:
        return tune_id in self.favorites

    def toggle_favorite(self, tune_id) -> bool:
        """Flip and persist. Returns the new state."""
        if tune_id in self.favorites:
            self.favorites.discard(tune_id)
            now = False
        else:
            self.favorites.add(tune_id)
            now = True
        try:
            self._fav_path().write_text(json.dumps(sorted(self.favorites)))
        except OSError:
            pass  # a lost favorite must never break playback
        return now

    def prompt_snippet(self) -> str:
        """Instruction block appended to the adventure system prompt."""
        return (
            "\nBackground music: you control the soundtrack. To change it, "
            "output [[MUSIC: mood]] on its own at the START of a reply - "
            "TWO square brackets each side, not one (the status line's "
            "single brackets are a different thing) - "
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
        pool = [t for t in self.tunes if t["moods"].get(mood, 0) > 0]
        # Instantly recognizable themes (Pac-Man...) feel cheesy in-game
        pool = [t for t in pool if (t.get("iconic") or 0) < 0.85]
        # Avoid recent repeats unless that would empty the bucket
        # (small buckets / tiny demo libraries)
        fresh = [t for t in pool if t["id"] not in self._recent]
        if fresh:
            pool = fresh
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
    """Strips [[MUSIC: x]] / [[IMAGE: desc]] / [[STATE: json]] from
    streamed text without leaking partials.

    feed() returns text safe to forward; anything that could be the start
    of a directive is held back until it resolves either way. flush()
    returns whatever is still held at end of stream. Parsed directives
    accumulate in .moods (music), .images (image descriptions) and
    .states (adventure game-state JSON, newest last).
    """

    # Image descriptions can be a sentence and a state block carries a
    # whole JSON object; hold generously before concluding a '[[' wasn't
    # a directive after all
    MAX_HOLD = 600

    def __init__(self):
        self.held = ""
        self.moods = []
        self.images = []
        self.states = []

    # Openers worth holding a partial tail for (see _could_become_directive)
    _PREFIXES = ("[[MUSIC:", "[[IMAGE:", "[[STATE:", "[MUSIC:", "[IMAGE:")

    def _extract(self, text: str) -> str:
        def grab(m):
            # Groups 1/2 are the canonical form, 3/4 the single-bracket
            # fallback; exactly one alternative participates per match.
            kind = (m.group(1) or m.group(3)).upper()
            value = m.group(2) if m.group(1) else m.group(4)
            if kind == "MUSIC":
                self.moods.append(value.lower())
            elif kind == "STATE":
                self.states.append(value)
            else:
                self.images.append(value)
            return ""
        return DIRECTIVE_RE.sub(grab, text)

    @classmethod
    def _could_become_directive(cls, tail: str) -> bool:
        t = tail.upper().replace(" ", "")
        for p in cls._PREFIXES:
            if p.startswith(t) if len(t) <= len(p) else t.startswith(p):
                return True
        return False

    @staticmethod
    def _closed(tail: str) -> bool:
        """A '[[' opener is only finished by ']]'; a single '[' by ']'."""
        return "]]" in tail if tail.startswith("[[") else "]" in tail

    def feed(self, chunk: str) -> str:
        text = self._extract(self.held + chunk)
        self.held = ""
        # Walk '[' positions backwards and hold from the EARLIEST one that
        # still looks unfinished. Anchoring on the last '[' would break
        # [[STATE: ...]], whose JSON contains its own '[' - the outer
        # opener is the one that has to be held back.
        hold_at = -1
        i = text.rfind("[")
        while i != -1 and len(text) - i <= self.MAX_HOLD:
            tail = text[i:]
            if self._could_become_directive(tail) and not self._closed(tail):
                hold_at = i
            i = text.rfind("[", 0, i)
        if hold_at != -1:
            self.held = text[hold_at:]
            return text[:hold_at]
        return text

    def flush(self) -> str:
        text = self._extract(self.held)
        self.held = ""
        return text
