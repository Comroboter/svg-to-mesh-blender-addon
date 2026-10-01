"""Tests for the optional AI depth suggestions (no network access needed)."""

import io
import json
import urllib.error
import zlib

import pytest

np = pytest.importorskip("numpy")

from svg_to_mesh.core import ai_client  # noqa: E402
from svg_to_mesh.core.raster import encode_png, rasterize  # noqa: E402


def decode_png(data):
    """Minimal decoder for the PNGs written by encode_png (RGB, filter 0)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            w, h = int.from_bytes(chunk[:4], "big"), int.from_bytes(chunk[4:8], "big")
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows = [raw[r * (w * 3 + 1) + 1:(r + 1) * (w * 3 + 1)] for r in range(h)]
    return np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(h, w, 3)


def test_rasterize_and_png_roundtrip():
    square = np.array([[[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 1], [0, 1]]], dtype=float)
    small = np.array([[[0.4, 0.4], [0.6, 0.4], [0.6, 0.6]], [[0.4, 0.4], [0.6, 0.6], [0.4, 0.6]]])
    img, to_px = rasterize([((1, 0, 0), square), ((0, 0, 1), small)], (0, 0, 1, 1), max_size=100)
    back = decode_png(encode_png(img))
    assert back.shape == img.shape
    assert (back == img).all()
    cx, cy = (int(v) for v in to_px(0.5, 0.5))
    assert tuple(img[cy, cx]) == (0, 0, 255)  # top layer wins
    x, y = (int(v) for v in to_px(0.1, 0.9))
    assert tuple(img[y, x]) == (255, 0, 0)
    assert tuple(img[1, 1]) == (150, 150, 150)  # margin = background


def test_build_request_shape():
    req = ai_client.build_request([{"id": 1, "color": "#ff0000", "area_percent": 100.0}], b"png", "keychain")
    headers, body = req.headers, req.body
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "x-api-key" not in headers
    assert body["model"] == ai_client.DEFAULT_MODEL == "claude-opus-5-5"
    assert body["fallbacks"] == "default" and headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    image, text = body["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert "keychain" in text["text"] and '"#ff0000"' in text["text"]
    assert "thinking" not in body  # Opus 5.5: adaptive thinking is the default
    json.dumps(body)  # serializable


def test_build_request_haiku_has_no_effort_or_fallbacks():
    req = ai_client.build_request([], b"", model="claude-haiku-4-5")
    headers, body = req.headers, req.body
    assert "effort" not in body["output_config"]
    assert "fallbacks" not in body and "anthropic-beta" not in headers


def _response(payload, stop="end_turn"):
    return {"stop_reason": stop, "content": [{"type": "text", "text": json.dumps(payload)}]}


def test_parse_response_clamps_and_filters():
    data = _response({"summary": "ok", "regions": [
        {"id": 1, "height": 1.5, "base": 0, "reason": "background"},
        {"id": 2, "height": 99, "base": -50, "reason": "x"},
        {"id": 7, "height": 1, "base": 0, "reason": "unknown id"},
    ]})
    out, summary = ai_client.parse_response(data, {1, 2})
    assert summary == "ok"
    assert out[1] == (1.5, 0.0, "background")
    assert out[2][:2] == (ai_client.HEIGHT_RANGE[1], ai_client.BASE_RANGE[0])
    assert 7 not in out


@pytest.mark.parametrize("data", [
    {"stop_reason": "refusal", "content": []},
    {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"regions": [], "summary": ""}'}]},
])
def test_parse_response_errors(data):
    with pytest.raises(ai_client.AIError):
        ai_client.parse_response(data, {1})


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_api_retries_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout, context):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 529, "overloaded", {"retry-after": "1"},
                                         io.BytesIO(b'{"error": {"message": "Overloaded"}}'))
        return _FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(ai_client.urllib.request, "urlopen", fake_urlopen)
    slept = []
    req = ai_client.Request("ANTHROPIC", ai_client.API_URL, {"a": 1}, {"content-type": "application/json"})
    out = ai_client.call_api(req, "key", sleep=slept.append)
    assert out == {"ok": True}
    assert len(calls) == 2 and slept == [1.0]
    assert calls[0].get_header("X-api-key") == "key"
    assert calls[0].get_method() == "POST"


def test_call_api_maps_errors(monkeypatch):
    def fake_urlopen(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(ai_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ai_client.AIError, match="API key is invalid"):
        ai_client.call_api(ai_client.Request("ANTHROPIC", ai_client.API_URL, {}), "bad", sleep=lambda s: None)


# --------------------------------------------------------------------------
# OpenAI-compatible and Ollama servers (a real local HTTP server that checks
# the request format and answers like the real APIs)
# --------------------------------------------------------------------------

ANSWER = {"regions": [{"id": 1, "height": 2.0, "base": 0.0, "reason": "main"},
                      {"id": 2, "height": 1.0, "base": 1.0, "reason": "detail"}], "summary": "ok"}


@pytest.fixture()
def fake_server():
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.path, dict(self.headers), body))
            if self.path == "/v1/chat/completions":
                user = body["messages"][1]["content"]
                assert body["messages"][0]["role"] == "system"
                assert user[0]["type"] == "text" and user[1]["image_url"]["url"].startswith("data:image/png;base64,")
                assert body["response_format"]["json_schema"]["strict"] is True
                out = {"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(ANSWER), "refusal": None}}]}
            elif self.path == "/api/chat":
                if body["model"] == "missing":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "model \'missing\' not found"}')
                    return
                assert body["stream"] is False and body["format"]["type"] == "object"
                assert len(body["messages"][1]["images"]) == 2
                out = {"model": body["model"], "message": {"role": "assistant", "content": json.dumps(ANSWER)},
                       "done": True, "done_reason": "stop"}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_port, seen
    server.shutdown()


REGIONS = [{"id": 1, "color": "#000000", "area_percent": 80.0}, {"id": 2, "color": "#000000", "area_percent": 20.0}]


def test_openai_compatible_server(fake_server):
    url, seen = fake_server
    req = ai_client.build_request(REGIONS, [b"a", b"b"], "sign", "my-vision-model", provider="OPENAI",
                                  base_url=url + "/v1/")
    data = ai_client.call_api(req, "sk-test", sleep=lambda s: None)
    out, summary = ai_client.parse_response(data, {1, 2}, "OPENAI")
    assert out[1][:2] == (2.0, 0.0) and out[2][:2] == (1.0, 1.0) and summary == "ok"
    path, headers, body = seen[0]
    assert headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "my-vision-model" and body["max_tokens"] == 16000  # compatible server
    official = ai_client.build_request(REGIONS, [b"a"], provider="OPENAI")
    assert official.url == "https://api.openai.com/v1/chat/completions"
    assert "max_completion_tokens" in official.body and official.body["model"] == "gpt-5-mini"


def test_openai_refusal_and_cutoff():
    with pytest.raises(ai_client.AIError, match="declined"):
        ai_client.parse_response({"choices": [{"message": {"refusal": "no"}}]}, {1}, "OPENAI")
    with pytest.raises(ai_client.AIError, match="cut off"):
        ai_client.parse_response({"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}, {1}, "OPENAI")


def test_ollama_server(fake_server):
    url, seen = fake_server
    req = ai_client.build_request(REGIONS, [b"a", b"b"], provider="OLLAMA", base_url=url)
    assert req.body["model"] == "gemma3"
    data = ai_client.call_api(req, sleep=lambda s: None)
    out, _summary = ai_client.parse_response(data, {1, 2}, "OLLAMA")
    assert set(out) == {1, 2}
    assert "Authorization" not in seen[0][1]
    missing = ai_client.build_request(REGIONS, [b"a", b"b"], model="missing", provider="OLLAMA", base_url=url)
    with pytest.raises(ai_client.AIError, match="ollama pull missing"):
        ai_client.call_api(missing, sleep=lambda s: None)


def test_ollama_not_running():
    req = ai_client.build_request(REGIONS, [b"a"], provider="OLLAMA", base_url="http://127.0.0.1:9")
    with pytest.raises(ai_client.AIError, match="is it running"):
        ai_client.call_api(req, retries=0, timeout=5)


def test_code_fenced_json_is_accepted():
    data = {"message": {"content": "```json\n" + json.dumps(ANSWER) + "\n```"}, "done": True}
    out, _summary = ai_client.parse_response(data, {1, 2}, "OLLAMA")
    assert set(out) == {1, 2}


def test_bad_server_address():
    req = ai_client.build_request(REGIONS, [b"a"], provider="OPENAI", base_url="file:///etc")
    with pytest.raises(ai_client.AIError, match="http"):
        ai_client.call_api(req, "k")
