import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from bigboss.intel.digest import (
    PER_FILE_CHARS,
    collect_sources,
    parse_intel,
    refresh_intel,
    source_hash,
)
from bigboss.intel.squire import SquireClient, SquireError
from bigboss.mcp_stdio import MCPStdioServer
from bigboss.store import Store


GOOD_INTEL = {
    "status_line": "Phase E1 activation pending; approval plane is stable.",
    "blockers": ["Prepaid key not yet pointed at the router."],
    "roadmap_now": "E1 activation",
    "roadmap_next": "E2 escalation gate",
}


class _FakeSquireHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"data": [{"id": "google/gemma-4-e4b"}]})
            return
        self._json({}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/v1/chat/completions":
            self.server.chat_count += 1
            content = self.server.chat_replies[
                min(self.server.chat_count - 1, len(self.server.chat_replies) - 1)
            ]
            self._json(
                {
                    "model": "google/gemma-4-e4b",
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 60},
                }
            )
            return
        self._json({}, status=404)

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def resolved_project(path: str, slug: str) -> SimpleNamespace:
    return SimpleNamespace(
        canonical_path=path, slug=slug, purpose="", domain="", status="active",
        is_daemonized=False, git_commit_count=3,
        last_activity_at="2026-07-01T00:00:00Z", last_git_commit_at=None,
        last_transcript_at=None, last_handoff_at=None,
        markers={".git": True}, aliases=[],
    )


class SourceCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_planning_docs_come_first_and_empty_files_are_skipped(self):
        (self.root / "README.md").write_text("Readme text", encoding="utf-8")
        (self.root / "workflow").mkdir()
        (self.root / "workflow" / "PLAN.md").write_text("Plan text", encoding="utf-8")
        (self.root / "TODO.md").write_text("   \n", encoding="utf-8")
        sources = collect_sources(self.root)
        self.assertEqual([rel for rel, _ in sources], ["workflow/PLAN.md", "README.md"])

    def test_per_file_cap_is_applied(self):
        (self.root / "PLAN.md").write_text("x" * (PER_FILE_CHARS + 500), encoding="utf-8")
        sources = collect_sources(self.root)
        self.assertEqual(len(sources[0][1]), PER_FILE_CHARS)

    def test_hash_changes_with_content(self):
        (self.root / "PLAN.md").write_text("v1", encoding="utf-8")
        h1 = source_hash(collect_sources(self.root))
        (self.root / "PLAN.md").write_text("v2", encoding="utf-8")
        h2 = source_hash(collect_sources(self.root))
        self.assertNotEqual(h1, h2)


class ParseIntelTests(unittest.TestCase):
    def test_plain_json(self):
        intel = parse_intel(json.dumps(GOOD_INTEL))
        self.assertEqual(intel["status_line"], GOOD_INTEL["status_line"])
        self.assertEqual(intel["blockers"], GOOD_INTEL["blockers"])

    def test_fenced_json_with_preamble(self):
        text = "Here is the analysis:\n```json\n" + json.dumps(GOOD_INTEL) + "\n```"
        intel = parse_intel(text)
        self.assertEqual(intel["roadmap_now"], "E1 activation")

    def test_non_list_blockers_are_coerced(self):
        intel = parse_intel(json.dumps({"status_line": "ok", "blockers": "one thing"}))
        self.assertEqual(intel["blockers"], ["one thing"])

    def test_no_json_raises(self):
        with self.assertRaises(SquireError):
            parse_intel("I could not determine the status.")


class _SquireFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

        self.project_dir = Path(self.tempdir.name) / "proj"
        self.project_dir.mkdir()
        (self.project_dir / "PLAN.md").write_text("Phase E1 is pending activation.", encoding="utf-8")
        self.store.apply_reconciled(
            [resolved_project(str(self.project_dir).replace("\\", "/"), "proj")]
        )
        # Fixture models an already-known project; clear the E4a.1 discovery digest
        # gate so digestion behaves as before the gate existed.
        self.store.clear_digest_pending()

        self.squire = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSquireHandler)
        self.squire.chat_count = 0
        self.squire.chat_replies = [json.dumps(GOOD_INTEL)]
        threading.Thread(target=self.squire.serve_forever, daemon=True).start()
        host, port = self.squire.server_address
        self.client = SquireClient(base_url=f"http://{host}:{port}/v1", timeout=10)

    def tearDown(self):
        self.squire.shutdown()
        self.squire.server_close()
        self.tempdir.cleanup()

    def _events(self, event_type):
        return [e for e in self.store.events_after(0, limit=200) if e["event_type"] == event_type]


