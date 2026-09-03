"""Track B — Fable-led deep-research-per-project. Mocks the model caller (no live spend)."""

import json
import tempfile
import unittest
from pathlib import Path

from bigboss.council import research
from bigboss.registry.canonical import ResolvedProject
from bigboss.store import Store


def _rp(slug):
    return ResolvedProject(
        canonical_path=f"C:/dev/{slug}", slug=slug, family=slug, markers={".git": True},
        git_commit_count=3, last_activity_at="2026-06-01T00:00:00Z", status="active",
        purpose=f"{slug} purpose", domain="", sources=["fs-scan"], aliases=[],
    )


_BRIEF = {
    "summary": "s", "current_landscape": "cl", "angles": ["a1"], "reuse_from_lineage": ["r1"],
    "risks": ["risk"], "poc_sprint": "poc", "roadmap": [{"phase": "Research", "goal": "g", "tasks": ["t"]}],
    "open_questions": ["q"],
}


class ResearchEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.store.apply_reconciled([_rp("proj")])
        self.store.set_context("proj", notes="the operator's note", evolved_into="successor")
        self._orig = research.call_anthropic_model

    def tearDown(self):
        research.call_anthropic_model = self._orig
        self.tmp.cleanup()

    def test_research_parses_brief_and_persists(self):
        captured = {}

        def fake(model, system, user, *, max_tokens=0, timeout=0):
            captured["system"] = system
            captured["user"] = user
            return {"status": "success", "answer": json.dumps(_BRIEF), "model": model,
                    "usage": {"input": 100, "output": 200}}

        research.call_anthropic_model = fake
        result = research.run_project_research(self.store, "proj", model="claude-fable-5")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["brief"]["poc_sprint"], "poc")
        self.assertIn("Fable", captured["system"])
        self.assertIn("the operator's note", captured["user"])       # owner notes reach the prompt
        self.assertIn("evolved into: successor", captured["user"])  # lineage reaches the prompt
        # persistence
        self.store.record_project_research(result["project_id"], "proj", result["brief"],
                                           result["model"], result["usage"])
        with self.store.connect() as c:
            row = c.execute("SELECT slug, model, brief_json FROM project_research WHERE slug='proj'").fetchone()
        self.assertEqual(row[0], "proj")
        self.assertEqual(json.loads(row[2])["summary"], "s")

    def test_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            research.run_project_research(self.store, "ghost")

    def test_model_error_returns_error_status(self):
        research.call_anthropic_model = lambda *a, **k: {"status": "error", "usage": {}, "error": "boom"}
        result = research.run_project_research(self.store, "proj")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["brief"])

    def test_unparseable_output_flagged(self):
        research.call_anthropic_model = lambda *a, **k: {
            "status": "success", "answer": "not json", "model": "m", "usage": {"input": 1, "output": 1}}
        result = research.run_project_research(self.store, "proj")
        self.assertEqual(result["status"], "unparsed")

    def test_no_crawl_project_skips_docs(self):
        self.store.set_no_crawl("proj", True)
        research.call_anthropic_model = lambda *a, **k: {
            "status": "success", "answer": json.dumps(_BRIEF), "model": "m", "usage": {"input": 1, "output": 1}}
        result = research.run_project_research(self.store, "proj")
        self.assertEqual(result["docs_used"], 0)  # no_crawl -> no docs gathered


if __name__ == "__main__":
    unittest.main()
