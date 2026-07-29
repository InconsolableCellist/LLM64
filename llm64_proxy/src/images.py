"""Scene illustration service: generation, C64 conversion, storage.

The generator itself lives behind imagegen.make_backend() - Gemini,
an OpenAI-compatible endpoint, ComfyUI, or a fixture file (docs/12).
This module owns everything around it: the style wrapper, the C64
conversion, and where the results land. Modes (config [images].mode):

  auto   - [[IMAGE: desc]] directives generate immediately (rate-limited)
  ask    - directives become a suggestion; the user triggers with /pic
  off    - directives ignored; /pic still works

Originals and converted blobs are stored per conversation under
data/images/<conv_id>/<epoch>.{png,blob}, and the stem
("<conv_id>/<epoch>") is registered in the conversation's meta so /pics
can re-stream one without paying for it twice.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

AUTO_INTERVAL_S = 240.0


def _backend_label(backend) -> str:
    """How the sidecar names the generator: 'gemini/gemini-2.5-flash-image'
    where the backend carries a model, otherwise the bare name."""
    name = getattr(backend, 'name', '?')
    model = getattr(backend, 'model', None)
    return f"{name}/{model}" if model else name


def build_sidecar(meta: dict, final_prompt: str, scene: str,
                  backend_label: str, when: int) -> dict:
    """The record written beside every generated image (docs/13). Caller
    supplies the trigger context in `meta` (instructions, directive,
    caption, conv_id, at_msg); this fills in what only the service knows.
    Kept pure so it is testable without PIL or a backend."""
    out = dict(meta or {})
    out.update({
        'final_prompt': final_prompt,   # exactly what the backend received
        'scene': scene,                 # composed sentence, pre-prefix
        'backend': backend_label,
        'time': when,
    })
    return out

# Style guidance matters as much as the converter: art made of flat
# shapes and high contrast survives 16 colors nearly untouched, and image
# models love drawing frames unless told not to. Overridable via
# [images].style_prefix - ComfyUI workflows that own their look set "".
DEFAULT_STYLE_PREFIX = (
    "Dark fantasy adventure game scene painted in a bold 16-color retro "
    "palette, flat color areas, strong silhouettes, high contrast, "
    "dramatic lighting. The artwork fills the ENTIRE frame edge to edge "
    "- no borders, no frame, no letterboxing, no text. Scene: ")


class ImageService:
    def __init__(self, data_dir: Path, mode: str = "ask", backend=None,
                 style_prefix: str = None):
        self.dir = Path(data_dir) / "images"
        self.mode = mode
        self.backend = backend
        # None means "unset" (use the default); "" is a real choice.
        # An operator who set one keeps it for EVERY client - it beats
        # the per-profile style, so _configured has to remember which
        # case this was.
        self._configured = style_prefix is not None
        self.style_prefix = (DEFAULT_STYLE_PREFIX if style_prefix is None
                             else style_prefix)
        self.logger = logging.getLogger(__name__)
        self._last_auto = 0.0
        self.pending_prompt = None   # last suggested-but-not-generated

    @property
    def available(self) -> bool:
        if self.mode == "off" or self.backend is None:
            return False
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False
        return self.backend.available()

    def blob_path(self, stem: str) -> Path:
        """Where a registered picture's blob lives. Old flat stems
        (conv_epoch) still resolve; new ones carry their folder."""
        return self.dir / f"{stem}.blob"

    async def dib_from_stem(self, stem: str) -> tuple:
        """The retained original PNG as (packed 8-bit DIB, w, h) for a
        CAP_DIB_IMAGES client - one generation serves both machines,
        each converted to what it can display. Raises OSError if the
        PNG is gone (the caller falls back to the C64 blob, which is
        registered pictures' one guaranteed artifact)."""
        return await asyncio.to_thread(self._dib_sync, stem)

    def _dib_sync(self, stem: str):
        from .imaging import convert_to_dib8
        from PIL import Image
        with Image.open(self.dir / f"{stem}.png") as img:
            return convert_to_dib8(img)

    def prompt_snippet(self) -> str:
        """Instruction block appended to the adventure system prompt."""
        return (
            "\nScene illustrations: when the player enters a visually "
            "striking NEW scene, you may output [[IMAGE: one-sentence "
            "visual description of the scene]] on its own at the START of "
            "that reply - TWO square brackets each side, not one. "
            "Use it rarely - major locations and dramatic "
            "moments only, never two scenes in a row. Describe characters "
            "and places with their established appearance (clothing, hair, "
            "architecture, lighting) so successive illustrations stay "
            "consistent. Treat the directive as a SUGGESTION for the shot: "
            "name only who and what is actually present in the scene, no "
            "one else. Most replies should have no image directive."
        )

    def auto_ok(self) -> bool:
        return (self.mode == "auto"
                and time.monotonic() - self._last_auto >= AUTO_INTERVAL_S)

    def mark_auto(self):
        self._last_auto = time.monotonic()

    async def generate_blob(self, prompt: str, conv_id,
                            caption: str = None, meta: dict = None,
                            style: str = None) -> tuple:
        """Generate + convert. Returns (blob bytes, stem, bg color).
        The blob is multicolor: bitmap(8000) + screen(1000) + colram(1000);
        bg travels in the IMG_BEGIN frame. A caption is burned into the
        bottom of the frame. `meta` is the trigger context saved in the
        JSON sidecar (docs/13). `style` is the requesting profile's art
        prompt (profiles.py); an explicit config style_prefix outranks
        it, absence of both means the C64 default. Blocking work (API
        call, PIL) runs off the event loop."""
        return await asyncio.to_thread(self._generate_sync, prompt,
                                       conv_id, caption, meta, style)

    def _generate_sync(self, prompt: str, conv_id, caption=None, meta=None,
                       style=None):
        from .imaging import convert_to_c64_mc
        from .imagegen import ImageGenError
        from PIL import Image
        import io

        prefix = self.style_prefix if self._configured or style is None \
            else style
        final_prompt = prefix + prompt
        raw = self.backend.generate(final_prompt, purpose="adventure")

        # One folder per conversation, matching conversations/<id>.json.
        folder = self.dir / str(conv_id).replace("/", "_")
        folder.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        stem = f"{folder.name}/{now}"
        (self.dir / f"{stem}.png").write_bytes(raw)

        # Save what produced this image beside it (docs/13).
        self._write_sidecar(stem, meta, final_prompt, prompt, now)

        try:
            img = Image.open(io.BytesIO(raw))
        except Exception as e:
            # Whatever the backend handed back, it wasn't an image.
            raise ImageGenError(f"backend returned unreadable image data: {e}")
        bitmap, screen, colram, bg = convert_to_c64_mc(img, caption=caption)
        blob = bitmap + screen + colram
        (self.dir / f"{stem}.blob").write_bytes(blob + bytes([bg]))
        return blob, stem, bg

    def _write_sidecar(self, stem, meta, final_prompt, scene, when):
        """Write <stem>.json beside the image (docs/13). Best-effort: a
        failed sidecar must never lose an image already paid for - the
        same policy _log_usage follows."""
        sidecar = build_sidecar(meta, final_prompt, scene,
                                _backend_label(self.backend), when)
        try:
            (self.dir / f"{stem}.json").write_text(
                json.dumps(sidecar, indent=2))
        except OSError as e:
            self.logger.warning("sidecar write failed for %s: %s", stem, e)
