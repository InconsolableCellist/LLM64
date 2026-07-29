#!/usr/bin/env python3
"""One world, two machines: what each client hears from the same scene.

docs/16 section 13 calls this "the pleasant side effect" - a C64 and a
1993 PC in the same adventure, same map, same moods, one hearing a SID
chip and the other a General MIDI score. This is that claim, checked
against the two real libraries on this machine.

It writes NOTHING and edits NOTHING. profiles.py and protocol.py are
being changed by somebody else right now, so this reads the profile
table, resolves moods through both libraries, and reports. Where the
win16 profile still says music_fmt=None (because the wire side is
unfinished), it shows what that row WOULD resolve to with 'midi', using
dataclasses.replace on a local copy.

The thing worth watching in the output is the mood column: it is the
same word down both sides. Everything upstream of the egress edge - the
narrator, the adventure, the map - emitted one directive, and two
different machines each turned it into something they can actually play.
"""

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.midi_library import MidiLibrary                       # noqa: E402
from src.music import MusicLibrary                             # noqa: E402

# The profile table arrived with the multi-client work (docs/16 section
# 7) and is not on every branch this tool can usefully run from. What it
# demonstrates - one mood, two libraries, two machines - does not depend
# on it, so a missing table degrades to the two rows it would have held
# rather than to an ImportError on line one.
try:
    from src import profiles                                   # noqa: E402
except ImportError:
    profiles = None


@dataclasses.dataclass(frozen=True)
class _StandIn:
    """What profiles.py's rows say that matters here."""
    name: str
    music_fmt: str


def client_profiles():
    """(c64, win16, is_real_table)."""
    if profiles is not None:
        return profiles.C64, profiles.WIN16, True
    return _StandIn('c64', 'sid'), _StandIn('win16', None), False

# A scene sequence a narrator might plausibly emit over one session.
SCRIPT = [
    ("You push open the chapel door.",        "eerie"),
    ("Something moves behind the altar.",     "tense"),
    ("It lunges.",                            "combat"),
    ("The thing collapses into ash.",         "triumphant"),
    ("Beyond, a garden in full sun.",         "serene"),
    ("A road, and no idea where it goes.",    "adventurous"),
    ("You remember who you left behind.",     "melancholy"),
]


def line(t, fmt):
    if not t:
        return "  -- nothing tagged --"
    if fmt == "sid":
        return f"  {t['title'][:34]:<35} {t.get('author', '')[:22]}"
    game = f"({t.get('game', '')})" if t.get("game") else ""
    return f"  {t['title'][:34]:<35} {game[:22]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    data = Path(__file__).resolve().parents[1] / "data"
    ap.add_argument("--sids", type=Path, default=data / "sids" / "moods.json")
    ap.add_argument("--midi", type=Path, default=data / "midi" / "midi.json")
    args = ap.parse_args()

    sid_lib = MusicLibrary(args.sids)
    midi_lib = MidiLibrary(args.midi)

    print("libraries")
    print(f"  SID  {len(sid_lib.tunes):>6} tunes  {args.sids}")
    print(f"  MIDI {len(midi_lib.tunes):>6} tunes  {args.midi}")
    if not (sid_lib.available and midi_lib.available):
        print("\nboth libraries are needed for this check", file=sys.stderr)
        return 2

    # --- the shared vocabulary ----------------------------------------
    print("\nmood vocabulary")
    s, m = set(sid_lib.moods), set(midi_lib.moods)
    print(f"  SID  {len(s)}: {', '.join(sorted(s))}")
    print(f"  MIDI {len(m)}: {', '.join(sorted(m))}")
    if s == m:
        print("  -> identical. One system prompt serves both narrators.")
    else:
        print(f"  -> SID only:  {', '.join(sorted(s - m)) or '(none)'}")
        print(f"  -> MIDI only: {', '.join(sorted(m - s)) or '(none)'}")
        print("  -> the prompt must offer the INTERSECTION, or one client "
              "is told about moods it cannot play")

    same_prompt = sid_lib.prompt_snippet() == midi_lib.prompt_snippet()
    print(f"\nprompt_snippet() identical: {same_prompt}")

    # --- the profile decides ------------------------------------------
    c64, win16, real_table = client_profiles()
    print("\nprofiles as they stand" if real_table else
          "\nprofiles (src/profiles.py is not on this branch - "
          "standing in for it)")
    print(f"  {c64.name:<7} music_fmt={c64.music_fmt!r}")
    print(f"  {win16.name:<7} music_fmt={win16.music_fmt!r}"
          + ("   <- wire side unfinished; simulating 'midi' below"
             if win16.music_fmt != "midi" else ""))
    if win16.music_fmt != "midi":
        win16 = dataclasses.replace(win16, music_fmt="midi")

    libs = {"sid": sid_lib, "midi": midi_lib}

    def resolve(profile, mood):
        lib = libs.get(profile.music_fmt)
        return (lib.pick(mood) if lib else None), profile.music_fmt

    # --- the same adventure, twice ------------------------------------
    print("\n" + "=" * 74)
    print("one narrator, one [[MUSIC:]] directive per scene, two machines")
    print("=" * 74)
    bytes_c64 = bytes_win = 0
    for text, mood in SCRIPT:
        print(f"\n\"{text}\"")
        print(f"  [[MUSIC: {mood}]]")
        for profile in (c64, win16):
            tune, fmt = resolve(profile, mood)
            tag = f"{profile.name}/{fmt}"
            print(f"    {tag:<12}{line(tune, fmt).strip()}")
            if tune:
                n = len(libs[fmt].payload(tune))
                print(f"    {'':<12}{n:,} bytes"
                      + (f", {tune['secs'] / 60:.1f} min"
                         if tune.get("secs") else ""))
                if fmt == "sid":
                    bytes_c64 += n
                else:
                    bytes_win += n

    print("\n" + "-" * 74)
    print(f"session transfer: C64 {bytes_c64:,} B over a 2400-baud modem, "
          f"win16 {bytes_win:,} B over a socket")
    if bytes_c64:
        print(f"MIDI is {bytes_win / bytes_c64:.1f}x the bytes, on a link "
              "that is thousands of times faster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
