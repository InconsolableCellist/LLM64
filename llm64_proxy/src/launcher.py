"""Desktop launcher for the LLM64 proxy.

The same asyncio server `python -m src` runs, but inside a window:
start/stop/restart buttons, live status (connections, LLM calls), a
scrolling log with a file copy, and an editor for config.toml. This is
what the packaged binary (llm64_launcher.py / server.exe) boots into;
--headless bypasses it and runs the plain CLI server.

Threading model: the UI owns the main thread and never touches
asyncio; the server runs on one background thread with its own event
loop (asyncio.run). They meet in exactly three places - a Queue of
formatted log lines, plain attribute reads off the controller (ints
and strings, safe under the GIL), and call_soon_threadsafe for the
stop signal. Restart is stop + poll-until-dead + start, driven from
the UI tick so nothing ever blocks the window.

Config editing goes through tomlkit, not toml: config.toml is heavily
commented and a round-trip through toml.dump would strip every comment.
Only fields the user actually changed are written back; blanking a
field that was set removes the key (the server then uses its default).
"""

import asyncio
import logging
import logging.handlers
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

import tomlkit

from . import discovery
from . import preview
from .config import Config
from .api_client import APIClient
from .tcp_server import C64Server
from .respath import resource_dir, bundled_workflows_dir

logger = logging.getLogger('launcher')

POLL_MS = 400          # UI refresh cadence
LOG_PANE_MAX_LINES = 4000
STOP_TIMEOUT_S = 8     # give a wedged server thread this long, then report

# Illustration preview tab
PANEL_W, PANEL_H = 320, 200    # one render, at the size the C64 draws it
THUMB_W, THUMB_H = 112, 70
HISTORY_MAX = 24               # saved previews the strip goes back through
PREVIEW_MAX_BATCH = 8


# --------------------------------------------------------------------------
# Server side (background thread)

class _CountingAPIClient(APIClient):
    """APIClient that ticks the launcher's LLM-call counter.

    stream_chat is the one door every chat request goes through
    (protocol.py calls nothing else for text), so counting here sees
    all of them without touching protocol code.
    """

    def __init__(self, config, controller):
        super().__init__(config)
        self._controller = controller

    async def stream_chat(self, *args, **kwargs):
        self._controller.api_calls += 1
        self._controller.last_activity = time.time()
        async for piece in super().stream_chat(*args, **kwargs):
            yield piece


class ServerController:
    """Owns the server thread. All methods are called from the UI thread
    and return immediately; the UI polls snapshot() for the truth."""

    def __init__(self):
        self._thread = None
        self._loop = None
        self._stop_event = None
        self.server = None
        self.config = None
        self.state = 'stopped'      # stopped | starting | stopping | error
        self.error = ''
        self.addr = ''
        self.api_calls = 0
        self.last_activity = None

    def start(self, config_path, host, port):
        if self._thread and self._thread.is_alive():
            return
        self.state = 'starting'
        self.error = ''
        self.addr = f'{host}:{port}'
        self._thread = threading.Thread(
            target=self._thread_main, args=(config_path, host, port),
            name='llm64-server', daemon=True)
        self._thread.start()

    def stop(self):
        loop, ev = self._loop, self._stop_event
        if loop and ev and self._thread and self._thread.is_alive():
            self.state = 'stopping'
            loop.call_soon_threadsafe(ev.set)

    def snapshot(self):
        alive = bool(self._thread and self._thread.is_alive())
        state = self.state
        server = self.server
        if not alive and state in ('starting', 'stopping'):
            state = 'error' if self.error else 'stopped'
        # 'running' is derived, not declared: the listening socket
        # existing is the fact, a state flag would just be a race
        if alive and state == 'starting' and server and server.server:
            state = 'running'
        return {
            'alive': alive,
            'state': state,
            'error': self.error,
            'addr': self.addr,
            'clients': len(server.clients) if server else 0,
            'total': server.client_counter if server else 0,
            'api_calls': self.api_calls,
            'last_activity': self.last_activity,
            'data_dir': self.config.data_dir if self.config else None,
        }

    # ---- background thread ----

    def _thread_main(self, config_path, host, port):
        try:
            asyncio.run(self._serve(config_path, host, port))
        except Exception as e:
            logger.error(f'Server failed: {e}', exc_info=True)
            self.state = 'error'
            self.error = str(e)
        else:
            if self.state != 'error':
                self.state = 'stopped'
        finally:
            self._loop = None
            self._stop_event = None
            self.server = None

    async def _serve(self, config_path, host, port):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        config = Config(config_path if config_path
                        and Path(config_path).exists() else None)
        self.config = config
        logger.info(f'API endpoint: {config.api_base_url}  '
                    f'model: {config.model}')
        api_client = _CountingAPIClient(config, self)
        server = C64Server(host, port, config, api_client)
        self.server = server

        run_task = asyncio.create_task(server.run())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if run_task in done and run_task.exception():
                raise run_task.exception()
        finally:
            run_task.cancel()
            stop_task.cancel()
            try:
                await server.close()
            except Exception:
                pass
            try:
                await api_client.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Logging plumbing

class _QueueLogHandler(logging.Handler):
    """Formats records onto a Queue the UI drains each tick."""

    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait((record.levelno, self.format(record)))
        except Exception:
            pass


def _setup_logging(ui_queue):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    pane = _QueueLogHandler(ui_queue)
    pane.setFormatter(logging.Formatter(
        '%(asctime)s %(name)s %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'))
    root.addHandler(pane)
    # A frozen windowed exe has no stderr (it is None); only mirror to
    # the console when there is one to mirror to
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        root.addHandler(console)


# --------------------------------------------------------------------------
# Config schema for the form editor
#
# (dotted section, tab-visible title, [(key, type, label, help), ...]).
# type is str | secret | int | float | bool:<default> | choice:a,b,c
# | pick:<source>. pick is a free-text combobox whose ↻ button asks the
# endpoint what it actually serves (see _discover for the sources); the
# field stays hand-editable because discovery needs the server up.
# The help strings are the config.toml.example comments, shortened.

