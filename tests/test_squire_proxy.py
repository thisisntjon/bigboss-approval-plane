import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bigboss.intel.proxy import SquireProxyServer, upstream_root
from bigboss.store import Store


CHAT_RESPONSE = {
    "id": "chatcmpl-test",
    "model": "google/gemma-4-e4b",
    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 34},
}

SSE_STREAM = (
    b'data: {"id":"chatcmpl-test","model":"google/gemma-4-e4b","choices":[{"delta":{"content":"hel"}}]}\n\n'
    b'data: {"id":"chatcmpl-test","model":"google/gemma-4-e4b","choices":[{"delta":{"content":"lo"}}]}\n\n'
    b'data: {"id":"chatcmpl-test","model":"google/gemma-4-e4b","choices":[],'
    b'"usage":{"prompt_tokens":12,"completion_tokens":34}}\n\n'
    b"data: [DONE]\n\n"
)


class _FakeLMStudioHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"data": [{"id": "google/gemma-4-e4b"}]})
            return
        self._json({}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(SSE_STREAM)
            return
        self._json(CHAT_RESPONSE)

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class SquireProxyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLMStudioHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        up_host, up_port = self.upstream.server_address

        self.proxy = SquireProxyServer(
            ("127.0.0.1", 0), self.store, upstream=f"http://{up_host}:{up_port}/v1"
        )
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        host, port = self.proxy.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close()
        self.upstream.shutdown(); self.upstream.server_close()
        self.tempdir.cleanup()

    def _post(self, path, obj, headers=None):
        merged = {"content-type": "application/json"}
        merged.update(headers or {})
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(obj).encode(), headers=merged, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    def _calls(self):
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM squire_calls ORDER BY created_at")]

    def _wait_for_calls(self, n, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self._calls()
            if len(calls) >= n:
                return calls
            time.sleep(0.02)
        return self._calls()

    def test_upstream_root_strips_v1(self):
        self.assertEqual(upstream_root("http://lanbox:1234/v1"), "http://lanbox:1234")
        self.assertEqual(upstream_root("http://lanbox:1234"), "http://lanbox:1234")

    def test_non_stream_relays_and_meters_client_from_api_key(self):
        status, body = self._post(
            "/v1/chat/completions",
            {"model": "google/gemma-4-e4b", "messages": []},
            headers={"Authorization": "Bearer sampleapp"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "chatcmpl-test")  # relayed verbatim
        call = self._wait_for_calls(1)[0]
        self.assertEqual(call["client"], "sampleapp")
        self.assertEqual(call["purpose"], "proxy")
        self.assertEqual(call["prompt_tokens"], 12)
        self.assertEqual(call["completion_tokens"], 34)
        self.assertEqual(call["ok"], 1)

    def test_stream_relays_and_meters_final_usage_chunk(self):
        status, body = self._post(
            "/v1/chat/completions",
            {"model": "google/gemma-4-e4b", "stream": True, "messages": []},
            headers={"Authorization": "Bearer fin-analyzer"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"[DONE]", body)
        call = self._wait_for_calls(1)[0]
        self.assertEqual(call["client"], "fin-analyzer")
        self.assertEqual(call["prompt_tokens"], 12)
        self.assertEqual(call["completion_tokens"], 34)

    def test_header_fallback_when_key_is_generic(self):
        self._post(
            "/v1/chat/completions",
            {"model": "google/gemma-4-e4b", "messages": []},
            headers={"Authorization": "Bearer lm-studio", "X-Squire-Client": "webshop"},
        )
        call = self._wait_for_calls(1)[0]
        self.assertEqual(call["client"], "webshop")

    def test_unlabelled_client_is_unknown(self):
        self._post("/v1/chat/completions", {"model": "google/gemma-4-e4b", "messages": []})
        call = self._wait_for_calls(1)[0]
        self.assertEqual(call["client"], "unknown")

    def test_models_passthrough_is_not_metered(self):
        req = urllib.request.Request(f"{self.base}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        self.assertEqual(data["data"][0]["id"], "google/gemma-4-e4b")
        time.sleep(0.2)
        self.assertEqual(self._calls(), [])

    def test_upstream_down_returns_502_and_ledgers_failure(self):
        self.proxy.upstream = "http://127.0.0.1:1"
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps({"messages": []}).encode(),
            headers={"content-type": "application/json", "Authorization": "Bearer sampleapp"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(raised.exception.code, 502)
        call = self._wait_for_calls(1)[0]
        self.assertEqual(call["ok"], 0)
        self.assertEqual(call["client"], "sampleapp")

    def test_proxy_writes_readable_prompt_and_response(self):
        self._post(
            "/v1/chat/completions",
            {"model": "google/gemma-4-e4b", "messages": [{"role": "user", "content": "hi there"}]},
            headers={"Authorization": "Bearer sampleapp"},
        )
        self._wait_for_calls(1)
        deadline = time.monotonic() + 3.0
        rows = self.store.list_egress()
        while not rows and time.monotonic() < deadline:
            time.sleep(0.02)
            rows = self.store.list_egress()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["client"], "sampleapp")
        self.assertIn("hi there", row["prompt_text"])
        self.assertIn("hello", row["response_text"])

    def test_fronting_a_named_endpoint_carries_its_exposure(self):
        import os
        from unittest import mock

        up_host, up_port = self.upstream.server_address
        with mock.patch.dict(os.environ, {"BEASTMODE_BASE_URL": f"http://{up_host}:{up_port}/v1"}):
            proxy = SquireProxyServer(("127.0.0.1", 0), self.store, upstream=f"http://{up_host}:{up_port}/v1")
        self.assertEqual(proxy.endpoint_name, "beastmode")
        self.assertEqual(proxy.exposure, "managed-edr")
        proxy.server_close()

    def test_status_rollup_groups_by_client(self):
        self._post("/v1/chat/completions", {"messages": []}, headers={"Authorization": "Bearer sampleapp"})
        self._post("/v1/chat/completions", {"messages": []}, headers={"Authorization": "Bearer sampleapp"})
        self._post("/v1/chat/completions", {"messages": []}, headers={"Authorization": "Bearer webshop"})
        self._wait_for_calls(3)
        status = self.store.squire_status()
        by_client = {c["client"]: c for c in status["clients"]}
        self.assertEqual(by_client["sampleapp"]["calls"], 2)
        self.assertEqual(by_client["webshop"]["calls"], 1)
        self.assertEqual(status["totals"]["calls"], 3)
        self.assertEqual(status["totals"]["prompt_tokens"], 36)


if __name__ == "__main__":
    unittest.main()
