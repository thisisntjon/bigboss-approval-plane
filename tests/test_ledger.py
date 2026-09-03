import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bigboss.store import Store


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_record_bumps_period_total_and_emits_events(self):
        result = self.store.record_router_call(
            requested_model="claude-opus-4-8",
            served_model="claude-opus-4-8",
            usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            cost_usd="30",
            harness="agent-sdk",
        )
        self.assertEqual(result["period_spent_usd"], "30")
        snap = self.store.check_budget()
        self.assertEqual(snap["spent_usd"], "30")
        self.assertEqual(snap["hard_cap_usd"], "200")
        self.assertFalse(snap["blocked"])

        types = [e["event_type"] for e in self.store.events_after(0)]
        self.assertIn("routing.decided", types)
        self.assertIn("spend.recorded", types)

    def test_reroute_is_flagged_in_routing_event(self):
        self.store.record_router_call(
            requested_model="claude-fable-5",
            served_model="claude-opus-4-8",  # Anthropic rerouted cyber/bio/chem -> Opus
            usage={"input_tokens": 100, "output_tokens": 50},
            cost_usd="0.00375",
            stop_reason="refusal",
        )
        routing = [e for e in self.store.events_after(0) if e["event_type"] == "routing.decided"][-1]
        self.assertTrue(routing["payload"]["rerouted"])

    def test_alert_fires_once_per_threshold(self):
        # First call to 60% of the $200 cap crosses the 50% banner exactly once.
        r1 = self.store.record_router_call(
            requested_model="claude-opus-4-8", served_model="claude-opus-4-8",
            usage={}, cost_usd="120",
        )
        self.assertEqual(r1["alert"], "50%")
        # A tiny follow-up call stays under 75% -> no new alert.
        r2 = self.store.record_router_call(
            requested_model="claude-opus-4-8", served_model="claude-opus-4-8",
            usage={}, cost_usd="1",
        )
        self.assertIsNone(r2["alert"])
        # Crossing 75% then fires once.
        r3 = self.store.record_router_call(
            requested_model="claude-opus-4-8", served_model="claude-opus-4-8",
            usage={}, cost_usd="40",
        )
        self.assertEqual(r3["alert"], "75%")

        alerts = [e for e in self.store.events_after(0) if e["event_type"] == "budget.alert"]
        self.assertEqual([a["payload"]["threshold"] for a in alerts], ["50%", "75%"])

    def test_over_cap_marks_blocked_advisory(self):
        r = self.store.record_router_call(
            requested_model="claude-fable-5", served_model="claude-fable-5",
            usage={}, cost_usd="205",
        )
        self.assertEqual(r["alert"], "100%")
        snap = self.store.check_budget()
        self.assertTrue(snap["blocked"])  # advisory in E1 - the proxy does not act on it

    def test_spend_accumulates_exactly_with_decimals(self):
        for _ in range(3):
            self.store.record_router_call(
                requested_model="claude-haiku-4-5", served_model="claude-haiku-4-5",
                usage={}, cost_usd="0.10",
            )
        self.assertEqual(Decimal(self.store.check_budget()["spent_usd"]), Decimal("0.30"))

    def test_set_hard_cap(self):
        self.store.set_hard_cap("50")
        self.assertEqual(self.store.check_budget()["hard_cap_usd"], "50")


if __name__ == "__main__":
    unittest.main()
