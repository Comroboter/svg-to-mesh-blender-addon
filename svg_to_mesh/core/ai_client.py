"""Optional AI depth suggestions: Claude (Anthropic), OpenAI or compatible
servers (LM Studio, OpenRouter ...), or a local Ollama.

This module only uses the Python standard library: Blender add-ons cannot
rely on third-party packages (the official SDKs need compiled,
platform-specific dependencies), so the APIs are called over HTTP(S) with
``urllib``.  Nothing in here imports ``bpy``.
"""

import base64
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"

# (model id, label, description) - Claude Opus 5.5 is the default. The costs
# are estimates for a typical request (two small preview images, ~20 regions).
MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5", "Best judgement for illustrations (about 3-8 cents per request)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "Very good for most logos (about 2-4 cents per request)"),
    ("claude-haiku-4-5", "Claude Haiku 4.5", "Simple logos, fastest (about 0.5-1 cent per request)"),
]

# (id, label, description)
PROVIDERS = [
    ("ANTHROPIC", "Claude (Anthropic)", "Anthropic API, needs an API key"),
    ("OPENAI", "OpenAI or compatible", "OpenAI API, or any OpenAI-compatible server such as LM Studio or OpenRouter"),
    ("OLLAMA", "Ollama (local)", "Free, runs on your computer; needs a vision model such as gemma3 or qwen2.5vl"),
]
DEFAULT_URLS = {"OPENAI": "https://api.openai.com/v1", "OLLAMA": "http://localhost:11434"}
DEFAULT_MODELS = {"ANTHROPIC": DEFAULT_MODEL, "OPENAI": "gpt-5-mini", "OLLAMA": "gemma3"}

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
Several regions can have the same color: they are separate parts of that color (for example a mustache and the outlines). Judge every part on its own.
Use the preview images to understand what the regions depict. Give one entry for every region id, and keep each reason short (under 15 words)."""

SYSTEM_PROMPT_FLAT = """You help turn flat 2D artwork (logos, icons, emblems) into 3D relief models in Blender.

Every colored region of the artwork is a separate extruded mesh object, and all of them stand on the same ground plane: their bottoms are always at 0. For each region you only choose its height: its thickness in multiples of the base depth D (1.0 = D). A thicker region sticks out further, a thinner one looks recessed.

Aim for a 3D version that reads well and looks intentional:
- backgrounds, frames and large plates are a solid but moderate foundation,
- the main subject and text are thicker than what surrounds them,
- small details (highlights, eyes, snow caps, icons) are a bit thicker than the region around them,
- things that read as cut into a surface (rivers, grooves, engraved lines, shadows) are thinner than their surroundings,
- keep the steps between neighbouring regions moderate (about 0.3 to 1.5 D) unless the design needs drama.

