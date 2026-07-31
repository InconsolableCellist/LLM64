# 17 - The Windows client on modern Windows

*Analysis, 2026-07-29. Scope: what it would take to run `win311_client/`
natively on Windows 10/11 while keeping the 1993 look. No code written
yet. Doc 16 §9.2 sketched a dual target from the start; this is the
version of that sketch made concrete against the code as it now stands
(7,058 lines: `main.c` 5,560, `scroll.c` 550, `net.c` 230, `wire.c` 104).*

> **Status, 2026-07-30: built.** Read this as the plan it was, not as a
> description of the code. The dual target shipped on branch
> `win32-port` -- one source tree, `include/llmport.h` the only file that
> knows which target it is -- and the chrome went well past the tiers
> below: caption, menu bar, frame, MDI children, dialogs, message boxes,
> push buttons and checkboxes are all drawn by `src/chrome.c`, measured
> against a real 3.11 capture with `tools/pixdiff.py` and
> `tools/ctldiff.py` rather than from memory.
>
> Two verdicts here were wrong and are worth keeping visible. Dropping
> MDI turned out to be unnecessary -- an MDI child can give up its
> non-client area and keep `DefMDIChildProc`, which `spike/mdi.c` proved
> in an hour. And the pixel-scaling question below never had to be
> answered: the chrome is drawn at 1:1 and the host's own DPI handling
> was left alone.
>
> Still the host's, at the time of writing: scrollbars, the combo box
> dropdown, and the System font.

## 0. Verdict

**The mechanical port is nearly free. The aesthetic is the whole
project.**

Getting `LLM64.EXE` to compile as a 32-bit PE, run on Windows 11, and
talk to the proxy is roughly a **300-400 line diff plus a compatibility
header** -- about 65 edit sites, every one of them a known Win16-to-Win32
substitution with a mechanical answer. `wire.c` and `scroll.c` need
**zero** changes; they already build for the host, and `make test` passes
today. `net.c` needs type cosmetics only -- `WSAAsyncSelect` is fully
supported in Win32.

The hard part is that **modern Windows will draw your window for you**,
and it will draw it as Windows 11. The title bar, the borders, the system
colours, the menus, the scrollbars and the file dialog are all things the
OS supplies, and every one of them is wrong by 30 years. Keeping the
retro look means taking over that drawing. That is where the real lines
of code are, and it is a design decision, not a port.

**Recommendation: do the mechanical port, decide the pixel-scaling model
before writing any new drawing code, then buy fidelity in tiers and stop
when it looks right.** Full fidelity is ~2,000-2,800 new lines on top of
the port.

There is a second reason to want this build that has nothing to do with
modern Windows: **DGROUP on the 16-bit target is down to ~9.9 KB spare.**
The Win32 build has no 64 KB ceiling, so it is also the place where
features currently blocked by segment arithmetic can land.

---

## 1. What the mechanical port actually costs

Counted against the current tree, not estimated:

| Win16-ism | Sites | Win32 answer |
|---|---|---|
| `_export` / `PASCAL` on window procs | 14 procs | `CALLBACK`; macro it in the compat header |
| `GetWindowWord(h, GWW_HINSTANCE)` | 21 | collapse to one `g_inst` global set in `WinMain` |
| `WM_COMMAND` param packing | 9 | `CMD_ID()` / `CMD_NOTIFY()` cracking macros |
| `__huge` pointers, `huge_bite`/`huge_store` | 6 decls, 2 bodies | plain `memcpy`; the helpers keep their names |
| `MakeProcInstance`/`FreeProcInstance` | 4 pairs | `#define` them away |
| `MoveTo` | 5 | `MoveToEx` |
| `WM_MDIACTIVATE` param packing | 2 | `wParam`/`lParam` are the two HWNDs in Win32 |
| `WM_CTLCOLOR` | 1 | `WM_CTLCOLOREDIT` |
| `WM_VSCROLL` param packing | 1 | code and position both move into `wParam` |
| `GetTextExtent` | 1 | `GetTextExtentPoint32` |
| `OpenFile(..., OF_DELETE)` | 5 | `DeleteFile` (but see §6) |
| `WinMain` signature, `hPrev` test | 1 | `hPrev` is always NULL in Win32, so the class always registers |

