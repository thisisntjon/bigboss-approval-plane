"""P-ops.2a — read-only daemon awareness for BigBoss.

Reuses the ecosystem daemon registry (`~/.claude/daemons.json`, owned by the `/daemon` skill) as
METADATA ONLY — it never runs the registered `status_cmd` (no model-triggered shell) — plus honest
reachability/health probes of BigBoss's OWN service daemons. Presents evidence + `healthy_when`
guidance; never asserts up/idle where it can't actually know (the `squire_probe` lesson).

Restart / start / stop and writing to the registry are the DESTRUCTIVE half — deferred to P-ops.2b,
gated (and `restart_elevated` daemons emit a paste-block, never a silent privilege failure).
"""
from __future__ import annotations

import json
import os
import socket
import urllib.request
from pathlib import Path

_REGISTRY = Path.home() / ".claude" / "daemons.json"

# BigBoss's own service daemons — honest port/health probes (loopback).
_SERVICES = [
    {"name": "lan-ingest", "host": "127.0.0.1", "port": 8787, "health": "/api/health",
     "role": "approval plane + the remote host registry ingest (serve --lan; scheduled task 'BigBoss LAN Ingest')"},
    {"name": "cost-router", "host": "127.0.0.1", "port": 4000, "health": None,
     "role": "loopback Anthropic metering proxy (bigboss router)"},
    {"name": "squire-proxy", "host": "127.0.0.1", "port": 1235, "health": None,
     "role": "Squire metering proxy (bigboss squire-proxy)"},
]

# Remote/ecosystem endpoints BigBoss WATCHES but does not own — reachability only (no over-claim; a
# closed port just means unreachable-from-here). Governing this MCP traffic is the separate G-roadmap bet.
# The remote box is whatever host SQUIRE_BASE_URL points at (env-overridable; localhost by default).
def _remote_host() -> str:
    from urllib.parse import urlparse
    from .intel.squire import DEFAULT_BASE_URL
    return urlparse(os.environ.get("SQUIRE_BASE_URL") or DEFAULT_BASE_URL).hostname or "127.0.0.1"


_REMOTE_HOST = _remote_host()
_REMOTE_SERVICES = [
    {"name": "squire-lmstudio", "host": _REMOTE_HOST, "port": 1234, "health": None,
     "role": "Squire (LM Studio) on the remote host — the shared compute endpoint. Reachability only; "
             "live job/queue come from the remote host's self-reported snapshot (see the Squire panel), not this probe"},
    {"name": "comfyui-web", "host": _REMOTE_HOST, "port": 8188, "health": None,
     "role": "ComfyUI web UI on the remote host (LAN-opened, unauthenticated)"},
    {"name": "comfyui-mcp", "host": _REMOTE_HOST, "port": 8000, "health": None,
     "role": "ComfyUI MCP server on the remote host (SSE :8000/sse; LAN-opened, unauthenticated) — a governable spoke (five-planes G1/G3)"},
]


# BigBoss's own daemons, for P-ops.2b registration INTO ~/.claude/daemons.json (merge-preserving).
# All non-elevated: the scheduled-task restart is an S4U task the operator owns; the proxies are plain commands.
BIGBOSS_DAEMON_ENTRIES = {
    "bigboss-lan-ingest": {
        "project": "BigBoss",
        "restart_cmd": 'Stop-ScheduledTask -TaskName "BigBoss LAN Ingest"; Start-ScheduledTask -TaskName "BigBoss LAN Ingest"',
        "restart_elevated": False,
        "healthy_when": "GET http://127.0.0.1:8787/api/health returns ok",
        "dashboard_url": "http://127.0.0.1:8787/",
        "notes": "approval plane + the remote host ingest (serve --lan). Registered by bigboss daemon register.",
    },
    "bigboss-router": {
        "project": "BigBoss", "restart_cmd": "bigboss router --port 4000", "restart_elevated": False,
        "healthy_when": "port 4000 listening (loopback Anthropic metering proxy)",
        "notes": "cost router. Registered by bigboss daemon register.",
    },
    "bigboss-squire-proxy": {
        "project": "BigBoss", "restart_cmd": "bigboss squire-proxy --port 1235", "restart_elevated": False,
        "healthy_when": "port 1235 listening (Squire metering proxy)",
        "notes": "squire proxy. Registered by bigboss daemon register.",
    },
}


def get_entry(name: str, path: Path | None = None) -> dict | None:
    """Return the RAW registry entry for one daemon (incl. restart_cmd/restart_elevated) — used by
    the gated restart apply. Distinct from load_registry(), which strips the command fields."""
    reg = path or _REGISTRY
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(name) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    return {"name": name, **entry}


def register(entries: dict | None = None, path: Path | None = None) -> list[str]:
    """Merge-preserving write of BigBoss's own daemons INTO ~/.claude/daemons.json. Updates ONLY the
    given keys; every foreign entry (toolkit, CareerPipeline, ...) is preserved. Returns the keys written."""
    reg = path or _REGISTRY
    to_write = entries if entries is not None else BIGBOSS_DAEMON_ENTRIES
    try:
        data = json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for name, entry in to_write.items():
        data[name] = entry
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return list(to_write.keys())


def load_registry() -> list[dict]:
    """Registered ecosystem daemons — METADATA ONLY. `status_cmd` is deliberately NOT run here."""
    try:
        data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    items = data.items() if isinstance(data, dict) else []
    for name, d in items:
        if isinstance(d, dict):
            out.append({
                "name": name,
                "project": d.get("project"),
                "dashboard_url": d.get("dashboard_url") or None,
                "healthy_when": d.get("healthy_when") or None,
                "restart_elevated": bool(d.get("restart_elevated")),
                "notes": d.get("notes") or None,
            })
    return out


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_ok(host: str, port: int, path: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def service_status(services: list[dict] | None = None) -> list[dict]:
    """Honest reachability of a service list (defaults to BigBoss's own services). Never raises."""
    out = []
    for s in (services if services is not None else _SERVICES):
        listening = _port_open(s["host"], s["port"])
        health_ok = None
        if s["health"]:
            health_ok = _health_ok(s["host"], s["port"], s["health"]) if listening else False
        out.append({"name": s["name"], "role": s["role"], "endpoint": f"{s['host']}:{s['port']}",
                    "listening": listening, "health_ok": health_ok})
    return out


def status() -> dict:
    """Read-only daemon picture: BigBoss's own services + watched remote endpoints + registered-daemon metadata."""
    return {
        "bigboss_services": service_status(_SERVICES),
        "remote_services": service_status(_REMOTE_SERVICES),
        "registered_daemons": load_registry(),
        "note": ("`listening` = a TCP connect from this box succeeded right now; `health_ok` = the health "
                 "endpoint returned 2xx (null = no health endpoint — judge by `listening`). `remote_services` "
                 "are LAN endpoints on other machines BigBoss watches but does not own (reachability only). "
                 "Registered daemons show metadata + `healthy_when` guidance only; their status_cmd is NOT run "
                 "here (use the `/daemon` skill to probe those, and judge health by `healthy_when` — e.g. "
                 "heartbeat freshness — not raw PID/task state, which is misleading under S4U)."),
    }
