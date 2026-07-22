# 12 — Pluggable image generation backends

**Status: IMPLEMENTED** (`c64llm_proxy/src/imagegen.py`,
`tests/test_imagegen.py`). Everything below is what the code does, except
where a note says otherwise. The user guide at the bottom is the live
reference for configuring a backend.

Two things landed differently from the spec above:

- **Storage is per conversation.** Originals and blobs go to
  `<data_dir>/images/<conv_id>/<epoch>.{png,blob}` — one folder per
  conversation, mirroring `conversations/<id>.json` — and the stem
  recorded in the conversation meta is `"<conv_id>/<epoch>"`. Old flat
  stems (`<conv_id>_<epoch>`) still resolve through
  `ImageService.blob_path()`; a blob that is genuinely gone already
  reports "That picture's data is gone." Nothing outside `data_dir` is
  written on a fresh install.
- **`protocol.py` has a second one-line change** beyond building the
  backend: `_resend_pic` calls `images.blob_path(stem)` instead of
  assembling the path itself, so the layout stays owned by `images.py`.

Read the whole thing before changing any of it — the safety rules and the
"available() must be cheap" constraint are load-bearing.

Goal: scene illustrations currently come from exactly one place —
Google's `gemini-2.5-flash-image` ("nano banana") with an API key read
from a path specific to the original developer's machine. We want the
generator behind a small interface so that:

1. **ComfyUI** works (the immediate want: local Stable Diffusion / FLUX,
   no per-image API cost, full style control via a user-authored workflow).
2. **Any OpenAI-compatible images endpoint** works (the shippable generic
   option: OpenAI itself, Together AI, LocalAI, and other gateways that
   implement `POST /v1/images/generations`).
3. The existing Gemini path keeps working **with zero config changes**
   for the current install.

## How it works today (read before changing)

Two files, one clean seam:

- **`c64llm_proxy/src/nano_banana.py`** — the entire Gemini client.
  `generate(prompt, purpose) -> bytes` returns raw PNG/JPEG bytes or
  raises `NanoBananaError`; `key_available() -> bool` checks the key
  file. The key lives as a bare string in
  `~/Pictures/nano-banana/.gemini.env`, is sent only in the
  `x-goog-api-key` header, and is deliberately never interpolated into
  URLs or error messages. Successful generations append a line to
  `~/Pictures/nano-banana/usage.tsv` (that file is shared with other
  tools on the dev machine — leave it alone for legacy installs).

- **`c64llm_proxy/src/images.py`** — `ImageService`, the only consumer.
  `available` checks mode ≠ off, PIL importable, and
  `nano_banana.key_available()`. `_generate_sync()` wraps the scene
  description in a hardcoded dark-fantasy style prefix, calls
  `nano_banana.generate()`, then hands the bytes to
  `imaging.convert_to_c64_mc()` (which accepts anything PIL can open,
  letterboxes to 320x200, and quantizes to the C64 palette — backends do
  NOT need to produce any particular size or format). The
  `C64LLM_IMG_FIXTURE` env var substitutes a local PNG for every
  generation; the e2e suite depends on this.

`protocol.py` never touches `nano_banana` directly — it only uses
`ImageService.available`, `.generate_blob()`, `.mode`, `.auto_ok()`,
`.pending_prompt`, `.prompt_snippet()`. All errors from generation are
caught in `_generate_and_send_image()` and surfaced to the C64 as the
generic "Illustration failed." with detail only in the proxy log. Keep
both properties of that design.

**Critical constraint:** `ImageService.available` is consulted on every
assistant turn (directive filtering), on every `/pic`, and when building
the system prompt. It must therefore **never do network I/O** — file
existence and config checks only. A backend that can only be validated
over the network validates lazily, at generate time.

## Design

One new module, `c64llm_proxy/src/imagegen.py`, replaces
`nano_banana.py` and holds everything:

```python
class ImageGenError(Exception):
    """Any backend failure. The message may reach the proxy log but
    never the C64 and must never contain a key."""

class ImageBackend:
    name = "?"
    def available(self) -> bool: ...          # NO network I/O
    def generate(self, prompt: str, purpose: str) -> bytes: ...
        # returns raw image bytes (anything PIL opens); raises ImageGenError

def make_backend(images_cfg: dict, data_dir: Path) -> ImageBackend:
    """images_cfg is the raw [images] table from config.toml."""
```

`generate()` is called from a worker thread (`asyncio.to_thread` in
`ImageService.generate_blob`), so blocking `urllib` calls and polling
loops are fine — no async plumbing needed. Stay on stdlib `urllib` like
the current code; do not add dependencies.

### Config schema

```toml
[images]
mode = "ask"              # auto | ask | off        (existing key, unchanged)
backend = "gemini"        # gemini | openai | comfyui | fixture
# Style wrapper prepended to every scene description before it reaches
# the backend. Default is the current dark-fantasy text (see images.py).
# ComfyUI users whose workflow already carries the style set this to "".
# style_prefix = "..."

[images.gemini]
model = "gemini-2.5-flash-image"
# key = "..."             # else GEMINI_API_KEY env, else legacy
                          # ~/Pictures/nano-banana/.gemini.env file

[images.openai]
base_url = "https://api.openai.com/v1"
model = "dall-e-3"
size = "1024x1024"        # landscape (e.g. 1792x1024 / 1536x1024) fits the
                          # C64's 320x200 frame better when the model has it
# key = "..."             # else C64LLM_IMAGES_KEY env

[images.comfyui]
url = "http://127.0.0.1:8188"
workflow = "./comfyui_workflow_api.json"   # relative to config.toml's dir
timeout = 300             # seconds; local GPUs can be slow
randomize_seed = true
```

Precedence follows the project convention: env var beats config file.
Defaults must reproduce today's behavior exactly: `backend = "gemini"`,
key from the legacy file, current style prefix. `C64LLM_IMG_FIXTURE`
(env) **forces** the fixture backend regardless of config — the test
suite relies on this.

### Backend: gemini (port of nano_banana.py)

Move the existing code nearly verbatim. Changes:

- Key resolution order: `[images.gemini].key` → `GEMINI_API_KEY` env →
  legacy `~/Pictures/nano-banana/.gemini.env` file. `available()` is
  true if any source yields a non-empty string.
- `model` configurable, default `gemini-2.5-flash-image`.
- Usage log: if `~/Pictures/nano-banana/` already exists (legacy dev
  install), keep appending to `usage.tsv` there, same format. Otherwise
  log to `<data_dir>/images/usage.tsv` as
  `ISO-timestamp<TAB>gemini/<model><TAB>c64llm/<purpose>`. Never create
  the `~/Pictures` directory on a fresh install.
- Rename `NanoBananaError` → `ImageGenError`.

### Backend: openai (generic, shippable)

`POST {base_url}/images/generations` with
`Authorization: Bearer <key>`:

```json
{"model": "...", "prompt": "...", "n": 1, "size": "...",
 "response_format": "b64_json"}
```

Caveats an implementer must handle:

- `gpt-image-1` **rejects** `response_format` (it always returns
  b64_json). On an HTTP 400 whose error message mentions
  `response_format`, retry once without that key.
- Response: `data[0].b64_json` → base64-decode and return. If instead
  `data[0].url` is present, GET it (size-capped, see safety) and return
  the bytes.
- Timeout 300s (gpt-image-1 can take >60s). Map HTTP errors to
  `ImageGenError` with status + the server's `error.message`, exactly
  like the Gemini code does — parse defensively, never echo the request.

`available()`: key present (config or `C64LLM_IMAGES_KEY`). Nothing
else — a wrong base_url surfaces at generate time as
"Illustration failed." plus a logged error, which is acceptable.

### Backend: comfyui

