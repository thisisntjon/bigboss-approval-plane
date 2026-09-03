"""E4a.1 (brainz boundary hygiene): the no_crawl exclusion must hold on every
crawl/digest path, survive registry re-discovery, retroactively redact stored
egress text, and the default data dir must be pinned (not cwd-relative)."""

import importlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from bigboss.crawler import crawl_portfolio, refuse_no_crawl_path, resolve_project_path
from bigboss.intel.digest import refresh_intel
from bigboss.intel.squire import SquireClient
from bigboss.store import Store


GOOD_INTEL = {
    "status_line": "ok",
    "blockers": [],
    "roadmap_now": "now",
    "roadmap_next": "next",
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
            self._json(
                {
                    "model": "google/gemma-4-e4b",
                    "choices": [
                        {"message": {"role": "assistant", "content": json.dumps(GOOD_INTEL)}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
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
        canonical_path=path,
        slug=slug,
        purpose="",
        domain="",
        status="active",
        is_daemonized=False,
        git_commit_count=3,
        last_activity_at="2026-07-01T00:00:00Z",
        last_git_commit_at=None,
        last_transcript_at=None,
        last_handoff_at=None,
        markers={".git": True},
        aliases=[],
    )


class _NoCrawlFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.project_dir = Path(self.tempdir.name) / "brainz"
        self.project_dir.mkdir()
        (self.project_dir / "PLAN.md").write_text("private planning text", encoding="utf-8")
        self.other_dir = Path(self.tempdir.name) / "other"
        self.other_dir.mkdir()
        (self.other_dir / "PLAN.md").write_text("public planning text", encoding="utf-8")
        self.store.apply_reconciled(
            [
                resolved_project(str(self.project_dir).replace("\\", "/"), "brainz"),
                resolved_project(str(self.other_dir).replace("\\", "/"), "other"),
            ]
        )
        # These fixtures model already-known projects; clear the E4a.1 discovery
        # digest gate so digestion behaves as before the gate existed.
        self.store.clear_digest_pending()

    def tearDown(self):
        self.tempdir.cleanup()

    def _events(self, event_type):
        return [e for e in self.store.events_after(0, limit=200) if e["event_type"] == event_type]

    def _project(self, slug):
        return next(p for p in self.store.list_projects() if p["slug"] == slug)


class SetNoCrawlTests(_NoCrawlFixture):
    def test_exclude_sets_flag_and_emits_event(self):
        project = self.store.set_no_crawl("brainz", True)
        self.assertTrue(project["no_crawl"])
        self.assertTrue(self._project("brainz")["no_crawl"])
        self.assertEqual(len(self._events("project.crawl_excluded")), 1)

    def test_reallow_clears_flag_and_emits_event(self):
        self.store.set_no_crawl("brainz", True)
        project = self.store.set_no_crawl("brainz", False)
        self.assertFalse(project["no_crawl"])
        self.assertEqual(len(self._events("project.crawl_allowed")), 1)

    def test_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            self.store.set_no_crawl("nope", True)

    def test_exclusion_redacts_stored_egress_text_but_keeps_audit_metadata(self):
        project = self._project("brainz")
        self.store.record_egress(
            endpoint="squire",
            client="bigboss-intel",
            purpose="digest",
            project_id=project["id"],
            project_slug="brainz",
            source_files=["workflow/PLAN.md"],
            content_hash="abc123",
            chars_sent=42,
            prompt_text="private planning text",
            response_text="digested private text",
        )
        result = self.store.set_no_crawl("brainz", True)
        self.assertEqual(result["egress_rows_redacted"], 1)
        rows = self.store.list_egress(days=7)
        row = next(r for r in rows if r["project_slug"] == "brainz")
        self.assertEqual(row["prompt_text"], "")
        self.assertEqual(row["response_text"], "")
        self.assertEqual(row["content_hash"], "abc123")
        self.assertEqual(row["chars_sent"], 42)

    def test_exclusion_drops_the_intel_snapshot(self):
        project = self._project("brainz")
        self.store.upsert_project_intel(
            project["id"],
            {"status_line": "derived from private docs", "blockers": [], "source_hash": "h"},
        )
        self.store.set_no_crawl("brainz", True)
        self.assertIsNone(self._project("brainz")["intel"])

    def test_flag_survives_registry_rediscovery(self):
        self.store.set_no_crawl("brainz", True)
        self.store.apply_reconciled(
            [resolved_project(str(self.project_dir).replace("\\", "/"), "brainz")]
        )
        self.assertTrue(self._project("brainz")["no_crawl"])


class MigrationTests(unittest.TestCase):
    def test_old_schema_gains_no_crawl_column(self):
        import sqlite3

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        db = Path(tempdir.name) / "bigboss.sqlite3"
        conn = sqlite3.connect(db)
        # Pre-E4a.1 projects table: no no_crawl column.
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL,
                canonical_path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'unknown',
                purpose TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active', pinned INTEGER NOT NULL DEFAULT 0,
                pin_rank INTEGER, is_daemonized INTEGER NOT NULL DEFAULT 0,
                git_commit_count INTEGER NOT NULL DEFAULT 0, last_activity_at TEXT,
                last_git_commit_at TEXT, last_transcript_at TEXT, last_handoff_at TEXT,
                markers_json TEXT NOT NULL DEFAULT '{}', first_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()
        store = Store(db)
        with store.connect() as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(projects)").fetchall()}
        self.assertIn("no_crawl", cols)
        self.assertEqual(store.list_projects(), [])


class CrawlPathTests(_NoCrawlFixture):
    def setUp(self):
        super().setUp()
        self.store.set_no_crawl("brainz", True)

    def test_crawl_portfolio_skips_excluded_by_default(self):
        bundles = crawl_portfolio(self.store)
        self.assertEqual([b["project"]["slug"] for b in bundles], ["other"])

    def test_crawl_portfolio_returns_visible_refusal_for_explicit_slug(self):
        bundles = crawl_portfolio(self.store, slugs=["brainz"])
        self.assertEqual(len(bundles), 1)
        self.assertTrue(bundles[0]["excluded"])
        self.assertEqual(bundles[0]["files"], [])
        self.assertEqual(bundles[0]["source_hash"], "")

    def test_resolve_project_path_refuses_excluded_slug(self):
        with self.assertRaises(KeyError):
            resolve_project_path(self.store, "brainz")
        self.assertTrue(resolve_project_path(self.store, "other"))

    def test_refuse_no_crawl_path_blocks_root_and_subpaths(self):
        with self.assertRaises(KeyError):
            refuse_no_crawl_path(self.store, self.project_dir)
        with self.assertRaises(KeyError):
            refuse_no_crawl_path(self.store, self.project_dir / "workflow" / "PLAN.md")
        refuse_no_crawl_path(self.store, self.other_dir)  # must not raise


class RefreshIntelNoCrawlTests(_NoCrawlFixture):
    def setUp(self):
        super().setUp()
        self.store.set_no_crawl("brainz", True)
        self.squire = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSquireHandler)
        self.squire.chat_count = 0
        threading.Thread(target=self.squire.serve_forever, daemon=True).start()
        host, port = self.squire.server_address
        self.client = SquireClient(base_url=f"http://{host}:{port}/v1", timeout=10)

    def tearDown(self):
        self.squire.shutdown()
        self.squire.server_close()
        super().tearDown()

    def test_excluded_project_is_never_digested(self):
        summary = refresh_intel(self.store, client=self.client)
        self.assertEqual(summary["skipped_no_crawl"], 1)
        self.assertEqual(summary["digested"], 1)  # 'other' still digests
        self.assertEqual(self.squire.chat_count, 1)
        self.assertIsNone(self._project("brainz")["intel"])

    def test_explicit_slug_request_does_not_override_exclusion(self):
        summary = refresh_intel(self.store, client=self.client, slugs=["brainz"], force=True)
        self.assertEqual(summary["skipped_no_crawl"], 1)
        self.assertEqual(summary["digested"], 0)
        self.assertEqual(self.squire.chat_count, 0)


class DataDirPinningTests(unittest.TestCase):
    def test_default_data_dir_is_absolute_and_not_cwd_relative(self):
        import bigboss.cli as cli

        self.assertTrue(cli.DEFAULT_DATA_DIR.is_absolute())
        if not os.environ.get("BIGBOSS_DATA_DIR"):
            self.assertEqual(cli.DEFAULT_DATA_DIR, cli.REPO_ROOT / ".bigboss")

    def test_env_override_wins(self):
        import bigboss.cli as cli

        old = os.environ.get("BIGBOSS_DATA_DIR")
        os.environ["BIGBOSS_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "bb-test-data")
        try:
            importlib.reload(cli)
            self.assertEqual(cli.DEFAULT_DATA_DIR, Path(tempfile.gettempdir()) / "bb-test-data")
        finally:
            if old is None:
                os.environ.pop("BIGBOSS_DATA_DIR", None)
            else:
                os.environ["BIGBOSS_DATA_DIR"] = old
            importlib.reload(cli)


if __name__ == "__main__":
    unittest.main()
