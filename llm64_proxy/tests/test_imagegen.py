#!/usr/bin/env python3
"""Image generation backends (docs/12): protocol details and safety rules.

Every network test runs against a scripted http.server on a random
localhost port - no real API is ever contacted. The safety assertions
(size cap, keys never in exception text) are the point of this file as
much as the happy paths are.

Run: python3 tests/test_imagegen.py
"""

import base64
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import imagegen
from src.imagegen import (ComfyUIBackend, FixtureBackend, GeminiBackend,
                          ImageGenError, OpenAIImagesBackend, make_backend)

# 1x1 transparent PNG - the smallest thing a backend can plausibly return
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

failures = []
TMP = None          # tempdir for data_dir / workflow / key files


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def raises(name, fn, want_substr=""):
    try:
        fn()
    except ImageGenError as e:
        check(name, want_substr in str(e),
              f"want {want_substr!r} in {str(e)!r}")
        return str(e)
    except Exception as e:
        check(name, False, f"raised {type(e).__name__}: {e}")
        return ""
    check(name, False, "did not raise")
    return ""


# --- scripted server ---------------------------------------------------

class Stub(http.server.BaseHTTPRequestHandler):
    routes = {}         # path prefix -> fn(handler, body) -> (status, bytes)
    requests = []       # (path, query dict, body, headers msg)
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlparse(self.path)
        Stub.requests.append((parsed.path,
                              dict(urllib.parse.parse_qsl(parsed.query)),
                              body, self.headers))
        fn = None
        for prefix in sorted(Stub.routes, key=len, reverse=True):
            if parsed.path.startswith(prefix):
                fn = Stub.routes[prefix]
                break
        status, payload = fn(self, body) if fn else (404, b"{}")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass        # client hung up (expected in the size-cap test)


def serve(routes):
    """Start a stub on a random port; return (base_url, shutdown)."""
    Stub.routes = routes
    Stub.requests = []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown


def js(obj):
    return json.dumps(obj).encode()


# --- comfyui -----------------------------------------------------------

WORKFLOW = {
    "3": {"class_type": "KSampler",
          "inputs": {"seed": 42, "steps": 20, "model": ["4", 0]}},
    "6": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "flat colors, {PROMPT}", "clip": ["4", 1]}},
    "7": {"class_type": "SamplerCustom", "inputs": {"noise_seed": 7}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}


def comfy_routes(pending_rounds=0, state=None):
    """/prompt accepts, /history is pending N times then complete."""
    state = state if state is not None else {"polls": 0}

    def on_prompt(h, body):
        state["submitted"] = json.loads(body)
        return 200, js({"prompt_id": "pid-1"})

    def on_history(h, body):
        state["polls"] += 1
        if state["polls"] <= pending_rounds:
            return 200, js({})
        return 200, js({"pid-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"9": {"images": [{"filename": "out_001.png",
                                          "subfolder": "sub dir",
                                          "type": "output"}]}}}})

    def on_view(h, body):
        return 200, PNG

    return {"/prompt": on_prompt, "/history/": on_history,
            "/view": on_view}, state


def comfy_backend(url, **cfg):
    path = Path(TMP) / "workflow.json"
    path.write_text(json.dumps(WORKFLOW))
    return ComfyUIBackend({"url": url, "workflow": str(path), "timeout": 5,
                           **cfg}, TMP)


