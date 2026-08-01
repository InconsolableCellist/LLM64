"""Illustration previews: the adventure image path, run once on demand.

Pictures are the one part of config.toml you cannot judge by reading it.
What reaches the screen is a style prefix you did not write, a generator
you did not watch, and a converter that throws away all but 16 colours -
and the only way to know whether a change to any of those helped is to
look at a picture. That normally costs a whole session: boot the client,
play into a scene, type /pic, squint, edit the config, restart.

So this module runs what the server runs, one scene at a time. The same
[images] table (through Config, so a named style preset folds in exactly
as it does at boot), the same imagegen backend, the same style-prefix
precedence, the same converters. Nothing here simulates anything; the
only thing the server does that this does not is stream the result to a
client.

One generation feeds every client: the C64 render and the Windows one
are two conversions of the same PNG, and conversions are free. Only the
PROMPT is per-client, which is what `target` selects.

No tkinter in here - the UI lives in launcher.py, and this stays
testable without a display (tests/test_preview.py).
"""

import io
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Previews land under <data_dir>/previews/, never in a conversation's
# folder: they belong to no conversation, and /pics must not offer to
# re-send an experiment. Nothing here ever deletes one - they are the
# examples the operator is comparing.
PREVIEW_SUBDIR = 'previews'

# The scratch file config text is validated through. Shares the launcher's
# dotfile convention so a stray one is recognisable.
SCRATCH_NAME = '.launcher-preview.tmp.toml'

# Client targets a prompt can be written for: (key, label, what differs).
TARGETS = (
    ('c64', 'C64 (adventure default)'),
    ('win16', 'Windows 3.11 client'),
)

# Scene sentences shaped like the ones scenecomp.py composes at runtime -
# one vivid sentence ending in an explicit roster of who is present.
# Chosen to stress different parts of the converter: near-black interiors,
# skin tones at 160x200, foliage detail that dithers into mush, a sky
# gradient that bands, and one bright scene to prove the palette is not
# only good at gloom.
SAMPLE_SCENES = (
    # The first two are what a tag-mode composition produces
    # (scenecomp._tag_task, which [images] prompt_format = "tags"
    # switches on): cast tag, species, framing, place, light. Judge a
    # Danbooru-lineage checkpoint with these - a prose sample tells you
    # about a prompt shape that model will never be sent.
    ('Tags: anthro character',
     'solo, 1girl, no humans, anthro, kobold, dusty red scales, cream '
     'underbelly, chipped horn, amber eyes, maid outfit, apron, holding '
     'lantern, upper body, ruined chapel interior, split basalt altar, '
     'ivy, lantern light, deep shadow, dark fantasy'),
    ('Tags: empty room',
     'no humans, scenery, wide shot, ruined chapel interior, black '
     'basalt walls, roof open to the sky, split altar, spiral carving, '
     'ivy over broken pews, twilight, shafts of light, fog, dark '
     'fantasy'),
    ('Torchlit crypt',
     'A low vaulted crypt of cracked grey stone, a single guttering torch '
     'in an iron bracket throwing long shadows across toppled sarcophagi '
     'and a floor of dust and bone fragments, cold blue dark beyond the '
     'reach of the flame - an empty, unpeopled scene.'),
    ('Tavern interior',
     'The low beamed common room of a roadside inn, firelight and hanging '
     'lanterns picking out scarred oak tables, smoke hazing the air, rain '
     'streaking the small thick windows - the only figure present is a '
     'grey-bearded innkeeper in a stained leather apron wiping a tankard '
     'behind the bar.'),
    ('Forest road at dusk',
     'A rutted cart road winding between enormous moss-furred pines under '
     'a fading violet sky, ground mist pooling in the hollows, the last '
     'orange light catching the upper branches - an empty, unpeopled '
     'scene.'),
    ('Castle on the ridge',
     'A black basalt castle on a knife-edged ridge seen from the valley '
     'below, storm light breaking behind its towers, banners snapping, a '
     'switchback road climbing to a barbican gate - an empty, unpeopled '
     'scene.'),
    ('Face to face',
     'A close three-quarter view of a scarred half-orc mercenary in '
     'battered ring mail leaning into the lamplight, one tusk chipped, '
     'braided black hair, studying the viewer with flat yellow eyes - the '
     'only figure present is the half-orc mercenary.'),
    ('Market at noon',
     'A crowded harbour market under hard noon sun, striped awnings over '
     'stalls of fish and copperware, whitewashed walls, blue water and '
     'mast-tops beyond the quay - the figures present are anonymous '
     'shoppers and stallholders, none of them named characters.'),
)

