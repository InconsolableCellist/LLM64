"""The setup wizard's logic, and the config plumbing it shares with the
launcher.

The Settings tab is a flat editor: every key the proxy reads, in file
order, with no opinion about which of them you actually need. That is
the right shape once you know the system and the wrong one the first
time you open it. Nothing there tells you that the LLM endpoint is the
only setting the proxy cannot start usefully without, that music needs a
library you have to build yourself before any of it does anything, or
that /print already works without owning a printer if you use the
Windows client.

So this module describes setup as an ordered list of steps - each one
mandatory or optional, each one carrying the fields it owns and a check
that answers "is this actually working?" by asking the live system
rather than by reading the text back out of the file. A step you cannot
finish today (an empty SID directory, a printer that has not arrived) is
a step you leave alone: the wizard is re-runnable and says so, and
nothing it writes depends on the order you did things in.

No tkinter in here. The window is wizard.py; this half stays testable
without a display (tests/test_setupwiz.py).
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .respath import resource_dir

logger = logging.getLogger(__name__)

# Bumped when the steps change enough that an operator who finished an
# older wizard should be offered the new one. The marker in config.toml
# records the version that was completed, so this is a comparison, not a
# boolean.
WIZARD_VERSION = 1

# Where completion is recorded. A table of its own, so it can never
# collide with a key the server reads, and so `[wizard]` in a config
# file reads as what it is: something the wizard wrote about itself.
MARKER_SECTION = 'wizard'

# The api values config.toml.example ships. A config still carrying
# them, with no key set anywhere, has been created but never filled in -
# which is the case the wizard exists for.
TEMPLATE_API = {
    'base_url': 'https://api.openai.com/v1',
    'model': 'gpt-3.5-turbo',
}

# The LLM probe. One tiny completion, because the only thing that proves
# an endpoint/key/model triple is right is a reply coming back through
# it - GET /models answers for the endpoint and says nothing about
# whether the model will load or the key can spend.
PROBE_PROMPT = 'Reply with the single word: ready'
PROBE_MAX_TOKENS = 32
# Generous: a local llama.cpp loads the model on the first request, and
# on a cold GPU that is minutes. Failing a probe that would have
# answered teaches the operator the wrong thing.
PROBE_TIMEOUT = 180
MAX_BYTES = 1024 * 1024

TEST_PAGE = """LLM64 proxy - printer test page

If you are reading this on paper, the CUPS leg of /print works: the
proxy reached the queue, the queue reached the printer, and the
printer had paper in it.

Sent by the setup wizard at {when}.
"""


# --------------------------------------------------------------------------
# Results

@dataclass
class Result:
    """What a check found. `ok` is True (working), False (broken, and
    `detail` says what to do about it) or None (not run, or nothing to
    check). Every message here is shown to the operator, so - like every
    message in discovery.py and imagegen.py - it never carries a key."""

    ok: object = None
    summary: str = ''
    detail: str = ''

    @property
    def symbol(self):
        return {True: 'OK', False: '!'}.get(self.ok, '-')


# --------------------------------------------------------------------------
# Config plumbing (shared with launcher.py)

def dig(doc, dotted, create=False):
    """The table a dotted section name refers to, or None."""
    node = doc
    for part in dotted.split('.'):
        if not isinstance(node, dict) or part not in node:
            if not create:
                return None
            node[part] = tomlkit.table()
        node = node[part]
    return node


def template_path():
    """config.toml.example, in a checkout or in the bundle."""
    for base in (resource_dir().parent, resource_dir()):
        p = base / 'config.toml.example'
        if p.exists():
            return p
    return None


def validate_config_text(text, config_path):
    """None if the text is a config the server would accept, else the
    complaint. Runs the real Config parser against a scratch file in the
    same directory, so relative paths resolve exactly as they will at
    start time."""
    from .config import Config
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


def set_key(doc, section, key, value):
    """Write one key into a document. An empty string removes it, which
    is how a field gets handed back to the server's own default - the
    same rule the Settings tab's blank-means-unset uses."""
    if isinstance(value, str):
        value = value.strip()
    if value == '' or value is None:
        table = dig(doc, section)
        if isinstance(table, dict) and key in table:
            del table[key]
        return
    dig(doc, section, create=True)[key] = value


