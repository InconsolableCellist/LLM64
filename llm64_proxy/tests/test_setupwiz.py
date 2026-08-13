#!/usr/bin/env python3
"""setupwiz: the setup wizard's steps, its checks and its file handling.

Nothing real is contacted or spawned. The LLM probe runs against a
scripted http.server on a random localhost port, `lp` and `lpstat` are
stubbed on PATH the way test_printcups does it, and every config lands
in a temporary directory.

What is worth asserting here, in rough order of how badly it would hurt
to get wrong:

- The wizard writes config.toml. A step that silently reorders the file
  or eats the comments around a key it touched is worse than no wizard,
  so the round trips are checked against the real template.
- should_autorun decides whether a window opens over somebody's working
  install unasked. It has to say no for every config that is already
  set up.
- A check that raises instead of returning a Result takes the button
  with it, and every check message is shown to the operator, so none of
  them may carry an API key.

Run: .venv/bin/python tests/test_setupwiz.py
"""

import http.server
import json
import os
import shutil
import socket
import socketserver
import stat
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import setupwiz
from src.setupwiz import ConfigDoc, Result

failures = []
REAL_PATH = os.environ.get('PATH', '')
TMP = Path(tempfile.mkdtemp(prefix='setupwiz-'))


def check(name, cond, detail=''):
    if not cond:
        failures.append(f'{name} {detail}')


def check_eq(name, got, want):
    if got != want:
        failures.append(f'{name}:\n  got  {got!r}\n  want {want!r}')


def check_in(name, needle, hay):
    if needle not in hay:
        failures.append(f'{name}: {needle!r} missing from {hay!r:.300}')


# --- the step table -----------------------------------------------------
#
# Cheap, and it catches the two mistakes that are easy to make while
# editing prose: a field type the renderer has no branch for, and a
# pick: source no button knows how to fetch.

FIELD_TYPES = ('str', 'secret', 'int', 'float')
PICK_SOURCES = ('llm', 'styles', 'workflows', 'gemini', 'openai_images',
                'comfy_model', 'comfy_clip', 'comfy_vae', 'comfy_lora',
                'cups')

keys = [s.key for s in setupwiz.STEPS]
check_eq('the wizard starts at welcome and ends at finish',
         (keys[0], keys[-1]), ('welcome', 'finish'))
check_eq('step keys are unique', len(set(keys)), len(keys))
check('the mandatory steps are the two the README calls mandatory',
      {s.key for s in setupwiz.STEPS if s.required} ==
      {'welcome', 'config', 'llm', 'network', 'finish'},
      str({s.key for s in setupwiz.STEPS if s.required}))

all_fields = [f for s in setupwiz.STEPS for f in s.fields]
for table in setupwiz.IMAGE_BACKEND_FIELDS.values():
    all_fields.extend(table)
for spec in all_fields:
    check_eq(f'field {spec[:2]} has five parts', len(spec), 5)
    section, key, ftype, label, _help = spec
    ok = (ftype in FIELD_TYPES or ftype.startswith('bool:')
          or ftype.startswith('choice:')
          or (ftype.startswith('pick:') and ftype.split(':')[1]
              in PICK_SOURCES))
    check(f'field {section}.{key} has a renderable type', ok, ftype)
    check(f'field {section}.{key} has a label', bool(label))

# Every backend the images step offers has a settings block, or picking
# it in the wizard shows an empty page.
backend_field = [f for f in setupwiz.STEPS_BY_KEY['images'].fields
                 if f[1] == 'backend'][0]
offered = set(backend_field[2].split(':')[1].split(','))
check_eq('every image backend on offer has settings',
         offered - set(setupwiz.IMAGE_BACKEND_FIELDS), set())

# The music step cannot do its own work, and says so out loud rather
# than leaving the operator waiting for a button.
check('the music step tells you to come back',
      'reopen' in setupwiz.STEPS_BY_KEY['music'].revisit.lower())
check('the printing step mentions the Notebook',
      'Notebook' in setupwiz.STEPS_BY_KEY['printing'].intro)


# --- ConfigDoc ----------------------------------------------------------

template = setupwiz.template_path()
check('config.toml.example is findable', template is not None)

home = TMP / 'plain'
home.mkdir()
cfg = ConfigDoc(home / 'config.toml')
check('a missing config is not an error', cfg.error is None)
check('and it knows it is missing', not cfg.exists())
check_eq('creating it from the template works',
         cfg.create_from_template(), None)
check('and then it exists', cfg.exists())
check('creating it twice refuses',
      cfg.create_from_template() is not None)