ComfyUI exposes a queue-and-poll HTTP API (no auth, see safety):

1. **Load** the user's workflow JSON (exported in *API format*, not the
   UI format — see the user guide below) at first use; cache the parsed
   dict. The file must contain the literal token `{PROMPT}` inside at
   least one string input value.
2. **Substitute**: deep-copy the dict, walk every node's `inputs`, and
   in every *string* value replace the token `{PROMPT}` with the scene
   prompt. Substitute on the parsed structure, never on raw JSON text —
   that sidesteps all escaping bugs.
3. **Randomize seeds** (when `randomize_seed`, default true): every
   input named `seed` or `noise_seed` holding an int gets
   `random.randrange(2**31)`. (KSampler uses `seed`; SamplerCustom uses
   `noise_seed`.) Without this, ComfyUI dedupes identical prompts and
   every identical `/pic` returns the cached image.
4. **Submit**: `POST {url}/prompt` with
   `{"prompt": <workflow>, "client_id": "<uuid4>"}` →
   `{"prompt_id": "..."}`. A 400 here carries node validation errors —
   log the response body (it contains no secrets), raise
   `ImageGenError("ComfyUI rejected the workflow (see log)")`.
5. **Poll**: `GET {url}/history/{prompt_id}` every 1s until the
   response object contains the prompt_id key, or `timeout` expires.
   If the entry's `status.status_str` is `"error"`, raise with the
   node error summary.
6. **Fetch**: from the entry's `outputs`, take the first node output
   containing an `images` list; for its first element GET
   `{url}/view?filename=...&subfolder=...&type=...`
   (urlencode all three, they come straight from the history entry) and
   return the bytes.

`available()`: workflow file exists, parses as JSON, and contains
`{PROMPT}` in some string input. Cache the verdict per mtime so the
per-turn calls stay free. **No network probe** — an unreachable ComfyUI
surfaces at generate time.

Usage log: `<data_dir>/images/usage.tsv`, backend field `comfyui`.

### Backend: fixture

Promote the current `C64LLM_IMG_FIXTURE` special case into a proper
backend that returns the file's bytes. `make_backend` returns it
whenever the env var is set, regardless of `[images].backend`; it is
also selectable explicitly (`backend = "fixture"` +
`[images.fixture].path`) for dry runs without env vars.

### ImageService changes

- Constructor grows a `backend` parameter (built once in `protocol.py`
  via `make_backend(config.images_cfg, Path(config.data_dir))`).
