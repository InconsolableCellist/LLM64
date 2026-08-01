"""Pluggable image generation backends (docs/12).

One interface, four implementations:

  gemini   - Google's gemini-2.5-flash-image ("nano banana"), the original
  openai   - anything speaking POST /v1/images/generations (OpenAI,
             Together, LocalAI, ...)
  comfyui  - a local ComfyUI queue-and-poll run of a user-authored workflow
  fixture  - a local file returned verbatim (tests, dry runs)

Rules that outlive any one backend:

- Keys travel in headers only. Never in a URL, a log line, or an
  exception message - exceptions from here reach the proxy log.
- available() never touches the network. It is consulted on every
  assistant turn, so it may only look at config and the filesystem. A
  backend that can only be validated over the wire fails at generate
  time instead, which protocol.py already reports as "Illustration
  failed."
- Every HTTP body is read through a 16 MB cap. This process is also
  holding a live C64 session; a broken endpoint must not OOM it.
- generate() runs in a worker thread (asyncio.to_thread), so blocking
  urllib calls and polling sleeps are fine. Stdlib only, no new deps.

Successful generations are accounted in usage.tsv: the legacy
~/Pictures/nano-banana/ copy if that directory already exists (the
original dev install shares it with other tools), otherwise
<data_dir>/images/usage.tsv. That directory is never created here.
"""

import base64
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .respath import bundled_workflows_dir

logger = logging.getLogger(__name__)

MAX_BYTES = 16 * 1024 * 1024
LEGACY_DIR = Path.home() / "Pictures" / "nano-banana"
PROMPT_TOKEN = "{PROMPT}"
DEFAULT_WORKFLOW = "flux2-klein-retro.json"

# Everything else a ComfyUI workflow may template, and what it defaults to.
#
# The defaults describe a Flux-2 klein run because that is what the shipped
# workflow uses, but nothing here is Flux-specific: a token only does
# anything if the workflow actually contains it, so a Stable Diffusion
# workflow that uses {STEPS} and {CFG} and ignores {SHIFT} is fine.
#
# GEOMETRY IS THE ONE WORTH READING. The C64 frame is 320x200 and
# imaging.py letterboxes into it preserving aspect, so a square
# generation loses a third of the picture to black bars before the
# dithering even starts. 1024x640 is the same 1.6:1 and fills it.
COMFY_DEFAULTS = {
    "STYLE": "",            # empty: [images].style_prefix does the styling
    "NEGATIVE": ("text, watermark, signature, letterboxing, black bars, "
                 "border, frame, photographic, blurry, low contrast"),
    "WIDTH": 1024,
    "HEIGHT": 640,
    "STEPS": 8,
    "CFG": 1.0,
    "SAMPLER": "res_multistep",
    "SCHEDULER": "simple",
    "SHIFT": 3.0,
    "MODEL": "flux-2-klein-9b-fp8.safetensors",
    "CLIP": "qwen_3_8b_fp8mixed.safetensors",
    "VAE": "flux2-vae.safetensors",
}
# Config keys are the lowercased token names, so [images.comfyui] steps = 12
# fills {STEPS}. `cfg` is the CFG scale, not the config table.
COMFY_INT_KEYS = ("WIDTH", "HEIGHT", "STEPS")
COMFY_FLOAT_KEYS = ("CFG", "SHIFT")


class ImageGenError(Exception):
    """Any backend failure. The message may reach the proxy log but never
    the C64, and must never contain a key."""


# --- shared helpers ----------------------------------------------------