def test_comfyui():
    print("comfyui")
    routes, state = comfy_routes(pending_rounds=1)
    url, stop = serve(routes)
    try:
        be = comfy_backend(url)
        check("available (workflow parses, has token)", be.available())
        data = be.generate("a ruined tower at dusk", "adventure")
        check("returns the fetched PNG", data == PNG)

        inputs = {k: v["inputs"] for k, v in state["submitted"]["prompt"].items()}
        check("{PROMPT} substituted",
              inputs["6"]["text"] == "flat colors, a ruined tower at dusk")
        check("polled until complete", state["polls"] == 2)
        first = (inputs["3"]["seed"], inputs["7"]["noise_seed"])
        check("seed randomized", first != (42, 7), f"got {first}")

        be.generate("a ruined tower at dusk", "adventure")
        second = tuple(state["submitted"]["prompt"][n]["inputs"][k]
                       for n, k in (("3", "seed"), ("7", "noise_seed")))
        check("seeds differ between runs", first != second)

        # /view arguments come straight from the history entry
        view = [r for r in Stub.requests if r[0] == "/view"][0]
        check("view query urlencoded",
              view[1] == {"filename": "out_001.png", "subfolder": "sub dir",
                          "type": "output"}, f"got {view[1]}")

        be_fixed = comfy_backend(url, randomize_seed=False)
        be_fixed.generate("x", "adventure")
        check("randomize_seed=false leaves seeds alone",
              state["submitted"]["prompt"]["3"]["inputs"]["seed"] == 42)
    finally:
        stop()

    # never-completing history: the overall deadline must win
    routes, state = comfy_routes(pending_rounds=10 ** 6)
    url, stop = serve(routes)
    try:
        be = ComfyUIBackend({"url": url, "workflow": str(Path(TMP) / "workflow.json"),
                             "timeout": 0.5}, TMP)
        raises("poll timeout raises", lambda: be.generate("x", "adventure"),
               "timed out")
    finally:
        stop()

    # a rejected workflow keeps node errors in the log, not the message
    url, stop = serve({"/prompt": lambda h, b: (400, js(
        {"error": {"type": "prompt_outputs_failed_validation"},
         "node_errors": {"3": "missing model"}}))})
    try:
        be = comfy_backend(url)
        msg = raises("400 on submit raises", lambda: be.generate("x", "a"),
                     "rejected the workflow")
        check("node errors stay out of the message",
              "missing model" not in msg)
    finally:
        stop()


