"""Named local-compute endpoints BigBoss can talk to.

Two classes of machine, deliberately not interchangeable:

- **squire** (default): the always-on, zero-cost triage box (LM Studio on a LAN box). Cheap, safe to hammer, and the ONLY endpoint the background scheduler
  is allowed to use.
- **beastmode** (opt-in): a bigger, more valuable workstation (a large-GPU box). Guarded compute - never auto-routed, rate-limited/serialized so
  BigBoss can't monopolize the GPU, and ledgered under its own client/endpoint
  label so its usage is visible separately.

Endpoints resolve from env first (so secrets/hosts stay out of git), then fall
back to documented defaults. `auto_ok=False` is a hard rule enforced by the
scheduler and `refresh_intel`, not a suggestion.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from .squire import DEFAULT_BASE_URL, DEFAULT_MODEL, SquireClient


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    model: str
    auto_ok: bool          # may the background scheduler use it?
    min_interval_s: float  # floor between calls (0 = no throttle)
    api_key_env: str | None = None
    note: str = ""
    # Known channel by which data that reaches this machine can leave it (for the
    # egress audit trail). Empty = no specifically-tracked exposure. We do NOT gate
    # traffic; we record what we send so exposure is accountable after the fact.
    exposure: str = ""

    def client(self, timeout: int = 120) -> SquireClient:
        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        return SquireClient(base_url=self.base_url, model=self.model, timeout=timeout, api_key=api_key)


# Registry. Hosts/models/keys are env-overridable; defaults match docs/squire.md
# (squire) and the opt-in guarded box (beastmode).
def _endpoints() -> dict[str, Endpoint]:
    return {
        "squire": Endpoint(
            name="squire",
            base_url=os.environ.get("SQUIRE_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("SQUIRE_MODEL") or DEFAULT_MODEL,
            auto_ok=True,
            min_interval_s=0.0,
            api_key_env="SQUIRE_API_KEY",
            note="Always-on zero-cost triage (the remote host). Scheduler default.",
        ),
        "beastmode": Endpoint(
            name="beastmode",
            base_url=os.environ.get("BEASTMODE_BASE_URL") or "http://beastmode.local:1234/v1",
            # Default to a loaded *instruct* model: reasoning models (qwen3-14b) burn
            # the token budget on hidden thinking and return empty content for our
            # JSON-extraction task. qwen3-coder-30b-a3b-instruct is already loaded there.
            model=os.environ.get("BEASTMODE_MODEL") or "qwen3-coder-30b-a3b-instruct",
            auto_ok=False,  # HARD RULE: guarded 5090 box, never auto-invoked.
            min_interval_s=float(os.environ.get("BEASTMODE_MIN_INTERVAL_S") or 6.0),
            api_key_env="BEASTMODE_API_KEY",
            note="Guarded large-GPU workstation. Opt-in, rate-limited, never auto-routed.",
            # The guarded box runs a managed endpoint-security (EDR) agent — the known off-box channel:
            # anything we send there can be captured by its telemetry. Every send is recorded in the egress log.
            exposure=os.environ.get("BEASTMODE_EXPOSURE") or "managed-edr",
        ),
    }


def list_endpoints() -> list[Endpoint]:
    return list(_endpoints().values())


def get_endpoint(name: str | None) -> Endpoint:
    endpoints = _endpoints()
    key = (name or "squire").casefold()
    if key not in endpoints:
        raise KeyError(f"Unknown endpoint {name!r}. Known: {', '.join(endpoints)}.")
    return endpoints[key]


class _Throttle:
    """Process-wide per-endpoint minimum spacing between calls (protects the GPU box)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, name: str, min_interval_s: float) -> None:
        if min_interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            last = self._last.get(name, 0.0)
            gap = now - last
            if gap < min_interval_s:
                time.sleep(min_interval_s - gap)
            self._last[name] = time.monotonic()


_THROTTLE = _Throttle()


def throttle(endpoint: Endpoint) -> None:
    _THROTTLE.wait(endpoint.name, endpoint.min_interval_s)
