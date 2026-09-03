"""M1b — Council live path: vendor callers against a mock upstream (no real keys/spend)
+ session persistence + the throne track-record leaderboard."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bigboss.council import providers
from bigboss.store import Store


class _MockChatHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.hits += 1
        if self.server.fail_times and self.server.hits <= self.server.fail_times:
            self._send(500, {"error": "transient"})
            return
        self._send(200, {
            "model": "gpt-5.4-mini",
            "choices": [{"message": {"role": "assistant", "content": "walk or drive: drive."}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 8},
        })

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ProviderCallerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockChatHandler)
        self.server.hits = 0
        self.server.fail_times = 0
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._orig_url = providers.OPENAI_URL
        providers.OPENAI_URL = self.url
        self._orig_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key"
        providers._ENV_LOADED = True  # don't let the loader overwrite our test key

    def tearDown(self):
        providers.OPENAI_URL = self._orig_url
        if self._orig_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._orig_key
        self.server.shutdown()
        self.server.server_close()

    def test_openai_compatible_success(self):
        r = providers.call_seat("gpt", "system", "the question")
        self.assertEqual(r["status"], "success")
        self.assertIn("drive", r["answer"])
        self.assertEqual(r["usage"], {"input": 30, "output": 8})
        self.assertEqual(r["provider"], "openai")

    def test_missing_key_returns_error_result(self):
        os.environ.pop("OPENAI_API_KEY", None)
        r = providers.call_seat("gpt", "s", "q")
        self.assertEqual(r["status"], "error")
        self.assertIn("no API key", r["error"])
        os.environ["OPENAI_API_KEY"] = "test-key"

    def test_transient_error_retries_then_succeeds(self):
        self.server.fail_times = 1  # first attempt 500, retry succeeds
        r = providers.call_seat("gpt", "s", "q")
        self.assertEqual(r["status"], "success")
        self.assertGreaterEqual(self.server.hits, 2)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self):
        return {
            "mode": "live",
            "question": "walk or drive?",
            "seats": ["claude", "gpt"],
            "winner": "claude",
            "final": {"final_answer": "Drive."},
            "verified_claims": [{"id": "claude-1", "verdict": "supported"}],
            "per_model_scores": {
                "claude": {"model": "claude-haiku-4-5-20251001", "verified": 3, "partial": 1, "total": 4, "score": 87.5},
                "gpt": {"model": "gpt-5.4-mini", "verified": 2, "partial": 0, "total": 4, "score": 50.0},
            },
            "usage": {"input": 1200, "output": 300},
        }

    def test_record_and_leaderboard(self):
        rec = self.store.record_council_session(self._report(), cost_usd="0.0042")
        self.assertTrue(rec["session_id"].startswith("cnc"))
        # Two sessions so the leaderboard aggregates.
        self.store.record_council_session(self._report(), cost_usd="0.0042")
        lb = self.store.council_model_leaderboard(days=90)
        by = {r["model_id"]: r for r in lb}
        self.assertEqual(by["claude"]["sessions"], 2)
        self.assertAlmostEqual(by["claude"]["avg_score"], 87.5, places=1)
        self.assertEqual(by["claude"]["total"], 8)  # 4 per session x2
        self.assertEqual(lb[0]["model_id"], "claude")  # ordered by avg_score desc

    def test_council_events_emitted(self):
        self.store.record_council_session(self._report())
        types = {e["event_type"] for e in self.store.events_after(0, limit=200)}
        self.assertIn("council.session.answered", types)
        self.assertIn("council.score.recorded", types)


if __name__ == "__main__":
    unittest.main()
