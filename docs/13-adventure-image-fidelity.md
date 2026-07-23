# 13 — Adventure illustration fidelity: anchored prompts, steerable /pic, saved prompts

Status: DESIGN / HANDOFF — investigated 2026-07-23, not yet implemented.
This document is written so that an implementer with no prior context can
carry it out. Every claim below was verified against the code at commit
9a584e7 and against live playtest data copied from mlboy
(`data/mlboy_snapshot/`, see §2).

## 0. The problem

Three complaints, one root cause:

1. **Wrong cast.** Generated illustrations routinely contain monsters and
   people who are not in the scene, and when the player character appears
   it does not match their established description. Field examples from
   the playtest data (§2): "A lone traveler stands before a crumbling
   stone archway" (the character was a Kobold), "a person stands amidst
   writhing shadows" (nobody had been described), "A swarm of pale,
   many-legged creatures" (fine — those were actually attacking).
2. **`/pic <text>` is taken literally.** Whatever the player types after
   `/pic` is sent to the image backend verbatim (plus the style prefix).
   The desired behaviour: `/pic <text>` is an *instruction to the
   narrator's illustrator* — "give me an illustration of the footprints
   in the sand" — and the chat model must first read the recent
   transcript and game state, then write the actual image prompt.
3. **Prompts are not saved with the images.** The only record of a
   prompt is a 200-char truncation in the conversation's meta
   (`protocol.py:1355`). The PNG and blob land in
   `data/images/<conv_id>/<epoch>.{png,blob}` with nothing beside them
   saying what produced them, which backend ran, or what the player
   asked for.

The fix leverages state we now track and did not have when illustration
shipped: `adv_state` (the `[[STATE:]]` JSON block with `location`,
`appearance`, `companions`, `inventory`), the adventure map
(`adv_map`, with a per-room `note`), and the character sheet from
character generation (`AdventureMode.background`).

## 1. Current pipeline — verified, with line references

All line numbers are `c64llm_proxy/src/` at commit 9a584e7.

There are **four ways** an illustration prompt comes to exist, and only
one of them is anchored to game state:

| # | Trigger | Prompt source | Anchored? |
|---|---------|--------------|-----------|
| 1 | `/pic <text>` | player text, **verbatim** | no |
| 2 | `/pic` with a parked suggestion (`[images].mode = "ask"`) | the model's `[[IMAGE: ...]]` directive text, **verbatim** | no |
| 3 | auto mode: `[[IMAGE: ...]]` in a reply | directive text, **verbatim** | no |
| 4 | bare `/pic`, nothing parked | `_derive_scene_prompt()` — a utility LLM call | **yes** |

Flow details:

- `protocol.py:563-576` — `/pic` dispatch. `prompt = arg or
  self.images.pending_prompt`; a non-empty result skips derivation
  entirely (`_illustrate`, `protocol.py:1320`: "Priority: explicit
  description > pending suggestion > model-derived scene").
- `protocol.py:1764-1773` — end-of-reply directive handling. Auto mode
  calls `_generate_and_send_image(mfilter.images[0])` directly; ask mode
  parks the directive text in `self.images.pending_prompt`.
- `protocol.py:1099-1138` — `_derive_scene_prompt()`. The good path.
  Already reads the last 12 messages, the last 3 stored image prompts,
  and — since commit 7de3530 — the `adv_state` block, with an explicit
  "Depict ONLY the player character ... Do NOT add other people or
  creatures" instruction. **This is the model for what all paths should
  do.** Note what it still lacks: the map room note, the character
  sheet (race/class — `adv_state.appearance` is one short phrase like
  "a traveler in tattered linen robes" and does not carry "Kobold"),
  and any way for the player to steer it.
- `protocol.py:1341-1364` — `_generate_and_send_image()` makes a
  caption (`_make_caption`, second utility call), calls
  `images.generate_blob(prompt, conv_id, caption)`, then appends
  `{'stem', 'prompt': prompt[:200], 'caption': caption[:100],
  'at_msg'}` to conversation meta key `images`.
- `images.py:95-118` — `_generate_sync()` prepends
  `self.style_prefix` (default at `images.py:29`) and calls the
  backend; writes `<data>/images/<conv_id>/<epoch>.png` and `.blob`.
  **The final prompt string exists only here, transiently.**
- `images.py:64-76` — `prompt_snippet()`, the `[[IMAGE:]]` instruction
  appended to the system prompt (attached in `_attach_snippets`,
  `protocol.py:806-823`, for both adventure and roleplay). It asks for
  consistency of appearance but says nothing about *who may appear*.
- `imagegen.py` — backends (gemini/openai/comfyui/fixture). No changes
  needed there; `FixtureBackend` + the `C64LLM_IMG_FIXTURE` env hook is
  how tests generate without a network.

Prompt storage today: conversation meta only, truncated to 200 chars.
The full derived prompt (often 300-400 chars — `_derive_scene_prompt`
caps at 400) and the final backend prompt (style prefix + scene) are
never persisted anywhere.

## 2. Field data

`data/mlboy_snapshot/` is a full copy (2026-07-23, minus `sids/`) of
mlboy's live `~/c64llm_proxy/data/` — conversations, images, and
adventure saves from real playtesting. Refresh it any time with:

    rsync -a --exclude sids mlboy:c64llm_proxy/data/ data/mlboy_snapshot/

Useful facts found there:

- 46 image files use the pre-folder flat stem `conv_epoch.{png,blob}`;
  the two newest conversations use the current `conv/epoch` folder
  layout. `ImageService.blob_path` (`images.py:59-62`) resolves both.
  Sidecar writing (§4) applies to *new* generations only; do not
  migrate old files.
- Conversation JSON: top-level keys are `id, title, auto_titled,
  created_at, updated_at, meta, chat` (messages live under **`chat`**,
  not `messages`).
- `meta.adv_state` in recent games really does carry a stable
  `appearance` phrase and a `companions` list, e.g.
  `"appearance":"a traveler in tattered linen robes with slimy hands",
  "companions":[]` — the anchoring data is reliably present.
- An adventure save (`adventures/unnamed-world-1784753056.json`) shows
  why the character sheet matters for images: `character` reads "The
  player character is ?, a Kobold Femboy maid. ... carrying: gun,
  french maid outfit, cigarettes." None of that visual identity can be
  recovered from `adv_state.appearance` alone, and none of it reached
  any image prompt.

## 3. Design

### 3.1 One composition path for every trigger

Replace the "verbatim wins" priority with: **every** illustration
prompt is written by the chat model in a single composition step; the
trigger only changes what steering that step receives.

Extend `_derive_scene_prompt` to:

```python
async def _derive_scene_prompt(self, instructions: str = '',
                               directive: str = '') -> str:
```

- `instructions` — the player's `/pic <text>`, treated as a request to
  the illustrator ("give me an illustration of the footprints in the
  sand"), not as the prompt.
- `directive` — the narrator's `[[IMAGE: ...]]` text, treated as the
  narrator's *suggestion* for the shot, still subject to the cast
  rules.

Callers after the change:

| Trigger | Call |
|---|---|
| `/pic <text>` | `_derive_scene_prompt(instructions=arg)` |
| `/pic` with parked suggestion | `_derive_scene_prompt(directive=pending)` |
| auto-mode directive | `_derive_scene_prompt(directive=mfilter.images[0])` |
| bare `/pic` | `_derive_scene_prompt()` (unchanged) |

This means auto mode and `/pic <text>` each gain one utility LLM
round-trip (a few seconds on mlboy). Both already run behind heartbeats
(`_illustrate` shows "Studying the scene..."; move the heartbeat so it
covers every path, not just the bare one). Auto mode is rate-limited to
one image per 240 s (`images.py:23`), so the added call is negligible.

The `Illustrating: <prompt>` echo (`protocol.py:1338`) stays — it is
now the player's confirmation of what the composition step decided,
which is exactly what you want visible when debugging fidelity.

### 3.2 The composition prompt

Rewrite the utility question inside `_derive_scene_prompt` along these
lines (final wording is the implementer's, but every numbered element
must be present):

1. Transcript: last 12 messages, 500 chars each (as today).
2. Prior illustrations: last 3 prompts from meta `images` (as today).
3. `adv_state`, when present (as today), **plus two new context
   sources**:
   - the current map room's display name and its `note`, from meta
     `adv_map` (`m['rooms'][m['at']]`, fields `name` and `note` —
     see `advmap.py:255-271`). The note is where props like "an iron
     key on a hook" tend to live.
   - the character's visual identity: `self.mode.background` includes
     the chargen `describe()` block ("The player character is X, a
     Kobold Femboy maid ..."). Pass it through when
     `getattr(self.mode, 'background', '')` is non-empty. Extract just
     the character portion if the bible makes it long — see
     `_background()` at `protocol.py:2089`; character and bible are
     joined there, and it may be worth keeping a separate
     `mode.character` attribute to avoid re-parsing. (Small plumbing
     change; do it — string-splitting the joined blob is worse.)
4. Cast rules, strengthened from the existing wording: the scene shows
   the current location; the player character may appear ONLY if the
   instructions ask for it or the current scene is about them, and if
   shown must match the sheet + appearance phrase; companions from the
   state list may appear; **no other people, creatures, or monsters
   unless the transcript's current scene explicitly puts them there**;
   props and scenery named in the scene or room note (keys, doors,
   altars) should be included.
5. When `instructions` is non-empty: "The player asked the illustrator
   for: <instructions>. Honor this request — find what it refers to in
   the transcript and describe THAT." Instructions outrank the default
   framing (they may name a detail, not the whole room).
6. When `directive` is non-empty: "The narrator suggested this shot:
   <directive>. Use it as the basis, corrected to obey the rules
   above."
7. Output contract: ONE sentence (allow two for busy scenes), no
   preamble — same as today, `limit=400`.

### 3.3 The cast roster in the final backend prompt

Image models follow "contains only X, Y" far better than "do not
include Z" — negations are weak in image-generation prompting. So in
addition to the composition rules, have the composition step end its
sentence with an explicit positive roster when people are present, e.g.
"... — the only figure present is the kobold in a maid outfit", or, for
an empty room, "— an empty, unpeopled scene." Bake this requirement
into the composition prompt (element 4 above) rather than
post-processing the string; one writer, one voice.

Leave `DEFAULT_STYLE_PREFIX` (`images.py:29`) alone. It is style-only
today and users can override it; putting cast rules there would break
ComfyUI users who set `style_prefix = ""`.

### 3.4 `[[IMAGE:]]` snippet touch-up

In `images.py prompt_snippet()`, add one sentence telling the narrator
its directive is a *suggestion for the shot* and should name only who
is actually present. This is cheap compliance help; the composition
step remains the enforcement point.

## 4. Saving prompts with the images

Write a JSON sidecar next to every generated pair:
`data/images/<conv_id>/<epoch>.json` —

```json
{
  "final_prompt":  "<style prefix + scene, exactly what the backend got>",
  "scene":         "<the composed scene sentence, pre-prefix>",
  "instructions":  "<player /pic text, or empty>",
  "directive":     "<[[IMAGE:]] text this came from, or empty>",
  "caption":       "<the burned-in caption>",
  "backend":       "gemini/gemini-2.5-flash-image",
  "conv_id":       "1784787911",
  "at_msg":        14,
  "time":          1784787941
}
```

Implementation: extend `ImageService.generate_blob(prompt, conv_id,
caption)` with a `meta: dict = None` parameter, passed through to
`_generate_sync`, which merges in what only it knows (`final_prompt`,
backend name — `self.backend.name` plus model where the backend has
one, timestamp = the same `int(time.time())` used for the stem) and
writes `<stem>.json` beside the PNG. Write it **after** the backend
call succeeds, best-effort (an `OSError` here must not lose a paid-for
image — same policy as `_log_usage`, `imagegen.py:114`).
`_generate_and_send_image` builds the meta dict; give it `instructions`
and `directive` parameters so the trigger information reaches it (its
callers know which path fired).

Keep the conversation-meta record as is (it drives `/pics` and
consistency lookback); the 200-char truncation there is fine now that
the sidecar holds the full text.

## 5. Implementation order

Each step compiles and passes tests on its own; land in this order.

1. **Plumbing** — `mode.character` attribute set in `_start_adventure`
   callers (`protocol.py:2179`, `2211`; `_background()` callers carry
   both strings already). No behaviour change.
2. **Sidecars** (§4) — `images.py` + `_generate_and_send_image`
   signature. Independent of everything else; ship first, it starts
   collecting data for evaluating the rest.
3. **Composition** (§3.1-3.3) — `_derive_scene_prompt` extension, the
   four call sites, heartbeat coverage for the new slow paths.
4. **Snippet touch-up** (§3.4).
5. Update `/help` text (`protocol.py:498`) — `/pic [request|n]`, and
   the `_list_pics` hint string ("`/pic <desc> makes one`" →
   "`/pic makes one`").

## 6. Tests

Repo convention: script-style test files under `c64llm_proxy/tests/`,
plain `check(name, got, want)` + failure list, run directly with
`python3 tests/test_X.py` (see `tests/test_directives.py` header;
Pillow-free, network-free, model-free). Follow it.

### 6.1 New: `tests/test_scenecomp.py`

`_derive_scene_prompt` currently builds its question inline. **Extract
the question-building into a pure function** so it is testable without
an event loop or model — e.g. in a new `scenecomp.py` or as a module
function in `protocol.py`'s style:

```python
def compose_question(convo, priors, adv_state, room, character,
                     instructions='', directive='') -> str
```

Then assert, minimum set:

- adv_state present → the question contains the appearance phrase, the
  companions rule, and the no-extra-creatures rule.
- room with a note ("an iron key hangs on a hook") → note text present.
- `character` ("... a Kobold Femboy maid ...") → present.
- `instructions='the footprints in the sand'` → the steering element
  present, and phrased as outranking the default (order or explicit
  wording).
- `directive='A dragon looms'` → suggestion element present.
- **all-empty degradation**: no adv_state, no room, no character (this
  is roleplay mode — `_attach_snippets` gives roleplay the images
  snippet too, `protocol.py:875`) → question is still well-formed,
  contains transcript + output contract, no leftover placeholder text.

### 6.2 Extend `tests/test_imagegen.py`: sidecars

Using `FixtureBackend` and a temp data dir (the file has this pattern
already — `test_make_backend`, `test_usage_log`):

- `generate_blob(..., meta={...})` → `<stem>.json` exists;
  `final_prompt` equals `style_prefix + scene`; `backend` names the
  fixture; stem of the json matches the png/blob stem.
- meta omitted → still writes a sidecar with the fields it can fill
  (final_prompt, backend, time). No crash.
- unwritable directory for the json only → image still returned
  (best-effort check; monkeypatch `Path.write_text` to raise).

### 6.3 Call-site behaviour (no-model async tests)

If a protocol-level test exists cheaply (the handler can be constructed
with stub conv_manager/images — check how `test_advsetup.py` fakes
things; if it cannot be done without dragging in the wire, settle for
6.1 coverage plus manual validation):

- `/pic sand footprints` no longer reaches `generate_blob` verbatim:
  assert the backend-received prompt came from the (stubbed)
  composition call.
- ask-mode parked directive → composition receives it as `directive`.

### 6.4 End-to-end and manual

- `make test-all` must stay green (it exercises the image path via
  `C64LLM_IMG_FIXTURE`, `emu/test_e2e.py:268`).
- Manual on mlboy (deploy per HANDOFF/README rsync flow): start an
  adventure with a distinctive generated character (the Kobold save in
  `data/mlboy_snapshot/adventures/` is ideal), then:
  1. bare `/pic` in a room with a noted prop → prop present, no
     invented figures;
  2. `/pic show me my character examining the door` → character
     matches race/class/appearance;
  3. wait for an auto/ask `[[IMAGE:]]` → cast obeys state;
  4. confirm `~/c64llm_proxy/data/images/<conv>/<epoch>.json` sidecars
     appear and `final_prompt` matches the "Illustrating:" echo.

## 7. Out of scope / risks

- **No migration** of old flat-stem images or backfilled sidecars.
- **Caption merging**: composition and caption remain two utility
  calls. Merging them into one JSON-returning call would save a
  round-trip but adds a parse-failure mode; not worth it now.
- **Latency**: `/pic <text>` and auto-directive gain one utility call.
  Acceptable behind the existing heartbeats; do not add a config knob
  for it.
- **Risk — small local models** may over-comply and produce sterile
  empty-room prompts, or ignore the roster rule entirely. The sidecars
  (step 2 first!) are the instrument for judging this: compare
  `directive`/`instructions` against `scene` across a playtest session
  before tuning wording further.
- The consistency lookback still reads meta `images` prompts
  (truncated at 200). With sidecars in place a later improvement could
  read the full `scene` strings from the sidecars instead; not
  required now.