CONFIG_SCHEMA = [
    ('server', 'Server', [
        ('host', 'str', 'Bind address',
         '0.0.0.0 listens on every interface; 127.0.0.1 is this machine '
         'only. Launcher-only setting.'),
        ('port', 'int', 'TCP port',
         'The port the C64 dials (default 6400). Launcher-only setting.'),
    ]),
    ('api', 'LLM API', [
        ('base_url', 'str', 'Base URL',
         'OpenAI-compatible endpoint, e.g. http://localhost:8080/v1'),
        ('key', 'secret', 'API key',
         'Blank is fine for local servers. OPENAI_API_KEY env overrides.'),
        ('model', 'pick:llm', 'Model',
         '↻ lists what the endpoint serves (GET /models)'),
        ('temperature', 'float', 'Temperature',
         '0.0 - 2.0, lower = more focused (default 0.7)'),
        ('max_tokens', 'int', 'Max reply tokens',
         'Kept small so replies reach the C64 fast (default 2000)'),
        ('max_context_tokens', 'int', 'Context window fallback',
         'Used when the model does not report its own size (default 8192)'),
        ('read_timeout', 'int', 'API read timeout (s)',
         'Longest silent pause allowed mid-request; raise for big local '
         'models that load or prompt-eval slowly (default 600)'),
        ('disable_thinking', 'bool:true', 'Disable model thinking',
         'Thinking blocks eat the token budget; the C64 just sees a pause'),
        ('system_prompt', 'str', 'System prompt', ''),
    ]),
    ('storage', 'Storage', [
        ('data_dir', 'str', 'Data directory',
         'Conversations, images, music and the log land here '
         '(default ./data, relative to config.toml)'),
    ]),
    ('serial', 'Serial pacing', [
        ('wire_baud', 'int', 'Wire baud',
         'What the client ACIA is set to. Move only in lockstep with the '
         'client build (default 9600).'),
        ('chunk_pace_base', 'float', 'Chunk pace base (s)',
         'Streaming pace floor per chunk; the C64 screen is the limit, '
         'not the wire (default 0.016)'),
        ('chunk_pace_per_byte', 'float', 'Chunk pace per byte (s)',
         'Per-byte pacing on top of the base (default 0.0018). Watch the '
         'client\'s data-loss counters before raising.'),
    ]),
    ('modes', 'Modes', [
        ('user_name', 'str', 'User name',
         'How the user is addressed in chat (default You)'),
        ('cards_dir', 'str', 'Cards directory',
         'Your own character cards; wins over the built-ins on a name '
         'clash (default ./cards)'),
    ]),
    ('images', 'Images', [
        ('mode', 'choice:ask,auto,off', 'Mode',
         'ask = model suggests, /pic confirms. auto = striking scenes '
         'illustrate themselves. off = never.'),
        ('backend', 'choice:gemini,openai,comfyui,fixture', 'Backend', ''),
        ('style', 'pick:styles', 'Style preset',
         'A named look: cinematic, oil-chiaroscuro, painted-noir and '
         'nova-furry (the anthro chain - SDXL workflow and tag prompts) '
         'ship built in; ↻ also lists [images.styles.*] tables from this '
         'config. Empty = the default look. When set, the preset\'s '
         'prefix wins over Style prefix below.'),
        ('style_prefix', 'str', 'Style prefix',
         'Prepended to every scene description. Empty string = none; '
         'unset = the built-in dark-fantasy text.'),
    ]),
    ('images.gemini', 'Images: Gemini', [
        ('model', 'pick:gemini', 'Model',
         'default gemini-2.5-flash-image; ↻ needs the key set'),
        ('key', 'secret', 'API key',
         'GEMINI_API_KEY env is the recommended place instead'),
    ]),
    ('images.openai', 'Images: OpenAI-style', [
        ('base_url', 'str', 'Base URL',
         'Anything serving POST /v1/images/generations'),
        ('model', 'pick:openai_images', 'Model',
         'e.g. dall-e-3; ↻ lists what the endpoint serves'),
        ('size', 'str', 'Size',
         'landscape suits the 320x200 frame; 1024x1024 is the safe choice'),
        ('key', 'secret', 'API key', 'or the LLM64_IMAGES_KEY env var'),
    ]),
    ('images.comfyui', 'Images: ComfyUI', [
        ('url', 'str', 'URL', 'e.g. http://127.0.0.1:8188 - ComfyUI has '
         'no auth, keep it on a trusted network'),
        ('workflow', 'pick:workflows', 'Workflow JSON',
         'API-format export with {PROMPT} in the positive prompt; '
         'relative to config.toml. Empty = the bundled Flux workflow.'),
        ('timeout', 'int', 'Timeout (s)', 'raise on slow GPUs'),
        ('width', 'int', 'Width', 'keep ~1.6:1 or the letterbox eats it'),
        ('height', 'int', 'Height', ''),
        ('steps', 'int', 'Steps', ''),
        ('cfg', 'float', 'CFG', 'cfg 1 disables the negative prompt'),
        ('model', 'pick:comfy_model', 'Checkpoint',
         'filename as ComfyUI sees it; ↻ asks the running instance'),
        ('clip', 'pick:comfy_clip', 'CLIP',
         'fills {CLIP} - only Flux-style split workflows use it'),
        ('vae', 'pick:comfy_vae', 'VAE', 'fills {VAE}, same deal'),
        ('negative', 'str', 'Negative prompt', ''),
        ('style', 'str', 'Style', ''),
    ]),
    # These fields define ONE user preset named "custom" - they only do
    # anything when the Style preset above is set to "custom". For more
    # presets, or to override a built-in, write [images.styles.<name>]
    # tables in the Raw config tab.
    ('images.styles.custom', 'Images: custom style preset', [
        ('style_prefix', 'str', 'Style prefix',
         'The look, as prose ending in "Scene: ". Only used when Style '
         'preset above is "custom". Empty = keep the default prefix and '
         'let the LoRA do the styling.'),
        ('lora', 'pick:comfy_lora', 'LoRA',
         'File in ComfyUI\'s models/loras; ↻ asks the running instance. '
         'Fills {LORA} via the bundled flux2-klein-lora.json workflow.'),
        ('lora_strength', 'float', 'LoRA strength',
         '0.0 - 1.0ish; unset = 1.0. 0.8 is a good start.'),
    ]),
    ('claude', 'Claude Code', [
        ('command', 'str', 'claude command',
         'Path to the claude CLI on this machine (absolute path is '
         'safest)'),
        ('workdir', 'str', 'Project directory',
         'Where the agent works by default'),
        ('model', 'str', 'Model',
         'opus / sonnet / haiku or a full id; empty = the CLI default'),
    ]),
    ('printer', 'Printer', [
        ('width', 'int', 'Document width (cols)',
         'The printer\'s width, not the screen\'s (MPS-803 = 78)'),
        ('formfeed', 'bool:true', 'Form feed after job',
         'The C64 Ultimate otherwise holds a partial page until F5'),
        ('max_tokens', 'int', 'Print generation budget',
         '/print composes a full page; 2000 covers it'),
        ('backend', 'choice:c64,cups,both', 'Backend',
         'c64 = IEC printer on device 4. cups needs a queue below. '
         '(CUPS needs the lp command - Linux/macOS only.)'),
        ('cups_queue', 'str', 'CUPS queue', ''),
        ('cups_server', 'str', 'CUPS server',
         'empty = local cupsd, else host[:port]'),
        ('cups_options', 'str', 'lp options', 'passed to lp as -o'),
        ('cups_width', 'int', 'CUPS columns',
         '0 = share document width; a roll printer is narrower'),
        ('cups_feed_lines', 'int', 'Feed lines',
         'clears a roll printer\'s tear bar; 0 for page printers'),
        ('cups_pic_scale', 'int', 'Picture scale',
         'printer dots per C64 pixel, raise in steps of 4'),
        ('cups_pic_dpi', 'int', 'Picture DPI',
         'the queue\'s real dot pitch or CUPS moires the halftone'),
    ]),
]


def music_status_lines(data_dir):
    """One line each for the SID and MIDI libraries under data_dir.

    Goes through the real loaders (MusicLibrary/MidiLibrary), so
    "detected" means the server would actually play it, not just that a
    file exists. The loaders swallow parse errors and come back empty,
    which is why a present-but-empty database gets its own wording.
    """
    from .music import MusicLibrary
    from .midi_library import MidiLibrary
    lines = []
    for name, db_path, cls, hint in (
            ('SID', Path(data_dir) / 'sids' / 'moods.json', MusicLibrary,
             'run llm64_proxy/tools/sid_build.py (README: "Building the '
             'SID music library")'),
            ('MIDI', Path(data_dir) / 'midi' / 'midi.json', MidiLibrary,
             'run llm64_proxy/tools/midi_fetch.py, midi_scan.py, '
             'midi_mood.py, midi_makedb.py (README: "Music for the '
             'Windows client")')):
        try:
            if not db_path.exists():
                lines.append(f'{name}: not set up - {hint}')
                continue
            lib = cls(db_path)
            if lib.available:
                lines.append(f'{name}: {len(lib.tunes)} tunes, '
                             f'{len(lib.moods)} moods ({db_path})')
            else:
                lines.append(f'{name}: {db_path} exists but loaded no '
                             f'tunes (broken or empty database) - {hint}')
        except Exception as e:
            lines.append(f'{name}: {db_path} failed to load '
                         f'({type(e).__name__}: {e})')
    return lines


