"""P-ops.3 — harness fleet session-reports: store, CLI, MCP tool, chat agent-tool."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bigboss import agent_tools
from bigboss.cli import main
from bigboss.mcp_stdio import MCPStdioServer
from bigboss.store import Store


class FleetSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_record_and_read_roundtrip(self):
        result = self.store.record_harness_session({
            "harness": "codex", "vendor": "openai", "project": "a sibling project",
            "summary": "wired plan executor", "files_touched": "planner.py",
            "next_steps": ["add retries", "ship"], "decisions": ["use asyncio"],
        })
        self.assertTrue(result["session_id"].startswith("hs_"))
        rows = self.store.list_harness_sessions(days=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["harness"], "codex")
        self.assertEqual(row["vendor"], "openai")
        self.assertEqual(row["files_touched"], ["planner.py"])  # scalar coerced to list
        self.assertEqual(row["next_steps"], ["add retries", "ship"])
        self.assertEqual(row["decisions"], ["use asyncio"])

    def test_missing_harness_defaults_and_extras_preserved(self):
        self.store.record_harness_session({"summary": "x", "model": "gpt-5.4-mini"})
        row = self.store.list_harness_sessions(days=1)[0]
        self.assertEqual(row["harness"], "unknown")
        self.assertEqual(row["detail"].get("model"), "gpt-5.4-mini")

    def test_session_ref_is_idempotent(self):
        first = self.store.record_harness_session(
            {"harness": "codex", "summary": "v1", "session_ref": "sess-1"})
        second = self.store.record_harness_session(
            {"harness": "codex", "summary": "v2", "session_ref": "sess-1"})
        self.assertEqual(first["session_id"], second["session_id"])
        rows = self.store.list_harness_sessions(days=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "v2")  # updated in place

    def test_filters(self):
        self.store.record_harness_session({"harness": "codex", "project": "a sibling project"})
        self.store.record_harness_session({"harness": "gemini-cli", "project": "Squire"})
        self.assertEqual(len(self.store.list_harness_sessions(days=1, project="a sibling project")), 1)
        self.assertEqual(len(self.store.list_harness_sessions(days=1, harness="gemini-cli")), 1)
        self.assertEqual(len(self.store.list_harness_sessions(days=1, project="Nope")), 0)

    def test_reported_event_is_appended(self):
        self.store.record_harness_session({"harness": "codex", "summary": "y"})
        events = self.store.events_after(0, limit=50)
        self.assertTrue(any(e["event_type"] == "harness.session.reported" for e in events))

    def test_host_column_roundtrip_and_filter(self):
        self.store.record_harness_session({"harness": "codex", "host": "lanbox", "summary": "remote"})
        self.store.record_harness_session({"harness": "claude-code", "host": "workstation", "summary": "local"})
        lanbox = self.store.list_harness_sessions(days=1, host="lanbox")
        self.assertEqual(len(lanbox), 1)
        self.assertEqual(lanbox[0]["host"], "lanbox")
        self.assertEqual(len(self.store.list_harness_sessions(days=1, host="workstation")), 1)

    def test_host_updated_on_idempotent_report(self):
        self.store.record_harness_session({"harness": "codex", "host": "lanbox", "session_ref": "x"})
        self.store.record_harness_session({"harness": "codex", "host": "lanbox", "summary": "v2", "session_ref": "x"})
        rows = self.store.list_harness_sessions(days=1, host="lanbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "v2")

    def test_host_migration_on_preexisting_table(self):
        import sqlite3
        from pathlib import Path
        old = Path(self.tempdir.name) / "old.sqlite3"
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE harness_sessions (id TEXT PRIMARY KEY, harness TEXT NOT NULL, "
            "vendor TEXT DEFAULT '', project TEXT DEFAULT '', workspace TEXT DEFAULT '', "
            "title TEXT DEFAULT '', summary TEXT DEFAULT '', payload_json TEXT DEFAULT '{}', "
            "session_ref TEXT DEFAULT '', created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO harness_sessions (id, harness, created_at) VALUES ('hs_o','codex','2026-07-01T00:00:00Z')")
        conn.commit()
        conn.close()
        migrated = Store(old)  # _migrate adds the host column
        rows = migrated.list_harness_sessions()
        self.assertEqual(rows[0]["host"], "")  # old rows default to this-box


class RemoteSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_upsert_keeps_latest_per_host_kind(self):
        self.store.record_remote_snapshot("lanbox", "squire", {"depth": 2}, reported_at="2026-07-05T20:00:00Z")
        self.store.record_remote_snapshot("lanbox", "squire", {"depth": 5}, reported_at="2026-07-05T21:00:00Z")
        snaps = self.store.get_remote_snapshots(kind="squire")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["payload"]["depth"], 5)

    def test_staleness_and_provenance_present(self):
        self.store.record_remote_snapshot("lanbox", "squire", {"live_job": {"label": "TRIAGE"}})
        snap = self.store.get_remote_snapshots(kind="squire")[0]
        self.assertIn("age_seconds", snap)
        self.assertIsNotNone(snap["age_seconds"])
        self.assertEqual(snap["host"], "lanbox")

    def test_kind_filter_and_event(self):
        self.store.record_remote_snapshot("lanbox", "squire", {"depth": 1})
        self.store.record_remote_snapshot("lanbox", "comfyui", {"jobs": 0})
        self.assertEqual(len(self.store.get_remote_snapshots(kind="squire")), 1)
        self.assertEqual(len(self.store.get_remote_snapshots()), 2)
        events = self.store.events_after(0, limit=50)
        self.assertTrue(any(e["event_type"] == "remote.snapshot.reported" for e in events))

    def test_payload_capped(self):
        big = {"blob": "x" * 100_000}
        self.store.record_remote_snapshot("lanbox", "squire", big)
        snap = self.store.get_remote_snapshots(kind="squire")[0]
        # cap_egress_text truncates; the stored blob must be bounded, not the full 100k.
        self.assertLess(len(str(snap["payload"])), 100_000)


class FleetSessionCLITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, *args, stdin=None):
        import sys
        buf = io.StringIO()
        argv = ["--data-dir", self.tempdir.name, *args]
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buf):
                code = main(argv)
        finally:
            sys.stdin = old_stdin
        return code, buf.getvalue()

    def test_report_via_flags_then_list_json(self):
        code, out = self._run(
            "session-report", "--harness", "codex", "--vendor", "openai",
            "--project", "a sibling project", "--summary", "did stuff", "--files", "a.py,b.py",
            "--next-steps", "ship")
        self.assertEqual(code, 0)
        self.assertIn("Recorded session", out)

        code, out = self._run("sessions", "--days", "1", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["harness"], "codex")
        self.assertEqual(rows[0]["files_touched"], ["a.py", "b.py"])

    def test_report_via_stdin(self):
        payload = json.dumps({"harness": "grok", "vendor": "xai", "summary": "s"})
        code, out = self._run("session-report", "--stdin", stdin=payload)
        self.assertEqual(code, 0)
        code, out = self._run("sessions", "--days", "1", "--json")
        self.assertEqual(json.loads(out)[0]["harness"], "grok")

    def test_report_requires_harness(self):
        code, out = self._run("session-report", "--summary", "orphan")
        self.assertEqual(code, 2)
        self.assertIn("harness is required", out)

    def test_sessions_empty_message(self):
        code, out = self._run("sessions", "--days", "1")
        self.assertEqual(code, 0)
        self.assertIn("No harness sessions", out)


class FleetSessionMCPTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.server = MCPStdioServer(self.store)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tool_listed_and_records(self):
        tools = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertIn("bigboss_session_report", names)

        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "bigboss_session_report",
                       "arguments": {"harness": "claude-code", "vendor": "anthropic",
                                     "summary": "added fleet tracking"}},
        })
        session = response["result"]["structuredContent"]["session"]
        self.assertTrue(session["session_id"].startswith("hs_"))
        self.assertEqual(len(self.store.list_harness_sessions(days=1)), 1)


class FleetSessionAgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tool_registered_and_reads_local_state(self):
        self.assertIn("harness_sessions", agent_tools.read_tool_names())
        self.store.record_harness_session({"harness": "codex", "summary": "did stuff"})
        out = agent_tools.execute("harness_sessions", {"days": 1}, self.store)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["sessions"][0]["harness"], "codex")

    def test_tool_never_raises_on_bad_args(self):
        out = agent_tools.execute("harness_sessions", {"days": "not-a-number"}, self.store)
        self.assertEqual(out["window_days"], 7)


if __name__ == "__main__":
    unittest.main()
