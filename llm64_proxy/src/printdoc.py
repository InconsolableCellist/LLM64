"""Compose the document /print puts on paper (docs/14).

The C64 is a dumb printer driver: it opens IEC device 4, writes the
bytes it is handed, and closes. Everything about what a page LOOKS like
is decided here - what to extract, how wide to wrap, what the header
says - so the layout can change with no client rebuild.

Three sources, cheapest first:

  bare /print          the last assistant reply, reflowed. No API call.
  /print my inventory  the adventure character sheet, rendered from the
                       [[STATE]] JSON. Deterministic, no API call.
  /print the recipe    a one-shot utility question to the chat model,
                       which extracts and composes the document.

Kept as pure functions (no event loop, no model, no I/O) so the whole
thing is testable - tests/test_printdoc.py.
"""

import json
import re
import textwrap
import time

# Common Unicode punctuation -> ASCII, applied before the ascii/replace
# encode so LLM typography doesn't print as '?'. Shared with the wire
# path rather than defined twice.
from .markup import UNICODE_TO_ASCII

# The literal phrase every composed question carries. The e2e mock keys
# on it (emu/mock_llm.py), so it must not drift.
MARKER = 'PRINTABLE DOCUMENT'

# The system prompt the compose call runs under, and the reason it must
# exist at all: api_client.stream_chat reads system_prompt=None as "use
# the configured one", so a document request inherited the CHAT prompt -
# on this deployment "You are chatting with a user on a Commodore 64
# with a 40-column screen. Keep replies short and conversational."
# Asking that persona for a one-page recipe gets a one-paragraph recipe,
# and no token budget fixes it: measured on the live proxy, a whole
# story summary composed to 782 characters with printer max_tokens at
# 2000. The page is not a chat turn and must not be written by the chat
# persona (docs/14 13.9).
SYSTEM = (
    "You are composing a document that will be printed on paper. This "
    "is not a chat message: nobody is reading it on a 40-column screen, "
    "there is no conversation to continue, and nothing is addressed to "
    "a reader. Write the document itself, in plain ASCII, at whatever "
    "length the request asks for - brevity is not a virtue here."
)

# "/print my inventory", "/print the character sheet" - the fast path
# that needs no model at all.
SHEET_RE = re.compile(r'\b(inventory|character|char|sheet|stats)\b', re.I)

# ...but only when the request is ABOUT the sheet. These words mean
# "compose this from the conversation" and outrank it, because the fast
# path is deterministic: it renders stored state and never calls the
# model, so a document request that merely mentions a character came
# back as a character sheet in the same second it was typed - "/print
# summary of the story with critical character details" (2026-07-24,
# 13.13). Being wrong here is silent and total, where being wrong the
# other way only costs a model call.
DOC_RE = re.compile(
    r'\b(summary|summarize|summarize|synopsis|history|story|tale|recap|'
    r'account|chronicle|journal|diary|log|timeline|events|narrative|'
    r'transcript|report|happened|so far|write[- ]?up)\b', re.I)

# "/print the picture" - the conversation's last illustration on paper
# instead of a document (printpic.py, docs/14 13.11). Checked BEFORE
# SHEET_RE: "print a picture of my character" is a picture request, and
# 'character' would otherwise claim it.
PIC_RE = re.compile(
    r'\b(pic|pics|picture|image|illustration|artwork|art|drawing|drawn|'
    r'drew|painting|portrait|sketch|screenshot|photo)\b', re.I)

# "/print picture 2" - the same numbering /pics shows and /pic <n>
# re-displays, newest first. Bare = the latest.
PIC_N_RE = re.compile(r'\b(\d+)\b')

# "/print the map" - the adventure map, rendered from stored state.
# ASCII art, so unlike a picture it prints on BOTH backends; it is the
# character sheet's sibling, not the illustration's (docs/14 13.12).
MAP_RE = re.compile(r'\bmaps?\b', re.I)

# What the player's own words ask for, read off the /print argument.
#
# Fidelity: by default a document is an EXTRACTION - everything on the
# page was said in the conversation. But "/print please complete this
# recipe" is a different job: the player is asking for the gaps to be
# filled (the measurements nobody stated, the ingredient list that was
# never written out), and refusing to invent would just reprint the
# same holes. Only an explicit ask flips this - the default stays
# faithful, because a printed page is easy to mistake for a record.
SYNTH_RE = re.compile(
    r'\b(finish|fill\s+(?:in|out)|flesh\s+out|expand|elaborat\w*|'
    r'embellish|synthesi[sz]e|collate|compile|infer|missing|'
    r'draft|invent|improvis\w*)\b', re.I)

