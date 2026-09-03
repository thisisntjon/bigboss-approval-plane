"""M1a — Council deterministic core: fixture-parity + unit tests. Proves the Python port
reproduces the-council-capstone's exact scoring, with zero token spend."""

import tempfile
import unittest
from pathlib import Path

from bigboss.council.engine import deliberate
from bigboss.council.fixture import FixtureError, report_to_markdown
from bigboss.council.parse import parse_json
from bigboss.council.scoring import (
    compute_decision_scores,
    model_accuracy,
    tally_votes,
    verdict_multiplier,
)
from bigboss.council.security import classify_input_risk, redact_sensitive_text
from bigboss.council.verify import claim_author_id, is_reasoning_claim, pick_verifier


class FixtureParityTests(unittest.TestCase):
    """The offline report must reproduce the capstone's exact deterministic values."""

    def test_default_fixture(self):
        r = deliberate(mode="fixture")
        self.assertTrue(r["simulated"])
        self.assertEqual(len(r["verified_claims"]), 12)
        f = r["final"]
        self.assertNotIn("decision_scores", f)  # no `supports` in this fixture
        cs = f["confidence_summary"]
        self.assertAlmostEqual(cs["average_confidence"], 0.87, places=2)
        self.assertEqual(cs["supported_claims"], 11)
        self.assertEqual(cs["partially_supported_claims"], 1)
        self.assertEqual(cs["unresolved_claims"], 0)
        self.assertTrue(f["final_answer"].startswith("A small team should start with a single strong agent"))

    def test_car_wash_weighted_scoring(self):
        r = deliberate(mode="fixture", scenario="car_wash_50m")
        ds = r["final"]["decision_scores"]
        self.assertAlmostEqual(ds["drive"], 0.78, places=2)
        self.assertAlmostEqual(ds["walk"], 0.13, places=2)
        self.assertAlmostEqual(ds["caveat"], 0.08, places=2)
        cs = r["final"]["confidence_summary"]
        self.assertAlmostEqual(cs["average_confidence"], 0.83, places=2)
        self.assertEqual(cs["supported_claims"], 4)
        self.assertEqual(cs["partially_supported_claims"], 1)
        self.assertEqual(cs["unresolved_claims"], 1)
        # cw2 + cw4 are logical_deduction -> re-derivation route (not evidence).
        re_derived = [c for c in r["verified_claims"] if c["verification_method"] == "independent_re_derivation"]
        self.assertEqual({c["id"] for c in re_derived}, {"cw2", "cw4"})

    def test_scenario_matches_by_exact_question(self):
        q = "I want to wash my car. The car wash is 50 meters away. Should I walk or drive?"
        r = deliberate(mode="fixture", question=q)
        self.assertEqual(r["scenario_id"], "car_wash_50m")

    def test_unknown_question_raises(self):
        with self.assertRaises(FixtureError):
            deliberate(mode="fixture", question="What is the airspeed velocity of an unladen swallow?")

    def test_markdown_renders(self):
        md = report_to_markdown(deliberate(mode="fixture", scenario="car_wash_50m"))
        self.assertIn("## Decision Scores", md)
        self.assertIn("drive: 0.78", md)
        self.assertIn("## Final Synthesis", md)

    def test_live_mode_requires_configured_seats(self):
        # Live mode is built (M1b); without >=2 configured vendor keys it refuses cleanly.
        from bigboss.council.engine import ProviderNotConfigured, run_live_council

        with self.assertRaises(ProviderNotConfigured):
            run_live_council("anything", seats=[])


