#!/usr/bin/env python3
"""The launcher's illustration preview: prompt precedence, the DIB
inverse, and one whole generation through the fixture backend.
Run: python3 tests/test_preview.py

The point of the preview is that it is NOT a separate rendering path -
it is the server's, run once. So what matters here is that it reads the
same settings the server reads (a named style preset folded in, a
per-client prefix chosen by the same rules) and that what it puts on
screen came back through the real converters.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image

from src import preview
from src.imaging import convert_to_dib8, render_preview_dib
from src.images import DEFAULT_STYLE_PREFIX
from src.imgstyles import LORA_WORKFLOW, PRESETS
from src.profiles import VGA_STYLE

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}:\n  got  {got!r}\n  want {want!r}")


def check_true(name, cond, detail=''):
    if not cond:
        failures.append(f"{name}: false {detail}")


# --- the DIB inverse ---------------------------------------------------
#
# render_preview_dib is what the preview tab shows for a Windows client,
# so a bug in it would be read as a bug in the converter. Orientation and
# channel order are the two things that can silently be wrong.

src = Image.new("RGB", (800, 500), (40, 90, 160))
for x in range(100):
    for y in range(100):
        src.putpixel((x, y), (250, 30, 30))              # top-left red
        src.putpixel((799 - x, 499 - y), (30, 250, 30))  # bottom-right green

dib, w, h = convert_to_dib8(src)
back = render_preview_dib(dib)
check("the inverse gives the DIB's own geometry", back.size, (w, h))


def dominant(img, box):
    """Which primary the corner reads as, by channel."""
    r, g, b = 0, 0, 0
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            px = img.getpixel((x, y))
            r, g, b = r + px[0], g + px[1], b + px[2]
    return max((r, 'r'), (g, 'g'), (b, 'b'))[1]


check("top-left stays red (rows are bottom-up, not flipped twice)",
      dominant(back, (2, 2, 20, 20)), 'r')
check("bottom-right stays green",
      dominant(back, (w - 20, h - 20, w - 2, h - 2)), 'g')
check("the field between them is blue, not red (RGBQUAD is B,G,R)",
      dominant(back, (w // 2 - 10, h // 2 - 10, w // 2 + 10, h // 2 + 10)),
      'b')

for name, bad in (("a truncated header", dib[:60]),
                  ("pixel data cut short", dib[:40 + 1024 + 16])):
    try:
        render_preview_dib(bad)
        failures.append(f"{name}: accepted, should have raised")
    except ValueError:
        pass


# --- which style prefix a client gets ----------------------------------

check("no config: the C64 gets the built-in default",
      preview.resolve_prefix({}, 'c64')[0], DEFAULT_STYLE_PREFIX)
check("no config: a Windows client gets VGA pixel art instead",
      preview.resolve_prefix({}, 'win16')[0], VGA_STYLE)
check("and says where that came from",
      preview.resolve_prefix({}, 'win16')[1], 'the win16 client profile')

configured = {'style_prefix': 'Woodcut. Scene: '}
check("a configured prefix wins for the C64",
      preview.resolve_prefix(configured, 'c64')[0], 'Woodcut. Scene: ')
check("...and for Windows too - it is the operator's global choice",
      preview.resolve_prefix(configured, 'win16')[0], 'Woodcut. Scene: ')

# "" is a real answer (a ComfyUI workflow that owns its own look), and
# has to beat the profile default the same way any other setting does.
check("an empty prefix is a choice, not an absence",
      preview.resolve_prefix({'style_prefix': ''}, 'win16')[0], '')

check("the scene is appended verbatim",
      preview.compose_prompt(configured, 'a dark room', 'c64')[0],
      'Woodcut. Scene: a dark room')


# --- reading the [images] table the way the server does ----------------

CFG = """
[storage]
data_dir = "./data"

[images]
mode = "ask"
backend = "fixture"
style = "cinematic"