class ConfigDoc:
    """config.toml as an editable document.

    The wizard owns the file while it is open: it reads on entry, writes
    whole steps at a time, and the launcher reloads its own editor when
    the window closes. That is simpler than binding two forms to one
    document, and it means a wizard run that is abandoned half way
    through still leaves every step it did finish on disk.

    tomlkit, not toml, for the reason the launcher uses it: config.toml
    is mostly comments and a round trip through toml.dump would take
    every one of them out.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.doc = tomlkit.document()
        self.error = None
        self.load()

    def load(self):
        """Reread from disk. `error` is set (and the document left empty)
        when the file will not parse - the wizard refuses to run then,
        because it cannot write into a document it could not read."""
        self.error = None
        if not self.path.exists():
            self.doc = tomlkit.document()
            return
        try:
            self.doc = tomlkit.parse(self.path.read_text())
        except Exception as e:
            self.doc = tomlkit.document()
            self.error = str(e)

    def exists(self):
        return self.path.exists()

    def get(self, section, key, default=None):
        table = dig(self.doc, section)
        if not isinstance(table, dict) or key not in table:
            return default
        return table[key]

    def set(self, section, key, value):
        set_key(self.doc, section, key, value)

    def text(self):
        return tomlkit.dumps(self.doc)

    def text_with(self, overrides):
        """What the file would say with these {(section, key): value}
        edits folded in, without touching the real document.

        A check reads the fields the operator has just typed, not the
        ones last saved - but writing them in to read them back and
        undoing it afterwards would not be a no-op: tomlkit reattaches a
        deleted key at the end of its table, taking its comment with it.
        So the edits land on a copy.
        """
        import copy
        doc = copy.deepcopy(self.doc)
        for (section, key), value in overrides.items():
            set_key(doc, section, key, value)
        return tomlkit.dumps(doc)

    def validate(self):
        return validate_config_text(self.text(), str(self.path))

    def save(self):
        """Write the file. Returns None, or the reason it could not."""
        try:
            self.path.write_text(self.text())
        except OSError as e:
            return str(e)
        logger.info(f'Saved {self.path}')
        return None


    def create_from_template(self):
        """Copy config.toml.example into place. Returns None, or why
        not. An existing file is never overwritten - it is the one file
        in this program the operator cannot get back."""
        if self.path.exists():
            return f'{self.path} already exists'
        src = template_path()
        if src is None:
            return 'config.toml.example is not in this build'
        try:
            shutil.copy(src, self.path)
        except OSError as e:
            return str(e)
        logger.info(f'Created {self.path} from the template')
        self.load()
        return None

    def mark_completed(self):
        """Record that the wizard was run to the end, so it stops
        opening itself. Visible in the file, in a table of its own, and
        deleting it brings the first-run behavior back."""
        table = dig(self.doc, MARKER_SECTION, create=True)
        table['completed'] = WIZARD_VERSION
        table['when'] = time.strftime('%Y-%m-%d %H:%M:%S')


class Rollback:
    """What every file this window touches looked like before it did.

    The wizard writes a step as you leave it, which is what lets an
    abandoned run keep the part you finished - and it is also what makes
    Cancel a real question rather than a no-op, because by the time you
    press it the earlier steps are already on disk. So the first time a
    path is opened or created, its contents are put here (None for "there
    was no file"), and Cancel puts every one of them back.

    Keyed by path rather than held on the ConfigDoc: the config step can
    point the wizard at a different file mid-run, and a Cancel after that
    has two files to answer for.
    """

    def __init__(self):
        self.before = {}      # resolved path -> text, or None if absent

    def note(self, path):
        """Record a file's state, once. Later calls are ignored - the
        first sighting is the one Cancel has to get back to."""
        path = Path(path).resolve()
        if path in self.before:
            return
        try:
            self.before[path] = path.read_text() if path.exists() else None
        except OSError as e:
            # Unreadable now means unrestorable later, and pretending
            # otherwise is worse than admitting it: leave it unrecorded
            # so `changed` never claims it can undo this one.
            logger.warning(f'cannot snapshot {path} for rollback: {e}')

    def changed(self):
        """The paths whose contents differ from what was recorded."""
        out = []
        for path, before in self.before.items():
            try:
                now = path.read_text() if path.exists() else None
            except OSError:
                continue
            if now != before:
                out.append(path)
        return out

    def restore(self):
        """Put every recorded file back. Returns a list of complaints,
        empty when it all worked.

        A file that did not exist before is deleted, because that is what
        "back to before" means for one the wizard created - and the only
        thing it can contain is the template plus this run's edits, which
        is precisely what is being discarded.
        """
        problems = []
        for path, before in self.before.items():
            try:
                if before is None:
                    if path.exists():
                        path.unlink()
                        logger.info(f'Rolled back: removed {path}')
                elif path.read_text() != before:
                    path.write_text(before)
                    logger.info(f'Rolled back: restored {path}')
            except OSError as e:
                problems.append(f'{path}: {e}')
        return problems


def should_autorun(config_path):
    """(open the wizard unasked?, why). True on a machine that has not
    been set up: no config file at all, or a config still carrying the
    template's placeholder endpoint with no key anywhere.

    An operator who already has a working config is never interrupted -
    theirs points somewhere real, or has a key, or has the marker. A
    config that will not parse is not this window's problem either; the
    launcher sends that one to the Raw config tab.
    """
    path = Path(config_path)
    if not path.exists():
        return True, 'there is no config.toml yet'
    try:
        doc = tomlkit.parse(path.read_text())
    except Exception:
        return False, ''
    marker = dig(doc, MARKER_SECTION)
    if isinstance(marker, dict) and 'completed' in marker:
        try:
            if int(marker['completed']) >= WIZARD_VERSION:
                return False, ''
        except (TypeError, ValueError):
            return False, ''
        return False, ''
    api = dig(doc, 'api')
    if not isinstance(api, dict):
        return True, 'config.toml has no [api] section'
    if str(api.get('key', '')).strip() or os.environ.get('OPENAI_API_KEY'):
        return False, ''
    base = str(api.get('base_url', '')).strip()
    model = str(api.get('model', '')).strip()
    if base in ('', TEMPLATE_API['base_url']) and \
            model in ('', TEMPLATE_API['model']):
        return True, 'config.toml still has the template endpoint and no key'
    return False, ''


# --------------------------------------------------------------------------
# Checks: the config file itself

def check_config(config_path):
    """Whether the server would accept the file as it stands. Runs the
    real parser, so a typo in a section the wizard never touched is
    caught here rather than at the next start."""
    path = Path(config_path)
    if not path.exists():
        return Result(False, 'there is no file at this path yet',
                      'Create it from the template, or carry on - the next '
                      'step writes one when it saves.')
    try:
        text = path.read_text()
    except OSError as e:
        return Result(False, 'the file cannot be read', str(e))
    problem = validate_config_text(text, str(path))
    if problem:
        return Result(False, 'the server would refuse this file', problem)
    return Result(True, 'the file loads',
                  f'{path} parses and the proxy accepts it as it stands.')


# --------------------------------------------------------------------------
# Checks: the LLM

def _http_json(url, headers, body=None, timeout=30):
    """POST/GET JSON, blocking. Raises OSError-family errors for the
    caller to turn into a Result."""
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('response over the size cap')
    return json.loads(raw)


def _http_hint(code, base):
    """What an HTTP status from a chat endpoint usually means. Guesses,
    but they are the guesses an operator would otherwise spend twenty
    minutes arriving at."""
    if code in (401, 403):
        return ('The endpoint rejected the key. A cloud provider needs a '
                'real one; a local server usually accepts anything, so '
                'this being a local URL points at a proxy in front of it.')
    if code == 404:
        return (f'No chat endpoint at {base}/chat/completions. Most '
                f'OpenAI-compatible servers want the /v1 on the end of '
                f'the base URL - check that first.')
    if code in (400, 422):
        return ('The endpoint understood the request and refused it, '
                'which is nearly always the model name. Use the button '
                'next to the Model field to list what it serves.')
    if code == 429:
        return 'Rate limited or out of quota. The endpoint and key work.'
    if code >= 500:
        return ('The endpoint failed on its own side. For llama.cpp that '
                'is usually the model failing to load - its console says '
                'why.')
    return ''


def check_llm(base_url, key, model, timeout=PROBE_TIMEOUT):
    """Send one small completion and report what came back.

    This is the only mandatory check in the wizard, so it goes all the
    way: the round trip proves the URL, the key and the model name
    together, and nothing short of it does.
    """
    base = (base_url or '').strip().rstrip('/')
    if not base:
        return Result(False, 'no base URL',
                      'Fill in the base URL - for a local llama.cpp that '
                      'is usually http://localhost:8080/v1')
    if not (model or '').strip():
        return Result(False, 'no model',
                      'Fill in the model name. The button next to the '
                      'field lists what the endpoint serves.')
    headers = {'Content-Type': 'application/json'}
    if (key or '').strip() and key.strip() != 'none':
        headers['Authorization'] = f'Bearer {key.strip()}'
    body = json.dumps({
        'model': model.strip(),
        'messages': [{'role': 'user', 'content': PROBE_PROMPT}],
        'max_tokens': PROBE_MAX_TOKENS,
        'stream': False,
        # Same reason api_client sends it: a thinking model would spend
        # the whole probe budget reasoning and answer with empty text.
        'chat_template_kwargs': {'enable_thinking': False},
    }).encode('utf-8')

    started = time.time()
    try:
        payload = _http_json(f'{base}/chat/completions', headers, body,
                             timeout)
    except urllib.error.HTTPError as e:
        return Result(False, f'HTTP {e.code} from the endpoint',
                      _http_hint(e.code, base))
    except urllib.error.URLError as e:
        return Result(False, f'cannot reach {base}',
                      f'{e.reason}. Check the server is running and that '
                      f'the host and port are right.')
    except (OSError, ValueError) as e:
        return Result(False, 'the endpoint answered with something unusable',
                      f'{type(e).__name__}: {e}')

    took = time.time() - started
    try:
        text = (payload['choices'][0]['message'].get('content') or '').strip()
    except (KeyError, IndexError, TypeError):
        return Result(False, 'not an OpenAI-compatible reply',
                      'The endpoint answered, but not with a chat '
                      'completion. Check the base URL points at the API '
                      'root and not at a web UI.')
    if not text:
        return Result(True, f'answered in {took:.1f}s, with no text',
                      'The round trip works, so the URL, key and model are '
                      'right. An empty reply usually means a thinking '
                      'model spent the probe\'s small token budget before '
                      'it said anything.')
    return Result(True, f'answered in {took:.1f}s',
                  f'The model replied: {text[:200]}')


def list_models(base_url, key):
    """Model ids the endpoint serves, or a DiscoveryError."""
    from . import discovery
    return discovery.openai_models(base_url, key)


# --------------------------------------------------------------------------
# Checks: where the C64 dials

def local_addresses():
    """This machine's LAN addresses, best guess, most useful first.

    The C64 has to be told an address, and "which of my IPs is the one
    the C64 can see" is the question a first run actually gets stuck on.
    The UDP socket never sends a packet - connect() on a datagram socket
    only picks the route - so this works with no network traffic and no
    name lookup.
    """
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))     # TEST-NET-1, never routed
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith('127.'):
                found.append(addr)
    except OSError:
        pass
    return found


def check_port(host, port, running=False):
    """Whether the server could take this port, and what the C64 should
    dial to reach it."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return Result(False, f'{port!r} is not a port number', '')
    if not 1 <= port <= 65535:
        return Result(False, f'{port} is not a usable port number', '')
    addrs = local_addresses()
    where = (f'On the C64, dial {addrs[0]} port {port}.' if addrs else
             f'Dial this machine on port {port}.')
    if len(addrs) > 1:
        where += ('  This machine also answers on ' +
                  ', '.join(addrs[1:]) + '.')
    if running:
        return Result(True, f'the proxy is running on port {port}', where)
    bind_host = (host or '').strip() or '0.0.0.0'
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, port))
    except OSError as e:
        return Result(False, f'cannot listen on {bind_host}:{port}',
                      f'{e}. Something else is probably using the port - '
                      f'pick another one, and set the same number on the '
                      f'C64.')
    finally:
        sock.close()
    note = ''
    if bind_host.startswith('127.'):
        note = ('  The bind address is loopback, so nothing outside this '
                'machine can connect. Use 0.0.0.0 for a real C64.')
    return Result(True, f'{bind_host}:{port} is free', where + note)


