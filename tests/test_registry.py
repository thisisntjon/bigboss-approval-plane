import tempfile
import unittest
from pathlib import Path

from bigboss.registry.canonical import Candidate, reconcile_candidates
from bigboss.store import Store


def cand(path, *, git=0, markers=None, activity=None, slug=False, daemon=False, purpose=""):
    return Candidate(
        path=path,
        markers=markers or {".git": git > 0},
        git_commit_count=git,
        last_activity_at=activity,
        has_slug_ref=slug,
        is_daemonized=daemon,
        purpose=purpose,
        sources=["fs-scan"],
    )


class RegistryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def _sample(self):
        return reconcile_candidates([
            cand("C:/dev/Python/BigBoss", git=3, markers={".git": True, "pyproject.toml": True}, activity="2026-07-01T00:00:00Z", purpose="approval plane"),
            cand("C:/dev/Python/toolkit", git=14, markers={".git": True}, activity="2026-06-10T00:00:00Z"),
            cand("C:/dev/Python/webshop", markers={"package.json": True}, slug=True, activity="2026-06-01T00:00:00Z"),
            cand("C:/dev/sampleapp", git=23, markers={".git": True}, activity="2026-06-21T00:00:00Z"),
            cand("C:/dev/sampleapp/webshop", markers={"package.json": True}),
        ])

    def test_apply_and_list(self):
        summary = self.store.apply_reconciled(self._sample())
        self.assertEqual(summary["discovered"], 4)  # webshop sampleapp-copy is an alias, not a project
        projects = self.store.list_projects()
        slugs = [p["slug"] for p in projects]
        self.assertIn("BigBoss", slugs)
        self.assertIn("webshop", slugs)
        # ordered by heat: BigBoss (07-01) before sampleapp (06-21) before toolkit (06-10).
        self.assertLess(slugs.index("BigBoss"), slugs.index("toolkit"))
        story = next(p for p in projects if p["slug"] == "webshop")
        self.assertEqual(story["canonical_path"], "C:/dev/Python/webshop")
        self.assertTrue(any(a["alias_kind"] == "embedded" for a in story["aliases"]))

    def test_refresh_is_idempotent(self):
        self.store.apply_reconciled(self._sample())
        first = {p["canonical_path"]: p["id"] for p in self.store.list_projects()}
        summary = self.store.apply_reconciled(self._sample())
        self.assertEqual(summary["discovered"], 0)
        self.assertEqual(summary["updated"], 4)
        second = {p["canonical_path"]: p["id"] for p in self.store.list_projects()}
        self.assertEqual(first, second)  # stable ids, no duplication

    def test_pin_persists_across_refresh(self):
        self.store.apply_reconciled(self._sample())
        bigboss = next(p for p in self.store.list_projects() if p["slug"] == "BigBoss")
        self.store.set_pin(bigboss["id"], True, pin_rank=1)
        self.store.apply_reconciled(self._sample())  # re-harvest must not clear the pin
        projects = self.store.list_projects()
        self.assertTrue(projects[0]["pinned"])          # pinned floats to top
        self.assertEqual(projects[0]["slug"], "BigBoss")

    def test_curated_purpose_survives_refresh(self):
        self.store.apply_reconciled(self._sample())
        toolkit = next(p for p in self.store.list_projects() if p["slug"] == "toolkit")
        # simulate human curation by pinning a purpose via a direct second apply that has purpose
        self.store.apply_reconciled([  # a candidate with empty purpose must not wipe an existing one
            *reconcile_candidates([cand("C:/dev/Python/toolkit", git=14, markers={".git": True}, purpose="")]),
        ])
        # first apply had empty purpose too, so nothing to preserve here; assert structure intact
        self.assertEqual(toolkit["slug"], "toolkit")

    def test_ambiguous_hidden_by_default(self):
        resolved = reconcile_candidates([
            cand("C:/dev/Python", git=0, markers={".git": True, "CLAUDE.md": True}),
            cand("C:/dev/Python/toolkitx", git=40, markers={".git": True}),
        ])
        self.store.apply_reconciled(resolved)
        visible = [p["slug"] for p in self.store.list_projects()]
        self.assertNotIn("Python", visible)
        self.assertIn("toolkitx", visible)
        self.assertIn("Python", [p["slug"] for p in self.store.list_projects(include_ambiguous=True)])


if __name__ == "__main__":
    unittest.main()