def _read_capped(resp, limit=MAX_BYTES):
    """Read a response body, refusing anything over the cap."""
    chunks, total = [], 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ImageGenError(
                f"response exceeded the {limit // (1024 * 1024)} MB cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _http(req, timeout, what):
    """Perform a request; return (status, body). HTTP error bodies come
    back rather than raising, because they carry the server's error JSON
    and callers want to inspect it (the 400 retry, node validation)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", resp.getcode()), _read_capped(resp)
    except urllib.error.HTTPError as e:
        try:
            body = _read_capped(e)
        except Exception:
            body = b""
        return e.code, body
    except urllib.error.URLError as e:
        raise ImageGenError(f"{what} unreachable: {e.reason}")
    except OSError as e:        # socket timeouts, connection resets
        raise ImageGenError(f"{what} failed: {e}")


def _json_body(raw, what):
    try:
        return json.loads(raw)
    except ValueError:
        raise ImageGenError(f"{what} returned a non-JSON response")


def _error_detail(raw):
    """The server's own error text, defensively. Never echoes the request."""
    try:
        err = json.loads(raw).get("error")
    except Exception:
        return ""
    if isinstance(err, str):
        return f": {err[:300]}"
    if isinstance(err, dict):
        detail = f"{err.get('status', '')} {err.get('message', '')}".strip()
        return f": {detail[:300]}" if detail else ""
    return ""


def _log_usage(data_dir, backend_field, purpose):
    """Append one accounting line. Best effort - a failure here must not
    lose an image we already paid for."""
    try:
        if LEGACY_DIR.is_dir():
            path = LEGACY_DIR / "usage.tsv"
        else:
            path = Path(data_dir) / "images" / "usage.tsv"
            path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(path, "a") as f:
            f.write(f"{stamp}\t{backend_field}\tllm64/{purpose}\n")
    except OSError as e:
        logger.warning(f"usage log write failed: {e}")


def _fetch_image(url, what, timeout=120):
    status, raw = _http(urllib.request.Request(url), timeout, what)
    if status != 200:
        raise ImageGenError(f"{what} HTTP {status}")
    return raw


# --- backends ----------------------------------------------------------

class ImageBackend:
    name = "?"

    def available(self) -> bool:
        """Cheap, local, no network. See the module docstring."""
        return False

    def generate(self, prompt: str, purpose: str) -> bytes:
        """Raw image bytes in any format PIL can open - the converter
        letterboxes and quantizes, so size and format are free."""
        raise ImageGenError(f"backend {self.name} cannot generate")


class GeminiBackend(ImageBackend):
    """Google's image model over plain REST (no SDK)."""

    name = "gemini"
    URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "{model}:generateContent")
    LEGACY_KEY_PATH = LEGACY_DIR / ".gemini.env"

    def __init__(self, cfg, data_dir):
        self.model = cfg.get("model") or "gemini-2.5-flash-image"
        self._cfg_key = (cfg.get("key") or "").strip()
        self.data_dir = data_dir

    def _key(self):
        """config key, then env, then the original dev machine's file."""
        if self._cfg_key:
            return self._cfg_key
        env = os.environ.get("GEMINI_API_KEY", "").strip()
        if env:
            return env
        try:
            return self.LEGACY_KEY_PATH.read_text().strip()
        except OSError:
            return ""

    def available(self):
        return bool(self._key())

    def generate(self, prompt, purpose):
        key = self._key()
        if not key:
            raise ImageGenError("no Gemini API key configured")
        req = urllib.request.Request(
            self.URL.format(model=self.model),
            data=json.dumps(
                {"contents": [{"parts": [{"text": prompt}]}]}).encode(),
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": key},
        )
        status, raw = _http(req, 120, "Gemini API")
        if status != 200:
            raise ImageGenError(f"Gemini API HTTP {status}{_error_detail(raw)}")
        payload = _json_body(raw, "Gemini API")

        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ImageGenError(f"prompt blocked: {feedback['blockReason']}")
        candidates = payload.get("candidates")
        if not candidates:
            raise ImageGenError("no candidates in response")
        cand = candidates[0]
        finish = cand.get("finishReason")
        if finish not in (None, "STOP"):
            raise ImageGenError(f"generation stopped: {finish}")

        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    data = base64.b64decode(inline["data"])
                except Exception:
                    raise ImageGenError("undecodable image data in response")
                _log_usage(self.data_dir, f"gemini/{self.model}", purpose)
                return data
        raise ImageGenError("response contained no image data")


