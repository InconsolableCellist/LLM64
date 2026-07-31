#!/usr/bin/env python3
"""Model discovery (the launcher's ↻ buttons): parsing, precedence,
and the key-stays-in-headers rule.

Every test runs against a scripted http.server on a random localhost
port - no real API is ever contacted.

Run: python3 tests/test_discovery.py
"""

import http.server
import json
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import discovery
from src.discovery import (DiscoveryError, comfy_model_choices,
                           gemini_image_models, openai_models)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def raises(name, fn, want_substr=""):
    try:
        fn()
    except DiscoveryError as e:
        check(name, want_substr in str(e),
              f"want {want_substr!r} in {str(e)!r}")
        return str(e)
    except Exception as e:
        check(name, False, f"raised {type(e).__name__}: {e}")
        return ""
    check(name, False, "did not raise")
    return ""


# --- scripted server (same shape as test_imagegen's) --------------------

class Stub(http.server.BaseHTTPRequestHandler):
    routes = {}
    requests = []       # (path, query dict, headers msg)
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        Stub.requests.append((parsed.path,
                              dict(urllib.parse.parse_qsl(parsed.query)),
                              self.headers))
        fn = None
        for prefix in sorted(Stub.routes, key=len, reverse=True):
            if parsed.path.startswith(prefix):
                fn = Stub.routes[prefix]
                break
        status, payload = fn(self) if fn else (404, b"{}")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass


def serve(routes):
    Stub.routes = routes
    Stub.requests = []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown


def js(obj):
    return json.dumps(obj).encode()


# --- openai-compatible /models -------------------------------------------

def test_openai_models():
    print("openai_models")
    url, stop = serve({"/models": lambda h: (200, js({"data": [
        {"id": "zephyr"}, {"id": "alpha"}, {"id": "alpha"},
        {"object": "not-a-model"}]}))})
    try:
        got = openai_models(url, "sk-SECRET")
        check("ids sorted and deduped", got == ["alpha", "zephyr"], got)
        path, query, headers = Stub.requests[0]
        check("GET <base>/models", path == "/models")
        check("key travels as a Bearer header",
              headers.get("Authorization") == "Bearer sk-SECRET")
        check("key never in the URL",
              "SECRET" not in path and "SECRET" not in str(query))
    finally:
        stop()

    url, stop = serve({"/models": lambda h: (200, js({"data": [
        {"id": "m"}]}))})
    try:
        openai_models(url + "/", "")
        path = Stub.requests[0][0]
        check("trailing slash tolerated", path == "/models")
        check("no key -> no Authorization header",
              "Authorization" not in Stub.requests[0][2])
    finally:
        stop()

    url, stop = serve({"/models": lambda h: (401, js({"error": "nope"}))})
    try:
        msg = raises("HTTP error becomes DiscoveryError",
                     lambda: openai_models(url, "sk-SECRET"), "401")
        check("error text never contains the key", "SECRET" not in msg)
    finally:
        stop()

    url, stop = serve({"/models": lambda h: (200, b"<html>")})
    try:
        raises("non-JSON rejected", lambda: openai_models(url), "non-JSON")
    finally:
        stop()

    url, stop = serve({"/models": lambda h: (200, js({"data": []}))})
    try:
        raises("empty list is an error, not a silent no-op",
               lambda: openai_models(url), "no models")
    finally:
        stop()

    raises("blank base URL rejected before any network",
           lambda: openai_models("  "), "no base URL")
    raises("unreachable endpoint", lambda: openai_models(
        "http://127.0.0.1:1"), "unreachable")


# --- gemini ---------------------------------------------------------------

def test_gemini_models():
    print("gemini_image_models")
    url, stop = serve({"/v1beta/models": lambda h: (200, js({"models": [
        {"name": "models/gemini-2.5-flash-image"},
        {"name": "models/gemini-2.5-pro"},
        {"name": "models/imagen-4.0-generate-001"}]}))})
    old = discovery.GEMINI_MODELS_URL
    discovery.GEMINI_MODELS_URL = f"{url}/v1beta/models?pageSize=1000"
    try:
        got = gemini_image_models("g-SECRET")
        check("only image models, prefix stripped",
              got == ["gemini-2.5-flash-image", "imagen-4.0-generate-001"],
              got)
        path, query, headers = Stub.requests[0]
        check("key travels in the x-goog-api-key header",
              headers.get("x-goog-api-key") == "g-SECRET")
        check("key never in the URL", "SECRET" not in str(query))
    finally:
        discovery.GEMINI_MODELS_URL = old
        stop()

    raises("no key is an error before any network",
           lambda: gemini_image_models(""), "no Gemini API key")


# --- comfyui ----------------------------------------------------------------

def comfy_object_info(known):
    """Routes for /object_info/<class>: 404 for classes not in `known`."""
    def handler(h):
        node = h.path.rsplit("/", 1)[1]
        if node not in known:
            return 404, b"{}"
        input_name, files = known[node]
        return 200, js({node: {"input": {"required": {
            input_name: [files, {}]}}}})
    return {"/object_info/": handler}


def test_comfy_models():
    print("comfy_model_choices")
    url, stop = serve(comfy_object_info({
        "UNETLoader": ("unet_name", ["flux-2-klein-9b-fp8.safetensors"]),
        "CheckpointLoaderSimple": ("ckpt_name", ["sd15.ckpt"])}))
    try:
        got = comfy_model_choices(url)
        check("union of UNET and checkpoint loaders, sorted",
              got == ["flux-2-klein-9b-fp8.safetensors", "sd15.ckpt"], got)
        raises("unknown node alone is an error (wrong URL, not an empty "
               "install)",
               lambda: comfy_model_choices(url, discovery.COMFY_CLIP_NODES),
               "none of the loader nodes")
    finally:
        stop()

    url, stop = serve(comfy_object_info({
        "CheckpointLoaderSimple": ("ckpt_name", ["sd15.ckpt"])}))
    try:
        got = comfy_model_choices(url)
        check("a 404 node class is skipped, not fatal",
              got == ["sd15.ckpt"], got)
    finally:
        stop()

    url, stop = serve(comfy_object_info({
        "VAELoader": ("vae_name", ["flux2-vae.safetensors"])}))
    try:
        got = comfy_model_choices(url, discovery.COMFY_VAE_NODES)
        check("VAE choices come from VAELoader",
              got == ["flux2-vae.safetensors"], got)
    finally:
        stop()

    raises("unreachable ComfyUI", lambda: comfy_model_choices(
        "http://127.0.0.1:1"), "unreachable")
    raises("blank URL rejected before any network",
           lambda: comfy_model_choices(""), "no ComfyUI URL")


if __name__ == "__main__":
    test_openai_models()
    test_gemini_models()
    test_comfy_models()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all discovery tests passed")
