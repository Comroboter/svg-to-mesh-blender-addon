"""Optional AI depth suggestions through the Claude API (Anthropic).

This module only uses the Python standard library: Blender add-ons cannot
rely on third-party packages (the official ``anthropic`` SDK needs compiled,
platform-specific dependencies), so the Messages API is called over HTTPS
with ``urllib``.  Nothing in here imports ``bpy``.
"""

import base64
import json
import socket
import ssl
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"

# (model id, label, description) - Claude Opus 5.5 is the default
MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5", "Best results (default)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "Faster and cheaper"),
    ("claude-haiku-4-5", "Claude Haiku 4.5", "Fastest and cheapest"),
]

# Value ranges the suggestions are clamped to (JSON schema in structured
# outputs does not support numeric limits, so they are enforced here).
HEIGHT_RANGE = (0.1, 8.0)
BASE_RANGE = (-4.0, 8.0)

SYSTEM_PROMPT = """You help turn flat 2D artwork (logos, icons, emblems) into 3D relief models in Blender.

Every colored region of the artwork is a separate extruded mesh object. For each region you choose:
- height: its thickness in multiples of the base depth D (1.0 = D),
- base: where its bottom sits, in multiples of D (0.0 = on the ground plane).
Its top is therefore at (base + height) * D.

Aim for a 3D version that reads well and looks intentional:
- backgrounds, frames and large plates usually sit low and are the thickest foundation,
- the main subject and text stand out above what surrounds them,
- small details (highlights, snow caps, eyes, icons) sit slightly above the region they lie on,
- things that read as cut into a surface (rivers, grooves, engraved lines) end a little below its top,
- keep the steps between neighbouring layers moderate (about 0.3 to 1.5 D) unless the design needs drama.

Regions lying inside another region should usually end above or below that region's top, never at exactly the same height.
Use the preview image to understand what the regions depict. Give one entry for every region id, and keep each reason short (under 15 words)."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "height": {"type": "number"},
                    "base": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "height", "base", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["regions", "summary"],
    "additionalProperties": False,
}


class AIError(Exception):
    """A user-facing error message (no traceback needed)."""


def build_request(regions, png_bytes, hint="", model=DEFAULT_MODEL):
    """Return (headers_without_key, body) for the Messages API.

    regions: list of dicts with at least ``id``, ``color`` and ``area_percent``
    (plus optional ``name``, ``bbox_px``, ``center_px``, ``layer``).
    """
    lines = [
        "The preview image shows the artwork from above (gray = empty space). "
        "Pixel coordinates start at the top left. Regions (layer = paint order, 0 = bottom):",
        json.dumps(regions, indent=1, sort_keys=True),
    ]
    if hint.strip():
        lines.append("What the user wants to make: " + hint.strip())
    lines.append("Suggest height and base for every region.")
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png_bytes).decode("ascii")},
        },
        {"type": "text", "text": "\n".join(lines)},
    ]
    body = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
    }
    headers = {"content-type": "application/json", "anthropic-version": API_VERSION}
    if model.startswith(("claude-opus-5", "claude-sonnet-5")):
        # effort is not supported on Haiku 4.5; refusal fallback only exists on the 5.x models
        body["output_config"]["effort"] = "medium"
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    return headers, body


def parse_response(data, region_ids):
    """Validate an API response and return (suggestions, summary).

    suggestions: {region id: (height, base, reason)}, clamped to sane ranges.
    """
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AIError("The AI declined this request.")
    if stop == "max_tokens":
        raise AIError("The AI answer was cut off - please try again.")
    text = next((b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), "")
    try:
        result = json.loads(text)
    except ValueError:
        raise AIError("The AI answer could not be read.") from None
    out = {}
    for entry in result.get("regions", []):
        try:
            rid = int(entry["id"])
            height = float(entry["height"])
            base = float(entry["base"])
        except (KeyError, TypeError, ValueError):
            continue
        if rid not in region_ids:
            continue
        height = min(max(height, HEIGHT_RANGE[0]), HEIGHT_RANGE[1])
        base = min(max(base, BASE_RANGE[0]), BASE_RANGE[1])
        out[rid] = (height, base, str(entry.get("reason", ""))[:200])
    if not out:
        raise AIError("The AI answer did not contain any usable heights.")
    return out, str(result.get("summary", ""))[:500]


def _ssl_context():
    """System certificates plus certifi's (bundled with Blender) when available.

    Using both covers systems without a usable CA store as well as company
    proxies whose certificate is only installed system-wide.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi is optional
        pass
    return ctx


_STATUS_MESSAGES = {
    400: "The request was rejected",
    401: "The API key is invalid",
    403: "The API key is not allowed to use this model",
    404: "The selected model is not available for this API key",
    413: "The request is too large",
    429: "Rate limit reached - wait a moment and try again",
    529: "The API is overloaded - try again in a minute",
}


def call_api(api_key, headers, body, timeout=180, retries=2, sleep=time.sleep):
    """POST to the Messages API and return the decoded JSON response.

    Retries rate limits (429), overload (529), server errors (5xx) and
    connection problems with a short backoff.
    """
    data = json.dumps(body).encode("utf-8")
    hdrs = dict(headers)
    hdrs["x-api-key"] = api_key
    attempt = 0
    while True:
        req = urllib.request.Request(API_URL, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            status = ex.code
            try:
                detail = json.loads(ex.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                detail = ""
            retryable = status in (408, 409, 429, 529) or status >= 500
            if retryable and attempt < retries:
                attempt += 1
                try:
                    delay = float(ex.headers.get("retry-after", "") or 2 ** attempt)
                except ValueError:
                    delay = 2 ** attempt
                sleep(min(delay, 20.0))
                continue
            msg = _STATUS_MESSAGES.get(status, "The API returned an error (HTTP %d)" % status)
            if status == 401:
                detail = ""  # the API message only repeats ours
            raise AIError(msg + (": " + detail if detail else "")) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as ex:
            if attempt < retries:
                attempt += 1
                sleep(2 ** attempt)
                continue
            reason = getattr(ex, "reason", ex)
            raise AIError("Could not reach the API: %s" % reason) from None