check_eq('a value comes back', str(cfg.get('api', 'model')),
         'gpt-3.5-turbo')
check_eq('a missing key comes back as the default',
         cfg.get('api', 'nope', 'fallback'), 'fallback')
check_eq('a missing section comes back as the default',
         cfg.get('nosuch.section', 'k', 'fallback'), 'fallback')

cfg.set('api', 'model', 'qwen3-30b')
cfg.set('api', 'base_url', 'http://localhost:8080/v1')
cfg.set('server', 'port', 6400)          # a section the template lacks
check_eq('saving works', cfg.save(), None)

reread = ConfigDoc(home / 'config.toml')
check_eq('the value survives the round trip',
         str(reread.get('api', 'model')), 'qwen3-30b')
check_eq('a new section survives too', int(reread.get('server', 'port')),
         6400)
# The whole reason the launcher uses tomlkit rather than toml: this file
# is mostly explanation, and an edit that strips it makes it useless.
text = (home / 'config.toml').read_text()
check_in('comments around a rewritten key survive', '# Model to use', text)
check_in('comments elsewhere survive', '# Directory for conversation', text)
check_in('the commented-out examples survive', '# key = "sk-..."', text)

# Blank means "unset it and let the server default apply", the same rule
# the Settings tab follows.
reread.set('api', 'model', '')
check_eq('blanking a key removes it', reread.get('api', 'model'), None)
check_in('and does not disturb its neighbours', 'temperature',
         reread.text())

# text_with is what a check reads while the operator is still typing. It
# must not touch the document, or moving between steps would rewrite the
# file every time a check ran.
doc = ConfigDoc(home / 'config.toml')
before = doc.text()
pending = doc.text_with({('api', 'model'): 'not-saved',
                         ('images', 'backend'): 'fixture'})
check_eq('text_with leaves the document alone', doc.text(), before)
check_in('text_with folds the edit in', 'not-saved', pending)
check_in('text_with can add a key', 'fixture', pending)
check('text_with did not write the file',
      'not-saved' not in (home / 'config.toml').read_text())


# --- Rollback (what Cancel undoes) --------------------------------------
#
# The wizard writes each step as you leave it, so Cancel has real writes
# to take back rather than merely unsaved widgets. What matters: it
# restores a file that existed, removes one the wizard created, and
# knows the difference between "nothing happened" and "something did".

roll_dir = TMP / 'rollback'
roll_dir.mkdir()
existing = roll_dir / 'config.toml'
existing.write_text('[api]\nmodel = "before"\n# a comment\n')
created = roll_dir / 'made-by-the-wizard.toml'

rb = setupwiz.Rollback()
rb.note(existing)
rb.note(created)
check_eq('nothing written yet, nothing to undo', rb.changed(), [])

rb.note(existing)                      # a second sighting must not
existing.write_text('[api]\nmodel = "after"\n')   # re-baseline
created.write_text('[api]\nmodel = "new"\n')
check_eq('both files are seen as changed', len(rb.changed()), 2)

check_eq('the rollback reports no problems', rb.restore(), [])
check_eq('a file that existed goes back verbatim', existing.read_text(),
         '[api]\nmodel = "before"\n# a comment\n')
check('a file the wizard created is removed', not created.exists())
check_eq('and afterwards there is nothing left to undo', rb.changed(), [])

# Restoring twice is not an error, and neither is canceling a run that
# only ever read - both are ordinary ways to press the button.
check_eq('a second restore is a no-op', rb.restore(), [])

rb2 = setupwiz.Rollback()
rb2.note(TMP / 'never' / 'nor' / 'here.toml')
check_eq('a path that never existed is not a change', rb2.changed(), [])
check_eq('and restoring it does nothing', rb2.restore(), [])


# --- check_config -------------------------------------------------------

check_eq('a config file that is not there fails',
         setupwiz.check_config(TMP / 'never' / 'config.toml').ok, False)
check_eq('the template loads',
         setupwiz.check_config(home / 'config.toml').ok, True)
bad = TMP / 'badconf'
bad.mkdir()
(bad / 'config.toml').write_text('[api\nbase_url = ')
res = setupwiz.check_config(bad / 'config.toml')
check_eq('a config that will not parse fails', res.ok, False)
check_in('and the complaint mentions TOML', 'TOML', res.detail)
check('the validation scratch file never survives',
      not (bad / '.launcher-validate.tmp.toml').exists())


# --- should_autorun -----------------------------------------------------

