"""The setup wizard's window.

A guided pass over the same config.toml the Settings tab edits, in the
order the pieces have to happen, with each step checked against the live
system rather than against the text in the file. setupwiz.py holds the
steps, the checks and the file handling; everything here is widgets.

Two rules shape the design:

- Every check runs on a worker thread and comes back through a queue.
  A wizard that freezes for three minutes while a cold llama.cpp loads
  a model looks broken, and the step it is stuck on is precisely the
  one people are least sure about.

- Leaving a step writes it. The wizard owns config.toml while it is
  open, so an abandoned run still leaves the steps that were finished
  on disk, and coming back later resumes rather than restarts. That is
  the same promise the music and printing steps make out loud - go do
  the part that is not in this window, then reopen it.

  Which is why the buttons say what they do. There is no closing this
  window without saving, so the one that closes it is "Save and close";
  Cancel is a separate, deliberate act that puts every file back the
  way it was when the window opened (setupwiz.Rollback).

The launcher passes itself in as `owner` so the last step can restart
the server; nothing else here reaches back into it, and this module
never imports launcher (that arrow points the other way).
"""

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from . import discovery
from . import setupwiz
from .setupwiz import STEPS

logger = logging.getLogger(__name__)

POLL_MS = 200
WRAP = 620                 # text column width, in pixels
PICTURE_W, PICTURE_H = 320, 200

# Steps whose check is cheap enough to run the moment the page opens.
# The LLM probe is not on the list: it costs a real completion, and on a
# local server it can take minutes, so it stays on a button.
AUTO_CHECK = ('config', 'network', 'images', 'music', 'printing', 'claude')

MARKS = {True: '✓', False: '!', None: '·'}


# --------------------------------------------------------------------------
# Small widget helpers, shared with launcher.py (which imports them from
# here rather than keeping a second copy).

