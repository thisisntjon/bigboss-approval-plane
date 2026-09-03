import unittest

from bigboss.registry.canonical import (
    Candidate,
    family_key,
    is_project_root,
    reconcile_candidates,
)


def cand(path, *, git=0, markers=None, activity=None, slug=False, sources=None):
    return Candidate(
        path=path,
        markers=markers or {".git": git > 0, "README.md": True},
        git_commit_count=git,
        last_activity_at=activity,
        has_slug_ref=slug,
        sources=sources or ["fs-scan"],
    )


class FamilyKeyTests(unittest.TestCase):
    def test_family_key_groups_council(self):
        self.assertEqual(family_key("the-council-capstone"), family_key("thecouncil"))

    def test_family_key_groups_suffixed_copies(self):
        self.assertEqual(family_key("legacyproj_files"), family_key("legacyproj"))

    def test_family_key_distinct_projects_differ(self):
        self.assertNotEqual(family_key("bigboss"), family_key("sampleapp"))


class ProjectRootTests(unittest.TestCase):
    def test_strong_marker_is_root(self):
        self.assertTrue(is_project_root({".git": True}, has_slug_ref=False))

    def test_weak_marker_needs_slug_ref(self):
        self.assertFalse(is_project_root({"README.md": True}, has_slug_ref=False))
        self.assertTrue(is_project_root({"README.md": True}, has_slug_ref=True))


class ReconcileTests(unittest.TestCase):
    def _by_slug(self, resolved):
        return {r.slug: r for r in resolved}

    def test_multi_copy_project_dedupes_to_one_with_embedded_aliases(self):
        candidates = [
            cand("C:/dev/Python/webshop", markers={"package.json": True, "CLAUDE.md": True}, slug=True),
            cand("C:/dev/sampleapp", git=23, markers={".git": True}),
            cand("C:/dev/sampleapp/webshop", markers={"package.json": True}),
            cand("C:/dev/sampleapp/mcps/webshop", markers={"package.json": True}),
        ]
        resolved = reconcile_candidates(candidates)
        by_slug = self._by_slug(resolved)
        self.assertIn("webshop", by_slug)
        story = by_slug["webshop"]
        self.assertEqual(story.canonical_path, "C:/dev/Python/webshop")
        alias_paths = {a["alias_value"] for a in story.aliases}
        self.assertIn("C:/dev/sampleapp/webshop", alias_paths)
        self.assertIn("C:/dev/sampleapp/mcps/webshop", alias_paths)
        self.assertTrue(all(a["alias_kind"] == "embedded" for a in story.aliases))
        # sampleapp remains its own project.
        self.assertIn("sampleapp", by_slug)

    def test_thecouncil_git_repo_is_canonical(self):
        candidates = [
            cand("C:/dev/Python/the-council-capstone", git=9, markers={".git": True, "package.json": True}),
            cand("C:/dev/Python/thecouncil", markers={"README.md": True}, slug=True),
            cand("C:/dev/Python/thecouncil/thecouncil", markers={"package.json": True}),
            cand("C:/dev/Notes 2026/thecouncil", markers={"README.md": True}, slug=True),
        ]
        resolved = reconcile_candidates(candidates)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].canonical_path, "C:/dev/Python/the-council-capstone")
        self.assertEqual(len(resolved[0].aliases), 3)

    def test_archive_alias_classified(self):
        candidates = [
            cand("C:/dev/Python/legacyproj", markers={"CLAUDE.md": True}, slug=True),
            cand("C:/dev/Dump 2026/Legacyproj", markers={"README.md": True}),
            cand("C:/dev/oldwork", git=23, markers={".git": True}),
            cand("C:/dev/oldwork/legacyproj_files", markers={"README.md": True}),
        ]
        resolved = reconcile_candidates(candidates)
        by_slug = self._by_slug(resolved)
        legacyproj = by_slug["legacyproj"]
        self.assertEqual(legacyproj.canonical_path, "C:/dev/Python/legacyproj")
        kinds = {a["alias_value"]: a["alias_kind"] for a in legacyproj.aliases}
        self.assertEqual(kinds["C:/dev/Dump 2026/Legacyproj"], "archive")
        self.assertEqual(kinds["C:/dev/oldwork/legacyproj_files"], "embedded")

    def test_ambiguous_parent_flagged(self):
        candidates = [
            cand("C:/dev/Python", git=0, markers={".git": True, "CLAUDE.md": True}),
            cand("C:/dev/Python/toolkitx", git=40, markers={".git": True}),
        ]
        resolved = reconcile_candidates(candidates)
        by_slug = self._by_slug(resolved)
        self.assertEqual(by_slug["Python"].status, "ambiguous")
        self.assertEqual(by_slug["toolkitx"].status, "active")

    def test_distinct_projects_stay_separate(self):
        candidates = [
            cand("C:/dev/Python/BigBoss", git=3, markers={".git": True, "pyproject.toml": True}),
            cand("C:/dev/Python/toolkit", git=14, markers={".git": True}),
        ]
        resolved = reconcile_candidates(candidates)
        self.assertEqual(len(resolved), 2)


if __name__ == "__main__":
    unittest.main()
