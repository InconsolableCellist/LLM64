"""Named image style presets: [images] style = "cinematic".

A preset is one table of settings - a style_prefix plus optional
ComfyUI overrides (model, sampler, a LoRA) - selected by name, so a
user can switch the whole look of the illustrations without editing
workflow JSON. Three presets ship built in (PRESETS below); a config
can add its own or override a built-in with [images.styles.<name>].

Resolution runs ONCE, at config load (config.py calls apply_style on
the raw [images] table), and works by folding the preset into the keys
the rest of the proxy already reads:

  style_prefix       -> [images].style_prefix (ImageService reads it),
                        REPLACING the profile/default prefix - picking
                        a named style is the more specific choice
  prompt_format      -> [images].prompt_format: 'prose' (default) or
                        'tags', which changes what the COMPOSITION step
                        writes (scenecomp.py). A tag-trained checkpoint
                        wants tags all the way up the chain, not a
                        sentence with tags glued to the front
  model, clip, vae, steps, cfg, sampler, scheduler, width, height
                     -> [images.comfyui].<key>, overriding what the
                        table had (only meaningful for that backend).
                        Geometry is in the list because a checkpoint
                        family has a native one: SDXL wants ~1 MP and
                        Flux does not care.
  workflow           -> [images.comfyui].workflow
  lora, lora_strength-> [images.comfyui.vars] LORA / LORA_STRENGTH,
                        and - when neither the preset nor the config
                        names a workflow - the bundled LoRA workflow
                        (flux2-klein-lora.json), which is the retro
                        one plus a LoraLoaderModelOnly node

Nothing downstream (images.py, imagegen.py, protocol.py) knows presets
exist, which is the point: `style` unset leaves the table untouched
and today's behavior exact. An unknown style name warns and falls back
to the same - a typo must not take the proxy down.
"""

import logging

logger = logging.getLogger(__name__)

# The bundled retro workflow with a LoRA node spliced in. Used when a
# preset carries a lora and nobody named a workflow; a preset (or the
# config) that names its own workflow keeps it, and the LORA vars are
# still injected so a custom workflow's {LORA} token fills too.
LORA_WORKFLOW = "flux2-klein-lora.json"

# Preset keys that map 1:1 onto [images.comfyui] settings (which map
# 1:1 onto workflow tokens - see imagegen.COMFY_DEFAULTS).
COMFY_KEYS = ("model", "clip", "vae", "steps", "cfg",
              "sampler", "scheduler", "width", "height")

# Built-in presets. A user [images.styles.<name>] table with one of
# these names is merged OVER the built-in, so overriding just
# lora_strength keeps the prefix.
PRESETS = {
    "cinematic": {
        # "snout", never "muzzle": an operator who points this prose
        # prefix at an e621-lineage checkpoint gets the restraint, not
        # the anatomy - a fox in a literal leather muzzle.
        "style_prefix": (
            "A moody cinematic film still, anamorphic framing, "
            "practical light sources, volumetric haze, muted grade "
            "with deep blacks. The subject characters keep their "
            "described species and anatomy exactly - an "
            "anthropomorphic beast-person stays a beast-person, "
            "snout, fur and tail visible, never rendered as a "
            "human. Scene: "),
        "lora": "MovieClips_Klein9B_copy_000000880.safetensors",
        "lora_strength": 0.8,
    },
    "oil-chiaroscuro": {
        "style_prefix": (
            "A dark oil painting in heavy chiaroscuro, in the manner "
            "of a Rembrandt night scene: one warm light source, deep "
            "umber shadow, visible brushwork, muted earth palette "
            "with a single cold accent. Scene: "),
    },
    "painted-noir": {
        "style_prefix": (
            "A painted dark fantasy scene lit like film noir: hard "
            "single-source light carving the figure out of "
            "near-black, long shadows, fog catching the beam, "
            "restrained cold palette with one warm highlight. "
            "Scene: "),
    },
    # The anthro answer. A general-purpose model draws a beast-person by
    # drawing a person and hoping; an e621-trained SDXL checkpoint draws
    # the muzzle, the digitigrade legs and the fur because that is most
    # of what it has ever seen. So this preset is a whole stack - its own
    # workflow (SDXL: one checkpoint, no Flux VAE or CLIP), the
    # checkpoint, and the sampler settings that family wants, since
    # COMFY_DEFAULTS are Flux's and would run this at 8 steps and cfg 1.
    #
    # Tags, not prose, the whole way down: an Illustrious-lineage model
    # was trained on Danbooru/e621 tag strings, so this preset also
    # switches the COMPOSITION step to writing tags (prompt_format).
    # Half-measures do not work here - a tag prefix in front of a prose
    # sentence gave a second figure in a shot asked for `solo` and put
    # the subject on the horizon, because prose has no `solo` and no
    # `upper body`.
    #
    # NO SPECIES CLAUSE, deliberately - and this is the one thing not to
    # "fix" by adding it back. The other presets carry a sentence telling
    # the model an anthro stays an anthro, because they run models that
    # would not otherwise. Here it is both unnecessary and harmful: an
    # A/B on fixed seeds had "anthro, muzzle, fur, ears and tail, never a
    # human face" in the prefix produce a beast in an explicitly
    # unpeopled castle scene, and drag two different scenes to nearly the
    # same picture. The prefix competes with the scene for a fixed CLIP
    # budget, and this checkpoint needs no convincing to draw fur.
    #
    # The flatness tags are the C64's, not taste: flat color, thick
    # outlines and a limited palette survive 16 colors and a 160x200
    # grid; the airbrushed detail "absurdres" buys turns to dither mush.
    #
    # This checkpoint knows the whole e621 rating range. Nothing here
    # asks for or blocks any of it; put "rating_safe, " at the front of
    # the prefix, or "nsfw" in [images.comfyui] negative, if a given
    # table wants that decided rather than left to the scene.
    "nova-furry": {
        # The quality/aesthetic tags this lineage was trained with, then
        # the source and medium, then the flatness the C64 needs. What
        # is NOT here: cast, species, framing and setting. Those are
        # per-scene and come from the composition step, which writes
        # tags too (prompt_format below) - a fixed prefix cannot know
        # whether this shot has two characters in it or nobody.
        "style_prefix": (
            "masterpiece, best quality, amazing quality, very aesthetic, "
            "newest, source_furry, dark fantasy adventure game "
            "illustration, flat color, cel shading, thick outlines, "
            "limited palette, strong silhouettes, high value contrast, "
            "one dominant light source, "),
        "prompt_format": "tags",
        # Checkpoint, sampler, steps, cfg and geometry are NOT here.
        # They live in the workflow's own "_defaults" table, one layer
        # further down, because a preset key overrides [images.comfyui]
        # outright: with them here, an operator who selected this preset
        # would find the launcher's Steps and Width fields doing
        # nothing. From _defaults they still make the workflow work on
        # its own, and the config can still tune them.
        "workflow": "novafurryxl.json",
    },
}


