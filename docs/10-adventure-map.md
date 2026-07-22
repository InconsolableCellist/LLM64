# Adventure map: the proxy keeps a real graph — implementation spec

Goal, in the user's words: the virtual DM gets **a fully realized
internal state of the world map**, so the story stays coherent and
backtracking feels like a real text adventure; and **the player can view
that map**, either drawn and dismissed like a picture or laid into the
scrollback as ASCII.

This document is a build spec, not a decision memo. The decisions are
made (§10 records what was rejected and why). Follow it top to bottom.

**Everything in phases 1-3 costs ZERO client bytes and needs no lockstep
deploy, no d64 rebuild, and no reboot.** It is all proxy Python.

---

## 0. The one idea that makes this safe

An earlier draft of this design hung the whole feature on a new
`[[MAP: ...]]` directive that the model had to emit on every move, and
then admitted in its risk section that nothing had been tested and that
this was "the whole feature". That is a bad bet, and it is an
unnecessary one.

**The map is built from three signals, in priority order. Only the third
is new, and the feature works without it.**

| # | Signal | Where it comes from | Reliability |
|---|--------|--------------------|-------------|
| 1 | **`[[STATE]].location` changed** | already shipping; parsed, validated and stored every turn (`protocol.py :: _stream_response`, search `mfilter.states`) | **high — measured, see §0.1** |
| 2 | **The player's typed command** | `handle_chat_request`'s `text`, e.g. "n", "go north" | **low for this user** — see below |
| 3 | **`[[MAP: ...]]` directive** | new; the model volunteers direction and prose | **unknown, untested** |

Signal 1 alone gives you a correct **topological** map: which rooms
exist, which are adjacent, and how to get from here to there. That is
already most of what the user asked for — coherence and backtracking.
Signals 2 and 3 add **cardinal direction and flavour**, which upgrade it
to a *cartographic* map that can be drawn on a grid.

So: a move is recorded when `location` changes, whether or not any
directive arrived. `dir` is simply unknown (`null`) on those edges, and
every downstream consumer must handle `dir=null` gracefully. Build it in
that order and phase 1 cannot fail for reasons outside your control.

### 0.1 The measurement that justifies this (2026-07-22)

Taken from the **live** transcripts on mlboy
(`~/c64llm_proxy/data/conversations`). The local `data/conversations`
mirror in this repo is stale and shows fewer turns — do not measure from
it. The `[[STATE]]` block landed in `ADVENTURE_PROMPT` on 2026-07-20
(commit `7de3530`), so only adventures created after that date are
evidence about compliance; the three older ones with no state block
predate the feature and are not counterexamples.

Across the **seven** post-feature adventures — **85 assistant turns
total — the status line appeared on 85 of 85 replies.** Every one of
those conversations also has `adv_state` stored. Per-turn compliance
with the state instructions is, on the evidence available, total.

The load-bearing detail is in the longest one (`1784607137`, 42 turns,
5 distinct locations, 6 transitions):

```
19x The Whispering Woods
10x The Void
 6x The Sunlit Meadow
 1x The Foothills
 2x The Sunlit Meadow      <- returned
 1x The Foothills          <- returned
 3x The Tower Entrance
```

**The player backtracked, and the location string was byte-identical on
every revisit.** That is precisely the operation this feature exists to
support, and it means slug-keyed identity works on real data with the
fuzzy matcher never firing. Room growth was 5 rooms over 42 turns,
confirming the ~1-room-per-8-turns figure used in §4.1.

Two caveats a implementer must carry:

- **This measures the human-visible status line, not
  `[[STATE]].location`.** They are separate fields the model emits
  separately, and the STATE block is stripped from stored text so its
  history cannot be recovered. The two are near-certainly correlated —
  and every one of these conversations stored an `adv_state` — but
  per-turn `location` stability is inferred, not observed. **Confirming
  it is the first job of the phase-1 playtest**: log `location` every
  turn and read the log.
- **Two decoration hazards are already visible.** One conversation
  emitted `Location: The Sunken Sanctum` (a `Location: ` prefix), and
  `The Void` appears as a dream/death pseudo-room. Neither breaks
  anything — they are stable within a conversation, so they produce an
  ugly room name rather than a split node — but strip a leading
  `location:\s*` in `slug()` anyway. It is two characters of regex.

