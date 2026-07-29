#!/usr/bin/env python3
"""Stage 3 of the MIDI pipeline: classify tunes into adventure moods.

The SID tagger's twin, and deliberately not a fork of it: MOODS,
SETTINGS, the scoring axes and the JSON contract are IMPORTED from
sid_mood.py rather than copied.

That is not tidiness, it is a correctness requirement. The narrator is
offered the mood vocabulary in its system prompt
(MusicLibrary.prompt_snippet), and nothing upstream of the egress edge
knows which client it is talking to - by design, so a C64 and a Windows
machine can share one adventure (docs/16 section 13). If the two
libraries were tagged against two vocabularies, the model would be told
about moods that only half the world could play. One import makes that
impossible.

What changes is the EVIDENCE. The SID tagger gets a filesystem path and,
when it is lucky, a STIL entry. Here every tune arrives with:

  - the game it is from        (VGMusic's index page, human-entered)
  - a written song title       ("Forest Interlude", not FOREST3.MID)
  - who sequenced it
  - exact duration
  - the actual GM instruments the file selects

That last one is evidence the SID side cannot have. "Choir Aahs, String
Ensemble, Timpani" and "Music Box, Celesta, Harp" are different moods
before anybody has heard a note.
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Same directory; running this as a script puts it on sys.path.
from sid_mood import MOODS, SETTINGS, chat, extract_json   # noqa: E402

# General MIDI program names, index = program number. The file stores the
# number; the model reasons far better about the word.
GM = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
    "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2",
    "Harpsichord", "Clavi", "Celesta", "Glockenspiel", "Music Box",
    "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)",
    "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar",
    "Guitar Harmonics", "Acoustic Bass", "Electric Bass (finger)",
    "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1", "Slap Bass 2",
    "Synth Bass 1", "Synth Bass 2", "Violin", "Viola", "Cello",
    "Contrabass", "Tremolo Strings", "Pizzicato Strings",
    "Orchestral Harp", "Timpani", "String Ensemble 1", "String Ensemble 2",
    "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs",
    "Synth Voice", "Orchestra Hit", "Trumpet", "Trombone", "Tuba",
    "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1",
    "Synth Brass 2", "Soprano Sax", "Alto Sax", "Tenor Sax",
    "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle",
    "Shakuhachi", "Whistle", "Ocarina", "Lead 1 (square)",
    "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
    "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)",
    "Lead 8 (bass + lead)", "Pad 1 (new age)", "Pad 2 (warm)",
    "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)",
    "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)", "FX 1 (rain)",
    "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)",
    "FX 8 (sci-fi)", "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba",
    "Bagpipe", "Fiddle", "Shanai", "Tinkle Bell", "Agogo", "Steel Drums",
    "Woodblock", "Taiko Drum", "Melodic Tom", "Synth Drum",
    "Reverse Cymbal", "Guitar Fret Noise", "Breath Noise", "Seashore",
    "Bird Tweet", "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]

SYSTEM_PROMPT = f"""You are tagging General MIDI video-game music for a text-adventure game's dynamic soundtrack. The game engine picks background music by mood, so accurate tags matter more than generous ones.

For each tune you receive: the game it comes from, the song's title, the platform, who sequenced it, how long it is, and the actual General MIDI instruments the file plays. Use all of it - the instrument list is real evidence about the arrangement, not a guess.

Moods (pick 1-3 that the music would fit, weight 0..1):
{", ".join(MOODS)}

Settings (pick 0-2 game genres the music evokes, weight 0..1 - leave empty when generic):
{", ".join(SETTINGS)}

Also score:
- arcadey: 0..1. High = abstract high-energy arcade bleeping (score attack, pinball, puzzle loops, sports). Low = evocative scene-setting music. Adventure games want low-arcadey music, so be honest.
- iconic: 0..1. How instantly recognizable this is as a SPECIFIC famous theme (the Mario overworld, the Zelda overworld, Sonic's Green Hill = 1.0; an obscure game's cave theme = 0.1). Iconic tunes yank players out of the story and feel cheesy, so the selector avoids them - flag honestly.
- confidence: 0..1. Base it on how much you actually know: a well-known game whose score you can recall is high; an obscure title with a generic song name is low (0.2 or less). Never guess moods confidently from a filename alone.

A song title like "Battle", "Cave", "Town", "Ending" is strong evidence and should raise confidence. A title that is just a track number is not.