def resolve_style(images_cfg):
    """The active preset as (name, merged table), or (None, None).

    User [images.styles.*] tables merge over a built-in of the same
    name; a name known to neither warns and resolves to nothing, so a
    typo means today's default look rather than no proxy."""
    cfg = images_cfg or {}
    name = str(cfg.get("style") or "").strip()
    if not name:
        return None, None
    user_tables = cfg.get("styles")
    if not isinstance(user_tables, dict):
        user_tables = {}
    user = user_tables.get(name)
    base = PRESETS.get(name)
    if base is None and not isinstance(user, dict):
        known = sorted(set(PRESETS) | {k for k, v in user_tables.items()
                                       if isinstance(v, dict)})
        logger.warning("[images] style = %r is not a known preset "
                       "(known: %s) - using the default style instead",
                       name, ", ".join(known))
        return None, None
    merged = dict(base or {})
    if isinstance(user, dict):
        merged.update(user)
    return name, merged


def apply_style(images_cfg):
    """Fold the selected preset into the [images] table, in place.

    Mutates images_cfg (a plain dict from toml.load) so that the
    existing consumers - ImageService via style_prefix, make_backend
    via the comfyui table - see the resolved values without knowing a
    preset was involved. No style selected = no mutation at all."""
    name, preset = resolve_style(images_cfg)
    if not preset:
        return

    prefix = preset.get("style_prefix")
    if prefix is None:
        # Legal for a bare-LoRA custom table: keep the default prefix
        # and let the model/lora keys do the styling.
        logger.info("style %r has no style_prefix - keeping the "
                    "default prefix", name)
    else:
        if "style_prefix" in images_cfg \
                and images_cfg["style_prefix"] != str(prefix):
            logger.info("style %r overrides the configured "
                        "[images].style_prefix", name)
        images_cfg["style_prefix"] = str(prefix)

    # How the scene itself should be written. Belongs to the preset
    # because it belongs to the checkpoint: see scenecomp._tag_task.
    if preset.get("prompt_format"):
        images_cfg["prompt_format"] = str(preset["prompt_format"])

    comfy = images_cfg.get("comfyui")
    if not isinstance(comfy, dict):
        comfy = {}
        images_cfg["comfyui"] = comfy
    for key in COMFY_KEYS:
        if key in preset:
            comfy[key] = preset[key]

    lora = preset.get("lora")
    if preset.get("workflow"):
        comfy["workflow"] = str(preset["workflow"])
    elif lora and not comfy.get("workflow"):
        # imagegen resolves the bare name to the bundled copy.
        comfy["workflow"] = LORA_WORKFLOW
    if lora:
        vars_ = comfy.get("vars")
        if not isinstance(vars_, dict):
            vars_ = {}
            comfy["vars"] = vars_
        vars_["LORA"] = str(lora)
        try:
            # A float on purpose: strength_model is a float input, and
            # a value that is exactly one token keeps its Python type.
            vars_["LORA_STRENGTH"] = float(preset.get("lora_strength", 1.0))
        except (TypeError, ValueError):
            logger.warning("style %r: lora_strength=%r is not a number, "
                           "using 1.0", name, preset.get("lora_strength"))
            vars_["LORA_STRENGTH"] = 1.0
    logger.info("image style preset %r active", name)