def scroll_host(parent):
    """A vertically scrolling frame filling `parent`. Returns (inner
    frame, wheel handler) - the handler has to be bound to each child as
    it is built, because a child with its own bindings swallows the
    wheel event before the canvas sees it.

    Pack the fixed furniture (a button row) BEFORE calling this: the
    canvas takes every pixel the earlier packs left.
    """
    # tk.Canvas takes its color from the X defaults, not the ttk theme,
    # so an unstyled one is a dark slab wherever the content does not
    # reach the bottom of the page. Borrow the color it is sitting on.
    canvas = tk.Canvas(parent, highlightthickness=0,
                       background=ttk.Style().lookup('TFrame', 'background'))
    vsb = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side='right', fill='y')
    canvas.pack(side='top', fill='both', expand=True)
    inner = ttk.Frame(canvas, padding=8)
    inner_id = canvas.create_window((0, 0), window=inner, anchor='nw')
    inner.bind('<Configure>', lambda e: canvas.configure(
        scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda e: canvas.itemconfigure(
        inner_id, width=e.width))

    def _wheel(event):
        if event.num == 4 or event.delta > 0:
            canvas.yview_scroll(-3, 'units')
        else:
            canvas.yview_scroll(3, 'units')
    for w in (canvas, inner):
        w.bind('<MouseWheel>', _wheel)
        w.bind('<Button-4>', _wheel)
        w.bind('<Button-5>', _wheel)
    return inner, _wheel


def bind_wheel(widget, wheel):
    """Hand a page's wheel handler down a finished subtree.

    Skipped: text boxes, which scroll themselves, and the value widgets
    whose Tk bindings read the wheel as "change me" - a scroll over a
    combobox must not silently pick a different backend.
    """
    for child in widget.winfo_children():
        if not isinstance(child, (tk.Text, ttk.Combobox, ttk.Spinbox,
                                  ttk.Scrollbar)):
            child.bind('<MouseWheel>', wheel)
            child.bind('<Button-4>', wheel)
            child.bind('<Button-5>', wheel)
        bind_wheel(child, wheel)


def auto_wrap(label, margin=16):
    """Keep a label's text wrapped to the width it actually has.

    A fixed wraplength is a guess about the window, and this window is
    mostly prose: too small wastes half the page, too large runs the
    explanation off the right edge, which is where the person reading it
    is least able to guess what it said. Watching the parent costs one
    binding and is always right - including for the help under a field,
    which starts partway across its box and so has less room than the
    paragraph above it.

    Bound to the parent, not the label: a label's own size changes when
    the wrap does, and it would chase its own tail.
    """
    tries = [0]

    def resize(_event=None):
        if not label.winfo_exists():
            return
        # Measured now, not taken off the event: a label gridded into
        # the value column of a form starts partway across its parent,
        # and winfo_x is only true once the geometry manager has placed
        # it - which may be after the Configure that brought us here.
        width = label.master.winfo_width()
        if width <= 1:
            tries[0] += 1
            if tries[0] < 20:
                label.after(50, resize)
            return
        want = max(200, width - label.winfo_x() - margin)
        if label.cget('wraplength') != want:
            label.configure(wraplength=want)
    label.master.bind('<Configure>', resize, add='+')
    label.bind('<Map>', resize, add='+')
    label.after_idle(resize)
    return label


def photo_image(master, img, box=None):
    """A tk image from a PIL one. Encoded as PNG and handed to Tk's own
    decoder rather than going through PIL.ImageTk: one fewer optional
    Pillow component to be missing from a frozen build, and Tk has read
    PNG since 8.6."""
    import base64
    import io
    from PIL import Image
    if box and (img.width > box[0] or img.height > box[1]):
        img = img.copy()
        img.thumbnail(box, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return tk.PhotoImage(
        data=base64.b64encode(buf.getvalue()).decode('ascii'), master=master)


# --------------------------------------------------------------------------

class SetupWizard(tk.Toplevel):

    def __init__(self, master, config_path, owner=None):
        super().__init__(master)
        self.title('LLM64 proxy - setup')
        self.geometry('960x720')
        self.minsize(820, 560)
        self.owner = owner
        self.transient(master)

        self.rollback = setupwiz.Rollback()
        self.cfg = setupwiz.ConfigDoc(config_path)
        self.rollback.note(self.cfg.path)
        self.index = 0
        self.results = {}        # step key -> setupwiz.Result
        self.vars = {}           # (section, key) -> (tk var, field type)
        self._queue = queue.Queue()
        self._busy = 0           # checks in flight; buttons disable above 0
        self._page_buttons = []
        self._check_widgets = None
        self._photos = {}        # tk images the page is showing
        self._backend_box = None
        self._close_hidden = False
        # Set when something wrote to the document outside a field
        # widget, so _commit still knows the file needs writing.
        self._doc_dirty = False

        self._build_ui()
        self._show(0)
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(POLL_MS, self._tick)
        if self.cfg.error:
            messagebox.showwarning(
                'config.toml does not parse',
                f'{self.cfg.error}\n\nThe wizard is starting from an empty '
                f'document. Saving from here would throw away what is in '
                f'the file, so close this window and fix the file in the '
                f'launcher\'s Raw config tab instead.', parent=self)

    # ---- layout ----

    def _build_ui(self):
        head = ttk.Frame(self, padding=(12, 10, 12, 6))
        head.pack(fill='x')
        ttk.Label(head, text='Set up the LLM64 proxy',
                  font=('TkDefaultFont', 13, 'bold')).pack(side='left')
        self.head_note = ttk.Label(head, text='', foreground='#777')
        self.head_note.pack(side='right')

        foot = ttk.Frame(self, padding=(12, 6, 12, 10))
        foot.pack(side='bottom', fill='x')
        # Named for what it does. Leaving a step writes it, so there is
        # no such thing as closing this window without saving - only
        # Cancel undoes, and it says so on the tin.
        self.btn_cancel = ttk.Button(foot, text='Cancel',
                                     command=self._cancel)
        self.btn_cancel.pack(side='right')
        self.btn_close = ttk.Button(foot, text='Save and close',
                                    command=self._on_close)
        self.btn_close.pack(side='right', padx=6)
        self.btn_next = ttk.Button(foot, text='Next', command=self._next)
        self.btn_next.pack(side='right', padx=(0, 6))
        self.btn_back = ttk.Button(foot, text='Back', command=self._back)
        self.btn_back.pack(side='right')
        self.status = ttk.Label(foot, text='', foreground='#777')
        self.status.pack(side='left')

        body = ttk.Frame(self, padding=(12, 0, 12, 0))
        body.pack(fill='both', expand=True)

        rail = ttk.Frame(body, width=190)
        rail.pack(side='left', fill='y')
        rail.pack_propagate(False)
        self.rail = ttk.Treeview(rail, show='tree', selectmode='browse')
        self.rail.pack(fill='both', expand=True)
        self.rail.tag_configure('optional', foreground='#777')
        for i, step in enumerate(STEPS):
            self.rail.insert('', 'end', iid=str(i), text=step.title,
                             tags=() if step.required else ('optional',))
        self.rail.bind('<<TreeviewSelect>>', self._on_rail_select)

        page = ttk.Frame(body)
        page.pack(side='left', fill='both', expand=True, padx=(10, 0))
        self.page, self.page_wheel = scroll_host(page)

    # ---- navigation ----

    def _step(self):
        return STEPS[self.index]

    def _on_rail_select(self, _event):
        sel = self.rail.selection()
        if sel and int(sel[0]) != self.index:
            self._show(int(sel[0]))

    def _back(self):
        if self.index > 0:
            self._show(self.index - 1)

    def _next(self):
        if self.index + 1 < len(STEPS):
            self._show(self.index + 1)
        else:
            self._finish()

    def _show(self, index):
        """Leave the current step (writing what it changed) and draw
        another one."""
        if self.vars:
            self._commit()
        self.index = index
        step = self._step()
        self.vars = {}
        self._page_buttons = []
        self._check_widgets = None
        self._backend_box = None
        self._photos.clear()
        for child in self.page.winfo_children():
            child.destroy()

        self._para(f'{index + 1}. {step.title}',
                   font=('TkDefaultFont', 12, 'bold'), pad=(0, 0, 0, 6))
        if not step.required:
            self._para('Optional - the proxy runs without this.',
                       color='#777', pad=(0, 0, 0, 6))
        self._para(step.intro)
        if step.fields:
            self._fields_box(step.fields)
        builder = getattr(self, f'_page_{step.key}', None)
        if builder:
            builder(step)
        if step.outro:
            self._para(step.outro, color='#555', pad=(0, 10, 0, 0))
        if step.revisit:
            self._para(step.revisit, color='#555', pad=(0, 10, 0, 0))

        self.btn_back.state(['disabled'] if index == 0 else ['!disabled'])
        last = index == len(STEPS) - 1
        self.btn_next.configure(text='Finish' if last else 'Next')
        # Finish is Save and close plus the marker that stops the wizard
        # opening itself, so on the last page the plain one is a second
        # button for almost the same thing. It goes away there rather
        # than inviting the comparison.
        if last and not self._close_hidden:
            self.btn_close.pack_forget()
            self._close_hidden = True
        elif self._close_hidden and not last:
            # after=, not before=: with side='right' the first widget
            # packed is the rightmost, so re-packing before Cancel would
            # put it back on the wrong side of it.
            self.btn_close.pack(after=self.btn_cancel, side='right',
                                padx=6)
            self._close_hidden = False
        self.head_note.configure(text=str(self.cfg.path))
        if self.rail.selection() != (str(index),):
            self.rail.selection_set(str(index))
        self.rail.see(str(index))
        self._refresh_rail()
        bind_wheel(self.page, self.page_wheel)
        if step.key in AUTO_CHECK and not self.cfg.error:
            self._run_check(step.key)

    def _refresh_rail(self):
        for i, step in enumerate(STEPS):
            mark = MARKS[getattr(self.results.get(step.key), 'ok', None)]
            self.rail.item(str(i), text=f'{mark}  {step.title}')

    # ---- page furniture ----

    def _para(self, text, color=None, font=None, pad=(0, 0, 0, 8),
              parent=None):
        lbl = ttk.Label(parent or self.page, text=text, wraplength=WRAP,
                        justify='left')
        if color:
            lbl.configure(foreground=color)
        if font:
            lbl.configure(font=font)
        lbl.pack(anchor='w', padx=(pad[0], 0),
                 pady=(pad[1], pad[3]) if len(pad) == 4 else pad)
        return auto_wrap(lbl)

    def _fields_box(self, fields, parent=None, title='Settings'):
        box = ttk.LabelFrame(parent or self.page, text=title, padding=8)
        box.pack(fill='x', pady=(4, 8))
        box.columnconfigure(1, weight=1)
        for row, spec in enumerate(fields):
            self._field_row(box, row, spec)
        return box

    def _field_row(self, box, row, spec):
        """One (section, key, type, label, help) rendered into `box`.

        The type vocabulary is the launcher's - str | secret | int |
        float | bool:<default> | choice:a,b,c | pick:<source> - so a
        field means the same thing in both windows.
        """
        section, key, ftype, label, help_text = spec
        raw = self.cfg.get(section, key)
        ttk.Label(box, text=label).grid(row=row * 2, column=0, sticky='w',
                                        padx=(0, 8))
        if ftype.startswith('bool'):
            default = ftype.split(':')[1] == 'true'
            var = tk.BooleanVar(value=bool(raw) if raw is not None
                                else default)
            widget = ttk.Checkbutton(box, variable=var)
        elif ftype.startswith('choice:'):
            var = tk.StringVar(value='' if raw is None else str(raw))
            widget = ttk.Combobox(box, textvariable=var, state='readonly',
                                  values=[''] + ftype.split(':')[1].split(','))
        elif ftype.startswith('pick:'):
            var = tk.StringVar(value='' if raw is None else str(raw))
            widget = ttk.Combobox(box, textvariable=var)
            source = ftype.split(':')[1]
            btn = ttk.Button(box, text='↻', width=3)
            btn.configure(command=lambda s=source, c=widget, b=btn:
                          self._discover(s, c, b))
            btn.grid(row=row * 2, column=2, padx=(4, 0))
            self._page_buttons.append(btn)
        else:
            var = tk.StringVar(value='' if raw is None else str(raw))
            widget = ttk.Entry(box, textvariable=var,
                               show='*' if ftype == 'secret' else '')
        widget.grid(row=row * 2, column=1, sticky='ew')
        if help_text:
            hint = ttk.Label(box, text=help_text, foreground='#777',
                             wraplength=WRAP, justify='left',
                             font=('TkDefaultFont', 8))
            hint.grid(row=row * 2 + 1, column=1, columnspan=2, sticky='w',
                      pady=(0, 4))
            auto_wrap(hint)
        # The initial value goes along for the ride so _commit can leave
        # untouched fields entirely alone. Rewriting a key with the value
        # it already had is not free in tomlkit: the item is replaced and
        # any comment sitting on it goes with the old one.
        self.vars[(section, key)] = (var, ftype, var.get())

    def _check_box(self, buttons, title='Check'):
        """The status block every checked step ends with: the buttons
        that run something, one line of verdict, and the detail that
        says what to do about it."""
        box = ttk.LabelFrame(self.page, text=title, padding=8)
        box.pack(fill='x', pady=(4, 8))
        row = ttk.Frame(box)
        row.pack(fill='x')
        for label, command in buttons:
            btn = ttk.Button(row, text=label, command=command)
            btn.pack(side='left', padx=(0, 6))
            self._page_buttons.append(btn)
        status = ttk.Label(row, text='not checked yet', foreground='#777')
        status.pack(side='left', padx=(6, 0))
        detail = tk.Text(box, height=5, wrap='word', state='disabled',
                         font=('TkDefaultFont', 9), relief='flat',
                         background=ttk.Style().lookup('TFrame', 'background'))
        detail.pack(fill='x', pady=(6, 0))
        self._check_widgets = (status, detail)
        self._paint_result(self.results.get(self._step().key))
        return box

    def _paint_result(self, result):
        if not self._check_widgets:
            return
        status, detail = self._check_widgets
        if result is None:
            status.configure(text='not checked yet', foreground='#777')
            text = ''
        else:
            color = {True: '#227722', False: '#cc2222'}.get(result.ok,
                                                             '#777')
            status.configure(text=f'{MARKS[result.ok]} {result.summary}',
                             foreground=color)
            text = result.detail
        detail.configure(state='normal')
        detail.delete('1.0', 'end')
        detail.insert('1.0', text)
        detail.configure(state='disabled')

    # ---- values ----

    def _value(self, section, key, env=None, default=''):
        """What the wizard has for a field right now: the widget if this
        page owns it, otherwise the file, then an environment variable,
        then a default - the same precedence the server applies."""
        entry = self.vars.get((section, key))
        if entry is not None:
            val = entry[0].get()
            val = val.strip() if isinstance(val, str) else val
        else:
            val = self.cfg.get(section, key, '')
            val = str(val).strip() if val is not None else ''
        if not val and env:
            val = os.environ.get(env, '').strip()
        return val or default

    def _typed(self, section, key, ftype, value, complain=True):
        """A widget value as the config should hold it, or the string
        unchanged when it should have been a number and is not."""
        if ftype.startswith('bool'):
            return bool(value)
        value = value.strip() if isinstance(value, str) else value
        if value == '' or ftype not in ('int', 'float'):
            return value
        try:
            return int(value) if ftype == 'int' else float(value)
        except ValueError:
            if complain:
                messagebox.showerror(
                    'Not a number',
                    f'{section}.{key} is {value!r}, which is not a number. '
                    f'It has been left as it was.', parent=self)
            return None

    def _commit(self):
        """Fold this page's widgets into the document and write it out
        if anything actually changed."""
        dirty = self._doc_dirty
        for (section, key), (var, ftype, initial) in self.vars.items():
            if var.get() == initial:
                continue
            value = self._typed(section, key, ftype, var.get())
            if value is None:
                continue
            self.cfg.set(section, key, value)
            dirty = True
        if not dirty:
            return
        problem = self.cfg.save()
        if problem:
            messagebox.showerror('Could not save', problem, parent=self)
            return
        self._doc_dirty = False
        self._say(f'Saved {self.cfg.path}')

    def _say(self, text):
        self.status.configure(text=text)

    # ---- background work ----

    def _spawn(self, kind, step_key, fn, *args):
        """Run one blocking check off the UI thread. Everything comes
        back through the queue; nothing in the worker touches a widget."""
        self._busy += 1
        self._set_buttons(False)
        if kind == 'result':
            self._say('Checking...')

        def work():
            try:
                payload = fn(*args)
            except Exception as e:                  # a check must not
                payload = e                         # take the window down
            self._queue.put((kind, step_key, payload))
        threading.Thread(target=work, name='llm64-wizard',
                         daemon=True).start()

    def _set_buttons(self, enabled):
        for btn in self._page_buttons:
            if btn.winfo_exists():
                btn.state(['!disabled'] if enabled else ['disabled'])

    def _tick(self):
        while True:
            try:
                kind, step_key, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            self._busy = max(0, self._busy - 1)
            handler = {'result': self._got_result,
                       'picture': self._got_picture,
                       'choices': self._got_choices}[kind]
            handler(step_key, payload)
        if not self._busy:
            self._set_buttons(True)
        self.after(POLL_MS, self._tick)

    def _got_result(self, step_key, payload):
        if isinstance(payload, Exception):
            payload = setupwiz.Result(
                False, 'the check itself failed',
                f'{type(payload).__name__}: {payload}')
            logger.warning(f'wizard check {step_key} raised', exc_info=False)
        self.results[step_key] = payload
        self._refresh_rail()
        self._say('')
        if step_key == self._step().key:
            self._paint_result(payload)

    def _got_choices(self, step_key, payload):
        if not isinstance(payload, tuple):
            logger.warning(f'wizard discovery {step_key} failed: {payload}')
            return
        combo, values = payload
        if not combo.winfo_exists():
            return
        if isinstance(values, Exception):
            messagebox.showerror(
                'Nothing to list',
                values if isinstance(values, discovery.DiscoveryError)
                else f'{type(values).__name__}: {values}', parent=self)
            return
        combo['values'] = values
        combo.focus_set()
        combo.event_generate('<Down>')

    # ---- checks, per step ----

    def _run_check(self, step_key):
        runner = getattr(self, f'_check_{step_key}', None)
        if runner:
            runner()

    def _check_config(self):
        self._spawn('result', 'config', setupwiz.check_config,
                    str(self.cfg.path))

    def _check_llm(self):
        self._spawn('result', 'llm', setupwiz.check_llm,
                    self._value('api', 'base_url', 'OPENAI_API_BASE'),
                    self._value('api', 'key', 'OPENAI_API_KEY'),
                    self._value('api', 'model'))

    def _check_network(self):
        running = bool(self.owner and
                       self.owner.ctl.snapshot().get('alive'))
        self._spawn('result', 'network', setupwiz.check_port,
                    self._value('server', 'host', None, '0.0.0.0'),
                    self._value('server', 'port', None, '6400'),
                    running)

    def _check_images(self):
        text = self._pending_text()
        self._spawn('result', 'images', setupwiz.check_images, text,
                    str(self.cfg.path))

    def _check_music(self):
        self._spawn('result', 'music', setupwiz.check_music,
                    self._data_dir())

    def _check_printing(self):
        self._spawn('result', 'printing', setupwiz.check_printer,
                    self._value('printer', 'backend', None, 'c64'),
                    self._value('printer', 'cups_queue'),
                    self._value('printer', 'cups_server'))

    def _check_claude(self):
        self._spawn('result', 'claude', setupwiz.check_claude,
                    self._value('claude', 'command', 'LLM64_CLAUDE_CMD',
                                'claude'),
                    self._value('claude', 'workdir'))

    def _pending_text(self):
        """The config as this page has it, unsaved edits included - what
        the image checks have to read, because the whole point of that
        step is the settings you have just typed."""
        overrides = {}
        for (section, key), (var, ftype, _initial) in self.vars.items():
            value = self._typed(section, key, ftype, var.get(),
                                complain=False)
            if value is not None:
                overrides[(section, key)] = value
        return self.cfg.text_with(overrides)

    def _data_dir(self):
        """data_dir as the running server sees it: relative means next
        to config.toml, because that is where the server chdirs to."""
        raw = self._value('storage', 'data_dir', 'LLM64_DATA_DIR', './data')
        path = Path(raw)
        if not path.is_absolute():
            path = self.cfg.path.resolve().parent / path
        return path

    # ---- discovery (the up-arrow buttons) ----

    def _discover(self, source, combo, _btn):
        if source == 'styles':
            from .imgstyles import PRESETS
            names = set(PRESETS)
            tables = setupwiz.dig(self.cfg.doc, 'images.styles')
            if isinstance(tables, dict):
                names.update(k for k, v in tables.items()
                             if isinstance(v, dict))
            combo['values'] = sorted(names)
            combo.event_generate('<Down>')
            return
        if source == 'workflows':
            combo['values'] = self._workflow_choices()
            combo.event_generate('<Down>')
            return
        try:
            fetch = self._discover_fetch(source)
        except discovery.DiscoveryError as e:
            messagebox.showerror('Nothing to list', str(e), parent=self)
            return
        self._spawn('choices', source,
                    lambda: (combo, self._safely(fetch)))

    @staticmethod
    def _safely(fetch):
        try:
            return fetch()
        except Exception as e:
            return e

    def _discover_fetch(self, source):
        """A zero-arg callable for the worker, with everything it needs
        captured here on the UI thread (tk vars are not for other
        threads to read)."""
        if source == 'llm':
            base = self._value('api', 'base_url', 'OPENAI_API_BASE',
                               'https://api.openai.com/v1')
            key = self._value('api', 'key', 'OPENAI_API_KEY')
            return lambda: discovery.openai_models(base, key)
        if source == 'openai_images':
            base = self._value('images.openai', 'base_url', None,
                               'https://api.openai.com/v1')
            key = self._value('images.openai', 'key', 'LLM64_IMAGES_KEY')
            return lambda: discovery.openai_models(base, key)
        if source == 'gemini':
            key = self._value('images.gemini', 'key', 'GEMINI_API_KEY')
            if not key:
                raise discovery.DiscoveryError(
                    'Set the Gemini API key (or GEMINI_API_KEY) first')
            return lambda: discovery.gemini_image_models(key)
        if source == 'cups':
            server = self._value('printer', 'cups_server')
            return lambda: (setupwiz.cups_queues(server) or
                            _no_queues(server))
        url = self._value('images.comfyui', 'url', None,
                          'http://127.0.0.1:8188')
        nodes = {'comfy_model': discovery.COMFY_MODEL_NODES,
                 'comfy_clip': discovery.COMFY_CLIP_NODES,
                 'comfy_vae': discovery.COMFY_VAE_NODES,
                 'comfy_lora': discovery.COMFY_LORA_NODES}[source]
        return lambda: discovery.comfy_model_choices(url, nodes)

    def _workflow_choices(self):
        """Workflow files a config can point at: the bundled ones by bare
        name, plus JSON next to config.toml and in its workflows/ folder,
        as config-relative paths."""
        from .respath import bundled_workflows_dir
        config_dir = self.cfg.path.resolve().parent
        choices = {}
        for label, path in (
                [(p.name, p)
                 for p in sorted(bundled_workflows_dir().glob('*.json'))]
                + [(f'workflows/{p.name}', p)
                   for p in sorted((config_dir / 'workflows').glob('*.json'))]
                + [(p.name, p) for p in sorted(config_dir.glob('*.json'))]):
            resolved = path.resolve()
            if resolved not in choices.values() and label not in choices:
                choices[label] = resolved
        return list(choices)

    # ---- the pages that are more than a form ----

    def _page_config(self, _step):
        box = ttk.LabelFrame(self.page, text='config.toml', padding=8)
        box.pack(fill='x', pady=(4, 8))
        box.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar(value=str(self.cfg.path))
        ttk.Entry(box, textvariable=self.path_var).grid(
            row=0, column=0, sticky='ew')
        for col, (label, command) in enumerate((
                ('Browse...', self._browse_config),
                ('Use this path', self._use_config_path)), start=1):
            btn = ttk.Button(box, text=label, command=command)
            btn.grid(row=0, column=col, padx=(6, 0))
            self._page_buttons.append(btn)
        self.config_note = ttk.Label(box, wraplength=WRAP, justify='left')
        self.config_note.grid(row=1, column=0, columnspan=3, sticky='w',
                              pady=(8, 0))
        auto_wrap(self.config_note)
        create = ttk.Button(box, text='Create from template',
                            command=self._create_config)
        create.grid(row=2, column=0, sticky='w', pady=(8, 0))
        self._page_buttons.append(create)
        self.btn_create = create
        self._refresh_config_note()
        self._check_box((('Recheck', self._check_config),))

    def _refresh_config_note(self):
        exists = self.cfg.exists()
        self.config_note.configure(
            text=(f'File exists') if exists else
            (f'There is no file here yet. Create it from the template '),
            foreground='#227722' if exists else '#b8860b')
        self.btn_create.state(['disabled'] if exists else ['!disabled'])

    def _browse_config(self):
        path = filedialog.askopenfilename(
            parent=self, title='Choose config.toml',
            filetypes=[('TOML config', '*.toml'), ('All files', '*')])
        if path:
            self.path_var.set(path)
            self._use_config_path()

    def _use_config_path(self):
        path = self.path_var.get().strip()
        if not path:
            return
        self.cfg = setupwiz.ConfigDoc(path)
        self.rollback.note(self.cfg.path)
        self.results.clear()
        self._say(f'Now editing {path}')
        self._show(self.index)

    def _create_config(self):
        problem = self.cfg.create_from_template()
        if problem:
            messagebox.showerror('Could not create it', problem, parent=self)
            return
        self._say(f'Created {self.cfg.path}')
        self._show(self.index)

    def _page_llm(self, _step):
        env = os.environ.get('OPENAI_API_KEY')
        if env:
            self._para('OPENAI_API_KEY is set in this environment, so it '
                       'is what the proxy will use whatever the key field '
                       'says.', color='#b8860b')
        self._check_box((('Test the connection', self._check_llm),))

    def _page_network(self, _step):
        self._check_box((('Check the port', self._check_network),))

    def _page_images(self, _step):
        from . import preview
        self._backend_box = ttk.Frame(self.page)
        self._backend_box.pack(fill='x')
        backend_var = self.vars[('images', 'backend')][0]
        backend_var.trace_add('write', lambda *_: self._rebuild_backend_box())
        self._rebuild_backend_box()

        self._check_box((('Check the backend', self._check_images),))

        box = ttk.LabelFrame(self.page, text='Test picture', padding=8)
        box.pack(fill='x', pady=(4, 8))
        self.picture_scene = tk.StringVar(
            value=dict(preview.SAMPLE_SCENES)['Torchlit crypt'])
        ttk.Label(box, text='Scene').pack(anchor='w')
        ttk.Entry(box, textvariable=self.picture_scene).pack(fill='x')
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(6, 0))
        btn = ttk.Button(row, text='Generate one', command=self._test_picture)
        btn.pack(side='left')
        self._page_buttons.append(btn)
        ttk.Label(row, foreground='#777', font=('TkDefaultFont', 8),
                  text='Costs whatever one picture costs on this '
                       'backend.').pack(side='left', padx=(8, 0))
        self.picture_canvas = tk.Canvas(
            box, width=PICTURE_W, height=PICTURE_H, background='#222',
            highlightthickness=0)
        self.picture_canvas.pack(pady=(8, 0))
        self.picture_canvas.create_text(
            PICTURE_W // 2, PICTURE_H // 2, fill='#888',
            text='(nothing generated yet)')
        ttk.Label(box, foreground='#777', wraplength=WRAP, justify='left',
                  font=('TkDefaultFont', 8),
                  text='What you get back is the C64 render - the same '
                       '16-color conversion the client is sent, not the '
                       'original. The launcher\'s Illustrations tab shows '
                       'the original and the Windows version beside it, '
                       'and keeps a history to compare against.').pack(
                           anchor='w', pady=(6, 0))

    def _rebuild_backend_box(self):
        """Show only the settings belonging to the selected backend.

        Switching backends takes the old block's widgets away, so what
        was typed into them goes into the document on the way out. It is
        not written to disk here - leaving the step does that - but a
        key typed and then switched past is not silently lost, and the
        other backend's settings stay in the file where they were.
        """
        if self._backend_box is None:
            return
        for key in list(self.vars):
            if not key[0].startswith('images.'):
                continue
            var, ftype, initial = self.vars.pop(key)
            if var.get() != initial:
                value = self._typed(key[0], key[1], ftype, var.get())
                if value is not None:
                    self.cfg.set(key[0], key[1], value)
                    self._doc_dirty = True
        for child in self._backend_box.winfo_children():
            child.destroy()
        backend = (self.vars[('images', 'backend')][0].get().strip()
                   or 'gemini')
        fields = setupwiz.IMAGE_BACKEND_FIELDS.get(backend)
        if not fields:
            return
        self._fields_box(fields, parent=self._backend_box,
                         title=f'Settings for the {backend} backend')
        bind_wheel(self._backend_box, self.page_wheel)

    def _test_picture(self):
        scene = self.picture_scene.get().strip()
        if not scene:
            messagebox.showerror('Nothing to illustrate',
                                 'Type a scene first.', parent=self)
            return
        self._say('Generating a picture...')
        self._spawn('picture', 'images', self._generate,
                    self._pending_text(), str(self.cfg.path), scene)

    @staticmethod
    def _generate(text, config_path, scene):
        from . import preview
        return preview.generate_preview(
            text, config_path, scene, target='c64',
            caption=preview.SAMPLE_CAPTION)

    def _got_picture(self, step_key, payload):
        self._say('')
        if isinstance(payload, Exception):
            self.results[step_key] = setupwiz.Result(
                False, 'the picture failed', str(payload))
            self._refresh_rail()
            self._paint_result(self.results[step_key])
            return
        self.results[step_key] = setupwiz.Result(
            True, f'generated one picture via {payload.backend}',
            f'Saved with the launcher\'s other previews. Style prefix from '
            f'{payload.prefix_source}.')
        self._refresh_rail()
        self._paint_result(self.results[step_key])
        if not self.picture_canvas.winfo_exists():
            return
        photo = photo_image(self, payload.c64, (PICTURE_W, PICTURE_H))
        self._photos['picture'] = photo      # tk keeps no reference
        self.picture_canvas.delete('all')
        self.picture_canvas.create_image(PICTURE_W // 2, PICTURE_H // 2,
                                         image=photo)

    def _page_music(self, _step):
        self._check_box((('Recheck', self._check_music),),
                        title='What is on disk')
        base = self._value('api', 'base_url', 'OPENAI_API_BASE',
                           'http://localhost:8080/v1')
        style = ttk.Style()
        for title, body in setupwiz.music_commands(base):
            box = ttk.LabelFrame(self.page, text=title, padding=8)
            box.pack(fill='x', pady=(4, 8))
            # An unstyled tk.Text takes its colors from the X defaults
            # rather than the ttk theme, which on some servers is white
            # on near-black in the middle of a light window. Borrow an
            # entry's colors so it reads as the block of text to copy
            # that it is - disabled still selects and copies.
            text = tk.Text(box, height=body.count('\n') + 2, wrap='word',
                           font=('TkFixedFont', 9), relief='flat',
                           background=style.lookup('TEntry',
                                                   'fieldbackground')
                           or 'white',
                           foreground=style.lookup('TLabel', 'foreground')
                           or 'black')
            text.insert('1.0', body)
            text.configure(state='disabled')
            text.pack(fill='x')

    def _page_printing(self, _step):
        self._check_box((('Check', self._check_printing),
                         ('Send a test page', self._test_page)))

    def _test_page(self):
        queue_name = self._value('printer', 'cups_queue')
        if not queue_name:
            messagebox.showerror('No queue',
                                 'Fill in the CUPS queue first.', parent=self)
            return
        self._say('Sending a test page...')
        self._spawn('result', 'printing', setupwiz.send_test_page,
                    queue_name, self._value('printer', 'cups_server'),
                    self._value('printer', 'cups_options'))

    def _page_claude(self, _step):
        self._check_box((('Check the CLI', self._check_claude),))

    def _page_finish(self, _step):
        box = ttk.LabelFrame(self.page, text='Where each step stands',
                             padding=8)
        box.pack(fill='x', pady=(4, 8))
        box.columnconfigure(2, weight=1)
        for row, step in enumerate(STEPS):
            if step.key in ('welcome', 'finish'):
                continue
            result = self.results.get(step.key)
            mark = MARKS[getattr(result, 'ok', None)]
            color = {True: '#227722', False: '#cc2222'}.get(
                getattr(result, 'ok', None), '#777')
            ttk.Label(box, text=mark, foreground=color, width=2).grid(
                row=row, column=0, sticky='w')
            ttk.Label(box, text=step.title + ('' if step.required
                                              else ' (optional)')).grid(
                row=row, column=1, sticky='w', padx=(0, 12))
            if result:
                note = result.summary
            elif hasattr(self, f'_check_{step.key}'):
                note = 'not checked - open the step and press its button'
            else:
                note = 'nothing to check here'
            auto_wrap(ttk.Label(box, foreground='#555', justify='left',
                                wraplength=WRAP - 160, text=note)).grid(
                                    row=row, column=2, sticky='w')
        # Only the restart lives here; Finish at the bottom of the window
        # does the saving, and two buttons for one action is two buttons
        # to wonder about.
        row = ttk.Frame(self.page)
        row.pack(fill='x', pady=(4, 8))
        btn = ttk.Button(row, text='Save and restart the proxy',
                         command=self._restart)
        btn.pack(side='left')
        self._page_buttons.append(btn)
        ttk.Label(row, foreground='#777', font=('TkDefaultFont', 8),
                  text='Finish saves and closes without touching a '
                       'running server.').pack(side='left', padx=(8, 0))
        self._para('Reopen this wizard any time with the Setup wizard '
                   'button in the launcher. Nothing here is one-way, and '
                   'the steps you left alone are still waiting.',
                   color='#555', pad=(0, 8, 0, 0))

    # ---- finishing ----

    def _finish(self):
        self._commit()
        self.cfg.mark_completed()
        problem = self.cfg.save()
        if problem:
            messagebox.showerror('Could not save', problem, parent=self)
            return
        self._close()

    def _restart(self):
        self._commit()
        self.cfg.mark_completed()
        problem = self.cfg.save()
        if problem:
            messagebox.showerror('Could not save', problem, parent=self)
            return
        owner = self.owner
        self._close()
        if owner:
            owner.config_path.set(str(self.cfg.path))
            owner._on_restart()

    def _on_close(self):
        """Stopping part way through is a supported ending, not a
        cancel: every step already visited has been written, and this
        writes the one on screen too."""
        self._commit()
        self._close()

    def _cancel(self):
        """Put config.toml back the way it was when this window opened.

        Worth being explicit about, because it is not the same as
        closing: the wizard has been writing each step as you left it,
        so by now there is something on disk to undo. The confirmation
        names the files rather than asking in the abstract.
        """
        changed = self.rollback.changed()
        if not changed:
            self._close()
            return
        names = '\n'.join(f'  {p}' for p in changed)
        if not messagebox.askyesno(
                'Discard these changes?',
                f'The wizard has already written what you finished. '
                f'Canceling puts it back the way it was:\n\n{names}\n\n'
                f'Anything you set in this run is lost.',
                parent=self, default='no'):
            return
        problems = self.rollback.restore()
        if problems:
            messagebox.showerror(
                'Could not undo all of it',
                'Some of it went back, some did not:\n\n'
                + '\n'.join(problems), parent=self)
        else:
            self._say('Canceled - config.toml is back as it was')
        self._close()

    def _close(self):
        if self.owner:
            self.owner.config_path.set(str(self.cfg.path))
            self.owner._load_editor()
            self.owner._wizard = None
        self.destroy()


def _no_queues(server):
    raise discovery.DiscoveryError(
        f'lpstat listed no queues on {server.strip() or "this machine"}. '
        f'Check cupsd is running, and sharing its printers if it is on '
        f'another host.')


def open_wizard(app, config_path=None):
    """Open the wizard over the launcher, or raise the one already open."""
    existing = getattr(app, '_wizard', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return existing
    win = SetupWizard(app, config_path or app.config_path.get(), owner=app)
    app._wizard = win
    win.lift()
    win.focus_force()
    return win
