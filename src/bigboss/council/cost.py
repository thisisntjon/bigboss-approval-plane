"""Rough cost estimate for a council session (M1b). The engine aggregates token usage
across all seats/stages, so this is a BLENDED estimate at cheap-tier rates — enough to see
that a run is inexpensive and stays within budget. Precise per-vendor metering (Anthropic
via the router ledger) is a later refinement.
"""

from __future__ import annotations

# Blended cheap-tier rates ($/token). Council seats are value-tier models (~$1-2/1M in,
# ~$5-10/1M out); this over-estimates slightly, which is the safe direction for a budget.
_INPUT_RATE = 1.50 / 1_000_000
_OUTPUT_RATE = 8.00 / 1_000_000


def estimate_cost(report: dict) -> str:
    usage = report.get("usage") or {}
    cost = int(usage.get("input") or 0) * _INPUT_RATE + int(usage.get("output") or 0) * _OUTPUT_RATE
    return f"{cost:.4f}"
