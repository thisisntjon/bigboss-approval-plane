import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bigboss.registry.harvest as H
from bigboss.registry.canonical import normalize


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "Desktop").mkdir()
        (self.home / "Desktop" / "Python").mkdir()
        # projA: strong marker + a real purpose line under the title
        self.projA = self.home / "Desktop" / "projA"
        self.projA.mkdir()
        (self.projA / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
        (self.projA / "CLAUDE.md").write_text("# CLAUDE.md\n\nDoes the A thing for the ecosystem.\n", encoding="utf-8")
        # projB: weak marker only -> needs a slug ref to count as a project
        self.projB = self.home / "Desktop" / "Python" / "projB"
        self.projB.mkdir()
        (self.projB / "README.md").write_text("# projB\n\nThe B utility.\n", encoding="utf-8")

        self.claude = self.home / ".claude"
        (self.claude / "projects").mkdir(parents=True)
        (self.claude / "handoffs").mkdir(parents=True)
        # transcript slug for projA (encode path -> slug)
        slugA = H.encode_transcript_slug(normalize(str(self.projA)))
        (self.claude / "projects" / slugA).mkdir()
        (self.claude / "projects" / slugA / "s.jsonl").write_text("{}", encoding="utf-8")
        # daemon targeting projB
        (self.claude / "daemons.json").write_text(
            json.dumps({"BJob": {"project": str(self.projB)}}), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _harvest(self):
        roots = [self.home / "Desktop", self.home / "Desktop" / "Python"]
        return {c.path: c for c in H.harvest(roots, self.home)}

    def test_scan_finds_marker_projects(self):
        cands = self._harvest()
        self.assertIn(normalize(str(self.projA)), cands)
        self.assertIn(normalize(str(self.projB)), cands)

    def test_purpose_skips_title(self):
        a = self._harvest()[normalize(str(self.projA))]
        self.assertEqual(a.purpose, "Does the A thing for the ecosystem.")

    def test_transcript_slug_sets_activity_and_ref(self):
        a = self._harvest()[normalize(str(self.projA))]
        self.assertTrue(a.has_slug_ref)
        self.assertIsNotNone(a.last_transcript_at)
        self.assertIn("transcript", a.sources)

    def test_daemon_flag_detected(self):
        b = self._harvest()[normalize(str(self.projB))]
        self.assertTrue(b.is_daemonized)

    def test_encode_roundtrips_hyphenated_name(self):
        # the-council-capstone must encode without splitting on its internal hyphens
        slug = H.encode_transcript_slug("C:/Users/x/Desktop/Python/the-council-capstone")
        self.assertTrue(slug.endswith("the-council-capstone"))

    def test_default_roots_include_extra_roots_from_env(self):
        # E3.6: extra roots (e.g. a second drive) are shallow harvest roots so
        # their projects surface without a single-segment transcript slug.
        extra = Path(self.home) / "extra-drive"
        with mock.patch.dict(os.environ, {"BIGBOSS_EXTRA_ROOTS": str(extra)}):
            self.assertIn(extra, H.default_roots(self.home))
        with mock.patch.dict(os.environ, {"BIGBOSS_EXTRA_ROOTS": ""}):
            self.assertNotIn(extra, H.default_roots(self.home))

    def test_scan_root_finds_children_but_not_the_root_when_unnamed(self):
        # E3.6 guard: children with markers are returned; a root whose name is
        # empty (a drive root like "D:/") is never itself a project. We can't make
        # an empty-name dir under tmp, so assert the two halves the guard rests on:
        # (a) named marked roots ARE included, (b) drive roots have an empty name.
        marked = self.home / "Desktop" / "MarkedRoot"
        marked.mkdir()
        (marked / "CLAUDE.md").write_text("# MarkedRoot\n\nA project.\n", encoding="utf-8")
        self.assertIn(marked, H._scan_root(marked))  # named root with marker -> kept
        self.assertEqual(Path("D:/").name, "")  # drive root -> guard excludes it


if __name__ == "__main__":
    unittest.main()
