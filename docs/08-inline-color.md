# Inline colour in adventure output — design (investigated 2026-07-21)

Goal: the LLM writes `You see the [color=grey]steel door[/color] to the
**north**`, the proxy turns the tags into in-band marker bytes, and the
soft-80 client renders "steel door" in grey — in the live stream, in
scrollback, and after a conversation reload — without meaningful memory
cost.

**Verdict: feasible.** Zero extra scrollback RAM, ~250–350 bytes of
resident CODE against a measured 1474-byte headroom (BSS end $963E vs
slot $9C00, hayes-80 build of 2026-07-21), and — with the space-swallow
trick below — zero visible colour bleed in practice. This design
supersedes the per-line colour-run table sketched in HANDOFF.md (which
cost ~1KB of RAM and new wrap machinery); markers ride the existing
wrap logic instead.

## 1. Hardware reality (soft-80)

There is no "CTRL+white for free" in bitmap mode — that's a PETSCII
editor feature. The soft-80 screen is a hires bitmap with the colour
matrix at $CC00: ONE foreground/background nibble pair per 8x8 cell,
and each cell holds TWO soft-80 glyphs. So:

- Colour changes snap to even columns (a "pair"). Two adjacent
  characters in the same pair cannot differ in colour.
- Changing a pair's colour is just a matrix byte poke — cheap, no
  bitmap work. The rainbow attention line (`ui_blit_row`'s 0xFF path in
  display.c) already does exactly this: glyphs first, then per-pair
  matrix writes. That code path is the model for this feature.
- Bonus: the matrix LOW nibble is the per-cell background — a future
  `[bg=...]` highlight costs nothing architecturally.

Reverse video (cell bit 7) is per-CHARACTER with no granularity limit,
which is why `**bold**` gets a different, cheaper mechanism (§5).

## 2. Wire format: in-band marker cells

Cell values 0x00–0x1F are dead space today: the soft-80 renderer draws
them as spaces (font index underflow clamps to glyph 0), and
`chat_append_ascii_char` discards or mangles them. We claim:

| byte        | meaning                                   |
|-------------|-------------------------------------------|
| 0x10 \| c   | open colour run, colour c (c = 1–14)      |
| 0x01        | close colour run (revert to line colour)  |
| 0x02 / 0x03 | reverse video on / off (bold; append-time) |

Constraints that shaped this:
- 0x00 is unusable — the client walks text payloads as C strings.
- 0x0A/0x0D stay newline/CR.
- Run colour 15 is forbidden (proxy maps grey→12/GRAY2): it keeps the
  carry encoding (§4) from ever colliding with the 0xFF rainbow
  sentinel in `line_color`.

Markers are stored in `line_text[]` like ordinary cells, so scrollback,
`build_view_row`, and conversation reload need **no new storage** —
the colour information lives inside the 80 columns it decorates.

## 3. The space-swallow trick (kills the bleed problem)

A marker occupies one cell and renders as a space. So the proxy doesn't
*add* markers — it *replaces* the adjacent space:

```
LLM:    the [color=grey]steel door[/color] to the north
wire:   the◄G►steel door◄/►to the north      (◄G► = 0x1C, ◄/► = 0x01)
screen: the steel door to the north           (identical spacing)
```

- Open marker replaces the space *before* the tag; close marker
  replaces the space *after* it.
- A run is therefore always delimited by marker-spaces, and the render
  rule "a pair takes the run colour iff it contains a run glyph" means
  the only characters ever recoloured by pair-snapping are those
  spaces — i.e. **no visible bleed**, ever, for normally punctuated
  text. Mid-word tags (`mid[color=red]word`) would bleed one glyph;
  the LLM has no reason to write them; accept.
- Punctuation: the proxy hoists a close tag past an adjacent
  punctuation cluster (`door[/color],` → `door,[/color]`) so the comma
  is coloured with the word — typographically standard, and it keeps
  the close marker adjacent to a swallowable space.
- Tag at start-of-line / end-of-line with no space to swallow: emit
  the marker anyway; one blank column there is invisible.

## 4. Client changes

**Append path** (`chat_append_ascii_char`, the single choke point —
streamed chunks, notices, and conversation loads all funnel here):
- `0x10|c`: `flush_word()` (the marker acts as the word break it
  replaced), set `run_color = c`, push the marker into the fresh
  `wbuf` so it wraps glued to the word it colours.
- `0x01`: append marker to `wbuf` (glued to the preceding word),
  `flush_word()`, clear `run_color`.
- `0x02/0x03`: set/clear a reverse flag; while set, OR 0x80 into
  appended cells. Consumed at append time — never stored, zero column
  cost (bit 7 is already part of the cell format).
- 40-column build: consume colour markers and drop them (v1). The
  hardware could do per-char colour trivially, but the line renderer
  memsets one colour per row and adventure ships on soft-80.

**Wrap carry** — a run crossing a committed line must survive the
break. `line_color[]` already exists per line; encode
`carry_in << 4 | base_color` (carry 0 = none, base colours are all
nibble-sized, rainbow lines never carry). Client keeps one static:
the run state at the moment the current line started; `commit_line`
stores it. Zero RAM, no marker wasted on continuation lines, and the
wrap logic itself is untouched — markers are just cells to it.

