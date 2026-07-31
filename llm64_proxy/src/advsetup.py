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

import re

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


def _has_skills(setup, answers):
    """A class the rules do not know (the player typed their own) has no
    skill list to offer. Skipping the stage beats showing an empty menu
    and asking them to pick exactly zero things from it."""
    cls = chargen.find_class(setup.rules, answers.get('class', ''))
    return bool(cls and cls.get('skills'))


# key, label, kind, question, needs. `needs` drives the cascade: editing
# one of those keys puts this one back in question. `intro` is a line
# shown above the question - used once, to say out loud that character
# creation has started, because arriving at a dice roll with no warning
# reads as the setup having skipped a step.
STAGES = [
    dict(key='world', label='World', kind='text', needs=(),
         q='What kind of world? A place, a genre, a mood - '
           'or "?" to let the narrator decide.'),
    dict(key='tone', label='Tone', kind='text', needs=(),
         q='How should it feel? (grim, hopeful, funny, dangerous...) '
           'Or "?" to let the narrator decide.'),
    dict(key='scores', label='Scores', kind='roll', needs=(),
         intro='Now for your character. First you\'ll roll for your '
               'stats, then, when you\'re happy, choose or define a race.',
         q='Rolling 4d6 for each ability, dropping the lowest die.'),
    dict(key='race', label='Race', kind='choice', needs=(),
         q='What are you?'),
    dict(key='class', label='Class', kind='choice', needs=('scores', 'race'),
         q='What do you do? (only what your scores allow)'),
    dict(key='skills', label='Skills', kind='multi', needs=('class',),
         q='Pick your trained skills - what your background taught you.\n'
           '\n'
           'Trained means the narrator leans your way when a roll could go\n'
           'either way, and may not call for one at all when the task is\n'
           'squarely yours. Untrained never stops you trying; it just gets\n'
           'you no help.',
         applies=_has_skills),
    dict(key='spells', label='Spells', kind='multi', needs=('class',),
         q='What spells have you learned?', applies=_has_spells),
    dict(key='gear', label='Gear', kind='spend', needs=('class',),
         q='You prepare for your journey by visiting various supply\n'
           'houses. You walk the shelves and carefully pick the\n'
           'following items.'),
    dict(key='name', label='Name', kind='text', needs=('race', 'class'),
         q='Your name, and how you look. "?" to be named by the narrator.'),
    dict(key='opening', label='Opening', kind='text', needs=('world',),
         q='Where does it start? Or "?" for the narrator to choose.'),
]
STAGE_KEYS = [s['key'] for s in STAGES]

# Stages where the player may ignore the list and type their own answer.
# The rules cannot describe every race anyone will ever want to be, and
# refusing "Automaton" because it is not in a JSON file is the wrong
# answer for a game whose whole point is that a narrator improvises.
CUSTOM_OK = ('race', 'class')

# Multi-pick stages where the player may name their own instead of
# taking the listed ones. Same argument as CUSTOM_OK: a class's skill
# list is a starting point, not the set of things a person can be good
# at, and "my background was smuggling" deserves an answer. Spells are
# deliberately NOT here - a spell list is a balance decision the rules
# own, where a skill is just a claim about who you used to be.
CUSTOM_MULTI_OK = ('skills',)

# Kit-shop nav hints. Item numbers are GLOBAL across the whole catalogue
# (see _gear_cat_body), so they mean the same thing on every screen and a
# player who knows the book can type a whole kit from memory without
# opening a single shelf.
MSG_OVERVIEW = ("One number opens that shelf. Several numbers take those "
                "items straight off the catalogue.")
MSG_SHELF = "Numbers take or put back, or b to go back."


