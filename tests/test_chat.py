"""`bigboss chat` — call_chat per-vendor message mapping + ChatSession dispatch. No live spend."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from bigboss import chat
from bigboss.council import engine as engine_mod
from bigboss.council import providers as P
from bigboss.store import Store


class CallChatMappingTests(unittest.TestCase):
    def setUp(self):
        self._orig_post = P._http_post
        self._env = dict(os.environ)
        os.environ["ANTHROPIC_API_KEY"] = "k"
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["GOOGLE_API_KEY"] = "k"
        self.captured = {}

    def tearDown(self):
        P._http_post = self._orig_post
        os.environ.clear()
        os.environ.update(self._env)

    def _patch(self, response):
        def fake(url, headers, body, timeout):
            self.captured = {"url": url, "headers": headers, "body": body}
            return response
        P._http_post = fake

    def test_anthropic_multiturn_body(self):
        self._patch({"content": [{"type": "text", "text": "hi there"}],
                     "usage": {"input_tokens": 5, "output_tokens": 3}, "model": "claude-haiku-4-5"})
        r = chat.call_chat([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                            {"role": "user", "content": "c"}], seat="claude", system="be nice")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["answer"], "hi there")
        self.assertEqual(self.captured["body"]["system"], "be nice")   # system stays top-level
        self.assertEqual([m["role"] for m in self.captured["body"]["messages"]], ["user", "assistant", "user"])
        self.assertIn("x-api-key", self.captured["headers"])

    def test_openai_puts_system_first(self):
        self._patch({"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
        r = chat.call_chat([{"role": "user", "content": "hi"}], seat="gpt", system="sys")
        self.assertEqual(r["answer"], "ok")
        self.assertEqual(self.captured["body"]["messages"][0], {"role": "system", "content": "sys"})

    def test_gemini_maps_assistant_to_model(self):
        self._patch({"candidates": [{"content": {"parts": [{"text": "g"}]}}],
                     "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}})
        r = chat.call_chat([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
                           seat="gemini")
        self.assertEqual(r["answer"], "g")
        self.assertEqual([c["role"] for c in self.captured["body"]["contents"]], ["user", "model"])

    def test_unknown_seat_errors_cleanly(self):
        r = chat.call_chat([{"role": "user", "content": "x"}], seat="nope")
        self.assertEqual(r["status"], "error")


class SelfKnowledgeTests(unittest.TestCase):
    def test_build_bigboss_system_includes_persona_and_portfolio(self):
        from bigboss.registry.canonical import ResolvedProject
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "bigboss.sqlite3")
        store.apply_reconciled([ResolvedProject(
            canonical_path="C:/dev/sampleapp", slug="sampleapp", family="sampleapp", markers={".git": True},
            git_commit_count=5, last_activity_at="2026-07-01T00:00:00Z", status="active",
            purpose="an AI media workbench", domain="", sources=["fs-scan"], aliases=[])])
        store.set_baseline("sampleapp", lifecycle="active", ownership="personal")
        sysprompt = chat.build_bigboss_system(store)
        self.assertIn("Big Boss", sysprompt)
        self.assertIn("sampleapp", sysprompt)          # portfolio surfaced
        self.assertIn("AI media workbench", sysprompt)


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.session = chat.ChatSession(self.store, seat="claude")
        self._orig_loop = chat.run_tool_loop
        self._orig_council = engine_mod.run_live_council

    def tearDown(self):
        chat.run_tool_loop = self._orig_loop
        engine_mod.run_live_council = self._orig_council
        self.tmp.cleanup()

    def test_send_appends_turns(self):
        chat.run_tool_loop = lambda store, messages, **k: {
            "status": "success", "answer": "reply", "usage": {"input": 3, "output": 2},
            "model": "m", "tools_used": [], "multistep": False, "rounds": 0}
        out = self.session.handle("hello")
        self.assertEqual(out, ("reply", False))
        self.assertEqual([m["role"] for m in self.session.messages], ["user", "assistant"])
        self.assertEqual(self.session.usage["input"], 3)

    def test_fabricated_card_id_is_stripped(self):
        # The cheap seat sometimes mimics a "card raised" line WITHOUT calling the tool (acceptance-test
        # bug). Any apr_ id not actually raised this session must be redacted + corrected.
        chat.run_tool_loop = lambda store, messages, **k: {
            "status": "success", "answer": "Done! Approval card apr_FAKE123abcdef raised.",
            "usage": {"input": 1, "output": 1}, "model": "m", "tools_used": [], "cards_raised": [],
            "multistep": False}
        out = self.session.handle("exclude gigfinder")[0]
        self.assertNotIn("apr_FAKE123abcdef", out)
        self.assertIn("no approval card was actually created", out)

    def test_real_card_id_is_preserved(self):
        chat.run_tool_loop = lambda store, messages, **k: {
            "status": "success", "answer": "Raised card apr_REAL999xyzABC.",
            "usage": {"input": 1, "output": 1}, "model": "m", "tools_used": ["propose_exclude"],
            "cards_raised": ["apr_REAL999xyzABC"], "multistep": False}
        out = self.session.handle("exclude gigfinder")[0]
        self.assertIn("apr_REAL999xyzABC", out)
        self.assertNotIn("no approval card was actually created", out)

    def test_slash_commands(self):
        self.assertEqual(self.session.handle("/help")[1], False)
        self.assertTrue(self.session.handle("/exit")[1])          # done
        self.session.messages = [{"role": "user", "content": "x"}]
        self.assertIn("reset", self.session.handle("/reset")[0])
        self.assertEqual(self.session.messages, [])
        self.assertIn("gpt", self.session.handle("/seat gpt")[0])
        self.assertEqual(self.session.seat, "gpt")
        self.session.handle("/system be terse")
        self.assertEqual(self.session.system, "be terse")
        self.assertIn("unknown", self.session.handle("/bogus")[0])

    def test_save_writes_jsonl(self):
        path = Path(self.tmp.name) / "t.jsonl"
        self.session.save_path = str(path)
        chat.run_tool_loop = lambda store, messages, **k: {
            "status": "success", "answer": "a", "usage": {"input": 1, "output": 1},
            "model": "m", "tools_used": [], "multistep": False, "rounds": 0}
        self.session.handle("q")
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["role"] for r in lines], ["user", "assistant"])

    def test_council_escalation_raises_card(self):
        report = {
            "mode": "live", "question": "q", "seats": ["claude", "gpt"], "winner": "claude",
            "final": {"final_answer": "council answer"}, "usage": {"input": 100, "output": 50},
            "per_model_scores": {}, "verified_claims": [{"text": "c", "verdict": "refuted", "confidence": 0.2}],
            "escalation": {"escalate": True, "trigger": "vote_tie", "reason": "tied"},
        }
        engine_mod.run_live_council = lambda q, **k: report
        try:
            out, done = self.session.handle("/council should i fork or build")
        finally:
            pass
        self.assertIn("council answer", out)
        self.assertIn("escalation card", out)
        # a fable_escalation card exists
        with self.store.connect() as c:
            row = c.execute("SELECT proposed_action_json FROM approval_requests ORDER BY created_at DESC "
                            "LIMIT 1").fetchone()
        self.assertEqual(json.loads(row[0])["kind"], "fable_escalation")


if __name__ == "__main__":
    unittest.main()
