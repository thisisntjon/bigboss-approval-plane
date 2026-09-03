"""M6.2 — Brainz recall bridge (display-only + a fail-closed shareability gate).

Big Boss can surface relevant past conversations from Brainz (the operator's local memory system, warm webapp
on 127.0.0.1:8077). PRIVACY-FIRST: recalled content is shown to the operator in their terminal and NEVER
auto-sent anywhere. The chat's only model path is CLOUD vendors, so injecting Brainz content into a
turn would be egress — the gate below makes that fail-closed.

Why display-only: `brainz_search` results carry no per-item `exportable`/tier we can trust, nothing in
Brainz is `exportable` today, and no tier-gated Brainz endpoint exists yet. So off-box sharing is denied
by construction until Brainz builds the authenticated `/api/recall`; this gate is built to consume it later. Stdlib only (mirrors intel/squire.py).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8077"  # Brainz warm webapp — loopback, same box
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# Where a recalled block may go. Only the local terminal is ever allowed today.
DISPLAY_LOCAL = "display_local"
CLOUD_VENDOR = "cloud_vendor"     # the chat seats (Anthropic/OpenAI/...) — off-box egress
PHONE_TUNNEL = "phone_tunnel"     # dashboard served over localhost.run — public transit

# Sensitivity tiers (Brainz config), ascending.
TIER_ORDER = ("public", "personal", "sensitive", "restricted")


class BrainzError(RuntimeError):
    """Brainz was unreachable or returned an unusable response."""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def may_share(tier: str | None, destination: str, *, exportable: bool = False) -> bool:
    """Fail-closed shareability gate. The local terminal display is always allowed; any OFF-BOX
    destination (cloud vendor, phone/tunnel) is denied unless the content is provably `exportable`
    AND `public` — which Brainz search cannot assert today, so off-box always denies. When Brainz's
    gated `/api/recall` later returns a real `exportable`, callers pass it and public content can cross."""
    if destination == DISPLAY_LOCAL:
        return True
    # Off-box: only gate-flipped, public content may cross. (Ceiling env reserved for that future path.)
    return bool(exportable) and (tier == "public")


class BrainzClient:
    """Stdlib urllib client for Brainz's warm webapp. Loopback-only by default (the webapp is
    unauthenticated plaintext — don't cross the LAN unless BIGBOSS_BRAINZ_ALLOW_LAN=1)."""

    def __init__(self, base_url: str | None = None, timeout: int = 20):
        self.base_url = (base_url or os.environ.get("BIGBOSS_BRAINZ_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS and not _env_flag("BIGBOSS_BRAINZ_ALLOW_LAN"):
            raise BrainzError(
                f"Brainz URL {self.base_url!r} is not loopback; the Brainz webapp is unauthenticated "
                "plaintext. Set BIGBOSS_BRAINZ_ALLOW_LAN=1 only if you accept LAN exposure.")

    def ping(self) -> dict[str, Any]:
        """Cheap reachability probe against /api/stats. Never raises."""
        started = time.monotonic()
        try:
            self._get("/api/stats")
            return {"ok": True, "detail": "", "latency_ms": int((time.monotonic() - started) * 1000)}
        except BrainzError as exc:
            return {"ok": False, "detail": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}

    def recall(self, query: str, limit: int = 6) -> dict[str, Any]:
        """Search Brainz for relevant past conversations. Returns
        {mode, degraded, notices, results:[{title, platform, date, snippet, tier, conversation_id}]}.
        DISPLAY-ONLY — callers must not forward results off-box without `may_share`."""
        qs = urllib.parse.urlencode({"q": query, "limit": limit})
        data = self._get(f"/api/search?{qs}")
        mode = str(data.get("mode") or "")
        results = []
        for r in (data.get("results") or [])[:limit]:
            results.append({
                "conversation_id": r.get("conversation_id"),
                "title": (r.get("title") or "(untitled)"),
                "platform": r.get("platform") or "",
                "date": (r.get("created_at") or "")[:10],
                "snippet": (r.get("snippet") or r.get("summary") or "")[:280],
                "tier": r.get("tier") or "?",   # advisory badge (webapp-only); not a trust boundary
            })
        return {
            "mode": mode,
            "degraded": mode != "semantic",     # keyword fallback (0.12 floor) → tangential; flag it
            "notices": data.get("notices") or [],
            "results": results,
        }

    # -- transport (mirrors intel/squire.py) --------------------------------
    def _get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                pass
            raise BrainzError(f"Brainz HTTP {exc.code}: {detail or exc.reason}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise BrainzError(f"Brainz unreachable: {exc}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BrainzError(f"Brainz returned non-JSON: {exc}")


def format_recall(recall: dict[str, Any]) -> str:
    """Render a recall result for the local terminal (display-only)."""
    results = recall.get("results") or []
    if not results:
        return "[no matching conversations in Brainz]"
    lines = []
    if recall.get("degraded"):
        lines.append(f"⚠ degraded ({recall.get('mode') or 'keyword'}) — results may be tangential.")
    for i, r in enumerate(results, 1):
        meta = " · ".join(x for x in (r["platform"], r["date"], f"tier:{r['tier']}") if x)
        lines.append(f"{i}. {r['title']}  [{meta}]\n     {r['snippet']}")
    lines.append("\n(shown locally only — NOT sent to the model. Paste anything you want it to use.)")
    return "\n".join(lines)
