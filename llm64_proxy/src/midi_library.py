"""MIDI music library: the same questions MusicLibrary answers, for a
machine with a MIDI Mapper instead of a SID chip.

Deliberately DUCK-TYPED against MusicLibrary rather than sharing a base
class with it - a spike decision made while the wire side was being
written in parallel. The wire has since landed and protocol.py now
serves CAP_MIDI clients from THIS class (every egress decision routes
through _music_lib()). The two classes are ~80% identical and should
become one base plus two payload methods - see the note at the bottom.

What actually differs between the two, and it is only these:

  payload()   a SID is a relocated 6502 memory image with a PSID header
              to strip. A MIDI file goes to MCI verbatim - the bytes on
              disk are the bytes on the wire.
  weighting   MusicLibrary weights by `rank`, the C64 scene's published
              opinion. Nothing publishes an opinion about MIDI game-music
              sequences, so `quality` (midi_makedb.py) stands in.

Everything else - the mood vocabulary, the recent-repeat window, the
rate limit, the iconic damping, favorites - is a property of how the
NARRATOR uses music, not of the audio format, and so is identical by
construction.
"""

import json
import random
import time
from collections import deque
from pathlib import Path

# Never repeat any of the last N tunes.
RECENT_N = 3

# Selection floor, mirroring sid_ranking.weight: a low-quality tune is
# damped, never excluded. A corpus is mostly unremarkable, and
# unremarkable is not the same as bad.
QUALITY_FLOOR = 0.25


class MidiLibrary:
    def __init__(self, db_path: Path, min_interval_s: float = 90.0):
        self.db_path = Path(db_path)
        self.min_interval_s = min_interval_s
        self.tunes = []
        self.moods = []
        self._recent = deque(maxlen=RECENT_N)
        self._last_change = 0.0
        self.tune_started = None
        try:
            db = json.loads(self.db_path.read_text())
            self.tunes = db["tunes"]
            self.moods = sorted(set(db["moods"])
                                | {m for t in self.tunes for m in t["moods"]})
        except (OSError, KeyError, json.JSONDecodeError):
            pass  # library optional: the proxy runs fine without music
        self.favorites = self._load_favorites()

    @property
    def available(self) -> bool:
        return bool(self.tunes)

    # --- favorites ----------------------------------------------------
    # Kept beside the database, and separate from the SID library's own
    # favorites file: they are different tunes, and a listener's opinion
    # of one says nothing about the other.

    def _fav_path(self) -> Path:
        return self.db_path.parent / "midi_favorites.json"

    def _load_favorites(self) -> set:
        try:
            return set(json.loads(self._fav_path().read_text()))
        except (OSError, ValueError):
            return set()

    def is_favorite(self, tune_id) -> bool:
        return tune_id in self.favorites

    def toggle_favorite(self, tune_id) -> bool:
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
        """Instruction block appended to the adventure system prompt.

        Word for word what MusicLibrary emits. It has to be: the prompt
        is built upstream of the profile, so both clients' narrators are
        told the same thing, and the mood vocabulary is shared by
        construction (midi_mood.py imports it from sid_mood.py).
        """
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
        return time.monotonic() - self._last_change < self.min_interval_s

    def mark_changed(self):
        self._last_change = time.monotonic()

    def stale(self, after_s: float = 300.0) -> bool:
        return (self.tune_started is not None
                and time.monotonic() - self.tune_started > after_s)

    def find(self, tune_id):
        for t in self.tunes:
            if t["id"] == tune_id:
                return t
        return None

    def pick(self, mood: str):
        """Best tune for a mood, avoiding recent repeats. None if no fit.

        Filter order is MusicLibrary's, for a reason worth stating: the
        two libraries have to FEEL the same. If the C64 avoided famous
        themes and the PC did not, one adventure would be atmospheric and
        the other a medley, in the same shared world.
        """
        mood = mood.lower()
        pool = [t for t in self.tunes if t["moods"].get(mood, 0) > 0]
        # Instantly recognizable themes feel cheesy in-game
        pool = [t for t in pool if (t.get("iconic") or 0) < 0.85]
        fresh = [t for t in pool if t["id"] not in self._recent]
        if fresh:
            pool = fresh
        confident = [t for t in pool if (t.get("confidence") or 0) >= 0.5]
        if confident:
            pool = confident
        if not pool:
            return None
        weights = [t["moods"][mood]
                   * (1.0 - 0.5 * (t.get("arcadey") or 0))
                   * (1.0 - 0.6 * (t.get("iconic") or 0))
                   * self.quality_weight(t.get("quality"))
                   for t in pool]
        tune = random.choices(pool, weights=weights, k=1)[0]
        self._recent.append(tune["id"])
        return tune

    @staticmethod
    def quality_weight(q) -> float:
        """Percentile -> selection weight, squared like sid_ranking's so
        the top of the library is favoured without silencing the rest."""
        if q is None:
            return 1.0
        return QUALITY_FLOOR + (1.0 - QUALITY_FLOOR) * (q * q)

    def payload(self, tune) -> bytes:
        """The .MID file, verbatim.

        No transformation at all, which is the whole point: the client
        writes these bytes to a temp file and hands the name to MCI. The
        SID side had to strip a PSID header and relocate 6502 code to a
        fixed address; here the file IS the payload.
        """
        p = Path(tune["file"])
        if not p.is_absolute():
            p = self.db_path.parent / p
        return p.read_bytes()


# Follow-up: lift everything above except payload() and the weighting
# hook into a shared base in music.py, and let MusicLibrary and
# MidiLibrary be the two short subclasses they deserve to be. The race
# that deferred this is over - protocol.py's MIDI_* work has landed and
# imports this class - so it is now an ordinary refactor.
