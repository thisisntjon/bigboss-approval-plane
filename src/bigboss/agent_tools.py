"""M6.3 — read-only agent tools for the Big Boss chat. Each tool queries BigBoss's OWN local state
(portfolio / registry / Squire ledger) — data that already rides in the chat system prompt, so no
new egress boundary is crossed. Vendor-neutral specs translate to Anthropic / OpenAI / Gemini shapes.

Deliberately NOT here: `brainz_search`. Brainz recall is fail-closed / display-only (`brainz.may_share`
denies off-box unless content is provably public+exportable, which it can't assert) — it must never
become a model-callable tool until a Brainz-side gated endpoint lands. `test_agent_tools` guards this.
Write tools (reprioritize / reap / spend) are M6.4, behind the approval spine.
"""
from __future__ import annotations

from typing import Any, Callable


def _portfolio_intel(store, **_) -> dict:
    from .council.prioritize import build_portfolio_context
    return {"projects": build_portfolio_context(store), "squire": store.squire_status()}


def _project_status(store, slug: str = "", **_) -> dict:
    from .council.prioritize import build_portfolio_context
    want = (slug or "").strip().lower()
    if not want:
        return {"error": "slug is required"}
    for c in build_portfolio_context(store):
        if (c.get("slug") or "").lower() == want:
            return c
    return {"error": f"no active project with slug {slug!r} (try registry_list for slugs)"}


def _squire_status(store, **_) -> dict:
    return store.squire_status()


def _host_port(base_url: str) -> tuple[str | None, str | None]:
    from urllib.parse import urlsplit
    u = urlsplit(base_url)
    return u.hostname, (str(u.port) if u.port else None)


def _active_connections(host: str, port: str) -> int | None:
    """Count ESTABLISHED TCP connections from this box to host:port (netstat foreign-addr match)."""
    import subprocess
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return None
    needle = f"{host}:{port}"
    n = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[2] == needle and parts[3] == "ESTABLISHED":
            n += 1
    return n