def autorun(name, path, want):
    got, why = setupwiz.should_autorun(path)
    check_eq(f'autorun: {name}', got, want)
    if got:
        check(f'autorun: {name} says why', bool(why))


def fresh(name, body):
    """A config directory holding exactly `body`."""
    d = TMP / name
    d.mkdir()
    (d / 'config.toml').write_text(body)
    return d / 'config.toml'


autorun('no file at all', TMP / 'nothing' / 'config.toml', True)
autorun('the untouched template', fresh('tmpl', template.read_text()), True)
autorun('no [api] section', fresh('empty', '[storage]\ndata_dir = "./x"\n'),
        True)
autorun('the template plus a key',
        fresh('keyed', '[api]\nbase_url = "https://api.openai.com/v1"\n'
                       'model = "gpt-3.5-turbo"\nkey = "sk-real"\n'), False)
autorun('a local endpoint',
        fresh('local', '[api]\nbase_url = "http://localhost:8080/v1"\n'
                       'model = "qwen3"\n'), False)
autorun('a config that will not parse',
        fresh('broken', '[api\nbase_url = '), False)

marked = fresh('marked', template.read_text())
done = ConfigDoc(marked)
done.mark_completed()
done.save()
autorun('a config the wizard has finished', marked, False)
check_in('the marker is visible in the file', '[wizard]', marked.read_text())
check_in('and dated', 'when', marked.read_text())

# OPENAI_API_KEY in the environment is a configured install even when
# the file says nothing, because that is where the README tells you to
# put it.
os.environ['OPENAI_API_KEY'] = 'sk-from-the-environment'
autorun('the template plus OPENAI_API_KEY', fresh('envkey',
        template.read_text()), False)
del os.environ['OPENAI_API_KEY']


# --- the LLM probe ------------------------------------------------------

class Stub(http.server.BaseHTTPRequestHandler):
    mode = 'ok'
    seen = []
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        Stub.seen.append((self.path, dict(self.headers), body))
        if Stub.mode in ('404', '401', '500'):
            self.send_error(int(Stub.mode))
            return
        if Stub.mode == 'notchat':
            payload = {'object': 'list', 'data': []}
        elif Stub.mode == 'empty':
            payload = {'choices': [{'message': {'content': ''}}]}
        else:
            payload = {'choices': [{'message': {
                'role': 'assistant', 'content': 'ready'}}]}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(socketserver.TCPServer):
    allow_reuse_address = True