# "complete" is two different words. As a verb it asks for the gaps to
# be filled ("please complete this recipe"); as an adjective it asks for
# nothing to be left out ("the complete recipe"), which is extraction.
# The object's determiner is what tells them apart.
COMPLETE_VERB_RE = re.compile(
    r'\bcomplet(?:e|ing)\s+(?:the|this|that|these|my|our|it\b)', re.I)

# Length: paper has a page, and the model can't see it. "detailed" or
# "one-page" asks for the sheet filled; "brief" asks for a note.
FULL_RE = re.compile(
    r'\b(detailed|detail|full|full-page|one[-\s]?page|whole\s+page|long|'
    r'in[-\s]?depth|comprehensive|thorough|complete|exhaustive)\b', re.I)
BRIEF_RE = re.compile(
    r'\b(brief|briefly|short|shorter|summary|summari[sz]e[d]?|concise|'
    r'terse|quick|one[-\s]?liner?|note)\b', re.I)

# Roughly what fits a US-letter/A4 page at 6 lpi with the header and
# rules taken off. The model is told lines, not tokens - it has no idea
# how wide the paper is otherwise.
FULL_LINES = 55
BRIEF_LINES = 20

# Belt and braces: stored replies have already been through the
# directive filter, but a document must never carry [[STATE:...]] to
# paper - it is the one text the player was never meant to read.
DIRECTIVE_RE = re.compile(r'\[\[.*?\]\]', re.S)

RULE = '-'
MIN_WIDTH = 20


def wants_sheet(arg: str) -> bool:
    """Does this /print argument ask for the character sheet ITSELF?

    'my inventory' and 'the character sheet' do. 'a summary of the story
    with critical character details' does not - it names a character and
    asks for a document, and the sheet path would answer it from stored
    JSON without ever reading the conversation."""
    arg = arg or ''
    return bool(SHEET_RE.search(arg)) and not DOC_RE.search(arg)


def wants_pic(arg: str) -> bool:
    """Does this /print argument ask for the picture rather than a
    document? Wins over wants_sheet when both match."""
    return bool(PIC_RE.search(arg or ''))


def pic_index(arg: str):
    """Which picture, in /pics numbering (1 = newest), or None for the
    latest. 'the last image' is None, not 'last': there is no number in
    it, and the latest is what it means anyway."""
    hit = PIC_N_RE.search(arg or '')
    return int(hit.group(1)) if hit else None


def wants_map(arg: str) -> bool:
    """Does this /print argument ask for the adventure map?"""
    return bool(MAP_RE.search(arg or ''))


def transcript(msgs, budget: int, per_msg: int = 4000) -> str:
    """The recent conversation as one block, newest-first until `budget`
    characters are spent, then put back in order.

    Not a fixed number of messages. The composer used to take the last
    12, which is about six turns - fine for "the recipe", useless for
    "a detailed history of the story", which is exactly what a player
    asks a printer for. A 233-message adventure got six turns of itself
    and the document was thin for a reason no one could see (13.13).
    The budget comes from the model's own context window, so a 131k
    model reads the whole adventure and an 8k one reads what it can.

    Whole messages only: half a turn reads as the model being confused
    rather than the transcript being clipped."""
    out, used = [], 0
    for m in reversed(msgs or []):
        line = f"{m['role']}: {m['content'][:per_msg]}"
        if out and used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out))


def last_reply(msgs) -> str:
    """The newest assistant message - what a bare /print prints."""
    for m in reversed(msgs or []):
        if m.get('role') == 'assistant' and (m.get('content') or '').strip():
            return m['content']
    return ''


def wants_synthesis(arg: str) -> bool:
    """Has the player asked the model to fill the document's gaps rather
    than only extract what was already said?"""
    arg = arg or ''
    return bool(SYNTH_RE.search(arg) or COMPLETE_VERB_RE.search(arg))


def target_lines(arg: str):
    """Printed lines the document should aim for, or None when the
    player said nothing about length. 'brief' wins a tie: asking for
    both is asking for the short version of something detailed."""
    arg = arg or ''
    if BRIEF_RE.search(arg):
        return BRIEF_LINES
    if FULL_RE.search(arg):
        return FULL_LINES
    return None


