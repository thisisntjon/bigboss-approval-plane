"""Squire metering proxy: a pass-through front for LM Studio on the remote host that
ledgers per-client usage (calls, tokens, latency, failures) into squire_calls.

Multiple projects share Squire and LM Studio has no auth or logging, so this is
the monitoring point. Clients switch by pointing SQUIRE_BASE_URL at this proxy
and setting their API key to their project name - LM Studio ignores the key, we
read it as the client label:

    SQUIRE_BASE_URL=http://127.0.0.1:1235/v1
    SQUIRE_API_KEY=<project-name>       # becomes the ledger's client label

Unlike the Anthropic router (E1), no live credential is forwarded here and the
the remote host endpoint is already LAN-open, so a LAN bind is allowed via --host when
other machines need metered access.

Every metered request is also written to the egress audit: the readable prompt sent
and the model's response, tagged with the endpoint's known exposure channel.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..store import Store
from .digest import messages_to_text
from .endpoints import list_endpoints
from .squire import DEFAULT_BASE_URL


# Endpoints whose responses carry an OpenAI-style usage object worth metering.
METERED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")

FORWARD_REQUEST_HEADERS = ("content-type", "accept", "user-agent")

_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def upstream_root(base_url: str | None = None) -> str:
    """SQUIRE_BASE_URL includes /v1; incoming proxy paths also include /v1 - so the
    upstream root is the base URL with the trailing /v1 removed."""
    base = (base_url or os.environ.get("SQUIRE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def prompt_from_body(raw: bytes) -> str:
    """Pull the human-readable prompt out of an OpenAI-style request body."""
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw.decode("utf-8", errors="replace")
    messages = body.get("messages")
    if isinstance(messages, list):
        return messages_to_text(messages)
    if body.get("prompt"):
        return str(body["prompt"])
    return raw.decode("utf-8", errors="replace")


class UsageSniffer:
    """Pull usage, model, and the readable response text out of a relayed reply."""

    def __init__(self) -> None:
        self._buf = b""
        self.model = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.response_text = ""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                self._absorb(json.loads(payload))
            except (json.JSONDecodeError, ValueError):
                continue

    def feed_full(self, body: bytes) -> None:
        try:
            self._absorb(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            pass

    def _absorb(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        if event.get("model"):
            self.model = str(event["model"])
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            self.completion_tokens = int(usage.get("completion_tokens") or 0)
        choices = event.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or {}
            msg = choice.get("message") or {}
            piece = delta.get("content") or msg.get("content") or choice.get("text") or ""
            if piece:
                self.response_text += str(piece)


class SquireProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, store: Store, upstream: str | None = None):
        super().__init__(server_address, SquireProxyHandler)
        self.store = store
        self.upstream = upstream_root(upstream)
        # If the upstream is a named endpoint, ledger under its name and carry its
        # known exposure channel into the egress audit (e.g. beastmode/managed-edr).
        self.endpoint_name, self.exposure = "squire", ""
        for ep in list_endpoints():
            if upstream_root(ep.base_url) == self.upstream:
                self.endpoint_name, self.exposure = ep.name, ep.exposure
                break


class SquireProxyHandler(BaseHTTPRequestHandler):
    server: SquireProxyServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} squire-proxy {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        self._relay("GET")

    def do_POST(self) -> None:
        self._relay("POST")

    def _relay(self, method: str) -> None:
        parsed = urlparse(self.path)
        raw = self._read_body() if method == "POST" else b""
        metered = method == "POST" and parsed.path in METERED_PATHS
        client_label = self._client_label()
        self._pending_body = raw
        started = time.monotonic()

        url = self.server.upstream + parsed.path + (("?" + parsed.query) if parsed.query else "")
        req = urllib.request.Request(url, data=raw if raw else None, method=method)
        for key in FORWARD_REQUEST_HEADERS:
            value = self.headers.get(key)
            if value:
                req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            # Real upstream error: relay it; ledger the failure for metered paths.
            if metered:
                self._record(client_label, started, ok=False, error=f"HTTP {exc.code}")
            return self._relay_response(exc, sniffer=None)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if metered:
                self._record(client_label, started, ok=False, error=str(exc)[:300])
            return self._bad_gateway(str(exc))

        sniffer = UsageSniffer() if metered else None
        with resp:
            self._relay_response(resp, sniffer=sniffer)
        if sniffer is not None:
            self._record(
                client_label, started, ok=True,
                model=sniffer.model,
                prompt_tokens=sniffer.prompt_tokens,
                completion_tokens=sniffer.completion_tokens,
                sniffer=sniffer,
            )

    def _relay_response(self, resp, sniffer: UsageSniffer | None) -> None:
        status = getattr(resp, "status", None) or resp.getcode()
        content_type = resp.headers.get("content-type", "") or "application/json"
        streaming = content_type.startswith("text/event-stream")

        self.send_response(status)
        self.send_header("content-type", content_type)
        if streaming:
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                if sniffer is not None:
                    sniffer.feed(chunk)
            return
        data = resp.read()
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if sniffer is not None:
            sniffer.feed_full(data)

    def _record(self, client_label: str, started: float, *, ok: bool, model: str = "",
                prompt_tokens: int = 0, completion_tokens: int = 0, error: str | None = None,
                sniffer: UsageSniffer | None = None) -> None:
        try:
            self.server.store.record_squire_call(
                client=client_label, purpose="proxy", endpoint=self.server.endpoint_name,
                model=model,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=ok, error=error,
            )
            body = getattr(self, "_pending_body", b"")
            response_text = sniffer.response_text if sniffer else (error or "")
            self.server.store.record_egress(
                endpoint=self.server.endpoint_name, exposure=self.server.exposure,
                base_url=self.server.upstream, model=model,
                client=client_label, purpose="proxy",
                chars_sent=len(body),
                tokens_sent=prompt_tokens, tokens_received=completion_tokens,
                prompt_text=prompt_from_body(body),
                response_text=response_text,
                ok=ok,
            )
        except Exception as exc:  # metering must never break the relay path
            print(f"squire-proxy: metering error (relay already delivered): {exc!r}")

    def _client_label(self) -> str:
        auth = self.headers.get("Authorization") or ""
        candidate = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not candidate or candidate.lower() in {"lm-studio", "none", "null"}:
            candidate = (self.headers.get("X-Squire-Client") or "").strip()
        label = _LABEL_RE.sub("-", candidate).strip("-")[:48]
        return label or "unknown"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _bad_gateway(self, detail: str) -> None:
        payload = json.dumps(
            {"error": {"message": f"BigBoss squire-proxy upstream error: {detail}", "type": "api_error"}}
        ).encode("utf-8")
        self.send_response(HTTPStatus.BAD_GATEWAY)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