Regions lying inside another region should be thicker or thinner than it, never exactly the same.
Several regions can have the same color: they are separate parts of that color (for example a mustache and the outlines). Judge every part on its own.
Use the preview images to understand what the regions depict. Give one entry for every region id, and keep each reason short (under 15 words)."""

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


RESPONSE_SCHEMA_FLAT = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "height": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "height", "reason"],
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


@dataclass
class Request:
    provider: str
    url: str
    body: dict
    headers: dict = field(default_factory=dict)


def _prompt_text(regions, hint, with_map, flat_bottom=True):
    lines = [
        "The first image shows the artwork from above (gray = empty space). "
        "Pixel coordinates start at the top left.",
    ]
    if with_map:
        lines.append("The second image is a region map: every region is filled with its own flat color, "
                     "given as map_color below.")
    lines += [
        "Regions (layer = paint order, 0 = bottom):",
        json.dumps(regions, indent=1, sort_keys=True),
    ]
    if hint.strip():
        lines.append("What the user wants to make: " + hint.strip())
    lines.append("Suggest the height (thickness) of every region." if flat_bottom
                 else "Suggest height and base for every region.")
    return "\n".join(lines)


def _b64(png):
    return base64.b64encode(png).decode("ascii")


def build_request(regions, images, hint="", model=None, provider="ANTHROPIC", base_url="", flat_bottom=True):
    """Return a Request (without credentials) for the chosen provider.

    regions: list of dicts with at least ``id``, ``color`` and ``area_percent``
    (plus optional ``name``, ``bbox_px``, ``layer``, ``map_color``).
    images: PNG bytes of the preview, optionally followed by the region map.
    flat_bottom: all regions start on the ground plane, only their thickness
    is chosen (otherwise the AI may also lift or sink regions).
    """
    if isinstance(images, (bytes, bytearray)):
        images = [images]
    text = _prompt_text(regions, hint, len(images) > 1, flat_bottom)
    system = SYSTEM_PROMPT_FLAT if flat_bottom else SYSTEM_PROMPT
    schema = RESPONSE_SCHEMA_FLAT if flat_bottom else RESPONSE_SCHEMA
    model = model or DEFAULT_MODELS.get(provider, "")
    base = (base_url or DEFAULT_URLS.get(provider, "")).rstrip("/")
    headers = {"content-type": "application/json"}

    if provider == "OPENAI":
        content = [{"type": "text", "text": text}]
        content += [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(png)}} for png in images]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "depth_suggestions", "strict": True, "schema": schema}},
        }
        # the official API wants max_completion_tokens, most compatible servers max_tokens
        official = urllib.parse.urlsplit(base).hostname == "api.openai.com"
        body["max_completion_tokens" if official else "max_tokens"] = 16000
        return Request(provider, base + "/chat/completions", body, headers)

    if provider == "OLLAMA":
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text, "images": [_b64(png) for png in images]},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2},
        }
        return Request(provider, base + "/api/chat", body, headers)

    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(png)}}
               for png in images]
    content.append({"type": "text", "text": text})
    body = {
        "model": model,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    headers["anthropic-version"] = API_VERSION
    if model.startswith(("claude-opus-5", "claude-sonnet-5")):
        # effort is not supported on Haiku 4.5; refusal fallback only exists on the 5.x models
        body["output_config"]["effort"] = "medium"
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    return Request("ANTHROPIC", API_URL, body, headers)


def response_text(data, provider="ANTHROPIC"):
    """The JSON text of a response; raises AIError for refusals and cut-off answers."""
    if provider == "OPENAI":
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise AIError("The AI declined this request.")
        if choice.get("finish_reason") == "length":
            raise AIError("The AI answer was cut off - please try again.")
        return message.get("content") or ""
    if provider == "OLLAMA":
        if data.get("done_reason") == "length":
            raise AIError("The AI answer was cut off - please try again.")
        return (data.get("message") or {}).get("content") or ""
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise AIError("The AI declined this request.")
    if stop == "max_tokens":
        raise AIError("The AI answer was cut off - please try again.")
    return next((b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), "")


def parse_response(data, region_ids, provider="ANTHROPIC", flat_bottom=False):
    """Validate an API response and return (suggestions, summary).

    suggestions: {region id: (height, base, reason)}, clamped to sane ranges;
    base is 0 with *flat_bottom* (or when the answer has none).
    """
    text = response_text(data, provider).strip()
    if text.startswith("```"):  # some local models wrap JSON in a code fence
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError
    except ValueError:
        raise AIError("The AI answer could not be read.") from None
    out = {}
    for entry in result.get("regions", []):
        try:
            rid = int(entry["id"])
            height = float(entry["height"])
            base = 0.0 if flat_bottom else float(entry.get("base", 0.0))
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


def _error_detail(ex):
    try:
        err = json.loads(ex.read().decode("utf-8")).get("error", "")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(err, dict):
        err = err.get("message", "")
    return str(err)[:300]


def call_api(request, api_key="", timeout=None, retries=2, sleep=time.sleep):
    """Send *request* (see build_request) and return the decoded JSON response.

    Retries rate limits (429), overload (529), server errors (5xx) and
    connection problems with a short backoff.
    """
    scheme = urllib.parse.urlsplit(request.url).scheme
    if scheme not in ("http", "https"):
        raise AIError("The server address must start with http:// or https://")
    if timeout is None:
        timeout = 600 if request.provider == "OLLAMA" else 180  # local models can be slow
    data = json.dumps(request.body).encode("utf-8")
    hdrs = dict(request.headers)
    if request.provider == "ANTHROPIC":
        hdrs["x-api-key"] = api_key
    elif api_key:
        hdrs["authorization"] = "Bearer " + api_key
    context = _ssl_context() if scheme == "https" else None
    attempt = 0
    while True:
        req = urllib.request.Request(request.url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            status = ex.code
            detail = _error_detail(ex)
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
            if request.provider == "OLLAMA" and status == 404:
                msg = "Model not found - download it first with 'ollama pull %s'" % request.body.get("model", "")
            if status == 401:
                detail = ""  # the API message only repeats ours
            raise AIError(msg + (": " + detail if detail else "")) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as ex:
            if attempt < retries:
                attempt += 1
                sleep(2 ** attempt)
                continue
            reason = getattr(ex, "reason", ex)
            if request.provider == "OLLAMA":
                raise AIError("Could not reach Ollama at %s - is it running? (%s)" % (request.url, reason)) from None
            raise AIError("Could not reach the API: %s" % reason) from None
        except ValueError:
            raise AIError("The server did not answer with JSON - check the server address") from None