def _squire_probe(store, **_) -> dict:
    """LIVE probe: is Squire reachable right now + what's loaded, plus the usage ledger. Cannot see
    Squire's true busy/idle state (that lives on the remote host) — see the note; never claim 'idle' from here."""
    import json as _json
    import urllib.request
    from .intel.endpoints import get_endpoint

    base = get_endpoint("squire").base_url.rstrip("/")
    host, port = _host_port(base)
    live: dict = {"endpoint": base, "up": False, "models": [], "bigboss_to_squire_conns_now": None}
    try:
        req = urllib.request.Request(base + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        live["up"] = True
        live["models"] = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception as exc:  # unreachable / timeout / bad body — report down, never raise
        live["error"] = f"{type(exc).__name__}: {exc}"
    if host and port:
        live["bigboss_to_squire_conns_now"] = _active_connections(host, port)
    return {
        "live": live,
        "ledger": store.squire_status(),
        "note": ("`bigboss_to_squire_conns_now` counts only connections FROM this box, at this instant — "
                 "it is NOT a busy/idle signal for Squire: clients connect per-request (so it flickers to "
                 "0 between calls) and clients local to the remote host or on other hosts are invisible from here. Do NOT "
                 "infer that Squire is idle from a zero count. Squire's true busy/idle state and the "
                 "in-flight job are only knowable on the remote host (LM Studio server logs / `lms ps`)."),
    }


def _daemon_status(store, **_) -> dict:
    from .daemon_registry import status
    return status()


def _security_posture(store, **_) -> dict:
    from .security_posture import posture
    return posture(store)


def _harness_sessions(store, days: int = 7, project: str = "", host: str = "", **_) -> dict:
    try:
        window = int(days)
    except (TypeError, ValueError):
        window = 7
    rows = store.list_harness_sessions(
        days=window, project=(project or None), host=(host or None), limit=100
    )
    sessions = [
        {"harness": r.get("harness"), "vendor": r.get("vendor"), "host": r.get("host") or "",
         "project": r.get("project"), "title": r.get("title"), "summary": r.get("summary"),
         "files_touched": r.get("files_touched", []), "next_steps": r.get("next_steps", []),
         "when": (r.get("created_at") or "")[:16]}
        for r in rows
    ]
    return {"window_days": window, "count": len(sessions), "sessions": sessions}


def _squire_activity(store, **_) -> dict:
    """Latest remote-reported Squire live-job/queue snapshots, WITH provenance + staleness.
    Never asserts Squire's live state independently — surfaces what a remote box reported."""
    snaps = store.get_remote_snapshots(kind="squire")
    out = []
    for s in snaps:
        p = s.get("payload") or {}
        out.append({
            "host": s.get("host"), "age_seconds": s.get("age_seconds"),
            "reported_at": s.get("reported_at"),
            "live_job": p.get("live_job"), "queue": p.get("queue"),
            "depth": p.get("depth"), "models": p.get("models"),
        })
    return {
        "count": len(out),
        "snapshots": out,
        "note": ("These are REMOTE self-reports (e.g. from the remote host), not independently verified. "
                 "Judge freshness by age_seconds; a stale snapshot is not Squire's current state. "
                 "Never claim Squire is idle/busy from this — that truth lives only on the remote box."),
    }


def _registry_list(store, **_) -> dict:
    rows = store.list_projects(include_ambiguous=False)
    return {"projects": [
        {"slug": p.get("slug"), "lifecycle": p.get("lifecycle"), "ownership": p.get("ownership"),
         "pinned": bool(p.get("pinned")), "pin_rank": p.get("pin_rank"),
         "last_activity": (p.get("last_activity") or "")[:10]}
        for p in rows
    ]}


# name -> (description, JSON-schema for args, callable). Schemas kept flat + well-described for
# small-model reliability.
_TOOLS: dict[str, tuple[str, dict, Callable]] = {
    "portfolio_intel": (
        "BigBoss's portfolio brain: every active project with its digested status, blockers, and "
        "roadmap, plus Squire compute health. Use for 'what am I working on', priorities, blockers.",
        {"type": "object", "properties": {}, "required": []},
        _portfolio_intel,
    ),
    "project_status": (
        "Digested status / blockers / roadmap for ONE project, by slug.",
        {"type": "object",
         "properties": {"slug": {"type": "string", "description": "project slug, e.g. 'bigboss'"}},
         "required": ["slug"]},
        _project_status,
    ),
    "squire_status": (
        "Squire (the LM Studio compute endpoint on the remote host) health + per-client usage from BigBoss's "
        "ledger. Note: this is BigBoss's view (is it up, who used it, how much) — NOT the remote host's live task.",
        {"type": "object", "properties": {}, "required": []},
        _squire_status,
    ),
    "squire_probe": (
        "LIVE probe of Squire right now: is the endpoint reachable this instant + which models are "
        "loaded, plus the recent-usage ledger. Prefer this over squire_status for 'what is Squire doing "
        "right now'. IMPORTANT: it CANNOT tell whether Squire is busy or idle, and does NOT show the "
        "in-flight job — those live only on the remote host (LM Studio logs). Read the returned `note` and never "
        "claim Squire is idle from this tool.",
        {"type": "object", "properties": {}, "required": []},
        _squire_probe,
    ),
    "registry_list": (
        "Compact list of registered projects: slug, lifecycle, ownership, pinned rank, last activity.",
        {"type": "object", "properties": {}, "required": []},
        _registry_list,
    ),
    "security_posture": (
        "Read-only security-posture EVIDENCE: what's LAN-exposed on this box (and whether gated), which "
        "compute/MCP endpoints are unauthenticated (incl. ComfyUI code-exec), what data has left the box "
        "(egress audit), the ungoverned inter-project MCP gap, and the standing rulings. Use for 'is my "
        "network/computer safe'. Report the `summary` faithfully: it is EVIDENCE with a 'safe only if the "
        "LAN is trusted' caveat — never a numeric score, never a false all-clear.",
        {"type": "object", "properties": {}, "required": []},
        _security_posture,
    ),
    "daemon_status": (
        "Read-only health of BigBoss's service daemons (LAN ingest / cost router / squire-proxy — "
        "reachable? health 2xx?), plus WATCHED remote endpoints on other machines (e.g. ComfyUI web + "
        "ComfyUI MCP server on the remote host — reachability only), plus metadata for registered ecosystem "
        "daemons. Use for 'are my daemons up / is the ingest running / is ComfyUI up on the remote host'. Does NOT "
        "run registered status commands or restart anything; read the `note` and judge registered "
        "daemons by their `healthy_when` guidance.",
        {"type": "object", "properties": {}, "required": []},
        _daemon_status,
    ),
    "harness_sessions": (
        "BigBoss's cross-vendor, CROSS-MACHINE FLEET LOG: what each harness session (Claude Code, Codex, "
        "Gemini, Grok, Cursor...) reported accomplishing at wrap-up — summary, files touched, next steps, and "
        "the `host` machine it ran on (e.g. workstation vs lanbox). Use for 'what have my harnesses been up to', "
        "'what did the fleet do today', 'what is Codex working on', 'what ran on lanbox'. Optional `days` "
        "(default 7), `project`, and `host` filters. Self-reported at session end, NOT live process state.",
        {"type": "object",
         "properties": {
             "days": {"type": "integer", "description": "look-back window in days (default 7)"},
             "project": {"type": "string", "description": "filter to one project name/slug (optional)"},
             "host": {"type": "string", "description": "filter to one machine, e.g. 'lanbox' (optional)"}},
         "required": []},
        _harness_sessions,
    ),
    "squire_activity": (
        "Squire's live job + QUEUE as reported by the remote host — the coordination view for shared "
        "compute. Returns the latest snapshot per host with `age_seconds` freshness. Use for 'what is Squire "
        "working on', 'what's in the Squire queue', 'how deep is the queue'. IMPORTANT: these are REMOTE "
        "self-reports, not independently verified; read `age_seconds` (a stale snapshot is not current) and "
        "never claim Squire is idle/busy — that truth lives only on the remote host. Distinct from `squire_status` "
        "(BigBoss's own usage ledger) and `squire_probe` (this box's reachability check).",
        {"type": "object", "properties": {}, "required": []},
        _squire_activity,
    ),
}


# --- M6.4: gated WRITE tools. A write tool NEVER mutates state — it raises a human approval card via
# store.create_approval_request (policy forces any non-read-only kind to `pending`), and the actual
# mutation runs in store.resolve_approval ONLY on the operator's dashboard approval. So the model can propose,
# never commit. `reap`/`spend` stay deferred.

def _propose_reprioritize(store, slug: str = "", rank: int = 1, **_) -> dict:
    slug = (slug or "").strip().lower()
    if not slug:
        return {"error": "slug is required"}
    try:
        rank = int(rank)
    except (ValueError, TypeError):
        return {"error": "rank must be an integer (1 = highest priority)"}
    known = {(p.get("slug") or "").lower() for p in store.list_projects(include_ambiguous=True)}
    if slug not in known:
        return {"error": f"no project with slug {slug!r} — call registry_list for valid slugs"}
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"Reprioritize: pin '{slug}' to #{rank}?",
        "summary": (f"Big Boss proposes pinning project '{slug}' to priority rank {rank}. "
                    "Approve on the dashboard to apply — nothing changes until you do."),
        "proposed_action": {"kind": "agent_reprioritize", "slug": slug, "rank": rank},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: pin '{slug}' to #{rank}. "
                        "the operator must approve it on the dashboard — no change has been made yet.")}