**On signal 2, do not over-invest.** Real transcripts on this machine
show free-form play, not compass play:

```
1784319754.json — 114 user messages: "eat the cell", "scream for the
guards", "sniff deeper", "check the cell door, maybe the guard was
stupid and left it unlocked". Approximately none of the form "go north".
```

The command parser is ~15 lines and worth having because it is free, but
it must never be load-bearing. It fires only on a message that is
*entirely* a movement command (`^\s*(go\s+)?(n|north|s|south|...)\s*$`),
and it only fills a `dir` that would otherwise be null.

---

## 1. Data model

### 1.1 Where it lives

`adv_map`, in conversation meta, beside `adv_state`.
`conv_manager.set_meta('adv_map', ...)` / `get_meta('adv_map')` take any
JSON-serialisable value (`conversation.py :: set_meta`). Meta is saved
and reloaded wholesale, so a loaded conversation gets its map back for
free — `handle_load_conversation` already rebuilds `AdventureMode` from
meta and needs no change.

Directives never survive into stored message text (the filter strips
them before `full_response` is assembled), exactly as `[[STATE]]` does
not. **`adv_map` in meta is the only copy.**

### 1.2 The schema

```json
{
  "at": "whispering-woods",
  "turn": 57,
  "rooms": {
    "whispering-woods": {
      "num": 1,
      "name": "The Whispering Woods",
      "seen": 57,
      "visited": true,
      "exits": ["n", "e", "d", "w"],
      "note": "a boarded door to the west"
    }
  },
  "edges": [
    {"a": "whispering-woods", "b": "sunken-gate",
     "dir": "n", "via": "through the iron door", "oneway": false}
  ]
}
```

Field rules — a less-capable implementer should treat these as law:

- **`num`** is assigned at room creation from a monotonically increasing
  counter and **never changes**. It is what the player sees on `/map`
  and types into `/map 4`. Stability matters more than tidiness: do not
  renumber, ever, not even after a merge.
- **`seen`** is the turn number the room was last entered. `turn` is a
  counter the ingest bumps once per assistant reply. Used only for
  ordering the render when the budget bites.
- **`visited`** is false for rooms seeded from the prep notes (§7) that
  the player has not actually entered. `/map` shows those in
  parentheses; routing may pass through them.
- **`exits`** is what the model *says* leads out of the room (§3), which
  is a superset of the edges you know. `exits` minus known edge
  directions = "unexplored from here", which is the single line that
  makes the map feel like a real text adventure. Optional; absent is
  fine.
- **`dir`** is one of `n s e w ne nw se sw u d in out` **or `null`**.
  `null` is normal, not an error (§0). It is stored from A's point of
  view; B's view is the opposite (`OPPOSITE` table).
- **`edges` is a list, and an edge is stored once.** Do not store both
  directions. Lookups walk the list from both ends. At the scale this
  reaches (§4) a linear scan is correct and a index is premature.
- **`oneway`** suppresses the reverse traversal. Default false.

### 1.3 Slugs

Room identity is a slug derived by the proxy, never by the model:

```python
def slug(name):
    s = name.strip().lower()
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = re.sub(r"[^a-z0-9]+", '-', s).strip('-')
    return s[:40]
```

Models write "The Sunken Gate" one turn and "Sunken Gate" the next;
slugging kills most of that for nothing.

**One fuzzy repair, and only one.** If a slug is unknown, compare its
token set (minus `of in at the a an`) against every known room's. An
exact token-set match is the same room ("sunken-gate-ruins" vs
"ruins-of-the-sunken-gate"). Anything less than an exact match after
that normalisation is a **new room** — resist the urge to add edit
distance or substring matching, which merges "North Tower" into "Tower"
and silently destroys the geography this feature exists to preserve.

### 1.4 The module

New file `c64llm_proxy/src/advmap.py`, following the precedent set by
`advsetup.py`: **a pure module — no network, no model, no conversation,
no asyncio.** It takes and returns plain dicts. That is what makes the
interesting behaviour unit-testable without a model or an emulator, and
the interesting behaviour here is ingest and layout, not plumbing.

