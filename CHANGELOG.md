# Changelog

What changed between releases, grouped by the part of the system it
changed. Each entry says what you get, not what was refactored; the
commit messages carry the reasoning and the measurements behind them.

Versions are `major.minor`. The three programs -- the C64 client, the
Windows client and the proxy -- ship together and share one number,
because the wire protocol they speak is versioned separately and a
client is only ever tested against the proxy released beside it.

## 1.1 -- 2026-08-01

Two large things and a lot of small ones.

**The Windows client runs on Windows 10 and 11.** One source tree now
builds twice: Open Watcom still produces the 16-bit `LLM64.EXE` for real
Windows for Workgroups 3.11 (and 95/98), and mingw-w64 produces
`LLM32.EXE`, a 32-bit binary for modern Windows that needs no 16-bit
subsystem. Modern Windows draws your window for you and draws it as
Windows 11, so the client draws its own: caption, menu bar, frame, MDI
children, menus, dialogs, message boxes, buttons, checkboxes and
scrollbars are all ours, and every metric in them was measured against a
capture of a real 3.11 machine rather than remembered.

**The proxy is something you install rather than something you
configure.** It packages as a single self-contained binary for Linux and
Windows, boots into a launcher window instead of a terminal, and opens a
setup wizard the first time that walks you through the mandatory
settings and offers the optional ones -- checking each against the live
system as it goes.

### Windows client -- modern Windows

- `LLM32.EXE`, a 32-bit build for Windows 10/11 and Wine. `make both`
  builds it alongside the 16-bit `LLM64.EXE`; `include/llmport.h` is the
  only file that knows which target it is compiling for.
- The 16-bit build is unchanged in behaviour and still targets real
  3.11, which is the point of building both every time.
- Two latent bugs the second compiler found: a truncated `MDICREATE`
  result that handed back a nonexistent window on Win32, and a subclass
  procedure stored as a `FARPROC`, which Win32's `CallWindowProc`
  rejects.

### Windows client -- the 1993 chrome, drawn by us

All of this lives in `src/chrome.c`, links into both targets, and
matches a real 3.11 window to **50 pixels in 17,252** -- every one of
them inside a letterform, none structural. The controls match to **0 in
1,851**.

- The frame's caption, menu bar and sizing border, including the corner
  grips that delimit the diagonal-resize zone.
- MDI children get 3.1 captions, and a maximised child's sysmenu box and
  restore arrow move into the menu bar the way 3.1 did it.
- The application's own dropdowns are owner-drawn: the System font, an
  18 px gutter and item height, right-aligned accelerators, mnemonic
  underlines, a checkmark in the gutter, and 3.1's plain black separator
  rather than 95's etched pair.
- The control menu on the frame and on every child, with 3.1's greying
  rules, opened on the mouse *release* the way 3.1 opened it.
- Dialogs and message boxes. `MessageBox()` was the last surface still
  wearing 2026; all eight calls go through our own template now.
- Push buttons (a two-pixel bevel mitred at 45 degrees, white corner
  pixels), flat 3.1 checkboxes, flat black edit and list frames instead
  of 95's sunken well, and scrollbars whose arrows have a stem and whose
  trough is solid rather than dithered.
- `Alt+Space` and `Alt+`*letter* work against the drawn menu bar, which
  needed answering `SC_KEYMENU` by hand.
- Windows 11's rounded corners are squared at run time, so the same
  binary stays quiet on Wine and Windows 7.

Verified with two pixel differs kept in the tree
(`tools/pixdiff.py`, `tools/ctldiff.py`) and with standalone spikes
under `spike/` that put chromed windows on screen without the client
attached. Several faults were only ever visible on real hardware --
menus that opened and vanished, a full-window repaint that flashed on a
486, a garbage minimised icon -- which is why the floppy images carry
the spikes.

### Windows client -- editing, reading and the desk

