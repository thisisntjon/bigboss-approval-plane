import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bigboss.crawler import (
    PER_FILE_CHARS,
    collect_sources,
    crawl_portfolio,
    crawl_project,
    resolve_project_path,
    source_hash,
)
from bigboss.mcp_stdio import MCPStdioServer, tool_definitions
from bigboss.store import Store


def resolved_project(path: str, slug: str) -> SimpleNamespace:
    return SimpleNamespace(
        canonical_path=path, slug=slug, purpose="", domain="", status="active",
        is_daemonized=False, git_commit_count=3,
        last_activity_at="2026-07-01T00:00:00Z", last_git_commit_at=None,
        last_transcript_at=None, last_handoff_at=None,
        markers={".git": True}, aliases=[],
    )


class CrawlProjectTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "workflow").mkdir()
        (self.root / "workflow" / "PLAN.md").write_text("Plan body", encoding="utf-8")
        (self.root / "README.md").write_text("Readme body", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_bundle_shape_and_priority_order(self):
        bundle = crawl_project(self.root)
        self.assertTrue(bundle["exists"])
        self.assertEqual([f["rel"] for f in bundle["files"]], ["workflow/PLAN.md", "README.md"])
        self.assertEqual(bundle["files"][0]["text"], "Plan body")
        self.assertTrue(bundle["source_hash"])
        self.assertEqual(bundle["total_chars"], len("Plan body") + len("Readme body"))

    def test_include_text_false_strips_text_but_hash_is_stable(self):
        full = crawl_project(self.root, include_text=True)
        lean = crawl_project(self.root, include_text=False)
        self.assertNotIn("text", lean["files"][0])
        self.assertEqual(full["source_hash"], lean["source_hash"])

    def test_truncation_is_flagged(self):
        (self.root / "PLAN.md").write_text("x" * (PER_FILE_CHARS + 1), encoding="utf-8")
        bundle = crawl_project(self.root)
        plan = next(f for f in bundle["files"] if f["rel"] == "PLAN.md")
        self.assertTrue(plan["truncated"])
        self.assertEqual(plan["chars"], PER_FILE_CHARS)

    def test_missing_directory_is_empty_not_error(self):
        bundle = crawl_project(self.root / "nope")
        self.assertFalse(bundle["exists"])
        self.assertEqual(bundle["files"], [])
        self.assertEqual(bundle["source_hash"], "")

    def test_collect_sources_pairs_match_bundle_hash(self):
        sources = collect_sources(self.root)
        self.assertEqual(source_hash(sources), crawl_project(self.root)["source_hash"])


class CrawlPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.proj_a = Path(self.tempdir.name) / "alpha"
        self.proj_b = Path(self.tempdir.name) / "beta"
        for p, text in ((self.proj_a, "Alpha plan"), (self.proj_b, "Beta plan")):
            p.mkdir()
            (p / "PLAN.md").write_text(text, encoding="utf-8")
        self.store.apply_reconciled([
            resolved_project(str(self.proj_a).replace("\\", "/"), "alpha"),
            resolved_project(str(self.proj_b).replace("\\", "/"), "beta"),
        ])

    def tearDown(self):
        self.tempdir.cleanup()

    def test_portfolio_carries_registry_metadata_without_text(self):
        bundles = crawl_portfolio(self.store)
        self.assertEqual(len(bundles), 2)
        slugs = {b["project"]["slug"] for b in bundles}
        self.assertEqual(slugs, {"alpha", "beta"})
        self.assertNotIn("text", bundles[0]["files"][0])
        self.assertTrue(all(b["source_hash"] for b in bundles))

    def test_slug_filter_and_text_opt_in(self):
        bundles = crawl_portfolio(self.store, slugs=["ALPHA"], include_text=True)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["files"][0]["text"], "Alpha plan")

    def test_resolve_project_path(self):
        self.assertEqual(
            resolve_project_path(self.store, "beta"),
            str(self.proj_b).replace("\\", "/"),
        )
        with self.assertRaises(KeyError):
            resolve_project_path(self.store, "missing")

    def test_mcp_crawl_tools(self):
        server = MCPStdioServer(self.store)
        names = [t["name"] for t in tool_definitions()]
        self.assertIn("crawl_project", names)
        self.assertIn("crawl_portfolio", names)

        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "crawl_project", "arguments": {"slug": "alpha"}}}
        )
        bundle = response["result"]["structuredContent"]["bundle"]
        self.assertEqual(bundle["files"][0]["text"], "Alpha plan")

        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "crawl_portfolio", "arguments": {}}}
        )
        bundles = response["result"]["structuredContent"]["bundles"]
        self.assertEqual(len(bundles), 2)
        self.assertNotIn("text", bundles[0]["files"][0])

    def test_mcp_crawl_project_unknown_slug_is_error(self):
        server = MCPStdioServer(self.store)
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "crawl_project", "arguments": {"slug": "missing"}}}
        )
        self.assertIn("error", response)


if __name__ == "__main__":
    unittest.main()
