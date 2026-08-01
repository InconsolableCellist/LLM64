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


# --- the narrator's own dice ------------------------------------------
#
# A pool of real rolls handed to the model with every adventure turn, so
# it can resolve a check ITSELF instead of stopping the story to ask the
# player for one. The player's [roll:...] macro still exists and still
# wins when they choose to use it; this is for the far more common case
# where the narrator should just roll and get on with it.
#
# Pre-generated rather than round-tripped: a model that emits a roll
# REQUEST needs a second call to resolve it, doubling the latency of
# every fight over a 9600 baud link. These cost nothing - they ride
# along in the prompt that was going to be sent anyway.
#
# Rolled by the proxy for the same reason the macro is: a model asked to
# roll invents a number, and invents a flattering one.
POOL_SPEC = (('d20', 20, 6), ('d6', 6, 6), ('d100', 100, 2))


def pool(rng=None) -> str:
    """One turn's worth of dice, as a prompt block. Deterministic given
    an rng, so the e2e can assert on it."""
    rng = rng or random
    lines = []
    for label, sides, count in POOL_SPEC:
        rolls = [rng.randint(1, sides) for _ in range(count)]
        lines.append(f"  {label}: " + ", ".join(str(r) for r in rolls))
    return "\n".join(lines)


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


# --- the [[ROLL:]] directive ------------------------------------------
#
# The narrator's answer to "which die decided that?": when it spends one
# of pool()'s dice it stamps [[ROLL: d20 14+3=17 vs 12, attack - hit]]
# into the reply. The directive filter swaps the stamp for the rendered
# line below on its way to the player, and the SAVED reply drops it
# entirely (MusicDirectiveFilter.strip_notes) - so the player always
# sees the die, while the model never rereads its own roll-talk and
# cannot ratchet itself into rolling for the quiet moments.

ROLL_NOTE_MAX = 100


def render_roll(payload: str) -> str:
    """The player-visible line for one [[ROLL: ...]] payload.

    Rendered as "dice", not "roll": [roll: ...] is the PLAYER macro's
    spelling and [[ROLL:]] the directive's, and this line travels back
    through text both parsers scan - "dice" matches neither, so a
    rendered line can never be re-rolled or re-extracted."""
    text = " ".join(str(payload).split())
    if len(text) > ROLL_NOTE_MAX:
        text = text[:ROLL_NOTE_MAX - 3] + "..."
    return f"[dice: {text}]"