def compose_question(arg: str, convo: str) -> str:
    """The one-shot question that asks the chat model to write the
    document. `convo` is the joined recent transcript (already
    trimmed), `arg` the player's own words after /print.

    Two things in `arg` steer it beyond naming the subject: a request to
    complete or expand relaxes the extract-only rule, and a word about
    length gives the model the page it cannot see. Neither fires unless
    the player asked - a bare subject still gets today's faithful
    extraction at whatever length the source runs to."""
    synth = wants_synthesis(arg)
    lines = target_lines(arg)

    verb = "Compose" if synth else "Extract and compose"
    if synth:
        # Still anchored: what the conversation DID say is quoted, not
        # paraphrased. Only the holes are the model's own work.
        fidelity = (
            "Keep every concrete detail (quantities, names, steps, "
            "numbers) the conversation already gives exactly as it "
            "appears. Where the document is incomplete, supply what is "
            "missing - measurements, an ingredient or parts list, "
            "omitted steps, times - so the page stands on its own "
            "without the conversation. Do not contradict anything that "
            "was said.")
    else:
        fidelity = (
            "Keep every concrete detail (quantities, names, steps, "
            "numbers) exactly as it appears; do not invent any.")

    length = ''
    if lines == BRIEF_LINES:
        length = (f" Keep it under {BRIEF_LINES} lines - this is a note, "
                  "not a report.")
    elif lines == FULL_LINES:
        length = (f" Fill the page: aim for about {FULL_LINES} lines of "
                  "78 columns. Use the room for detail that earns it - "
                  "complete steps, full lists - never padding or "
                  "repetition.")

    return (
        f"Below is the latest part of a conversation. {verb} "
        f"a {MARKER} from it for: {arg}\n"
        "Reply with ONLY the document: a short title on the first line, "
        "then its content. Plain text for a 1980s dot-matrix printer - "
        "no markdown, no bullets beyond '- ', no commentary before or "
        f"after, nothing addressed to the reader. {fidelity}{length}\n\n"
        "Conversation:\n" + convo)


def split_title(text: str):
    """An LLM document arrives as 'title\\nbody'. Returns (title, body);
    a single-line reply becomes its own body with no title."""
    text = (text or '').strip()
    if not text:
        return '', ''
    head, _, rest = text.partition('\n')
    if not rest.strip():
        return '', head
    return head.strip().strip('#').strip(), rest.strip()


def render_sheet(adv_state, character: str = '', background: str = '') -> str:
    """The adventure character sheet as printable text. `adv_state` is
    the stored [[STATE]] JSON (string or dict); `character` is the fixed
    sheet from setup. Every field is optional - a state block that only
    tracks hp and a location still prints cleanly."""
    if isinstance(adv_state, str):
        try:
            adv_state = json.loads(adv_state)
        except (ValueError, TypeError):
            adv_state = {}
    st = adv_state if isinstance(adv_state, dict) else {}
    out = []

    if st.get('location'):
        out.append(f"Location: {st['location']}")

    # One stat row, only the stats this adventure actually uses
    stats = []
    if st.get('hp') is not None:
        hp = str(st['hp'])
        if st.get('maxhp') is not None:
            hp += f"/{st['maxhp']}"
        stats.append('HP ' + hp)
    if st.get('mana') is not None:
        mana = str(st['mana'])
        if st.get('maxmana'):
            mana += f"/{st['maxmana']}"
        stats.append('Mana ' + mana)
    # Level and XP were on the screen sidebar and on the wire, and only
    # the printed sheet did not have them.
    for key, label in (('ac', 'AC'), ('level', 'Level'), ('xp', 'XP'),
                       ('gold', 'Gold'), ('score', 'Score')):
        if st.get(key) is not None:
            stats.append(f"{label} {st[key]}")
    if stats:
        out.append('   '.join(stats))

    if st.get('appearance'):
        out += ['', 'Appearance:', '  ' + str(st['appearance'])]

    for key, label, empty in (('effects', 'Afflictions', '(none)'),
                              ('inventory', 'Inventory', '(nothing)'),
                              ('companions', 'Companions', '(alone)')):
        items = st.get(key)
        if items is None:
            continue
        out += ['', label + ':']
        out += ['  - ' + str(i) for i in items] if items else ['  ' + empty]

    sheet = (character or background or '').strip()
    if sheet:
        out += ['', 'Character:']
        out += ['  ' + ln for ln in sheet.splitlines() if ln.strip()]

    return '\n'.join(out).strip('\n')