- `available` = mode ≠ off, PIL importable, `backend.available()`.
- `_generate_sync` calls `self.backend.generate(styled_prompt,
  purpose="adventure")`; the style prefix comes from
  `[images].style_prefix` (default: the exact current hardcoded text —
  move it, don't retype it).
- Startup log line gains the backend name:
  `Images enabled (backend: comfyui, mode: ask)`.

### config.py changes

Add `self.images_cfg = config.get('images', {})` (raw dict, handed to
`make_backend`) next to the existing `images_mode` parsing.
`config.toml.example` currently has **no `[images]` section at all** —
add the full commented schema above as part of this work.

## Safety rules (non-negotiable)

- **Keys travel only in headers** (`x-goog-api-key`,
  `Authorization: Bearer`), never in URLs, never in exception messages,
  never in logs. The existing Gemini code models this — copy its error
  handling style.
- **Errors to the C64 stay generic.** `protocol.py` already catches
  everything and sends "Illustration failed." — don't add any path that
  forwards backend error text to the client.
- **Cap response sizes.** Read HTTP bodies with a bounded loop and
  refuse anything over 16 MB (pre-base64-decode for JSON payloads,
  raw for image downloads). A hostile or broken endpoint must not OOM
  the proxy that is also holding a live C64 session.
- **Validate that bytes are an image** by letting PIL open them (the
  converter already does; make sure a PIL failure becomes
  `ImageGenError`, not an unhandled exception — it is inside the
  catch-all today, keep it that way after refactoring).
- **Timeouts on every HTTP call**, including each poll request (10s
  connect/read per poll, `timeout` overall deadline for the ComfyUI
  loop; 120s Gemini as today; 300s OpenAI).
- **`available()` never touches the network** (rationale above).
- **Never fetch workflow files from the network.** The ComfyUI workflow
  is a local file the user placed; treat its path like config.
- ComfyUI itself has **no authentication** and its API can execute
  arbitrary workflows on the host GPU box — the user guide below must
  (and does) tell users to keep it on localhost or a trusted
  LAN/tailnet, never the open internet. The proxy is a client, not a
  mitigation.

## Implementation steps (in order)

1. Create `src/imagegen.py`: `ImageGenError`, `ImageBackend`,
   `GeminiBackend` (moved from `nano_banana.py`), `OpenAIImagesBackend`,
   `ComfyUIBackend`, `FixtureBackend`, `make_backend()`, shared helpers
   (`_http_json`, `_read_capped`, `_log_usage`).
2. `config.py`: add `images_cfg`. `config.toml.example`: add the
   `[images]` schema.
3. `images.py`: inject the backend; move the style prefix to config with
   the current text as default; route `C64LLM_IMG_FIXTURE` through
   `FixtureBackend`.
4. `protocol.py:165`: build the backend, pass it in, extend the startup
   log line. (This should be the only protocol.py change.)
5. Delete `nano_banana.py`; `grep -rn nano_banana src/ tests/ ../emu` and
   fix any stragglers (`emu/test_e2e.py` may reference it).
6. Tests (see below), then run the full suite and the e2e suite.
7. Deploy note: config lives per-host; mlboy's `config.toml` is NOT
   rsynced (deploy copies `src/` only), so mlboy keeps working on the
   gemini default untouched.

## Test plan / acceptance criteria

- **Unit — comfyui**: stand up `http.server` on a random localhost port
  inside the test, scripted to answer `/prompt` (capture the submitted
  workflow), `/history/<id>` (pending once, then complete), and `/view`
  (serve a tiny PNG). Assert: `{PROMPT}` was substituted, seeds were
  randomized (two runs differ), returned bytes are the PNG, poll timeout
  raises `ImageGenError`.
- **Unit — openai**: same stub pattern; assert b64_json decode, the
  `response_format` 400-retry, and the URL-fetch fallback.
- **Unit — gemini**: key resolution order (config beats env beats file);
  no network test needed beyond what exists.
- **Unit — safety**: a stub serving a >16 MB body raises; assert no
  configured key substring appears in any raised message.
- **Regression**: entire existing suite passes; `C64LLM_IMG_FIXTURE`
  still short-circuits everything (test_directives.py and the e2e suite
  must run unmodified).
- **Acceptance**: with no config changes on the dev machine, `/pic`
  still generates via Gemini from the legacy key file; with
  `backend = "comfyui"` pointed at a live ComfyUI, `/pic` produces a
  converted multicolor image on the emulator.

---

# User guide: configuring an image backend

Illustrations are optional. With no backend configured the proxy runs
normally and simply reports images as unavailable. All settings live in
`c64llm_proxy/config.toml` under `[images]`; restart the proxy after
editing and look for the startup log line
`Images enabled (backend: ..., mode: ...)`.

Common to every backend:

```toml
[images]
mode = "ask"          # "ask": model suggests, you confirm with /pic
                      # "auto": scenes illustrate themselves (rate-limited)
                      # "off": never
backend = "gemini"    # pick one below
```

## Option A — Gemini "nano banana" (hosted, easiest)

1. Get an API key at https://aistudio.google.com/apikey (image output
   is a paid feature — check current pricing).
2. Provide the key **one** of these ways:
   - `key = "..."` under `[images.gemini]`, or
   - `GEMINI_API_KEY` in the proxy's environment (recommended — keeps
     secrets out of the config file).
3. Set `backend = "gemini"`. Done — the model default is fine.