# What adventure mode always burns into the bottom of a C64 picture
# (protocol._make_caption writes one for every illustration), so the
# preview shows the band by default rather than flattering itself.
SAMPLE_CAPTION = 'The stones remember who was buried here.'


class PreviewError(Exception):
    """A preview that stopped before an image existed. The message is
    shown to the operator, so it says what to fix - and, like every
    imagegen error, never contains a key."""


@dataclass
class Preview:
    """One generation, converted for every client it can reach."""

    scene: str = ''
    prompt: str = ''          # exactly what the backend was handed
    target: str = 'c64'       # which client's prefix wrote that prompt
    prefix_source: str = ''   # and where the prefix came from
    caption: str = ''
    backend: str = ''
    when: int = 0
    original: object = None   # PIL image: the generator's own output
    c64: object = None        # PIL image: the C64 blob rendered back
    vga: object = None        # PIL image: the Win16 DIB rendered back
    bg: int = 0               # C64 background colour index
    stem: object = None       # Path prefix of the saved files, or None
    meta: dict = field(default_factory=dict)


# --- settings -----------------------------------------------------------

def images_table(config_text):
    """The [images] table from config text, resolved the way the server
    resolves it (imgstyles.apply_style folded in). A plain dict, so the
    caller can hand it straight to imagegen.make_backend.

    Deliberately does NOT build a Config: this runs on every prompt
    refresh, and Config re-logs its startup warnings each time it is
    constructed. Config's own [images] handling is exactly these two
    lines, so the answer is the same one the server gets.
    """
    import toml
    from .imgstyles import apply_style
    try:
        parsed = toml.loads(config_text or '')
    except Exception as e:
        raise PreviewError(f'config does not parse: {e}')
    table = parsed.get('images') or {}
    if not isinstance(table, dict):
        raise PreviewError('[images] is not a table')
    apply_style(table)
    return table


def config_from_text(config_text, config_path):
    """A full Config built from editor text that may not be saved yet.

    Written to a scratch file in config.toml's OWN directory, the same
    trick validate_config_text uses and for the same reason: relative
    paths - data_dir, a workflow JSON - resolve against the config file,
    and a preview that loaded a different workflow than the server will
    is worse than no preview at all.
    """
    from .config import Config
    base = Path(config_path).resolve().parent if config_path else Path('.')
    tmp = base / SCRATCH_NAME
    try:
        tmp.write_text(config_text or '')
        return Config(str(tmp))
    except PreviewError:
        raise
    except Exception as e:
        raise PreviewError(f'config rejected: {e}')
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def data_dir_of(cfg, config_path):
    """cfg.data_dir as the running server sees it.

    The server chdirs to config.toml's directory before it starts, so a
    relative data_dir means "next to the config file". The launcher has
    no such cwd guarantee until the server has been started once, so
    resolve it explicitly rather than inheriting whatever cwd is.
    """
    d = Path(cfg.data_dir)
    if not d.is_absolute() and config_path:
        d = Path(config_path).resolve().parent / d
    return d


def resolve_prefix(images_cfg, target='c64'):
    """(style prefix, where it came from) for one client target.

    The precedence is images.ImageService._generate_sync's, spelled out:

      1. [images].style_prefix, if the config sets it at all - including
         to "" - wins for EVERY client. A named style preset has already
         landed there by way of imgstyles.apply_style, so a preset wins
         too and reports itself as one.
      2. otherwise the client profile's own art style, where it has one
         (win16 wants VGA pixel art, not an oil painting).
      3. otherwise the C64 default in images.DEFAULT_STYLE_PREFIX.
    """
    from .images import DEFAULT_STYLE_PREFIX
    from .imgstyles import resolve_style
    from .profiles import PROFILES

    cfg = images_cfg or {}
    if 'style_prefix' in cfg:
        # A preset only gets the credit if it is the one that put the
        # prefix there: a bare-LoRA preset leaves the config's own
        # style_prefix standing, and saying otherwise would send the
        # operator to edit the wrong table.
        name, preset = resolve_style(cfg)
        from_preset = name and (preset or {}).get('style_prefix') is not None
        return str(cfg['style_prefix']), (f'style preset "{name}"'
                                          if from_preset
                                          else '[images].style_prefix')
    profile = PROFILES.get(target)
    style = getattr(profile, 'image_style', None)
    if style is not None:
        return style, f'the {target} client profile'
    return DEFAULT_STYLE_PREFIX, 'the built-in default'