Public surface, and nothing more:

```python
new_map()                                  -> dict
ingest(m, *, location=None, directives=(), player_text='') -> list[str]
prompt_block(m, budget=2000)               -> str
render_ascii(m, width=78, rows=None)       -> list[str]   # no colour tags
route(m, dest_slug)                        -> list[str] | None
find_room(m, query)                        -> slug | None # "4" or a name
seed_from_notes(m, text)                   -> int         # phase 3
```

`ingest` returns a list of human-readable log lines (what it accepted,
what it rejected and why). The caller logs them. This keeps `advmap.py`
free of the logger and makes rejection assertable in tests.

---

## 2. Ingest — the whole of phase 1

Called **once per assistant reply, after the reply completes**, from
`_stream_response`, immediately after the existing `adv_state` block is
persisted. Both signals are in hand by then: the filter has collected
every directive from the whole stream, and the state block has been
parsed. **Ordering within the reply is therefore irrelevant** — the
model may put `[[MAP:]]` anywhere.

```python
# in _stream_response, after the adv_state set_meta/save block
if self.mode.name == 'adventure' and mfilter:
    # Read the state back from meta rather than reusing the local
    # `state`: that name is bound inside the `if mfilter.states:`
    # branch above and does NOT exist on a turn with no state block.
    # Meta is also the value that actually survived validation.
    loc = None
    try:
        loc = (json.loads(
            self.conv_manager.get_meta('adv_state') or '{}')
            or {}).get('location')
    except (ValueError, TypeError):
        pass
    m = self.conv_manager.get_meta('adv_map') or advmap.new_map()
    for line in advmap.ingest(m, location=loc,
                              directives=mfilter.maps,
                              player_text=self._last_user_text):
        self.logger.info("map: %s", line)
    self.conv_manager.set_meta('adv_map', m)
    self.conv_manager.save()
```

Note the consequence of reading from meta: on a turn whose state block
was malformed and therefore dropped, `location` is last turn's value, so
ingest correctly sees no move. That is the right behaviour and it falls
out for free.

`self._last_user_text` is set in `handle_chat_request` right before the
stream task is created. (It is the post-dice-expansion `text`; that is
fine, dice macros never look like movement.)

### 2.1 The algorithm

```
turn += 1

dest = to= from the first directive, else location, else None
dest = slug(dest); if dest is empty -> nothing to do, return

if dest not in rooms and no fuzzy match:
    create it (num = next, visited = True, seen = turn)

src  = from= from the first directive, else m['at']

if src and src != dest:
    dir = directive dir=, else parsed from player_text, else None
    add_edge(src, dest, dir, via, oneway)

at = dest; rooms[dest].seen = turn; rooms[dest].visited = True

for each directive:  apply exits= / note= to rooms[at]
```

Apply directives **in order**; a reply that moves twice ("you flee north
and then east") is legitimate, and each subsequent directive's `from`
defaults to the `at` produced by the previous one.

### 2.2 Validation table

Never store something you would be ashamed to re-inject. This is the
same rule that governs `[[STATE]]`, where a malformed block is dropped
wholesale rather than fed back to the model.

| Situation | Do this |
|---|---|
| Directive does not parse | Drop that directive. Map untouched. Log. |
| `to` names an unknown room | **Create it.** This is the normal case. |
| `from` given and unknown | Reject that edge, log, but still move `at` to `to`. A move must start somewhere real; the destination is still true. |
| `from` omitted | Use `at`. |
| `dir` not in the vocabulary | Keep the edge with `dir=null`. Do not discard geography over a vocabulary slip. |
| `dir` given but `location` did not change | Store `exits`/`note` only. No edge. The model is describing, not moving. |
| `location` changed, no directive | **Add the edge with `dir=null`.** This is the load-bearing case (§0). |
| Edge already exists between these two | Upgrade in place: fill a null `dir`/`via`, never overwrite a non-null one. |
| A *different* room already lies in `dir` from `src` | **Keep the first, drop the new `dir` (store the edge with `dir=null`), log.** Do not send the model a correction — next turn's injected block restates the truth, and a nag every turn buys nothing. |
| No reverse edge | Nothing to do — edges are undirected in storage (§1.2). This is why. |
| `location` is missing or empty | Leave the map alone. |
| More than one `[[MAP:]]` | Apply in order (§2.1). |

Because ingest is incremental and every increment is validated, **the
map can never be replaced wholesale by one bad turn**, so no
previous-good snapshot is needed.

---

## 3. The `[[MAP:]]` directive

### 3.1 Syntax

```
[[MAP: dir=n | via=through the iron door | exits=n,e,d,w | note=a boarded door west]]
```

Every field optional. Also accepted: `to=`, `from=`, `oneway=1`.
Parse as `|`-separated `key=value`, case-insensitive keys, values
trimmed. Unknown keys ignored.

**Keep it short on purpose.** `to=` is deliberately not required,
because the destination comes from `[[STATE]].location` which the model
already emits reliably. Every field the model does not have to write is
a field it cannot get wrong, and short directives are the ones models
actually comply with.

### 3.2 Wiring it into the filter

Three edits in `c64llm_proxy/src/music.py`, all mechanical:

1. `DIRECTIVE_RE`: `(MUSIC|IMAGE|STATE)` -> `(MUSIC|IMAGE|STATE|MAP)` in
   the canonical (double-bracket) alternative, **and**
   `(MUSIC|IMAGE)` -> `(MUSIC|IMAGE|MAP)` in the single-bracket
   fallback. MAP is safe in the single-bracket form because its value
   contains no `]` — unlike STATE, whose JSON does. Models copy the
   status line's single-bracket shape; this catches that.
2. `MusicDirectiveFilter._PREFIXES`: add `"[[MAP:"` and `"[MAP:"`.
3. `MusicDirectiveFilter.__init__`: add `self.maps = []`, and in
   `_extract`'s `grab()` add the `MAP` branch beside MUSIC/STATE.

Add **no new regex groups** — extend the existing alternations only.
`grab()` dispatches on group indices 1/3/5 and adding a group renumbers
them, which is exactly the kind of silent breakage that costs an
afternoon. `MAX_HOLD` is 600, comfortably above any directive; do not
change it.

### 3.3 Prompt text

New `advmap.prompt_snippet()`, attached in
`protocol.py :: _attach_snippets` alongside music/images/colour. Keep it
short — `ADVENTURE_PROMPT` is already long, and every instruction
competes with the others for compliance:

```
Map: when the player MOVES to a different place, add
[[MAP: dir=n | via=through the iron door | exits=n,e,w]] to that reply -
dir is the compass direction they went (n s e w ne nw se sw u d in out),
via is a short phrase for how, exits lists every way out of the place
they have ARRIVED in. Omit any field you are unsure of. The player never
sees this. Keep the "location" in your STATE block exactly consistent
with the place you just described - that is what the map is keyed on.
```

That last sentence earns its tokens: it reinforces the signal the whole
design depends on.

---

## 4. What goes back into the prompt

Rendered by `advmap.prompt_block()` and appended in `_stream_response`
**after** the `adv_state` append. The ordering rule from
docs/09-adventure-setup.md §4b is not optional: llama.cpp prefix-caches
the prompt, so **everything that changes must come after everything that
does not**, or you invalidate the cache from that point on and pay full
prompt-eval every turn.

Order: stable system prompt + bible + sheet, then `adv_state`, then the
map block, then the music-stale nudge.

**Display names only. The model never sees a slug** — slugs are internal
identity, and showing the model two names for one room invites it to use
the wrong one.

```
MAP - 6 places known. You are at: The Whispering Woods.
Exits from here: n -> The Sunken Gate (through the iron door);
e -> The Sunlit Meadow (along the deer track); d -> The Rotted Cellar.
Unexplored from here: w.
Known places: The Sunken Gate: s>Whispering Woods, e>Tower Entrance |
The Sunlit Meadow: w>Whispering Woods, n>Foothills |
The Tower Entrance: w>Sunken Gate | The Foothills: s>Sunlit Meadow |
The Rotted Cellar: u>Whispering Woods
Routes from here: The Foothills = e then n; The Tower Entrance = n then e
```

- Lines 1-3 are the **restatement**, and they are the prompt discipline
  that actually matters: the model never has to *find* the current node
  in a graph, it is told, with its exits and their prose. Whatever it
  believed last turn is silently overwritten by the truth this turn.
- "Routes from here" is a proxy-side BFS to the six nearest rooms,
  emitted only above four rooms known. Models are poor at graph
  traversal and good at reading a table, and "how do I get back to the
  gate" is the stated requirement. ~40 tokens to remove traversal from
  the model entirely.
- Edges with `dir=null` render as `?>Room Name`. Do not hide them.

### 4.1 The budget

Rooms grow slowly — **measured**, see §0.1: a real 42-reply transcript
shows five distinct locations, roughly one new room per eight turns, so
50 rooms is a ~400-turn campaign. The cap is insurance, not a live
problem. Do not over-engineer it.

The rule, complete:

1. Always emit lines 1-3 (current room, exits, unexplored) and the
   routes line. These are never dropped.
2. Then emit "Known places" entries, **most-recently-seen first**,
   until the whole block reaches `budget` characters (default 2000).
3. If any were dropped, append `(+N older places not shown)`.

**Never delete a room from `adv_map`** — pruning is a *rendering*
decision. Deleting is what breaks "how do I get back to X", the one
thing this feature exists to do. The proxy keeps everything and can
still route through rooms the model is not currently being shown; `/map`
still draws them all. That asymmetry is the point: the proxy is the one
that can afford to remember.

(Character counts here are just that. No tokenizer is installed on this
machine; divide by ~3.3 for a rough token estimate and do not quote the
result as measured.)

---

## 5. `/map` on the C64 — text (phase 2)

Streamed with `_send_canned()`, which puts it in the scrollback where it
can be scrolled back to, survives a conversation reload, and costs
nothing. ~19 lines at the text pacing (60-byte frames,
`0.016 + 60*0.0018` per frame) is roughly **2.5-3 seconds**.

### 5.1 Output format — the legend is the truth, the picture is ornament

Build it in that order, and if the drawing is ever wrong nothing
important is lost.

```
 THE MAP - 6 places, you are at 1

         [2]-----[4]     [5]

 [6]     [1]-----[3]

 1 The Whispering Woods  <- you are here
     n>2  e>3  d>6  (w unexplored)
 2 The Sunken Gate        s>1  e>4
 3 The Sunlit Meadow      w>1  n>5
 4 The Tower Entrance     w>2
 5 The Foothills          s>3
 6 The Rotted Cellar      u>1

 /map 5 - how to get to The Foothills
```

Room numbers are `num` from §1.2 and are stable for the life of the
adventure, which is what makes `/map 5` learnable. Unvisited seeded
rooms render parenthesised: `(7) The Salt Cloister`.

### 5.2 Layout algorithm

Deterministic, integer-only, no cleverness. Put it in
`advmap.render_ascii()` and unit-test it.

```
DELTA = {n:(0,-1), s:(0,1), e:(1,0), w:(-1,0),
         ne:(1,-1), nw:(-1,-1), se:(1,1), sw:(-1,1)}
         # u, d, in, out and null have NO delta
```

1. BFS from `at`, placing it at (0,0). Visit neighbours in a fixed
   order (sort by `num`) so output is stable and testable.
2. For each unplaced neighbour: candidate = current + `DELTA[dir]` if
   the direction has one, else the current cell itself.
3. If the candidate is taken by another room, spiral outward to the
   nearest free cell — candidate offsets sorted by
   `(abs(dx)+abs(dy), dy, dx)`, radius up to 3. If nothing is free,
   leave the room unplaced; it still appears in the legend.
4. Normalise so min x and min y are 0.
5. **Window**: the grid is `W=8` chars per column, `H=2` rows per row.
   With a 78-column line that is 9 grid columns; pick the row count to
   suit (8 grid rows ≈ 16 lines). If the extent exceeds the window,
   crop to a window centred on `at` and note `(+N places off this view)`.
6. Draw a room's `[num]` label at `(gx*8, gy*2)`.
7. **Draw a connector only when the edge's direction matches the actual
   placement**: an `e`/`w` edge between horizontally adjacent cells gets
   `-` filling the gap; an `n`/`s` edge between vertically adjacent
   cells gets `|` on the odd row under the label's second column.
   Everything else — displaced edges, `u`/`d`/`in`/`out`, `dir=null` —
   is **not drawn**. It is in the legend, which is complete. Drawing a
   horizontal line for a "down" edge is a lie, and a map that lies is
   worse than a map that is sparse.

### 5.3 Client landmines — all four are real

1. **Leading spaces are dropped.** `chat_append_ascii_char` stores a
   space only when `cur_len > 0` (`display.c`, search `cur_len > 0`), so
   every indented line of ASCII art loses its indentation.
   **Fix: begin every map line with a colour tag**, e.g.
   `[color=cyan]`. It becomes a marker cell, is pushed into `wbuf`, and
   `flush_word` moves it into the line — so `cur_len` is 1 by the time
   the first space arrives and every subsequent space survives. Markers
   render as spaces (`colorize_row` rewrites them to `0x20`), so it
   costs exactly one column and looks like an indent.
   **Therefore: keep drawn lines to 78 characters, not 79**, and
   **emit `[/color]` on the final line** — a run carries across the line
   break (`run_at_line_start`), so an unclosed colour tints the rest of
   the chat.
2. **A line of exactly 80 characters produces a spurious blank row** —
   `flush_word` commits at `cur_len >= TEXT_COLS` and the following
   `\n` commits again.
3. **Never put `**` in map art.** `colorize_for_wire` turns `**x**`
   into bold markers. `[`, `]`, `|`, `-`, `>`, `<`, `(` and `)` are all
   safe; a lone `*` is safe but there is no reason to risk it.
4. This all runs through `_send_text` -> `split_safe` ->
   `colorize_for_wire` like any other prose. `_send_canned` flushes at
   the end, so nothing is left held.

### 5.4 Commands

| Command | Behaviour |
|---|---|
| `/map` | Draw it. Empty map -> "No map yet - the story has not moved you anywhere." |
| `/map <n>` or `/map <name>` | BFS route: "The Foothills: e, then n. (3 places away.)" **No model call, instant** — the cheapest correct answer in the whole design. |
| `/map` outside adventure mode | "The map only exists in adventure mode." |

Dispatch in `handle_command` next to `/pic`. Add one line to `/help`.

**F1 menu**: adventure mode already returns **exactly 13** entries
against the client's `MAX_MENU` of 13, so a menu entry means dropping
one. **Replace `('m', 'Models', '/models')` with `('m', 'Map', '/map')`**
in the adventure/roleplay branch of `_menu_entries()`. Switching models
mid-adventure is rare and `/models` is still typeable; a map is
something you reach for constantly. One line, trivially reversible if
the user disagrees.

---

## 6. `/map` as a picture (phase 3, optional)

The fullscreen image path writes the bitmap to `$E000` and the matrix to
`$CC00` — **the same memory the soft-80 renderer uses**. A hires image
is not a picture over the text screen; it *is* a screen.
`send_image_blob(blob, 0, fmt=0)` already exists, any key dismisses it
and the chat repaints itself, and the transfer runs with music playing
(the client only stops the tune for `SID_BEGIN` and conversation loads),
which `/pic` has proved in the field.

`tools/make_font.py` holds the 4x8 glyph table as Python source, so the
proxy can rasterise labels in the client's *exact* font and draw real
connecting lines at pixel resolution — something no client-side renderer
could do with a 95-glyph ASCII set. Reuse the same `advmap` layout
(§5.2) at pixel scale; the palette is in `imaging.py`. Colour is per 8x8
cell = per two glyph columns, so lay boxes out on even columns.

Cost: **0 client bytes, 0 wire changes**, perhaps 200-300 lines of
Python. The price is time: a hires blob is fixed at 9000 bytes and the
client shows nothing until it has all of them. At `SID_CHUNK` 256 and
the default `wire_baud` 9600 pacing that is ~36 frames and roughly
**11.5s** (computed from the pacing constants, never timed; the C64U
actually runs 19200, so the real figure may be better).

**Keep `/map` as text by default.** A map is a reference you glance at,
not an event you stage; 11 seconds is the wrong price for a glance. Add
the picture as `/map pic`, and only if the text version proves it wants
prettifying.

**Do not add `/map`-as-image to the field-test load while the NMI/ACIA
transposed-byte race (HANDOFF.md) is still being characterised.** It is
another bulk transfer and it will muddy that evidence.

---

## 7. Seeding from the prep notes (phase 4, optional)

`PREP_SYSTEM` already asks the prep pass for "3-5 named locations with
how they connect", so the geography exists as prose in the bible on
every adventure that came through the front door. Seeding the map from
it means the first `/map` is not empty and — more valuable — the model
is anchored to place names it already committed to.

Implementation, in this order:

1. Add a machine-readable tail to `PREP_SYSTEM`: after the prose, emit
   `MAP:` and one line per place,
   `- The Flooded Nave | n: The Choir Stair | e: The Salt Cloister`.
2. `advmap.seed_from_notes(m, bible)` parses that section best-effort.
   Rooms get `visited: false`. **Failure is silent and harmless** — a
   bad parse must never block an adventure starting, the same rule
   `_prep_world` already follows.
3. Call it in `_start_adventure` when `background` is non-empty.

Leave the `MAP:` section in the bible text. It costs nothing (the bible
rides the cached prompt prefix) and gives the model a second look at its
own geography.

---

## 8. Build order

Each phase is independently shippable and independently useful.

### Phase 1 — the graph (proxy only, no C64 involvement at all)

`advmap.py` + filter wiring + ingest + prompt block + prompt snippet.
New `c64llm_proxy/tests/test_map.py` — plain script, no pytest, run as
`python3 tests/test_map.py`, matching `test_directives.py`.

Test cases, all pure Python:

- a location change with **no directive** creates the room and a
  `dir=null` edge  *(this is the case that must never regress — it is
  the whole safety argument)*
- a directive split across stream chunks (feed one character at a time,
  as `test_directives.py` does)
- the single-bracket `[MAP: ...]` fallback
- slug aliasing: "The Sunken Gate" / "Sunken Gate" / "sunken gate" are
  one room; token-set match merges; near-misses do **not** merge
- `num` is stable across ingests and never reused
- a second edge in the same `dir` from the same room is stored with
  `dir=null`, and the first edge is untouched
- `from=` unknown: edge rejected, `at` still moves
- reverse traversal works (BFS from B reaches A over an edge stored A->B)
- `oneway=1` blocks it
- `exits` minus known edges yields the unexplored list
- prompt block honours the budget, keeps lines 1-3 and routes, and
  appends `(+N older places not shown)`
- BFS routing returns the shortest path and `None` for unreachable

**Then play a real adventure on the hardware and read
`data/conversations/<id>.json` and the proxy log before writing another
line of code.** The open question this phase exists to answer is: does
Gemma emit `[[MAP:]]`, and how often does `location` change without a
matching narrative move? Everything after this is layout work; this is
the only part that can be wrong for reasons you cannot see from here.

### Phase 2 — `/map` as text

`render_ascii` + `route` + `find_room`, the command dispatch, the `/help`
line, the menu swap. Tests: layout determinism (same map -> same lines),
every line ≤78 chars, no line of exactly 80, the colour-tag prefix is
present on every drawn line and closed exactly once, connectors appear
only for placement-consistent cardinal edges, the window crop reports
what it dropped.

No lockstep deploy, no reboot, no d64 rebuild.

### Phase 3 — `/map pic`

Rasteriser over the same layout. e2e: the mock LLM emits `[[MAP:]]` over
several turns, `/map pic` is issued, completion asserted by polling
`_img_shown` through `labels.txt` — **never by reading screen text**
(HANDOFF.md). Run tui-80 + tui + hayes + watchdog, and add long-rt
because a 9000-byte transfer sits on the streaming path.

### Phase 4 — seeding from the prep notes

### Explicitly NOT in scope

**A client-side overlay module.** It would buy speed and panning for
~30 resident bytes plus a whole new 3.5K module, a sixth overlay slot in
`c64-soft80.cfg`, Makefile and d64 changes, a new wire message pair, and
a lockstep deploy — and graph layout does not belong on a 6502 with no
divide (a `uint32` divide cost this project 233 resident bytes once
already). If it is ever built, the proxy must send a *placed display
list* (`[room x y len name]`, `[edge x1 y1 x2 y2 style]`) so the module
only draws boxes and straight runs. Do not start it without the user
asking for it by name.

---

## 9. Risks and what has not been verified

- **Model compliance with `[[MAP:]]` is untested.** No API call was made
  while writing this. Prior evidence is good but not about this
  directive: the model emits the status line on 85 of 85 post-feature
  replies (§0.1), yet it has also been seen closing a `[[STATE]]` block
  with a single bracket. **This is why §0 puts the load on `location`
  instead.** If the directive turns out to be reliable, the map gets
  prettier; if not, it still works.
- **The most likely disappointing outcome is a *correct but sparse*
  map.** If the model ignores `[[MAP:]]`, every edge carries
  `dir=null`, no connectors are drawn (§5.2 step 7 forbids drawing what
  is not known), and `/map` degrades to a numbered legend with exits
  shown as `?>4`. The internal state — the user's first goal — is fully
  served; the drawn picture — the second goal — is not. If that is what
  the phase-1 playtest shows, the fix is prompt work on §3.3, or asking
  the model for direction in a **separate one-shot utility call** via
  `_ask_model` after a detected move ("the player just went from A to B;
  reply with one of n s e w ... or ?"), which trades ~1s per move for
  directions that do not depend on in-band compliance at all. Do not
  build that speculatively.
- **Silent non-movement.** If the model narrates a move and neither
  changes `location` nor emits a directive, that move is lost. Nothing
  detects this. Accepted: the map is a best-effort atlas, not a
  simulation.
- **Spurious movement.** The inverse is likelier and worse: the model
  rewrites `location` cosmetically ("The Whispering Woods" ->
  "Deeper in the Whispering Woods") and ingest invents a room and an
  edge. Slugging and the token-set match catch some of it. **Watch for
  this specifically in the phase-1 playtest** — if it is common, the
  answer is to tighten the prompt line in §3.3, not to add fuzzier
  matching.
- **Character counts are not token counts.** No tokenizer on this
  machine.
- **The 11.5s image figure** is computed from pacing constants, not
  timed. Nobody has measured a `/pic` end to end in this repo.
- **The leading-space behaviour** in §5.3 is read from source, not
  observed on screen. It is the first thing to check if the drawn map
  comes out ragged.
- **Interaction with the open NMI/ACIA race**: see §6.

---

## 10. Rejected, and why (so it is not re-litigated)

- **Put the graph inside `[[STATE]]`.** No. A malformed state block is
  dropped wholesale; putting the map inside means one missing quote in
  the geography also throws away HP and inventory. `adv_state` is also
  pasted verbatim into the illustration prompt by
  `_derive_scene_prompt`, where 40 rooms of geography would swamp the
  image description and make pictures worse. And STATE is a snapshot by
  design while a map is an accumulation.
- **Have the model re-emit the whole graph each turn.** No. Captured
  state blocks run 126-169 characters; a 20-room graph is ~1000
  characters of structured data to transcribe verbatim every turn,
  forever, with 20 fresh chances per turn to corrupt an edge written
  forty turns ago. That is the operation LLMs are worst at. Deltas have
  nothing to re-transcribe.
- **DOT syntax (`woods -> gate [dir=n]`).** No. A second syntax for the
  filter to learn, whose `[`/`]` collide with the directive parser.
- **Storing both directions of an edge.** No. Two records that can
  disagree, for a lookup that a linear scan does correctly at this
  scale.
- **Deleting old rooms when the prompt budget is exceeded.** No — that
  breaks backtracking, which is the requirement. Degrade the
  *rendering* only.
- **Collapsing degree-1 leaves into their parent line at the cap.**
  Rejected as over-engineering: at one new room per eight turns the cap
  first bites around turn 400. Truncate by recency and say so.
- **Letting the model raise the map itself (an `[[IMAGE:]]`-style
  event).** No. An image is an event the narrator stages; a map is a
  reference the player asks for. An unprompted fullscreen takeover
  mid-scene is an interruption.
- **Parsing the player's command as the primary direction source.** No —
  this user plays free-form (§0). Keep it as a null-filler only.
