#!/usr/bin/env python3
"""Stage 3 of the SID pipeline: classify tunes into adventure-game moods.

Joins the sid_scan.py candidate list with HVSC's STIL metadata and asks an
OpenAI-compatible endpoint (the same llama.cpp server the proxy uses) to
tag each tune. Two axes plus modifiers:

  moods    - what game situation the music fits (combat, serene, ...)
  settings - what genre of game it evokes (fantasy, scifi, ...); empty
             means generic/no strong genre signal
  arcadey  - 0..1, how much it reads as abstract arcade bleeping rather
             than scene-setting music (high = down-rank for adventures)
  confidence - 0..1, the model's own certainty; filenames alone with no
             STIL entry and no recognizable game name should score low

Run with --pilot N for a small graded batch to verify quality by hand.
"""

import argparse
import json
import re
import ssl
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MOODS = [
    "combat", "heroic", "triumphant", "tense", "menacing", "eerie",
    "mysterious", "melancholy", "somber", "serene", "dreamlike",
    "playful", "festive", "adventurous", "urgent",
]
SETTINGS = [
    "fantasy", "scifi", "military", "horror", "noir", "western",
    "nautical", "post_apocalyptic", "urban", "whimsical",
]

SYSTEM_PROMPT = f"""You are tagging Commodore 64 SID music for a text-adventure game's dynamic soundtrack. The game engine picks background music by mood, so accurate tags matter more than generous ones.

For each tune you receive: the HVSC file path (the directory often names the game or demo scene context), title, artist, release info, and the tune's STIL entry (community-maintained notes) when one exists.

Moods (pick 1-3 that the music would fit, weight 0..1):
{", ".join(MOODS)}

Settings (pick 0-2 game genres the music evokes, weight 0..1 - leave empty when generic):
{", ".join(SETTINGS)}

Also score:
- arcadey: 0..1. High = abstract high-energy arcade bleeping (score attack, pinball, puzzle loops). Low = evocative scene-setting music. Adventure games want low-arcadey music, so be honest.
- iconic: 0..1. How instantly recognizable this is as a SPECIFIC famous theme (Pac-Man, Indiana Jones, Tetris = 1.0; an obscure game's title tune = 0.1). Iconic tunes yank players out of the story and feel cheesy, so the selector avoids them - flag honestly.
- confidence: 0..1. Base it on how much you actually know: a famous game theme or a rich STIL entry is high; a bare cryptic filename is low (0.2 or less). Never guess moods confidently from a filename alone.

Use what you know about the actual games (e.g. a war game's title music is likely military/heroic), the composer's typical style, and any STIL comments about covers or intended feel.

Reply with ONLY a JSON array, one object per tune, same order as given:
[{{"i": <index>, "moods": {{"<mood>": <w>}}, "settings": {{"<setting>": <w>}}, "arcadey": <x>, "iconic": <x>, "confidence": <x>}}]"""


def parse_stil(path: Path) -> dict:
    """Map '/GAMES/.../Foo.sid' -> raw STIL block text."""
    entries = {}
    cur_path, cur_lines = None, []
    for line in path.read_text(encoding="latin-1").splitlines():
        if line.startswith("### ") or line.startswith("#"):
            continue
        if line.startswith("/"):
            if cur_path:
                entries[cur_path] = "\n".join(cur_lines).strip()
            cur_path, cur_lines = line.strip(), []
        elif cur_path:
            if line.strip():
                cur_lines.append(line.rstrip())
            elif cur_lines:
                entries[cur_path] = "\n".join(cur_lines).strip()
                cur_path, cur_lines = None, []
    if cur_path and cur_lines:
        entries[cur_path] = "\n".join(cur_lines).strip()
    return entries


def hvsc_rel(path: str) -> str:
    """'.../C64Music/GAMES/Foo.sid' -> '/GAMES/Foo.sid' (STIL key form)."""
    m = re.search(r"C64Music(/.*)$", path)
    return m.group(1) if m else path