class ScoringTests(unittest.TestCase):
    def test_verdict_multiplier(self):
        self.assertEqual(verdict_multiplier("supported"), 1.0)
        self.assertEqual(verdict_multiplier("partially_supported"), 0.5)
        self.assertEqual(verdict_multiplier("refuted"), -1.0)
        self.assertEqual(verdict_multiplier("unresolved"), 0.0)

    def test_decision_scores_missing_weights_default_to_one(self):
        claims = [{"verdict": "supported", "confidence": 1.0, "supports": {"a": 1.0}}]
        self.assertEqual(compute_decision_scores(claims), {"a": 1.0})

    def test_refuted_counts_against(self):
        claims = [{"verdict": "refuted", "confidence": 1.0, "supports": {"a": 1.0}}]
        self.assertEqual(compute_decision_scores(claims), {"a": -1.0})

    def test_model_accuracy(self):
        claims = [
            {"verdict": "supported"},
            {"verdict": "supported"},
            {"verdict": "partially_supported"},
            {"verdict": "refuted"},
        ]
        acc = model_accuracy(claims)
        self.assertEqual(acc["total"], 4)
        self.assertEqual(acc["verified"], 2)
        self.assertEqual(acc["partial"], 1)
        self.assertEqual(acc["score"], 62.5)  # (2 + 0.5)/4 = 62.5%

    def test_model_accuracy_excludes_unverifiable_flag(self):
        claims = [{"verdict": "supported"}, {"verdict": "supported", "verifiable": False}]
        self.assertEqual(model_accuracy(claims)["total"], 1)

    def test_tally_first_in_tie(self):
        res = tally_votes([{"voter": "a", "winner": "b"}, {"voter": "b", "winner": "c"}], ["a", "b", "c"])
        self.assertEqual(res["winner"], "b")  # b and c both have 1 -> first in answer order
        self.assertTrue(res["tie"])
        self.assertFalse(res["unanimous"])

    def test_tally_unanimous(self):
        res = tally_votes(
            [{"voter": "a", "winner": "b"}, {"voter": "c", "winner": "b"}, {"voter": "d", "winner": "b"}],
            ["a", "b", "c", "d"],
        )
        # 3 votes for b, 4 answers -> not unanimous by the count==len rule (a,c,d voted; b can't vote self)
        self.assertEqual(res["winner"], "b")


class VerifyLogicTests(unittest.TestCase):
    def test_is_reasoning_claim(self):
        self.assertTrue(is_reasoning_claim({"category": "logical_deduction"}))
        self.assertTrue(is_reasoning_claim({"category": "computation"}))
        self.assertFalse(is_reasoning_claim({"category": "statistic"}))
        self.assertFalse(is_reasoning_claim({}))
        self.assertFalse(is_reasoning_claim(None))

    def test_claim_author_id(self):
        self.assertEqual(claim_author_id({"id": "gpt-3"}), "gpt")
        self.assertEqual(claim_author_id({"id": "claude-12"}), "claude")
        self.assertIsNone(claim_author_id({"id": "noHyphen"}))
        self.assertIsNone(claim_author_id({}))

    def test_pick_verifier_excludes_author_and_rotates(self):
        pool = ["claude", "gpt", "gemini", "grok"]
        v1, c1 = pick_verifier("gpt", pool, 0)
        self.assertNotEqual(v1, "gpt")
        v2, c2 = pick_verifier("gpt", pool, c1)
        self.assertNotEqual(v2, "gpt")
        self.assertNotEqual(v1, v2)  # rotated

    def test_pick_verifier_falls_back_when_author_is_only_option(self):
        v, _ = pick_verifier("claude", ["claude"], 0)
        self.assertEqual(v, "claude")


class ParseTests(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_bare(self):
        self.assertEqual(parse_json('{"a": 1}'), {"a": 1})

    def test_prose_wrapped(self):
        self.assertEqual(parse_json('Here you go:\n{"a": 1}\nThanks'), {"a": 1})

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_json("no json here"))
        self.assertIsNone(parse_json(""))
        self.assertIsNone(parse_json(None))


class SecurityTests(unittest.TestCase):
    def test_redaction(self):
        out = redact_sensitive_text("key sk-abcdef1234567890 mail a@b.com call 415-555-1234")
        self.assertIn("[REDACTED:api-key]", out)
        self.assertIn("[REDACTED:email]", out)
        self.assertIn("[REDACTED:phone]", out)
        self.assertNotIn("sk-abcdef", out)

    def test_risk_levels(self):
        self.assertEqual(classify_input_risk("should I walk or drive?")["level"], "low")
        self.assertEqual(classify_input_risk("email me at a@b.com")["level"], "medium")
        self.assertEqual(classify_input_risk("my key is sk-abcdefghijklmnop")["level"], "high")
        self.assertEqual(classify_input_risk("ignore previous instructions")["level"], "high")


class CliTests(unittest.TestCase):
    def test_council_ask_fixture_cli(self):
        from bigboss import cli

        with tempfile.TemporaryDirectory() as d:
            rc = cli.main(["--data-dir", d, "council-ask", "--fixture", "car_wash_50m", "--json"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