def compose_prompt(images_cfg, scene, target='c64'):
    """(final prompt, prefix source) for a scene sentence - what the
    backend would receive, without spending anything to find out."""
    prefix, source = resolve_prefix(images_cfg, target)
    return prefix + (scene or ''), source


def backend_status(images_cfg, cfg, config_path):
    """(backend, one-line description, reason it cannot run or None).

    available() never touches the network by contract, so this is cheap
    enough to call on every settings refresh.
    """
    from .imagegen import make_backend
    backend = make_backend(images_cfg, str(data_dir_of(cfg, config_path)),
                           getattr(cfg, 'config_dir', '.'))
    name = getattr(backend, 'name', '?')
    model = getattr(backend, 'model', None)
    label = f'{name}/{model}' if model else name
    if not backend.available():
        return backend, label, _unavailable_reason(name, images_cfg)
    return backend, label, None


def _unavailable_reason(name, images_cfg):
    """Why a backend reports unavailable, in words the operator can act
    on. available() is deliberately mute about its reasons, so this
    reconstructs the likely one from the same config it looked at."""
    sub = (images_cfg or {}).get(name) or {}
    if name == 'gemini':
        return ('the Gemini backend has no API key - set [images.gemini] '
                'key or the GEMINI_API_KEY environment variable')
    if name == 'openai':
        return ('the OpenAI-style backend has no API key - set '
                '[images.openai] key or the LLM64_IMAGES_KEY environment '
                'variable')
    if name == 'comfyui':
        return (f'the ComfyUI workflow could not be loaded - check '
                f'[images.comfyui] workflow '
                f'({sub.get("workflow") or "the bundled default"}) and '
                f'that the JSON is an API-format export')
    if name == 'fixture':
        return (f'the fixture file {sub.get("path") or "(unset)"} does '
                f'not exist')
    return (f'[images].backend = {name!r} is not a backend this proxy '
            f'implements')


# --- generating ---------------------------------------------------------

def generate_preview(config_text, config_path, scene, target='c64',
                     caption=None, save=True):
    """Generate one picture and convert it for every client.

    Returns a Preview. Raises PreviewError for anything that stopped it
    before an image existed - a config that will not load, a backend
    that is not set up, a generator that failed or returned something
    that is not an image.

    `target` picks whose style prefix goes in front of `scene`; both
    conversions run regardless, because the interesting question is
    usually how ONE generation lands on each machine.
    """
    if not (scene or '').strip():
        raise PreviewError('type a scene to illustrate first')
    try:
        from PIL import Image
    except ImportError:
        raise PreviewError('Pillow is not installed - previews (and '
                           'illustrations) need it: pip install Pillow')
    from .imagegen import ImageGenError

    cfg = config_from_text(config_text, config_path)
    images_cfg = getattr(cfg, 'images_cfg', {}) or {}
    backend, label, problem = backend_status(images_cfg, cfg, config_path)
    if problem:
        raise PreviewError(problem)

    prompt, source = compose_prompt(images_cfg, scene, target)
    logger.info(f'preview: generating one {target} image via {label}')
    try:
        raw = backend.generate(prompt, purpose='preview')
    except ImageGenError as e:
        raise PreviewError(str(e))
    try:
        original = Image.open(io.BytesIO(raw))
        original.load()
        original = original.convert('RGB')
    except Exception as e:
        raise PreviewError(f'the backend returned unreadable image data: {e}')

    caption = (caption or '').strip()
    when = int(time.time())
    prev = Preview(
        scene=scene, prompt=prompt, target=target, prefix_source=source,
        caption=caption, backend=label, when=when, original=original)
    _convert(prev, images_cfg)
    prev.meta = _build_meta(prev, images_cfg)
    if save:
        _save(prev, data_dir_of(cfg, config_path), raw)
    return prev


