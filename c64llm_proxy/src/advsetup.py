"""The /adventure front door: chooser, staged setup, review loop.

See docs/09-adventure-setup.md. This module is a PURE state machine -
no network, no model, no conversation. feed() takes what the player
typed and returns (reply_text, action), where action tells the caller
what to actually do. Everything the player can reach is therefore
unit-testable without a model or an emulator, which matters because the
interesting behaviour is the edit/cascade rules rather than the prose.

The caller owns: asking the model for suggestions, the prep pass, and
creating the conversation. This owns: what screen you are on, what has
been decided, and what an edit invalidates.
"""

# Actions handed back to the caller
ACT_NONE = 'none'          # just show the reply
ACT_CANCEL = 'cancel'      # player backed out; drop the setup
ACT_QUICK = 'quick'        # option 1: start immediately, no theme
ACT_THEME = 'theme'        # option 2: one-line idea, in .theme
ACT_BEGIN = 'begin'        # review confirmed; bundle in .answers
ACT_SUGGEST = 'suggest'    # caller may offer model suggestions for .stage

# Ordered, and the order is the screen order. `needs` lists the keys a
# stage depends on: editing one of those re-validates this one.
STAGES = [
    ('world', 'World', 'What kind of world? A place, a genre, a mood - '
                       'or "?" to let the story decide.', ()),
    ('tone', 'Tone', 'How should it feel? (grim, hopeful, funny, '
                     'dangerous...) Or "?" to decide for me.', ()),
    ('character', 'Character', 'Who are you? A sentence is plenty, '
                               'or "?" to be surprised.', ('world',)),
    ('opening', 'Opening', 'Where does it start? Or "?" for the story '
                           'to choose.', ('world', 'character')),
]
STAGE_KEYS = [s[0] for s in STAGES]

SURPRISE = '?'


class AdventureSetup:
    """One player's trip through the front door."""

    def __init__(self, templates=()):
        self.templates = list(templates)
        self.answers = {}
        self.state = 'choose'
        self.stage = 0
        self.theme = ''
        # Set while editing a single decision: the review screen is the
        # destination afterwards, not the next stage. This is the whole
        # point of the review loop - change one thing, come back.
        self.editing = False
        self.invalid = set()

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
        key, label, question, _ = STAGES[self.stage]
        head = (f"[change: {label}]" if self.editing
                else f"[step {self.stage + 1} of {len(STAGES)}]")
        return f"{head}\n{question}"

    def review_screen(self) -> str:
        lines = ["Your adventure:", ""]
        for i, (key, label, _, _) in enumerate(STAGES, 1):
            val = self.answers.get(key, '')
            flag = "  ! needs a look" if key in self.invalid else ""
            lines.append(f"  {i}  {label:10} {val[:44]}{flag}")
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
        """(reply_or_None, action). A None reply means the caller should
        show nothing extra - it is about to do something louder."""
        text = (text or '').strip()
        if self.state == 'choose':
            return self._choose(text)
        if self.state == 'theme':
            return self.set_theme(text)
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
            return self.stage_screen(), ACT_SUGGEST
        if pick == '4' and self.templates:
            names = "\n".join(f"  {i}  {t}"
                              for i, t in enumerate(self.templates, 1))
            self.state = 'template'
            return f"Saved worlds:\n\n{names}\n\nPick a number:", ACT_NONE
        return ("Pick a number from the list, or /chat to cancel.\n\n"
                + self.opening_screen()), ACT_NONE

    def _answer(self, text):
        key, label, _, _ = STAGES[self.stage]
        self.answers[key] = SURPRISE if text in ('', SURPRISE) else text
        self.invalid.discard(key)
        # Editing this key may have invalidated things built on top of it
        for k, _l, _q, needs in STAGES:
            if key in needs and k in self.answers:
                self.invalid.add(k)
        if self.editing:
            self.editing = False
            self.state = 'review'
            return self.review_screen(), ACT_NONE
        self.stage += 1
        if self.stage < len(STAGES):
            return self.stage_screen(), ACT_SUGGEST
        self.state = 'review'
        return self.review_screen(), ACT_NONE

    def _review(self, text):
        low = text.lower()
        if low.startswith('y'):
            if self.invalid:
                return ("Check the flagged lines first.\n\n"
                        + self.review_screen()), ACT_NONE
            return None, ACT_BEGIN
        if text[:1].isdigit():
            n = int(text[:2]) if text[:2].isdigit() else int(text[:1])
            if 1 <= n <= len(STAGES):
                self.stage = n - 1
                self.editing = True
                self.state = 'stage'
                return self.stage_screen(), ACT_SUGGEST
        return ("Reply y to begin, or the number of the line to change.\n\n"
                + self.review_screen()), ACT_NONE

    # --- caller helpers ----------------------------------------------

    def set_theme(self, text):
        """Option 2's one-liner arrives on the turn after the prompt."""
        self.theme = text.strip()
        return None, ACT_THEME

    def bundle(self) -> dict:
        """What the prep pass and the template are built from. '?' means
        the player asked to be surprised - the caller leaves that to the
        model rather than inventing a placeholder here."""
        return {k: v for k, v in self.answers.items() if v != SURPRISE}