## Option B — OpenAI-compatible endpoint (hosted or local)

Works with any server implementing `POST /v1/images/generations`:
OpenAI (`dall-e-3`, `gpt-image-1`), Together AI (FLUX models), LocalAI,
and similar gateways.

1. Set `backend = "openai"` and fill in:

   ```toml
   [images.openai]
   base_url = "https://api.openai.com/v1"
   model = "dall-e-3"
   size = "1792x1024"      # landscape suits the C64 screen; use a size
                           # your model actually supports (1024x1024 is
                           # universally safe)
   ```

2. Key: `key = "..."` there, or the `C64LLM_IMAGES_KEY` env var.
3. Sanity-check the endpoint from the proxy host before blaming the C64:

   ```sh
   curl -sS $BASE/images/generations \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model":"dall-e-3","prompt":"a lighthouse","size":"1024x1024","response_format":"b64_json"}' \
     | head -c 300
   ```

## Option C — ComfyUI (local, free per image, full style control)

You bring a working ComfyUI and a workflow that already produces images
you like; the proxy just feeds it scene descriptions.

1. **Build the look in ComfyUI first.** Any model (SDXL, FLUX, SD1.5).
   A landscape resolution near the C64's shape works best — e.g.
   1024x640 or 832x512. Bold flat-color styles survive the 16-color
   conversion far better than photorealism; put that in your prompt
   ("retro game art, flat colors, high contrast, strong silhouettes").
2. **Insert the placeholder.** In your positive-prompt text box, put the
   literal token `{PROMPT}` where the scene description should go:

   ```
   dark fantasy retro game art, flat colors, high contrast, {PROMPT}
   ```

   The proxy also prepends its own style text; if your workflow fully
   owns the style, set `style_prefix = ""` under `[images]`.
3. **Export in API format** (a plain "Save"/"Export" will NOT work —
   the proxy needs the API graph):
   - Current UI: menu **Workflow → Export (API)**.
   - Classic UI: Settings (gear) → enable **Dev mode Options**, then a
     **Save (API Format)** button appears in the menu.
4. Save the JSON next to `config.toml` (e.g.
   `comfyui_workflow_api.json`) and configure:

   ```toml
   [images]
   mode = "ask"
   backend = "comfyui"

   [images.comfyui]
   url = "http://127.0.0.1:8188"
   workflow = "./comfyui_workflow_api.json"
   timeout = 300           # raise on slow GPUs
   randomize_seed = true   # leave on, or identical prompts return
                           # ComfyUI's cached previous image
   ```

5. **Security:** ComfyUI's API has **no authentication** — anyone who
   can reach the port can run arbitrary workflows on your GPU box. Keep
   it bound to `127.0.0.1`, or reach it over a trusted LAN or tailnet
   address. Never port-forward it to the internet.
6. Verify connectivity from the proxy host:
   `curl -sS http://127.0.0.1:8188/system_stats | head -c 200`
7. Restart the proxy, start an adventure, `/pic a ruined tower at dusk`.

## Troubleshooting (all backends)

- **"Images unavailable" on the C64** — mode is `off`, PIL isn't
  installed (`pip install Pillow`), or the backend failed its check: no
  key (gemini/openai) or missing/invalid workflow file / no `{PROMPT}`
  token (comfyui). The startup log says which.
- **"Illustration failed."** — the C64 message is deliberately terse;
  the real error (HTTP status, ComfyUI node errors, timeout) is in the
  proxy log. On mlboy: `~/c64llm_proxy/proxy-live.log`.
- **Same image every time (ComfyUI)** — seeds aren't being randomized:
  `randomize_seed` is off, or your sampler's seed input has a
  non-standard name. Rename it, or accept fixed seeds.
- **Pictures look muddy after conversion** — a style problem, not a
  backend problem: push the prompt/workflow toward flat colors and high
  contrast (see docs/09 and the style prefix in `images.py`).
