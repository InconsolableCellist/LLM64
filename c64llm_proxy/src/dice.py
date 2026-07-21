"""Player-side dice macros: [roll:1d20] in what the user types.

Rolled by the proxy, substituted into the message BEFORE it reaches the
model, so the player and the model see the same number and neither can
argue with it. A model asked to "roll" invents a result and will happily
invent a flattering one; this makes the die real.

Syntax is [roll:NdX] with an optional modifier - [roll:2d6+3],
[roll:1d20-1]. N defaults to 1, so [roll:d20] works too.

It cannot collide with the [[MUSIC:]]/[[IMAGE:]]/[[STATE:]] directives
even though those now also match single brackets: those match three
fixed keywords, this matches "roll", and they travel in opposite
directions - directives come out of the model, macros go into it.
"""

import random
import re

# [roll:2d6+3] / [roll:d20] / [ROLL: 1d20 ]. Bounds are part of the
# pattern, not a later check: three digits of dice and four of sides
# cannot produce a message big enough to matter.
ROLL_RE = re.compile(
    r"\[\s*roll\s*:\s*(\d{0,3})\s*d\s*(\d{1,4})\s*([+-]\s*\d{1,3})?\s*\]",
    re.IGNORECASE)

MAX_DICE = 20
MAX_SIDES = 1000


def _roll_one(m, rng) -> str:
    n = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0

    # Out-of-range asks are left as written rather than clamped: silently
    # rolling something other than what was typed is worse than not
    # rolling at all, and the player can see it did not take.
    if not (1 <= n <= MAX_DICE) or not (2 <= sides <= MAX_SIDES):
        return m.group(0)

    rolls = [rng.randint(1, sides) for _ in range(n)]
    total = sum(rolls) + mod

    spec = f"{n}d{sides}"
    if mod:
        spec += f"{mod:+d}"
    if n == 1 and not mod:
        detail = str(total)
    else:
        detail = " + ".join(str(r) for r in rolls)
        if mod:
            detail += f" {'+' if mod > 0 else '-'} {abs(mod)}"
        detail += f" = {total}"
    return f"[you rolled {spec}: {detail}]"


def expand(text: str, rng=None):
    """(expanded_text, [roll descriptions]) - the list is empty when the
    message contained no macros, which is the common case and lets the
    caller skip the echo entirely."""
    rng = rng or random
    rolled = []

    def sub(m):
        out = _roll_one(m, rng)
        if out != m.group(0):
            rolled.append(out.strip("[]"))
        return out

    return ROLL_RE.sub(sub, text), rolled