Three things people expect to be problems and are not:

- **`GlobalAlloc`/`GlobalLock`/`GlobalFree` (25 sites) need no change.**
  They all still exist in Win32; `GlobalLock` just hands back the
  pointer. The picture, image and MIDI buffers port untouched. Only the
  `__huge` *declarations* go.
- **`lstrcpy`/`lstrlen`/`wsprintf` (70 sites) need no change.** All three
  are still in the Win32 API.
- **`GetWindowLong(GWL_WNDPROC)` subclassing and `CallWindowProc` are
  unchanged.** The `EditProc` subclass on the input box ports as-is.

### The one architectural rule

Do this with a `include/win16.h` compatibility header and cracking
macros, so **one source tree serves both targets**. Fork the file and the
3.11 build rots inside a month -- and the 3.11 build is the point of the
exercise. `make` should build both, and `make test` should stay green for
both.

### 32-bit or 64-bit

**Build 32-bit (`i686-w64-mingw32`).** WoW64 is present on every
consumer x64 Windows and Windows 11 on ARM emulates x86-32, so it runs
everywhere; the diff is strictly smaller, because `SetWindowLong(hwnd, 0,
(LONG)v)` for the pane's `View *` and the four other `(LONG)` pointer
casts stay legal.

But do the `LONG_PTR`/`GetWindowLongPtr` pass anyway -- it is about six
sites -- so a 64-bit build becomes a Makefile flag rather than a second
project.

Resources: `wrc` becomes `windres`. `src/llm64.rc` is 156 lines of plain
`MENU`, `DIALOG` and `ACCELERATORS` and should need nothing.

**Estimate: 1-2 days to a binary that runs on Windows 11 and holds a
conversation.**

---

## 2. What actually breaks the look

In rough order of how much it hurts:

**1. The window frame.** Windows 10/11 draw top-level window frames
through DWM. You get the modern flat caption, the modern min/max/close
buttons, and on Windows 11 rounded corners and a drop shadow. There is no
flag that gives you a 1993 caption. This is the single most jarring thing
and the only fix is to own the non-client area.

**2. The system colour scheme.** With no visual-styles manifest you get
*classic drawing* but *modern colours*: `COLOR_BTNFACE` is 240,240,240,
not the 3.1 grey of C0C0C0, and `COLOR_MENU` is white. You cannot change
this per process -- `SetSysColors` is global and DWM ignores much of it.
The code already helps itself here: most of its greys come from
`GetStockObject(LTGRAY_BRUSH)`, which is a hardcoded C0C0C0 and survives.
But the 11 `GetSysColor`/`GetSystemMetrics` calls will return 2026
values.

**3. MDI child captions.** These are child windows, so USER32 draws them,
not DWM -- and with visual styles off it draws them *classic*. Classic is
Windows 95, not Windows 3.1: three caption buttons instead of 3.1's
sysmenu box plus a down-arrow and an up-arrow. Closer than the frame, but
still two years wrong.

**4. Menus.** Classic-drawn but with modern colours, per (2).

**5. Scrollbars.** The closest of the lot. Classic Win32 scrollbars are
very nearly the 3.1 article; the deltas are the arrow glyph and hot-track
highlighting.

**6. The Save Picture dialog.** Worth being precise about this one,
because the code looks like it already asks for the old dialog and does
not. `main.c:4254` sets `OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST |
OFN_HIDEREADONLY` and no `OFN_EXPLORER` -- but the old-style dialog
requires `OFN_ENABLEHOOK` **and** an `OFNHookProcOldStyle`, not merely
the absence of `OFN_EXPLORER`. Without the hook you get the Explorer
dialog. (The old style is still supported on Windows 10/11, so this is
reachable -- it just needs the hook.)

