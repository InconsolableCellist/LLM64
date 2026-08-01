# Packaging the proxy as a standalone binary

`pyinstaller llm64.spec` turns the proxy into one self-contained file
with a desktop launcher: a window with Start/Stop/Restart buttons,
live status (connections, LLM calls), a scrolling log, and an editor
for config.toml. No Python install is needed on the machine that runs
it.

PyInstaller does not cross-compile: build on Linux to get the Linux
binary, build on Windows to get the .exe. The spec, the code and these
steps are identical on both. You can still make both on one Linux box,
because a Windows Python running under Wine is a Windows Python as far
as PyInstaller is concerned -- see "Build the .exe on Linux, with
Wine" below.

`make release` in the repository root runs all of this for you, along
with the C64 and Windows clients.

## Build on Linux

```bash
cd llm64_proxy
python3 -m venv .venv-build          # a python with tkinter; the
                                     # linuxbrew one does not have it
.venv-build/bin/pip install -r requirements.txt pyinstaller
.venv-build/bin/pyinstaller llm64.spec
# result: dist/llm64-proxy  (~47 MB)
```

Build on the oldest distro you intend to support: the binary carries
Python but not glibc, so it runs on the build machine's glibc version
or newer.

## Build on Windows

Install Python 3.12+ from python.org (keep the default "tcl/tk and
IDLE" component checked - the launcher UI needs it). Then in a
terminal:

```bat
cd llm64_proxy
py -m venv .venv-build
.venv-build\Scripts\pip install -r requirements.txt pyinstaller
.venv-build\Scripts\pyinstaller llm64.spec
rem result: dist\llm64-proxy.exe
```

Rename the exe to whatever you like; nothing inside cares.

## Build the .exe on Linux, with Wine

If you have Wine, you do not need a Windows machine. Install a Windows
Python into a Wine prefix once:

```bash
cd llm64_proxy
./tools/win-build-setup.sh      # ~15 min, ~600 MB in ~/.wine-llm64proxy
```

It downloads CPython 3.12 from python.org, installs it under Wine with
tcl/tk included, and pip-installs the requirements and PyInstaller.
Then build the exe any time with either of:

```bash
make proxy-bin-win              # from the repository root
./tools/win-build.sh            # or directly
# result: dist/llm64-proxy.exe
```

The two builds share `dist/` but not their PyInstaller work
directories -- the Windows one uses `build/win`, so neither poisons the
other's cache.

Overrides: `PYVER=3.13.7 ./tools/win-build-setup.sh` for a different
CPython, `WINEPREFIX=...` for a different prefix (export it for both
scripts if you change it). Throw a broken prefix away with
`rm -rf ~/.wine-llm64proxy` and rerun the setup; nothing else lives
there.

Test the result before shipping it: `wine dist/llm64-proxy.exe` should
open the launcher window. Wine is a good enough Win32 to build against,
but the binary has only really been exercised once it has run on
Windows itself.

## Running the packaged proxy

Double-click the binary (or run it with no arguments) and the
launcher window opens. It looks for `config.toml` next to the
executable; if there isn't one, the Settings tab's "Create config"
button writes one from the built-in template. Edit either in the
Settings form (each field validated and explained) or in the Raw
config tab, then "Save & Restart".

Two settings live in a `[server]` table the launcher owns (the CLI
takes them as flags instead): `host` (default `0.0.0.0`) and `port`
(default `6400`).

Relative paths in the config (`./data`, `./cards`) resolve against the
directory config.toml is in, exactly as they do when you run the
proxy from a checkout.

The log shows in the window and is also written to
`<data_dir>/proxy.log` (rotated at 1 MB, 3 backups); the path is
displayed at the bottom of the Log tab once the server starts.

`--headless` skips the window and runs the plain CLI server with the
same flags as `python -m src` (`--host`, `--port`, `--config`, `-v`).
On Windows the binary is a windowed app, so headless mode has no
console output - watch the log file.

## Windows notes

- The first start pops a Windows Firewall prompt (the proxy listens
  for the C64). Allow it, at least on private networks.
- SmartScreen may warn about an unsigned exe the first time: "More
  info", then "Run anyway". Signing the binary is the fix if you
  distribute it widely.
- In config.toml, write Windows paths with forward slashes
  (`C:/Users/you/code`) or single quotes (`'C:\Users\you\code'`) -
  double-quoted TOML strings treat backslash as an escape.
- The `cups`/`both` printer backends need the `lp` command and so stay
  Linux/macOS-only; leave `backend = "c64"` (the default) on Windows.
- Claude Code mode needs the `claude` CLI installed and authenticated
  on the same machine, as ever; the npm `claude.cmd` shim is found
  automatically.