[images.fixture]
path = "{fixture}"
"""

table = preview.images_table(CFG.format(fixture='/nonexistent.png'))
check("a named preset lands in style_prefix, as at boot",
      table['style_prefix'], PRESETS['cinematic']['style_prefix'])
check("a preset carrying a LoRA also chooses the LoRA workflow",
      table['comfyui']['workflow'], LORA_WORKFLOW)
check("the LoRA reaches the workflow's token",
      table['comfyui']['vars']['LORA'], PRESETS['cinematic']['lora'])
prefix, source = preview.resolve_prefix(table, 'win16')
check("a preset beats the client profile", prefix,
      PRESETS['cinematic']['style_prefix'])
check("and reports itself as a preset", source, 'style preset "cinematic"')

try:
    preview.images_table("this is not toml =")
    failures.append("broken toml: accepted, should have raised")
except preview.PreviewError:
    pass

# A preset that is only a LoRA leaves the config's own prefix standing,
# so it must not be credited with it - that would send an operator to
# edit the wrong table.
bare = preview.images_table("""
[images]
style = "mylora"
style_prefix = "Woodcut. Scene: "
[images.styles.mylora]
lora = "x.safetensors"
""")
check("a bare-LoRA preset keeps the configured prefix",
      preview.resolve_prefix(bare, 'c64'),
      ('Woodcut. Scene: ', '[images].style_prefix'))
check("...and still gets the LoRA workflow",
      bare['comfyui']['workflow'], LORA_WORKFLOW)


# --- one whole generation ----------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    fixture = tmp / 'fixture.png'
    # Something with structure, so the converters have work to do.
    art = Image.new("RGB", (1024, 640), (20, 20, 40))
    for x in range(1024):
        for y in range(640):
            if (x // 64 + y // 64) % 2 == 0:
                art.putpixel((x, y), (200, 160, 60))
    art.save(fixture)

    config_path = tmp / 'config.toml'
    config_text = CFG.format(fixture=fixture)
    config_path.write_text(config_text)

    result = preview.generate_preview(config_text, str(config_path),
                                      'a ruined chapel', target='c64',
                                      caption='Rain on old stone')
    check("the prompt is preset prefix + scene", result.prompt,
          PRESETS['cinematic']['style_prefix'] + 'a ruined chapel')
    check("the C64 render is a full frame", result.c64.size, (320, 200))
    check("the Windows render is 320x200 too (1.6:1 source)",
          result.vga.size, (320, 200))
    check("the original is handed back unresized",
          result.original.size, (1024, 640))
    check("the backend is named for the record", result.backend, 'fixture')

    # The caption band is burned in by the real converter, so it is
    # visible in the render: the bottom rows must hold white pixels the
    # original (a two-tone check pattern) has nowhere else.
    bottom = [result.c64.getpixel((x, 195)) for x in range(320)]
    check_true("the caption band reaches the C64 render",
               (255, 255, 255) in bottom)

    # Saved beside the config, under data_dir, in its own folder.
    saved = tmp / 'data' / preview.PREVIEW_SUBDIR
    check("the original is kept", result.stem.with_suffix('.png').exists(),
          True)
    check("both renders are kept",
          all((result.stem.parent / f'{result.stem.name}{s}.png').exists()
              for s in ('-c64', '-vga')), True)
    meta = json.loads(result.stem.with_suffix('.json').read_text())
    check("the settings that made it are written down", meta['style'],
          'cinematic')
    check("...including the prompt", meta['prompt'], result.prompt)
    check("previews stay out of the conversation folders",
          result.stem.parent, saved)

    listed = preview.list_previews(tmp / 'data')
    check("the strip finds it", len(listed), 1)
    check("with its renders", [listed[0][k] is not None
                               for k in ('original', 'c64', 'vga')],
          [True, True, True])

    # Two in the same second must not overwrite each other - a cached
    # backend answers fast enough for that to happen.
    second = preview.generate_preview(config_text, str(config_path),
                                      'a ruined chapel', target='win16')
    check("a second preview gets its own files",
          second.stem != result.stem, True)
    check("the win16 prompt uses the same preset prefix (it is configured)",
          second.prompt.startswith(PRESETS['cinematic']['style_prefix']), True)
    check("both are listed, newest first", len(preview.list_previews(
        tmp / 'data')), 2)

    # A backend that cannot run says so before spending anything.
    broken = CFG.format(fixture=tmp / 'gone.png')
    try:
        preview.generate_preview(broken, str(config_path), 'a dark room')
        failures.append("missing fixture: generated, should have raised")
    except preview.PreviewError as e:
        check_true("the complaint names the missing file",
                   'gone.png' in str(e), str(e))

    try:
        preview.generate_preview(config_text, str(config_path), '   ')
        failures.append("empty scene: generated, should have raised")
    except preview.PreviewError:
        pass

    check("the scratch config never survives",
          (tmp / preview.SCRATCH_NAME).exists(), False)


if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("test_preview: all checks passed")
