# Adventure setup: chooser, staged creation, templates — design

Goal: `/adventure` stops being a one-shot and becomes a front door.
Pick how much preparation you want, optionally build a world and
character through a short interview, have the model prep the campaign
the way a GM would, then play — and keep what you built as a reusable
template.

**Verdict: feasible, and it costs ZERO client bytes.** Every screen is
canned text and numbered replies, which the client already renders; no
new frames, no wire change, no lockstep deploy, no reboot. All of it
lives in the proxy.

## 1. Thinking mode — corrected, and load-bearing here

The note in HANDOFF.md ("requests must send `enable_thinking: false` or
replies are empty") was **wrong about the cause**, and the mistake
matters because the prep pass in §4 depends on it.

Measured against the live server 2026-07-21:

| max_tokens | finish_reason | completion tokens | wall | content |
|-----------:|---------------|------------------:|-----:|---------|
| 400        | `length`      | 400               | 7.7s | *empty* |
| 2000       | `stop`        | 1285              | 24s  | full    |
| 6000       | `stop`        | 1157              | 22s  | full    |

Thinking works fine. It emits `reasoning_content` first and only then
`content`, so a small budget is spent entirely on reasoning and the
answer never arrives — which looked like "thinking is broken". With
room it completes normally.

Consequences:
- Thinking needs **≥ ~2000 completion tokens**. The normal chat budget
  (`max_tokens`, 2000 by default but often lower per mode) must be
  raised for any request that enables it.
- ~20-25s per call. The client watchdog is ~40s and `_heartbeat` already
  feeds it every 10s, so a single thinking call is safe. Two in a row is
  not — keep prep to one call per stage.
- Streaming shows `reasoning` chunks, which `_stream_response` already
  routes to a "Thinking..." status instead of the chat area. That path
  exists and works.
- Keep it OFF for ordinary turns: 20s per reply would ruin play.

## 2. The chooser

`/adventure` with no argument sends a canned numbered list and parks a
pending choice. `/adventure <theme>` keeps working exactly as today, so
nothing anyone has learned is invalidated.

```
Start an adventure:

  1  Surprise me
  2  I have an idea (one line)
  3  Build a world and character
  4  Load a saved world        <- only when templates exist

Reply with a number, or /chat to cancel.
```

Precedent for "the next message answers a question" is already in the
codebase twice: Claude Code's tool-permission y/n, and the disk copier's
delete confirm. Session state, not conversation state - a proxy restart
mid-choice simply forgets it, which is the right failure.

## 3. Staged creation (option 3)

The proxy owns the STAGE LIST; the model generates the content of each
stage. Deterministic where it needs to be testable, generative where it
needs to be good.

| # | Stage | Model does | Player does |
|---|-------|-----------|-------------|
| 1 | World | offers 3 short settings, or takes yours | picks a number or types |
| 2 | Tone | offers tone/danger/humour options | picks or types |
| 3 | Character | offers 3 characters fitting the world | picks or types |
| 4 | Opening | proposes where and how it starts | picks or types |

Each stage shows `[step N of 4]` so the flow is legible on a 40-column
screen. Any stage accepts free text instead of a number - the numbered
options are a convenience, never a cage. `/back` re-runs the previous
stage; `/chat` cancels the whole thing.

Then:

```
Ready to begin?  y = start   e N = change step N   /chat = cancel
```

**The interview runs in a scratch buffer, NOT in the conversation.**
`_switch_mode()` opens a fresh conversation, so the adventure must not
be created until "y" - otherwise abandoning halfway leaves a wrecked
conversation behind. It also keeps the interview transcript out of the
adventure's context, which matters: the model should start with the
world, not with a memory of being asked about it.

## 4. The prep pass (where thinking earns its keep)

On "y", one thinking-enabled call produces the campaign bible: plot
beats it intends to hit, secrets, a few named NPCs, hazards, and the
initial `[[STATE]]` JSON. This is the "DM prepares a campaign" step -
slow, once, off the critical path, and exactly the case §1 says
thinking is for.

The bible goes into the adventure's system prompt as authoritative
background, alongside the existing state/music/image/colour snippets.
It is NOT shown to the player.

Budget: `max_tokens` raised to ~3000 for this one call, `enable_thinking`
on, `_heartbeat` running so the client watchdog stays fed. The C64 shows
"Preparing the world... (20-30s)".

## 4b. Where the character lives (measured)

The rolled character is "always in the context window" - but that costs
essentially nothing if it is placed correctly, and the placement rule is
the whole answer.

llama.cpp prefix-caches the prompt. Measured against the live server
2026-07-21, same system prompt, different user message each call:

| call | prompt tokens | cached | wall |
|------|--------------:|-------:|-----:|
| 1 (cold) | 1747 | 0 | 2.07s |
| 2 | 1747 | **1736** | **0.16s** |
| 3 | 1747 | **1736** | **0.16s** |

So the split is:

- **Immutable sheet** - race, class, ability scores, skills, traits,
  spells known, background, appearance - goes in the STABLE head of the
  system prompt (inside `mode.system_prompt()`), together with the
  campaign bible. Re-sent every turn, cached every turn, ~free.
- **Mutable state** - hp, mana, gold, xp, location, inventory,
  companions, conditions - stays in `adv_state`, which is APPENDED after
  the stable text (protocol.py:1422-1435) and re-injected each turn.
  This is the part that must stay small.

**The rule that makes it work: anything that changes must come AFTER
everything that does not.** A mutable field in the middle of the system
prompt invalidates the cache from that point on. The current code
already appends `adv_state` last, and the music-stale nudge after that -
correct today, but worth stating so it stays correct.

Corollary: the campaign bible from §4 is free per-turn for the same
reason, which is what makes a long prep pass affordable.

## 4c. Character creation, classic style

Stage 3 is not "pick one of three" but a real creation flow, D&D /
Daggerfall shaped. The split follows the same principle as the dice
macros: **the proxy owns the mechanics, the model owns the flavour.**
Asking a model to roll gets you an invented and suspiciously flattering
number, and asking it to apply racial modifiers gets you arithmetic it
will quietly fudge.

| step | proxy (deterministic) | model (flavour) |
|------|----------------------|-----------------|
| roll | 4d6-drop-lowest x6 via `dice.py` | comments on the spread |
| race | applies modifiers from the rules table | describes the people |
| class | offers those the scores qualify for | describes the calling |
| points | validates the allocation | - |
| skills | enforces picks-allowed | suggests thematic sets |
| spells/traits | filters by class | names and describes them |
| identity | - | proposes names, appearance |

The rules table (races, classes, skills, spell lists, modifiers) lives
in editable JSON rather than code, so a setting can ship its own -
fantasy, sci-fi, whatever the world stage produced. That also keeps the
model from having to remember the rules: it is handed the legal options
each step.

Rolls are shown, not hidden - the existing `[roll:NdX]` echo path is
right there, and seeing 4d6-drop-lowest land is half the pleasure.

Open: point-buy as an alternative to rolling, for players who hate
losing a character to bad dice before they have one. Cheap to add, and
Daggerfall-ish.

## 5. Templates

The moment an adventure begins, the bundle is saved:

```
data/adventures/<slug>.json
  { name, created, world, tone, character, opening, bible, model }
```

Named by the model (same trick as conversation auto-titling). Option 4
lists them newest-first and loading one skips straight to play with the
saved bible - so a world you liked can be replayed with a different
character, and a good prep pass is never paid for twice.

Templates are proxy-side for the same reason favourites are: the C64
cannot hold them, and they should outlive a disk swap.

Loading a template offers BOTH (user's call, 2026-07-21): replay the
saved character exactly, or keep the world and re-roll a new character
through §4c. Re-roll is the more useful of the two and the reason the
sheet is stored separately from the world and bible in the template.

## 6. What this does NOT need

- No client changes. No new message types. No d64 rebuild.
- No new directive syntax - the bible is prompt content, not markup.
- No change to `[[STATE]]`, though the prep pass now seeds the first one
  instead of the model inventing it on turn 1 (which is also the fix for
  "old adventures have no status bar until the model emits one").

## 7. Ordering and tests

1. Chooser + pending-choice state + `/adventure <theme>` unchanged.
   Unit-testable: feed choices, assert the state machine.
2. Stages, `/back`, cancel. Still no model needed for the state machine
   itself - the mock supplies stage content.
3. Prep pass with thinking. Assert `max_tokens` is raised and that a
   `reasoning`-only response is not mistaken for an empty reply.
4. Templates: save, list, load. Round-trip test.
5. e2e: the mock answers each stage deterministically; assert the
   adventure starts with the bible in the system prompt and a valid
   first `[[STATE]]`.

## 8. Risks

- **Interview fatigue.** Four stages on a 40-column screen at modem
  speed is a lot of reading. Mitigation: every stage is skippable, and
  option 1 stays one keystroke away. If it drags in practice, cut to
  two stages (world+character).
- **Thinking latency.** 20-30s of silence at the most exciting moment.
  The heartbeat covers the watchdog, but the status text has to make it
  feel deliberate rather than hung.
- **A wrong-shaped bible** poisons the whole adventure, and unlike
  `adv_state` there is no per-turn correction. Keep it short and
  validate it parses before committing the template.
- **Scope.** This is the largest proxy-side feature so far. Stages 1-2
  are independently useful and shippable without 3-5.
