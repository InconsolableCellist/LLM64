"""Gemini image generation client ("nano banana").

Calls gemini-2.5-flash-image over the plain REST API (stdlib urllib, no
SDK). The API key lives as a bare string in ~/Pictures/nano-banana/.gemini.env
and is sent in a request header, never in a URL or an error message.
Every successful generation is appended to ~/Pictures/nano-banana/usage.tsv
(tab-separated: ISO timestamp, model, label).
"""

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

MODEL = "gemini-2.5-flash-image"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
KEY_PATH = Path.home() / "Pictures" / "nano-banana" / ".gemini.env"
USAGE_PATH = Path.home() / "Pictures" / "nano-banana" / "usage.tsv"


class NanoBananaError(Exception):
    """API-level failure: HTTP error, quota, safety block, or empty result."""


def key_available():
    try:
        return bool(KEY_PATH.read_text().strip())
    except OSError:
        return False


def _read_key():
    try:
        key = KEY_PATH.read_text().strip()
    except OSError as e:
        raise NanoBananaError(f"cannot read API key file {KEY_PATH}: {e.strerror}")
    if not key:
        raise NanoBananaError(f"API key file {KEY_PATH} is empty")
    return key


def _log_usage(purpose):
    stamp = datetime.now().isoformat(timespec="seconds")
    with open(USAGE_PATH, "a") as f:
        f.write(f"{stamp}\t{MODEL}\tc64llm/{purpose}\n")


def generate(prompt, purpose):
    """Generate one image; return raw PNG/JPEG bytes.

    purpose is a short label recorded in usage.tsv as c64llm/<purpose>.
    """
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": _read_key(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err = json.loads(e.read()).get("error", {})
            detail = f": {err.get('status', '')} {err.get('message', '')}".rstrip()
        except Exception:
            pass
        raise NanoBananaError(f"Gemini API HTTP {e.code}{detail}")
    except urllib.error.URLError as e:
        raise NanoBananaError(f"Gemini API unreachable: {e.reason}")

    feedback = payload.get("promptFeedback", {})
    if feedback.get("blockReason"):
        raise NanoBananaError(f"prompt blocked: {feedback['blockReason']}")
    candidates = payload.get("candidates")
    if not candidates:
        raise NanoBananaError("no candidates in response")
    cand = candidates[0]
    finish = cand.get("finishReason")
    if finish not in (None, "STOP"):
        raise NanoBananaError(f"generation stopped: {finish}")

    for part in cand.get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            data = base64.b64decode(inline["data"])
            _log_usage(purpose)
            return data
    raise NanoBananaError("response contained no image data")
