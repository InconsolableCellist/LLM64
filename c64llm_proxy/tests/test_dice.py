#!/usr/bin/env python3
"""Dice macro expansion. Run: python3 tests/test_dice.py

Uses a seeded RNG so the numbers are exact rather than pattern-matched -
a test that only checks the shape would not notice modifiers being
applied twice, or dropped.
"""

import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.dice import expand, ROLL_RE
from src.music import DIRECTIVE_RE

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def seeded():
    return random.Random(1234)


# Known sequence for seed 1234 so totals can be asserted exactly.
r = seeded()
d20 = r.randint(1, 20)
r = seeded()
three_d6 = [r.randint(1, 6) for _ in range(3)]

out, rolls = expand("I swing wildly [roll:1d20]", seeded())
check("single die", out, f"I swing wildly [you rolled 1d20: {d20}]")
check("single die reported", rolls, [f"you rolled 1d20: {d20}"])

out, _ = expand("[roll:d20] shorthand", seeded())
check("implicit count", out, f"[you rolled 1d20: {d20}] shorthand")

out, _ = expand("[roll:3d6]", seeded())
detail = " + ".join(str(x) for x in three_d6)
check("multiple dice show each", out,
      f"[you rolled 3d6: {detail} = {sum(three_d6)}]")

out, _ = expand("[roll:3d6+2]", seeded())
check("positive modifier", out,
      f"[you rolled 3d6+2: {detail} + 2 = {sum(three_d6) + 2}]")

out, _ = expand("[roll:3d6-2]", seeded())
check("negative modifier", out,
      f"[you rolled 3d6-2: {detail} - 2 = {sum(three_d6) - 2}]")

out, rolls = expand("[roll:1d20] then [roll:1d20]", seeded())
check("two macros both roll", len(rolls), 2)
if "[roll:" in out:
    failures.append(f"two macros: unexpanded macro left in {out!r}")

# Case and spacing tolerance - people type what they type.
out, rolls = expand("[ ROLL : 2d6 + 1 ]", seeded())
check("tolerant of case and spaces", len(rolls), 1)

# Refusals are left verbatim: silently rolling something other than what
# was asked for is worse than not rolling.
for bad in ("[roll:0d6]", "[roll:99d6]", "[roll:1d1]", "[roll:1d99999]"):
    out, rolls = expand(bad, seeded())
    check(f"out of range left alone {bad}", (out, rolls), (bad, []))

check("no macro, no work", expand("just talking", seeded()),
      ("just talking", []))

# The point of the syntax choice: it cannot be confused with the
# directive filter, which now also accepts single brackets.
for probe in ("[roll:1d20]", "[you rolled 1d20: 7]"):
    if DIRECTIVE_RE.search(probe):
        failures.append(f"directive filter matched a dice macro: {probe!r}")
for probe in ("[[MUSIC: eerie]]", "[MUSIC: eerie]", "[IMAGE: a door]"):
    if ROLL_RE.search(probe):
        failures.append(f"dice matched a directive: {probe!r}")

if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("all dice tests pass")
