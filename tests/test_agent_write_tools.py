"""M6.4 — gated write tool. The load-bearing guarantee: the model can only PROPOSE (raise an approval
card); the mutation happens ONLY on the human's dashboard approval, inside store.resolve_approval."""
import tempfile
import unittest
from pathlib import Path

from bigboss import agent_tools
from bigboss.registry.canonical import ResolvedProject
from bigboss.store import Store

class GatedWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.store.apply_reconciled([ResolvedProject(
            canonical_path="C:/dev/sampleapp", slug="sampleapp", family="sampleapp", markers={".git": True},
            git_commit_count=5, last_activity_at="2026-07-01T00:00:00Z", status="active",
            purpose="an AI media workbench", domain="", sources=["fs-scan"], aliases=[])])
        d = self.store.enroll_device("Workstation", method="auto-local")
        self.device = {"id": d["device_id"], "name": d["device_name"]}

    def tearDown(self):
        self.tmp.cleanup()

    def _pin(self, slug="sampleapp"):
        for p in self.store.list_projects(include_ambiguous=True):
            if (p.get("slug") or "").lower() == slug:
                return bool(p.get("pinned")), p.get("pin_rank")
        return None

    def test_propose_raises_pending_card_and_does_not_mutate(self):
        self.assertEqual(self._pin(), (False, None))  # starts unpinned
        out = agent_tools.execute("propose_reprioritize", {"slug": "sampleapp", "rank": 1}, self.store)
        self.assertEqual(out["status"], "approval_requested")
        appr = self.store.get_approval(out["approval_id"])
        self.assertEqual(appr["status"], "pending")                       # gated, not auto-applied
        self.assertEqual(appr["proposed_action"]["kind"], "agent_reprioritize")
        self.assertEqual(self._pin(), (False, None))                       # NOTHING mutated yet

    def test_human_approval_applies_the_pin(self):
        out = agent_tools.execute("propose_reprioritize", {"slug": "sampleapp", "rank": 2}, self.store)
        self.store.resolve_approval(out["approval_id"], "approve_once", "", self.device)
        self.assertEqual(self._pin(), (True, 2))                           # applied only after approval

    def test_reject_is_a_noop(self):
        out = agent_tools.execute("propose_reprioritize", {"slug": "sampleapp", "rank": 1}, self.store)
        self.store.resolve_approval(out["approval_id"], "reject", "no", self.device)
        self.assertEqual(self._pin(), (False, None))                       # unchanged

    def test_unknown_slug_raises_no_card(self):
        out = agent_tools.execute("propose_reprioritize", {"slug": "ghost", "rank": 1}, self.store)
        self.assertIn("error", out)
        self.assertNotIn("approval_id", out)

    def _proj(self, slug="sampleapp"):
        return next(p for p in self.store.list_projects(include_ambiguous=True)
                    if (p.get("slug") or "").lower() == slug)

    # --- M6.4b safe writes: exclude ---------------------------------------------------------
    def test_exclude_propose_gates_then_approve_applies(self):
        self.assertFalse(self._proj().get("excluded"))
        out = agent_tools.execute("propose_exclude", {"slug": "sampleapp"}, self.store)
        self.assertEqual(self.store.get_approval(out["approval_id"])["proposed_action"]["kind"], "agent_exclude")
        self.assertFalse(self._proj().get("excluded"))                       # not mutated while pending
        self.store.resolve_approval(out["approval_id"], "approve_once", "", self.device)
        self.assertTrue(self._proj().get("excluded"))                        # applied on approval

    def test_exclude_reject_is_a_noop(self):
        out = agent_tools.execute("propose_exclude", {"slug": "sampleapp"}, self.store)
        self.store.resolve_approval(out["approval_id"], "reject", "no", self.device)
        self.assertFalse(self._proj().get("excluded"))

    # --- M6.4b safe writes: set_context -----------------------------------------------------
    def test_set_context_propose_then_approve_applies(self):
        out = agent_tools.execute("propose_set_context",
                                  {"slug": "sampleapp", "notes": "the operator's north-star note."}, self.store)
        self.assertEqual(self.store.get_approval(out["approval_id"])["proposed_action"]["kind"],
                         "agent_set_context")
        self.assertNotEqual(self._proj().get("notes"), "the operator's north-star note.")  # not applied yet
        self.store.resolve_approval(out["approval_id"], "approve_once", "", self.device)
        self.assertEqual(self._proj().get("notes"), "the operator's north-star note.")

    def test_set_context_requires_notes(self):
        out = agent_tools.execute("propose_set_context", {"slug": "sampleapp"}, self.store)
        self.assertIn("error", out)
        self.assertNotIn("approval_id", out)


if __name__ == "__main__":
    unittest.main()