**Render path** (`ui_blit_row`, SOFT80): if the row has no marker and
no carry-in, the existing path runs unchanged. Otherwise:
1. Walk `rowbuf` once: compute each pair's colour (start from
   carry-in, update at markers, "run glyph in pair → run colour"),
   into a 40-byte scratch; replace marker cells with 0x20 *in rowbuf*.
2. `soft80_row()` as today (asm sees pure ASCII — **no soft80.s
   changes**, and the $C000 shadow shows plain text with normal
   spacing, so the e2e screen-scraper is unaffected).
3. Write the scratch nibbles to the matrix — the rainbow precedent.

`rowbuf` is a scratch copy (every caller rebuilds it), so mutating it
is safe.

**Streaming fast path** (`chat_redraw_stream` span optimization):
`soft80_span` deliberately skips the matrix. When a run is active on
the partial line, the client already *knows* the tail colour without
scanning — after the span blit, poke the span's matrix pairs with the
current run colour (~25 bytes). No full-row fallback in the hot path,
so serial pacing is untouched.

Budget: ~250–350 bytes CODE + ~3 bytes BSS + 40-byte scratch (BSS).
Headroom after: >1100 bytes. Per-row render cost: one 80-cell C scan +
40 matrix pokes, only on rows that contain markers — the same order of
work as the attention-line rainbow, which streams fine today.

## 5. Proxy changes

All in the existing directive machinery — this is the easy half.

- **Parsing** lives in `MusicDirectiveFilter` (music.py): add
  `[COLOR`, `[/COLOR` (accept `=` or `:`, optional spaces, case- and
  spelling-insensitive — `colour` too) to `_PREFIXES` so a tag split
  across stream chunks is held back, exactly like `[[MUSIC:` today.
  Unknown/malformed colour names: strip the tag, emit the text plain —
  tags must never leak to the screen.
- **Transform at egress, tags in storage.** `full_response` (and thus
  saved history and future LLM context) keeps the original `[color=…]`
  tags — the model sees its own past usage and stays consistent, and
  no control bytes enter the JSON or the prompt. A single
  `colorize_for_wire(text) -> bytes` helper applies tags→markers +
  space-swallow + punctuation-hoist at every client-bound text egress:
  the stream chunk sender, `flush()` tail, `LOAD_CONVERSATION` replay,
  and `/history` pages (which would otherwise print literal tags).
- **Palette**: name→C64 index table, readable-on-black variants only:
  white 1, red 2, cyan 3, purple 4, green 5, yellow 7, orange 8,
  brown 9, pink/lightred 10, grey 12 (never 15, see §2), lightgreen 13,
  blue→lightblue 14 (blue 6 is illegible on black).
- **`**bold**`** → 0x02/0x03 (reverse video). If reverse blocks read
  too heavy in practice, remap to a white colour run — proxy-only
  change, one line.
- **System prompt** (modes.py, adventure/roleplay): teach the markup
  with the allowed palette and "colour sparingly — objects, exits,
  hazards". Keep it a few lines; the state/music prompt is long
  already.

Syntax safety: `[color` is a distinct keyword from `[[MUSIC/IMAGE/
STATE`, `[roll:` dice macros, so the existing filter and the colour
extension coexist; the hold-back walker already anchors on the
earliest unresolved `[`.

## 6. Ordering and tests

1. Proxy: `colorize_for_wire` + filter prefixes + unit tests
   (split-chunk tags, space swallow, punctuation hoist, malformed
   tags, `/history` egress). Pure Python, no client yet — markers
   render as '?' on an old client, so deploy in lockstep (rule 2).
2. Client: append-path markers + wrap carry + render patch + span
   poke. `make -C c64_client clean && make MODE80=1`, check map
   headroom (rule 9).
3. e2e: mock_llm emits a tagged reply; assert the $C000 shadow shows
   plain text with normal spacing (proves marker→space), then peek the
   $CC00 matrix row via the VICE monitor for the expected nibbles
   (same monitor channel the media tests use for `_img_shown`).
   Run tui-80 + tui + hayes + watchdog + long-rt (streaming touched).
4. Real-hardware pass: colour runs during live streaming with music
   playing (the span-poke path is the one that matters).

## 7. Risks / open decisions

- **Pair granularity is authentic C64 jank**: a run's delimiting
  spaces take the run colour. Invisible (they're spaces), but worth
  knowing.
- **Model compliance**: Gemma may over- or under-use the markup, or
  invent colours. Malformed tags strip to plain text by design, so the
  failure mode is cosmetic absence, not garbage on screen.
- **Old conversations**: no tags in history → no colour until the
  model starts emitting them. Same accepted precedent as `adv_state`.
- **`0x42` in payloads** is already legal (framing is length-based);
  markers add nothing new to the transport.
- Decide: bold as reverse video vs. white run (ship reverse, one-line
  proxy change if it reads badly).
