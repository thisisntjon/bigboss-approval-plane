"""Read-only security-posture VIEW.

Assembles signals BigBoss already has — LAN-exposed binds on this box, the compute-endpoint registry +
exposure tags, watched unauthenticated endpoints (ComfyUI), the egress audit, and the standing rulings —
into an HONEST picture. It reports EVIDENCE plus the load-bearing "safe only if the LAN is trusted" caveat.
It NEVER emits a numeric safety score or a false all-clear (the squire_probe/daemon anti-over-claim lesson).

Visibility, NOT control: enforcement (deny-by-default gateway, single-use class-3 tokens, the Warden
sensor mesh) is the separate five-planes G-roadmap. This report is the honest precursor.
"""
from __future__ import annotations

import subprocess
import sys
from urllib.parse import urlsplit

# Notable LAN-bound ports — ecosystem services + security-relevant OS ports. Everything else (Windows RPC,
# dynamic 49xxx, misc service ports) is bucketed as generic noise so the real signal isn't buried.
# port -> (label, gated?, detail)
_NOTABLE_PORTS = {
    8787: ("BigBoss dashboard/ingest", True,
           "token-gated ingest + device-authed dashboard; admin/pair loopback-only"),
    4000: ("cost-router", False, "expected loopback-only — a 0.0.0.0 bind here is unexpected"),
    1235: ("squire-proxy", False, "expected loopback-only — a 0.0.0.0 bind here is unexpected"),
    3389: ("Windows RDP", False,
           "remote desktop is LAN-exposed — a real attack surface; keep NLA on, scope to LAN, strong creds"),
    445: ("Windows SMB", False, "file sharing is LAN-exposed — ensure it is never internet-reachable"),
    5985: ("WinRM (HTTP)", False, "Windows remote management — verify it is intended"),
    5986: ("WinRM (HTTPS)", False, "Windows remote management — verify it is intended"),
}

_RULINGS = [
    "Admin / pair / router / squire-proxy endpoints are loopback-only (127.0.0.1).",
    "Approval decisions are token-bound, NOT loopback-auto-trust — a governed harness on this box cannot self-approve.",
    "Default `serve` binds 127.0.0.1; the LAN surface (:8787) is opt-in via `--lan` and token-gated.",
    "Compute endpoints (Squire / Beastmode) are 'accountability, not gating' (owner call): unauthenticated, "
    "but every send is written to the egress audit.",
    "Sensitive projects (e.g. personal memory / health data) are `no_crawl` — never off-boxed; at-rest intel redacted.",
    "Inter-project MCP traffic is NOT yet governed (no identity / allowlist / per-call audit) — the "
    "five-planes gateway (G3, fail-closed) is designed but unbuilt.",
]


def _lan_listeners() -> list[int]:
    """TCP ports on this box bound to all interfaces (0.0.0.0 / ::) — i.e. reachable from the LAN."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING":
            local = parts[1]
            if local.startswith("0.0.0.0:") or local.startswith("[::]:"):
                try:
                    ports.add(int(local.rsplit(":", 1)[-1]))
                except ValueError:
                    continue
    return sorted(ports)


def _local_exposure() -> tuple[list[dict], list[int]]:
    """Returns (notable LAN-bound ports annotated, other/generic LAN-bound ports as a bare list)."""
    notable, other = [], []
    for port in _lan_listeners():
        spec = _NOTABLE_PORTS.get(port)
        if spec:
            label, gated, detail = spec
            notable.append({"port": port, "label": label, "gated": gated, "detail": detail})
        else:
            other.append(port)
    return notable, other


def _unauthenticated_endpoints() -> list[dict]:
    out: list[dict] = []
    try:
        from .intel.endpoints import list_endpoints
        for ep in list_endpoints():
            u = urlsplit(ep.base_url)
            out.append({"name": ep.name, "endpoint": f"{u.hostname}:{u.port}" if u.hostname else ep.base_url,
                        "auth": "none (LM Studio ignores keys — accountability-not-gating)",
                        "exposure_channel": ep.exposure or None, "note": ep.note})
    except Exception:
        pass
    try:
        from .daemon_registry import _REMOTE_SERVICES, _port_open
        for s in _REMOTE_SERVICES:
            out.append({"name": s["name"], "endpoint": f"{s['host']}:{s['port']}",
                        "auth": "none (LAN-open)", "reachable": _port_open(s["host"], s["port"]),
                        "code_exec": s["name"] == "comfyui-mcp", "note": s["role"]})
    except Exception:
        pass
    return out


def _egress_summary(store) -> dict:
    try:
        rows = store.list_egress(days=30, limit=500)
    except Exception:
        return {"available": False}
    by_endpoint: dict = {}
    exposures: set = set()
    for r in rows:
        by_endpoint[r.get("endpoint")] = by_endpoint.get(r.get("endpoint"), 0) + 1
        if r.get("exposure"):
            exposures.add(r.get("exposure"))
    return {"available": True, "sends_30d": len(rows), "by_endpoint": by_endpoint,
            "exposure_channels": sorted(exposures), "latest": rows[0].get("created_at") if rows else None}


def posture(store) -> dict:
    """Honest, read-only security picture. Evidence + the trusted-LAN caveat; never a safe/unsafe score."""
    notable, other = _local_exposure()
    unauth = _unauthenticated_endpoints()
    ungated = [e for e in notable if not e["gated"]]
    code_exec = [e for e in unauth if e.get("code_exec")]

    soft_spots = []
    risky_os = [e["label"] for e in ungated if e["label"].startswith("Windows")]
    if risky_os:
        soft_spots.append("LAN-exposed OS services: " + ", ".join(risky_os))
    if code_exec:
        soft_spots.append("ComfyUI MCP is an unauthenticated, code-execution-capable LAN surface")
    soft_spots.append("inter-project MCP traffic is ungoverned (no identity / allowlist / per-call audit)")

    summary = (
        "BigBoss does NOT compute a safe/unsafe verdict — this is the evidence. Posture is SAFE ONLY IF THE "
        "LAN IS FULLY TRUSTED; the design rests on that assumption. BigBoss's own LAN surface is just the "
        "token-gated dashboard/ingest (:8787); its admin paths are loopback-only. If the LAN is not fully "
        "trusted, the soft spots are: " + "; ".join(soft_spots) + f" (+ {len(other)} generic Windows/service "
        "ports). This is visibility, not control — enforcement is the (still-unbuilt) five-planes gateway."
    )
    return {
        "local_lan_exposure": notable,
        "other_lan_ports": {"count": len(other), "ports": other},
        "unauthenticated_endpoints": unauth,
        "egress_audit": _egress_summary(store),
        "ungoverned_inter_project_mcp": True,
        "rulings": _RULINGS,
        "summary": summary,
    }