def blocks(data: bytes, size: int):
    """Split the encoded document into PRINT_DATA payloads. A document
    whose length is an exact multiple of `size` must not gain a trailing
    empty block: the client would print nothing and the proxy would
    still sit waiting for its ACK."""
    return [data[i:i + size] for i in range(0, len(data), size)]


def _wrap(text: str, width: int):
    """Wrap line by line, keeping blank lines and leading indent. Not
    paragraph-joining: the sheet's one-item-per-line layout has to
    survive, and prose reflows the same either way once it is wrapped."""
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            lines.append('')
            continue
        indent = line[:len(line) - len(line.lstrip())]
        cont = indent + ('  ' if line.lstrip().startswith('- ') else '')
        lines += textwrap.wrap(line, width, subsequent_indent=cont,
                               break_long_words=False,
                               break_on_hyphens=False) or ['']
    return lines


def finish(title: str, body: str, width: int = 78, date: str = None,
           wrap: bool = True) -> str:
    """Header, rule, body, rule. Pure ASCII with \\n line ends - exactly
    what the client's PETSCII mapping expects. Returns '' when there is
    nothing to print.

    wrap=False is for art rather than prose: the map is a grid, and
    reflowing a grid destroys it. Overlong lines are then CLIPPED, not
    folded - a map line that ran past the paper would come back on the
    next row and read as a second corridor. Callers of the no-wrap path
    render to the width they are given (advmap.render_ascii takes one),
    so the clip is a guarantee, not the plan."""
    width = max(MIN_WIDTH, int(width))
    body = DIRECTIVE_RE.sub('', body or '').translate(UNICODE_TO_ASCII)
    title = DIRECTIVE_RE.sub('', title or '').translate(
        UNICODE_TO_ASCII).strip()
    if not body.strip():
        return ''

    head = []
    if title:
        head += _wrap(title, width)
    head.append(date if date is not None else time.strftime('%Y-%m-%d'))
    head.append(RULE * width)

    laid = (_wrap(body, width) if wrap else
            [ln.rstrip()[:width] for ln in body.splitlines()])
    out = '\n'.join(head + laid + [RULE * width]) + '\n'
    return out.encode('ascii', 'replace').decode('ascii')


# --- the kit catalog -------------------------------------------------
#
# Deterministic, like the character sheet and for the same reason: the
# catalog is proxy data that never enters the prompt, so asking the
# model for it gets a plausible invention rather than the 94 items the
# shop actually stocks - and the numbers would not match the screen,
# which is the entire point of printing it.
CATALOG_RE = re.compile(r'\b(catalog|catalog|shop|shelves|kit list)\b',
                        re.I)


def wants_catalog(arg: str) -> bool:
    """True for '/print the entire catalog' and friends.

    The CALLER must also check that character creation is actually open
    (protocol.py). A roleplay scene can easily put a catalog in front
    of the player - a fence's stock list, a ship's manifest - and
    printing the adventure kit shop instead of what they were reading
    would be baffling."""
    return bool(CATALOG_RE.search(arg or ''))


def render_catalog(rules: dict, items, title_note='') -> str:
    """The shop, in catalog order, with the SAME numbers the screen
    shows - a printed number that does not work when typed back in would
    be worse than no printout."""
    gear = (rules or {}).get('equipment') or {}
    cats = gear.get('categories') or []
    by_kind = {}
    for it in items:
        by_kind.setdefault(it.get('kind'), []).append(it)

    out = []
    if title_note:
        out += [title_note, '']
    out.append("%d points to spend. Custom items cost %d each, up to %d."
               % (gear.get('points', 6), gear.get('custom_cost', 1),
                  gear.get('custom_max', 6)))
    out.append('')
    n = 0
    for c in cats:
        rows = [it for k in (c.get('kinds') or []) for it in by_kind.get(k, [])]
        if not rows:
            continue
        out.append(c['label'].upper())
        for it in rows:
            n += 1
            out.append("  %3d  %-24s %d  %s"
                       % (n, it['name'], it['cost'], it.get('blurb', '')))
        out.append('')
    return "\n".join(out).rstrip()
