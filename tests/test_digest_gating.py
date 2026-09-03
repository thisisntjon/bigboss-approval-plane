"""E4a.1 digest-egress gating: newly-discovered projects wait for batch approval
before any off-box digestion."""

import tempfile
import unittest
from pathlib import Path

from bigboss.registry.canonical import ResolvedProject
from bigboss.registry.ingest import ingest_bundle
from bigboss.store import Store


def _rp(path, slug):
    return ResolvedProject(
        canonical_path=path, slug=slug, family=slug, markers={".git": True},
        git_commit_count=1, last_activity_at="2026-06-01T00:00:00Z", status="active",
        purpose="", domain="", sources=["fs-scan"], aliases=[],
    )


class DigestGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.device = self._device()

    def tearDown(self):
        self.tmp.cleanup()

    def _device(self):
        d = self.store.enroll_device("Workstation", method="auto-local")
        return {"id": d["device_id"], "name": d["device_name"]}

    def _proj(self, slug):
        return next(p for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == slug)

    def test_new_discovery_is_gated_update_is_not(self):
        self.store.apply_reconciled([_rp("C:/x/alpha", "alpha")])
        self.assertTrue(self._proj("alpha")["digest_pending"])
        # Clear (approve), then re-run the same reconcile (an UPDATE) — must stay cleared.
        self.store.clear_digest_pending(["alpha"])
        self.store.apply_reconciled([_rp("C:/x/alpha", "alpha")])
        self.assertFalse(self._proj("alpha")["digest_pending"])

    def test_batch_card_is_created_once_and_lists_slugs(self):
        self.store.apply_reconciled([_rp("C:/x/a", "a"), _rp("C:/x/b", "b")])
        card = self.store.create_digest_batch_card()
        self.assertIsNotNone(card)
        self.assertEqual(card["status"], "pending")
        self.assertEqual(card["proposed_action"]["kind"], "digest_batch")
        self.assertEqual(sorted(card["proposed_action"]["slugs"]), ["a", "b"])
        # Idempotent while one is open.
        self.assertIsNone(self.store.create_digest_batch_card())

    def test_no_card_when_nothing_pending(self):
        self.store.apply_reconciled([_rp("C:/x/a", "a")])
        self.store.clear_digest_pending()
        self.assertIsNone(self.store.create_digest_batch_card())

    def test_approve_clears_gate(self):
        self.store.apply_reconciled([_rp("C:/x/a", "a")])
        card = self.store.create_digest_batch_card()
        self.store.resolve_approval(card["id"], "approve_once", "ok", self.device)
        self.assertFalse(self._proj("a")["digest_pending"])
        self.assertFalse(self._proj("a")["no_crawl"])

    def test_reject_excludes(self):
        self.store.apply_reconciled([_rp("C:/x/a", "a")])
        card = self.store.create_digest_batch_card()
        self.store.resolve_approval(card["id"], "reject", "no", self.device)
        self.assertFalse(self._proj("a")["digest_pending"])
        self.assertTrue(self._proj("a")["no_crawl"])

    def test_remote_ingest_gates_new_projects_and_raises_card(self):
        bundle = {
            "bundle_version": 1, "host": "lanbox", "generated_at": "2026-07-02T00:00:00Z",
            "projects": [{"path": "V:/newremote", "markers": {".git": True}}],
        }
        summary = ingest_bundle(self.store, bundle)
        self.assertIsNotNone(summary["digest_batch_card"])
        self.assertTrue(self._proj("newremote")["digest_pending"])

    def test_pinned_and_existing_untouched(self):
        # An already-known (cleared) project stays ungated across further discoveries.
        self.store.apply_reconciled([_rp("C:/x/known", "known")])
        self.store.clear_digest_pending()
        self.store.apply_reconciled([_rp("C:/x/known", "known"), _rp("C:/x/fresh", "fresh")])
        self.assertFalse(self._proj("known")["digest_pending"])
        self.assertTrue(self._proj("fresh")["digest_pending"])


if __name__ == "__main__":
    unittest.main()
