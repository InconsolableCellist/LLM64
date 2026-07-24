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

# "/print my inventory", "/print the character sheet" - the fast path
# that needs no model at all.
SHEET_RE = re.compile(r'\b(inventory|character|char|sheet|stats)\b', re.I)

# Belt and braces: stored replies have already been through the
# directive filter, but a document must never carry [[STATE:...]] to
# paper - it is the one text the player was never meant to read.
DIRECTIVE_RE = re.compile(r'\[\[.*?\]\]', re.S)

RULE = '-'
MIN_WIDTH = 20


def wants_sheet(arg: str) -> bool:
    """Does this /print argument ask for the character sheet?"""
    return bool(SHEET_RE.search(arg or ''))


def last_reply(msgs) -> str:
    """The newest assistant message - what a bare /print prints."""
    for m in reversed(msgs or []):
        if m.get('role') == 'assistant' and (m.get('content') or '').strip():
            return m['content']
    return ''


def compose_question(arg: str, convo: str) -> str:
    """The one-shot question that asks the chat model to write the
    document. `convo` is the joined recent transcript (already
    trimmed), `arg` the player's own words after /print."""
    return (
        "Below is the latest part of a conversation. Extract and compose "
        f"a {MARKER} from it for: {arg}\n"
        "Reply with ONLY the document: a short title on the first line, "
        "then its content. Plain text for a 1980s dot-matrix printer - "
        "no markdown, no bullets beyond '- ', no commentary before or "
        "after, nothing addressed to the reader. Keep every concrete "
        "detail (quantities, names, steps, numbers) exactly as it "
        "appears; do not invent any.\n\n"
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
    for key, label in (('mana', 'Mana'), ('gold', 'Gold'),
                       ('score', 'Score')):
        if st.get(key) is not None:
            stats.append(f"{label} {st[key]}")
    if stats:
        out.append('   '.join(stats))

    if st.get('appearance'):
        out += ['', 'Appearance:', '  ' + str(st['appearance'])]

    for key, label, empty in (('inventory', 'Inventory', '(nothing)'),
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


def finish(title: str, body: str, width: int = 78, date: str = None) -> str:
    """Header, rule, wrapped body, rule. Pure ASCII with \\n line ends -
    exactly what the client's PETSCII mapping expects. Returns '' when
    there is nothing to print."""
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

    out = '\n'.join(head + _wrap(body, width) + [RULE * width]) + '\n'
    return out.encode('ascii', 'replace').decode('ascii')