class OpenAIImagesBackend(ImageBackend):
    """Any server implementing POST /v1/images/generations."""

    name = "openai"

    def __init__(self, cfg, data_dir):
        self.base_url = (cfg.get("base_url")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = cfg.get("model") or "dall-e-3"
        self.size = cfg.get("size") or "1024x1024"
        self._cfg_key = (cfg.get("key") or "").strip()
        self.data_dir = data_dir

    def _key(self):
        return self._cfg_key or os.environ.get("LLM64_IMAGES_KEY", "").strip()

    def available(self):
        # Key only. A wrong base_url surfaces at generate time.
        return bool(self._key())

    def _post(self, payload, key):
        req = urllib.request.Request(
            f"{self.base_url}/images/generations",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
        )
        return _http(req, 300, "images API")

    def generate(self, prompt, purpose):
        key = self._key()
        if not key:
            raise ImageGenError("no images API key configured")
        payload = {"model": self.model, "prompt": prompt, "n": 1,
                   "size": self.size, "response_format": "b64_json"}
        status, raw = self._post(payload, key)
        # gpt-image-1 rejects response_format outright (it always returns
        # b64_json); retry once without it rather than making users know.
        if status == 400 and "response_format" in _error_detail(raw):
            payload.pop("response_format")
            status, raw = self._post(payload, key)
        if status != 200:
            raise ImageGenError(f"images API HTTP {status}{_error_detail(raw)}")

        data = _json_body(raw, "images API").get("data") or []
        if not data or not isinstance(data[0], dict):
            raise ImageGenError("images API returned no image")
        item = data[0]
        if item.get("b64_json"):
            try:
                out = base64.b64decode(item["b64_json"])
            except Exception:
                raise ImageGenError("undecodable base64 in response")
        elif item.get("url"):
            url = item["url"]
            if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                raise ImageGenError("image URL had an unsupported scheme")
            out = _fetch_image(url, "image download")
        else:
            raise ImageGenError("images API response contained no image")
        _log_usage(self.data_dir, f"openai/{self.model}", purpose)
        return out


class ComfyUIBackend(ImageBackend):
    """Local ComfyUI: submit the user's API-format workflow, poll, fetch.

    The workflow is a local file the user exported. {PROMPT} inside any
    node input string is where the scene description lands, and the tokens
    in COMFY_DEFAULTS may appear anywhere alongside it - prompts, sizes,
    step counts, model filenames. Substitution happens on the PARSED
    structure and never on raw JSON text, so no escaping bug is possible.

    A value that is EXACTLY one token keeps that token's type: "{WIDTH}"
    becomes the integer 1024, not the string "1024", because ComfyUI
    validates node input types and rejects the graph outright otherwise.
    A token inside a longer string is textual, which is what makes
    "{STYLE}{PROMPT}" work."""

    name = "comfyui"
    SEED_KEYS = ("seed", "noise_seed")

    def __init__(self, cfg, data_dir, base_dir="."):
        self.url = (cfg.get("url") or "http://127.0.0.1:8188").rstrip("/")
        self.workflow_path = self._resolve_workflow(
            cfg.get("workflow"), base_dir)
        self.timeout = float(cfg.get("timeout", 300))
        self.randomize_seed = bool(cfg.get("randomize_seed", True))
        self.data_dir = data_dir
        self._cache = None      # ((mtime_ns, size), workflow or None)

        # Token values: the defaults, then whatever the config overrides,
        # coerced because TOML gives a bare 8 as an int but "8" as a
        # string and ComfyUI will not accept the second.
        #
        # _explicit remembers which of them the CONFIG named, because a
        # workflow may carry its own defaults (_defaults below) and those
        # have to beat COMFY_DEFAULTS while losing to the config.
        self.values = dict(COMFY_DEFAULTS)
        self._explicit = set()
        for token, default in COMFY_DEFAULTS.items():
            if token.lower() not in cfg:
                continue
            raw = cfg[token.lower()]
            try:
                self.values[token] = self._coerce(token, raw)
                self._explicit.add(token)
            except (TypeError, ValueError):
                logger.warning("ComfyUI: [images.comfyui] %s=%r is not a "
                               "number, using %r",
                               token.lower(), raw, default)
        # A fixed seed reproduces one picture forever, which is useful for
        # comparing prompts and useless for playing. None = vary.
        self.seed = cfg.get("seed")
        if self.seed is not None:
            try:
                self.seed = int(self.seed)
            except (TypeError, ValueError):
                logger.warning("ComfyUI: [images.comfyui] seed=%r is not an "
                               "integer, varying instead", self.seed)
                self.seed = None
        # Anything else the workflow templates, for nodes this file has
        # never heard of: [images.comfyui.vars] LORA = "foo.safetensors".
        extra = cfg.get("vars") or {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                self.values[str(key).upper()] = value
                self._explicit.add(str(key).upper())

    @staticmethod
    def _coerce(token, raw):
        """A config or workflow value as the type ComfyUI validates for."""
        if token in COMFY_INT_KEYS:
            return int(raw)
        if token in COMFY_FLOAT_KEYS:
            return float(raw)
        return str(raw)

    def _workflow_defaults(self, workflow):
        """A workflow's own "_defaults" table, coerced.

        The global COMFY_DEFAULTS describe the SHIPPED Flux workflow -
        8 steps, cfg 1, a Flux checkpoint filename - which are wrong for
        any other model family, and picking a workflow in the launcher
        does not also fill in a checkpoint. So a workflow may carry the
        settings it needs to run at all:

            "_defaults": {"MODEL": "novaFurryXL_ilV120.safetensors",
                          "STEPS": 28, "CFG": 5.0}

        They sit between the global defaults and the config: selecting
        that workflow alone gives a working run, and [images.comfyui] (or
        a style preset) still overrides any of it. Like "_comment", the
        key is not a node and never reaches ComfyUI.
        """
        table = (workflow or {}).get("_defaults")
        if not isinstance(table, dict):
            return {}
        out = {}
        for key, raw in table.items():
            token = str(key).upper()
            try:
                out[token] = (self._coerce(token, raw)
                              if token in COMFY_DEFAULTS else raw)
            except (TypeError, ValueError):
                logger.warning("ComfyUI workflow %s: _defaults %s=%r is not "
                               "a number, ignoring",
                               self.workflow_path.name, key, raw)
        return out

    @staticmethod
    def _resolve_workflow(configured, base_dir):
        """The workflow file to run. A relative path resolves against
        config.toml's directory as always; if nothing is there but a
        bundled workflow has that name, the bundled copy is used - which
        is also what makes the bare default work in a frozen binary,
        where the bundle unpacks somewhere unknowable at config-write
        time. No workflow configured at all means the shipped Flux one,
        so backend = "comfyui" alone is a working setup."""
        wf = Path(os.path.expanduser(configured or DEFAULT_WORKFLOW))
        if wf.is_absolute():
            return wf
        local = Path(base_dir) / wf
        if local.exists():
            return local
        bundled = bundled_workflows_dir() / wf
        if bundled.exists():
            return bundled
        return local

    @staticmethod
    def _node_inputs(graph):
        for node in graph.values():
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                yield node["inputs"]

    def _load(self):
        """Parsed workflow, or None if missing/unparseable/tokenless.
        Cached per (mtime, size) so the per-turn available() stays free."""
        try:
            st = self.workflow_path.stat()
        except OSError:
            self._cache = None
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        if self._cache and self._cache[0] == stamp:
            return self._cache[1]

        workflow = None
        try:
            parsed = json.loads(self.workflow_path.read_text())
            if isinstance(parsed, dict) and any(
                    isinstance(v, str) and PROMPT_TOKEN in v
                    for inputs in self._node_inputs(parsed)
                    for v in inputs.values()):
                workflow = parsed
            elif isinstance(parsed, dict):
                logger.warning(f"ComfyUI workflow {self.workflow_path} has no "
                               f"{PROMPT_TOKEN} token in any node input "
                               "(exported in UI format instead of API?)")
            else:
                logger.warning(f"ComfyUI workflow {self.workflow_path} is not "
                               "a node graph")
        except (OSError, ValueError) as e:
            logger.warning(f"ComfyUI workflow {self.workflow_path}: {e}")
        self._cache = (stamp, workflow)
        return workflow

    def available(self):
        return self._load() is not None

    def _tokens(self, prompt, workflow=None):
        """{TOKEN} -> value, for this one run.

        Precedence, lowest first: COMFY_DEFAULTS, the workflow's own
        _defaults, then whatever the config named."""
        values = dict(self.values)
        for token, value in self._workflow_defaults(workflow).items():
            if token not in self._explicit:
                values[token] = value
        tokens = {"{%s}" % k: v for k, v in values.items()}
        tokens[PROMPT_TOKEN] = prompt
        tokens["{SEED}"] = (self.seed if self.seed is not None
                            else random.randrange(2 ** 31))
        return tokens

    @staticmethod
    def _sub(value, tokens):
        """One node input value with the tokens applied."""
        if not isinstance(value, str):
            return value
        # Whole value is a single token: hand back the TYPED value, so a
        # width stays an integer and passes ComfyUI's validation.
        if value in tokens:
            return tokens[value]
        for token, replacement in tokens.items():
            if token in value:
                value = value.replace(token, str(replacement))
        return value

    @staticmethod
    def _is_node(value):
        return isinstance(value, dict) and "class_type" in value

    def _prepare(self, workflow, prompt):
        # Only nodes are submitted. A workflow file is a thing a human has
        # to read and edit, so it is allowed a "_comment" key explaining
        # its tokens - and ComfyUI validates every top-level entry as a
        # node, so that key has to be dropped rather than passed on.
        graph = {k: deepcopy(v) for k, v in workflow.items()
                 if self._is_node(v)}
        tokens = self._tokens(prompt, workflow)
        seen = set()
        for inputs in self._node_inputs(graph):
            for key, value in list(inputs.items()):
                if isinstance(value, str):
                    new = self._sub(value, tokens)
                    if new != value:
                        seen.update(t for t in tokens if t in value)
                        inputs[key] = new
                        continue
                if (self.randomize_seed and key in self.SEED_KEYS
                        and isinstance(value, int)
                        and not isinstance(value, bool)):
                    # A workflow with a literal seed rather than {SEED}.
                    # Without this ComfyUI dedupes identical prompts and
                    # every /pic returns the same cached picture.
                    inputs[key] = random.randrange(2 ** 31)
        # A token that was meant to be filled and is not is silent
        # otherwise: the run succeeds and quietly ignores the setting.
        unused = [t for t in ("{STYLE}", "{NEGATIVE}", "{WIDTH}", "{HEIGHT}")
                  if t not in seen]
        if unused:
            logger.debug("ComfyUI workflow %s has no %s - those settings "
                         "do nothing for it",
                         self.workflow_path.name, ", ".join(unused))
        return graph

    def _submit(self, graph):
        req = urllib.request.Request(
            f"{self.url}/prompt",
            data=json.dumps({"prompt": graph,
                             "client_id": uuid.uuid4().hex}).encode(),
            headers={"Content-Type": "application/json"},
        )
        status, raw = _http(req, 30, "ComfyUI")
        if status != 200:
            # The body is node validation errors and holds no secrets.
            logger.error("ComfyUI rejected the workflow: HTTP %s %s", status,
                         raw[:2000].decode("utf-8", "replace"))
            raise ImageGenError(
                f"ComfyUI rejected the workflow{_reject_detail(raw)}")
        prompt_id = _json_body(raw, "ComfyUI").get("prompt_id")
        if not prompt_id:
            raise ImageGenError("ComfyUI returned no prompt_id")
        return prompt_id

    def _poll(self, prompt_id):
        deadline = time.monotonic() + self.timeout
        url = f"{self.url}/history/{urllib.parse.quote(str(prompt_id))}"
        while time.monotonic() < deadline:
            time.sleep(1.0)
            status, raw = _http(urllib.request.Request(url), 10, "ComfyUI")
            if status != 200:
                continue
            entry = _json_body(raw, "ComfyUI").get(prompt_id)
            if not entry:
                continue
            state = entry.get("status") or {}
            if state.get("status_str") == "error":
                raise ImageGenError(
                    f"ComfyUI run failed: {_comfy_error(state)}")
            if entry.get("outputs"):
                return entry
            if state.get("completed"):
                raise ImageGenError("ComfyUI run produced no outputs")
        raise ImageGenError(f"ComfyUI timed out after {self.timeout:.0f}s")

    def _fetch(self, entry):
        image = None
        for out in (entry.get("outputs") or {}).values():
            images = out.get("images") if isinstance(out, dict) else None
            if images:
                image = images[0]
                break
        if not isinstance(image, dict) or not image.get("filename"):
            raise ImageGenError("ComfyUI run produced no image output")
        query = urllib.parse.urlencode({
            "filename": image.get("filename", ""),
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        })
        return _fetch_image(f"{self.url}/view?{query}", "ComfyUI image fetch",
                            timeout=60)

    def generate(self, prompt, purpose):
        workflow = self._load()
        if workflow is None:
            raise ImageGenError(
                f"ComfyUI workflow {self.workflow_path} is missing, "
                f"unparseable, or has no {PROMPT_TOKEN} token")
        data = self._fetch(self._poll(self._submit(
            self._prepare(workflow, prompt))))
        _log_usage(self.data_dir, "comfyui", purpose)
        return data


def _reject_detail(raw):
    """The first node validation complaint from a rejected /prompt, as a
    clause to hang off "ComfyUI rejected the workflow".

    Worth digging out because this is the error an operator can actually
    fix - a checkpoint filename that is not on that machine, a sampler
    that does not exist - and "see log" is a poor thing to show in a
    preview dialog. Returns "" if the body is not what we expect; the
    full response is in the log either way.
    """
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
        for node_id, node in (body.get("node_errors") or {}).items():
            for err in (node.get("errors") or []):
                detail = err.get("details") or err.get("message") or ""
                if detail:
                    return (f": node {node_id} "
                            f"({node.get('class_type', '?')}) "
                            f"{detail[:300]}")
        message = (body.get("error") or {}).get("message")
        if message:
            return f": {str(message)[:300]}"
    except (ValueError, AttributeError, TypeError):
        pass
    return " (see log)"


def _comfy_error(state):
    """One line out of ComfyUI's status.messages structure."""
    try:
        for kind, payload in state.get("messages") or []:
            if "error" in str(kind):
                return str(payload)[:300]
    except Exception:
        pass
    return state.get("status_str", "unknown error")


class FixtureBackend(ImageBackend):
    """A local file, returned as-is. LLM64_IMG_FIXTURE forces this."""

    name = "fixture"

    def __init__(self, path):
        self.path = Path(os.path.expanduser(path)) if path else None

    def available(self):
        return bool(self.path) and self.path.is_file()

    def generate(self, prompt, purpose):
        if not self.path:
            raise ImageGenError("no fixture path configured")
        try:
            return self.path.read_bytes()
        except OSError as e:
            raise ImageGenError(f"fixture {self.path}: {e.strerror}")


class UnknownBackend(ImageBackend):
    """Named in config but not implemented. Reports unavailable rather
    than taking the proxy down over a typo."""

    def __init__(self, name):
        self.name = name

    def available(self):
        return False


BACKENDS = ("gemini", "openai", "comfyui", "fixture")


def make_backend(images_cfg, data_dir, base_dir="."):
    """Build the configured backend. images_cfg is the raw [images] table;
    base_dir is config.toml's directory (relative paths resolve there)."""
    cfg = images_cfg or {}
    fixture = os.environ.get("LLM64_IMG_FIXTURE")
    if fixture:
        return FixtureBackend(fixture)

    name = str(cfg.get("backend") or "gemini").strip().lower()
    sub = cfg.get(name) or {}
    if name == "gemini":
        return GeminiBackend(sub, data_dir)
    if name == "openai":
        return OpenAIImagesBackend(sub, data_dir)
    if name == "comfyui":
        return ComfyUIBackend(sub, data_dir, base_dir)
    if name == "fixture":
        return FixtureBackend(sub.get("path"))
    logger.error(f"unknown [images].backend {name!r} "
                 f"(known: {', '.join(BACKENDS)}) - images disabled")
    return UnknownBackend(name)