def test_comfyui_templating():
    """Every token, and the type coercion that makes them usable.

    ComfyUI validates node input types and rejects the whole graph on a
    mismatch, so "{WIDTH}" has to arrive as an int and not as "1024".
    That failure is a 400 with a validation body - easy to miss, and
    nothing else in the suite would catch it."""
    print("comfyui templating")

    TEMPLATED = {
        "_comment": ["not a node - must never be submitted"],
        "5": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": "{WIDTH}", "height": "{HEIGHT}",
                         "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "{STYLE}{PROMPT}"}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "{NEGATIVE}"}},
        "8": {"class_type": "KSampler",
              "inputs": {"seed": "{SEED}", "steps": "{STEPS}",
                         "cfg": "{CFG}", "sampler_name": "{SAMPLER}",
                         "denoise": 1, "latent_image": ["5", 0]}},
        "9": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "{MODEL}"}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }
    path = Path(TMP) / "templated.json"
    path.write_text(json.dumps(TEMPLATED))

    be = ComfyUIBackend({"url": "http://127.0.0.1:1", "workflow": str(path),
                         "width": 640, "height": 400, "steps": 12,
                         "cfg": 2.5, "style": "PIXEL ART. ",
                         "negative": "no text", "model": "m.safetensors",
                         "vars": {"unused": "x"}}, TMP)
    check("a templated workflow loads", be.available())
    g = be._prepare(be._load(), "a dark room")

    check("the comment key is not submitted", "_comment" not in g,
          sorted(g))
    check("width is an int, not a string", g["5"]["inputs"]["width"] == 640
          and isinstance(g["5"]["inputs"]["width"], int),
          repr(g["5"]["inputs"]["width"]))
    check("height too", g["5"]["inputs"]["height"] == 400
          and isinstance(g["5"]["inputs"]["height"], int))
    check("steps is an int", isinstance(g["8"]["inputs"]["steps"], int))
    check("cfg is a float", g["8"]["inputs"]["cfg"] == 2.5
          and isinstance(g["8"]["inputs"]["cfg"], float))
    check("seed is an int", isinstance(g["8"]["inputs"]["seed"], int))
    check("a string token stays a string",
          g["8"]["inputs"]["sampler_name"] == "res_multistep")
    check("style and prompt concatenate",
          g["6"]["inputs"]["text"] == "PIXEL ART. a dark room",
          g["6"]["inputs"]["text"])
    check("the negative prompt is filled",
          g["7"]["inputs"]["text"] == "no text")
    check("the model filename is filled",
          g["9"]["inputs"]["unet_name"] == "m.safetensors")
    check("untouched values survive", g["8"]["inputs"]["denoise"] == 1)
    check("node links survive", g["8"]["inputs"]["latent_image"] == ["5", 0])

    # Defaults, and the geometry that matters: 1.6:1 is the C64 frame, so
    # a default run must not letterbox.
    plain = ComfyUIBackend({"url": "http://127.0.0.1:1",
                            "workflow": str(path)}, TMP)
    gp = plain._prepare(plain._load(), "x")
    w, h = gp["5"]["inputs"]["width"], gp["5"]["inputs"]["height"]
    check("the default aspect matches the C64 frame",
          abs(w / h - 320 / 200) < 0.001, f"{w}x{h}")
    check("style defaults to empty so style_prefix owns the look",
          gp["6"]["inputs"]["text"] == "x", gp["6"]["inputs"]["text"])

    # A seed varies per run, or is pinned when asked.
    a = plain._prepare(plain._load(), "x")["8"]["inputs"]["seed"]
    b = plain._prepare(plain._load(), "x")["8"]["inputs"]["seed"]
    check("{SEED} varies between runs", a != b, f"{a} == {b}")
    pinned = ComfyUIBackend({"url": "http://127.0.0.1:1",
                             "workflow": str(path), "seed": 99}, TMP)
    check("a pinned seed is honoured",
          pinned._prepare(pinned._load(), "x")["8"]["inputs"]["seed"] == 99)

    # Junk in the config must not take the proxy down mid-adventure.
    bad = ComfyUIBackend({"url": "http://127.0.0.1:1", "workflow": str(path),
                          "width": "wide", "cfg": "loud", "seed": "soon"},
                         TMP)
    gb = bad._prepare(bad._load(), "x")
    check("a non-numeric width falls back to the default",
          gb["5"]["inputs"]["width"] == 1024, repr(gb["5"]["inputs"]["width"]))
    check("a non-numeric cfg falls back too",
          isinstance(gb["8"]["inputs"]["cfg"], float))
    check("a non-numeric seed still varies",
          isinstance(gb["8"]["inputs"]["seed"], int))

    # An untokenized workflow keeps the legacy literal-seed randomization.
    legacy = comfy_backend("http://127.0.0.1:1")
    gl = legacy._prepare(legacy._load(), "a dark room")
    check("a literal seed is still randomized", gl["3"]["inputs"]["seed"] != 42)
    check("and {PROMPT} inside a longer string still works",
          gl["6"]["inputs"]["text"] == "flat colors, a dark room")


def test_comfyui_available():
    print("comfyui availability (no network)")
    path = Path(TMP) / "wf-check.json"
    be = ComfyUIBackend({"url": "http://127.0.0.1:1", "workflow": str(path)},
                        TMP)
    check("missing file -> unavailable", not be.available())
    path.write_text("{not json")
    check("unparseable -> unavailable", not be.available())
    path.write_text(json.dumps(
        {"3": {"inputs": {"text": "no placeholder here"}}}))
    check("no {PROMPT} token -> unavailable", not be.available())
    path.write_text(json.dumps(WORKFLOW))
    check("valid workflow -> available", be.available())
    check("verdict cached by mtime", be._cache is not None)


# --- openai ------------------------------------------------------------

