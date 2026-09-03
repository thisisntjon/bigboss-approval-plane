"""Forward a request to the Anthropic API and relay the response, streaming.

Header discipline (compass section 1.3b): forward the client's own credential + beta flags
verbatim (never synthesize our own), swap Host, drop hop-by-hop headers. Streamed
responses are relayed chunk-by-chunk with no Content-Length and never buffered; the
optional meter sniffs the usage frames as they pass.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


UPSTREAM_BASE = "https://api.anthropic.com"

# Client -> upstream: forward only these (the client already attached the right
# credential + beta capability). Everything else is dropped.
FORWARD_REQUEST_HEADERS = (
    "authorization",
    "x-api-key",
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "content-type",
    "accept",
    "user-agent",
)

# Upstream -> client: safe response headers to copy (content-type/-length and
# hop-by-hop are handled explicitly per branch, so they are NOT in this list).
COPY_RESPONSE_HEADERS = (
    "anthropic-version",
    "request-id",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "retry-after",
)


def _filtered_request_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in FORWARD_REQUEST_HEADERS:
        value = headers.get(key)
        if value:
            out[key] = value
    return out


def relay(handler, method: str, path: str, query: str, headers: Any, body: bytes, meter=None, timeout: int = 600) -> dict[str, Any]:
    """Forward (method, path) to UPSTREAM_BASE and relay the response to handler.wfile.
    Feeds `meter` from a successful (non-error) response. Returns {status, streamed}."""
    url = UPSTREAM_BASE + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, data=body if body else None, method=method)
    for key, value in _filtered_request_headers(headers).items():
        req.add_header(key, value)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:  # non-2xx: relay the real Anthropic error, don't meter
        return _relay(handler, exc, meter=None, is_error=True)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return _bad_gateway(handler, str(exc))
    with resp:
        return _relay(handler, resp, meter=meter)


def _relay(handler, resp, meter, is_error: bool = False) -> dict[str, Any]:
    status = getattr(resp, "status", None) or resp.getcode()
    content_type = resp.headers.get("content-type", "") or ""
    streaming = content_type.startswith("text/event-stream")

    handler.send_response(status)
    for header in COPY_RESPONSE_HEADERS:
        value = resp.headers.get(header)
        if value:
            handler.send_header(header, value)

    if streaming:
        handler.send_header("content-type", content_type)
        handler.end_headers()
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            if meter is not None and not is_error:
                meter.feed(chunk)
        return {"status": status, "streamed": True}

    data = resp.read()
    handler.send_header("content-type", content_type or "application/json")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass
    if meter is not None and not is_error:
        meter.feed_full(data)
    return {"status": status, "streamed": False}


def _bad_gateway(handler, detail: str) -> dict[str, Any]:
    payload = json.dumps(
        {"type": "error", "error": {"type": "api_error", "message": f"BigBoss router upstream error: {detail}"}}
    ).encode("utf-8")
    handler.send_response(502)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    try:
        handler.wfile.write(payload)
    except (BrokenPipeError, ConnectionResetError):
        pass
    return {"status": 502, "streamed": False}