- The input line is multiline: Shift+Enter, grows to four rows, real
  word-grained undo/redo (Ctrl+Z, Ctrl+Shift+Z, Ctrl+Y, Ctrl+_), Up/Down
  history, and Ctrl+V/X spelled out for the 3.1 EDIT control.
- The transcript selects with the mouse and copies with Ctrl+C -- colour
  markers skipped, soft wraps rejoined -- and your own lines sit on a
  faint band. Escape clears the selection first, then cancels the reply.
- The mouse wheel scrolls the transcript, by both routes Windows
  delivers it. Nothing arrives on 3.11, so the period build is
  unchanged.
- The Message menu and a right-click on the transcript type `/redo`,
  `/retcon` and `/fork`.
- The Notebook renames, edits and deletes sheets, and its index splitter
  drags and is remembered.
- Map names wrap with an ellipsis, and edges between distant rooms route
  dotted through the gutters.
- The frame remembers its size and position in `LLM64.INI`, and only
  restores a position that still lands on a screen.
- Help > About says what the program is, who wrote it, and that it is
  donationware -- with a button that opens foxipso.com where the host
  has an association for it.
- Four bugs of one family, all Win16 spellings that compile clean on
  Win32 and misbehave: `EM_SETSEL` packing (Ctrl+Backspace ate the
  line), `WM_COMMAND` ids read raw (Ctrl+1..7 dead on `LLM32`),
  `GetTempFileName`'s drive byte read as a NULL path (every temp file
  failed as "disk full", which took MIDI and pictures with it), and the
  Notebook index filling before the selection moved.
- Music no longer freezes the program while the software synth loads,
  and a closed tune's abort is no longer read as the next tune failing
  to open.

### Proxy -- launcher and packaging

- `pyinstaller llm64.spec` builds the proxy into one self-contained file
  per platform. `PACKAGING.md` has both recipes.
- The binary boots a launcher window: start/stop/restart, live status
  taken off the real server objects, the log mirrored to
  `<data_dir>/proxy.log`, and a `config.toml` editor -- a schema-driven
  form plus a raw tab, written through `tomlkit` so the file's comments
  survive, and validated by running the real config parser before
  anything touches disk. `--headless` is the old CLI, unchanged.
- Every model field is a combobox with a refresh button that asks the
  endpoint what it actually serves: `GET /models` for OpenAI-compatible
  APIs, `ListModels` for Gemini, and ComfyUI's per-node `/object_info`
  for checkpoint, CLIP, VAE and LoRA choices.
- The window title carries the version, because a packaged binary has
  nowhere else to say it.

### Proxy -- the setup wizard

New, and it opens by itself on a machine with no `config.toml` (or one
still carrying the template's placeholder endpoint with no key
anywhere). Otherwise it is the **Setup wizard** button in the launcher
toolbar, and it is re-runnable by design.

- Ten steps. Exactly two of them are settings the proxy cannot start
  usefully without -- an LLM endpoint, and the address the client dials
  -- and the five feature steps after them are marked optional on the
  page itself.
- Each step checks itself against the live system rather than against
  the text in the file. The LLM step sends one real completion, which is
  the only thing that proves the URL, the key and the model name
  together; the network step binds the port and tells you which of this
  machine's addresses to type into the client; the printing step is
  checked against `lpstat` and can spool a real test page; the images
  step can generate a real picture and show you the C64 render.
- Checks run on a worker thread, so a cold local model loading for three
  minutes does not look like a hung window.
- Steps that cannot be finished from a window say so and tell you to
  come back: the music libraries are large downloads that take hours to
  tag, and the wizard gives you the commands and picks the result up on
  the next run.
- Every step is written as you leave it, so an abandoned run keeps what
  you finished. Edits are surgical -- the file's comments, key order and
  anything the wizard does not know about all survive.
- **Save and close** is named for what it does, and **Cancel** is the
  other ending: it puts every file the wizard touched back the way it
  was when the window opened, and removes a `config.toml` the wizard
  created. It names the files and asks first.