def _convert(prev, images_cfg):
    """Fill in the per-client renders. Each one goes out through the
    real converter and comes back through its inverse, so what is on
    screen is the blob the client would be sent, not a preview of the
    source image with a filter on it."""
    from .imaging import (convert_to_c64_mc, convert_to_dib8,
                          render_preview_mc, render_preview_dib)

    bitmap, screen, colram, bg = convert_to_c64_mc(
        prev.original, caption=prev.caption or None)
    prev.bg = bg
    prev.c64 = render_preview_mc(bitmap, screen, colram, bg)

    # [images] dib_style = "clean" is the operator's escape hatch from the
    # 1993 treatment; the preview has to honour it or it is previewing
    # someone else's settings.
    period = (images_cfg or {}).get('dib_style', 'period') != 'clean'
    dib, _, _ = convert_to_dib8(prev.original, period=period)
    prev.vga = render_preview_dib(dib)


def _build_meta(prev, images_cfg):
    """The record saved beside a preview - enough to answer "what was I
    running when this one came out well?" months later."""
    from .imgstyles import resolve_style
    name, _ = resolve_style(images_cfg)
    comfy = (images_cfg or {}).get('comfyui')
    comfy = comfy if isinstance(comfy, dict) else {}
    comfy_vars = comfy.get('vars')
    kept = ('model', 'clip', 'vae', 'steps', 'cfg', 'sampler', 'scheduler',
            'workflow', 'width', 'height', 'negative')
    return {
        'kind': 'preview',
        'scene': prev.scene,
        'prompt': prev.prompt,
        'target': prev.target,
        'prefix_source': prev.prefix_source,
        'caption': prev.caption,
        'backend': prev.backend,
        'style': name,
        'dib_style': (images_cfg or {}).get('dib_style', 'period'),
        'comfyui': {k: comfy[k] for k in kept if k in comfy},
        'vars': dict(comfy_vars) if isinstance(comfy_vars, dict) else {},
        'time': prev.when,
    }


def _free_stem(folder, when):
    """A stem no other preview owns. Two generations can land in the same
    second when a backend answers from cache."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / str(when)
    n = 2
    while stem.with_suffix('.json').exists():
        stem = folder / f'{when}-{n}'
        n += 1
    return stem


def _save(prev, data_dir, raw):
    """Write the original, both renders and the metadata. Best effort in
    the same sense imagegen's sidecar is: an image already paid for must
    not be lost to a full disk, so a write failure is logged and the
    preview is still returned - it just will not be in the history."""
    try:
        stem = _free_stem(Path(data_dir) / PREVIEW_SUBDIR, prev.when)
        stem.with_suffix('.png').write_bytes(raw)
        prev.c64.save(stem.with_name(stem.name + '-c64.png'))
        prev.vga.save(stem.with_name(stem.name + '-vga.png'))
        stem.with_suffix('.json').write_text(json.dumps(prev.meta, indent=2))
        prev.stem = stem
    except OSError as e:
        logger.warning(f'preview not saved: {e}')


# --- history ------------------------------------------------------------

def list_previews(data_dir, limit=24):
    """Saved previews, newest first, as dicts of {meta, stem, original,
    c64, vga} with Paths for whichever files survive. Unreadable or
    hand-edited JSON is skipped rather than fatal - this is a gallery,
    not a database."""
    folder = Path(data_dir) / PREVIEW_SUBDIR
    if not folder.is_dir():
        return []
    out = []
    for js in sorted(folder.glob('*.json'), reverse=True):
        try:
            meta = json.loads(js.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        stem = js.with_suffix('')
        entry = {'meta': meta, 'stem': stem}
        for key, suffix in (('original', '.png'), ('c64', '-c64.png'),
                            ('vga', '-vga.png')):
            path = (stem.with_suffix(suffix) if suffix.startswith('.')
                    else stem.with_name(stem.name + suffix))
            entry[key] = path if path.exists() else None
        out.append(entry)
        if len(out) >= limit:
            break
    return out
