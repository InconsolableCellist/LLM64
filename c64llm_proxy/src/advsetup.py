"""The /adventure front door: chooser, staged setup, review loop.

See docs/09-adventure-setup.md. This module is a PURE state machine -
no network, no model, no conversation. feed() takes what the player
typed and returns (reply_text, action), where action tells the caller
what to actually do. Everything the player can reach is therefore
unit-testable without a model or an emulator, which matters because the
interesting behaviour is the edit/cascade rules rather than the prose.

The character sub-steps are FLATTENED into the same stage list rather
than nested. They are decisions like any other, so the review screen,
the edit-and-return loop and the dependency cascade all work on them
without a second mechanism.

The caller owns: asking the model for suggestions, the prep pass, and
creating the conversation. This owns: what screen you are on, what has
been decided, and what an edit invalidates.
"""

from . import chargen

# Actions handed back to the caller
ACT_NONE = 'none'          # just show the reply
ACT_CANCEL = 'cancel'      # player backed out; drop the setup
ACT_QUICK = 'quick'        # option 1: start immediately, no theme
ACT_THEME = 'theme'        # option 2: one-line idea, in .theme
ACT_BEGIN = 'begin'        # review confirmed; bundle in .answers
ACT_SUGGEST = 'suggest'    # caller may offer model suggestions for .stage
ACT_LOAD = 'load'          # play a saved world: .template_slug

SURPRISE = '?'


def _has_spells(setup, answers):
    cls = chargen.find_class(setup.rules, answers.get('class', ''))
    return bool(cls and cls.get('spells_pick'))


# key, label, kind, question, needs. `needs` drives the cascade: editing
# one of those keys puts this one back in question.
STAGES = [
    dict(key='world', label='World', kind='text', needs=(),
         q='What kind of world? A place, a genre, a mood - '
           'or "?" to let the story decide.'),
    dict(key='tone', label='Tone', kind='text', needs=(),
         q='How should it feel? (grim, hopeful, funny, dangerous...) '
           'Or "?" to decide for me.'),
    dict(key='scores', label='Scores', kind='roll', needs=(),
         q='Rolling 4d6, dropping the lowest.'),
    dict(key='race', label='Race', kind='choice', needs=(),
         q='What are you?'),
    dict(key='class', label='Class', kind='choice', needs=('scores', 'race'),
         q='What do you do? (only what your scores allow)'),
    dict(key='skills', label='Skills', kind='multi', needs=('class',),
         q='Pick your trained skills.'),
    dict(key='spells', label='Spells', kind='multi', needs=('class',),
         q='Pick what you know.', applies=_has_spells),
    dict(key='name', label='Name', kind='text', needs=('race', 'class'),
         q='Your name, and how you look. "?" to be named by the story.'),
    dict(key='opening', label='Opening', kind='text', needs=('world',),
         q='Where does it start? Or "?" for the story to choose.'),
]
STAGE_KEYS = [s['key'] for s in STAGES]