class RefreshIntelTests(_SquireFixture):
    def test_refresh_digests_and_projects_carry_intel(self):
        summary = refresh_intel(self.store, client=self.client)
        self.assertTrue(summary["squire_up"])
        self.assertEqual(summary["digested"], 1)
        project = self.store.list_projects()[0]
        self.assertEqual(project["intel"]["status_line"], GOOD_INTEL["status_line"])
        self.assertEqual(project["intel"]["blockers"], GOOD_INTEL["blockers"])
        self.assertEqual(project["intel"]["source_files"], ["PLAN.md"])
        self.assertEqual(len(self._events("intel.updated")), 1)
        status = self.store.squire_status()
        self.assertTrue(status["up"])
        self.assertEqual(status["clients"][0]["client"], "bigboss-intel")
        self.assertEqual(status["clients"][0]["prompt_tokens"], 120)

    def test_unchanged_docs_skip_the_squire_call(self):
        refresh_intel(self.store, client=self.client)
        summary = refresh_intel(self.store, client=self.client)
        self.assertEqual(summary["skipped_unchanged"], 1)
        self.assertEqual(self.squire.chat_count, 1)  # second run never hit Squire

    def test_digest_pending_project_is_not_sent_off_box(self):
        # A newly-discovered (gated) project must never reach Squire until approved.
        gated_dir = self.project_dir.parent / "gated"
        gated_dir.mkdir()
        (gated_dir / "PLAN.md").write_text("secret new project docs", encoding="utf-8")
        self.store.apply_reconciled(
            [resolved_project(str(gated_dir).replace("\\", "/"), "gated")]
        )  # digest_pending=1, NOT cleared
        summary = refresh_intel(self.store, client=self.client)
        self.assertEqual(summary["skipped_pending"], 1)
        self.assertEqual(summary["digested"], 1)  # only the known 'proj' digested
        self.assertIsNone(
            next(p for p in self.store.list_projects() if p["slug"] == "gated")["intel"]
        )

    def test_force_redigests_unchanged_docs(self):
        refresh_intel(self.store, client=self.client)
        summary = refresh_intel(self.store, client=self.client, force=True)
        self.assertEqual(summary["digested"], 1)
        self.assertEqual(self.squire.chat_count, 2)

    def test_changed_docs_trigger_a_new_digest(self):
        refresh_intel(self.store, client=self.client)
        (self.project_dir / "PLAN.md").write_text("Phase E2 has begun.", encoding="utf-8")
        summary = refresh_intel(self.store, client=self.client)
        self.assertEqual(summary["digested"], 1)

    def test_garbage_reply_marks_error_but_keeps_last_snapshot(self):
        refresh_intel(self.store, client=self.client)
        (self.project_dir / "PLAN.md").write_text("Docs changed.", encoding="utf-8")
        self.squire.chat_replies = ["not json at all"]
        self.squire.chat_count = 0
        summary = refresh_intel(self.store, client=self.client)
        self.assertEqual(summary["failed"], 1)
        intel = self.store.list_projects()[0]["intel"]
        self.assertEqual(intel["status_line"], GOOD_INTEL["status_line"])  # preserved
        self.assertIsNotNone(intel["error"])

    def test_squire_down_aborts_cleanly_and_ledgers_health(self):
        down = SquireClient(base_url="http://127.0.0.1:1/v1", timeout=2)
        summary = refresh_intel(self.store, client=down)
        self.assertFalse(summary["squire_up"])
        self.assertEqual(summary["digested"], 0)
        self.assertFalse(self.store.squire_status()["up"])
        self.assertEqual(len(self._events("squire.health.changed")), 1)

    def test_mcp_portfolio_intel_tool(self):
        refresh_intel(self.store, client=self.client)
        server = MCPStdioServer(self.store)
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "portfolio_intel", "arguments": {}}}
        )
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["projects"][0]["intel"]["roadmap_now"], "E1 activation")
        self.assertIn("up", payload["squire"])

    def test_beastmode_endpoint_labels_separately_and_never_auto(self):
        from bigboss.intel.endpoints import Endpoint, get_endpoint

        beast = Endpoint(
            name="beastmode", base_url=self.client.base_url, model=self.client.model,
            auto_ok=False, min_interval_s=0.0,
        )
        summary = refresh_intel(self.store, client=self.client, endpoint=beast)
        self.assertEqual(summary["endpoint"], "beastmode")
        self.assertEqual(summary["digested"], 1)

        status = self.store.squire_status()
        endpoints = {e["endpoint"]: e for e in status["endpoints"]}
        self.assertIn("beastmode", endpoints)
        self.assertEqual(endpoints["beastmode"]["calls"], 1)
        clients = {c["client"] for c in status["clients"]}
        self.assertIn("bigboss-intel-beastmode", clients)

        # Hard rule: the real beastmode endpoint is never auto-usable.
        self.assertFalse(get_endpoint("beastmode").auto_ok)
        self.assertTrue(get_endpoint("squire").auto_ok)

    def test_per_endpoint_health_is_independent(self):
        # Squire up, beastmode down -> two independent health rows + transitions.
        refresh_intel(self.store, client=self.client)
        down = SquireClient(base_url="http://127.0.0.1:1/v1", timeout=2)
        from bigboss.intel.endpoints import Endpoint

        beast = Endpoint(name="beastmode", base_url=down.base_url, model="x",
                         auto_ok=False, min_interval_s=0.0)
        refresh_intel(self.store, client=down, endpoint=beast)
        status = self.store.squire_status()
        endpoints = {e["endpoint"]: e for e in status["endpoints"]}
        self.assertTrue(endpoints["squire"]["up"])
        self.assertFalse(endpoints["beastmode"]["up"])
        self.assertTrue(status["up"])  # top-level reflects squire (primary)