class AdventureSetup:
    """One player's trip through the front door."""

    def __init__(self, templates=(), rules=None, rng=None, width=40):
        self.rules = rules or chargen.load_rules()
        self.rng = rng
        # The client's screen, in columns. Only the review screen reads
        # it: its value column is capped so the "! needs a look" flag is
        # never pushed onto a wrapped line, and the cap has to know how
        # wide a line is. 40 (the C64) is the floor every other client
        # widens from.
        self.width = max(int(width or 40), 40)
        self.templates = list(templates)
        self.answers = {}
        self.state = 'choose'
        self.stage = 0
        self.theme = ''
        self.editing = False
        # A listed race/class the player picked but has not yet confirmed
        # (the "be a Kobold? (Y/n)" gate). None = no pick awaiting a yes.
        self.confirm = None
        self.invalid = set()
        # Stages already answered by a loaded template. They still appear
        # on the review (and can still be edited) but are not asked again
        # on the way through - a re-roll should walk the character, not
        # the world you just chose to keep.
        self.prefilled = set()
        self.template_slug = ''
        # The kit shop's own little state machine (see _spend_feed).
        # None = the category overview, a slug = that list is open,
        # 'own' = the player's own things, 'done' = the approve screen.
        self.cat = None
        self.kit = []        # catalogue items, by name
        self.kit_own = []    # things the player typed
        self.own_at = {}     # own item -> the shelf it was invented on
        self.page = 0        # which page of a long shelf is showing

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
        if key == 'gear':
            return [it['name'] for it in
                    chargen.gear_options(self.rules,
                                         self.answers.get('class', ''))]
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

    def _planned(self):
        """The stages that still count toward "step N of M". A stage
        gated on a decision not yet made counts as planned, so the total
        cannot wobble while the player walks it; once the decision rules
        it out it drops off, which is honest ("that class has no
        spells") rather than confusing.

        This exists because numbering by raw STAGES index made the
        screens read 'step 6' then 'step 8' when a non-caster skipped
        the spell stage, which looks exactly like a setup that lost
        track of itself."""
        out = []
        for st in STAGES:
            decided = all(k in self.answers for k in st['needs'])
            if decided and not self._applies(st):
                continue
            out.append(st)
        return out

    def stage_screen(self) -> str:
        st = self._stage()
        plan = self._planned()
        pos = plan.index(st) + 1 if st in plan else len(plan)
        head = (f"[change: {st['label']}]" if self.editing
                else f"[step {pos} of {len(plan)}]")
        body = [head]
        # Not on an edit: coming back to change one line does not need
        # to be told that character creation is starting.
        if st.get('intro') and not self.editing:
            body += [st['intro'], ""]
        if st['kind'] == 'spend':
            # The question belongs on the overview only. Repeating "What
            # are you carrying?" above every shelf costs a line of wire
            # on each repaint and tells the player nothing they did not
            # just read.
            if self.cat is None:
                body.append(st['q'])
            return "\n".join(body + self._gear_body())
        body.append(st['q'])
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
                    extra = "  " + " ".join(f"{k}{v:+d}"
                                            for k, v in mods.items())
                body.append(f"  {i:2}  {o:11}{extra}".rstrip())
                # The blurb is NOT listed here - the race/class lists ran
                # long enough to scroll the connect text off. It is shown
                # when you pick one, on the confirm screen (_confirm_screen).
            if st['key'] in CUSTOM_OK:
                body.append(f"  {len(opts) + 1:2}  Something else - "
                            "your own idea")
            if st['key'] in CUSTOM_MULTI_OK:
                body.append(f"  {len(opts) + 1:2}  Something else - "
                            "name your own")
            if st['kind'] == 'multi':
                want = self.picks_allowed(st)
                body.append("")
                body.append(f"Pick {want}, as numbers: e.g. 1 3")
                if st['key'] in CUSTOM_MULTI_OK:
                    body.append(f"{len(opts) + 1} = name your own "
                                f"{want} instead")
        return "\n".join(body)

    # --- the kit shop -------------------------------------------------
    #
    # A browsable shop rather than one long list: 75 items do not fit a
    # screen, and a flat list gave no way to see what you had already
    # taken. Categories come from the rules JSON (equipment.categories),
    # so a setting can ship its own shelves without touching this code.
    # Every screen is canned text and numbered replies - zero client
    # bytes - but each one is a full repaint down the wire, so numbers
    # BATCH ("1 3 5" toggles three) rather than costing a round trip
    # each.

    def _gear_conf(self) -> dict:
        return self.rules.get('equipment') or {}

    def _gear_items(self) -> list:
        """The catalogue this character may buy from."""
        return chargen.gear_options(self.rules, self.answers.get('class', ''))

    def _gear_cats(self) -> list:
        """Categories that actually have something in them for this
        class. A Wizard offered an empty 'Armor' shelf learns nothing;
        the shelf simply is not there."""
        items = self._gear_items()
        out = []
        for c in self._gear_conf().get('categories', []):
            kinds = set(c.get('kinds') or [])
            n = [it for it in items if it.get('kind') in kinds]
            if n:
                out.append((c, n))
        return out

    def _gear_numbers(self) -> dict:
        """name -> its number in the WHOLE catalogue.

        Numbering is global rather than per-shelf so a number means one
        thing everywhere: a player who knows the book can type a kit
        straight in ("1 13 45 23 11") without opening a shelf at all,
        and a number remembered from one screen still works on the next.
        Shelf order drives it, so the numbers a shelf shows are always
        contiguous."""
        n, out = 0, {}
        for _c, items in self._gear_cats():
            for it in items:
                n += 1
                out[it['name']] = n
        return out

    def _gear_by_number(self) -> dict:
        """The inverse: number -> item."""
        n, out = 0, {}
        for _c, items in self._gear_cats():
            for it in items:
                n += 1
                out[n] = it
        return out

    def _gear_spent(self) -> int:
        return chargen.gear_cost(self.rules, self.kit + self.kit_own)

    def _gear_left(self) -> int:
        return self._gear_conf().get('points', 6) - self._gear_spent()

    def _gear_purse(self) -> str:
        budget = self._gear_conf().get('points', 6)
        return f"{self._gear_spent()} of {budget} points spent"

    def _gear_body(self) -> list:
        """Whichever kit-shop screen is open."""
        if self.cat == 'done':
            return self._gear_confirm_body()
        if self.cat == 'own':
            return self._gear_own_body()
        if self.cat:
            return self._gear_cat_body(self.cat)
        return self._gear_overview_body()

    def _gear_overview_body(self) -> list:
        cats = self._gear_cats()
        out = ["", self._gear_purse(), ""]
        lo = 1
        for i, (c, items) in enumerate(cats, 1):
            n = len([it for it in items if it['name'] in self.kit])
            hi = lo + len(items) - 1
            out.append(("  %2d  %s items %d-%d %s"
                        % (i, c['label'].ljust(20, '.'), lo, hi,
                           '*' * n)).rstrip())
            lo = hi + 1
        out.append(("  %2d  %s %s" % (len(cats) + 1,
                                      "Your own things".ljust(20, '.'),
                                      len(self.kit_own) or '')).rstrip())
        out += ["",
                "number = open a shelf,  d = done,  x = put it all back",
                "or type item numbers straight in: 1 13 45 23 11"]
        return out

    def _gear_cat_body(self, slug) -> list:
        match = [(c, items) for c, items in self._gear_cats()
                 if c['slug'] == slug]
        if not match:
            self.cat = None
            return self._gear_overview_body()
        c, items = match[0]
        # The chat area is 19 lines and this screen spends 7 on its own
        # furniture, so a long shelf PAGES rather than scrolling off the
        # top - the client appends at the bottom, so an over-long list
        # would push its first items out of sight while leaving the
        # footer visible, which is exactly backwards. Numbers stay
        # ABSOLUTE across pages: item 17 is 17 wherever you are standing,
        # so a player who remembers a number never has to hunt for the
        # page it lives on.
        pages = max(1, (len(items) + self.PAGE - 1) // self.PAGE)
        self.page = min(self.page, pages - 1)
        lo = self.page * self.PAGE
        window = items[lo:lo + self.PAGE]
        nums = self._gear_numbers()
        title = f"{c['label']} - {self._gear_purse()}"
        if pages > 1:
            title += f"   (page {self.page + 1} of {pages})"
        out = ["", title, ""]
        for it in window:
            mark = '+' if it['name'] in self.kit else ' '
            out.append(" %s %2d  %-22s %d  %s"
                       % (mark, nums[it['name']], it['name'], it['cost'],
                          it.get('blurb', '')))
        # Own things belonging to THIS shelf are listed here too, so a
        # cleaver you invented sits with the weapons instead of only
        # showing up two screens away.
        mine = [n for n in self.kit_own if self.own_at.get(n) == c['slug']]
        for name in mine:
            out.append(" +  -  %s" % name)
        # The example is deliberately not "1 3": numbers are catalogue-
        # wide, so a low number typed on a high shelf takes something
        # from another one, and an example implying otherwise would set
        # the wrong expectation.
        nav = "numbers take or put back (any item, 1-%d),  b = back" % len(
            self._gear_items())
        if pages > 1:
            nav = ("numbers take or put back (1-%d),  n = more,  b = back"
                   % len(self._gear_items()))
        out += ["", nav,
                "or just describe your own %s (%d point each)"
                % (c['label'].lower(), self._gear_conf().get(
                    'custom_cost', 1))]
        return out

    def _gear_own_body(self) -> list:
        conf = self._gear_conf()
        cost = conf.get('custom_cost', 1)
        cap = conf.get('custom_max', 6)
        out = ["", f"Your own things - {self._gear_purse()}", ""]
        if self.kit_own:
            for i, name in enumerate(self.kit_own, 1):
                out.append(" + %2d  %s" % (i, name))
        else:
            out.append("  (nothing yet)")
        out += ["",
                f"Type anything to bring it ({cost} point"
                f"{'' if cost == 1 else 's'} each, up to {cap}).",
                "Several at once with +:  a rusty key + my father's coat",
                "number = leave it behind,  b = back"]
        return out

    def _gear_confirm_body(self) -> list:
        carried = self.kit + self.kit_own
        out = ["", self._gear_purse(), ""]
        if carried:
            for name in carried:
                out.append(f"  {name}")
        else:
            out.append("  Nothing at all. Travelling light.")
        out += ["", "y = that is my kit,  b = back to the shelves"]
        return out

    def review_screen(self) -> str:
        lines = ["Your adventure:", ""]
        n = 0
        # The value column's cap. 15 is the row prefix, 16 the flag; what
        # is left is what a value can use without pushing the flag onto a
        # wrapped line. Floored at the old 42 so the C64's screen (where
        # rows wrap regardless) reads exactly as it always has - the cap
        # only GROWS, for clients wide enough to show what the C64 could
        # not: an 80-column screen was cutting "STR .. CHA 15" off at
        # "CH" for no reason its user could see.
        vw = max(self.width - 15 - 16, 42)
        for st in STAGES:
            if not self._applies(st):
                continue
            n += 1
            flag = "  ! needs a look" if st['key'] in self.invalid else ""
            lines.append(f"  {n}  {st['label']:9} "
                         f"{self.shown(st['key'])[:vw]}{flag}")
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
        # Control characters are never typed on purpose. One arrived as
        # 0x1F - a Windows client's failed Ctrl+_ undo - and lived on
        # INSIDE a character's name for a whole adventure. Tabs and
        # newlines become spaces (the Windows client can send multiline
        # now); the rest are dropped.
        text = ''.join(' ' if ch in '\t\n\r' else ch
                       for ch in (text or '') if ch >= ' ' or ch in '\t\n\r')
        text = text.strip()
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
        """Anything a stage needs done before it is shown: the dice are
        thrown by the proxy, never the model, and the kit shop opens on
        its overview holding whatever was already chosen (so editing gear
        from the review resumes the kit rather than starting bare)."""
        st = self._stage()
        if st['kind'] == 'roll' and not self.answers.get('scores'):
            self.answers['scores'] = chargen.roll_scores(self.rules, self.rng)
        if st['kind'] == 'spend':
            self.cat = None
            catalogue = {it['name'] for it in self._gear_items()}
            chosen = self.answers.get(st['key']) or []
            self.kit = [n for n in chosen if n in catalogue]
            self.kit_own = [n for n in chosen if n not in catalogue]
            # Which shelf each own item came from is display-only and
            # is not persisted in the answer, so a resumed kit shows
            # them all under 'Your own things' rather than guessing.
            self.own_at = {n: 'own' for n in self.kit_own}
            self.page = 0

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
        if self.confirm is not None:
            return self._answer_confirm(text)
        st = self._stage()
        key, kind = st['key'], st['kind']

        if kind == 'roll':
            if text[:1].lower() == 'r':
                self.answers['scores'] = chargen.roll_scores(self.rules,
                                                             self.rng)
                return self.stage_screen(), ACT_NONE
            self._record('scores', self.answers.get('scores'))
        elif kind == 'spend':
            done, err, redraw = self._spend_feed(text)
            if not done:
                # Adding an invented item does NOT redraw: repainting a
                # 20-line shelf down a 9600 baud wire to show one new
                # line is the slowest possible way to say "noted".
                parts = [x for x in (err, self.stage_screen()
                                     if redraw else None) if x]
                return "\n\n".join(parts), ACT_NONE
            self._record(key, self.kit + self.kit_own)
        elif kind == 'choice':
            opts = self.options(st)
            chosen = self._match_one(text, opts)
            if chosen is None:
                custom = self._custom(text, opts, st)
                if custom is None:
                    return ("Pick one by number.\n\n"
                            + self.stage_screen()), ACT_NONE
                if not custom:
                    # They picked "something else" by its number: ask for
                    # the words, staying on this stage. No new state -
                    # the re-roll key already works exactly this way.
                    return (f"Your own {st['label'].lower()}, then: a "
                            "name, and a few words about what it is."), \
                        ACT_NONE
                # A typed custom answer is its own confirmation - the
                # player wrote it out, so there is nothing to double-check.
                self._record(key, custom)
            else:
                # A listed race/class waits for a yes: show what it is and
                # gate on "(Y/n)" so a misfired number is easy to undo.
                self.confirm = chosen
                return self._confirm_screen(key, chosen), ACT_NONE
        elif kind == 'multi':
            opts = self.options(st)
            want = self.picks_allowed(st)
            # Listed picks win the ambiguity: typing the NAMES of listed
            # skills must stay a listed pick rather than being read as
            # someone inventing skills that happen to already exist.
            picks = self._match_many(text, opts)
            if len(picks) == want:
                self._record(key, picks)
            else:
                own = self._custom_multi(text, opts, st)
                if own is None:
                    return (f"Pick exactly {want}, as numbers.\n\n"
                            + self.stage_screen()), ACT_NONE
                if not own:
                    # Picked the "name your own" line by its number: ask
                    # for the words and stay put, exactly as the custom
                    # race/class line does.
                    return (f"Your own {want}, then - name them, "
                            "separated by commas."), ACT_NONE
                if len(own) != want:
                    return (f"Name exactly {want}, separated by commas.\n\n"
                            + self.stage_screen()), ACT_NONE
                self._record(key, own)
        else:
            if key == 'name':
                text = self._name_or_delegate(text)
            self._record(key, SURPRISE if text in ('', SURPRISE) else text)

        # Say what they just chose, in words, before the next question:
        # a bare list of names teaches nothing about what a Lizardfolk
        # Ranger actually IS, and this is the only moment the player is
        # looking at that decision.
        note = self._flavour(key)
        if self.editing:
            self.editing = False
            self.state = 'review'
            return self._with(note, self.review_screen()), ACT_NONE
        reply, act = self._advance()
        return self._with(note, reply), act

    # "You can name my character" is a request, not a name - but it was
    # stored verbatim once, and a whole session's system prompt then
    # insisted the player character was called a full sentence. Any of
    # these phrasings means "the narrator decides", which is exactly
    # what SURPRISE already does.
    _DELEGATE = ('you name', 'you can name', 'you pick', 'you choose',
                 'you decide', 'name me', 'name my', 'name her',
                 'name him', 'name them', 'up to you', 'surprise me',
                 "dealer's choice", "don't care", 'dont care',
                 'narrator')

    def _name_or_delegate(self, text):
        low = ' '.join(text.lower().split())
        if any(p in low for p in self._DELEGATE):
            return SURPRISE
        return text

    @staticmethod
    def _with(note, screen):
        return f"{note}\n\n{screen}" if note else screen

    def _confirm_screen(self, key, name) -> str:
        """The 'be a Kobold? (Y/n)' gate. This is the ONE place the blurb
        is shown (it is off the list now), so colour the name to make the
        choice pop."""
        blurb = chargen.blurb(self.rules, key, name)
        art = 'an' if name[:1].lower() in 'aeiou' else 'a'
        head = f"[color=yellow]{name}[/color]"
        line = f"{head}: {blurb}" if blurb else head
        return f"{line}\n\nDo you want to be {art} {name}? (Y/n)"

    def _answer_confirm(self, text):
        """Resolve the (Y/n) gate. 'n' drops the pick and re-shows the
        list; anything else commits - Y is the default (it is capital in
        the prompt) and the client cannot send a bare Return, so there is
        no empty answer to treat as yes."""
        key = self._stage()['key']
        if (text or '').strip().lower() in ('n', 'no'):
            self.confirm = None
            return self.stage_screen(), ACT_NONE
        chosen = self.confirm
        self.confirm = None
        self._record(key, chosen)
        note = f"[color=yellow]{chosen}[/color] it is."
        if self.editing:
            self.editing = False
            self.state = 'review'
            return self._with(note, self.review_screen()), ACT_NONE
        reply, act = self._advance()
        return self._with(note, reply), act

    def _flavour(self, key) -> str:
        """One line confirming a choice, in the manual's voice."""
        value = self.answers.get(key)
        if key in ('race', 'class'):
            blurb = chargen.blurb(self.rules, key, value)
            if blurb:
                return f"{value}: {blurb}"
            # Something the rules have never heard of: say plainly that
            # no modifiers were applied, rather than letting the player
            # wonder why their scores did not move.
            return (f"{str(value)[:60]} it is - the narrator will make "
                    "sense of it. (No rule modifiers for that one.)")
        if key == 'gear' and value:
            gear = self.rules.get('equipment') or {}
            left = gear.get('points', 6) - chargen.gear_cost(self.rules,
                                                             value)
            tail = f" ({left} point{'' if left == 1 else 's'} unspent)" \
                if left > 0 else ""
            return "You carry: " + ", ".join(value) + tail + "."
        return ''

    def _custom(self, text, opts, st) -> str:
        """'' = they asked for the custom line by number (prompt them),
        a string = the custom answer itself, None = not a custom answer
        at all (a bad number, which must still be refused)."""
        t = text.strip()
        if st['key'] not in CUSTOM_OK or not t:
            return None
        if t.isdigit():
            return '' if int(t) == len(opts) + 1 else None
        return t[:60]

    def _custom_multi(self, text, opts, st):
        """The multi-pick counterpart of _custom: [] = they asked for the
        'name your own' line by number (prompt them), a list of names =
        the answer itself, None = not a custom answer at all (a bad
        number, which must still be refused).

        Any digit anywhere disqualifies the text: '1 9' is a misfired
        numeric pick, not a skill called '1 9', and silently accepting it
        as a custom answer would bury the typo in the character sheet."""
        t = (text or '').strip()
        if st['key'] not in CUSTOM_MULTI_OK or not t:
            return None
        if t.isdigit():
            return [] if int(t) == len(opts) + 1 else None
        if any(ch.isdigit() for ch in t):
            return None
        names, seen = [], set()
        for part in re.split(r'[,;]|\band\b', t):
            p = ' '.join(part.split())[:24]
            if p and p.lower() not in seen:
                seen.add(p.lower())
                names.append(p)
        return names or None

    def _spend_feed(self, text):
        """One keystroke-batch through the kit shop.

        Returns (done, message, redraw). done=True only when the
        player has approved the kit on the confirm screen; redraw=False
        asks the caller NOT to repaint, which is what makes typing an
        invented item cheap."""
        t = (text or '').strip()
        low = t.lower()
        if self.cat == 'done':
            if low.startswith('y'):
                return True, None, True
            self.cat = None
            return False, None, True
        if self.cat is None:
            return self._spend_overview(t, low)
        if self.cat == 'own':
            return self._spend_own(t, low)
        return self._spend_category(t, low)

    def _spend_overview(self, t, low):
        cats = self._gear_cats()
        if low == 'd':
            self.cat = 'done'
            return False, None, True
        if low == 'x':
            self.kit, self.kit_own, self.own_at = [], [], {}
            return False, "Back on the shelves.", True
        toks = t.replace(',', ' ').split()
        if toks and all(tok.isdigit() for tok in toks):
            # ONE number is navigation, several are a shopping list.
            # "open shelf 3 and shelf 7" means nothing, so a multi-number
            # answer can only be items - which is what lets a player who
            # knows the book type a whole kit without opening anything.
            if len(toks) == 1:
                n = int(toks[0])
                if 1 <= n <= len(cats):
                    self.cat = cats[n - 1][0]['slug']
                    self.page = 0
                    return False, None, True
                if n == len(cats) + 1:
                    self.cat = 'own'
                    return False, None, True
            return self._toggle(toks)
        if toks:
            # Words at the overview are an invented item too, filed under
            # Your own things since no shelf was open to claim it.
            return self._add_own(t, 'own')
        return False, MSG_OVERVIEW, True

    PAGE = 12            # items per shelf screen (19-line chat area)

    def _spend_category(self, t, low):
        if low == 'b':
            self.cat = None
            self.page = 0
            return False, None, True
        if low in ('n', 'p'):
            match = [items for c, items in self._gear_cats()
                     if c['slug'] == self.cat]
            pages = max(1, (len(match[0]) + self.PAGE - 1) // self.PAGE) \
                if match else 1
            self.page = ((self.page + (1 if low == 'n' else -1))
                         % pages)
            return False, None, True
        match = [(c, items) for c, items in self._gear_cats()
                 if c['slug'] == self.cat]
        if not match:
            self.cat = None
            return False, None, True
        toks = t.replace(',', ' ').split()
        if not toks:
            return False, MSG_SHELF, True
        if not all(tok.isdigit() for tok in toks):
            # Words on a shelf mean "I want one of these, but mine":
            # numbers already mean toggle, so there is nothing to
            # disambiguate and no extra keystroke to reach it.
            return self._add_own(t, self.cat)
        return self._toggle(toks)

    def _toggle(self, toks):
        """Take or put back by catalogue number. Numbers are global, so
        this is deliberately NOT limited to the open shelf: typing 45
        while standing in Weapons takes item 45 wherever it lives, which
        is the whole point of numbering the book rather than the page.

        Dropping is always allowed; taking is checked against the purse
        ONE AT A TIME so a batch that only partly fits still does what it
        can and says plainly what it could not - refusing the lot would
        make the player work out which item was the problem."""
        book = self._gear_by_number()
        refused = []
        for tok in toks:
            n = int(tok)
            it = book.get(n)
            if it is None:
                refused.append(tok[:12])
                continue
            if it['name'] in self.kit:
                self.kit.remove(it['name'])
            elif it['cost'] > self._gear_left():
                refused.append(f"{it['name']} needs {it['cost']}")
            else:
                self.kit.append(it['name'])
        if refused:
            return False, ("No room for " + "; ".join(refused)
                           + f" ({self._gear_left()} left)."), True
        return False, None, True

    def _spend_own(self, t, low):
        """Numbers remove, words add. Unambiguous because a thing you
        bring along is described, never numbered - the same split the
        custom-skills line uses."""
        conf = self._gear_conf()
        cost = conf.get('custom_cost', 1)
        cap = conf.get('custom_max', 6)
        if low == 'b':
            self.cat = None
            return False, None, True
        if t.isdigit():
            n = int(t)
            if 1 <= n <= len(self.kit_own):
                self.own_at.pop(self.kit_own.pop(n - 1), None)
                return False, None, True
            return False, "No such thing on the list.", True
        toks = t.replace(',', ' ').split()
        if toks and all(tok.isdigit() for tok in toks):
            for name in [self.kit_own[int(x) - 1] for x in sorted(
                    {int(x) for x in toks}, reverse=True)
                    if 1 <= int(x) <= len(self.kit_own)]:
                self.kit_own.remove(name)
                self.own_at.pop(name, None)
            return False, None, True
        return self._add_own(t, 'own')

    def _add_own(self, text, slug):
        """Bring along something the catalogue does not stock.

        EVERY '+' starts a new item, not just the first. Splitting on
        only the first one turned "rope + a lucky coin" into a single
        item named "rope + a lucky coin", charged once and cut off at
        40 characters - wrong, and silently so."""
        conf = self._gear_conf()
        cost = conf.get('custom_cost', 1)
        cap = conf.get('custom_max', 6)
        added, refused = [], []
        for part in text.split('+'):
            name = ' '.join(part.split())[:40]
            if not name:
                continue
            if name in self.kit_own:
                continue
            if len(self.kit_own) >= cap:
                refused.append("you can only carry so much")
                break
            if cost > self._gear_left():
                refused.append(f"no points left for \"{name}\"")
                break
            self.kit_own.append(name)
            self.own_at[name] = slug
            added.append(name)
        if refused:
            return False, refused[0].capitalize() + ".", True
        if not added:
            return False, "Describe it, or b to go back.", True
        # No repaint. The whole point of typing an item is that it is
        # quick, and redrawing the shelf to show one added line is the
        # slowest possible acknowledgement - so the confirmation carries
        # the purse instead, which is the only thing that changed.
        return False, ("Taken: " + ", ".join(added)
                       + f".  ({self._gear_purse()})"), False

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

    def character_sheet(self) -> dict:
        """The same character, still structured. The prose above is what
        the model reads; this is what a client with a sheet window draws,
        and what the conversation stores so a /load gets the character
        back instead of an empty sidebar."""
        return chargen.sheet(self.rules, self.answers)
