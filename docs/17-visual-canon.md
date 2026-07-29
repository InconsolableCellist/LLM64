# 17 - Visual canon: the same face every time

*Investigation and implementation, 2026-07-29. Follows docs/13, which
made image prompts anchored and steerable; this makes them CONSISTENT.
Proxy-side and profile-agnostic: the C64's multicolor conversions and
the Windows client's DIBs both inherit it, because it lives upstream of
either.*

## 0. The problem

Illustration N of the player character rarely looks like illustration
N-1. The composition step (docs/13) already anchors each prompt to game
state, but its consistency inputs decay:

- **The priors decay.** compose_question is shown the last three
  illustration *prompts* - descriptions of scenes, not of people. If the
  player was last illustrated five pictures ago, their appearance has
  scrolled out of the window entirely.
- **The transcript drifts.** The 12-message excerpt may or may not
  contain the sentence where the narrator described the character's
  cloak, and the composing model re-derives appearance from whatever it
  happens to see, differently each time.
- **The STATE block is terse.** `appearance` is one narrator-maintained
  phrase ("a wiry traveler in a patched gray cloak"), useful but far
  short of what an image prompt needs, and the narrator rewrites it
  freely.

The result is a character whose hair, build and gear vary between
pictures of the same afternoon. Recurring NPCs and places fare worse -
nothing tracks them at all.

## 1. The decision

**A settled ledger, written once, injected verbatim, amended only on
conflict.** Conversation META gains `visual_canon`:

```json
{
  "player":  "one precise visual sentence",
  "npcs":    {"Name": "one visual sentence"},
  "places":  {"Name": "one visual sentence"},
  "appearance_seen": "the STATE appearance it was built against",
  "built_at_msg": 4,
  "updated_at_msg": 4,
  "version": 1
}
```

Hard caps, enforced on every write (`scenecomp.normalize_canon`): the
player entry 400 characters, at most 8 NPCs and 8 places at 200
characters each. A canon that cannot bloat is a canon that can be
injected into every composition without budget anxiety.

Why META and not handler state: the whole point is surviving loads. A
conversation reopened next week must illustrate the same character it
illustrated last week. There is deliberately NO cached copy on the
protocol handler - the meta read costs nothing, and state that exists
once cannot go stale twice.

## 2. Lifecycle

**Build (once).** The first time a conversation illustrates anything,
`_ensure_canon` asks the chat model one dedicated question
(`canon_build_question`): write the ledger, seeded from the STATE
block's `appearance`, the chargen character sheet, and the recent
transcript. The reply is parsed as JSON (leniently - the first `{...}`
span; a model that answers in prose gets its whole reply stored as the
player entry rather than dropped, because an imprecise canon still beats
none). This rides inside the existing "Studying the scene..." heartbeat,
so the one-time cost hides in the latency budget the user already
accepted.

**Inject (every time).** compose_question gains a `canon` argument and
renders it as an AUTHORITATIVE VISUAL CANON block: repeat these
descriptions precisely wherever these people and places appear; they
outrank the transcript's phrasing. Verbatim injection is the point -
the composing model copies instead of re-deriving, and copying is the
one thing small models do reliably.

**Amend (only on conflict).** The happy path after the build is one
meta read and one string comparison - no model call. The staleness
heuristic (`canon_stale`) is deliberately the cheapest one that
matters: the STATE block's `appearance` no longer matches the one the
canon was built against (whitespace- and case-insensitive). When it
fires, `canon_update_question` shows the model the old ledger and the
new narrative and instructs it: **the narrative wins** - rewrite the
entries the story has contradicted, keep every other entry VERBATIM,
return the full corrected ledger. Version bumps; `updated_at_msg`
records where.

What deliberately does NOT trigger an update: an [[IMAGE:]] directive
describing a canon entity differently. The narrator's directive is a
suggested *shot*, already subordinated to game state by docs/13; letting
it rewrite the ledger would hand the least authoritative input the most
authority. If the story truly changed the character, the narrator's own
STATE block will say so, and that is the signal we watch.

## 3. Provenance

Every image sidecar (docs/13) records `canon_version`, so a playtest
can line a picture up against the exact ledger it was composed under -
the same reason sidecars record the final prompt. A conversation that
never built a canon writes no version, which is itself the datum.

## 4. What was left out, on purpose

- **Fuzzy NPC-name contradiction detection.** Deciding whether "the
  innkeeper's red apron" contradicts "Mara: a stout innkeeper in
  grease-stained leathers" is a judgment call, and making it every turn
  costs a model call per reply. The appearance-string trigger is exact,
  free, and covers the entity that matters most.
- **A /canon command.** Letting the player read and edit the ledger is
  clearly right and clearly separate work - it needs client UI thought
  (the C64's 40 columns, the Windows client's dialogs) that has nothing
  to do with the pipeline.
- **Automatic place harvesting from the map.** advmap already knows
  room names; folding its notes into `places` automatically was
  tempting, but map notes are functional ("an iron key on a hook"), not
  visual, and stuffing them into the canon dilutes it. Places enter the
  canon when the model, asked to write the ledger, considers them
  visually settled.

## 5. Testing

`tests/test_canon.py` pins the pure functions: cap enforcement, lenient
parsing (JSON, JSON-in-prose, prose-only fallback, garbage), the
staleness trigger's exactness and its insensitivity to whitespace/case,
and the build/update questions' load-bearing phrases (the seed
appearance present; "narrative wins"; "VERBATIM"; the LEDGER marker the
mock keys on). `tests/test_scenecomp.py` grows the injection cases:
canon block present and verbatim, absent cleanly when there is no
canon, and the authority wording intact.

The mock (`emu/mock_llm.py`) answers canon-ledger questions with a
deterministic JSON ledger, keyed on "VISUAL CANON LEDGER" - a marker
that appears only in build/update questions, and is distinct from the
"AUTHORITATIVE VISUAL CANON" heading injected into compose questions,
so the mock's scene branch cannot shadow it. The e2e path is then:
first /pic builds the ledger, every later /pic composes under it, and
the sidecars carry `canon_version` for the comparison.
