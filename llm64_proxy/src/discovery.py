"""Model discovery for the launcher's dropdowns.

Small blocking HTTP lookups that turn "what models does this endpoint
serve?" into a list of strings for a combobox:

  openai_models         - GET <base_url>/models (OpenAI-compatible; also
                          llama.cpp, Ollama, vLLM, LM Studio, Together...)
  gemini_image_models   - Google's ListModels, filtered to image models
  comfy_model_choices   - ComfyUI GET /object_info/<node>: the valid
                          filenames for a loader input ARE the node's
                          declared choices, so this asks per node class
                          rather than downloading the multi-MB full
                          /object_info dump

Rules, inherited from imagegen.py:

- Keys travel in headers only. Never in a URL, a log line, or an
  exception message.
- Bodies are read through a cap; a broken endpoint must not OOM the
  launcher.
- Everything here BLOCKS (urllib + stdlib only). Callers own the
  threading; the launcher runs these off the UI thread.
"""

import json
import urllib.error
import urllib.request

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 10
GEMINI_MODELS_URL = ("https://generativelanguage.googleapis.com/"
                     "v1beta/models?pageSize=1000")

# Loader node -> input whose declared choices are the installed files.
# UNETLoader covers Flux-style split checkpoints, CheckpointLoaderSimple
# the classic all-in-one ones; the launcher offers the union.
COMFY_MODEL_NODES = (("UNETLoader", "unet_name"),
                     ("CheckpointLoaderSimple", "ckpt_name"))
COMFY_CLIP_NODES = (("CLIPLoader", "clip_name"),)
COMFY_VAE_NODES = (("VAELoader", "vae_name"),)


class DiscoveryError(Exception):
    """Endpoint unreachable or talking something unexpected. The message
    is shown to the user and must never contain a key."""


def _get_json(url, headers=None, what="endpoint"):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise DiscoveryError(f"{what}: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise DiscoveryError(f"{what} unreachable: {e.reason}")
    except OSError as e:
        raise DiscoveryError(f"{what} failed: {e}")
    if len(raw) > MAX_BYTES:
        raise DiscoveryError(f"{what}: response over the size cap")
    try:
        return json.loads(raw)
    except ValueError:
        raise DiscoveryError(f"{what} returned a non-JSON response")


def openai_models(base_url, key=""):
    """Model ids from an OpenAI-compatible GET /models, sorted."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise DiscoveryError("no base URL configured")
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = _get_json(f"{base}/models", headers, "models endpoint")
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise DiscoveryError("models endpoint: no model list in response")
    ids = sorted({str(m["id"]) for m in data
                  if isinstance(m, dict) and m.get("id")})
    if not ids:
        raise DiscoveryError("models endpoint listed no models")
    return ids


def gemini_image_models(key):
    """Gemini models that generate images, e.g. gemini-2.5-flash-image."""
    if not key:
        raise DiscoveryError("no Gemini API key configured")
    payload = _get_json(GEMINI_MODELS_URL, {"x-goog-api-key": key},
                        "Gemini API")
    names = []
    for m in payload.get("models") or []:
        name = str(m.get("name", "")).removeprefix("models/")
        # The reliable signal is the name: image generators carry
        # "image" in it, while supportedGenerationMethods varies
        # between "generateContent" and "predict" across families.
        if name and "image" in name:
            names.append(name)
    if not names:
        raise DiscoveryError("Gemini API listed no image models")
    return sorted(set(names))


def _comfy_choices(url, node_class, input_name):
    """The declared choices of one loader input, [] if the node or the
    choice list is not there (older ComfyUI, custom trims)."""
    base = (url or "").strip().rstrip("/")
    if not base:
        raise DiscoveryError("no ComfyUI URL configured")
    payload = _get_json(f"{base}/object_info/{node_class}", {}, "ComfyUI")
    try:
        spec = payload[node_class]["input"]
        for group in ("required", "optional"):
            entry = (spec.get(group) or {}).get(input_name)
            if entry and isinstance(entry[0], list):
                return [str(v) for v in entry[0]]
    except (KeyError, TypeError, IndexError):
        pass
    return []


def comfy_model_choices(url, nodes=COMFY_MODEL_NODES):
    """Union of the loader choices across `nodes`, sorted. A node class
    the server does not know is skipped; only nothing-at-all is an
    error, because that means the wrong URL, not an empty install."""
    found, reachable = set(), False
    for node_class, input_name in nodes:
        try:
            found.update(_comfy_choices(url, node_class, input_name))
            reachable = True
        except DiscoveryError as e:
            if "HTTP" not in str(e):
                raise           # unreachable/garbage: stop, don't mask
    if not reachable:
        raise DiscoveryError("ComfyUI knows none of the loader nodes asked")
    if not found:
        raise DiscoveryError("ComfyUI lists no files for those loaders")
    return sorted(found)