class EgressAuditTests(_SquireFixture):
    """Every payload sent to a compute endpoint lands in the egress audit -
    that is the whole accountability model (no key gating anywhere)."""

    def test_digest_records_readable_prompt_and_response(self):
        refresh_intel(self.store, client=self.client)
        rows = self.store.list_egress()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["endpoint"], "squire")
        self.assertIn("[system]", row["prompt_text"])
        self.assertIn("Phase E1 is pending activation.", row["prompt_text"])
        self.assertIn(GOOD_INTEL["status_line"], row["response_text"])
        self.assertTrue(row["ok"])
        self.assertEqual(len(self._events("egress.recorded")), 1)

    def test_beastmode_sends_carry_the_edr_exposure_tag(self):
        from bigboss.intel.endpoints import Endpoint, get_endpoint

        beast = Endpoint(
            name="beastmode", base_url=self.client.base_url, model=self.client.model,
            auto_ok=False, min_interval_s=0.0, exposure="managed-edr",
        )
        refresh_intel(self.store, client=self.client, endpoint=beast)
        row = self.store.list_egress(endpoint="beastmode")[0]
        self.assertEqual(row["exposure"], "managed-edr")
        self.assertEqual(row["client"], "bigboss-intel-beastmode")
        # The real registry entry ships with the exposure tag by default.
        self.assertEqual(get_endpoint("beastmode").exposure, "managed-edr")

    def test_failed_digest_still_logs_the_prompt(self):
        self.squire.chat_replies = ["not json at all"]
        refresh_intel(self.store, client=self.client)
        row = self.store.list_egress()[0]
        self.assertFalse(row["ok"])
        self.assertIn("Phase E1 is pending activation.", row["prompt_text"])
        self.assertIn("not json", row["response_text"])

    def test_endpoint_status_counts_audited_sends(self):
        refresh_intel(self.store, client=self.client)
        status = self.store.squire_status()
        squire = next(e for e in status["endpoints"] if e["endpoint"] == "squire")
        self.assertEqual(squire["egress_sends"], 1)
        self.assertGreater(squire["egress_chars_sent"], 0)
        self.assertIsNotNone(squire["last_egress_at"])

    def test_mcp_squire_egress_tool(self):
        refresh_intel(self.store, client=self.client)
        server = MCPStdioServer(self.store)
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "squire_egress", "arguments": {"endpoint": "squire"}}}
        )
        payload = response["result"]["structuredContent"]
        self.assertEqual(len(payload["egress"]), 1)
        self.assertEqual(payload["egress"][0]["project_slug"], "proj")


class SquireHealthTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_events_fire_only_on_transitions(self):
        self.assertTrue(self.store.record_squire_health(True)["changed"])   # first observation
        self.assertFalse(self.store.record_squire_health(True)["changed"])  # steady state
        self.assertTrue(self.store.record_squire_health(False, detail="timeout")["changed"])
        events = [e for e in self.store.events_after(0, limit=50) if e["event_type"] == "squire.health.changed"]
        self.assertEqual([e["payload"]["up"] for e in events], [True, False])


if __name__ == "__main__":
    unittest.main()