def _propose_exclude(store, slug: str = "", excluded: bool = True, **_) -> dict:
    slug = (slug or "").strip().lower()
    if not slug:
        return {"error": "slug is required"}
    known = {(p.get("slug") or "").lower() for p in store.list_projects(include_ambiguous=True)}
    if slug not in known:
        return {"error": f"no project with slug {slug!r} — call registry_list for valid slugs"}
    excluded = bool(excluded)
    verb = "exclude" if excluded else "re-include"
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"{verb.capitalize()} '{slug}' in prioritization?",
        "summary": (f"Big Boss proposes {verb}-ing project '{slug}' "
                    f"{'from' if excluded else 'into'} the Council's prioritization context (intel is "
                    "kept either way). Approve on the dashboard to apply — nothing changes until you do."),
        "proposed_action": {"kind": "agent_exclude", "slug": slug, "excluded": excluded},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: {verb} '{slug}'. The operator must approve it on the "
                        "dashboard — no change has been made yet.")}


def _propose_set_context(store, slug: str = "", notes: str = "", **_) -> dict:
    slug = (slug or "").strip().lower()
    notes = (notes or "").strip()
    if not slug:
        return {"error": "slug is required"}
    if not notes:
        return {"error": "notes is required (the authoritative context to attach)"}
    known = {(p.get("slug") or "").lower() for p in store.list_projects(include_ambiguous=True)}
    if slug not in known:
        return {"error": f"no project with slug {slug!r} — call registry_list for valid slugs"}
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"Set a context note on '{slug}'?",
        "summary": (f"Big Boss proposes attaching an authoritative note to project '{slug}':\n\n"
                    f"{notes[:600]}\n\nApprove on the dashboard to apply — nothing changes until you do."),
        "proposed_action": {"kind": "agent_set_context", "slug": slug, "notes": notes},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: set a context note on '{slug}'. The operator must "
                        "approve it on the dashboard — no change has been made yet.")}