def tune_blurb(rec: dict, stil: dict) -> str:
    rel = hvsc_rel(rec["path"])
    lines = [f"path: {rel}",
             f"title: {rec['name']}",
             f"artist: {rec['author']}",
             f"released: {rec['released']}"]
    entry = stil.get(rel)
    if entry:
        lines.append("stil: " + entry[:600])
    return "\n".join(lines)


def chat(base_url: str, model: str, messages: list, timeout: int = 90,
         max_tokens: int = 4096) -> str:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        # thinking otherwise consumes the whole token budget and content
        # comes back empty (same reason the proxy disables it)
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def extract_json(text: str):
    """Model output -> parsed JSON array (tolerates code fences/prose)."""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON array in reply: {text[:200]}")
    return json.loads(m.group(0))


def classify_batch(base_url: str, model: str, batch: list, stil: dict) -> list:
    listing = "\n\n".join(
        f"[{i}]\n{tune_blurb(rec, stil)}" for i, rec in enumerate(batch))
    reply = chat(base_url, model, [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Tag these {len(batch)} tunes:\n\n{listing}"},
    ], max_tokens=512 + 90 * len(batch))
    tags = {t["i"]: t for t in extract_json(reply)}
    out = []
    for i, rec in enumerate(batch):
        t = tags.get(i, {})
        # the model occasionally leaks a setting into moods (or invents
        # labels); keep only taxonomy keys
        moods = {k: v for k, v in t.get("moods", {}).items() if k in MOODS}
        settings = {k: v for k, v in t.get("settings", {}).items()
                    if k in SETTINGS}
        out.append({
            "path": hvsc_rel(rec["path"]),
            "title": rec["name"],
            "author": rec["author"],
            "moods": moods,
            "settings": settings,
            "arcadey": t.get("arcadey"),
            "iconic": t.get("iconic"),
            "confidence": t.get("confidence"),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("candidates", type=Path,
                    help="JSON list from sid_scan.py (optionally filtered)")
    ap.add_argument("--stil", type=Path,
                    default=Path("data/sids/C64Music/DOCUMENTS/STIL.txt"))
    ap.add_argument("--base-url", default="https://mlboy.tail99c274.ts.net:5000/v1")
    ap.add_argument("--model", default="gemma4-26b-a4b-it-qat-q4-mlboy")
    ap.add_argument("--pilot", type=int, metavar="N",
                    help="classify only the first N tunes")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent requests (match the server's parallel "
                         "slot count)")
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    records = json.loads(args.candidates.read_text())
    if args.pilot:
        records = records[:args.pilot]
    stil = parse_stil(args.stil)

    # Resume support: an overnight run must survive interruption. Output is
    # rewritten after every batch; on restart, already-tagged tunes are
    # skipped (failed ones are retried).
    results = []
    if args.output.exists():
        done = {r["path"]: r for r in json.loads(args.output.read_text())
                if "error" not in r}
        results = list(done.values())
        before = len(records)
        records = [r for r in records if hvsc_rel(r["path"]) not in done]
        if before != len(records):
            print(f"resuming: {before - len(records)} already tagged",
                  file=sys.stderr)
    lock = threading.Lock()
    done_count = [0]
    started = time.monotonic()

    def run_batch(start):
        batch = records[start:start + args.batch_size]
        # one retry: a lost request (model reload, network blip) shouldn't
        # cost a whole batch
        for attempt in (1, 2):
            try:
                out = classify_batch(args.base_url, args.model, batch, stil)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"batch at {start} failed twice: {e}",
                          file=sys.stderr)
                    out = [{"path": hvsc_rel(r["path"]), "title": r["name"],
                            "author": r["author"], "error": str(e)}
                           for r in batch]
                else:
                    print(f"batch at {start}: retrying ({e})",
                          file=sys.stderr)
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
    with_stil = sum(1 for r in records if hvsc_rel(r["path"]) in stil)
    print(f"done: {len(results)} tunes tagged ({with_stil} had STIL entries)")


if __name__ == "__main__":
    main()