Reply with ONLY a JSON array, one object per tune, same order as given:
[{{"i": <index>, "moods": {{"<mood>": <w>}}, "settings": {{"<setting>": <w>}}, "arcadey": <x>, "iconic": <x>, "confidence": <x>}}]"""


def instrument_words(rec: dict, limit: int = 10) -> str:
    names = [GM[p] for p in rec.get("programs", []) if 0 <= p < len(GM)]
    if len(names) > limit:
        names = names[:limit] + [f"+{len(names) - limit} more"]
    if rec.get("drum_fraction", 0) > 0.05:
        names.append("drum kit")
    return ", ".join(names) if names else "(none declared - default piano)"


def tune_blurb(rec: dict) -> str:
    mins, secs = divmod(int(rec["seconds"]), 60)
    lines = [
        f"game: {rec.get('game') or '(unknown)'}",
        f"song: {rec.get('title') or rec['file']}",
        f"platform: {rec.get('platform', '')}",
        f"length: {mins}:{secs:02d}",
        f"instruments: {instrument_words(rec)}",
    ]
    if rec.get("seq"):
        lines.append(f"sequenced by: {rec['seq']}")
    # The file's own meta text sometimes names the piece or the composer
    # when the index page was thin.
    embedded = [s for s in (rec.get("names") or []) if len(s) > 2][:4]
    if embedded:
        lines.append("track names: " + "; ".join(embedded)[:200])
    return "\n".join(lines)


def classify_batch(base_url: str, model: str, batch: list) -> list:
    listing = "\n\n".join(
        f"[{i}]\n{tune_blurb(rec)}" for i, rec in enumerate(batch))
    reply = chat(base_url, model, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",
         "content": f"Tag these {len(batch)} tunes:\n\n{listing}"},
    ], max_tokens=512 + 90 * len(batch))
    tags = {t["i"]: t for t in extract_json(reply)}
    out = []
    for i, rec in enumerate(batch):
        t = tags.get(i, {})
        moods = {k: v for k, v in t.get("moods", {}).items() if k in MOODS}
        settings = {k: v for k, v in t.get("settings", {}).items()
                    if k in SETTINGS}
        out.append({
            "path": rec["path"],
            "title": rec.get("title") or rec["file"],
            "game": rec.get("game", ""),
            "author": rec.get("seq", ""),
            "moods": moods,
            "settings": settings,
            "arcadey": t.get("arcadey"),
            "iconic": t.get("iconic"),
            "confidence": t.get("confidence"),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("candidates", type=Path, help="scan.json from midi_scan.py")
    ap.add_argument("--base-url", default="http://localhost:5000/v1")
    ap.add_argument("--model", default="gemma4-26b-a4b-it-qat-q4-mlboy")
    ap.add_argument("--pilot", type=int, metavar="N",
                    help="classify only the first N tunes")
    ap.add_argument("--batch-size", type=int, default=24)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    records = json.loads(args.candidates.read_text())
    if args.pilot:
        records = records[:args.pilot]

    results = []
    if args.output.exists():
        done = {r["path"]: r for r in json.loads(args.output.read_text())
                if "error" not in r}
        results = list(done.values())
        before = len(records)
        records = [r for r in records if r["path"] not in done]
        if before != len(records):
            print(f"resuming: {before - len(records)} already tagged",
                  file=sys.stderr)

    lock = threading.Lock()
    done_count = [0]
    started = time.monotonic()

    def run_batch(start):
        batch = records[start:start + args.batch_size]
        for attempt in (1, 2):
            try:
                out = classify_batch(args.base_url, args.model, batch)
                break
            except Exception as e:                          # noqa: BLE001
                if attempt == 2:
                    print(f"batch at {start} failed twice: {e}",
                          file=sys.stderr)
                    out = [{"path": r["path"],
                            "title": r.get("title") or r["file"],
                            "error": str(e)} for r in batch]
                else:
                    print(f"batch at {start}: retrying ({e})", file=sys.stderr)
        with lock:
            results.extend(out)
            args.output.write_text(json.dumps(results, indent=1))
            done_count[0] += len(batch)
            rate = done_count[0] / max(1.0, time.monotonic() - started)
            print(f"{done_count[0]}/{len(records)} ({rate:.1f} tunes/s)",
                  file=sys.stderr, flush=True)

    starts = range(0, len(records), args.batch_size)
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(run_batch, starts))
    else:
        for s in starts:
            run_batch(s)
    print(f"done: {len(results)} tunes tagged")


if __name__ == "__main__":
    main()