def test_openai():
    print("openai")
    seen = {"n": 0, "bodies": []}

    def on_gen(h, body):
        seen["n"] += 1
        seen["bodies"].append(json.loads(body))
        return 200, js({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    url, stop = serve({"/v1/images/generations": on_gen})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "sk-test",
                                  "model": "dall-e-3", "size": "1792x1024"},
                                 TMP)
        check("available with a key", be.available())
        check("unavailable without one",
              not OpenAIImagesBackend({"base_url": url}, TMP).available())
        check("b64_json decoded", be.generate("a lighthouse", "adventure") == PNG)
        sent = seen["bodies"][0]
        check("request carries model/size/n",
              (sent["model"], sent["size"], sent["n"]) == ("dall-e-3",
                                                           "1792x1024", 1))
        auth = [r for r in Stub.requests][0][3].get("Authorization")
        check("key travels in the header only", auth == "Bearer sk-test")
    finally:
        stop()

    # gpt-image-1 rejects response_format; we retry once without it
    tries = {"n": 0, "bodies": []}

    def on_gen_strict(h, body):
        tries["n"] += 1
        payload = json.loads(body)
        tries["bodies"].append(payload)
        if "response_format" in payload:
            return 400, js({"error": {
                "message": "Unknown parameter: 'response_format'."}})
        return 200, js({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    url, stop = serve({"/v1/images/generations": on_gen_strict})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "sk-test",
                                  "model": "gpt-image-1"}, TMP)
        check("response_format 400 retried", be.generate("x", "adventure") == PNG)
        check("retried exactly once", tries["n"] == 2, f"got {tries['n']}")
        check("retry dropped response_format",
              "response_format" not in tries["bodies"][1])
    finally:
        stop()

    # url-style responses get fetched
    def on_gen_url(h, body):
        return 200, js({"data": [{"url": f"{holder['url']}/img/x.png"}]})

    holder = {}
    url, stop = serve({"/v1/images/generations": on_gen_url,
                       "/img/": lambda h, b: (200, PNG)})
    holder["url"] = url
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "k"}, TMP)
        check("url response fetched", be.generate("x", "adventure") == PNG)
    finally:
        stop()

    # a non-http url must not become a file read
    url, stop = serve({"/v1/images/generations": lambda h, b: (
        200, js({"data": [{"url": "file:///etc/passwd"}]}))})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "k"}, TMP)
        raises("file:// url refused", lambda: be.generate("x", "a"), "scheme")
    finally:
        stop()


# --- gemini ------------------------------------------------------------

def test_gemini_keys():
    print("gemini key resolution")
    legacy = Path(TMP) / ".gemini.env"
    legacy.write_text("filekey\n")
    old_path, old_env = GeminiBackend.LEGACY_KEY_PATH, os.environ.pop(
        "GEMINI_API_KEY", None)
    GeminiBackend.LEGACY_KEY_PATH = legacy
    try:
        os.environ["GEMINI_API_KEY"] = "envkey"
        check("config key wins",
              GeminiBackend({"key": "cfgkey"}, TMP)._key() == "cfgkey")
        check("env beats the legacy file",
              GeminiBackend({}, TMP)._key() == "envkey")
        del os.environ["GEMINI_API_KEY"]
        check("legacy file is the last resort",
              GeminiBackend({}, TMP)._key() == "filekey")
        GeminiBackend.LEGACY_KEY_PATH = Path(TMP) / "nope.env"
        be = GeminiBackend({}, TMP)
        check("no key -> unavailable", not be.available())
        raises("no key -> generate raises",
               lambda: be.generate("x", "a"), "no Gemini API key")
    finally:
        GeminiBackend.LEGACY_KEY_PATH = old_path
        os.environ.pop("GEMINI_API_KEY", None)
        if old_env is not None:
            os.environ["GEMINI_API_KEY"] = old_env


