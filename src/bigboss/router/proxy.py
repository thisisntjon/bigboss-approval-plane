"""The BigBoss router HTTP server (Ecosystem Phase E1): a loopback-only proxy at
ANTHROPIC_BASE_URL that meters /v1/messages spend and transparently passes through
everything else. Metering-only: no model rewriting, no the remote host, no hard-block.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
from urllib.parse import urlparse

from ..store import Store
from . import pricing, upstream
from .meter import UsageMeter


class RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, store: Store, harness: str = "agent-sdk", hard_cap: str | None = None):
        super().__init__(server_address, RouterHandler)
        self.store = store
        self.harness = harness
        if hard_cap is not None:
            self.store.set_hard_cap(str(hard_cap))


ROUTER_BODY_MAX_BYTES = 1024 * 1024


class RouterHandler(BaseHTTPRequestHandler):
    server: RouterHTTPServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} router {self.address_string()} {fmt % args}")

    def do_POST(self) -> None:
        if not self._client_is_loopback():
            return self._forbid()
        parsed = urlparse(self.path)
        if parsed.path == "/v1/messages":
            return self._handle_messages(parsed)
        return self._passthrough(parsed, "POST")

    def do_GET(self) -> None:
        if not self._client_is_loopback():
            return self._forbid()
        return self._passthrough(urlparse(self.path), "GET")

    def _handle_messages(self, parsed) -> None:
        raw = self._read_body()
        if raw is None:
            return
        try:
            body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        requested_model = str(body.get("model") or "")

        meter = UsageMeter()
        result = upstream.relay(self, "POST", parsed.path, parsed.query, self.headers, raw, meter=meter)

        usage = meter.result()
        # Only record real, successful responses; skip upstream errors (no billable usage).
        if result.get("status", 200) >= 400:
            return
        if not (usage.served_model or usage.input_tokens or usage.output_tokens):
            return
        served = usage.served_model or requested_model
        try:
            cost = pricing.cost(served, usage.as_pricing_usage())
            self.server.store.record_router_call(
                requested_model=requested_model or served,
                served_model=served,
                usage=usage.as_pricing_usage(),
                cost_usd=cost,
                harness=self.server.harness,
                tier_reason="passthrough",
                streamed=bool(result.get("streamed")),
                stop_reason=usage.stop_reason,
            )
        except Exception as exc:  # metering must never break the proxy path
            print(f"router: metering error (relay already delivered): {exc!r}")

    def _passthrough(self, parsed, method: str) -> None:
        if method == "POST":
            raw = self._read_body()
            if raw is None:
                return
        else:
            raw = b""
        upstream.relay(self, method, parsed.path, parsed.query, self.headers, raw, meter=None)

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Content-Length is not an integer.")
            return None
        if length < 0:
            self._send_error(HTTPStatus.BAD_REQUEST, "Content-Length is invalid.")
            return None
        if length > ROUTER_BODY_MAX_BYTES:
            self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Request body exceeds {ROUTER_BODY_MAX_BYTES} bytes.",
            )
            return None
        return self.rfile.read(length) if length else b""

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _client_is_loopback(self) -> bool:
        try:
            return ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _forbid(self) -> None:
        self._send_error(HTTPStatus.FORBIDDEN, "BigBoss router is loopback-only.")