def _dig(doc, dotted, create=False):
    """The table a dotted section name refers to, or None."""
    node = doc
    for part in dotted.split('.'):
        if not isinstance(node, dict) or part not in node:
            if not create:
                return None
            node[part] = tomlkit.table()
        node = node[part]
    return node


def _template_path():
    """config.toml.example, in a checkout or in the bundle."""
    for base in (resource_dir().parent, resource_dir()):
        p = base / 'config.toml.example'
        if p.exists():
            return p
    return None


def validate_config_text(text, config_path):
    """None if the text is a config the server would accept, else the
    complaint. Runs the real Config parser against a scratch file in
    the same directory, so relative paths resolve exactly as they will
    at start time."""
    try:
        tomlkit.parse(text)
    except Exception as e:
        return f'TOML syntax: {e}'
    base = Path(config_path).resolve().parent if config_path else Path('.')
    tmp = base / '.launcher-validate.tmp.toml'
    try:
        tmp.write_text(text)
        Config(str(tmp))
    except Exception as e:
        return f'Config rejected: {e}'
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------
# UI

class LauncherApp(tk.Tk):

    def __init__(self, config_path):
        super().__init__()
        self.title('LLM64 Proxy')
        # Tall enough for the illustration tab's two rows of 320x200
        # renders; every tab scrolls, so a smaller screen still works.
        self.geometry('980x760')
        self.minsize(640, 420)

        self.ctl = ServerController()
        self.log_queue = queue.Queue()
        _setup_logging(self.log_queue)

        self.config_path = tk.StringVar(value=str(config_path))
        self.autoscroll = tk.BooleanVar(value=True)
        self._restart_pending = False
        self._stop_deadline = None
        self._quit_after_stop = False
        self._file_handler = None
        self._file_handler_path = None
        self._fields = {}       # (section, key) -> (widget var, type, initial)
        self._doc = None        # tomlkit document behind the form
        self._discover_queue = queue.Queue()   # (combo, btn, list or exc)

        # Illustration preview: a worker thread generates, the tick loop
        # draws. _preview_photos holds every tk image the tab is showing -
        # tk keeps no reference of its own, and a garbage-collected
        # PhotoImage paints as an empty box.
        self._preview_queue = queue.Queue()
        self._preview_busy = False
        self._preview_cancel = False
        self._preview_photos = {}
        self._preview_history = []
        self._preview_prefix = {}   # target -> (style prefix, its source)

        self._build_ui()
        self._load_editor()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        logger.info('LLM64 proxy launcher ready')
        if not Path(self.config_path.get()).exists():
            logger.warning(
                f'No config file at {self.config_path.get()} - the server '
                f'would run on built-in defaults. Use "Create config" in '
                f'the Settings tab.')
        self.after(POLL_MS, self._tick)

    # ---- layout ----

    def _build_ui(self):
        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.pack(fill='x')
        ttk.Label(top, text='Config:').pack(side='left')
        ttk.Entry(top, textvariable=self.config_path).pack(
            side='left', fill='x', expand=True, padx=(4, 4))
        ttk.Button(top, text='Browse...', command=self._browse).pack(
            side='left')

        bar = ttk.Frame(self, padding=(8, 0, 8, 4))
        bar.pack(fill='x')
        self.btn_start = ttk.Button(bar, text='Start',
                                    command=self._on_start)
        self.btn_stop = ttk.Button(bar, text='Stop', command=self._on_stop)
        self.btn_restart = ttk.Button(bar, text='Restart',
                                      command=self._on_restart)
        self.btn_start.pack(side='left')
        self.btn_stop.pack(side='left', padx=4)
        self.btn_restart.pack(side='left')

        self.status_dot = tk.Label(bar, text='●', fg='#888')
        self.status_dot.pack(side='left', padx=(16, 2))
        self.status_text = ttk.Label(bar, text='Stopped')
        self.status_text.pack(side='left')
        self.stats_text = ttk.Label(bar, text='')
        self.stats_text.pack(side='right')

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self._build_log_tab(nb)
        self._build_settings_tab(nb)
        self._build_preview_tab(nb)
        self._build_raw_tab(nb)

    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text='Log')
        self.log_text = scrolledtext.ScrolledText(
            tab, state='disabled', wrap='word', font=('TkFixedFont', 9))
        self.log_text.pack(fill='both', expand=True)
        self.log_text.tag_configure('warn', foreground='#b8860b')
        self.log_text.tag_configure('error', foreground='#cc2222')
        foot = ttk.Frame(tab)
        foot.pack(fill='x')
        ttk.Checkbutton(foot, text='Autoscroll',
                        variable=self.autoscroll).pack(side='left')
        ttk.Button(foot, text='Clear', command=self._clear_log).pack(
            side='left', padx=8)
        self.logfile_label = ttk.Label(
            foot, text='Log file: (created when the server starts)')
        self.logfile_label.pack(side='right')

    @staticmethod
    def _scroll_host(parent):
        """A vertically scrolling frame filling `parent`. Returns
        (inner frame, wheel handler) - the handler has to be bound to
        each child as it is built, because a child with its own bindings
        swallows the wheel event before the canvas sees it.

        Pack the tab's fixed furniture (a button row) BEFORE calling
        this: the canvas takes every pixel the earlier packs left.
        """
        canvas = tk.Canvas(parent, highlightthickness=0)
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
        # bind on the canvas subtree only, not the whole app, so the log
        # and raw tabs keep their own wheel behaviour
        for w in (canvas, inner):
            w.bind('<MouseWheel>', _wheel)
            w.bind('<Button-4>', _wheel)
            w.bind('<Button-5>', _wheel)
        return inner, _wheel

    def _bind_wheel(self, widget, wheel):
        """Hand the page's wheel handler down a finished subtree.

        Skipped: text boxes, which scroll themselves, and the value
        widgets whose Tk bindings read the wheel as "change me" - a
        scroll over a combobox must not silently pick a different style
        preset.
        """
        for child in widget.winfo_children():
            if not isinstance(child, (tk.Text, ttk.Combobox, ttk.Spinbox,
                                      ttk.Scrollbar)):
                child.bind('<MouseWheel>', wheel)
                child.bind('<Button-4>', wheel)
                child.bind('<Button-5>', wheel)
            self._bind_wheel(child, wheel)

    def _build_settings_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text='Settings')

        foot = ttk.Frame(tab, padding=(0, 6, 0, 0))
        foot.pack(side='bottom', fill='x')
        ttk.Button(foot, text='Reload', command=self._load_editor).pack(
            side='left')
        ttk.Button(foot, text='Validate',
                   command=self._validate_form).pack(side='left', padx=4)
        ttk.Button(foot, text='Save', command=self._save_form).pack(
            side='left')
        ttk.Button(foot, text='Save && Restart',
                   command=self._save_and_restart).pack(side='left', padx=4)
        self.btn_create = ttk.Button(foot, text='Create config',
                                     command=self._create_config)
        self.btn_create.pack(side='right')

        self.form, self._form_wheel = self._scroll_host(tab)

    def _build_raw_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text='Raw config')
        self.raw_text = scrolledtext.ScrolledText(
            tab, wrap='none', undo=True, font=('TkFixedFont', 9))
        self.raw_text.pack(fill='both', expand=True)
        foot = ttk.Frame(tab, padding=(0, 6, 0, 0))
        foot.pack(fill='x')
        ttk.Button(foot, text='Reload', command=self._load_editor).pack(
            side='left')
        ttk.Button(foot, text='Validate',
                   command=self._validate_raw).pack(side='left', padx=4)
        ttk.Button(foot, text='Save', command=self._save_raw).pack(
            side='left')

    # ---- form construction ----

    def _load_editor(self):
        """(Re)build the form and raw tab from the file on disk."""
        path = Path(self.config_path.get())
        text = path.read_text() if path.exists() else ''
        try:
            self._doc = tomlkit.parse(text)
        except Exception as e:
            self._doc = None
            logger.error(f'config.toml does not parse: {e} - fix it in '
                         f'the Raw config tab')
        self.raw_text.delete('1.0', 'end')
        self.raw_text.insert('1.0', text)
        self.raw_text.edit_reset()

        for child in self.form.winfo_children():
            child.destroy()
        self._fields = {}
        if self._doc is None:
            ttk.Label(self.form, text='config.toml has a syntax error - '
                      'fix it in the Raw config tab, then Reload.').pack()
            return

        for section, title, fields in CONFIG_SCHEMA:
            table = _dig(self._doc, section) or {}
            box = ttk.LabelFrame(self.form, text=title, padding=6)
            box.pack(fill='x', pady=(0, 8))
            box.columnconfigure(1, weight=1)
            for row, (key, ftype, label, help_text) in enumerate(fields):
                present = key in table
                raw = table.get(key) if present else None
                ttk.Label(box, text=label).grid(
                    row=row * 2, column=0, sticky='w', padx=(0, 8))
                if ftype.startswith('bool'):
                    default = ftype.split(':')[1] == 'true'
                    var = tk.BooleanVar(
                        value=bool(raw) if present else default)
                    w = ttk.Checkbutton(box, variable=var)
                    initial = var.get()
                elif ftype.startswith('choice:'):
                    var = tk.StringVar(value=str(raw) if present else '')
                    w = ttk.Combobox(
                        box, textvariable=var, state='readonly',
                        values=[''] + ftype.split(':')[1].split(','))
                    initial = var.get()
                elif ftype.startswith('pick:'):
                    # Free-text combobox; ↻ fills the dropdown with what
                    # the endpoint reports. Typing stays possible because
                    # discovery needs the server up and configured.
                    var = tk.StringVar(value='' if raw is None else str(raw))
                    source = ftype.split(':')[1]
                    w = ttk.Combobox(box, textvariable=var)
                    btn = ttk.Button(box, text='↻', width=3)
                    btn.configure(command=lambda s=source, c=w, b=btn:
                                  self._discover(s, c, b))
                    btn.grid(row=row * 2, column=2, padx=(4, 0))
                    initial = var.get()
                else:
                    var = tk.StringVar(value='' if raw is None else str(raw))
                    show = '*' if ftype == 'secret' else ''
                    w = ttk.Entry(box, textvariable=var, show=show)
                    initial = var.get()
                w.grid(row=row * 2, column=1, sticky='ew')
                w.bind('<MouseWheel>', self._form_wheel)
                w.bind('<Button-4>', self._form_wheel)
                w.bind('<Button-5>', self._form_wheel)
                if help_text:
                    hint = ttk.Label(box, text=help_text, foreground='#777',
                                     wraplength=560, justify='left',
                                     font=('TkDefaultFont', 8))
                    hint.grid(row=row * 2 + 1, column=1, sticky='w',
                              pady=(0, 3))
                self._fields[(section, key)] = (var, ftype, initial, present)
            if section == 'images':
                self._build_images_note()
            if section == 'storage':
                # Music lives under data_dir, so its status box goes
                # right below the field that decides where to look
                self._build_music_box()
        self._preview_reload()

    def _build_images_note(self):
        """A pointer rather than a setting: none of the fields above can
        be judged by reading them, and the tab that turns them into a
        picture is one click away."""
        box = ttk.Frame(self.form, padding=(6, 0, 6, 6))
        box.pack(fill='x')
        lbl = ttk.Label(
            box, foreground='#777', wraplength=560, justify='left',
            font=('TkDefaultFont', 8),
            text='Try these on a real scene in the Illustrations tab: it '
                 'generates one picture using the form as it stands - '
                 'unsaved edits included - and shows what the C64 and the '
                 'Windows client would each display.')
        lbl.pack(anchor='w')
        for w in (box, lbl):
            w.bind('<MouseWheel>', self._form_wheel)
            w.bind('<Button-4>', self._form_wheel)
            w.bind('<Button-5>', self._form_wheel)

    def _build_music_box(self):
        """Music status: there are no music config keys (both library
        paths are derived from storage.data_dir), so this box reports
        instead of editing - whether the server would find each library,
        and how big it is."""
        box = ttk.LabelFrame(self.form, text='Music', padding=6)
        box.pack(fill='x', pady=(0, 8))
        box.columnconfigure(0, weight=1)
        self._music_labels = []
        for row in range(2):
            lbl = ttk.Label(box, text='', wraplength=560, justify='left')
            lbl.grid(row=row, column=0, sticky='w')
            self._music_labels.append(lbl)
        btn = ttk.Button(box, text='Refresh', command=self._refresh_music)
        btn.grid(row=0, column=1, rowspan=2, padx=(8, 0), sticky='e')
        hint = ttk.Label(
            box, text='Checked when this form loads; Refresh after a '
            'library rebuild. Paths follow the data directory above.',
            foreground='#777', wraplength=560, justify='left',
            font=('TkDefaultFont', 8))
        hint.grid(row=2, column=0, sticky='w', pady=(3, 0))
        for w in (box, btn, hint, *self._music_labels):
            w.bind('<MouseWheel>', self._form_wheel)
            w.bind('<Button-4>', self._form_wheel)
            w.bind('<Button-5>', self._form_wheel)
        self._refresh_music()

    def _refresh_music(self):
        data_dir = Path(self._field_value('storage', 'data_dir',
                                          'LLM64_DATA_DIR', './data'))
        if not data_dir.is_absolute():
            # same resolution the server gets: _on_start chdirs to the
            # config file's directory before it runs
            data_dir = Path(self.config_path.get()).resolve().parent \
                / data_dir
        for lbl, line in zip(self._music_labels,
                             music_status_lines(data_dir)):
            lbl.configure(text=line)

    # ---- illustration preview ----
    #
    # The settings that decide what a picture looks like - a style
    # preset, a LoRA, a workflow's steps and CFG, the style prefix
    # itself - are the ones you cannot judge by reading them. This tab
    # runs the real path (src/preview.py) against the form as it stands
    # RIGHT NOW, unsaved edits included, so the loop is edit -> Generate
    # -> look, not edit -> save -> restart -> boot a C64 -> play into a
    # scene -> /pic.

    def _build_preview_tab(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text='Illustrations')
        # Two 320x200 renders plus their controls are taller than a
        # laptop's share of this window, so the whole tab scrolls.
        tab, wheel = self._scroll_host(outer)
        self._preview_wheel = wheel

        self.preview_scene = tk.StringVar()
        self.preview_caption = tk.StringVar(value=preview.SAMPLE_CAPTION)
        self.preview_target = tk.StringVar(value=preview.TARGETS[0][1])
        self.preview_count = tk.StringVar(value='1')

        ctl = ttk.Frame(tab)
        ctl.pack(fill='x')
        ctl.columnconfigure(1, weight=1)

        ttk.Label(ctl, text='Scene').grid(row=0, column=0, sticky='w',
                                          padx=(0, 6))
        entry = ttk.Entry(ctl, textvariable=self.preview_scene)
        entry.grid(row=0, column=1, columnspan=5, sticky='ew')
        # The prompt box is a plain concatenation of a cached prefix and
        # this text, so it can follow every keystroke for free.
        self.preview_scene.trace_add('write',
                                     lambda *_: self._preview_show_prompt())

        ttk.Label(ctl, text='Sample scene').grid(row=1, column=0, sticky='w',
                                                 padx=(0, 6), pady=(4, 0))
        samples = ttk.Combobox(
            ctl, state='readonly', width=18,
            values=[name for name, _ in preview.SAMPLE_SCENES])
        samples.grid(row=1, column=1, sticky='w', pady=(4, 0))
        samples.bind('<<ComboboxSelected>>',
                     lambda e, c=samples: self.preview_scene.set(
                         dict(preview.SAMPLE_SCENES)[c.get()]))
        ttk.Label(ctl, text='Caption').grid(row=1, column=2, sticky='e',
                                            padx=(8, 6), pady=(4, 0))
        ttk.Entry(ctl, textvariable=self.preview_caption).grid(
            row=1, column=3, columnspan=3, sticky='ew', pady=(4, 0))

        ttk.Label(ctl, text='Prompt for').grid(row=2, column=0, sticky='w',
                                               padx=(0, 6), pady=(4, 0))
        target = ttk.Combobox(
            ctl, state='readonly', width=18,
            textvariable=self.preview_target,
            values=[label for _, label in preview.TARGETS])
        target.grid(row=2, column=1, sticky='w', pady=(4, 0))
        target.bind('<<ComboboxSelected>>',
                    lambda e: self._preview_show_prompt())
        ttk.Label(ctl, text='Count').grid(row=2, column=2, sticky='e',
                                          padx=(8, 6), pady=(4, 0))
        ttk.Spinbox(ctl, from_=1, to=PREVIEW_MAX_BATCH, width=4,
                    textvariable=self.preview_count).grid(
                        row=2, column=3, sticky='w', pady=(4, 0))
        self.btn_preview = ttk.Button(ctl, text='Generate',
                                      command=self._preview_generate)
        self.btn_preview.grid(row=2, column=4, padx=(8, 0), pady=(4, 0))
        self.btn_preview_stop = ttk.Button(ctl, text='Stop', state='disabled',
                                           command=self._preview_stop)
        self.btn_preview_stop.grid(row=2, column=5, padx=(4, 0), pady=(4, 0))

        prompt_box = ttk.LabelFrame(tab, text='Final prompt', padding=6)
        prompt_box.pack(fill='x', pady=(8, 0))
        self.preview_prompt = tk.Text(prompt_box, height=4, wrap='word',
                                      state='disabled',
                                      font=('TkFixedFont', 8))
        self.preview_prompt.pack(fill='x')
        line = ttk.Frame(prompt_box)
        line.pack(fill='x', pady=(4, 0))
        self.preview_source = ttk.Label(line, text='', foreground='#777',
                                        font=('TkDefaultFont', 8))
        self.preview_source.pack(side='left')
        ttk.Button(line, text='Reread settings',
                   command=self._preview_refresh).pack(side='right')
        self.preview_status = ttk.Label(line, text='')
        self.preview_status.pack(side='right', padx=8)

        self._build_preview_panels(tab)
        self._build_preview_strip(tab)
        self._bind_wheel(tab, wheel)
        # No refresh here: the form this reads has not been loaded yet
        # (__init__ builds the UI first). _load_editor calls it.

    def _build_preview_panels(self, tab):
        """The three views of one generation: what the backend drew, and
        what each client would actually display. All three matter - a
        picture that is wrong in the original is a prompt problem, and one
        that is fine there but mud on the C64 is a subject problem."""
        panels = ttk.Frame(tab)
        panels.pack(fill='both', expand=True, pady=(8, 0))
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)

        self.preview_panels = {}
        for i, (key, title) in enumerate((
                ('original', 'Generated original'),
                ('c64', 'C64 multicolor (160x200, 16 colours)'),
                ('vga', 'Windows 3.11 (320x200, 256 colours)'))):
            box = ttk.LabelFrame(panels, text=title, padding=4)
            box.grid(row=i // 2, column=i % 2, sticky='nsew',
                     padx=(0, 6) if i % 2 == 0 else 0, pady=(0, 6))
            # A canvas, not a Label: its width/height are pixels whether
            # it is holding a picture or the placeholder text, and it
            # centres a render that came out shorter than the frame.
            cv = tk.Canvas(box, width=PANEL_W, height=PANEL_H,
                           background='#222', highlightthickness=0)
            cv.pack()
            cv.create_text(PANEL_W // 2, PANEL_H // 2, fill='#888',
                           text='(nothing generated yet)')
            self.preview_panels[key] = cv

        box = ttk.LabelFrame(panels, text='This picture', padding=4)
        box.grid(row=1, column=1, sticky='nsew', pady=(0, 6))
        self.preview_detail = tk.Text(box, height=11, width=44, wrap='word',
                                      state='disabled',
                                      font=('TkFixedFont', 8))
        self.preview_detail.pack(fill='both', expand=True)

    def _build_preview_strip(self, tab):
        """Previous previews, newest first. Every generation is written to
        <data_dir>/previews/ with the settings that made it, so the strip
        survives a restart and the comparison you care about - this LoRA
        against last night's - is still there tomorrow."""
        box = ttk.LabelFrame(tab, text='Previous previews', padding=4)
        box.pack(fill='x')
        # tk.Canvas takes its colour from the X defaults, not the ttk
        # theme, so an unstyled one is a dark slab in the middle of the
        # tab. Borrow the frame colour it is sitting on.
        canvas = tk.Canvas(box, height=THUMB_H + 34, highlightthickness=0,
                           background=ttk.Style().lookup('TFrame',
                                                         'background'))
        hsb = ttk.Scrollbar(box, orient='horizontal', command=canvas.xview)
        canvas.configure(xscrollcommand=hsb.set)
        hsb.pack(side='bottom', fill='x')
        canvas.pack(side='top', fill='x')
        self.preview_strip = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self.preview_strip, anchor='nw')
        self.preview_strip.bind('<Configure>', lambda e: canvas.configure(
            scrollregion=canvas.bbox('all')))
        foot = ttk.Frame(box)
        foot.pack(side='bottom', fill='x')
        ttk.Button(foot, text='Reload', command=self._preview_load_history
                   ).pack(side='left', pady=(4, 0))
        self.preview_strip_note = ttk.Label(
            foot, text='', foreground='#777', font=('TkDefaultFont', 8))
        self.preview_strip_note.pack(side='left', padx=8)

    # ---- preview: settings and prompt ----

    def _preview_target_key(self):
        label = self.preview_target.get()
        for key, text in preview.TARGETS:
            if text == label:
                return key
        return preview.TARGETS[0][0]

    def _preview_config_text(self, quiet=False):
        """The config as the form has it, unsaved edits included. Returns
        None when the form cannot produce one - with a dialog when the
        user asked for this directly, quietly when it is a background
        refresh."""
        problem = None
        text = None
        if self._doc is None:
            problem = ('config.toml has a syntax error - fix it in the Raw '
                       'config tab, then Reload.')
        else:
            text, errors = self._collect_form()
            if errors:
                problem, text = '\n'.join(errors), None
        if problem is None:
            return text
        if quiet:
            self._preview_set_status(problem.replace('\n', '; '), ok=False)
        else:
            messagebox.showerror('Config invalid', problem)
        return None

    def _preview_data_dir(self):
        """Where previews are written and read, resolved exactly as
        _refresh_music resolves the music libraries."""
        data_dir = Path(self._field_value('storage', 'data_dir',
                                          'LLM64_DATA_DIR', './data'))
        if not data_dir.is_absolute():
            data_dir = Path(self.config_path.get()).resolve().parent \
                / data_dir
        return data_dir

    def _preview_reload(self):
        """What a (re)loaded config means for this tab. Called from
        _load_editor, so opening the launcher, saving, and Reload all
        leave the prompt and the strip current."""
        self._preview_refresh(deep=False)
        self._preview_load_history()

    def _preview_refresh(self, deep=True):
        """Reread the form: which style prefix each client would get, and
        (deep) whether the configured backend could run at all. The
        backend check builds a real Config, which re-logs the server's
        startup warnings - so it belongs on a button, not on every
        reload."""
        text = self._preview_config_text(quiet=not deep)
        if text is None:
            return
        try:
            images_cfg = preview.images_table(text)
        except preview.PreviewError as e:
            self._preview_set_status(str(e), ok=False)
            return
        self._preview_prefix = {
            key: preview.resolve_prefix(images_cfg, key)
            for key, _ in preview.TARGETS}
        self._preview_show_prompt()
        if not deep:
            return
        try:
            cfg = preview.config_from_text(text, self.config_path.get())
            _, label, problem = preview.backend_status(
                images_cfg, cfg, self.config_path.get())
        except preview.PreviewError as e:
            self._preview_set_status(str(e), ok=False)
            return
        if problem:
            self._preview_set_status(problem, ok=False)
        else:
            self._preview_set_status(f'Backend: {label}', ok=True)

    def _preview_set_status(self, text, ok=True):
        self.preview_status.configure(
            text=text if len(text) < 90 else text[:87] + '...',
            foreground='#2a7' if ok else '#cc2222')
        if not ok:
            logger.warning(f'preview: {text}')

    def _preview_show_prompt(self):
        """Prefix + scene, exactly as the backend would receive it."""
        prefix, source = self._preview_prefix.get(
            self._preview_target_key(), ('', 'settings not read yet'))
        self.preview_prompt.configure(state='normal')
        self.preview_prompt.delete('1.0', 'end')
        self.preview_prompt.insert('1.0', prefix + self.preview_scene.get())
        self.preview_prompt.configure(state='disabled')
        self.preview_source.configure(text=f'Style prefix from {source}')

    # ---- preview: generating ----

    def _preview_generate(self):
        if self._preview_busy:
            return
        scene = self.preview_scene.get().strip()
        if not scene:
            messagebox.showerror('Nothing to illustrate',
                                 'Type a scene, or pick a sample one.')
            return
        text = self._preview_config_text()
        if text is None:
            return
        try:
            count = max(1, min(PREVIEW_MAX_BATCH,
                               int(self.preview_count.get())))
        except ValueError:
            count = 1
        self.preview_count.set(str(count))
        self._preview_busy = True
        self._preview_cancel = False
        self.btn_preview.state(['disabled'])
        self.btn_preview_stop.state(['!disabled'])
        args = (text, self.config_path.get(), scene, self._preview_target_key(),
                self.preview_caption.get(), count)
        threading.Thread(target=self._preview_worker, args=args,
                         name='llm64-preview', daemon=True).start()

    def _preview_stop(self):
        """Cancel the rest of a batch. The generation already in flight
        finishes - a backend call is not interruptible, and an image that
        has been paid for should at least be shown."""
        self._preview_cancel = True
        self._preview_queue.put(('status', 'Stopping after this one...'))

    def _preview_worker(self, text, config_path, scene, target, caption,
                        count):
        """Off the UI thread: generate, convert, save, post back. Every
        result travels through the queue; nothing here touches a widget."""
        for i in range(count):
            if self._preview_cancel:
                break
            self._preview_queue.put(
                ('status', f'Generating {i + 1} of {count}...'))
            try:
                result = preview.generate_preview(
                    text, config_path, scene, target=target, caption=caption)
                self._preview_queue.put(('done', result))
            except preview.PreviewError as e:
                # The next attempt would fail the same way, so stop.
                self._preview_queue.put(('error', str(e)))
                break
            except Exception as e:
                self._preview_queue.put(
                    ('error', f'{type(e).__name__}: {e}'))
                break
        self._preview_queue.put(('finished', None))

    def _drain_preview(self):
        while True:
            try:
                kind, payload = self._preview_queue.get_nowait()
            except queue.Empty:
                return
            if kind == 'status':
                self.preview_status.configure(text=payload,
                                              foreground='#777')
            elif kind == 'done':
                self._preview_show(payload)
                self._preview_history.insert(0, {
                    'meta': payload.meta,
                    'images': {'original': payload.original,
                               'c64': payload.c64, 'vga': payload.vga},
                    'paths': {}})
                del self._preview_history[HISTORY_MAX:]
                self._preview_fill_strip()
            elif kind == 'error':
                self._preview_set_status(payload, ok=False)
                messagebox.showerror('Preview failed', payload)
            else:
                self._preview_busy = False
                self.btn_preview.state(['!disabled'])
                self.btn_preview_stop.state(['disabled'])

    # ---- preview: drawing ----

    def _photo(self, img, box=None):
        """A tk image from a PIL one. Encoded as PNG and handed to Tk's
        own decoder rather than going through PIL.ImageTk: one fewer
        optional Pillow component to be missing from a frozen build, and
        Tk has read PNG since 8.6."""
        import base64
        import io
        from PIL import Image
        if box and (img.width > box[0] or img.height > box[1]):
            img = img.copy()
            img.thumbnail(box, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        return tk.PhotoImage(
            data=base64.b64encode(buf.getvalue()).decode('ascii'),
            master=self)

    def _preview_show(self, result):
        """Put one preview in the panels. `result` is a preview.Preview
        (freshly generated) or a history entry loaded from disk."""
        images = (result['images'] if isinstance(result, dict)
                  else {'original': result.original, 'c64': result.c64,
                        'vga': result.vga})
        meta = result['meta'] if isinstance(result, dict) else result.meta
        for key, canvas in self.preview_panels.items():
            canvas.delete('all')
            img = images.get(key)
            if img is None:
                canvas.create_text(PANEL_W // 2, PANEL_H // 2, fill='#888',
                                   text='(this render was not saved)')
                self._preview_photos.pop(key, None)
                continue
            photo = self._photo(img, (PANEL_W, PANEL_H))
            self._preview_photos[key] = photo    # tk holds no reference
            canvas.create_image(PANEL_W // 2, PANEL_H // 2, image=photo)
        self._preview_detail(meta)
        self._preview_set_status('Done', ok=True)

    def _preview_detail(self, meta):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S',
                              time.localtime(meta.get('time', 0)))
        lines = [
            f"when     {stamp}",
            f"prompt   written for the {meta.get('target', '?')} client",
            f"prefix   {meta.get('prefix_source', '?')}",
            f"backend  {meta.get('backend', '?')}",
            f"style    {meta.get('style') or '(none)'}",
            f"caption  {meta.get('caption') or '(none)'}",
        ]
        comfy = meta.get('comfyui') or {}
        if comfy:
            lines.append('comfyui  ' + ', '.join(
                f'{k}={v}' for k, v in sorted(comfy.items())))
        for k, v in sorted((meta.get('vars') or {}).items()):
            lines.append(f'{k.lower():<8} {v}')
        lines += ['', 'scene', meta.get('scene', '')]
        self.preview_detail.configure(state='normal')
        self.preview_detail.delete('1.0', 'end')
        self.preview_detail.insert('1.0', '\n'.join(lines))
        self.preview_detail.configure(state='disabled')

    def _preview_load_history(self):
        """Rebuild the strip from disk. Anything generated this session is
        already saved there, so this is also how the strip recovers if a
        preview was written by a different launcher window."""
        entries = preview.list_previews(self._preview_data_dir(),
                                        limit=HISTORY_MAX)
        self._preview_history = [
            {'meta': e['meta'], 'images': {},
             'paths': {k: e[k] for k in ('original', 'c64', 'vga')}}
            for e in entries]
        self._preview_fill_strip()

    def _preview_fill_strip(self):
        from PIL import Image
        for child in self.preview_strip.winfo_children():
            child.destroy()
        for entry in self._preview_history:
            # Cached on the entry, which the history list keeps alive:
            # the strip is rebuilt after every generation, and re-reading
            # two dozen PNGs each time to redraw the same thumbnails is
            # work for nothing.
            photo = entry.get('thumb')
            if photo is None:
                img = entry['images'].get('c64')
                if img is None:
                    path = entry['paths'].get('c64')
                    if path is None or not path.exists():
                        continue
                    try:
                        with Image.open(path) as opened:
                            img = opened.convert('RGB')
                    except OSError:
                        continue
                photo = entry['thumb'] = self._photo(img, (THUMB_W, THUMB_H))
            cell = ttk.Frame(self.preview_strip)
            cell.pack(side='left', padx=(0, 6))
            btn = tk.Button(cell, image=photo, bd=1,
                            command=lambda e=entry: self._preview_open(e))
            btn.pack()
            meta = entry['meta']
            ttk.Label(cell, font=('TkDefaultFont', 8), foreground='#777',
                      text=(time.strftime('%H:%M',
                                          time.localtime(meta.get('time', 0)))
                            + f" {meta.get('target', '')}")).pack()
        # The thumbnails are built after the tab was, so they need the
        # page's wheel handler handed to them here.
        self._bind_wheel(self.preview_strip, self._preview_wheel)
        note = (f'{len(self._preview_history)} in '
                f'{self._preview_data_dir() / preview.PREVIEW_SUBDIR} '
                f'- click one to bring it back')
        self.preview_strip_note.configure(
            text=note if self._preview_history else
            f'Nothing yet. Generated previews are saved in '
            f'{self._preview_data_dir() / preview.PREVIEW_SUBDIR}.')

    def _preview_open(self, entry):
        """A thumbnail was clicked: load its full renders (from memory if
        it is from this session, from disk otherwise) and show it."""
        from PIL import Image
        if not entry['images']:
            for key, path in entry['paths'].items():
                if path is None or not path.exists():
                    continue
                try:
                    with Image.open(path) as opened:
                        entry['images'][key] = opened.convert('RGB')
                except OSError as e:
                    logger.warning(f'preview {path}: {e}')
        self._preview_show(entry)
        # Restore the settings that made it, so "make it a bit darker"
        # starts from the picture you liked rather than from whatever was
        # last typed.
        meta = entry['meta']
        if meta.get('scene'):
            self.preview_scene.set(meta['scene'])
        self.preview_caption.set(meta.get('caption', ''))
        for key, label in preview.TARGETS:
            if key == meta.get('target'):
                self.preview_target.set(label)
        self._preview_show_prompt()

    # ---- model discovery ----

    def _field_value(self, section, key, env=None, default=''):
        """What the user has in a form field right now (not what is
        saved), falling back to an env var and then a default - the same
        precedence the server applies at start."""
        entry = self._fields.get((section, key))
        val = entry[0].get().strip() if entry else ''
        if not val and env:
            val = os.environ.get(env, '').strip()
        return val or default

    def _discover(self, source, combo, btn):
        """↻ pressed: fill the combobox's dropdown with live choices.
        Network sources run on a throwaway thread and come back through
        _discover_queue; the workflows source is a local scan."""
        if source == 'workflows':
            combo['values'] = self._workflow_choices()
            combo.focus_set()
            combo.event_generate('<Down>')
            return
        if source == 'styles':
            # Local too: built-in presets plus this config's own
            # [images.styles.*] tables. No endpoint involved.
            combo['values'] = self._style_choices()
            combo.focus_set()
            combo.event_generate('<Down>')
            return
        try:
            fetch = self._discover_fetch(source)
        except discovery.DiscoveryError as e:
            messagebox.showerror('Discovery failed', str(e))
            return
        btn.state(['disabled'])
        threading.Thread(
            target=lambda: self._discover_queue.put(
                (combo, btn, self._discover_run(fetch))),
            name='llm64-discover', daemon=True).start()

    @staticmethod
    def _discover_run(fetch):
        try:
            return fetch()
        except Exception as e:
            return e

    def _discover_fetch(self, source):
        """A zero-arg callable for the worker thread, with everything it
        needs captured here on the UI thread (tk vars are not for other
        threads to read)."""
        if source == 'llm':
            base = self._field_value('api', 'base_url', 'OPENAI_API_BASE',
                                     'https://api.openai.com/v1')
            key = self._field_value('api', 'key', 'OPENAI_API_KEY')
            return lambda: discovery.openai_models(base, key)
        if source == 'openai_images':
            base = self._field_value('images.openai', 'base_url', None,
                                     'https://api.openai.com/v1')
            key = self._field_value('images.openai', 'key',
                                    'LLM64_IMAGES_KEY')
            return lambda: discovery.openai_models(base, key)
        if source == 'gemini':
            key = self._field_value('images.gemini', 'key',
                                    'GEMINI_API_KEY')
            if not key:
                raise discovery.DiscoveryError(
                    'Set the Gemini API key (or GEMINI_API_KEY) first')
            return lambda: discovery.gemini_image_models(key)
        url = self._field_value('images.comfyui', 'url', None,
                                'http://127.0.0.1:8188')
        nodes = {'comfy_model': discovery.COMFY_MODEL_NODES,
                 'comfy_clip': discovery.COMFY_CLIP_NODES,
                 'comfy_vae': discovery.COMFY_VAE_NODES,
                 'comfy_lora': discovery.COMFY_LORA_NODES}[source]
        return lambda: discovery.comfy_model_choices(url, nodes)

    def _workflow_choices(self):
        """Workflow files a config can point at: bundled ones by bare
        name (imagegen falls back to the bundle for those, which also
        holds inside a frozen binary), plus JSON next to config.toml and
        in its workflows/ folder, as config-relative paths."""
        config_dir = Path(self.config_path.get()).resolve().parent
        choices = {}    # label -> resolved path, first one wins
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

    def _style_choices(self):
        """Style preset names: the built-ins plus any [images.styles.*]
        tables in the form's loaded document (which may hold unsaved
        edits from other fields, but tables only change via Raw/Reload,
        so the doc is the truth for them)."""
        from .imgstyles import PRESETS
        names = set(PRESETS)
        tables = _dig(self._doc, 'images.styles') if self._doc else None
        if isinstance(tables, dict):
            names.update(k for k, v in tables.items()
                         if isinstance(v, dict))
        return sorted(names)

    def _drain_discovery(self):
        while True:
            try:
                combo, btn, result = self._discover_queue.get_nowait()
            except queue.Empty:
                return
            # The form may have been rebuilt (Reload) since the fetch
            # started; stale widgets just drop their result.
            if btn.winfo_exists():
                btn.state(['!disabled'])
            if not combo.winfo_exists():
                continue
            if isinstance(result, discovery.DiscoveryError):
                messagebox.showerror('Discovery failed', str(result))
            elif isinstance(result, Exception):
                messagebox.showerror(
                    'Discovery failed',
                    f'{type(result).__name__}: {result}')
            else:
                combo['values'] = result
                combo.focus_set()
                combo.event_generate('<Down>')

    # ---- form -> toml ----

    def _collect_form(self):
        """Fold the form's changes into the tomlkit doc. Returns
        (toml_text, errors). Untouched fields are left alone, so the
        file's comments and unknown keys survive."""
        errors = []
        doc = self._doc
        for (section, key), (var, ftype, initial, present) in \
                self._fields.items():
            value = var.get()
            if value == initial:
                continue
            if ftype.startswith('bool'):
                _dig(doc, section, create=True)[key] = bool(value)
                continue
            value = value.strip() if isinstance(value, str) else value
            if value == '':
                # blanked out: drop the key, the server default returns
                table = _dig(doc, section)
                if table is not None and key in table:
                    del table[key]
                continue
            try:
                if ftype == 'int':
                    value = int(value)
                elif ftype == 'float':
                    value = float(value)
            except ValueError:
                errors.append(f'{section}.{key}: {value!r} is not a number')
                continue
            _dig(doc, section, create=True)[key] = value
        return tomlkit.dumps(doc), errors

    def _validate_form(self):
        if self._doc is None:
            return
        text, errors = self._collect_form()
        problem = errors[0] if errors else \
            validate_config_text(text, self.config_path.get())
        if problem:
            messagebox.showerror('Config invalid', problem)
        else:
            messagebox.showinfo('Config valid',
                                'The server accepts this configuration.')

    def _save_form(self):
        if self._doc is None:
            messagebox.showerror(
                'Config invalid',
                'The file has a syntax error - fix it in the Raw config '
                'tab first.')
            return False
        text, errors = self._collect_form()
        if errors:
            messagebox.showerror('Config invalid', '\n'.join(errors))
            return False
        problem = validate_config_text(text, self.config_path.get())
        if problem and not messagebox.askyesno(
                'Config invalid', f'{problem}\n\nSave anyway?'):
            return False
        Path(self.config_path.get()).write_text(text)
        logger.info(f'Saved {self.config_path.get()}')
        self._load_editor()
        return True

    def _save_and_restart(self):
        if self._save_form():
            self._on_restart()

    # ---- raw tab ----

    def _validate_raw(self):
        problem = validate_config_text(
            self.raw_text.get('1.0', 'end-1c'), self.config_path.get())
        if problem:
            messagebox.showerror('Config invalid', problem)
        else:
            messagebox.showinfo('Config valid',
                                'The server accepts this configuration.')

    def _save_raw(self):
        text = self.raw_text.get('1.0', 'end-1c')
        problem = validate_config_text(text, self.config_path.get())
        if problem and not messagebox.askyesno(
                'Config invalid', f'{problem}\n\nSave anyway?'):
            return
        Path(self.config_path.get()).write_text(text)
        logger.info(f'Saved {self.config_path.get()}')
        self._load_editor()

    def _create_config(self):
        path = Path(self.config_path.get())
        if path.exists():
            messagebox.showinfo('Config exists',
                                f'{path} already exists - editing that.')
            return
        template = _template_path()
        if template is None:
            logger.error('config.toml.example not found in the bundle')
            return
        shutil.copy(template, path)
        logger.info(f'Created {path} from the template')
        self._load_editor()

    # ---- server control ----

    def _server_host_port(self):
        """[server] host/port from the file - launcher-owned keys the
        Config class never reads (the CLI takes them as flags)."""
        host, port = '0.0.0.0', 6400
        try:
            doc = tomlkit.parse(Path(self.config_path.get()).read_text())
            table = _dig(doc, 'server') or {}
            host = str(table.get('host', host))
            port = int(table.get('port', port))
        except Exception:
            pass
        return host, port

    def _on_start(self):
        snap = self.ctl.snapshot()
        if snap['alive']:
            return
        path = self.config_path.get()
        if Path(path).exists():
            # './data' and './cards' in the config mean "next to the
            # config file", same as running the CLI from that directory
            os.chdir(Path(path).resolve().parent)
        host, port = self._server_host_port()
        logger.info(f'Starting server on {host}:{port}')
        self.ctl.start(path, host, port)

    def _on_stop(self):
        self.ctl.stop()
        self._stop_deadline = time.time() + STOP_TIMEOUT_S

    def _on_restart(self):
        snap = self.ctl.snapshot()
        if snap['alive']:
            self._restart_pending = True
            self._on_stop()
        else:
            self._on_start()

    def _on_close(self):
        snap = self.ctl.snapshot()
        if snap['alive']:
            self._quit_after_stop = True
            self._on_stop()
            # the tick loop destroys the window once the thread is done;
            # this deadline is the backstop for a wedged shutdown
            self.after(int(STOP_TIMEOUT_S * 1000) + 500, self.destroy)
        else:
            self.destroy()

    # ---- periodic UI refresh ----

    def _tick(self):
        self._drain_log()
        self._drain_discovery()
        self._drain_preview()
        snap = self.ctl.snapshot()
        self._update_status(snap)
        self._ensure_file_log(snap)

        if not snap['alive']:
            if self._quit_after_stop:
                self.destroy()
                return
            if self._restart_pending:
                self._restart_pending = False
                self._on_start()
        elif self._stop_deadline and time.time() > self._stop_deadline:
            self._stop_deadline = None
            logger.error('Server thread did not stop in time - it may be '
                         'wedged; quitting the launcher will end it.')
        self.after(POLL_MS, self._tick)

    def _update_status(self, snap):
        state = snap['state']
        colors = {'running': '#22aa22', 'error': '#cc2222',
                  'stopped': '#888888'}
        self.status_dot.configure(fg=colors.get(state, '#b8860b'))
        if state == 'running':
            text = f"Running on {snap['addr']}"
        elif state == 'error':
            text = f"Error: {snap['error'] or 'see log'}"
        else:
            text = state.capitalize()
        self.status_text.configure(text=text)

        if snap['last_activity']:
            ago = int(time.time() - snap['last_activity'])
            last = f'{ago // 60}m ago' if ago >= 60 else f'{ago}s ago'
        else:
            last = 'never'
        self.stats_text.configure(
            text=f"C64 connected: {snap['clients']}   "
                 f"connections: {snap['total']}   "
                 f"LLM calls: {snap['api_calls']}   last: {last}")

        busy = snap['alive']
        self.btn_start.state(['disabled'] if busy else ['!disabled'])
        self.btn_stop.state(['!disabled'] if busy else ['disabled'])

    def _ensure_file_log(self, snap):
        """Once the server knows its data_dir, mirror the log to a file
        there and say so in the UI."""
        data_dir = snap['data_dir']
        if not data_dir:
            return
        path = str(Path(data_dir) / 'proxy.log')
        if path == self._file_handler_path:
            return
        root = logging.getLogger()
        if self._file_handler:
            root.removeHandler(self._file_handler)
            self._file_handler.close()
        try:
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
        except OSError as e:
            logger.warning(f'Cannot write log file {path}: {e}')
            return
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        root.addHandler(handler)
        self._file_handler = handler
        self._file_handler_path = path
        self.logfile_label.configure(text=f'Log file: {Path(path).resolve()}')

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state='normal')
        for levelno, line in lines:
            tag = ('error' if levelno >= logging.ERROR
                   else 'warn' if levelno >= logging.WARNING else None)
            self.log_text.insert('end', line + '\n', tag or ())
        overflow = int(self.log_text.index('end-1c').split('.')[0]) \
            - LOG_PANE_MAX_LINES
        if overflow > 0:
            self.log_text.delete('1.0', f'{overflow + 1}.0')
        self.log_text.configure(state='disabled')
        if self.autoscroll.get():
            self.log_text.see('end')

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def _browse(self):
        path = filedialog.askopenfilename(
            title='Choose config.toml',
            filetypes=[('TOML config', '*.toml'), ('All files', '*')])
        if path:
            self.config_path.set(path)
            self._load_editor()


# --------------------------------------------------------------------------

def default_config_path():
    """Next to the executable when frozen (the natural place to keep a
    config beside server.exe), the working directory otherwise."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'config.toml'
    return Path('config.toml')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='LLM64 proxy launcher')
    parser.add_argument('--config', default=None,
                        help='config.toml path (default: next to the '
                             'executable)')
    args = parser.parse_args(argv)

    if sys.platform == 'win32':
        # per-monitor DPI awareness so tk is not blurry on hidpi
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    config = Path(args.config) if args.config else default_config_path()
    app = LauncherApp(config)
    app.mainloop()


if __name__ == '__main__':
    main()