class AdventureSetup:
    """One player's trip through the front door."""

    def __init__(self, templates=(), rules=None, rng=None):
        self.rules = rules or chargen.load_rules()
        self.rng = rng
        self.templates = list(templates)
        self.answers = {}
        self.state = 'choose'
        self.stage = 0
        self.theme = ''
        self.editing = False
        self.invalid = set()
        # Stages already answered by a loaded template. They still appear
        # on the review (and can still be edited) but are not asked again
        # on the way through - a re-roll should walk the character, not
        # the world you just chose to keep.
        self.prefilled = set()
        self.template_slug = ''

    # --- stage helpers ------------------------------------------------

    def _stage(self, i=None):
        return STAGES[self.stage if i is None else i]

    def _applies(self, st) -> bool:
        fn = st.get('applies')
        return fn(self, self.answers) if fn else True

    def options(self, st=None):
        """Legal choices for a stage, computed from the rules and what
        has already been decided - so the player is never offered
        something the rules forbid."""
        st = st or self._stage()
        key = st['key']
        if key == 'race':
            return [r['name'] for r in self.rules['races']]
        if key == 'class':
            scores = chargen.final_scores(
                self.rules, self.answers.get('scores') or {},
                self.answers.get('race', ''))
            return [c['name']
                    for c in chargen.eligible_classes(self.rules, scores)]
        cls = chargen.find_class(self.rules, self.answers.get('class', ''))
        if key == 'skills':
            return list((cls or {}).get('skills', []))
        if key == 'spells':
            return list((cls or {}).get('spells', []))
        return []

    def picks_allowed(self, st=None) -> int:
        st = st or self._stage()
        cls = chargen.find_class(self.rules, self.answers.get('class', ''))
        return int((cls or {}).get(
            'skills_pick' if st['key'] == 'skills' else 'spells_pick', 0))

    def shown(self, key) -> str:
        """How a decision reads on the review screen."""
        v = self.answers.get(key)
        if v is None:
            return ''
        if key == 'scores':
            return chargen.fmt_scores(self.rules, chargen.final_scores(
                self.rules, v, self.answers.get('race', '')))
        if isinstance(v, list):
            return ", ".join(v)
        return str(v)

    # --- screens ------------------------------------------------------

    def opening_screen(self) -> str:
        lines = ["Start an adventure:", "",
                 "  1  Surprise me",
                 "  2  I have an idea (one line)",
                 "  3  Build a world and character"]
        if self.templates:
            lines.append("  4  Load a saved world")
        lines += ["", "Reply with a number, or /chat to cancel."]
        return "\n".join(lines)

    def stage_screen(self) -> str:
        st = self._stage()
        head = (f"[change: {st['label']}]" if self.editing
                else f"[step {self.stage + 1} of {len(STAGES)}]")
        body = [head, st['q']]
        if st['kind'] == 'roll':
            base = self.answers.get('scores') or {}
            body.append("")
            body.append("  " + chargen.fmt_scores(self.rules, base))
            body.append("")
            # NOT "press Return": the client drops empty messages
            # (main.c send_message), so every prompt must name a key
            # the player can actually type.
            body.append("r = roll again,  k = keep these")
        elif st['kind'] in ('choice', 'multi'):
            opts = self.options(st)
            body.append("")
            for i, o in enumerate(opts, 1):
                extra = ""
                if st['key'] == 'race':
                    mods = chargen.find_race(self.rules, o)['mods']
                    extra = "   " + " ".join(f"{k}{v:+d}"
                                             for k, v in mods.items())
                body.append(f"  {i}  {o}{extra}")
            if st['kind'] == 'multi':
                body.append("")
                body.append(f"Pick {self.picks_allowed(st)}, "
                            "as numbers: e.g. 1 3")
        return "\n".join(body)

    def review_screen(self) -> str:
        lines = ["Your adventure:", ""]
        n = 0
        for st in STAGES:
            if not self._applies(st):
                continue
            n += 1
            flag = "  ! needs a look" if st['key'] in self.invalid else ""
            lines.append(f"  {n}  {st['label']:9} "
                         f"{self.shown(st['key'])[:42]}{flag}")
        lines += ["", "  y  begin      N  change that      /chat  cancel"]
        if self.invalid:
            lines.append("(Something above changed under a choice you had "
                         "already made - check the flagged lines.)")
        return "\n".join(lines)

    def current_screen(self) -> str:
        if self.state == 'choose':
            return self.opening_screen()
        if self.state == 'stage':
            return self.stage_screen()
        return self.review_screen()

    # --- input --------------------------------------------------------

    def feed(self, text: str):
        text = (text or '').strip()
        if self.state == 'choose':
            return self._choose(text)
        if self.state == 'theme':
            return self.set_theme(text)
        if self.state == 'template':
            return self._pick_template(text)
        if self.state == 'template_mode':
            return self._template_mode(text)
        if self.state == 'stage':
            return self._answer(text)
        return self._review(text)

    def _choose(self, text):
        pick = text[:1]
        if pick == '1':
            return None, ACT_QUICK
        if pick == '2':
            self.state = 'theme'
            return "Give me one line to work from:", ACT_NONE
        if pick == '3':
            self.state = 'stage'
            self.stage = 0
            self._enter()
            return self.stage_screen(), ACT_SUGGEST
        if pick == '4' and self.templates:
            names = "\n".join(f"  {i}  {n}"
                              for i, (n, _slug) in enumerate(self.templates, 1))
            self.state = 'template'
            return f"Saved worlds:\n\n{names}\n\nPick a number:", ACT_NONE
        return ("Pick a number from the list, or /chat to cancel.\n\n"
                + self.opening_screen()), ACT_NONE

    def _pick_template(self, text):
        t = text.strip()
        if t.isdigit() and 1 <= int(t) <= len(self.templates):
            name, slug = self.templates[int(t) - 1]
            self.template_slug = slug
            self.state = 'template_mode'
            return (f"{name}\n\n"
                    "  1  Play it as it was\n"
                    "  2  Keep the world, roll a new character\n\n"
                    "Pick a number:"), ACT_NONE
        return "Pick one by number.", ACT_NONE

    def _template_mode(self, text):
        if text.strip().startswith('1'):
            return None, ACT_LOAD          # caller replays it whole
        if text.strip().startswith('2'):
            return None, ACT_LOAD          # caller pre-fills, then re-rolls
        return "Pick 1 or 2.", ACT_NONE

    def start_reroll(self, saved: dict):
        """Keep the world, build a new character. The world stages are
        pre-filled and skipped on the way through, but still listed on
        the review so they remain editable."""
        bundle = (saved or {}).get('bundle') or {}
        for key in ('world', 'tone', 'opening'):
            if key in bundle:
                self.answers[key] = bundle[key]
                self.prefilled.add(key)
        self.state = 'stage'
        self.stage = STAGE_KEYS.index('scores')
        self._enter()
        return self.stage_screen(), ACT_SUGGEST

    def _enter(self):
        """Anything a stage needs done before it is shown. Only the roll
        has one - the dice are thrown by the proxy, never the model."""
        st = self._stage()
        if st['kind'] == 'roll' and not self.answers.get('scores'):
            self.answers['scores'] = chargen.roll_scores(self.rules, self.rng)

    def _record(self, key, value):
        self.answers[key] = value
        self.invalid.discard(key)
        for st in STAGES:
            if key in st['needs'] and st['key'] in self.answers:
                self.invalid.add(st['key'])
        # A class change can make the spell stage vanish entirely; a
        # stale spell list would then ride along invisibly.
        if key == 'class' and not _has_spells(self, self.answers):
            self.answers.pop('spells', None)
            self.invalid.discard('spells')

    def _answer(self, text):
        st = self._stage()
        key, kind = st['key'], st['kind']

        if kind == 'roll':
            if text[:1].lower() == 'r':
                self.answers['scores'] = chargen.roll_scores(self.rules,
                                                             self.rng)
                return self.stage_screen(), ACT_NONE
            self._record('scores', self.answers.get('scores'))
        elif kind == 'choice':
            opts = self.options(st)
            chosen = self._match_one(text, opts)
            if chosen is None:
                return ("Pick one by number.\n\n"
                        + self.stage_screen()), ACT_NONE
            self._record(key, chosen)
        elif kind == 'multi':
            opts = self.options(st)
            want = self.picks_allowed(st)
            picks = self._match_many(text, opts)
            if len(picks) != want:
                return (f"Pick exactly {want}, as numbers.\n\n"
                        + self.stage_screen()), ACT_NONE
            self._record(key, picks)
        else:
            self._record(key, SURPRISE if text in ('', SURPRISE) else text)

        if self.editing:
            self.editing = False
            self.state = 'review'
            return self.review_screen(), ACT_NONE
        return self._advance()

    def _advance(self):
        self.stage += 1
        while self.stage < len(STAGES) and (
                not self._applies(STAGES[self.stage])
                or STAGES[self.stage]['key'] in self.prefilled):
            self.stage += 1     # spells for a non-caster, or a kept world
        if self.stage < len(STAGES):
            self._enter()
            return self.stage_screen(), ACT_SUGGEST
        self.state = 'review'
        return self.review_screen(), ACT_NONE

    @staticmethod
    def _match_one(text, opts):
        t = text.strip()
        if t.isdigit() and 1 <= int(t) <= len(opts):
            return opts[int(t) - 1]
        for o in opts:
            if o.lower() == t.lower():
                return o
        return None

    @classmethod
    def _match_many(cls, text, opts):
        out = []
        for tok in text.replace(',', ' ').split():
            got = cls._match_one(tok, opts)
            if got and got not in out:
                out.append(got)
        return out

    def _visible(self):
        return [st for st in STAGES if self._applies(st)]

    def _review(self, text):
        if text.lower().startswith('y'):
            if self.invalid:
                return ("Check the flagged lines first.\n\n"
                        + self.review_screen()), ACT_NONE
            return None, ACT_BEGIN
        if text[:2].strip().isdigit():
            n = int(text[:2].strip())
            vis = self._visible()
            if 1 <= n <= len(vis):
                self.stage = STAGES.index(vis[n - 1])
                self.editing = True
                self.state = 'stage'
                self._enter()
                return self.stage_screen(), ACT_SUGGEST
        return ("Reply y to begin, or the number of the line to change.\n\n"
                + self.review_screen()), ACT_NONE

    # --- caller helpers ----------------------------------------------

    def set_theme(self, text):
        self.theme = text.strip()
        return None, ACT_THEME

    def bundle(self) -> dict:
        """What the prep pass and the template are built from. '?' means
        the player asked to be surprised, so it is dropped rather than
        handed on as a literal question mark."""
        return {k: v for k, v in self.answers.items() if v != SURPRISE}

    def character_block(self) -> str:
        """Prose for the stable head of the system prompt (docs/09 §4b)."""
        return chargen.describe(self.rules, self.answers)