**7. Fonts.** `GetStockObject(SYSTEM_FIXED_FONT)` and `SYSTEM_FONT` are
expected to still return the raster faces (`vgafix.fon`, `vgasys.fon`
ship with Windows 10; GDI stock objects see raster fonts even though
DirectWrite does not). Verify on target, and see §5 for why you should
ship your own face regardless.

One piece of the existing design deserves credit here: `fonts_init` at
`main.c:216` **measures** every font variant and rejects any whose cell
metrics differ from the plain face. That is DPI-agnostic and font-source
agnostic, and it will keep working under all of this.

---

## 3. Decide the scaling model first

Do this before writing a single line of new drawing code, because it
determines the coordinate math in every paint and hit-test handler and
you do not want to write that twice.

**Option A -- stay DPI-unaware.** Windows bitmap-stretches the whole
window at 150%/200%. Zero work. Blurry, and blurry is the one thing that
reads as "broken" rather than "old".

**Option B -- integer pixel scaling (recommended).** Declare per-monitor
DPI awareness, render the entire UI into an offscreen 1x DIB section at
the logical size, then `StretchBlt` with `COLORONCOLOR` at an integer
factor derived from the monitor DPI. You get crisp chunky pixels, which
is exactly the aesthetic, and the picture window's 320x200 art lands on
exact integer multiples -- better than it ever looked on period hardware.

Option B is well-suited to this code: every window already paints through
a single `hdc` and the transcript is already a measured character grid.
The work is plumbing the `hdc` swap plus one inverse transform on mouse
coordinates. Call it ~250-400 lines, spread thin across the input
handlers.

Also, whichever you pick: one `DwmSetWindowAttribute` call with
`DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_DONOTROUND` to stop Windows 11
rounding the corners off a 1993 window.

---

## 4. Fidelity tiers

**Tier 0 -- it runs.** The §1 port, nothing else. Retro content inside a
Windows 11 frame with 2026 greys. *1-2 days.* Useful as a checkpoint;
not shippable as an aesthetic.

**Tier 1 -- 1993 inside the frame.** *~700-1,000 new lines, 3-5 days.*
- A `sys3d()` shim returning the 3.1 palette (C0C0C0 face, 808080
  shadow, FFFFFF highlight, 000080 active caption, 000000 frame) in place
  of the 11 `GetSysColor` calls.
- Owner-draw everything that shows chrome. The launcher buttons are
  already owner-drawn (`WM_DRAWITEM`, `main.c:5052`), so the pattern
  exists -- extend it to the checkbox, the listboxes
  (`LBS_OWNERDRAWFIXED`), and the menu bar and popups (`MF_OWNERDRAW`
  plus `WM_MEASUREITEM`; mnemonics keep working).
- Replace the standard scrollbars with your own. You cannot owner-draw a
  standard scrollbar, and the panes already compute their own ranges and
  positions, so this is ~150 lines and buys a lot.
- Ship the font instead of borrowing it: embed a pixel-exact face and
  register it with `AddFontMemResourceEx`, so the look does not depend on
  what Windows still happens to ship.
- Add the `OFNHookProcOldStyle`, or replace the save dialog with an
  in-house 3.1-style one.

**Tier 2 -- own the window frame.** *~600-1,000 lines, 4-7 days, and the
real risk.*
- The frame becomes `WS_POPUP` with no NC area, and you draw the 3.1
  caption yourself: solid navy when active, grey when not, bold white
  System-font title, sysmenu box on the left, down-arrow and up-arrow on
  the right, no close button. Plus the 4px 3D sizing border and
  `WM_NCHITTEST` for the eight resize zones and the caption drag.
- MDI children need the same treatment via `WM_NCCALCSIZE` /
  `WM_NCPAINT` / `WM_NCHITTEST` layered under `DefMDIChildProc`, and
  MDI's own NC handling will fight you.

  **Decision point:** the alternative is to drop MDI and hand-roll the
  child window manager. That sounds worse and is probably less total
  work, because the app already owns its layout -- `desk_remember`,
  Default Layout, the `Ctrl+1..7` toggles and the launcher strip are
  already a window manager in everything but name. Doc 16's reasons for
  choosing MDI were period accuracy and the free Window menu; weigh
  those against writing NC handlers twice.