# --------------------------------------------------------------------------
# Checks: images

def check_images(config_text, config_path):
    """Whether the configured image backend could run. Never generates -
    available() is a local check by contract, so this is free and the
    operator decides when to spend money on a real picture."""
    from . import preview
    try:
        images_cfg = preview.images_table(config_text)
    except preview.PreviewError as e:
        return Result(False, 'the [images] table is not usable', str(e))
    if not images_cfg:
        return Result(None, 'no image backend configured',
                      'Pictures are off. Everything else works without '
                      'them.')
    if str(images_cfg.get('mode', 'ask')) == 'off':
        return Result(None, 'images are switched off',
                      'mode = "off" ignores the model\'s illustration '
                      'directives. An explicit /pic still works.')
    try:
        cfg = preview.config_from_text(config_text, config_path)
        _, label, problem = preview.backend_status(images_cfg, cfg,
                                                   config_path)
    except preview.PreviewError as e:
        return Result(False, 'the image settings do not load', str(e))
    if problem:
        return Result(False, 'the backend cannot run', problem)
    return Result(True, f'{label} is ready',
                  'Nothing has been generated yet. Use Test picture below, '
                  'or the launcher\'s Illustrations tab, to see what these '
                  'settings actually draw.')


# --------------------------------------------------------------------------
# Checks: music

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