def test_gemini_protocol():
    print("gemini protocol")
    url, stop = serve({"/v1beta/": lambda h, b: (200, js({"candidates": [
        {"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"data": base64.b64encode(PNG).decode()}}]}}]}))})
    try:
        be = GeminiBackend({"key": "AIza-secret"}, TMP)
        be.URL = url + "/v1beta/models/{model}:generateContent"
        check("inlineData decoded", be.generate("x", "adventure") == PNG)
        check("key in the x-goog header",
              Stub.requests[0][3].get("x-goog-api-key") == "AIza-secret")
        check("key never in the URL", "AIza" not in Stub.requests[0][0])
    finally:
        stop()

    url, stop = serve({"/v1beta/": lambda h, b: (200, js(
        {"promptFeedback": {"blockReason": "SAFETY"}}))})
    try:
        be = GeminiBackend({"key": "k"}, TMP)
        be.URL = url + "/v1beta/models/{model}:generateContent"
        raises("safety block surfaces", lambda: be.generate("x", "a"),
               "prompt blocked")
    finally:
        stop()


# --- safety ------------------------------------------------------------

def test_safety():
    print("safety")
    big = b"x" * (17 * 1024 * 1024)
    url, stop = serve({"/v1/images/generations": lambda h, b: (200, big)})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "k"}, TMP)
        raises("17 MB body refused", lambda: be.generate("x", "a"), "MB cap")
    finally:
        stop()

    # the server's error text reaches the log; the key never does
    url, stop = serve({"/v1/images/generations": lambda h, b: (
        401, js({"error": {"message": "Incorrect API key provided: sk-sec***"}}))})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1",
                                  "key": "sk-secret-value"}, TMP)
        msg = raises("HTTP error surfaces status",
                     lambda: be.generate("x", "a"), "HTTP 401")
        check("key absent from the message", "sk-secret-value" not in msg)
    finally:
        stop()

    url, stop = serve({"/": lambda h, b: (200, b"<html>not json</html>")})
    try:
        be = OpenAIImagesBackend({"base_url": f"{url}/v1", "key": "k"}, TMP)
        raises("non-JSON body raises", lambda: be.generate("x", "a"),
               "non-JSON")
    finally:
        stop()

    be = OpenAIImagesBackend({"base_url": "http://127.0.0.1:1/v1",
                              "key": "k"}, TMP)
    raises("unreachable endpoint raises", lambda: be.generate("x", "a"),
           "unreachable")


# --- selection ---------------------------------------------------------

def test_make_backend():
    print("make_backend")
    fixture = Path(TMP) / "scene.png"
    fixture.write_bytes(PNG)
    check("default is gemini",
          make_backend({}, TMP).name == "gemini")
    check("openai selected",
          make_backend({"backend": "openai"}, TMP).name == "openai")
    check("unknown backend -> unavailable, not a crash",
          not make_backend({"backend": "bogus"}, TMP).available())

    cfg = {"backend": "comfyui", "comfyui": {"workflow": "wf.json"}}
    be = make_backend(cfg, TMP, base_dir=TMP)
    check("relative workflow resolves against config dir",
          be.workflow_path == Path(TMP) / "wf.json")

    # The bundled Flux workflow: the default when nothing is configured,
    # and the fallback for a bare name the config dir does not have.
    be = make_backend({"backend": "comfyui"}, TMP, base_dir=TMP)
    check("unset workflow -> the bundled flux workflow, ready to run",
          be.workflow_path.name == "flux2-klein-retro.json"
          and be.available())
    cfg = {"backend": "comfyui",
           "comfyui": {"workflow": "flux2-klein-retro.json"}}
    be = make_backend(cfg, TMP, base_dir=TMP)
    check("bare name falls back to the bundled copy",
          be.workflow_path.parent.name == "workflows" and be.available())
    (Path(TMP) / "flux2-klein-retro.json").write_text("{}")
    be = make_backend(cfg, TMP, base_dir=TMP)
    check("a config-dir file of the same name wins over the bundle",
          be.workflow_path == Path(TMP) / "flux2-klein-retro.json")

    os.environ["LLM64_IMG_FIXTURE"] = str(fixture)
    try:
        be = make_backend({"backend": "openai"}, TMP)
        check("LLM64_IMG_FIXTURE overrides the configured backend",
              isinstance(be, FixtureBackend) and be.available())
        check("fixture returns the file", be.generate("x", "a") == PNG)
    finally:
        del os.environ["LLM64_IMG_FIXTURE"]

    be = make_backend({"backend": "fixture",
                       "fixture": {"path": str(fixture)}}, TMP)
    check("fixture selectable from config", be.generate("x", "a") == PNG)
    check("missing fixture file -> unavailable",
          not FixtureBackend(str(Path(TMP) / "gone.png")).available())


def test_sidecars():
    print("image sidecars (docs/13)")
    from types import SimpleNamespace
    from src.images import ImageService, build_sidecar, _backend_label

    # backend labelling: model appended when the backend carries one.
    check("fixture labelled by bare name",
          _backend_label(SimpleNamespace(name="fixture")) == "fixture")
    check("model appended when present",
          _backend_label(SimpleNamespace(name="gemini", model="g-2.5"))
          == "gemini/g-2.5")

    # build_sidecar merges the trigger meta with the service-only fields.
    sc = build_sidecar({"instructions": "the door", "directive": "",
                        "caption": "A cold hall", "conv_id": "99",
                        "at_msg": 4},
                       "PREFIX a cold hall", "a cold hall", "fixture", 1234)
    check("final_prompt carried", sc["final_prompt"] == "PREFIX a cold hall")
    check("scene (pre-prefix) carried", sc["scene"] == "a cold hall")
    check("backend recorded", sc["backend"] == "fixture")
    check("time recorded", sc["time"] == 1234)
    check("trigger meta preserved",
          sc["instructions"] == "the door" and sc["at_msg"] == 4)
    check("meta omitted still fills service fields",
          build_sidecar(None, "FP", "s", "fixture", 7)["final_prompt"] == "FP")

    # _write_sidecar writes <stem>.json beside where the image lands, and
    # final_prompt is exactly style_prefix + scene (composed in _generate_sync).
    fixture = Path(TMP) / "scene.png"
    fixture.write_bytes(PNG)
    svc = ImageService(TMP, backend=FixtureBackend(str(fixture)),
                       style_prefix="STYLE ")
    check("style prefix wired", svc.style_prefix == "STYLE ")
    (svc.dir / "conv1").mkdir(parents=True, exist_ok=True)
    scene = "footprints in the sand"
    svc._write_sidecar("conv1/1000", {"instructions": "the footprints"},
                       svc.style_prefix + scene, scene, 1000)
    path = svc.dir / "conv1" / "1000.json"
    check("sidecar file written", path.exists())
    data = json.loads(path.read_text())
    check("final_prompt equals style_prefix + scene",
          data["final_prompt"] == "STYLE footprints in the sand")
    check("backend names the fixture", data["backend"] == "fixture")
    check("json stem matches the image stem", path.stem == "1000")
    check("instructions reached the sidecar",
          data["instructions"] == "the footprints")

    # Best-effort: an OSError writing the json must not raise (an image
    # already paid for must survive a bad sidecar write).
    orig = Path.write_text

    def boom(self, *a, **k):
        if str(self).endswith(".json"):
            raise OSError("read-only")
        return orig(self, *a, **k)

    Path.write_text = boom
    try:
        svc._write_sidecar("conv1/2000", None,
                           "STYLE x", "x", 2000)   # must not raise
        check("OSError on sidecar swallowed",
              not (svc.dir / "conv1" / "2000.json").exists())
    finally:
        Path.write_text = orig


def test_style_presets():
    """[images] style = "<name>" resolution (imgstyles.py).

    apply_style folds the preset into the raw [images] table at config
    load; nothing downstream knows presets exist. So the contract to
    pin is exactly that fold: untouched when unset, prefix replaced,
    comfyui keys overridden, the LoRA workflow selected - and a typo
    warning, never a crash."""
    print("style presets (imgstyles)")
    import copy
    import logging
    from src.imgstyles import PRESETS, apply_style, resolve_style

    # No style key: the table must come back byte-identical, because
    # this is every pre-preset config in the field.
    cfg = {"mode": "ask", "backend": "comfyui",
           "comfyui": {"url": "http://x:1", "steps": 8}}
    before = copy.deepcopy(cfg)
    apply_style(cfg)
    check("style unset leaves the table untouched", cfg == before)
    cfg["style"] = ""
    apply_style(cfg)
    del cfg["style"]
    check("style empty-string is unset too", cfg == before)

    # A built-in with a LoRA: prefix set, comfyui vars injected, and
    # the bundled LoRA workflow picked because nobody named one.
    cfg = {"backend": "comfyui", "style": "cinematic", "comfyui": {}}
    apply_style(cfg)
    check("preset prefix lands in style_prefix",
          cfg.get("style_prefix") == PRESETS["cinematic"]["style_prefix"])
    comfy = cfg["comfyui"]
    check("lora preset selects the lora workflow",
          comfy.get("workflow") == "flux2-klein-lora.json",
          repr(comfy.get("workflow")))
    check("LORA var injected",
          comfy["vars"]["LORA"] == PRESETS["cinematic"]["lora"])
    check("LORA_STRENGTH is a float (exact-token typing)",
          comfy["vars"]["LORA_STRENGTH"] == 0.8
          and isinstance(comfy["vars"]["LORA_STRENGTH"], float))
    # ...and the backend built from that table runs the BUNDLED copy,
    # ready to go: {PROMPT} present, {LORA} in the loader node.
    be = make_backend({"backend": "comfyui", **cfg}, TMP, base_dir=TMP)
    check("resolved workflow is the bundled lora one",
          be.workflow_path.parent.name == "workflows" and be.available())
    g = be._prepare(be._load(), "x")
    lora_nodes = [n for n in g.values()
                  if n["class_type"] == "LoraLoaderModelOnly"]
    check("lora node filled from the preset",
          lora_nodes and lora_nodes[0]["inputs"]["lora_name"]
          == PRESETS["cinematic"]["lora"]
          and lora_nodes[0]["inputs"]["strength_model"] == 0.8)

    # A prefix-only built-in must not touch the workflow choice.
    cfg = {"style": "oil-chiaroscuro", "comfyui": {}}
    apply_style(cfg)
    check("prefix-only preset leaves the workflow alone",
          "workflow" not in cfg["comfyui"])
    check("oil-chiaroscuro prefix applied",
          cfg["style_prefix"].startswith("A dark oil painting"))

    # The preset's prefix REPLACES a configured style_prefix - picking
    # a named style is the more specific choice.
    cfg = {"style": "painted-noir", "style_prefix": "OLD "}
    apply_style(cfg)
    check("preset prefix wins over style_prefix",
          cfg["style_prefix"] == PRESETS["painted-noir"]["style_prefix"])

    # Preset comfy keys override [images.comfyui]; untouched keys stay.
    cfg = {"style": "custom", "comfyui": {"model": "old.st", "steps": 8},
           "styles": {"custom": {"style_prefix": "P: ", "model": "new.st",
                                 "sampler": "euler"}}}
    apply_style(cfg)
    check("preset model overrides the comfyui table",
          cfg["comfyui"]["model"] == "new.st")
    check("preset sampler lands too",
          cfg["comfyui"]["sampler"] == "euler")
    check("keys the preset lacks survive", cfg["comfyui"]["steps"] == 8)

    # A user table with a built-in's name overrides it key by key.
    cfg = {"style": "cinematic",
           "styles": {"cinematic": {"lora_strength": 0.5}}}
    apply_style(cfg)
    check("user table merges over the built-in",
          cfg["comfyui"]["vars"]["LORA_STRENGTH"] == 0.5
          and cfg["style_prefix"] == PRESETS["cinematic"]["style_prefix"])

    # A preset naming its own workflow keeps it; the vars still fill a
    # {LORA} token that workflow may carry.
    cfg = {"style": "custom",
           "styles": {"custom": {"style_prefix": "P: ", "lora": "l.st",
                                 "workflow": "mine.json"}}}
    apply_style(cfg)
    check("explicit preset workflow beats the lora default",
          cfg["comfyui"]["workflow"] == "mine.json")
    check("vars injected regardless",
          cfg["comfyui"]["vars"]["LORA"] == "l.st")

    # An operator's own [images.comfyui].workflow is not overridden by
    # the lora fallback either.
    cfg = {"style": "cinematic", "comfyui": {"workflow": "ops.json"}}
    apply_style(cfg)
    check("configured workflow survives a lora preset",
          cfg["comfyui"]["workflow"] == "ops.json")

    # A prefixless preset (bare-LoRA custom table) keeps the default
    # prefix path: style_prefix stays absent.
    cfg = {"style": "custom", "styles": {"custom": {"lora": "l.st"}}}
    apply_style(cfg)
    check("prefixless preset leaves style_prefix unset",
          "style_prefix" not in cfg)

    # A preset may carry its own negative - a look and the things that
    # ruin it are one decision (COMFY_KEYS).
    cfg = {"style": "anthro-illustrious", "comfyui": {"negative": "blurry"}}
    apply_style(cfg)
    check("preset negative overrides the comfyui table",
          "macro" in cfg["comfyui"]["negative"])
    check("preset sdxl sampler settings land",
          cfg["comfyui"]["cfg"] == 5.0 and cfg["comfyui"]["steps"] == 28)
    check("a prefixless-LoRA preset is not implied",
          "workflow" not in cfg["comfyui"])

    # THE regression this file exists to catch. A style_prefix rides on
    # EVERY prompt, including one for an empty room, so it may describe
    # the medium and the light and never the cast. "muzzle, fur and tail
    # visible" in the cinematic prefix put a giant beast in every
    # unpeopled crypt a furry-tuned checkpoint was asked for.
    from src.images import DEFAULT_STYLE_PREFIX
    subject_words = ("beast-person", "muzzle", "anthropomorphic", "fur and",
                     "creature", "figure", "character", "person")
    prefixes = {name: preset.get("style_prefix") or ""
                for name, preset in PRESETS.items()}
    prefixes["<default>"] = DEFAULT_STYLE_PREFIX
    for name, prefix in prefixes.items():
        offenders = [w for w in subject_words if w in prefix.lower()]
        check(f"{name} prefix names no subject", not offenders,
              f"found {offenders}")

    # Unknown name: a warning and today's behavior, never a crash.
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    logging.getLogger("src.imgstyles").addHandler(handler)
    try:
        cfg = {"style": "tpyo", "comfyui": {"url": "http://x:1"}}
        before = copy.deepcopy(cfg)
        apply_style(cfg)
        check("unknown style leaves the table untouched", cfg == before)
        check("unknown style warns",
              any(r.levelno == logging.WARNING
                  and "tpyo" in r.getMessage() for r in records))
        check("resolve reports nothing active",
              resolve_style(cfg) == (None, None))
    finally:
        logging.getLogger("src.imgstyles").removeHandler(handler)


def test_usage_log():
    print("usage accounting")
    imagegen._log_usage(TMP, "comfyui", "adventure")
    # earlier tests already appended here; the new line is the last
    line = (Path(TMP) / "images" / "usage.tsv").read_text().strip().split("\n")[-1]
    parts = line.split("\t")
    check("three tab-separated fields", len(parts) == 3, line)
    check("backend and purpose recorded",
          parts[1:] == ["comfyui", "llm64/adventure"], line)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        TMP = tmp
        # Keep tests off the dev machine's shared ~/Pictures usage log,
        # and off the real 1s poll interval (only imagegen's view of the
        # time module is swapped - the stub server keeps the real one).
        import time as _time
        from types import SimpleNamespace
        imagegen.LEGACY_DIR = Path(tmp) / "no-legacy-dir"
        imagegen.time = SimpleNamespace(monotonic=_time.monotonic,
                                        sleep=lambda s: _time.sleep(0.01))

        test_comfyui_available()
        test_comfyui_templating()
        test_comfyui()
        test_openai()
        test_gemini_keys()
        test_gemini_protocol()
        test_safety()
        test_make_backend()
        test_sidecars()
        test_style_presets()
        test_usage_log()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all image backend tests passed")