httpd = Server(('127.0.0.1', 0), Stub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{httpd.server_address[1]}/v1'

Stub.mode = 'ok'
res = setupwiz.check_llm(BASE, 'sk-secret-key', 'qwen3-30b', timeout=10)
check_eq('a working endpoint passes', res.ok, True)
check_in('and quotes what the model said', 'ready', res.detail)

path, headers, body = Stub.seen[-1]
check_eq('the probe posts to the chat endpoint', path, '/v1/chat/completions')
check_eq('with the model asked for', body['model'], 'qwen3-30b')
check('with a small token budget', body['max_tokens'] <= 64)
check_eq('and no streaming', body.get('stream'), False)
check_eq('the key travels as a bearer header',
         headers.get('Authorization'), 'Bearer sk-secret-key')
# Everything a Result carries is put on screen, so a key in one would be
# a key on somebody's screenshot.
for label, r in (('ok', res),
                 ('404', setupwiz.check_llm(BASE.replace('/v1', '/nope'),
                                            'sk-secret-key', 'm',
                                            timeout=10)),):
    check(f'no key leaks into the {label} result',
          'sk-secret-key' not in (r.summary + r.detail))

Stub.mode = '404'
res = setupwiz.check_llm(BASE, '', 'm', timeout=10)
check_eq('a 404 fails', res.ok, False)
check_in('and blames the URL', '/v1', res.detail)

Stub.mode = '401'
res = setupwiz.check_llm(BASE, 'bad', 'm', timeout=10)
check_eq('a 401 fails', res.ok, False)
check_in('and blames the key', 'key', res.detail)

Stub.mode = 'notchat'
res = setupwiz.check_llm(BASE, '', 'm', timeout=10)
check_eq('a non-chat reply fails', res.ok, False)
check_in('and says so', 'chat completion', res.detail)

# An endpoint that answers is configured correctly even when a thinking
# model spends the probe's budget before saying anything - failing that
# would send people to change settings that are already right.
Stub.mode = 'empty'
res = setupwiz.check_llm(BASE, '', 'm', timeout=10)
check_eq('an empty reply still counts as reachable', res.ok, True)
check_in('with an explanation', 'thinking', res.detail)

res = setupwiz.check_llm('', '', 'm')
check_eq('no base URL fails before any request', res.ok, False)
res = setupwiz.check_llm(BASE, '', '')
check_eq('no model fails before any request', res.ok, False)
res = setupwiz.check_llm('http://127.0.0.1:1/v1', '', 'm', timeout=3)
check_eq('an unreachable endpoint fails', res.ok, False)
check_in('naming the endpoint', '127.0.0.1:1', res.summary)

httpd.shutdown()


# --- the port check -----------------------------------------------------

probe = socket.socket()
probe.bind(('127.0.0.1', 0))
free_port = probe.getsockname()[1]
probe.close()

res = setupwiz.check_port('127.0.0.1', free_port)
check_eq('a free port passes', res.ok, True)
check_in('and warns that loopback locks the C64 out', 'loopback', res.detail)

held = socket.socket()
held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
held.bind(('127.0.0.1', free_port))
held.listen(1)
res = setupwiz.check_port('127.0.0.1', free_port)
check_eq('a port in use fails', res.ok, False)
res = setupwiz.check_port('127.0.0.1', free_port, running=True)
check_eq('unless it is our own server holding it', res.ok, True)
held.close()

check_eq('a non-numeric port fails', setupwiz.check_port('', 'abc').ok, False)
check_eq('port 0 fails', setupwiz.check_port('', 0).ok, False)
check_eq('port 70000 fails', setupwiz.check_port('', 70000).ok, False)
check('the addresses are IPv4 strings',
      all(a.count('.') == 3 for a in setupwiz.local_addresses()),
      str(setupwiz.local_addresses()))


# --- images -------------------------------------------------------------

imgdir = TMP / 'images'
imgdir.mkdir()
fixture = imgdir / 'picture.png'
fixture.write_bytes(b'\x89PNG\r\n\x1a\n')
conf = imgdir / 'config.toml'
conf.write_text('[storage]\ndata_dir = "./data"\n')


def images_check(body):
    return setupwiz.check_images(
        '[storage]\ndata_dir = "./data"\n' + body, str(conf))


check_eq('no [images] table is not a failure',
         images_check('').ok, None)
check_eq('mode = off is not a failure',
         images_check('[images]\nmode = "off"\n').ok, None)
res = images_check(f'[images]\nbackend = "fixture"\n'
                   f'[images.fixture]\npath = "{fixture}"\n')
check_eq('a usable fixture backend passes', res.ok, True)
res = images_check('[images]\nbackend = "fixture"\n'
                   '[images.fixture]\npath = "/nowhere/gone.png"\n')
check_eq('a fixture that is not there fails', res.ok, False)
check_in('and names the file', 'gone.png', res.detail)
res = images_check('[images]\nbackend = "nonsense"\n')
check_eq('a backend that does not exist fails', res.ok, False)
check('the preview scratch file never survives',
      not (imgdir / '.launcher-preview.tmp.toml').exists())


# --- music --------------------------------------------------------------

empty = TMP / 'nomusic'
empty.mkdir()
lines = setupwiz.music_status_lines(empty)
check_eq('both libraries are reported on', len(lines), 2)
check_in('the SID line says how to build it', 'sid_build.py', lines[0])
check_in('the MIDI line says how to build it', 'midi_makedb.py', lines[1])
res = setupwiz.check_music(empty)
check_eq('no music is neither pass nor fail', res.ok, None)
check_in('and the detail carries both lines', 'MIDI', res.detail)

titles = [t for t, _ in setupwiz.music_commands('http://host:1/v1')]
check_eq('there are two build recipes', len(titles), 2)
check_in('the MIDI recipe takes the endpoint you configured',
         'http://host:1/v1', '\n'.join(b for _, b in
                                       setupwiz.music_commands(
                                           'http://host:1/v1')))


# --- printing -----------------------------------------------------------

def stub_bin(name, body):
    """Put `name` on PATH running `body`. PATH is rebuilt from the real
    one each time, so one stub never shadows the next."""
    d = Path(tempfile.mkdtemp(prefix='setupwiz-bin-'))
    exe = d / name
    exe.write_text('#!/bin/sh\n' + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return d


res = setupwiz.check_printer('c64')
check_eq('the default printer backend needs nothing', res.ok, True)
check_in('and says where paper comes out', 'device 4', res.detail)

os.environ['PATH'] = str(TMP / 'empty-path')
(TMP / 'empty-path').mkdir()
res = setupwiz.check_printer('cups', 'n80')
check_eq('cups without lp fails', res.ok, False)
check_in('and says how to install it', 'cups-client', res.detail)

lp_dir = stub_bin('lp', 'cat >/dev/null\n')
lpstat_dir = stub_bin(
    'lpstat',
    'echo "n80 accepting requests since Thu 01 Jan 1970"\n'
    'echo "laser accepting requests since Thu 01 Jan 1970"\n')
os.environ['PATH'] = os.pathsep.join([str(lp_dir), str(lpstat_dir),
                                      REAL_PATH])

check_eq('the queue list is parsed to bare names',
         setupwiz.cups_queues(), ['n80', 'laser'])
res = setupwiz.check_printer('cups', 'n80')
check_eq('a queue that exists passes', res.ok, True)
res = setupwiz.check_printer('both', 'n80')
check_eq('backend both is checked the same way', res.ok, True)
res = setupwiz.check_printer('cups', 'nosuch')
check_eq('a queue that does not exist fails', res.ok, False)
check_in('and lists the ones that do', 'n80, laser', res.detail)
res = setupwiz.check_printer('cups', '')
check_eq('no queue name fails', res.ok, False)
check_in('and lists what is available', 'n80', res.detail)

res = setupwiz.send_test_page('n80')
check_eq('a test page that lp accepts passes', res.ok, True)

os.environ['PATH'] = os.pathsep.join(
    [str(stub_bin('lp', 'echo "lp: Error - The printer or class does not '
                        'exist." >&2\nexit 1\n')), str(lpstat_dir),
     REAL_PATH])
res = setupwiz.send_test_page('nosuch')
check_eq('a test page lp rejects fails', res.ok, False)
check_in('with lp\'s own words kept', 'does not exist', res.detail)

# lpstat exiting non-zero (no cupsd at all) must be an empty list, not an
# exception - the wizard turns it into words, the check must not raise.
os.environ['PATH'] = os.pathsep.join(
    [str(stub_bin('lpstat', 'exit 1\n')), str(lp_dir), REAL_PATH])
check_eq('an lpstat that fails lists nothing', setupwiz.cups_queues(), [])
res = setupwiz.check_printer('cups', 'n80')
check_eq('and the check fails rather than raising', res.ok, False)
os.environ['PATH'] = REAL_PATH


# --- Claude Code --------------------------------------------------------

os.environ['PATH'] = os.pathsep.join(
    [str(stub_bin('claude', 'echo "1.2.3 (Claude Code)"\n')), REAL_PATH])
res = setupwiz.check_claude('claude', str(TMP))
check_eq('a claude CLI that runs passes', res.ok, True)
check_in('reporting its version', '1.2.3', res.summary)
res = setupwiz.check_claude('claude', str(TMP / 'no-such-project'))
check_eq('a project directory that is not there fails', res.ok, False)

os.environ['PATH'] = os.pathsep.join(
    [str(stub_bin('claude', 'exit 3\n')), REAL_PATH])
check_eq('a claude CLI that errors fails',
         setupwiz.check_claude('claude').ok, False)
os.environ['PATH'] = REAL_PATH
res = setupwiz.check_claude('definitely-not-installed-anywhere')
check_eq('a claude CLI that is not installed fails', res.ok, False)
check_in('and says what it is for', '/code', res.detail)


# --- every check returns a Result ---------------------------------------
#
# The wizard disables its buttons while a check runs and re-enables them
# when the answer arrives. A check that raises instead never answers.

for name, call in (
        ('check_llm', lambda: setupwiz.check_llm('http://127.0.0.1:1/v1',
                                                 '', 'm', timeout=2)),
        ('check_port', lambda: setupwiz.check_port('999.999.999.999', 22)),
        ('check_images', lambda: setupwiz.check_images('[images', str(conf))),
        ('check_music', lambda: setupwiz.check_music('/nowhere/at/all')),
        ('check_printer', lambda: setupwiz.check_printer('cups', 'q', 'x:1')),
        ('check_claude', lambda: setupwiz.check_claude('')),
        ('send_test_page', lambda: setupwiz.send_test_page('q', 'x:1'))):
    try:
        out = call()
        check(f'{name} returns a Result', isinstance(out, Result), repr(out))
        check(f'{name} explains itself', bool(out.summary))
    except Exception as e:
        failures.append(f'{name} raised {type(e).__name__}: {e}')


shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f'FAIL ({len(failures)})')
    for f in failures:
        print(' -', f)
    sys.exit(1)
print('test_setupwiz: all checks passed')