# Cross-machine control — v1 action allowlist (all reversible Squire-queue ops). Unknown
# actions are rejected at propose time; the remote re-validates against its own allowlist.
REMOTE_COMMAND_ACTIONS = {
    "queue.pause": (),
    "queue.resume": (),
    "job.reprioritize": ("job_id",),
    "job.cancel": ("job_id",),
}


def _propose_remote_command(store, action: str = "", host: str = "lanbox", args: dict | None = None, **_) -> dict:
    action = (action or "").strip()
    host = (host or "lanbox").strip().lower()
    args = args or {}
    if action not in REMOTE_COMMAND_ACTIONS:
        return {"error": f"unknown action {action!r}. Allowed: {sorted(REMOTE_COMMAND_ACTIONS)}"}
    required = REMOTE_COMMAND_ACTIONS[action]
    missing = [a for a in required if not str(args.get(a) or "").strip()]
    if missing:
        return {"error": f"action {action!r} requires args: {missing}"}
    pretty = action + (f" {args}" if args else "")
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"Command @{host}: {action}?",
        "summary": (f"Big Boss proposes sending a control command to '{host}': {pretty}. "
                    "This is a reversible queue op. Approve on the dashboard to issue it — the remote "
                    "polls, executes, and acks. Nothing is sent until you approve."),
        "proposed_action": {"kind": "remote_command", "host": host, "action": action, "args": args},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: {action} @{host}. The operator must approve it on the "
                        "dashboard — nothing has been sent to the remote yet.")}


def _propose_reap(store, role: str = "", **_) -> dict:
    """Propose reaping the CURRENT orphaned processes (dead-parent servers). Computes the
    exact set now (allowlist + create-time guard + self/ancestor protection) and proposes
    only those PIDs; the apply re-validates orphan-ness at approval time (PID reuse guard)."""
    from . import procman
    procs = procman.list_processes()
    protect = procman.current_protect_pids(procs)
    orphans = procman.find_orphans(procs, protect_pids=protect)
    want = (role or "").strip().lower()
    if want:
        orphans = [p for p in orphans if want in procman._role_key(p).lower()]
    if not orphans:
        return {"info": "No orphaned processes to reap right now.", "targets": []}
    targets = [{"pid": p.pid, "name": p.name, "role": procman._role_key(p),
                "create_time": p.create_time} for p in orphans]
    names = ", ".join(f"{t['name']}#{t['pid']}" for t in targets[:8])
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"Reap {len(targets)} orphaned process(es)?",
        "summary": (f"Big Boss proposes terminating {len(targets)} orphaned process(es) (dead parent): "
                    f"{names}. Irreversible. The apply re-checks each PID is still orphaned (with a "
                    "create-time match) before killing. Approve on the dashboard — nothing is killed until you do."),
        "proposed_action": {"kind": "reap_process", "targets": targets},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: reap {len(targets)} orphan(s). This is HIGH-risk "
                        "(irreversible) — the operator must approve it on the dashboard; nothing has been killed yet.")}


def _propose_daemon_restart(store, name: str = "", **_) -> dict:
    """Propose restarting a registered daemon. Elevated daemons can't be restarted by BigBoss
    (it runs unelevated) — the apply hands the operator a paste-block instead of faking success."""
    from . import daemon_registry
    name = (name or "").strip()
    if not name:
        return {"error": "name is required (a registered daemon name — see `bigboss daemons`)"}
    entry = daemon_registry.get_entry(name)
    if entry is None:
        return {"error": f"no registered daemon {name!r}. Run `bigboss daemon register`, or check the name."}
    if not entry.get("restart_cmd"):
        return {"error": f"daemon {name!r} has no restart_cmd registered."}
    elevated = bool(entry.get("restart_elevated"))
    tail = (" (elevated — you'll get a paste-block to run yourself; BigBoss runs unelevated)"
            if elevated else "")
    card = store.create_approval_request({
        "harness": "bigboss", "workspace": "", "run_title": "Big Boss chat",
        "title": f"Restart daemon '{name}'?",
        "summary": (f"Big Boss proposes restarting daemon '{name}'{tail}. Approve on the dashboard to "
                    "apply — nothing restarts until you do."),
        "proposed_action": {"kind": "daemon_restart", "name": name,
                            "restart_cmd": entry.get("restart_cmd"),
                            "restart_elevated": elevated, "project": entry.get("project")},
    })
    return {"status": "approval_requested", "approval_id": card["id"],
            "message": (f"Raised approval card {card['id']}: restart '{name}'. The operator must approve it on the "
                        "dashboard — nothing has restarted yet.")}