**Tier 3 -- integer pixel scaling.** §3 Option B. *~250-400 lines, 2-3
days.* Sequenced last but **decided first**.

Sensible stopping point: **Tier 0 + Tier 1 + Tier 3.** That gets crisp
1993 pixels everywhere inside a modern title bar, which is a coherent
look ("a 1993 app running on your PC") rather than a broken one. Tier 2
is where you commit to owning window management, and it should be its own
decision made while looking at a screenshot of Tier 1.

---

## 5. What gets *better*

Not all of this is loss. The Win32 build fixes several things the README
currently has to apologise for:

- **Accelerators work.** `Ctrl+F4`, `Ctrl+F5`, `F1`/`F2`/`F3` are dead
  under Wine's 16-bit layer, which is the biggest caveat in the client
  README. On a real Win32 target they simply work, and the GUI smoke test
  can finally exercise them.
- **Music just works.** `mciSendString` with the `sequencer` device plays
  through the built-in GS Wavetable synth. No FluidSynth, no ALSA
  sequencer plumbing, no "Getting sound out of Wine" section. (Reword the
  "no MIDI Mapper configured" status message -- the MIDI Mapper UI has
  been gone since Vista.)
- **No DGROUP ceiling.** At ~9.9 KB spare, the 16-bit build is close to
  the wall. Features that do not fit there fit here.
- **The temp-file picture shelf becomes optional.** It exists because
  16-bit could not hold several DIBs at once. Keep the interface, drop
  the files, or leave it alone -- it costs nothing either way.
- **Testing gets cheaper.** A Win32 PE under Wine needs no 16-bit
  subsystem, so `xdotool type` behaves and `wine_smoke.sh` stops
  fighting the VDM's synthetic-keystroke handling.

---

## 6. Verify on target before trusting this doc

Five claims here are "expected, not measured". Check them early, because
two of them could move the estimate:

1. **`OpenFile`, `_lopen`, `_lread` and `OF_DELETE` in Win32.** All are
   kept in kernel32 as 16-bit compatibility APIs and mingw declares them,
   so those 8 sites may need *no* change. Try compiling before rewriting
   them.
2. **`SYSTEM_FIXED_FONT` / `SYSTEM_FONT` on Windows 11** still returning
   raster faces (§2.7).
3. **Exactly what classic MDI child captions look like** with no manifest
   on Windows 11 -- this decides how much of Tier 2 you actually need.
4. **`mciSendString` `sequencer`** on Windows 11.
5. **`OFNHookProcOldStyle`** still producing the 3.1-era file dialog.

---

## 7. The alternatives, for honesty

- **86Box, PCem, or QEMU with a real WfW 3.11 install.** Perfect
  fidelity, zero code, and it already works. It is a VM rather than a
  Windows application, which is the entire difference.
- **otvdm / winevdm** runs the existing NE binary on modern Windows, but
  it maps Win16 windows onto real Win32 windows -- so you get the modern
  chrome anyway, and none of Tier 1 or Tier 2 becomes cheaper. Not a
  fidelity route.
- **Win32s** is a trap, as doc 16 §9.2 already noted: modern mingw output
  will not run on it.

---

## 8. Estimate

| | New/changed lines | Days |
|---|---|---|
| Tier 0 -- mechanical port, one tree, two targets | ~300-400 diff | 1-2 |
| Tier 1 -- colours, owner-drawn controls, own font, own file dialog | ~700-1,000 | 3-5 |
| Tier 2 -- own the frame and the child captions | ~600-1,000 | 4-7 |
| Tier 3 -- integer pixel scaling | ~250-400 | 2-3 |

Full fidelity is ~2,000-2,800 new lines. The recommended stopping point
(0 + 1 + 3) is ~1,250-1,800 and about a week and a half of evenings.

The standing risk through all of it is the one in §1: two targets, one
tree. Every tier above is drawing code, and drawing code is exactly what
wants to diverge. Keep the 16-bit build green on every commit or accept
that you have shipped a Win32 client and retired a Win16 one.
