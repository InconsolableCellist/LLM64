#!/usr/bin/env python3
"""MusicDirectiveFilter: directive extraction and stream hold-back.

Covers the failure that motivated the single-bracket fallback: the model
copies the adventure status line's "[HP .. | Gold ..]" shape and emits
"[MUSIC: eerie]", which used to print on the C64 and do nothing.

Every case runs twice - once as a whole string, once fed one character at
a time - because the streaming path has its own hold-back logic and a
directive split across chunks must survive reassembly.

Run: python3 tests/test_directives.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.music import MusicDirectiveFilter

STATE_JSON = ('{"hp":15,"maxhp":20,"gold":0,"inventory":["rope","lamp"],'
              '"companions":[]}')
BAR = "[HP 15/20 | Gold 0 | The Whispering Woods]"

failures = []


def run(text, chunked):
    f = MusicDirectiveFilter()
    if chunked:
        out = "".join(f.feed(c) for c in text) + f.flush()
    else:
        out = f.feed(text) + f.flush()
    return out, f


def check(name, text, want_out, want_moods=(), want_images=(), want_states=()):
    for chunked in (False, True):
        out, f = run(text, chunked)
        how = "chunked" if chunked else "whole"
        for label, got, want in (
                ("text", out, want_out),
                ("moods", f.moods, list(want_moods)),
                ("images", f.images, list(want_images)),
                ("states", f.states, list(want_states))):
            if got != want:
                failures.append(
                    f"{name} [{how}] {label}:\n  got  {got!r}\n  want {want!r}")


# Canonical form still works.
check("canonical music", "[[MUSIC: eerie]]hello", "hello", want_moods=["eerie"])
check("canonical image", "[[IMAGE: a dark wood]]x", "x",
      want_images=["a dark wood"])

# The regression: single brackets, as the model actually emitted them.
check("single music", "[MUSIC: tense]\nYou kick.", "\nYou kick.",
      want_moods=["tense"])
check("single image", "[IMAGE: a stone arch]\nYou run.", "\nYou run.",
      want_images=["a stone arch"])

# Both shapes in one reply, after a status line.
check("bar + both", f"{BAR}\n\n[MUSIC: eerie]\n\n[[IMAGE: an arch]]\n\nText.",
      f"{BAR}\n\n\n\n\n\nText.", want_moods=["eerie"], want_images=["an arch"])

# The status line must survive untouched - it is not a directive.
check("status bar alone", f"{BAR}\nYou wait.", f"{BAR}\nYou wait.")

# STATE keeps its JSON intact: the ']' inside "inventory" must not close
# it, which is why STATE has no single-bracket fallback.
check("state json", f"Text.\n[[STATE: {STATE_JSON}]]", "Text.\n",
      want_states=[STATE_JSON])

# A STATE block the model closed with ONE bracket instead of two. Field
# case 2026-07-21: the whole block printed on screen and the state was
# lost. Accepting it is safe because this rule is anchored on the JSON
# OBJECT - the match must end at '}', so the ']' closing "inventory"
# cannot terminate it early.
BROKEN = ('{"hp":18,"maxhp":20,"inventory":["iron key"],'
          '"companions:[]}')
check("state closed with one bracket", f"Text.\n[[STATE: {BROKEN}]",
      "Text.\n", want_states=[BROKEN])

# ...and the same leniency must not let a one-bracket MUSIC/IMAGE be
# mistaken for a state block, nor swallow following text.
check("one-bracket state does not eat the tail",
      f"[[STATE: {BROKEN}] and more", " and more", want_states=[BROKEN])

# A single-bracket value may not swallow a newline or a ']'.
check("not a directive", "[MUSIC no colon] stays", "[MUSIC no colon] stays")
check("bracketed prose", "He said [see below] and left.",
      "He said [see below] and left.")

# Nothing may leak: a directive split across chunk boundaries is the
# whole reason feed() holds text back.
check("held across chunks", "a[[MUSIC: calm]]b", "ab", want_moods=["calm"])
# An opener the model never closes is just malformed text: flush() hands
# it back rather than silently eating the rest of the reply.
check("unterminated opener", "text [[MUSIC: hmm", "text [[MUSIC: hmm")

# [[ROLL:]] is replaced, not removed: the player sees the rendered
# [dice: ...] line exactly where the stamp fell, .rolls keeps the payload
# for the audit log, and strip_notes() returns the prose-only form that
# history keeps.
ROLL = "d20 14+3=17 vs 12, attack - hit"
for chunked in (False, True):
    how = "chunked" if chunked else "whole"
    text = f"You swing. [[ROLL: {ROLL}]] The blade bites."
    out, f = run(text, chunked)
    if out != f"You swing. [dice: {ROLL}] The blade bites.":
        failures.append(f"roll rendered [{how}]: got {out!r}")
    if f.rolls != [ROLL] or f.roll_texts != [f"[dice: {ROLL}]"]:
        failures.append(f"roll recorded [{how}]: {f.rolls!r} {f.roll_texts!r}")
    if f.strip_notes(out) != "You swing.  The blade bites.":
        failures.append(f"strip_notes [{how}]: got {f.strip_notes(out)!r}")

# ROLL deliberately has NO single-bracket fallback: "[roll:1d20]" is the
# player macro, and the narrator writes it out when inviting the player
# to roll. Both must pass through as text.
for chunked in (False, True):
    for invite in ("[ROLL: d6 3, damage] stays",
                   "Roll [roll:1d20] to strike, and add your STR."):
        out, f = run(invite, chunked)
        if out != invite or f.rolls:
            failures.append(
                f"single-bracket roll not left alone: {out!r} {f.rolls!r}")

# The rendered line must never be re-parseable - by this filter (fresh
# instance, as history replayed through anything) or by the player-macro
# regex in dice.py.
from src.dice import ROLL_RE, render_roll
rendered = render_roll(ROLL)
out2, f2 = run(rendered, False)
if out2 != rendered or f2.rolls:
    failures.append(f"rendered line re-parsed: {out2!r} {f2.rolls!r}")
if ROLL_RE.search(rendered):
    failures.append(f"rendered line matches the player macro: {rendered!r}")

# render_roll flattens whitespace and caps runaway payloads.
if render_roll("d20\n 14 vs   9") != "[dice: d20 14 vs 9]":
    failures.append(f"render_roll whitespace: {render_roll('d20  14 vs 9')!r}")
if len(render_roll("x" * 500)) > len("[dice: ]") + 100:
    failures.append("render_roll did not cap a runaway payload")

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all directive tests pass")
