import json
import tempfile
import threading
import time
import unittest
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bigboss.router import upstream
from bigboss.router.proxy import RouterHTTPServer
from bigboss.store import Store


NON_STREAM_MESSAGE = {
    "type": "message",
    "id": "msg_test",
    "model": "claude-opus-4-8",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
}

SSE_STREAM = (
    b'data: {"type":"message_start","message":{"model":"claude-opus-4-8",'
    b'"usage":{"input_tokens":100,"output_tokens":1}}}\n\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":250}}\n\n'
    b'data: {"type":"message_stop"}\n\n'
)


class _MockUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        raw = self._body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if self.path == "/v1/messages/count_tokens":
            payload = json.dumps({"input_tokens": 42}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(SSE_STREAM)
            return
        payload = json.dumps(NON_STREAM_MESSAGE).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RouterProxyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), _MockUpstreamHandler)
        self.mock_thread = threading.Thread(target=self.mock.serve_forever, daemon=True)
        self.mock_thread.start()
        mock_host, mock_port = self.mock.server_address
        self._orig_base = upstream.UPSTREAM_BASE
        upstream.UPSTREAM_BASE = f"http://{mock_host}:{mock_port}"

        self.router = RouterHTTPServer(("127.0.0.1", 0), self.store, harness="agent-sdk")
        self.router_thread = threading.Thread(target=self.router.serve_forever, daemon=True)
        self.router_thread.start()
        self.router_host, self.router_port = self.router.server_address

    def tearDown(self):
        upstream.UPSTREAM_BASE = self._orig_base
        self.router.shutdown(); self.router.server_close()
        self.mock.shutdown(); self.mock.server_close()
        self.tempdir.cleanup()

    def _post(self, path, obj):
        req = urllib.request.Request(
            f"http://{self.router_host}:{self.router_port}{path}",
            data=json.dumps(obj).encode(),
            headers={"content-type": "application/json", "x-api-key": "test-key"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    def test_non_stream_relays_and_meters(self):
        status, body = self._post("/v1/messages", {"model": "claude-opus-4-8", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "msg_test")  # relayed verbatim
        # 1M in @ $5 + 1M out @ $25 = $30 recorded (recording happens after relay).
        calls = self._wait_for_calls(1)
        self.assertEqual(Decimal(calls[0]["cost_usd"]), Decimal("30"))
        self.assertEqual(Decimal(self.store.check_budget()["spent_usd"]), Decimal("30"))

    def test_stream_relays_and_meters(self):
        status, body = self._post("/v1/messages", {"model": "claude-opus-4-8", "stream": True, "messages": []})
        self.assertEqual(status, 200)
        self.assertIn(b"message_delta", body)  # SSE relayed to client
        calls = self._wait_for_calls(1)
        self.assertEqual(calls[0]["streamed"], 1)
        # input 100 @ $5/M + output 250 @ $25/M = 0.000500 + 0.006250 = 0.00675
        self.assertEqual(Decimal(calls[0]["cost_usd"]), Decimal("0.00675"))

    def test_reroute_recorded_from_served_model(self):
        # Client asked for Fable; mock upstream serves Opus (as the reroute would).
        self._post("/v1/messages", {"model": "claude-fable-5", "messages": []})
        call = self._wait_for_calls(1)[-1]
        self.assertEqual(call["requested_model"], "claude-fable-5")
        self.assertEqual(call["served_model"], "claude-opus-4-8")

    def test_count_tokens_passthrough_not_metered(self):
        status, body = self._post("/v1/messages/count_tokens", {"model": "claude-opus-4-8", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["input_tokens"], 42)
        time.sleep(0.2)  # give any (erroneous) recording a chance to appear
        self.assertEqual(self._router_calls(), [])  # passthrough is never metered

    def _router_calls(self):
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM router_calls ORDER BY created_at")]

    def _wait_for_calls(self, n, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self._router_calls()
            if len(calls) >= n:
                return calls
            time.sleep(0.02)
        return self._router_calls()


if __name__ == "__main__":
    unittest.main()
