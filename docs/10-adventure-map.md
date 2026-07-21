# Adventure map: a real graph the model maintains — design

Goal: the narrator stops improvising geography. Every move records where
the player went, cardinally *and* logically ("north, through the iron
door"); the proxy keeps the graph; the graph goes back into the prompt
each turn so "how do I get back to the gate" has a correct answer; and
`/map` draws it on the C64.

**Verdict: feasible, and the first two thirds cost ZERO client bytes.**
The recommendation in one paragraph:

- **A separate `[[MAP: ...]]` directive, not an extension of
  `[[STATE]]`,** and **delta-only** — the model emits one line when the
  player moves, never the whole graph. This is the single decision that
  makes the rest work.
- **The proxy owns the graph**, stored as `adv_map` in conversation meta
  beside `adv_state`, re-injected into the system prompt in a compact
  text rendering (not JSON).
- **Ship the C64 side in two steps**: first `/map` as text through
  `_send_canned()` (zero client bytes, ~3s, in the scrollback), then —
  once the graph is proven coherent in play — `/map` as a **proxy-drawn
  hires image on the existing `IMG_*` path** (still zero client bytes,
  ~12s, genuinely pretty). A client-side overlay module is a third
  option that buys speed and panning for ~30 resident bytes plus a whole
  new module; it is not needed to ship.

The largest risk is not any of the above. It is whether Gemma will emit
a movement directive every time the player moves. Nothing in this
document has been tested against the live model.

---

## 1. Notation the model can actually maintain

The instinct is to put a graph in the state block and have the model
re-emit it whole each turn. That is the wrong shape, and the codebase
already contains the evidence.

`[[STATE]]` is re-emitted in full every reply (modes.py:71-80) and the
proxy keeps only the newest copy (protocol.py:1494-1513). Real captured
states run **126-169 characters** (`data/conversations/*.json`, meta
`adv_state`, four adventures). Ask the model to carry a 20-room graph
inside that and you have asked it to transcribe ~1000 characters of
structured data, verbatim, every turn, forever — and given it 20 fresh
chances per turn to corrupt an edge it wrote forty turns ago. That is
the operation LLMs are worst at.

So compare notations on the operation that actually happens, which is
**append one edge**:

| Notation | Append reliability | Cost/room in prompt | Legible? |
|---|---|---|---|
| Nested dict `{"woods":{"n":"gate"}}` | poor — key reordering and dropped nesting on each re-emission | ~45 ch | yes |
| DOT-like `woods -> gate [dir=n]` | good to append, but it is a second syntax the filter must learn, with `[`/`]` that collide with the directive parser | ~30 ch | yes |
| Edge list of arrays `[["woods","gate","n","the iron door"], ...]` | good — flat array, append is unambiguous | ~40 ch | so-so |
| **Line-oriented delta, one edge per directive** | **best — nothing to re-transcribe** | n/a (the model never sees its own storage) | yes |

**Recommendation: split the two jobs.**

*What the model writes* is a delta, once per move, in the existing
double-bracket idiom:

```
[[MAP: from=whispering woods | to=the sunken gate | dir=n | via=through the iron door]]
```

`from` and `via` are optional (`from` defaults to the current room).
That is one short line, ~70 characters, ~20 tokens — a fixed cost per
*move*, not per *room*, and it never grows.

*What the proxy stores* is JSON in `adv_map`, because meta is JSON
already and `set_meta`/`get_meta` (conversation.py:102-110) take any
serialisable value:

```json
{"at":"woods",
 "rooms":{"woods":"The Whispering Woods","gate":"The Sunken Gate"},
 "edges":[["woods","gate","n","through the iron door"]],
 "seen":{"woods":41,"gate":38}}
```

Room ids are **slugs** the proxy derives (lowercase, strip a leading
"the", collapse punctuation and spaces). Models write "The Sunken Gate"
one turn and "Sunken Gate" the next; slugging kills most of that
aliasing for nothing. The first-seen display name is kept for rendering.

*What the model reads* is neither of those — it is a rendered text block
(§3), because prose is what the model is good at reading and JSON in the
prompt invites the model to answer in JSON.

## 2. Where it lives: extend `[[STATE]]` or a new directive?

**A new directive, `[[MAP: ...]]`, sharing all of the STATE machinery.**
Four grounded reasons, in order of weight:

1. **Coupled failure domains.** A malformed state block is now DROPPED
   wholesale (protocol.py:1499-1510) — a hard-won rule, because
   re-injecting bad data teaches the model to produce more of it. Put
   the map inside STATE and one missing quote in the geography throws
   away the player's HP and inventory too, and vice versa. Separate
   directives fail separately: a bad `[[MAP:]]` costs exactly one edge.
2. **`adv_state` is injected somewhere else you have not thought
   about.** `_derive_scene_prompt()` pastes the whole state block into
   the illustration prompt (protocol.py:1010-1018) precisely because it
   is short and about the player. A 40-room graph in there would swamp
   the image prompt with irrelevant geography and make pictures worse.
3. **Delta vs. snapshot.** STATE is a snapshot by design (stats change
   every turn). The map is an accumulation. Mixing them forces the map
   to inherit snapshot semantics, which is exactly the transcription
   burden §1 rejects.
4. **Output tokens.** STATE is ~150 characters today. Keeping it that
   way keeps every reply's tail cheap.

The plumbing is genuinely free. `MusicDirectiveFilter` already parses
`(MUSIC|IMAGE|STATE)` in one regex (music.py:19-36) and holds back
partial matches by prefix (music.py:210-211, 247-264); adding `MAP` is
one alternation and one prefix string. `MAX_HOLD` is 600 (music.py:196),
comfortably above a ~70-character directive. Storage, persistence and
per-turn re-injection are the same three lines the state block uses
(protocol.py:1428-1435, 1511-1513).

`location` stays in `[[STATE]]` and remains authoritative for the status
bar. The proxy cross-checks it against `adv_map.at` (§4).

## 3. Context cost, cap, and what happens at 50 rooms

The map is rendered into the system prompt every turn, so this is the
number that matters. Measured against a concrete rendering (no
tokenizer is installed on this machine — these are character counts
divided by 3.3, which is a reasonable ratio for punctuated English and
should be treated as an estimate, not a measurement):

| Line form | chars | ≈ tokens |
|---|---|---|
| Compact: `meadow "The Sunlit Meadow": w>woods, n>foothills` | 49 | ~15 |
| Labelled: same plus `(along the deer track)` on each exit | 128 | ~39 |

| Rooms | compact-only block | ≈ tokens |
|---|---|---|
| 10 | 490 ch | ~150 |
| 20 | 980 ch | ~300 |
| 40 | 1960 ch | ~600 |
| 50 | 2450 ch | ~740 |

**How fast does this actually grow?** From a real transcript on mlboy
(`1784607137.json`): 42 assistant turns, **5 distinct locations** in the
status line — roughly one new room per eight turns. 50 rooms is a
~400-turn campaign. The cap below is insurance, not a live problem, and
that is worth knowing before anyone over-engineers the pruning.

**Two-tier rendering, and never delete a room.** Deleting rooms is what
breaks "how do I get back to X" — the one thing this feature exists to
do. Degrade instead:

- **Near tier** — the current room and everything within two hops:
  full labelled form, with the logical `via` phrases.
- **Atlas tier** — everything else: compact form, cardinal links only,
  labels dropped.
- **Cap**: 2500 characters. When the atlas tier alone would exceed it,
  drop the *labels already dropped* first (done), then collapse degree-1
  leaves that have not been visited in the last 30 turns into their
  parent line (`gate "The Sunken Gate": s>woods, e>tower (+2 dead ends)`).
  Only if that still overflows does anything actually leave the block,
  and then it is the least-recently-seen leaves, never a room with
  degree ≥ 3 (hubs are what routes are made of) and never a room with a
  known-but-unexplored exit.

The full graph stays in `adv_map` regardless — pruning is a *rendering*
decision, so `/map` on the C64 can still draw everything and the proxy
can still route through rooms the model is not currently being shown.
That asymmetry is the point: the proxy is the one that can afford to
remember.

**The block that is injected** (this is what earns the tokens):

```
MAP - 5 rooms known. You are at woods (The Whispering Woods).
Exits from here: n -> gate (through the iron door), e -> meadow
  (along the deer track), d -> cellar (down the rotted stair).
Not yet explored from here: a boarded door to the west.
woods "The Whispering Woods": n>gate, e>meadow, d>cellar
gate "The Sunken Gate": s>woods, e>tower
meadow "The Sunlit Meadow": w>woods, n>foothills
tower "The Tower Entrance": w>gate
foothills "The Foothills": s>meadow
Routes from here: tower = n then e | foothills = e then n
Emit [[MAP: to=... | dir=... | via=...]] whenever the player moves.
```

~500 characters, ~150 tokens, for a 5-room map.

## 4. Reliability

Models are bad at maintaining structures silently. Three defences, none
of which trust the model.

**Restate, every turn.** The first three lines of the injected block are
the current room, its exits with their logical phrasings, and its known
unexplored exits. That is the prompt discipline that matters: the model
does not have to *find* the current node in a graph, it is told. This
also self-corrects drift, because whatever the model believed last turn
is silently overwritten by the truth this turn.

**Precompute the routes.** `Routes from here: tower = n then e` is a
proxy-side BFS. Models are poor at graph traversal and good at reading a
table, and "how do I get back to X" is the stated requirement. Cap it at
the six nearest rooms and only emit it above four rooms known; it costs
~40 tokens and removes the traversal from the model entirely. A
`/map <room>` command answers the same question directly, with no model
call at all — that is the cheapest correct answer available anywhere in
this design.

**Validate and repair on ingest**, in the spirit of the STATE lesson
(protocol.py:1499-1510) — never store something you would be ashamed to
re-inject:

| Situation | Proxy does |
|---|---|
| Directive does not parse | Drop that directive. Map untouched. Log. |
| `to` is a room not in the map | **Create it.** This is the normal case. |
| `from` given and unknown | Reject the edge; log. (A move must start somewhere real.) |
| `from` omitted | Use `at`. |
| `dir` not in `n s e w ne nw se sw u d in out` | Keep the edge with `dir=None`. It is still routable by its `via` label and still legible. Do not discard geography over a vocabulary slip. |
| No reverse edge | **Insert it automatically** with the opposite cardinal, unless the directive says `oneway=1`. Models forget the way back constantly, and this is the single highest-value repair in the whole feature. |
| Edge contradicts an existing one (`gate n` already goes to `tower`, model now says `crypt`) | **Keep the first, drop the new one, log.** Do not send the model a correction message — the next turn's injected block restates the truth, which is the correction, and a nag per turn buys nothing. |
| `[[STATE]].location` names a room the map does not know, and no `[[MAP:]]` arrived | Leave the graph alone. Add one line to next turn's block: `Note: the state says you are at "The Crypt", which is not on the map. Emit a [[MAP:]] for the move that got you there.` Self-correcting nudge, no bad data stored. |
| More than one `[[MAP:]]` in a reply | Apply them in order. A reply that moves twice ("you flee north and then east") is legitimate. |

Because ingest is delta-based and every delta is validated, the map can
never be replaced wholesale by a bad turn. No previous-good-map snapshot
is needed — that is the payoff for rejecting the snapshot notation.

Two things worth flagging honestly: the directive-stripping filter means
`[[MAP:]]` blocks never reach stored history (`full_response` is built
from filtered chunks, protocol.py:1462-1467), exactly as STATE blocks do
not — verified by scanning every conversation on mlboy, where zero
`[[STATE:` strings survive in message text. So `adv_map` in meta is the
*only* copy. And an adventure started before this ships has no map until
the model emits its first directive, the same accepted precedent as
`adv_state` (HANDOFF.md:167-169).

## 5. Rendering on the C64

### The constraints, verified

- Soft-80 is a 320x200 hires bitmap at `$E000`, 80 columns of 4x8
  glyphs, ASCII cells with bit 7 = reverse, colour matrix at `$CC00`,
  and an ASCII shadow at `$C000` for the test harness (soft80.s:1-27).
- **One colour per 8x8 cell = per two characters** (display.c:114-127,
  144-164). Any map colouring snaps to even columns.
- The chat area is `CHAT_HEIGHT` 19 rows of `TEXT_COLS` 80
  (common.h:29, 77).
- Overlay modules live in a single `$9C00` slot, `$0E00` = 3584 bytes
  max, one at a time (c64-soft80.cfg:16-17). Biggest existing module is
  `OVERLAY5` at `$0AB3` = 2739 bytes (build/c64llm.map).
- **Resident headroom.** `build/c64llm.map` (built 2026-07-21 15:42)
  shows BSS ending at `$999B` against the slot at `$9C00` = **613 bytes
  free** — but that map contains `_diag_init`/`_diag_crumb`, so it is a
  `DIAG=1` build, which HANDOFF.md:92-93 prices at ~200 bytes. The plain
  hayes-80 figure is therefore ~825, matching the brief. I did **not**
  rebuild to confirm (this task is design-only and a build rewrites
  `build/`).
- 8-bit arithmetic only. A `uint32` divide cost this project 233
  resident bytes and a `uint8` divide ~100 (HANDOFF.md:364-368,
  461-471). Any layout maths on the client must avoid `/` and `%`.

### (a) Proxy renders text, streams it like any reply

`_send_canned()` (protocol.py:1274-1281) already streams arbitrary local
text into the chat as a normal reply. A 19x79 ASCII map costs nothing on
the client, lands in the scrollback where it can be scrolled back to,
survives a conversation reload, and can be coloured with the inline
markup that just shipped (docs/08-inline-color.md).

Cost: **0 client bytes.** Wire time at the text pacing constants
(protocol.py:810-812: 60-byte frames, `0.016 + 60*0.0018` per frame) is
~0.124s per line-and-a-bit, so a 19-line map ≈ **2.5-3s**.

One landmine, and it is a real one: **the append path drops leading
spaces.** `chat_append_ascii_char` only stores a space when
`cur_len > 0` (display.c:307-313), so every indented line of ASCII art
loses its indentation. Three ways out, in order of preference:

1. Have the proxy lead each map line with a colour-open marker
   (`0x10|c`). Markers are pushed into `wbuf` and land in the line as
   ordinary cells (display.c:293-305), so the first column is occupied
   and every subsequent space survives. Costs one column, renders as a
   space, zero client bytes. Slightly clever; needs a unit test.
2. Relax the guard in display.c to keep leading spaces on a line that
   has none yet. A handful of bytes, but it changes wrapping behaviour
   for *all* text — I would not do it for this.
3. Draw a map that never needs a leading space (left-flush box art).
   Ugly, but free.

Also: keep map lines ≤ 79 characters. A line of exactly 80 triggers
`commit_line` inside `flush_word` (display.c:249-257) and the following
`\n` commits again, giving a spurious blank row.

### (b) Client-side overlay module draws it

The jukebox is the template: the module hooks messages itself
(`jb_msg`/`jb_parse`, mod_sound.c:304-338), asks the server on open
(mod_sound.c:437) and keeps every static in `OVL5BSS` — zero resident
bytes, zero file bytes. Resident cost for a map module would be a
`menu_local` case plus a three-line opener (main.c:1073-1086, 562-576),
call it **~30 bytes**, plus a sixth overlay slot in `c64-soft80.cfg`,
the Makefile and the d64 build (Makefile:138-161) — all free of resident
bytes; the d64 has room (PRG 20220 bytes plus five modules in a 174848-
byte image).

Transfer is one `MAP_DATA` frame inside `MAX_PAYLOAD` 512
(common.h:145), ~0.6s at the bulk pacing (protocol.py:819-828). It is
the fast option and the only one that can pan around a map bigger than
one screen.

The catch is **layout**, which is a graph-drawing problem and does not
belong on a 6502 with no divide. The fix, if this is ever built: the
proxy sends a *placed display list* — `[room x y len name]` and
`[edge x1 y1 x2 y2 style]` — so the module only draws boxes and straight
runs. Even then it is a new module, a new wire message pair, a new
overlay slot and a lockstep deploy, for a feature whose real risk lives
entirely on the model side.

### (c) Reuse the image pipeline — **recommended for the pretty version**

This one is better than it first looks. In soft-80 the fullscreen image
path writes the bitmap to `$E000` and the matrix to `$CC00`
(main.c:60-65) — **the same memory the soft-80 renderer uses**. A hires
image is not "a picture over the text screen"; it *is* a screen, drawn
server-side at pixel resolution. `send_image_blob(blob, 0, fmt=0)`
already exists (protocol.py:949-970), any key dismisses it and the chat
repaints itself (main.c:1098-1102, 157-169), and the transfer runs with
music playing — the client never stops the tune for `IMG_*` (only for
`SID_BEGIN`, main.c:796, and conversation loads, main.c:436), which
`/pic` has proved in the field.

And the font is not a problem: `tools/make_font.py` holds the 4x8 glyph
table as Python source, so the proxy can rasterise text in the client's
*exact* font and then draw real connecting lines between boxes at pixel
resolution — something no client-side renderer could do with a 95-glyph
ASCII set. Colour is per 8x8 cell, i.e. per two glyph columns, so room
boxes should be laid out on even columns.

Cost: **0 client bytes, 0 wire changes.** Perhaps 200-300 lines of
Python (layout + rasteriser), reusing the palette already in
imaging.py:22-33.

The price is time. A hires blob is fixed at 9000 bytes and the client
requires exactly that many before it will show anything
(`img_got == img_expect`, main.c:940) — no partial transfer, no
"blank rows omitted". At `SID_CHUNK` 256 and the default `wire_baud`
9600 pacing that is 36 frames x (0.01 + 258 x 0.0011979) ≈ **11.5s**,
plus flow-control waits. Same order as an illustration, which the user
tolerates, but it is a lot for a map you want to glance at.

### Verdict on rendering

Ship **(a)** first — it costs nothing, it proves the graph, and a text
map in the scrollback is genuinely useful. Add **(c)** as the `/map`
default once the graph is trusted, keeping `/map t` (or `/map` with the
image service down) on the text path as the fast fallback. Treat **(b)**
as a later want, justified only by panning a map too big for one screen.

## 6. Triggering

**`/map` is the trigger. The model does not get to raise it.** The
`[[IMAGE:]]` precedent is real but points the other way: an image is an
*event* the narrator stages, and it is rate-limited and gated by an
`ask` mode for exactly that reason (protocol.py:1519-1528). A map is a
*reference*. An unprompted 11-second fullscreen takeover in the middle
of a scene is an interruption, not a flourish.

Variants worth having, all proxy-side:

- `/map` — draw it.
- `/map <room>` — "how do I get back to X", answered by BFS, no model
  call, instant.

The F1 menu is where users will look for it, and there is a concrete
obstacle: `_menu_entries()` already returns **exactly 13** entries in
adventure mode (protocol.py:1709-1750) against the client's `MAX_MENU`
13 (modapi.h:68), which is itself bounded by the panel fitting inside
the chat area. So a menu entry means dropping one. `('m', 'Models')`
mid-adventure is the obvious candidate. That is a user decision, not
mine — see §8.

## 7. Build order and tests

1. **Proxy: the graph.** `MAP` in `DIRECTIVE_RE`/`_PREFIXES`
   (music.py:22, 210), a `MapGraph` class, `adv_map` in meta, ingest
   validation per §4, and the rendered block appended in
   `_stream_response` next to the existing state injection
   (protocol.py:1428-1435). Prompt text in `ADVENTURE_PROMPT`
   (modes.py:56-82) — a *short* addition; that prompt is already long.
   New `c64llm_proxy/tests/test_map.py` (precedent: `test_directives.py`)
   covering split-chunk directives, slug aliasing, auto-reverse edges,
   contradiction rejection, unknown `from`, malformed drop, the
   two-tier cap, and BFS routing. Pure Python, no client, shippable
   alone. **Then play a real adventure and read the logs** before
   building anything else.
2. **`/map` and `/map <room>` as text**, via `_send_canned`. Still zero
   client bytes, no lockstep deploy, no reboot. Add the leading-marker
   trick and a test that asserts indentation survives.
3. **`/map` as a hires image.** Layout + rasteriser + `send_image_blob`.
   e2e: mock_llm emits `[[MAP:]]` directives across several turns,
   `/map` is issued, and completion is asserted by polling `_img_shown`
   through `labels.txt` — never screen text (HANDOFF.md:183-184). Run
   tui-80 + tui + hayes + watchdog; add long-rt because a 9000-byte
   transfer sits on the streaming path.
4. **Optional: module #6.** Only with the user's explicit go-ahead.
   Lockstep deploy, new overlay slot, rebuild the d64.

## 8. Open decisions for the user

- **Menu slot.** `/map` in the F1 menu needs one of the current 13
  adventure entries to go. Drop `('m', 'Models')`? Or leave `/map` as a
  typed command only?
- **`/map` default rendering** once both exist: pretty-but-11s image, or
  fast-but-plain text with the image behind `/map p`?
- **Should the map appear in the prep pass** from docs/09? The campaign
  bible is the natural place for an initial 3-5 room skeleton, and it
  would mean the first `/map` is not empty. It also risks the model
  inventing geography it then contradicts in play.
- **Room cap 2500 characters / degrade-don't-delete** — is that the
  right trade, or would you rather the model be shown a hard 20-room
  window and the rest kept proxy-only for routing?
- **`via` phrasing in the injected block**: near tier only (as
  designed), or everywhere until the cap bites?

## 9. Risks, and what I could not verify

- **The model.** Untested. I did not make a single API call. Whether
  Gemma emits `[[MAP:]]` on every move — and only on moves — is the
  whole feature, and the honest answer is that step 1 above exists to
  find out. Prior evidence is mixed: the same model reliably emits
  `[[STATE]]` (four captured adventures all carry one) but has also been
  seen closing it with a single bracket (music.py:38-50), and adventure
  transcripts exist where the status line never appears at all
  (`1784319754.json`: 115 assistant turns, one status line).
- **Silent non-movement.** If the model narrates a move and forgets the
  directive, the map silently diverges. The `location`-mismatch nudge in
  §4 catches it only when `[[STATE]].location` also changes. Some moves
  will be lost. Accepted; the map is a best-effort atlas, not a
  simulation.
- **Byte estimates for option (b)** are inferred from `build/c64llm.map`
  and the existing module wiring, not measured — I did not build, per
  the terms of this task. The ~30-byte figure is the wiring only.
- **Headroom** is likewise read from a `DIAG=1` map (613 bytes) and
  back-calculated to ~825 for the plain build using HANDOFF's ~200-byte
  DIAG figure. Confirm with a clean `make -C c64_client MODE80=1` before
  spending any of it.
- **The 11.5s image transfer** is computed from the pacing constants
  (protocol.py:819-828, 836), not timed on hardware, and `wire_baud`
  defaults to 9600 while the C64U actually runs 19200 — so the real
  figure may be better. Nobody has measured a `/pic` end-to-end for
  comparison in this repo.
- **The leading-space behaviour** in `chat_append_ascii_char` is read
  from source (display.c:307-313), not observed on screen.
- **Token counts are estimates.** No tokenizer is installed on this
  machine; every figure in §3 is characters ÷ 3.3.
- **Interaction with the open NMI/ACIA race** (HANDOFF.md:236-286): a
  9000-byte map image is another bulk transfer, i.e. more exposure to
  the transposed-byte bug. It corrupts a picture harmlessly, unlike a
  SID payload — but do not add `/map`-as-image to the field-test load
  while that bug is still being characterised, or you will muddy the
  evidence.
