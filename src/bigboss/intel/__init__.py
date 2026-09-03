"""BigBoss project intel (Ecosystem Phase E4a): the "eyes" of the portfolio brain.

A crawler on the BigBoss host walks each registered project's planning docs, delegates the
cheap extraction work (status, blockers, roadmap) to Squire - the zero-cost local
model on the remote host - and persists one digested snapshot per project. The expensive
brain (Fable 5 in a harness session) reads the pre-digested portfolio through the
`portfolio_intel` MCP tool instead of raw docs, so paid tokens are spent only on
synthesis, never on crawling.

Also home to the Squire metering proxy: multiple projects share the remote host
endpoint, so BigBoss fronts it with a pass-through proxy that ledgers per-client
usage, latency, and failures (Squire itself has no logging or auth).
"""

from __future__ import annotations

from .digest import collect_sources, parse_intel, refresh_intel, source_hash
from .squire import SquireClient, SquireError

__all__ = [
    "SquireClient", "SquireError",
    "collect_sources", "parse_intel", "refresh_intel", "source_hash",
]
