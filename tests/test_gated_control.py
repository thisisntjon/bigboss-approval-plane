"""Gated-write phases: cross-machine control (remote_command) + M6.4b-reap + P-ops.2b daemon.

All exercise the propose->human-approve->apply spine: the proposer raises a card and mutates
NOTHING; the effect happens only in resolve_approval on approve. Side effects (process kill,
subprocess restart, remote command) are mocked so nothing real is touched.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bigboss import agent_tools
from bigboss import daemon_registry
from bigboss import procman
from bigboss.policy import classify_action
from bigboss.store import Store


def _device(store):
    pair = store.create_pair_code("Test")
    claimed = store.claim_pair_code(pair["code"])
    return store.authenticate_device(claimed["auth_token"])


class PolicyRiskMapTests(unittest.TestCase):
    def test_reap_is_high_remote_is_medium_all_pending(self):
        self.assertEqual(classify_action({"kind": "reap_process"}).risk_level, "high")
        self.assertEqual(classify_action({"kind": "agent_reap"}).risk_level, "high")
        self.assertEqual(classify_action({"kind": "remote_command"}).risk_level, "medium")
        self.assertEqual(classify_action({"kind": "daemon_restart"}).risk_level, "medium")
        self.assertEqual(classify_action({"kind": "agent_reprioritize"}).risk_level, "low")
        for k in ("reap_process", "remote_command", "daemon_restart", "agent_reprioritize"):
            self.assertEqual(classify_action({"kind": k}).status, "pending")  # never auto-allowed


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.device = _device(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_propose_only_then_issue_on_approve(self):
        out = agent_tools.execute("propose_remote_command", {"action": "queue.pause", "host": "lanbox"}, self.store)
        self.assertEqual(out["status"], "approval_requested")
        # nothing issued yet
        self.assertEqual(len(self.store.list_remote_commands()), 0)
        self.store.resolve_approval(out["approval_id"], "approve_once", "go", self.device)
        cmds = self.store.list_remote_commands("lanbox")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["action"], "queue.pause")
        self.assertEqual(cmds[0]["status"], "pending")

    def test_reject_issues_nothing(self):
        out = agent_tools.execute("propose_remote_command", {"action": "queue.resume"}, self.store)
        self.store.resolve_approval(out["approval_id"], "reject", "no", self.device)
        self.assertEqual(len(self.store.list_remote_commands()), 0)

    def test_unknown_action_rejected_at_propose(self):
        out = agent_tools.execute("propose_remote_command", {"action": "rm -rf /"}, self.store)
        self.assertIn("error", out)
        self.assertEqual(len(self.store.list_approvals(status="all")), 0)

    def test_job_action_requires_job_id(self):
        out = agent_tools.execute("propose_remote_command", {"action": "job.cancel", "args": {}}, self.store)
        self.assertIn("error", out)

    def test_claim_and_ack_lifecycle(self):
        cid = self.store.record_remote_command({"host": "lanbox", "action": "queue.pause"})["command_id"]
        claimed = self.store.claim_pending_commands("lanbox")
        self.assertEqual([c["id"] for c in claimed], [cid])
        self.assertEqual(self.store.ack_command(cid, "acked", {"ok": True})["status"], "acked")
        self.assertTrue(self.store.ack_command(cid, "acked")["already"])  # idempotent
        self.assertFalse(self.store.ack_command("cmd_nope", "acked")["ok"])


class ReapGatedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.device = _device(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _orphan(self, pid, ct):
        return procman.ProcInfo(pid=pid, ppid=999999, name="python.exe", create_time=ct, exe="")

    def test_propose_reap_proposes_current_orphans_and_kills_nothing(self):
        fake = [self._orphan(4242, 100)]
        with mock.patch.object(procman, "list_processes", return_value=fake), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()):
            out = agent_tools.execute("propose_reap", {}, self.store)
        self.assertEqual(out["status"], "approval_requested")
        appr = self.store.get_approval(out["approval_id"])
        self.assertEqual(appr["risk_level"], "high")
        self.assertEqual([t["pid"] for t in appr["proposed_action"]["targets"]], [4242])

    def test_approve_kills_only_still_orphaned_matching_createtime(self):
        fake = [self._orphan(4242, 100)]
        with mock.patch.object(procman, "list_processes", return_value=fake), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()):
            out = agent_tools.execute("propose_reap", {}, self.store)
        killed = []
        # At approval time the PID is still orphaned with the same create_time → killed.
        with mock.patch.object(procman, "list_processes", return_value=fake), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()), \
             mock.patch("bigboss.ops.pid_is_alive", return_value=True), \
             mock.patch("bigboss.ops.terminate_pid", side_effect=lambda p: killed.append(p) or True):
            self.store.resolve_approval(out["approval_id"], "approve_once", "reap", self.device)
        self.assertEqual(killed, [4242])
        events = [e["event_type"] for e in self.store.events_after(0, limit=100)]
        self.assertIn("reap.executed", events)

    def test_pid_reuse_between_propose_and_approve_is_skipped(self):
        proposed = [self._orphan(4242, 100)]
        with mock.patch.object(procman, "list_processes", return_value=proposed), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()):
            out = agent_tools.execute("propose_reap", {}, self.store)
        # At approval time PID 4242 is a DIFFERENT process (newer create_time) → must be skipped.
        reused = [self._orphan(4242, 555)]
        killed = []
        with mock.patch.object(procman, "list_processes", return_value=reused), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()), \
             mock.patch("bigboss.ops.pid_is_alive", return_value=True), \
             mock.patch("bigboss.ops.terminate_pid", side_effect=lambda p: killed.append(p) or True):
            self.store.resolve_approval(out["approval_id"], "approve_once", "reap", self.device)
        self.assertEqual(killed, [])  # nothing killed — PID was reused
        events = [e["event_type"] for e in self.store.events_after(0, limit=100)]
        self.assertIn("reap.skipped", events)

    def test_no_orphans_no_card(self):
        with mock.patch.object(procman, "list_processes", return_value=[]), \
             mock.patch.object(procman, "current_protect_pids", return_value=set()):
            out = agent_tools.execute("propose_reap", {}, self.store)
        self.assertIn("info", out)
        self.assertEqual(len(self.store.list_approvals(status="all")), 0)


class DaemonRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.device = _device(self.store)
        self.reg = Path(self.tmp.name) / "daemons.json"
        self.reg.write_text(json.dumps({"toolkit": {"project": "toolkit", "restart_cmd": "keep"}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_is_merge_preserving(self):
        daemon_registry.register(path=self.reg)
        data = json.loads(self.reg.read_text())
        self.assertIn("toolkit", data)                 # foreign entry preserved
        self.assertIn("bigboss-router", data)          # BigBoss's own added

    def test_non_elevated_restart_runs_command_on_approve(self):
        daemon_registry.register(path=self.reg)
        with mock.patch.object(daemon_registry, "get_entry", side_effect=lambda n, path=None: _raw(self.reg, n)):
            out = agent_tools.execute("propose_daemon_restart", {"name": "bigboss-router"}, self.store)
        ran = {}
        def _run(*a, **k):
            ran["cmd"] = a
            return _Proc(0)
        with mock.patch("subprocess.run", side_effect=_run):
            self.store.resolve_approval(out["approval_id"], "approve_once", "restart", self.device)
        self.assertIn("cmd", ran)
        events = [e["event_type"] for e in self.store.events_after(0, limit=100)]
        self.assertIn("daemon.restarted", events)

    def test_elevated_restart_emits_pasteblock_and_runs_nothing(self):
        d = json.loads(self.reg.read_text())
        d["career"] = {"project": "Career", "restart_cmd": "Start-ScheduledTask X", "restart_elevated": True}
        self.reg.write_text(json.dumps(d))
        with mock.patch.object(daemon_registry, "get_entry", side_effect=lambda n, path=None: _raw(self.reg, n)):
            out = agent_tools.execute("propose_daemon_restart", {"name": "career"}, self.store)
        with mock.patch("subprocess.run", side_effect=AssertionError("must NOT run an elevated restart")):
            self.store.resolve_approval(out["approval_id"], "approve_once", "restart", self.device)
        events = [e["event_type"] for e in self.store.events_after(0, limit=100)]
        self.assertIn("daemon.restart.elevation_required", events)


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stderr = ""
        self.stdout = ""


def _raw(reg_path, name):
    data = json.loads(reg_path.read_text())
    entry = data.get(name)
    return {"name": name, **entry} if isinstance(entry, dict) else None


if __name__ == "__main__":
    unittest.main()