def check_music(data_dir):
    """Both libraries in one verdict. Neither one existing is a normal,
    finished state - the wizard says so rather than showing a red mark
    at somebody who does not want music."""
    lines = music_status_lines(data_dir)
    built = [ln for ln in lines if ' tunes, ' in ln]
    detail = '\n'.join(lines)
    if len(built) == 2:
        return Result(True, 'both music libraries are built', detail)
    if built:
        return Result(True, 'one of the two music libraries is built',
                      detail)
    return Result(None, 'no music libraries yet', detail)


# --------------------------------------------------------------------------
# Checks: printing

def _run(cmd, timeout=20, stdin=None):
    """A blocking command, with its output. Returns (rc, text) and turns
    a missing binary into rc 127."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              input=stdin, text=True)
    except FileNotFoundError:
        return 127, f'{cmd[0]} is not installed'
    except subprocess.TimeoutExpired:
        return 124, f'{cmd[0]} did not finish within {timeout}s'
    except OSError as e:
        return 1, str(e)
    return proc.returncode, ((proc.stdout or '') + (proc.stderr or '')).strip()


def cups_queues(server=''):
    """Queue names `lp` can print to, from lpstat. Raises OSError-free:
    an unreachable server comes back as an empty list, and check_cups
    turns that into words."""
    cmd = ['lpstat']
    if (server or '').strip():
        cmd += ['-h', server.strip()]
    rc, out = _run(cmd + ['-a'])
    if rc != 0:
        return []
    # "queuename accepting requests since ..." - the name is the first
    # token, and that shape is stable across cups versions.
    return [line.split()[0] for line in out.splitlines() if line.split()]


def check_printer(backend, queue='', server=''):
    """Whether /print can deliver paper the way it is configured."""
    backend = (backend or 'c64').strip().lower()
    if backend == 'c64':
        return Result(True, 'printing to the C64\'s own printer',
                      'Nothing to set up on this machine. The C64 prints '
                      'the document itself on IEC device 4 - a real MPS-80x, '
                      'the Ultimate\'s virtual printer (off by default: turn '
                      'it on in the Ultimate\'s F5 menu), or VICE\'s device-4 '
                      'emulation.')
    if shutil.which('lp') is None:
        hint = ('lp is not on PATH. On Debian and Ubuntu that is '
                '`sudo apt install cups-client`.')
        if sys.platform == 'win32':
            hint = ('CUPS printing needs the lp command, which Windows does '
                    'not have. Use backend "c64", or run the proxy on a '
                    'Linux or macOS host.')
        return Result(False, 'lp is not installed', hint)
    if not (queue or '').strip():
        found = cups_queues(server)
        return Result(False, 'no queue name set',
                      ('Queues this host can see: ' + ', '.join(found))
                      if found else
                      'No queues found either. Set one up with lpadmin, or '
                      'point CUPS server at the machine the printer is on.')
    found = cups_queues(server)
    if not found:
        where = server.strip() or 'this machine'
        return Result(False, f'no queues found on {where}',
                      'lpstat listed nothing. For a remote CUPS host check '
                      'the name resolves and that cupsd is sharing its '
                      'printers (`sudo cupsctl --share-printers`).')
    if queue.strip() not in found:
        return Result(False, f'no queue called {queue.strip()!r}',
                      'Queues that host does have: ' + ', '.join(found))
    return Result(True, f'queue {queue.strip()!r} is accepting jobs',
                  'CUPS will take the job. Whether a page comes out is up '
                  'to the printer - use Send a test page to find out.')


def send_test_page(queue, server='', options=''):
    """Spool one page through the real /print CUPS path."""
    import asyncio
    from . import printcups
    doc = TEST_PAGE.format(when=time.strftime('%Y-%m-%d %H:%M:%S'))
    try:
        res = asyncio.run(printcups.send(
            doc, queue.strip(), (server or '').strip(),
            options or printcups.OPTIONS, title='llm64 setup'))
    except Exception as e:
        return Result(False, 'the test page could not be sent',
                      f'{type(e).__name__}: {e}')
    if res.ok:
        return Result(True, 'CUPS accepted the test page',
                      'A page should appear. If it does not, the job is '
                      'spooled into a printer that is asleep, empty or '
                      'unplugged - `lpstat -o` on the print host shows it '
                      'waiting.')
    return Result(False, res.reason, res.detail)


# --------------------------------------------------------------------------
# Checks: Claude Code

def check_claude(command, workdir=''):
    """Whether /code has a CLI to drive."""
    command = (command or 'claude').strip()
    exe = shutil.which(command) or (command if Path(command).exists() else '')
    if not exe:
        return Result(False, f'{command!r} is not on PATH',
                      'Install Claude Code and put the absolute path to '
                      'the claude binary in this field. /code is the only '
                      'thing that needs it.')
    rc, out = _run([exe, '--version'], timeout=30)
    if rc != 0:
        return Result(False, f'{exe} did not run',
                      out or f'exit {rc}')
    if workdir and not Path(os.path.expanduser(workdir)).is_dir():
        return Result(False, f'{workdir} is not a directory',
                      f'The CLI works ({out.strip()}), but the project '
                      f'directory does not exist.')
    return Result(True, out.strip() or 'the CLI runs',
                  f'Found at {exe}. Note the agent runs with your own '
                  f'permissions on this machine.')


# --------------------------------------------------------------------------
# The steps
#
# Fields use the launcher's schema types - str | secret | int | float |
# bool:<default> | choice:a,b,c | pick:<source> - so a field means the
# same thing in both windows and moving one between them is a cut and a
# paste.

# What each image backend needs before it can draw anything. The
# wizard shows only the block belonging to the backend that is
# selected - the Settings tab is where the rest of each table lives,
# and the difference between the two windows is exactly this: getting
# it working, then tuning it.
IMAGE_BACKEND_FIELDS = {
    'gemini': (
        ('images.gemini', 'model', 'pick:gemini', 'Model',
         'gemini-2.5-flash-image is the current one. The button needs '
         'the key below to be set first.'),
        ('images.gemini', 'key', 'secret', 'API key',
         'From https://aistudio.google.com/apikey. The GEMINI_API_KEY '
         'environment variable is the better place for it and wins over '
         'this field.'),
    ),
    'openai': (
        ('images.openai', 'base_url', 'str', 'Base URL',
         'Anything serving POST /v1/images/generations - OpenAI, '
         'Together, LocalAI.'),
        ('images.openai', 'model', 'pick:openai_images', 'Model',
         'e.g. dall-e-3. The button lists what the endpoint serves.'),
        ('images.openai', 'size', 'str', 'Size',
         'A landscape size suits the 320x200 frame; 1024x1024 is the '
         'one every endpoint accepts.'),
        ('images.openai', 'key', 'secret', 'API key',
         'Or the LLM64_IMAGES_KEY environment variable.'),
    ),
    'comfyui': (
        ('images.comfyui', 'url', 'str', 'URL',
         'e.g. http://127.0.0.1:8188. ComfyUI has no authentication of '
         'any kind, so keep it on localhost or a network you trust.'),
        ('images.comfyui', 'workflow', 'pick:workflows', 'Workflow JSON',
         'Empty uses the bundled Flux workflow, which is a complete '
         'working setup. Your own must be an API-format export with '
         '{PROMPT} in the positive prompt box.'),
        ('images.comfyui', 'model', 'pick:comfy_model', 'Checkpoint',
         'The filename as ComfyUI sees it. The button asks the running '
         'instance. Empty = whatever the workflow already names.'),
        ('images.comfyui', 'timeout', 'int', 'Timeout (seconds)',
         'Raise it on a slow GPU - a cold model load counts against it.'),
    ),
    'fixture': (
        ('images.fixture', 'path', 'str', 'Image file',
         'Returned verbatim for every request. This is the backend for '
         'trying the picture path end to end without spending anything.'),
    ),
}


@dataclass
class Step:
    key: str
    title: str
    required: bool
    intro: str
    fields: tuple = ()
    outro: str = ''
    # A step whose work happens somewhere else entirely (building a
    # music library, wiring up a printer) says so here, and the wizard
    # tells the operator to come back rather than pretending the window
    # can finish it.
    revisit: str = ''


# Paragraphs, not lines: the page wraps this to whatever width the
# window has, so a newline here is a break the reader gets whether it
# suits the window or not. Only the blank lines between paragraphs are
# deliberate.
WELCOME = (
    'This wizard walks you through the setup of both the required and '
    'optional modules for LLM64.\n\n'

    'The required modules: an OpenAI-Compatible LLM (Large Language '
    'Model) endpoint and the address the C64/Win 3.11 client dials '
    '(TCP) to reach this proxy.\n\n'

    'Optional modules include image generation, SID/MIDI music '
    'streaming, real printer support, Claude Code integration, and '
    'various tweaks.\n\n'

    'You can re-run this Setup Wizard at any time. Changes are made to '
    'your config.toml alongside the llm64_proxy.')


STEPS = (
    Step('welcome', 'Welcome', True, WELCOME),

    Step('config', 'Config file', True,
         'Your configuration is saved into a config.toml file. Initially, '
         'you should create one using the config.toml.example or the '
         '"Create from Template" button below.'),

    Step('llm', 'The LLM', True,
         'The proxy needs to connect to an OpenAI-compatible LLM '
         'endpoint, either a local one on your network (run with '
         'llama.cpp, LM Studio, ollama, or similar) or OpenRouter, '
         'ChatGPT, etc.\n\n'
         'The base URL usually ends in /v1 and the API key doesn\'t '
         'matter. For a hosted server, set API key here (and then don\'t '
         'share your config.toml).',
         (('api', 'base_url', 'str', 'Base URL',
           'e.g. http://localhost:8080/v1 for a local llama.cpp, or '
           'https://api.openai.com/v1'),
          ('api', 'key', 'secret', 'API key',
           'Blank is fine for a local server. The OPENAI_API_KEY '
           'environment variable takes precedence over this field.'),
          ('api', 'model', 'pick:llm', 'Model',
           'The button asks the endpoint what it serves. A local server '
           'that loads on demand may only list one.'),
          ('api', 'max_tokens', 'int', 'Max reply tokens',
           'Kept small on purpose: a C64 renders about 8 characters a '
           'frame, so a 4000-token essay takes minutes to arrive. '
           '2000 is the default.'),
          ('api', 'read_timeout', 'int', 'Read timeout (seconds)',
           'How long the endpoint may sit silent mid-reply before the '
           'proxy gives up. Raise it for a big local model that loads '
           'slowly - the C64 is kept fed for the whole wait.')),
         outro='Test sends one real completion. That is the only thing '
               'that proves the URL, the key and the model name are all '
               'right at once, and on a local server it also forces the '
               'model to load, which can take a minute the first time.'),

    Step('network', 'What the C64 dials', True,
         'The proxy listens on a TCP port and the client dials it - '
         'through a WiFi modem on a real C64, or straight from VICE.\n\n'
         'Leave the bind address at 0.0.0.0 unless you know you want '
         'otherwise. 127.0.0.1 accepts connections from this machine '
         'only, which locks out a real C64.',
         (('server', 'host', 'str', 'Bind address',
           '0.0.0.0 listens on every interface. This key is the '
           'launcher\'s own - the command-line server takes --host '
           'instead.'),
          ('server', 'port', 'int', 'TCP port',
           'Default 6400. Any free port works as long as the client is '
           'told the same number.')),
         outro='The check confirms nothing else holds the port, and lists '
               'the addresses this machine answers on.'),

    Step('storage', 'Files and names', False,
         'The data directory stores conversations, images, previews and '
         'the logs. You can leave the default values (empty), and set '
         'your name if you wish.',
         (('storage', 'data_dir', 'str', 'Data directory',
           'Conversations, generated images and proxy.log land here. '
           'Relative paths are read as next to config.toml.'),
          ('modes', 'user_name', 'str', 'Your name',
           'How the model addresses you in chat and adventure modes. '
           'Default "You".'),
          ('modes', 'cards_dir', 'str', 'Character cards',
           'Your own roleplay cards. A few ship built in, so this can '
           'stay empty until you write one.'))),

    Step('images', 'Pictures', False,
         'The model can illustrate a scene, and the proxy converts the '
         'result to something each client can draw - 16 colors at '
         '160x200 for the C64, 256 at 320x200 for the Windows client.\n\n'
         'Pick a backend and give it what it needs. Gemini and the '
         'OpenAI-style backends are hosted and cost money per picture. '
         'ComfyUI runs on your own GPU and has some decent free models '
         'that run on consumer GPUs.\n\n'
         'Mode "\'ask\'" means that you\'ll need to type "/pic" to '
         'generate the image. "\'auto\'" means the proxy will '
         'automatically generate a new image for you as soon as you '
         'enter a new room/area.\n\n'
         'Depending on your model, workflow, LoRAs, etc., you may need '
         'to tweak the settings below. Generally, frontier models (Nano '
         'Banana, etc.) don\'t require so much hand-holding and can '
         'generate good images without tweaks.',
         (('images', 'mode', 'choice:ask,auto,off', 'When to illustrate',
           'ask = the model suggests, /pic confirms. auto = striking '
           'scenes illustrate themselves. off = never.'),
          ('images', 'backend', 'choice:gemini,openai,comfyui,fixture',
           'Backend',
           'fixture is for testing - it returns the same file every '
           'time and costs nothing.'),
          ('images', 'style', 'pick:styles', 'Style preset',
           'A named look, switched with one key. Empty = the built-in '
           'dark-fantasy style. The button lists the built-ins and any '
           'you have written into config.toml yourself.')),
         outro='The check only asks whether the backend could run. Test '
               'picture generates a real one and shows you what each '
               'client would display - which is the only way to judge a '
               'style, and the launcher\'s Illustrations tab is the same '
               'thing with more room.'),

    Step('music', 'Music', False,
         'Both clients can play music while you talk, chosen to match '
         'the mood of the scene: SID tunes on the C64, General MIDI on '
         'the Windows client.\n\n'
         'Neither library ships with the proxy for copyright reasons, '
         'and neither one can be built from this window - they are '
         'large downloads that need some processing, but scripts are '
         'provided. '
         'The commands are below, but also check the README.md; the proxy '
         'works fine in the meantime and simply reports that music is '
         'unavailable.',
         revisit='Run the commands, then reopen this wizard (or press '
                 'Recheck) - the libraries are found by being on disk, so '
                 'nothing here needs saving or restarting once they are.'),

    Step('printing', 'Printing', False,
         '/print intelligently composes a document - a scene, a '
         'character sheet, the session so far - and delivers it, either '
         'to a buffer or to a real printer.\n\n'
         'The Windows client keeps '
         'every /print in its Notebook window, which is virtual paper you '
         'can scroll and copy, and that works with the default '
         'backend and no setup at all. The C64 backend prints on IEC '
         'device 4 - a real MPS-80x, the Ultimate\'s virtual printer, or '
         'VICE\'s emulated one.\n\n'
         'Choose cups only if you want paper out of a modern printer: it '
         'needs the lp command on this host and a CUPS queue, which may '
         'be on this machine or on a Raspberry Pi sitting next to the '
         'printer.',
         (('printer', 'backend', 'choice:c64,cups,both', 'Where paper '
           'comes out',
           'c64 = the C64 prints it. cups = this host spools it to a '
           'queue. both = one document, delivered twice.'),
          ('printer', 'cups_queue', 'pick:cups', 'CUPS queue',
           'The queue name, as lpstat calls it. The button lists what '
           'this host can see.'),
          ('printer', 'cups_server', 'str', 'CUPS server',
           'Empty = the cupsd on this machine. Otherwise host[:port] - '
           'prefer an mDNS name like printpi.local:631 over an IP.'),
          ('printer', 'width', 'int', 'Document width (columns)',
           'The printer\'s width, not the screen\'s. An MPS-803 is 78.')),
         outro='Send a test page spools one real page through the same '
               'path /print uses.'),

    Step('claude', 'Claude Code', False,
         'The /code mode turns the C64 into a terminal for a coding agent '
         'running on this machine. It needs Claude Code installed and '
         'already logged in here; the proxy only starts it.\n\n'
         'Worth knowing before you switch it on: the agent runs as you, '
         'with your permissions, in the directory below. Skip this step '
         'if that is not what you want - nothing else depends on it.',
         (('claude', 'command', 'str', 'claude command',
           'An absolute path is safest - a launcher started from a '
           'desktop icon often has a shorter PATH than your shell.'),
          ('claude', 'workdir', 'str', 'Project directory',
           'Where the agent works by default.'),
          ('claude', 'model', 'str', 'Model',
           'opus / sonnet / haiku or a full id. Empty = the CLI\'s own '
           'default. This is separate from the chat model above.'))),

    Step('finish', 'Done', True,
         'Where each piece stands. Anything still marked with a ! is '
         'something the proxy will run without - only the two mandatory '
         'steps have to be green.\n\n'
         'Saving writes config.toml; the proxy reads it at start, so a '
         'running server needs the restart below to see the changes.'),
)


STEPS_BY_KEY = {s.key: s for s in STEPS}


def music_commands(base_url='http://localhost:8080/v1'):
    """The two library builds, as commands to copy.

    Written against the repo layout, run from the repo root, because
    that is where the tools are: they are development scripts and a
    packaged binary does not carry them. Both pipelines write into
    llm64_proxy/data/ themselves, which is why nothing here takes a
    data directory - point data_dir somewhere else and the last step of
    each build is to move or rsync the result across (README).
    """
    return (
        ('SID tunes, for the C64',
         'Get HVSC (85 MB) from https://www.hvsc.c64.org/downloads, then '
         'run the whole pipeline from the repo root:\n\n'
         '  llm64_proxy/tools/sid_build.py --hvsc '
         '~/Downloads/HVSC_85-all-of-them.7z\n\n'
         'Eight stages, resumable, about five hours - most of it emulating '
         'every tune to measure loudness and asking the LLM to tag each one '
         'for mood. --dry-run shows what is already done, --no-loudness '
         'drops the longest stage, and --info prints the license position. '
         'If the proxy lives on another machine, finish with --deploy '
         'user@host:/path/to/llm64_proxy, which copies the 50 MB the proxy '
         'reads and not the 457 MB HVSC tree.'),
        ('MIDI, for the Windows 3.11 client',
         'About 10k files from VGMusic, ~300 MB, from the repo root:\n\n'
         '  llm64_proxy/tools/midi_fetch.py\n'
         '  llm64_proxy/tools/midi_scan.py\n'
         '  llm64_proxy/tools/midi_mood.py '
         'llm64_proxy/data/midi/scan.json \\\n'
         f'      --base-url {base_url} --workers 2 \\\n'
         '      -o llm64_proxy/data/midi/tags.json\n'
         '  llm64_proxy/tools/midi_makedb.py '
         'llm64_proxy/data/midi/scan.json \\\n'
         '      llm64_proxy/data/midi/tags.json \\\n'
         '      -o llm64_proxy/data/midi/midi.json\n\n'
         'Fetch, scan, tag with the LLM, assemble. Two tagging workers and '
         'no more - three time out against one llama.cpp. --pilot 48 tags a '
         'small batch first so you can read the results before committing '
         'to the run.'),
    )
