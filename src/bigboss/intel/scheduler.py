"""Background heartbeat for the portfolio tracker, run inside `bigboss serve`.

Two cadences on one daemon thread:
- Squire health probe (default every 5 min) -> squire_calls ledger, with an
  event only on an up/down transition (no SSE spam).
- Intel cycle (default every 6 h): registry refresh (discover/update projects)
  followed by an intel refresh (Squire digestion of changed docs only).

Every tick is wrapped so a Squire outage or scan error can never take down the
approval plane.
"""

from __future__ import annotations

import threading
import time

from .digest import refresh_intel
from .squire import SquireClient


# First intel cycle runs shortly after serve starts so the portfolio populates
# without waiting a full interval.
FIRST_DIGEST_DELAY_S = 20.0


class IntelScheduler(threading.Thread):
    def __init__(
        self,
        store,
        digest_interval_s: float,
        health_interval_s: float,
        client: SquireClient | None = None,
    ):
        super().__init__(name="bigboss-intel-scheduler", daemon=True)
        self.store = store
        self.digest_interval_s = digest_interval_s
        self.health_interval_s = health_interval_s
        self.client = client or SquireClient()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        now = time.monotonic()
        next_health = now if self.health_interval_s > 0 else float("inf")
        next_digest = now + FIRST_DIGEST_DELAY_S if self.digest_interval_s > 0 else float("inf")
        while not self._stop.wait(timeout=1.0):
            now = time.monotonic()
            if now >= next_health:
                next_health = now + self.health_interval_s
                self._tick_health()
            if now >= next_digest:
                next_digest = now + self.digest_interval_s
                self._tick_digest()

    def _tick_health(self) -> None:
        try:
            probe = self.client.ping()
            result = self.store.record_squire_health(
                probe["ok"], detail=probe["detail"], latency_ms=probe["latency_ms"]
            )
            if result["changed"]:
                print(f"intel-scheduler: squire is {'up' if probe['ok'] else 'down'} ({probe['detail'] or 'ok'})")
        except Exception as exc:
            print(f"intel-scheduler: health probe error: {exc!r}")

    def _tick_digest(self) -> None:
        try:
            from ..registry import refresh as registry_refresh

            registry_refresh(self.store)
        except Exception as exc:
            print(f"intel-scheduler: registry refresh error: {exc!r}")
        try:
            summary = refresh_intel(self.store, client=self.client)
            print(
                "intel-scheduler: digest cycle: "
                f"{summary['digested']} digested, {summary['skipped_unchanged']} unchanged, "
                f"{summary['failed']} failed (squire {'up' if summary['squire_up'] else 'down'})"
            )
        except Exception as exc:
            print(f"intel-scheduler: intel refresh error: {exc!r}")
