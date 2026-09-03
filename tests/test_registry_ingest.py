import tempfile
import unittest
from pathlib import Path

from bigboss.registry.ingest import BundleError, bundle_to_candidates, ingest_bundle
from bigboss.store import Store


def _bundle(projects, host="lanbox", version=1):
    return {"bundle_version": version, "host": host, "generated_at": "2026-07-02T00:00:00Z", "projects": projects}


class BundleParseTests(unittest.TestCase):
    def test_remote_path_is_unc_scoped_by_host(self):
        cands = bundle_to_candidates(_bundle([
            {"path": "V:/alpha", "markers": {".git": True}, "git_commit_count": 3},
        ]))
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].path, "//lanbox/V:/alpha")
        self.assertIn("remote:lanbox", cands[0].sources)
        self.assertTrue(cands[0].has_slug_ref)

    def test_weak_marker_only_remote_project_is_kept(self):
        # A remote README-only dir has no transcript slug we can mine, so the crawl
        # itself is the external reference — it should still count.
        cands = bundle_to_candidates(_bundle([
            {"path": "V:/docsonly", "markers": {"README.md": True}},
        ]))
        self.assertEqual(len(cands), 1)

    def test_markerless_project_is_dropped(self):
        cands = bundle_to_candidates(_bundle([
            {"path": "V:/empty", "markers": {}},
        ]))
        self.assertEqual(cands, [])

    def test_bad_version_and_host_rejected(self):
        with self.assertRaises(BundleError):
            bundle_to_candidates(_bundle([], version=2))
        with self.assertRaises(BundleError):
            bundle_to_candidates(_bundle([], host="the remote host Box"))

    def test_missing_path_rejected(self):
        with self.assertRaises(BundleError):
            bundle_to_candidates(_bundle([{"markers": {".git": True}}]))


class IngestPersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_upserts_remote_projects(self):
        summary = ingest_bundle(self.store, _bundle([
            {"path": "V:/alpha", "markers": {".git": True, "CLAUDE.md": True},
             "git_commit_count": 10, "last_commit_at": "2026-06-01T00:00:00Z",
             "purpose": "The alpha service."},
            {"path": "V:/beta", "markers": {"pyproject.toml": True}, "git_commit_count": 0},
        ]))
        self.assertEqual(summary["host"], "lanbox")
        self.assertEqual(summary["resolved"], 2)
        rows = {r["slug"]: r for r in self.store.list_projects(include_ambiguous=True)}
        self.assertIn("alpha", rows)
        self.assertEqual(rows["alpha"]["canonical_path"], "//lanbox/V:/alpha")
        self.assertEqual(rows["alpha"]["purpose"], "The alpha service.")

    def test_ingest_does_not_wipe_local_projects(self):
        # Seed a local project, then ingest remote — local must survive (upsert, not replace).
        from bigboss.registry.canonical import ResolvedProject
        self.store.apply_reconciled([ResolvedProject(
            canonical_path="C:/Users/x/Desktop/local", slug="local", family="local",
            markers={".git": True}, git_commit_count=5, last_activity_at="2026-06-01T00:00:00Z",
            status="active", purpose="", domain="", sources=["fs-scan"], aliases=[],
        )])
        ingest_bundle(self.store, _bundle([{"path": "V:/alpha", "markers": {".git": True}}]))
        slugs = {r["slug"] for r in self.store.list_projects(include_ambiguous=True)}
        self.assertEqual(slugs, {"local", "alpha"})


def _local(path, slug, pinned=False, commits=3):
    from bigboss.registry.canonical import ResolvedProject
    return ResolvedProject(
        canonical_path=path, slug=slug, family=slug, markers={".git": True},
        git_commit_count=commits, last_activity_at="2026-06-01T00:00:00Z", status="active",
        purpose="", domain="", sources=["fs-scan"], aliases=[],
    )


class PruneScopingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _slugs(self, include_all=True):
        return {r["slug"] for r in self.store.list_projects(include_ambiguous=include_all)}

    def test_local_prune_tombstones_absent_local_but_not_remote(self):
        # Seed two locals + one remote (via ingest).
        self.store.apply_reconciled([_local("C:/dev/keep", "keep"), _local("C:/dev/drop", "drop")])
        ingest_bundle(self.store, _bundle([{"path": "V:/alpha", "markers": {".git": True}}]))
        self.assertEqual(self._slugs(), {"keep", "drop", "alpha"})

        # Refresh with prune, "drop" absent from the local set → tombstoned; remote untouched.
        self.store.apply_reconciled([_local("C:/dev/keep", "keep")], prune_local=True)
        visible = self._slugs(include_all=False)
        self.assertIn("keep", visible)
        self.assertIn("alpha", visible)          # remote //lanbox row survives a LOCAL prune
        self.assertNotIn("drop", visible)        # local absentee tombstoned (hidden)
        # tombstoned row still exists under include_ambiguous
        self.assertIn("drop", self._slugs(include_all=True))

    def test_prune_never_tombstones_pinned(self):
        self.store.apply_reconciled([_local("C:/dev/keep", "keep")])
        rows = {r["slug"]: r for r in self.store.list_projects(include_ambiguous=True)}
        self.store.set_pin(rows["keep"]["id"], pinned=True)
        # keep is absent from an empty refresh, but pinned → must survive.
        self.store.apply_reconciled([], prune_local=True)
        self.assertIn("keep", self._slugs(include_all=False))

    def test_prune_is_opt_in(self):
        self.store.apply_reconciled([_local("C:/dev/keep", "keep"), _local("C:/dev/drop", "drop")])
        # Default (no prune): absent 'drop' stays active.
        self.store.apply_reconciled([_local("C:/dev/keep", "keep")])
        self.assertEqual(self._slugs(include_all=False), {"keep", "drop"})


class RemoteHostPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _slugs(self, include_all=False):
        return {r["slug"] for r in self.store.list_projects(include_ambiguous=include_all)}

    def test_full_sync_retires_absent_host_rows_only(self):
        # Seed: a local row, a different-host remote row, and two lanbox rows.
        self.store.apply_reconciled([_local("C:/dev/localkeep", "localkeep")])
        ingest_bundle(self.store, _bundle([{"path": "V:/other", "markers": {".git": True}}], host="lanbox2"))
        ingest_bundle(self.store, _bundle([
            {"path": "V:/keep", "markers": {".git": True}},
            {"path": "V:/old", "markers": {".git": True}},
        ], host="lanbox"))
        self.assertEqual(self._slugs(), {"localkeep", "other", "keep", "old"})

        # Re-ingest lanbox with only 'keep', prune=True → 'old' retired; everything else intact.
        ingest_bundle(self.store, _bundle([{"path": "V:/keep", "markers": {".git": True}}], host="lanbox"),
                      prune=True)
        visible = self._slugs()
        self.assertEqual(visible, {"localkeep", "other", "keep"})   # old gone; local + lanbox2 untouched
        self.assertIn("old", self._slugs(include_all=True))          # tombstoned, not deleted

    def test_default_ingest_does_not_prune_absent_host_rows(self):
        ingest_bundle(self.store, _bundle([
            {"path": "V:/keep", "markers": {".git": True}},
            {"path": "V:/old", "markers": {".git": True}},
        ], host="lanbox"))
        ingest_bundle(self.store, _bundle([{"path": "V:/keep", "markers": {".git": True}}], host="lanbox"))
        self.assertEqual(self._slugs(), {"keep", "old"})

    def test_pinned_remote_row_is_never_pruned(self):
        ingest_bundle(self.store, _bundle([{"path": "V:/pinme", "markers": {".git": True}}], host="lanbox"))
        rows = {r["slug"]: r for r in self.store.list_projects(include_ambiguous=True)}
        self.store.set_pin(rows["pinme"]["id"], pinned=True)
        ingest_bundle(self.store, _bundle([], host="lanbox"), prune=True)  # pinme absent
        self.assertIn("pinme", self._slugs())


if __name__ == "__main__":
    unittest.main()
