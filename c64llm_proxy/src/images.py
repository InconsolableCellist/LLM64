"""Scene illustration service: generation, C64 conversion, storage.

Backends: nano-banana (Gemini) by default; the C64LLM_IMG_FIXTURE env var
substitutes a local PNG for every generation (tests, dry runs - no API
cost). Modes (config [images].mode):

  auto   - [[IMAGE: desc]] directives generate immediately (rate-limited)
  ask    - directives become a suggestion; the user triggers with /pic
  off    - directives ignored; /pic still works

Generated originals and converted blobs are stored under data/images/
and registered in the conversation's meta so the UI can list them later.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

AUTO_INTERVAL_S = 240.0


class ImageService:
    def __init__(self, data_dir: Path, mode: str = "ask"):
        self.dir = Path(data_dir) / "images"
        self.mode = mode
        self.logger = logging.getLogger(__name__)
        self._last_auto = 0.0
        self.pending_prompt = None   # last suggested-but-not-generated
        self._fixture = os.environ.get("C64LLM_IMG_FIXTURE")

    @property
    def available(self) -> bool:
        if self.mode == "off":
            return False
        if self._fixture:
            return True
        try:
            import PIL  # noqa: F401
            from . import nano_banana
            return nano_banana.key_available()
        except ImportError:
            return False

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
            "consistent. Most replies should have no image directive."
        )

    def auto_ok(self) -> bool:
        return (self.mode == "auto"
                and time.monotonic() - self._last_auto >= AUTO_INTERVAL_S)

    def mark_auto(self):
        self._last_auto = time.monotonic()

    async def generate_blob(self, prompt: str, conv_id,
                            caption: str = None) -> tuple:
        """Generate + convert. Returns (blob bytes, stem, bg color).
        The blob is multicolor: bitmap(8000) + screen(1000) + colram(1000);
        bg travels in the IMG_BEGIN frame. A caption is burned into the
        bottom of the frame. Blocking work (API call, PIL) runs off the
        event loop."""
        return await asyncio.to_thread(self._generate_sync, prompt,
                                       conv_id, caption)

    def _generate_sync(self, prompt: str, conv_id, caption=None):
        from .imaging import convert_to_c64_mc
        from PIL import Image
        import io

        if self._fixture:
            raw = Path(self._fixture).read_bytes()
        else:
            from . import nano_banana
            # Style guidance matters as much as the converter: art made
            # of flat shapes and high contrast survives 16 colors nearly
            # untouched, and image models love drawing frames unless told
            raw = nano_banana.generate(
                "Dark fantasy adventure game scene painted in a bold "
                "16-color retro palette, flat color areas, strong "
                "silhouettes, high contrast, dramatic lighting. The "
                "artwork fills the ENTIRE frame edge to edge - no "
                "borders, no frame, no letterboxing, no text. Scene: "
                + prompt, purpose="adventure")

        self.dir.mkdir(parents=True, exist_ok=True)
        stem = f"{conv_id}_{int(time.time())}"
        (self.dir / f"{stem}.png").write_bytes(raw)

        img = Image.open(io.BytesIO(raw))
        bitmap, screen, colram, bg = convert_to_c64_mc(img, caption=caption)
        blob = bitmap + screen + colram
        (self.dir / f"{stem}.blob").write_bytes(blob + bytes([bg]))
        return blob, stem, bg
