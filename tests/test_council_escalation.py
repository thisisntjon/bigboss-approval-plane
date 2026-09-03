"""M5a — Fable tie-break escalation: the pure policy, the blind tie-break, the human-gated card
side-effect, and the eval aggregate. No live spend (the Fable caller is mocked)."""

import json
import tempfile
import unittest
from pathlib import Path

from bigboss.council import escalation
from bigboss.registry.canonical import ResolvedProject
from bigboss.store import Store


def _claim(verdict, conf):
    return {"text": f"claim-{verdict}", "verdict": verdict, "confidence": conf}


class ShouldEscalateTests(unittest.TestCase):
    def test_vote_tie_escalates(self):
        r = escalation.should_escalate({"mode": "live", "vote_tie": True,
                                        "verified_claims": [_claim("supported", 0.9)]})
        self.assertTrue(r["escalate"])
        self.assertEqual(r["trigger"], "vote_tie")

    def test_unanimous_confident_does_not_escalate(self):
        r = escalation.should_escalate({"mode": "live", "unanimous": True, "vote_tie": False,
                                        "verified_claims": [_claim("supported", 0.9)]})
        self.assertFalse(r["escalate"])

    def test_high_refuted_fraction_escalates(self):
        r = escalation.should_escalate({"mode": "live",
                                        "verified_claims": [_claim("refuted", 0.2), _claim("supported", 0.95)]})
        self.assertTrue(r["escalate"])
        self.assertEqual(r["trigger"], "verification_conflict")

    def test_low_mean_confidence_escalates(self):
        r = escalation.should_escalate({"mode": "live",
                                        "verified_claims": [_claim("supported", 0.3), _claim("supported", 0.4)]})
        self.assertTrue(r["escalate"])
        self.assertEqual(r["trigger"], "low_confidence")

    def test_high_risk_escalates_first(self):
        r = escalation.should_escalate({"mode": "live", "input_risk": {"level": "high"},
                                        "vote_tie": True, "verified_claims": [_claim("supported", 0.9)]})
        self.assertEqual(r["trigger"], "high_risk")

    def test_clean_consensus_does_not_escalate(self):
        r = escalation.should_escalate({"mode": "live", "vote_tie": False, "unanimous": False,
                                        "verified_claims": [_claim("supported", 0.85), _claim("supported", 0.9)]})
        self.assertFalse(r["escalate"])

    def test_prioritization_high_dissent_escalates(self):
        r = escalation.should_escalate({"mode": "prioritization", "dissent": ["a", "b", "c"], "dropped_seats": []})
        self.assertTrue(r["escalate"])
        self.assertEqual(r["trigger"], "high_dissent")

    def test_prioritization_clean_does_not_escalate(self):
        r = escalation.should_escalate({"mode": "prioritization", "dissent": ["a"], "dropped_seats": [],
                                        "ideation": {"degraded": False}})
        self.assertFalse(r["escalate"])


class TiebreakTests(unittest.TestCase):
    def setUp(self):
        self._orig = escalation.call_anthropic_model

    def tearDown(self):
        escalation.call_anthropic_model = self._orig

    def test_run_tiebreak_blind_prices_with_fable_rates(self):
        captured = {}

        def fake(model, system, user, *, max_tokens=0, timeout=0):
            captured["system"] = system
            captured["user"] = user
            return {"status": "success", "answer": "Fable's independent answer", "model": model,
                    "usage": {"input": 100, "output": 200}}

        escalation.call_anthropic_model = fake
        out = escalation.run_tiebreak({"question": "Should I do X or Y?",
                                       "verified_claims": [_claim("refuted", 0.2)]})
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["answer"], "Fable's independent answer")
        # blind: the question reaches Fable, but never the council's answers/votes
        self.assertIn("Should I do X or Y?", captured["user"])
        self.assertNotIn("council", captured["user"].lower())
        # exact Fable pricing: 100/1e6*10 + 200/1e6*50 = 0.001 + 0.010 = 0.011
        self.assertAlmostEqual(float(out["cost_usd"]), 0.011, places=4)


class CardSideEffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        d = self.store.enroll_device("Workstation", method="auto-local")
        self.device = {"id": d["device_id"], "name": d["device_name"]}
        rec = self.store.record_council_session({
            "mode": "live", "question": "split question", "seats": ["claude", "gpt"],
            "final": {"final_answer": "council tentative answer"}, "usage": {"input": 1, "output": 1},
            "escalation": {"escalate": True, "trigger": "vote_tie", "reason": "tied"},
        }, cost_usd="0.02")
        self.session_id = rec["session_id"]
        self._orig = escalation.call_anthropic_model
        escalation.call_anthropic_model = lambda *a, **k: {
            "status": "success", "answer": "FABLE RESOLVED ANSWER", "model": "claude-fable-5",
            "usage": {"input": 50, "output": 100}}

    def tearDown(self):
        escalation.call_anthropic_model = self._orig
        self.tmp.cleanup()

    def _card(self):
        return self.store.create_approval_request({
            "harness": "bigboss", "title": "escalate?",
            "proposed_action": {"kind": "fable_escalation", "session_id": self.session_id,
                                "question": "split question", "contested": ["claim-refuted"],
                                "trigger": "vote_tie", "reason": "tied"},
        })

    def _final_answer(self):
        with self.store.connect() as c:
            row = c.execute("SELECT final_answer, escalation_json FROM council_sessions WHERE id = ?",
                            (self.session_id,)).fetchone()
        return row[0], json.loads(row[1])

    def test_approve_runs_fable_and_updates_session(self):
        card = self._card()
        self.store.resolve_approval(card["id"], "approve_once", "", self.device)
        answer, esc = self._final_answer()
        self.assertEqual(answer, "FABLE RESOLVED ANSWER")   # session updated with Fable's blind answer
        self.assertTrue(esc["resolved"])
        types = {e["event_type"] for e in self.store.events_after(0, limit=500)}
        self.assertIn("council.escalation.resolved", types)

    def test_reject_leaves_council_answer(self):
        card = self._card()
        self.store.resolve_approval(card["id"], "reject", "not worth it", self.device)
        answer, _ = self._final_answer()
        self.assertEqual(answer, "council tentative answer")  # untouched — no Fable spend
        types = {e["event_type"] for e in self.store.events_after(0, limit=500)}
        self.assertIn("council.escalation.declined", types)
        self.assertNotIn("council.escalation.resolved", types)


class EvalStatsTests(unittest.TestCase):
    def test_eval_aggregates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "bigboss.sqlite3")
        store.record_council_session({"mode": "live", "final": {"final_answer": "a"}, "usage": {},
                                      "escalation": {"escalate": True, "trigger": "vote_tie"}}, cost_usd="0.02")
        store.record_council_session({"mode": "live", "final": {"final_answer": "b"}, "usage": {},
                                      "escalation": {"escalate": False}}, cost_usd="0.01")
        s = store.council_eval_stats(days=3650)
        self.assertEqual(s["sessions"], 2)
        self.assertEqual(s["flagged_unsure"], 1)
        self.assertEqual(s["triggers"], {"vote_tie": 1})
        self.assertAlmostEqual(s["total_cost_usd"], 0.03, places=4)


if __name__ == "__main__":
    unittest.main()
