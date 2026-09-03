"""M6.2 — Brainz recall bridge: the fail-closed shareability gate, the loopback guard, recall parsing,
and the load-bearing privacy invariant (/recall never injects into the model-bound conversation)."""

import os
import tempfile
import unittest
from pathlib import Path

from bigboss import brainz, chat
from bigboss.brainz import CLOUD_VENDOR, DISPLAY_LOCAL, PHONE_TUNNEL, BrainzClient, BrainzError, may_share
from bigboss.store import Store


class ShareabilityGateTests(unittest.TestCase):
    def test_display_local_always_allowed(self):
        for tier in ("public", "personal", "sensitive", "restricted", None):
            self.assertTrue(may_share(tier, DISPLAY_LOCAL))

    def test_offbox_denied_by_default(self):
        # search results can't prove exportable -> nothing crosses off-box
        self.assertFalse(may_share("public", CLOUD_VENDOR))
        self.assertFalse(may_share("public", PHONE_TUNNEL))
        self.assertFalse(may_share("restricted", CLOUD_VENDOR))

    def test_offbox_only_public_and_exportable(self):
        self.assertTrue(may_share("public", CLOUD_VENDOR, exportable=True))    # future gated-endpoint path
        self.assertFalse(may_share("personal", CLOUD_VENDOR, exportable=True))  # not public
        self.assertFalse(may_share("public", CLOUD_VENDOR, exportable=False))


class LoopbackGuardTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("BIGBOSS_BRAINZ_ALLOW_LAN", None)
        os.environ.pop("BIGBOSS_BRAINZ_URL", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_loopback_ok(self):
        self.assertTrue(BrainzClient("http://127.0.0.1:8077").base_url.endswith("8077"))

    def test_non_loopback_refused(self):
        with self.assertRaises(BrainzError):
            BrainzClient("http://198.51.100.5:8077")

    def test_non_loopback_allowed_with_optin(self):
        os.environ["BIGBOSS_BRAINZ_ALLOW_LAN"] = "1"
        self.assertTrue(BrainzClient("http://198.51.100.5:8077"))


class RecallParsingTests(unittest.TestCase):
    def test_recall_normalizes_and_flags_degraded(self):
        client = BrainzClient("http://127.0.0.1:8077")
        client._get = lambda path: {
            "mode": "keyword", "notices": ["semantic index unavailable"],
            "results": [{"title": "Syx design", "platform": "claude", "created_at": "2026-01-02T10:00:00Z",
                         "snippet": "we discussed the memory schema", "tier": "personal", "conversation_id": "c1"}]}
        rec = client.recall("memory schema")
        self.assertTrue(rec["degraded"])            # keyword fallback flagged
        self.assertEqual(rec["results"][0]["title"], "Syx design")
        self.assertEqual(rec["results"][0]["date"], "2026-01-02")


class RecallPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.session = chat.ChatSession(self.store, seat="claude")
        self._orig = brainz.BrainzClient

        class _Fake:
            base_url = "http://127.0.0.1:8077"
            def ping(self): return {"ok": True, "detail": "", "latency_ms": 1}
            def recall(self, q, limit=6):
                return {"mode": "semantic", "degraded": False, "notices": [],
                        "results": [{"title": "PRIVATE MEMORY", "platform": "claude", "date": "2026-01-01",
                                     "snippet": "SECRET SNIPPET", "tier": "restricted", "conversation_id": "c1"}]}
        brainz.BrainzClient = lambda *a, **k: _Fake()

    def tearDown(self):
        brainz.BrainzClient = self._orig
        self.tmp.cleanup()

    def test_recall_displays_but_never_injects(self):
        out, done = self.session.handle("/recall my memory system")
        self.assertFalse(done)
        self.assertIn("PRIVATE MEMORY", out)                 # shown to the operator locally
        self.assertIn("NOT sent to the model", out)          # the display-only disclaimer
        # THE load-bearing invariant: nothing entered the model-bound conversation
        self.assertEqual(self.session.messages, [])
        # and an audit event was recorded (no content)
        types = {e["event_type"] for e in self.store.events_after(0, limit=100)}
        self.assertIn("brainz.recall", types)


if __name__ == "__main__":
    unittest.main()
