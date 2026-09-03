"""Per-model cost computation for the BigBoss router ledger.

Rates are USD per **million** tokens, standard (non-batch) tier, verified in the
compass research artifact (2026-07-01). Cost is always priced against the model
that *actually served* the response (read off ``message_start``) - this is both
correct for Fable->Opus content reroutes (billed at Opus prices) and the only way
to detect the reroute in the ledger.

Uses ``decimal.Decimal`` throughout so accumulated spend is exact, never float.
The rate table is plain data - edit it without touching code when prices change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


# USD per million tokens. Keys are base model ids; served ids may carry a date
# suffix (e.g. claude-haiku-4-5-20251001) and are matched by prefix in rates_for().
#   input / output / cache_write_5m (1.25x in) / cache_write_1h (2x in) / cache_read (0.1x in)
MODEL_RATES: dict[str, dict[str, str]] = {
    "claude-haiku-4-5": {
        "input": "1", "output": "5",
        "cache_write_5m": "1.25", "cache_write_1h": "2", "cache_read": "0.10",
    },
    # Sonnet 5 intro pricing (through 2026-08-31). Post-intro is 3/15 - update then,
    # or add a dated override. Using intro rates as the current default.
    "claude-sonnet-5": {
        "input": "2", "output": "10",
        "cache_write_5m": "2.50", "cache_write_1h": "4", "cache_read": "0.20",
    },
    "claude-sonnet-4-6": {
        "input": "3", "output": "15",
        "cache_write_5m": "3.75", "cache_write_1h": "6", "cache_read": "0.30",
    },
    "claude-opus-4-8": {
        "input": "5", "output": "25",
        "cache_write_5m": "6.25", "cache_write_1h": "10", "cache_read": "0.50",
    },
    "claude-opus-4-7": {  # AWS fallback target for Fable reroutes on some lanes
        "input": "5", "output": "25",
        "cache_write_5m": "6.25", "cache_write_1h": "10", "cache_read": "0.50",
    },
    "claude-fable-5": {
        "input": "10", "output": "50",
        "cache_write_5m": "12.50", "cache_write_1h": "20", "cache_read": "1.00",
    },
}

# Unknown served models are priced conservatively against this tier and flagged by
# cost_breakdown()["known"] == False, so a new/misspelled id never silently reads $0.
UNKNOWN_MODEL_FALLBACK = "claude-opus-4-8"

_MILLION = Decimal(1_000_000)


def rates_for(model: str) -> tuple[dict[str, str], bool]:
    """Return (rate_dict, known). Exact match first, then a base-id prefix match
    (so dated served ids resolve), else the conservative fallback with known=False."""
    if not model:
        return MODEL_RATES[UNKNOWN_MODEL_FALLBACK], False
    if model in MODEL_RATES:
        return MODEL_RATES[model], True
    for base, rates in MODEL_RATES.items():
        if model.startswith(base):
            return rates, True
    return MODEL_RATES[UNKNOWN_MODEL_FALLBACK], False


def cost(model: str, usage: Mapping[str, Any]) -> Decimal:
    """Exact USD cost of one response given the *served* model and its usage object.

    usage keys (all optional, default 0): input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens. Thinking tokens are
    already folded into output_tokens by the API, so no special handling.

    Cache-write TTL split: if usage carries a ``cache_creation`` breakdown
    ({ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}) it is billed at the
    respective rates; otherwise cache_creation_input_tokens is assumed 5-min (the
    common case) - a small, bounded error on the cheapest token class.
    """
    return cost_breakdown(model, usage)["cost_usd"]


def cost_breakdown(model: str, usage: Mapping[str, Any]) -> dict[str, Any]:
    """Like cost() but returns the components + whether the model was known."""
    rates, known = rates_for(model)

    def _int(key: str) -> int:
        value = usage.get(key)
        return int(value) if value else 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    cache_read = _int("cache_read_input_tokens")
    cache_creation = _int("cache_creation_input_tokens")

    breakdown = usage.get("cache_creation") or {}
    write_5m = int(breakdown.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
    ttl_split = bool(write_5m or write_1h)
    if not ttl_split:
        # No breakdown: assume the whole cache_creation figure is 5-min.
        write_5m = cache_creation
        write_1h = 0

    total = (
        Decimal(input_tokens) * Decimal(rates["input"])
        + Decimal(output_tokens) * Decimal(rates["output"])
        + Decimal(write_5m) * Decimal(rates["cache_write_5m"])
        + Decimal(write_1h) * Decimal(rates["cache_write_1h"])
        + Decimal(cache_read) * Decimal(rates["cache_read"])
    ) / _MILLION

    return {
        "cost_usd": total,
        "known": known,
        "ttl_split_reported": ttl_split,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }
