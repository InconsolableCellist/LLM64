#!/usr/bin/env python3
"""Build the whole SID music library, from an HVSC download to a ranked
database the proxy can serve. One command, resumable, with progress.

The library is not shipped: the tunes in HVSC are copyrighted by their
composers (see LICENCE below), so everyone builds their own from their
own copy. That is seven separate tools and several hours of CPU, which is
what this script exists to hide.

    tools/sid_build.py --hvsc ~/Downloads/HVSC_85-all-of-them.7z

What it runs, in order (each stage is skipped when its output already
exists, so an interrupted build resumes where it stopped):

  unpack       HVSC .7z -> data/sids/C64Music/            ~1 min, 457 MB
  sidreloc     fetch + build Linus Akesson's relocator     ~10 s
  scan         which tunes could fit the client's window   ~2 min
  relocate     move each one to $B000 and verify it        ~1 h
  songlengths  per-subtune durations from HVSC             ~5 s
  loudness     emulate each tune, measure RMS + $D418      ~3 h
  moods        an LLM tags each tune for the narrator      ~1-2 h
  database     assemble moods.json                         ~10 s
  ranking      cross-reference the scene's own opinion     ~2 min

Everything lands in data/sids/, which is what the proxy reads. On a
different machine to the proxy, finish with:

    tools/sid_build.py --deploy user@proxyhost:/path/to/llm64_proxy

WHERE TO GET HVSC
  https://www.hvsc.c64.org/downloads      official page, all mirrors
  https://hvsc.brona.dk/HVSC/HVSC_85-all-of-them.7z    (85 MB, release 85)
  The "all of them" archive is the one to get; releases after 85 work
  too, though the ranking's path matching degrades a little.

LICENCE, AND WHY THERE IS NO PREBUILT DOWNLOAD
  Every tune in HVSC is copyrighted by its composer or publisher, and
  HVSC's own notice limits use to "private enjoyment" - which is exactly
  what this is: your copy, your machine, your C64. Redistributing the
  built library, or a disk image containing it, is not covered. See
  DOCUMENTS/Disclaimer.txt in the collection.

  sidreloc is (c) 2012 Linus Akesson, MIT licensed, fetched and built
  from source here. The ranking data comes from DeepSID (Jens-Christian
  Huus / Chordian) and CSDb; see tools/sid_rank.py.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROXY = HERE.parent

SIDRELOC_URL = 'https://hd0.linusakesson.net/files/sidreloc-1.0.tgz'
HVSC_PAGE = 'https://www.hvsc.c64.org/downloads'
HVSC_MIRROR = 'https://hvsc.brona.dk/HVSC/HVSC_85-all-of-them.7z'

# The relocator's own default is 20000 (a whole PAL frame). 1600 is what
# the shipped library was built with: the client used to call the play
# routine with interrupts disabled, so anything longer than one byte time
# at 9600 baud cost an ACIA byte. serial.s now drops I around the call
# (see the in_music guard), and a measured sample says 91% of the tunes
# this rejects need only 2200 cycles and 98% need 3000 - raising it to
# 3000 roughly DOUBLES the library for 15% of a frame instead of 8%.
# Left at the hardware-proven value; raise it deliberately.
DEFAULT_PLAY_CYCLES = 1600

# Matches the UPS budget the pipeline was developed against, and is a
# sane default for a laptop that also wants to stay responsive.
DEFAULT_JOBS = 4


# --- terminal chrome -------------------------------------------------

class Out:
    """Progress that behaves on a pipe as well as a terminal."""

    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.width = shutil.get_terminal_size((80, 24)).columns
        self.t0 = None
        self.label = ''
        self.last_line = 0.0

    def rule(self, text=''):
        bar = '-' * max(0, self.width - len(text) - 3)
        print(f'\n== {text} {bar}' if text else '', file=sys.stderr)

    def say(self, text, indent=2):
        print(' ' * indent + text, file=sys.stderr, flush=True)

    def start(self, label):
        self.label, self.t0, self.last_line = label, time.monotonic(), 0.0

    def tick(self, done, total):
        el = time.monotonic() - self.t0
        eta = (el / done * (total - done)) if done else 0
        if self.tty:
            frac = done / total if total else 0
            w = max(10, min(30, self.width - 46))
            fill = int(w * frac)
            print(f'\r  [{"#" * fill}{"." * (w - fill)}] {frac:4.0%} '
                  f'{done:>6}/{total:<6} eta {fmt_time(eta):>7}  {self.label}'
                  [:self.width - 1], end='', file=sys.stderr, flush=True)
        elif time.monotonic() - self.last_line > 30:
            self.last_line = time.monotonic()
            print(f'  {self.label}: {done}/{total}, eta {fmt_time(eta)}',
                  file=sys.stderr, flush=True)

    def done(self, note=''):
        el = fmt_time(time.monotonic() - self.t0) if self.t0 else ''
        if self.tty:
            print('\r' + ' ' * (self.width - 1) + '\r', end='',
                  file=sys.stderr)
        self.say(f'{self.label}: done in {el}{"  " + note if note else ""}')


def fmt_time(s):
    s = int(s)
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m{s % 60:02d}s'
    return f'{s // 3600}h{(s % 3600) // 60:02d}m'


def fmt_size(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1024:
            return f'{n:.0f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


out = Out()
PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')


def run(cmd, label, total=None, cwd=None, quiet=False):
    """Run a pipeline tool, turning its 'N/M' chatter into a progress bar.

    Tool stderr is kept in a ring buffer and only shown when something
    fails - a stage that works should print one line, not ten thousand.
    """
    out.start(label)
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors='replace', bufsize=1)
    tail, stdout = [], []
    for line in proc.stderr:
        tail.append(line.rstrip())
        del tail[:-40]
        m = PROGRESS_RE.search(line)
        if m and total is not False:
            done, tot = int(m.group(1)), int(m.group(2))
            out.tick(done, total or tot)
    stdout = proc.stdout.read()
    rc = proc.wait()
    if rc != 0:
        if out.tty:
            print(file=sys.stderr)
        out.say(f'FAILED: {label} (exit {rc})', indent=0)
        for t in tail[-15:]:
            out.say(t, indent=4)
        sys.exit(1)
    out.done()
    if stdout and not quiet:
        for line in stdout.strip().splitlines()[-4:]:
            out.say(line, indent=4)
    return stdout


def have(prog):
    return shutil.which(prog) is not None


def download(url, dest: Path, label):
    out.start(label)
    req = urllib.request.Request(
        url, headers={'User-Agent': 'llm64-sid-build'})
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        with open(dest, 'wb') as f:
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                got += len(chunk)
                if total:
                    out.tick(got // 1024, total // 1024)
    out.done(f'{fmt_size(dest.stat().st_size)}')


# --- stages ----------------------------------------------------------

def stage_unpack(args, d: Path) -> Path:
    """Return the C64Music root, unpacking the archive if needed."""
    hvsc = args.hvsc
    if hvsc and hvsc.is_dir():
        root = hvsc / 'C64Music' if (hvsc / 'C64Music').is_dir() else hvsc
        out.say(f'using the HVSC tree at {root}')
        return root
    root = d / 'C64Music'
    if root.is_dir() and any(root.glob('MUSICIANS/*')):
        out.say(f'already unpacked: {root}')
        return root
    if not hvsc:
        sys.exit(f'no HVSC tree at {root}. Download the collection and pass '
                 f'it to --hvsc:\n    {HVSC_PAGE}\n    {HVSC_MIRROR}\n'
                 f'  (the .7z, or an already-unpacked C64Music directory)')
    if not have('7z') and not have('7za'):
        sys.exit('need 7z to unpack the HVSC archive (install p7zip), '
                 'or unpack it yourself and pass the directory to --hvsc')
    d.mkdir(parents=True, exist_ok=True)
    run([have('7z') and '7z' or '7za', 'x', '-y', f'-o{d}', str(hvsc)],
        'unpack HVSC', total=False)
    if not root.is_dir():
        # Some archives carry a versioned top directory
        cands = [p for p in d.iterdir()
                 if p.is_dir() and (p / 'MUSICIANS').is_dir()]
        if not cands:
            sys.exit(f'unpacked, but no C64Music/MUSICIANS under {d}')
        cands[0].rename(root)
    return root


def stage_sidreloc(args, d: Path) -> str:
    if args.sidreloc:
        return str(args.sidreloc)
    if have('sidreloc'):
        out.say('sidreloc: found on PATH')
        return 'sidreloc'
    built = d / 'sidreloc-1.0' / 'sidreloc'
    if built.exists():
        out.say(f'sidreloc: already built at {built}')
        return str(built)
    if not (have('cc') or have('gcc')) or not have('make'):
        sys.exit('need a C compiler and make to build sidreloc, or point '
                 '--sidreloc at a binary')
    tgz = d / 'sidreloc-1.0.tgz'
    if not tgz.exists():
        download(SIDRELOC_URL, tgz, 'fetch sidreloc (MIT, Linus Akesson)')
    with tarfile.open(tgz) as t:
        t.extractall(d, filter='data')
    run(['make', '-C', str(d / 'sidreloc-1.0')], 'build sidreloc', total=False)
    if not built.exists():
        sys.exit(f'sidreloc did not build at {built}')
    return str(built)


def count_sids(root: Path) -> int:
    return sum(1 for _ in root.rglob('*.sid'))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hvsc', type=Path,
                    help='HVSC "all of them" .7z, or an unpacked C64Music dir')
    ap.add_argument('--data', type=Path, default=PROXY / 'data' / 'sids',
                    help='where the library is built (default: data/sids)')
    ap.add_argument('--sidreloc', type=Path,
                    help='sidreloc binary (default: build it from source)')
    ap.add_argument('-j', '--jobs', type=int, default=DEFAULT_JOBS,
                    help=f'parallel jobs (default: {DEFAULT_JOBS})')
    ap.add_argument('--play-cycles', type=int, default=DEFAULT_PLAY_CYCLES,
                    help=f'reject tunes whose play routine costs more than '
                         f'this (default: {DEFAULT_PLAY_CYCLES}; 3000 about '
                         f'doubles the library - see the source note)')
    ap.add_argument('--llm-url', default='http://127.0.0.1:8080/v1',
                    help='OpenAI-compatible endpoint for the mood tagger')
    ap.add_argument('--llm-model', default='local',
                    help='model name to ask that endpoint for')
    ap.add_argument('--llm-workers', type=int, default=4,
                    help="concurrent tagging requests (match the server's "
                         'parallel slots)')
    ap.add_argument('--tags', type=Path,
                    help='prebuilt mood tags JSON: skips the LLM stage')
    ap.add_argument('--pilot', type=int, metavar='N',
                    help='tag only N tunes - a cheap end-to-end trial')
    ap.add_argument('--no-loudness', action='store_true',
                    help='skip loudness (hours, needs pyresidfp + py65); '
                         'tunes then play at their own volume')
    ap.add_argument('--no-rank', action='store_true',
                    help='skip the scene-regard ranking')
    ap.add_argument('--csdb-ratings', action='store_true',
                    help='ranking: also fetch CSDb release ratings')
    ap.add_argument('--redo', metavar='STAGE', action='append', default=[],
                    help='force a stage to re-run: scan, relocate, '
                         'songlengths, loudness, moods, database, ranking')
    ap.add_argument('--deploy', metavar='[user@]host:/path/to/llm64_proxy',
                    help='rsync the finished library to a proxy elsewhere '
                         '(only what the proxy reads: ~50 MB, not HVSC)')
    ap.add_argument('--info', action='store_true',
                    help='print the download links and licence notes, then '
                         'exit')
    ap.add_argument('--dry-run', action='store_true',
                    help='show the plan and what is already done')
    args = ap.parse_args()

    if args.info:
        print(__doc__)
        return

    d = args.data
    if args.deploy and not any([args.hvsc, args.dry_run]) \
            and (d / 'moods.json').exists():
        return deploy(args, d)

    d.mkdir(parents=True, exist_ok=True)
    redo = set(args.redo)
    paths = {
        'candidates': d / 'candidates.json',
        'reloc': d / 'reloc.json',
        'survivors': d / 'candidates_ok.json',
        'siddir': d / 'b000_full',
        'songlengths': d / 'songlengths.json',
        'loudness': d / 'loudness.json',
        'd418': d / 'd418_trace.json',
        'tags': args.tags or d / 'tags.json',
        'db': d / 'moods.json',
        'ranking': d / 'ranking.json',
    }

    def pending(stage, target: Path) -> bool:
        if stage in redo:
            return True
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            out.say(f'{stage}: already done ({target.name}) - '
                    f'--redo {stage} to rebuild')
            return False
        return True

    out.rule('llm64 SID library build')
    free = shutil.disk_usage(d).free
    out.say(f'target: {d}   free space: {fmt_size(free)}')
    if free < 700 * 1024 * 1024:
        out.say('WARNING: HVSC unpacks to ~460 MB and the library adds ~50 MB')
    if args.play_cycles != DEFAULT_PLAY_CYCLES:
        out.say(f'play-cycles: {args.play_cycles} '
                f'(default {DEFAULT_PLAY_CYCLES})')

    out.rule('HVSC')
    root = stage_unpack(args, d)
    total_sids = count_sids(root)
    out.say(f'{total_sids} .sid files in the collection')
    songlengths = root / 'DOCUMENTS' / 'Songlengths.md5'
    stil = root / 'DOCUMENTS' / 'STIL.txt'

    if args.dry_run:
        out.rule('plan')
        for stage, target in (('scan', paths['candidates']),
                              ('relocate', paths['siddir']),
                              ('songlengths', paths['songlengths']),
                              ('loudness', paths['loudness']),
                              ('moods', paths['tags']),
                              ('database', paths['db']),
                              ('ranking', paths['ranking'])):
            state = 'done' if target.exists() else 'to run'
            out.say(f'{stage:<12} {state:<7} {target}')
        return

    out.rule('relocator')
    sidreloc = stage_sidreloc(args, d)

    out.rule('scan')
    if pending('scan', paths['candidates']):
        run([sys.executable, HERE / 'sid_scan.py', root,
             '-o', paths['candidates']], 'scanning headers', total=total_sids)
    cands = json.loads(paths['candidates'].read_text())
    out.say(f'{len(cands)} candidates fit the 4 KB window')

    out.rule('relocate')
    if pending('relocate', paths['siddir']):
        out.say(f'{len(cands)} tunes, {args.jobs} jobs - this is the long one')
        run([sys.executable, HERE / 'sid_reloc_batch.py', paths['candidates'],
             '--sidreloc', sidreloc, '--root', PROXY, '--outdir',
             paths['siddir'], '--page', 'b0',
             '--play-cycles', args.play_cycles, '-j', args.jobs,
             '-o', paths['reloc']], 'relocating to $B000', total=len(cands),
            quiet=True)     # its summary JSON; the counts are printed below
    reloc = json.loads(paths['reloc'].read_text())
    ok = {r['path'] for r in reloc if r['outcome'] == 'ok'}
    out.say(f'{len(ok)}/{len(reloc)} relocated cleanly '
            f'({100 * len(ok) / max(1, len(reloc)):.0f}%)')
    if not ok:
        sys.exit('nothing relocated - is sidreloc working?')
    # Only survivors are worth an LLM call
    paths['survivors'].write_text(json.dumps(
        [c for c in cands if c['path'] in ok], indent=1))

    out.rule('song lengths')
    if pending('songlengths', paths['songlengths']):
        if songlengths.exists():
            run([sys.executable, HERE / 'sid_songlengths.py', songlengths,
                 '-o', paths['songlengths']], 'parsing Songlengths.md5',
                total=False)
        else:
            out.say(f'no {songlengths} - the sound window loses its '
                    f'progress bar')

    out.rule('loudness')
    if args.no_loudness:
        out.say('skipped (--no-loudness): no volume normalization')
    elif pending('loudness', paths['loudness']):
        try:
            import pyresidfp, py65     # noqa: F401
        except ImportError:
            sys.exit('loudness needs the analysis venv: '
                     'pip install pyresidfp py65   (or pass --no-loudness)')
        n = len(list(paths['siddir'].glob('*.sid')))
        out.say(f'{n} tunes through a cycle-exact SID emulator - hours')
        run([sys.executable, HERE / 'sid_loudness.py', paths['siddir'],
             '-j', args.jobs, '-o', paths['loudness']],
            'measuring loudness', total=n)
        run([sys.executable, HERE / 'sid_loudness.py', paths['siddir'],
             '--d418-only', '-j', args.jobs, '-o', paths['d418']],
            'tracing $D418 writes', total=n)

    out.rule('moods')
    if args.tags:
        out.say(f'using prebuilt tags: {args.tags}')
    elif pending('moods', paths['tags']):
        check_llm(args)
        cmd = [sys.executable, HERE / 'sid_mood.py', paths['survivors'],
               '--base-url', args.llm_url, '--model', args.llm_model,
               '--workers', args.llm_workers, '-o', paths['tags']]
        if stil.exists():
            cmd += ['--stil', stil]
        else:
            out.say('no STIL.txt: the tagger works from filenames alone and '
                    'will be less sure of itself')
        if args.pilot:
            cmd += ['--pilot', args.pilot]
        run(cmd, 'tagging moods', total=args.pilot or len(ok))

    out.rule('database')
    if pending('database', paths['db']):
        cmd = [sys.executable, HERE / 'sid_makedb.py', paths['siddir'],
               paths['tags'], '-o', paths['db']]
        for flag, p in (('--loudness', paths['loudness']),
                        ('--d418', paths['d418']),
                        ('--songlengths', paths['songlengths'])):
            if p.exists():
                cmd += [flag, p]
        run(cmd, 'assembling moods.json', total=False)

    out.rule('ranking')
    if args.no_rank:
        out.say('skipped (--no-rank): every tune weighs the same')
    elif pending('ranking', paths['ranking']):
        cmd = [sys.executable, HERE / 'sid_rank.py', '--db', paths['db'],
               '-o', paths['ranking']]
        if args.csdb_ratings:
            cmd.append('--csdb-ratings')
        run(cmd, 'ranking by scene regard', total=False)

    summarize(paths, d)
    if args.deploy:
        deploy(args, d)


def check_llm(args):
    """Fail before an hours-long stage rather than during it."""
    url = args.llm_url.rstrip('/') + '/models'
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.loads(r.read())
        names = [m.get('id') for m in body.get('data', [])]
        listed = ', '.join(filter(None, names))[:60]
        out.say(f'LLM at {args.llm_url}: {listed}')
    except Exception as e:                                   # noqa: BLE001
        sys.exit(f'cannot reach the mood tagger endpoint {url}: {e}\n'
                 '  Point --llm-url at any OpenAI-compatible server '
                 '(llama.cpp, vLLM, Ollama...), or pass --tags with a\n'
                 '  prebuilt tag file to skip this stage entirely.')


def summarize(paths, d: Path):
    out.rule('done')
    db = json.loads(paths['db'].read_text())
    tunes = db['tunes']
    size = sum(f.stat().st_size for f in paths['siddir'].glob('*.sid'))
    out.say(f"{len(tunes)} tunes, {len(db['moods'])} moods, "
            f'{fmt_size(size)} of relocated SIDs')
    out.say(f"moods: {', '.join(db['moods'])}")
    if paths['ranking'].exists():
        rank = json.loads(paths['ranking'].read_text())['tunes']
        best = sorted(rank.items(), key=lambda kv: -kv[1].get('score', 0))[:3]
        titles = {t['id']: t['title'] for t in tunes}
        out.say('best regarded:')
        for tid, e in best:
            out.say(f"{titles.get(tid, tid)[:38]:<38} {e.get('why', '')[:44]}",
                    indent=6)
    out.say('')
    out.say(f'The proxy reads {paths["db"]} automatically on start.')
    out.say('Hear it: tools/sid_review.py    Re-rank: tools/sid_rank.py')
    out.say('These tunes are copyrighted - private enjoyment only. Do not '
            'redistribute the built library.')


def deploy(args, d: Path):
    """Copy only what the proxy actually reads: the database, the ranking,
    your verdicts' effects and the tunes themselves - not the 460 MB HVSC
    tree, and not the intermediate JSON."""
    out.rule('deploy')
    if not have('rsync'):
        sys.exit('need rsync for --deploy')
    dest = args.deploy.rstrip('/') + '/data/sids/'
    cmd = ['rsync', '-a',
           '--info=progress2' if out.tty else '--info=stats1',
           '--include=b000_full/', '--include=b000_full/**',
           '--include=moods.json', '--include=ranking.json',
           '--include=favorites.json', '--exclude=*',
           str(d) + '/', dest]
    # --mkpath is rsync 3.2.3+; older ones need the directory to exist
    ver = subprocess.run(['rsync', '--version'], capture_output=True,
                         text=True).stdout
    m = re.search(r'version (\d+)\.(\d+)\.(\d+)', ver)
    if m and tuple(map(int, m.groups())) >= (3, 2, 3):
        cmd.insert(3, '--mkpath')
    out.say(f'{d} -> {dest}')
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f'rsync failed ({rc})')
    out.say('deployed. Restart the proxy to pick the library up.')


if __name__ == '__main__':
    main()