- Finishing writes a `[wizard]` table into `config.toml`. That table is
  the only thing stopping it opening unasked; delete it to get the
  first-run behaviour back.

### Proxy -- illustrations

- **Illustrations tab** in the launcher. It runs the server's own image
  path against the Settings form as it stands, unsaved edits included,
  and shows three panels: what the backend drew, the C64 multicolor blob
  rendered back, and the Windows DIB rendered back. A picture that is
  wrong in the original is a prompt problem; one that is fine there and
  mud on the C64 is a subject problem, and you cannot tell which from a
  single image. Every run is saved with the settings that produced it,
  so last night's comparison survives a restart.
- **Style presets.** `[images] style` picks a named look in one key:
  `cinematic` (with a LoRA workflow), `oil-chiaroscuro`, `painted-noir`
  and `nova-furry`. Your own go in `[images.styles.<name>]` tables, and
  one that reuses a built-in name overrides it key by key.
- **Anthro art gets its own chain.** A general-purpose model draws a
  beast-person by drawing a person and hoping. `workflows/novafurryxl.json`
  is an SDXL graph for a furry-tuned checkpoint, and `[images]
  prompt_format = "tags"` has the scene composer write tag strings
  rather than prose, because a Danbooru-lineage checkpoint was trained
  on tags and prose has no `solo` and no `upper body`.
- Bundled workflows may carry a `_defaults` table, so selecting one in
  the launcher is enough to run it -- previously picking a non-Flux
  workflow by hand got `ckpt_name not in [...]` from ComfyUI.
- `workflows/flux2-klein-retro.json` rides inside the frozen binary and
  is the default when none is configured, so `backend = "comfyui"` alone
  is a working setup.
- A rejected workflow now says which node and which value ComfyUI
  refused, instead of "see log".
- **Fixed:** the `cinematic` style prefix carried a species-fidelity
  clause, and a style prefix rides on *every* prompt -- so it said that
  about empty rooms too, and a crypt described as deserted came back
  holding a giant muzzled wolf. Species fidelity moved into the composed
  sentence, where it can only be said about a character the scene
  actually has.
- **Fixed:** the composed scene was capped at 400 characters, which cut
  it mid-word and took the who-is-present roster with it -- the one
  thing keeping extra creatures out of the picture.
- **Fixed:** the visual canon was capped at 400 as well. It is JSON, so
  the cut meant the parse failed and the fallback stored the scaffolding
  itself: a live conversation's ledger beginning `{\n  "player": "`.
- A `/pic` naming a character now narrows the visual canon to that
  character, which is most of why the rest of the party used to turn up
  in a portrait of one of them.

### Proxy -- adventure and conversations

- **The narrator's dice are shown and audited.** When the model spends
  one of the turn's real rolls it stamps `[[ROLL: ...]]` into the reply;
  the player sees a rendered `[dice: ...]` line exactly where the roll
  fell, and the saved history drops it. The player always sees the die;
  the model never rereads its own roll-talk, which is how it learns to
  roll for crossing a quiet room. Payloads are kept in conversation meta
  so "did it roll silently?" is one grep.
- `/redo`, `/retcon` and `/fork` are plain commands, so the C64 has them
  too.
- The adventure state block carries spells -- a custom-class caster
  previously had no channel to learn one.
- `/sheet` fills in who you are when character generation never ran:
  `/adventure <theme>` and "surprise me" go straight into play, so name,
  race, class, skills and kit used to stay blank forever. The narrator
  is asked once for what the *story* has established. Ability scores
  stay blank on purpose -- a rolled score means dice were thrown, and a
  window showing invented numbers is worse than one showing a blank.
- Adventure setup strips control characters, treats "you can name my
  character" as delegation, and sizes the review screen to the client.
- **Fixed:** every reply carrying no `[[STATE:]]` block raised an error
  after the answer had already streamed, so a complete reply was
  followed by a red error line. This was not adventure-specific -- no
  turn outside adventure mode has a state block, so every one of them
  errored.

