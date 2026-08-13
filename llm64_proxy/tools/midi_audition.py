#!/usr/bin/env python3
"""Listen to what the selector actually chooses, mood by mood.

The tags say what a tune is FOR. Nothing in them says whether the result
is pleasant, and a soundtrack that is correctly labeled and unlistenable
is a failed feature. The SID library has tools/sid_review.py for exactly
this reason.

So this does not let you pick nice examples. It calls MidiLibrary.pick()
- the real selection path, with the real weighting, the real iconic
damping and the real confidence filter - and renders whatever comes out.
If the audition sounds bad, the library IS bad; there is no gap between
what this plays and what the narrator would have played.

Output is a directory of audio clips plus an index.html that groups them
by mood with the tags visible, so the verdict can be "these three are
wrong for eerie" rather than a general feeling.

Rendering is fluidsynth against whatever SoundFont you point it at,
which is also the honest preview: under Wine the client's MIDI goes to
ALSA and lands on FluidSynth with a SoundFont, so this is roughly the
signal path a Windows-client listener gets. On real 1993 hardware it
would be OPL3 FM or an MT-32 and would sound quite different - better in
character, worse in fidelity.
"""

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.midi_library import MidiLibrary                    # noqa: E402


def render(midi: Path, sf2: Path, out: Path, seconds: int,
           skip: int, gain: float) -> bool:
    """MIDI -> a normalized audio clip. False if either tool fails."""
    wav = out.with_suffix(".raw.wav")
    r = subprocess.run(
        ["fluidsynth", "-ni", "-F", str(wav), "-r", "44100",
         "-g", str(gain), str(sf2), str(midi)],
        capture_output=True)
    if r.returncode != 0 or not wav.exists():
        return False
    # Trim, fade, and loudness-normalize so clips are comparable by ear.
    # Normalization is an AUDITION convenience: on the real client every
    # tune plays at whatever level it was sequenced at, which is the
    # analogue of the SID pipeline's loudness stage and is listed as
    # unfinished work, not as something this hides.
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(skip), "-i", str(wav),
         "-t", str(seconds),
         "-af", f"loudnorm=I=-18:TP=-1.5:LRA=11,"
                f"afade=t=out:st={max(seconds - 3, 1)}:d=3",
         "-c:a", "libopus", "-b:a", "96k", str(out)],
        capture_output=True)
    wav.unlink(missing_ok=True)
    return r.returncode == 0 and out.exists()


PAGE_CSS = """
body{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;
padding:0 1rem;background:#14161a;color:#e8e6e3}
h1{font-size:1.4rem} h2{margin-top:2.5rem;border-bottom:1px solid #333;
padding-bottom:.3rem;color:#9ecbff;text-transform:capitalize}
table{width:100%;border-collapse:collapse} td{padding:.5rem .4rem;
border-bottom:1px solid #262a30;vertical-align:middle}
.t{font-weight:600} .g{color:#9aa4b2;font-size:.85em}
.m{color:#7fd1a8;font-size:.8em;font-family:ui-monospace,monospace}
audio{height:2rem;width:15rem} .q{color:#c9a227;font-size:.8em}
.hint{color:#9aa4b2;font-size:.9em}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    data = Path(__file__).resolve().parents[1] / "data" / "midi"
    ap.add_argument("--db", type=Path, default=data / "midi.json")
    ap.add_argument("--sf2", type=Path,
                    default=data / "soundfonts" / "FluidR3_GM.sf2")
    ap.add_argument("--out", type=Path, default=data / "audition")
    ap.add_argument("--per-mood", type=int, default=3,
                    help="how many picks to draw per mood")
    ap.add_argument("--seconds", type=int, default=40, help="clip length")
    ap.add_argument("--skip", type=int, default=12,
                    help="seconds to skip, to get past the intro into the "
                         "part that actually loops")
    ap.add_argument("--gain", type=float, default=0.6)
    ap.add_argument("--moods", nargs="*", default=None)
    args = ap.parse_args()

    for tool in ("fluidsynth", "ffmpeg"):
        if not shutil.which(tool):
            print(f"need {tool} on PATH", file=sys.stderr)
            return 2
    if not args.sf2.exists():
        print(f"no SoundFont at {args.sf2}", file=sys.stderr)
        return 2

    lib = MidiLibrary(args.db)
    if not lib.available:
        print(f"no library at {args.db}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    moods = args.moods or lib.moods
    picked, failed = {}, 0

    for mood in moods:
        rows = []
        seen = set()
        # pick() keeps its own recent-repeat window, so asking N times is
        # exactly what N consecutive scene changes would produce.
        for _ in range(args.per_mood * 3):
            if len(rows) >= args.per_mood:
                break
            t = lib.pick(mood)
            if not t or t["id"] in seen:
                continue
            seen.add(t["id"])
            clip = args.out / f"{mood}-{len(rows) + 1}.opus"
            src = Path(t["file"])
            if not src.is_absolute():
                src = args.db.parent / src
            print(f"  {mood:<12} {t['game'][:28]:<30} {t['title'][:30]}",
                  flush=True)
            if not render(src, args.sf2, clip, args.seconds, args.skip,
                          args.gain):
                failed += 1
                continue
            rows.append((t, clip.name))
        picked[mood] = rows

    # --- the page -----------------------------------------------------
    parts = [f"<style>{PAGE_CSS}</style>",
             "<h1>LLM64 &mdash; MIDI library audition</h1>",
             f"<p class=hint>{len(lib.tunes)} tunes. Every clip below is "
             "what <code>MidiLibrary.pick(mood)</code> actually returned "
             f"&mdash; nothing was chosen by hand. {args.seconds}s from "
             f"{args.skip}s in, rendered with "
             f"{html.escape(args.sf2.name)}.</p>"]
    for mood in moods:
        rows = picked.get(mood, [])
        parts.append(f"<h2>{html.escape(mood)}</h2>")
        if not rows:
            parts.append("<p class=hint>no tunes tagged for this mood</p>")
            continue
        parts.append("<table>")
        for t, clip in rows:
            tags = ", ".join(f"{k} {v}" for k, v in
                             sorted(t["moods"].items(), key=lambda kv: -kv[1]))
            parts.append(
                "<tr>"
                f"<td><div class=t>{html.escape(t['title'])}</div>"
                f"<div class=g>{html.escape(t['game'])} &middot; "
                f"{html.escape(t.get('platform', '').split('/')[-1])}</div>"
                f"<div class=m>{html.escape(tags)}</div></td>"
                f"<td class=q>q {t.get('quality', 0):.2f}<br>"
                f"ic {t.get('iconic') or 0:.1f}<br>"
                f"cf {t.get('confidence') or 0:.1f}</td>"
                f"<td><audio controls preload=none src='{clip}'></audio></td>"
                "</tr>")
        parts.append("</table>")

    index = args.out / "index.html"
    index.write_text("\n".join(parts))
    total = sum(len(v) for v in picked.values())
    print(f"\n{total} clips, {failed} failed -> {index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