_WRITE_TOOLS: dict[str, tuple[str, dict, Callable]] = {
    "propose_daemon_restart": (
        "Propose restarting a registered background daemon/service by name (see `bigboss daemons`). Raises "
        "a human approval card; restarts nothing until the operator approves. Elevated daemons hand the operator a paste-block "
        "(BigBoss runs unelevated). Use when the user asks to restart a daemon/service.",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "registered daemon name, e.g. 'bigboss-router'"}},
         "required": ["name"]},
        _propose_daemon_restart,
    ),
    "propose_reap": (
        "Propose terminating CURRENTLY orphaned processes (servers whose parent session died — the "
        "accumulation `ps`/`reap` finds). Raises a HIGH-risk human approval card; kills nothing until the operator "
        "approves, and re-validates each PID is still orphaned at approval time. Optional `role` substring "
        "filter. Use when the user asks to clean up / reap orphaned processes.",
        {"type": "object",
         "properties": {"role": {"type": "string", "description": "optional role substring filter, e.g. 'node'"}},
         "required": []},
        _propose_reap,
    ),
    "propose_remote_command": (
        "Propose a CONTROL command for another machine (e.g. a GPU box on the LAN) to run against its Squire queue: "
        "queue.pause, queue.resume, job.reprioritize {job_id, rank}, job.cancel {job_id}. Raises a human "
        "approval card; nothing is sent until the operator approves. On approval BigBoss records the command and the "
        "remote pulls + executes + acks. Use when the user asks to pause/resume/reprioritize/cancel remote work.",
        {"type": "object",
         "properties": {
             "action": {"type": "string", "description": "queue.pause | queue.resume | job.reprioritize | job.cancel"},
             "host": {"type": "string", "description": "target machine label, default 'lanbox'"},
             "args": {"type": "object", "description": "e.g. {job_id, rank} for job ops"}},
         "required": ["action"]},
        _propose_remote_command,
    ),
    "propose_reprioritize": (
        "Propose pinning a project to a priority rank (1 = highest). This does NOT change anything — "
        "it raises a human approval card the operator must approve on the dashboard. Use when the user asks to "
        "reprioritize or pin a project.",
        {"type": "object",
         "properties": {"slug": {"type": "string", "description": "project slug, e.g. 'sampleapp'"},
                        "rank": {"type": "integer", "description": "priority rank, 1 = highest"}},
         "required": ["slug", "rank"]},
        _propose_reprioritize,
    ),
    "propose_exclude": (
        "Propose excluding a project from (or re-including it in) the Council's prioritization context "
        "— keeps its intel, just drops it from ranking. Raises a human approval card; changes nothing "
        "until the operator approves. Use when the user asks to exclude / re-include / stop prioritizing a project.",
        {"type": "object",
         "properties": {"slug": {"type": "string", "description": "project slug"},
                        "excluded": {"type": "boolean",
                                     "description": "true = exclude, false = re-include (default true)"}},
         "required": ["slug"]},
        _propose_exclude,
    ),
    "propose_set_context": (
        "Propose attaching an authoritative context note to a project. Raises a human approval card; "
        "changes nothing until the operator approves. Use when the user asks to record a note / context about a project.",
        {"type": "object",
         "properties": {"slug": {"type": "string", "description": "project slug"},
                        "notes": {"type": "string", "description": "the note/context to attach"}},
         "required": ["slug", "notes"]},
        _propose_set_context,
    ),
}

_ALL_TOOLS: dict[str, tuple[str, dict, Callable]] = {**_TOOLS, **_WRITE_TOOLS}


def read_tool_names() -> list[str]:
    return list(_TOOLS)


def write_tool_names() -> list[str]:
    return list(_WRITE_TOOLS)


def tool_names() -> list[str]:
    return list(_ALL_TOOLS)


def execute(name: str, args: dict | None, store) -> dict:
    """Run a tool. Read tools return data; WRITE tools only raise an approval card (never mutate).
    Never raises — errors come back as {'error': ...}."""
    spec = _ALL_TOOLS.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return spec[2](store, **(args or {}))
    except Exception as exc:  # surface failures as data, never crash the loop
        return {"error": f"{type(exc).__name__}: {exc}"}


# --- per-vendor tool-schema translation ---------------------------------------

def anthropic_tools() -> list[dict]:
    return [{"name": n, "description": d, "input_schema": s} for n, (d, s, _f) in _ALL_TOOLS.items()]


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": s}}
            for n, (d, s, _f) in _ALL_TOOLS.items()]


def gemini_tools() -> list[dict]:
    return [{"function_declarations": [
        {"name": n, "description": d, "parameters": s} for n, (d, s, _f) in _ALL_TOOLS.items()]}]