### Proxy -- configuration and platform

- `[api] read_timeout` (default 600, `OPENAI_READ_TIMEOUT`) replaces the
  hardcoded httpx read timeout, and the client heartbeat cap derives
  from it so the API always gives up first.
- Bundled data -- the adventure rules, the SID overrides, the default
  character cards -- resolves through `respath.py` rather than
  `__file__`, so a frozen binary finds it.
- The Claude CLI is split and resolved the way Windows needs: non-POSIX
  splitting so `C:\bin\claude` survives, and `shutil.which` so npm's
  `claude.cmd` shim launches.
- The launcher reports the SID and MIDI libraries: counts when they are
  found, and the build scripts to run when they are not.
- The default prompts learned mood -- prose, one light source, no piles
  of negations.

### C64 client

- **A second disk for VICE, `llm64-vice.d64`.** The shipped disk is a
  `CONNECT=hayes` build: it dials `ATDT`, so it needs something that
  answers AT commands, and VICE has nothing that does. Running it there
  loops on "Resetting modem..." forever, which looks like a broken
  program rather than a missing `tcpser`. The VICE disk is the same
  client and the same overlay modules built `CONNECT=direct`, where the
  ACIA is the socket: VICE dials the proxy through `-rsdev1`, nothing
  extra is installed, and nothing is configured. `make disk-vice` builds
  it and `make release` ships both.
- Because a direct build never dials, it now says "Connected through the
  emulator's serial port" rather than naming the compiled-in address it
  did not use, and the config editor's address field says it is ignored
  instead of quietly changing nothing.
- **`llm64.d64` means one thing now.** `./run.sh emu-80` used to write a
  `CONNECT=direct` build under that name while `make disk` wrote a
  `CONNECT=hayes` one, so what the file contained depended on which
  command had run last. The emulator path writes `llm64-vice.d64`
  instead.
- `emu/run_emu.sh` boots a disk rather than a loose `llm64.prg`: the
  VICE disk in direct mode, the hardware disk in hayes mode, where it
  also starts a `tcpser` for you if the port is free and takes it down
  on exit. An image carries its client and its modules together, so it
  cannot pair a client with modules built against a different one.

### Build and release

- **`make release`** builds every shippable in one command: both C64
  disks, both Windows client binaries, the 1.44 MB floppy image and both
  proxy binaries, then prints a manifest with sizes and timestamps --
  because a stale artifact from last week looks exactly like a fresh one
  in an `ls`. `MODE80=1` on the disk build is not optional and the
  target remembers it for you.
- **The Windows proxy `.exe` builds on Linux.** PyInstaller does not
  cross-compile, but a Windows CPython running under Wine is a Windows
  CPython as far as it is concerned. `tools/win-build-setup.sh` does the
  one-time prefix, `tools/win-build.sh` the per-build half.
- `tools/llm32.sh` retires a resident wineserver before starting the
  client, which is what it takes to actually silence Wine's debug
  chatter, and filters the hardware-message spam that gets past
  `WINEDEBUG=-all` anyway.
- The end-to-end adventure test walks the kit shop the way the unit
  tests do, instead of answering the gear step with a keypress from
  before that screen was reworked.
- Watcom object files that had been committed by accident are out of the
  tree and covered by `.gitignore`.

### Documentation

- The proxy README gains sections on the standalone binary, the setup
  wizard, previewing illustrations, and packaging.
- The architecture diagram stops calling it the Linux proxy; port checks
  and firewall advice now cover Windows as well as Linux.
- The client comparison table gains a Windows 10/11 column and a
  screenshot.
- `docs/17-win32-modern-windows.md` keeps the pre-implementation
  analysis that started the port, headed with the two verdicts it got
  wrong.
- "Shareware" is "donationware" in all three places that say it.
- A missing SoundFont is silent MIDI routing that looks like working
  software; the README names the packages and a zero-install fallback.

## 1.0 -- 2026-07-29

Initial release.
