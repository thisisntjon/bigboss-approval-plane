"""Regression: council-research and council-prioritize must persist REAL cost_usd, not '0'
(system audit 2026-07-06 found both logged cost_usd='0' despite live spend, so council-eval
understated totals). Drives the CLI handlers with the live runners mocked (no spend)."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from bigboss import cli
from bigboss.council import prioritize as prioritize_mod
from bigboss.council import research as research_mod
from bigboss.registry.canonical import ResolvedProject
from bigboss.store import Store


def _rp(slug):
    return ResolvedProject(
        canonical_path=f"C:/dev/{slug}", slug=slug, family=slug, markers={".git": True},
        git_commit_count=3, last_activity_at="2026-06-01T00:00:00Z", status="active",
        purpose=f"{slug} purpose", domain="", sources=["fs-scan"], aliases=[],
    )


class CouncilCostPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        store = Store(self.data_dir / "bigboss.sqlite3")
        store.apply_reconciled([_rp("proj")])
        self._orig_research = research_mod.run_project_research
        self._orig_prioritize = prioritize_mod.run_prioritization

    def tearDown(self):
        research_mod.run_project_research = self._orig_research
        prioritize_mod.run_prioritization = self._orig_prioritize
        self.tmp.cleanup()

    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--data-dir", str(self.data_dir), *argv])
        return code, buf.getvalue()

    def test_research_persists_nonzero_cost(self):
        def fake(store, slug, model=None):
            pid = store.list_projects()[0]["id"]
            return {"status": "success", "project_id": pid, "slug": slug,
                    "model": model or "claude-fable-5",
                    "brief": {"summary": "s", "roadmap": []},
                    "usage": {"input": 8000, "output": 3000}, "docs_used": 1}
        research_mod.run_project_research = fake
        code, _ = self._run("council-research", "proj")
        self.assertEqual(code, 0)
        with Store(self.data_dir / "bigboss.sqlite3").connect() as c:
            cost = c.execute("SELECT cost_usd FROM project_research WHERE slug='proj'").fetchone()[0]
        self.assertNotEqual(str(cost), "0")
        self.assertGreater(float(cost), 0.0)

    def test_prioritize_persists_nonzero_cost(self):
        def fake(store, top_n=10):
            return {"consensus": ["proj"], "reasons": {"proj": "r"}, "seats": ["claude"],
                    "overall": "o", "dissent": "", "ideation": {"brief": "b"},
                    "usage": {"input": 6000, "output": 2000}}
        prioritize_mod.run_prioritization = fake
        code, _ = self._run("council-prioritize", "--top", "1")
        self.assertEqual(code, 0)
        with Store(self.data_dir / "bigboss.sqlite3").connect() as c:
            row = c.execute("SELECT cost_usd FROM council_sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(float(row[0]), 0.0)


if __name__ == "__main__":
    unittest.main()
