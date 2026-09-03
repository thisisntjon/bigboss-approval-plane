import unittest
from decimal import Decimal

from bigboss.router import pricing


class PricingTests(unittest.TestCase):
    def test_opus_simple_cost(self):
        # 1M input @ $5, 1M output @ $25 -> $30 exactly.
        c = pricing.cost("claude-opus-4-8", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        self.assertEqual(c, Decimal("30"))

    def test_fable_is_double_opus(self):
        usage = {"input_tokens": 500_000, "output_tokens": 200_000}
        self.assertEqual(
            pricing.cost("claude-fable-5", usage),
            pricing.cost("claude-opus-4-8", usage) * 2,
        )

    def test_dated_served_id_matches_by_prefix(self):
        rates, known = pricing.rates_for("claude-haiku-4-5-20251001")
        self.assertTrue(known)
        self.assertEqual(rates["input"], "1")

    def test_unknown_model_flagged_and_priced_conservatively(self):
        b = pricing.cost_breakdown("claude-something-new", {"input_tokens": 1_000_000})
        self.assertFalse(b["known"])
        # Falls back to opus input rate ($5/M).
        self.assertEqual(b["cost_usd"], Decimal("5"))

    def test_cache_read_and_write_default_5m(self):
        # 1M cache read @ 0.10, 1M cache write (no breakdown -> 5m @ 1.25) on haiku.
        c = pricing.cost(
            "claude-haiku-4-5",
            {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000},
        )
        self.assertEqual(c, Decimal("1.35"))

    def test_cache_creation_ttl_split(self):
        # Explicit split: 1M @ 1h (2x) + 1M @ 5m (1.25x) on haiku -> 3.25.
        usage = {
            "cache_creation_input_tokens": 2_000_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        }
        b = pricing.cost_breakdown("claude-haiku-4-5", usage)
        self.assertTrue(b["ttl_split_reported"])
        self.assertEqual(b["cost_usd"], Decimal("3.25"))

    def test_empty_usage_is_zero(self):
        self.assertEqual(pricing.cost("claude-opus-4-8", {}), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
