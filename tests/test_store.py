import tempfile
import unittest
from pathlib import Path

from bigboss.contracts import action_hash
from bigboss.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_pair_claim_create_and_resolve_approval(self):
        pair = self.store.create_pair_code("Phone")
        device = self.store.claim_pair_code(pair["code"])
        authed = self.store.authenticate_device(device["auth_token"])
        self.assertIsNotNone(authed)
        self.assertTrue(self.store.csrf_is_valid(authed, device["csrf_token"]))

        approval = self.store.create_approval_request(
            {
                "run_id": "run_test",
                "harness": "codex",
                "workspace": self.tempdir.name,
                "title": "Approve install",
                "summary": "Install dependencies.",
                "proposed_action": {"kind": "shell_command", "command": "uv sync"},
            }
        )
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(
            approval["action_hash"],
            action_hash({"kind": "shell_command", "command": "uv sync"}, self.tempdir.name),
        )

        resolved = self.store.resolve_approval(approval["id"], "approve_once", "OK", authed)
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(resolved["latest_decision"]["decision"], "approve_once")
        self.assertEqual(resolved["latest_decision"]["note"], "OK")

        events = self.store.events_after(0)
        self.assertGreaterEqual(len(events), 3)

    def test_auto_allowed_request_is_not_pending(self):
        approval = self.store.create_approval_request(
            {
                "run_id": "run_test",
                "harness": "codex",
                "workspace": self.tempdir.name,
                "title": "Read diff",
                "summary": "Read git diff.",
                "proposed_action": {"kind": "git_diff"},
            }
        )
        self.assertEqual(approval["status"], "auto_allowed")

    def test_submit_update_appends_event(self):
        update = self.store.submit_update(
            {
                "run_id": "run_update",
                "harness": "codex",
                "workspace": self.tempdir.name,
                "title": "Working",
                "summary": "Still running tests.",
            }
        )
        self.assertEqual(update["run_id"], "run_update")
        events = self.store.events_after(0)
        self.assertEqual(events[-1]["event_type"], "run.updated")


if __name__ == "__main__":
    unittest.main()
