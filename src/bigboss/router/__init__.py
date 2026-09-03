"""BigBoss router (Ecosystem Phase E1): a loopback-only, stdlib Anthropic proxy
that meters spend against a budget ledger.

v1 is metering-only: faithful passthrough of the Anthropic Messages API plus a
cost ledger. No model rewriting, no local-model triage, no hard-block. Tier routing and triage are deferred to v2.
"""

from __future__ import annotations

from .meter import Usage, UsageMeter
from .pricing import MODEL_RATES, cost, rates_for
from .proxy import RouterHTTPServer

__all__ = ["Usage", "UsageMeter", "MODEL_RATES", "cost", "rates_for", "RouterHTTPServer"]
