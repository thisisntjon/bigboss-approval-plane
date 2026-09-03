from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .security import read_or_create_lan_pin, read_or_create_secret, token_hash
from .hook_adapter import main as hook_main
from .mcp_stdio import MCPStdioServer
from .hosts import local_ip_hint
from .ops import (
    ServeState,
    acquire_pidfile,
    assert_serve_port_available,
    clear_serve_state,
    open_browser,
    read_serve_state,
    reclaim_previous_serve,
    release_pidfile,
    wait_for_health,
    write_serve_state,
)
from .pairing import DEFAULT_DEVICE_NAME, issue_pair_code, pairing_host
from .server import ApprovalHTTPServer, demo_payload
from .store import Store


REPO_ROOT = Path(__file__).resolve().parents[2]
# Pinned to the repo root (E4a.1): a cwd-relative default sprayed .bigboss state
# (tokens + DB) into whatever project dir a harness MCP session happened to run
# from. BIGBOSS_DATA_DIR overrides; --data-dir still wins over both.
DEFAULT_DATA_DIR = Path(os.environ.get("BIGBOSS_DATA_DIR") or REPO_ROOT / ".bigboss")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bigboss", description="Local harness approval dashboard."
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="State directory. Defaults to <repo>/.bigboss (override with BIGBOSS_DATA_DIR).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the local approval web server.")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host. Defaults to loopback (local dashboard); use --lan for LAN/phone access.",
    )
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Bind 0.0.0.0 for LAN/phone access (pairing desk, LAN PIN, phone URLs).",
    )
    serve.add_argument("--port", type=int, default=8787, help="Bind port.")
    serve.add_argument(
        "--intel-interval-hours",
        type=float,
        default=6.0,
        help="Background project-intel digest cadence in hours (0 disables).",
    )
    serve.add_argument(
        "--squire-health-interval",
        type=float,
        default=300.0,
        help="Squire health probe cadence in seconds (0 disables).",
    )
    serve.add_argument(
        "--no-open-pair",
        "--no-open",
        action="store_true",
        help="Do not open a browser on startup (dashboard in local mode, pairing desk in --lan mode).",
    )
    serve.add_argument(
        "--tunnel",
        action="store_true",
        help="Expose the server publicly via a secure, transient reverse SSH tunnel.",
    )

    open_cmd = subparsers.add_parser(
        "open",
        help="Open the local dashboard in your browser, auto-pairing this workstation if needed.",
    )
    open_cmd.add_argument("--port", type=int, default=8787, help="Server port.")

    pair_phone = subparsers.add_parser(
        "pair",
        help="Show the pairing QR (starts the server automatically if needed). Pin this command.",
    )
    pair_phone.add_argument("--port", type=int, default=8787, help="Server port.")
    pair_phone.add_argument("--host", default="0.0.0.0", help="Bind host when auto-starting serve.")

    pair = subparsers.add_parser("pair-code", help="Create a one-time phone pairing code.")
    pair.add_argument("--name", default="Phone", help="Device name for the paired phone.")
    pair.add_argument("--port", type=int, default=8787, help="Server port for the claim URL.")
    pair.add_argument("--ttl", type=int, default=3600, help="Pair code lifetime in seconds.")

    lan_pin = subparsers.add_parser(
        "lan-pin", help="Show or rotate the LAN PIN used for phone re-pairing."
    )
    lan_pin.add_argument("--rotate", action="store_true", help="Generate a new PIN.")

    devices = subparsers.add_parser("devices", help="List paired phone devices.")
    devices.add_argument("--all", action="store_true", help="Include revoked devices.")

    subparsers.add_parser("secrets", help="Print local admin and adapter token file locations.")
    subparsers.add_parser("demo-request", help="Insert a demo approval request.")
    subparsers.add_parser("mcp-stdio", help="Run the BigBoss MCP server over stdio.")
    hook = subparsers.add_parser(
        "hook-approval", help="Bridge a harness hook payload into BigBoss."
    )
    hook.add_argument("--harness", required=True)
    hook.add_argument("--url", default=None)
    hook.add_argument("--adapter-token", default=None)
    hook.add_argument("--adapter-token-file", default=None)
    hook.add_argument("--timeout", type=int, default=600)
    hook.add_argument("--no-wait", action="store_true")
    hook.add_argument("--mode", choices=["approval", "update", "auto"], default="approval")

    codex_run = subparsers.add_parser(
        "codex-run", help="Run one Codex app-server turn, routing approvals through BigBoss."
    )
    codex_run.add_argument("prompt", help="Task prompt for Codex.")
    codex_run.add_argument(
        "--cwd", default=None, help="Working directory for Codex (default: cwd)."
    )
    codex_run.add_argument("--model", default=None, help="Codex model override.")
    codex_run.add_argument(
        "--timeout", type=int, default=900, help="Per-approval wait timeout in seconds."
    )
    codex_run.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
    )
    codex_run.add_argument("--approval-policy", default="untrusted")

    router = subparsers.add_parser(
        "router", help="Run the loopback Anthropic metering proxy (Ecosystem Phase E1)."
    )
    router.add_argument(
        "--port", type=int, default=4000, help="Loopback bind port (127.0.0.1 only)."
    )
    router.add_argument(
        "--harness",
        default="agent-sdk",
        help="Label recorded for calls (e.g. agent-sdk, claude-p).",
    )
    router.add_argument(
        "--hard-cap", default=None, help="Monthly advisory cap in USD (default: existing or 200)."
    )

    regrefresh = subparsers.add_parser(
        "registry-refresh", help="Harvest + reconcile the project registry (Ecosystem Phase E3)."
    )
    regrefresh.add_argument(
        "--prune",
        action="store_true",
        help="Tombstone local projects that vanished since the last refresh (never touches remote/pinned).",
    )
    reglist = subparsers.add_parser("registry-list", help="Print the project portfolio.")
    reglist.add_argument("--all", action="store_true", help="Include ambiguous/hidden roots.")

    regingest = subparsers.add_parser(
        "registry-ingest",
        help="Ingest a remote crawl bundle (e.g. a remote machine's project drive) into the registry (E3.6).",
    )
    regingest.add_argument("bundle", help="Path to a crawl-bundle JSON file (bundle_version 1).")
    regingest.add_argument(
        "--prune",
        action="store_true",
        help="Full-sync: tombstone this host's rows absent from the bundle (never local/other hosts/pinned).",
    )

    council = subparsers.add_parser(
        "council-ask",
        help="Run the multi-model Council on a question (M1). --fixture = deterministic offline mode.",
    )
    council.add_argument("question", nargs="?", default=None, help="The question for the Council.")
    council.add_argument(
        "--fixture",
        nargs="?",
        const="__default__",
        default=None,
        help="Offline deterministic mode (no keys). Optional scenario id (e.g. car_wash_50m).",
    )
    council.add_argument("--json", action="store_true", help="Emit the full report as JSON.")
    council.add_argument("--no-context", action="store_true",
                         help="Skip injecting the operator's portfolio/self context into the Council (live mode).")

    council_lb = subparsers.add_parser(
        "council-leaderboard",
        help="Per-model verified-claim accuracy accrued from live council runs (the throne's track record).",
    )
    council_lb.add_argument("--days", type=int, default=90, help="Window in days (default 90).")

    council_pri = subparsers.add_parser(
        "council-prioritize",
        help="Have the Council rank the portfolio into a top-N priority shortlist (M2), gated by an approval card.",
    )
    council_pri.add_argument("--top", type=int, default=10, help="Shortlist size (default 10).")
    council_pri.add_argument("--json", action="store_true", help="Emit the full result as JSON.")

    chat_cmd = subparsers.add_parser(
        "chat", help="Interactive terminal chat (fast cheap seat; /council + /fable to escalate a turn).",
    )
    chat_cmd.add_argument("--seat", default=None, help="Chat seat: claude|gpt|gemini|grok (default claude/Haiku).")
    chat_cmd.add_argument("--model", default=None, help="Override the seat's model id for this session.")
    chat_cmd.add_argument("--system", default=None, help="System prompt (overrides the default Big Boss context).")
    chat_cmd.add_argument("--no-context", action="store_true", help="Skip the Big Boss self-knowledge system prompt.")
    chat_cmd.add_argument("--save", default=None, help="Append the transcript to this JSONL file.")

    brecall_cmd = subparsers.add_parser(
        "brainz-recall", help="Search Brainz for relevant past conversations (display-only; never sent off-box).",
    )
    brecall_cmd.add_argument("query", help="What to recall.")
    brecall_cmd.add_argument("--limit", type=int, default=6, help="Max results (default 6).")
    subparsers.add_parser("brainz-status", help="Probe the Brainz recall webapp (reachability).")

    council_eval = subparsers.add_parser(
        "council-eval",
        help="Escalation + cost report: how often the council was unsure, trigger mix, Fable escalations, cost.",
    )
    council_eval.add_argument("--days", type=int, default=30, help="Window in days (default 30).")

    council_res = subparsers.add_parser(
        "council-research",
        help="Fable-led deep research on ONE project → structured brief + phased roadmap (Track B, live).",
    )
    council_res.add_argument("slug", help="Registry slug to research.")
    council_res.add_argument("--model", default=None,
                             help="Override the research model (default COUNCIL_RESEARCH_MODEL or claude-fable-5).")
    council_res.add_argument("--json", action="store_true", help="Emit the full result as JSON.")

    ps_cmd = subparsers.add_parser(
        "ps", help="Inventory running ecosystem processes: roles, duplicates, orphans, port conflicts (P-ops).",
    )
    ps_cmd.add_argument("--all", action="store_true", help="Show every process, not just reapable server hosts.")
    reap_cmd = subparsers.add_parser(
        "reap", help="Reap ORPHANED server processes (dead parent) safely. Dry-run unless --yes.",
    )
    reap_cmd.add_argument("--yes", action="store_true", help="Actually terminate (default: dry-run/preview).")
    daemons_cmd = subparsers.add_parser(
        "daemons", help="Read-only health of BigBoss service daemons + registered ecosystem daemons (P-ops.2).",
    )
    daemons_cmd.add_argument("--json", action="store_true", help="Emit the full status as JSON.")
    daemon_cmd = subparsers.add_parser(
        "daemon", help="Manage daemons (P-ops.2b): register BigBoss's own into the registry, or restart one.",
    )
    daemon_cmd.add_argument("action", choices=["register", "restart"], help="register (BigBoss's daemons) or restart <name>.")
    daemon_cmd.add_argument("name", nargs="?", help="Daemon name (for restart).")
    security_cmd = subparsers.add_parser(
        "security", help="Read-only security-posture evidence: LAN exposure, unauthenticated endpoints, egress, ungoverned traffic.",
    )
    security_cmd.add_argument("--json", action="store_true", help="Emit the full posture as JSON.")
    reap_cmd.add_argument("--role", default=None, help="Only reap orphans whose role key contains this substring.")

    subparsers.add_parser(
        "digest-pending",
        help="List projects gated from off-box digest, awaiting batch approval (E4a.1).",
    )
    digest_approve = subparsers.add_parser(
        "digest-approve",
        help="Allow off-box digest for gated projects (CLI fallback for the approval card).",
    )
    digest_approve.add_argument("slug", nargs="*", help="Slugs to approve (omit with --all).")
    digest_approve.add_argument("--all", action="store_true", help="Approve every gated project.")

    crawl = subparsers.add_parser(
        "crawl",
        help="Crawl project planning docs into JSON bundles (general-purpose tool for Squire/harnesses).",
    )
    crawl.add_argument(
        "--project", action="append", default=None, help="Limit to a registry slug (repeatable)."
    )
    crawl.add_argument(
        "--path", default=None, help="Crawl one directory by path instead of the registry."
    )
    crawl.add_argument(
        "--full-text",
        action="store_true",
        help="Include doc text in the bundles (default: hashes + inventory only).",
    )
    crawl.add_argument(
        "--total-chars", type=int, default=22_000, help="Per-project character budget."
    )

    no_crawl_cmd = subparsers.add_parser(
        "registry-no-crawl",
        help="Exclude a project from every crawl/digest path (E4a.1); also redacts its stored egress text.",
    )
    no_crawl_cmd.add_argument("slug", help="Registry slug to exclude.")
    no_crawl_cmd.add_argument(
        "--allow", action="store_true", help="Re-allow crawling instead of excluding."
    )

    exclude_cmd = subparsers.add_parser(
        "registry-exclude",
        help="Exclude a project from portfolio prioritization (done/submitted, or work-not-home). "
             "Keeps intel intact; durable across registry-refresh.",
    )
    exclude_cmd.add_argument("slug", help="Registry slug to exclude from prioritization.")
    exclude_cmd.add_argument(
        "--include", action="store_true", help="Re-include the project instead of excluding."
    )

    baseline_cmd = subparsers.add_parser(
        "registry-baseline",
        help="Apply an agent-derived ground-truth baseline (purpose/lifecycle/ownership + fresh intel) "
             "from a JSON file produced by the deep re-baseline audit.",
    )
    baseline_cmd.add_argument("path", help="Path to the audit-results JSON (list of per-project records).")

    classify_cmd = subparsers.add_parser(
        "registry-classify",
        help="Print the portfolio classification table (lifecycle / ownership / excluded) for review.",
    )
    classify_cmd.add_argument(
        "--all", action="store_true", help="Include ambiguous/gone projects too."
    )

    intel_refresh_cmd = subparsers.add_parser(
        "intel-refresh",
        help="Digest project docs into status/blockers/roadmap via Squire (Ecosystem Phase E4a).",
    )
    intel_refresh_cmd.add_argument(
        "--project", action="append", default=None, help="Limit to a project slug (repeatable)."
    )
    intel_refresh_cmd.add_argument(
        "--force", action="store_true", help="Re-digest even if source docs are unchanged."
    )
    intel_refresh_cmd.add_argument(
        "--endpoint",
        default="squire",
        help="Which compute endpoint to use (default: squire). Opt-in guarded boxes like 'beastmode' are rate-limited.",
    )

    subparsers.add_parser(
        "squire-endpoints", help="List configured local-compute endpoints (squire, beastmode, ...)."
    )

    subparsers.add_parser(
        "intel-list", help="Print the portfolio with digested intel (status, blockers, roadmap)."
    )

    report_cmd = subparsers.add_parser(
        "session-report",
        help="Record a harness session-report into the fleet log (what a session accomplished).",
    )
    report_cmd.add_argument("--json-file", default=None, help="Read the full payload from a JSON file.")
    report_cmd.add_argument("--stdin", action="store_true", help="Read the full payload as JSON from stdin.")
    report_cmd.add_argument("--host", default=None, help="Machine that ran the session (default: this box's hostname).")
    report_cmd.add_argument("--harness", default=None, help="Harness id (claude-code, codex, gemini-cli, grok, cursor...).")
    report_cmd.add_argument("--vendor", default=None, help="Vendor (anthropic, openai, google, xai...).")
    report_cmd.add_argument("--project", default=None, help="Project name / slug.")
    report_cmd.add_argument("--workspace", default=None, help="Workspace absolute path.")
    report_cmd.add_argument("--title", default=None, help="Short session title.")
    report_cmd.add_argument("--summary", default=None, help="What the session accomplished.")
    report_cmd.add_argument("--files", default=None, help="Comma-separated files touched.")
    report_cmd.add_argument("--decisions", action="append", default=None, help="A decision made (repeatable).")
    report_cmd.add_argument("--next-steps", action="append", default=None, dest="next_steps", help="A next step (repeatable).")
    report_cmd.add_argument("--session-ref", default=None, dest="session_ref", help="External session id (idempotency key).")
    report_cmd.add_argument("--json", action="store_true", help="Emit the recorded row as JSON.")

    sessions_cmd = subparsers.add_parser(
        "sessions", help="Show the harness fleet log — what each harness session has been up to."
    )
    sessions_cmd.add_argument("--days", type=int, default=7, help="Window in days.")
    sessions_cmd.add_argument("--project", default=None, help="Filter to one project.")
    sessions_cmd.add_argument("--harness", default=None, help="Filter to one harness.")
    sessions_cmd.add_argument("--host", default=None, help="Filter to one machine (e.g. <host>).")

    commands_cmd = subparsers.add_parser(
        "commands", help="Show cross-machine control commands issued to remote hosts (status + result)."
    )
    commands_cmd.add_argument("--host", default=None, help="Filter to one machine (e.g. <host>).")
    commands_cmd.add_argument("--limit", type=int, default=50, help="Max rows to show.")
    commands_cmd.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    sessions_cmd.add_argument("--limit", type=int, default=100, help="Max rows to show.")
    sessions_cmd.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    squire_status_cmd = subparsers.add_parser(
        "squire-status", help="Print Squire health + per-client usage rollup."
    )
    squire_status_cmd.add_argument("--days", type=int, default=7, help="Rollup window in days.")

    egress_cmd = subparsers.add_parser(
        "squire-egress",
        help="Print the data-egress audit: exactly what was sent to each compute endpoint (files, hashes, sizes).",
    )
    egress_cmd.add_argument("--days", type=int, default=7, help="Window in days.")
    egress_cmd.add_argument(
        "--endpoint", default=None, help="Filter to one endpoint (e.g. beastmode)."
    )
    egress_cmd.add_argument("--limit", type=int, default=50, help="Max rows to print.")
    egress_cmd.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    egress_cmd.add_argument(
        "--full", action="store_true", help="Print full prompt/response (default: first 800 chars)."
    )

    squire_proxy = subparsers.add_parser(
        "squire-proxy",
        help="Run the Squire metering proxy (per-client usage ledger for the shared the remote host endpoint).",
    )
    squire_proxy.add_argument("--port", type=int, default=1235, help="Bind port.")
    squire_proxy.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host. 127.0.0.1 by default; 0.0.0.0 is allowed (no credential is forwarded).",
    )
    squire_proxy.add_argument(
        "--endpoint",
        default="squire",
        help="Named compute endpoint to front (default: squire). Fronting 'beastmode' meters + egress-audits it too.",
    )

    return parser


def _force_utf8_streams() -> None:
    """Make CLI output UTF-8 regardless of the console codepage.

    On a default Windows console (cp1252) any non-ASCII byte in real data — an emoji in
    a project purpose, a `→` in egress text — otherwise raises UnicodeEncodeError and
    aborts the command mid-output. `errors="replace"` degrades gracefully rather than
    crashing; json (ensure_ascii) output is unaffected.
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir)
    paths = ensure_paths(data_dir)
    store = Store(paths["db"])

    if args.command == "serve":
        return serve_app(
            store,
            paths,
            host="0.0.0.0" if args.lan else args.host,
            port=args.port,
            intel_interval_hours=args.intel_interval_hours,
            squire_health_interval=args.squire_health_interval,
            open_pair=not args.no_open_pair,
            tunnel=args.tunnel,
        )
    if args.command == "open":
        return open_dashboard(paths, port=args.port)
    if args.command == "pair":
        return pair_phone_ui(
            paths,
            host=args.host,
            port=args.port,
        )
    if args.command == "pair-code":
        pair_code = issue_pair_code(
            store,
            device_name=args.name,
            ttl_seconds=args.ttl,
            public_host=pairing_host(args.port),
        )
        print(f"Pair code: {pair_code['code']}")
        print(f"Device: {pair_code['device_name']}")
        print(f"Expires: {pair_code['expires_at']}")
        print(f"Claim URL: {pair_code['claim_url']}")
        print(f"Or open http://127.0.0.1:{args.port}/pair on this PC and scan the QR code.")
        return 0
    if args.command == "lan-pin":
        if args.rotate:
            from .security import new_lan_pin

            pin = new_lan_pin()
            paths["lan_pin"].write_text(pin + "\n", encoding="utf-8")
            print(f"LAN PIN rotated: {pin}")
            print(f"Saved to: {paths['lan_pin']}")
            return 0
        pin = read_or_create_lan_pin(paths["lan_pin"])
        print(f"LAN PIN: {pin}")
        print(f"File:    {paths['lan_pin']}")
        print("Enter this PIN on the phone to pair or re-pair.")
        return 0
    if args.command == "devices":
        rows = store.list_devices(include_revoked=args.all)
        if not rows:
            print("No paired devices.")
            return 0
        for row in rows:
            status = "revoked" if row.get("revoked_at") else "active"
            print(
                f"{row['id']}  {row['name']}  {status}  "
                f"created={row['created_at']}  last_seen={row.get('last_seen_at') or '-'}"
            )
        return 0
    if args.command == "secrets":
        print(f"Admin token:   {paths['admin']}")
        print(f"Adapter token: {paths['adapter']}")
        return 0
    if args.command == "demo-request":
        approval = store.create_approval_request(demo_payload())
        print(f"Created {approval['status']} approval: {approval['id']}")
        return 0
    if args.command == "mcp-stdio":
        return MCPStdioServer(store).run()
    if args.command == "hook-approval":
        hook_args = ["--harness", args.harness, "--timeout", str(args.timeout), "--mode", args.mode]
        if args.url:
            hook_args.extend(["--url", args.url])
        if args.adapter_token:
            hook_args.extend(["--adapter-token", args.adapter_token])
        if args.adapter_token_file:
            hook_args.extend(["--adapter-token-file", args.adapter_token_file])
        if args.no_wait:
            hook_args.append("--no-wait")
        return hook_main(hook_args)
    if args.command == "codex-run":
        from .codex_app_bridge import main as codex_main

        return codex_main(
            [
                args.prompt,
                args.cwd or str(Path.cwd()),
                args.model or "",
                str(args.timeout),
                args.sandbox,
                args.approval_policy,
            ],
            store,
        )
    if args.command == "router":
        return serve_router(store, port=args.port, harness=args.harness, hard_cap=args.hard_cap)
    if args.command == "registry-refresh":
        from .registry import refresh

        summary = refresh(store, prune=args.prune)
        pruned = f", {summary['pruned']} pruned" if summary.get("pruned") else ""
        print(
            f"Registry refreshed: {summary['discovered']} new, {summary['updated']} updated, "
            f"{summary['resolved']} projects from {summary['candidates']} candidates{pruned}."
        )
        return 0
    if args.command == "registry-ingest":
        import json as _json
        from .registry.ingest import ingest_bundle, BundleError

        try:
            bundle = _json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"ERROR: could not read bundle: {exc}")
            return 2
        try:
            summary = ingest_bundle(store, bundle, prune=args.prune)
        except BundleError as exc:
            print(f"ERROR: invalid bundle: {exc}")
            return 2
        pruned = f", {summary['pruned']} pruned" if summary.get("pruned") else ""
        print(
            f"Ingested {summary['host']!r}: {summary['discovered']} new, {summary['updated']} updated, "
            f"{summary['resolved']} projects from {summary['candidates']} candidates{pruned}."
        )
        return 0
    if args.command == "council-ask":
        import json as _json
        from .council.engine import deliberate
        from .council.fixture import FixtureError, report_to_markdown

        if args.fixture is not None:
            scenario = None if args.fixture == "__default__" else args.fixture
            try:
                report = deliberate(question=args.question, mode="fixture", scenario=scenario)
            except FixtureError as exc:
                print(f"ERROR: {exc}")
                return 2
            from .council.escalation import should_escalate
            esc = should_escalate(report)
            print(_json.dumps(report, indent=2) if args.json else report_to_markdown(report))
            if not args.json:
                verdict = f"ESCALATE ({esc['trigger']})" if esc["escalate"] else "no escalation"
                print(f"\n[escalation preview — offline, no spend] {verdict}: {esc['reason']}")
            return 0
        # Live mode (M1b): real multi-model deliberation, persisted, with per-model accuracy.
        if not args.question:
            print("Provide a question, e.g.: bigboss council-ask \"...\"  (or --fixture for offline)")
            return 2
        from .council.engine import ProviderNotConfigured, run_live_council
        from .council.cost import estimate_cost

        print("Convening the Council (live)... this makes several model calls.")
        # M6.1b — portfolio/self context by default so the Council knows the operator's ecosystem
        # (e.g. that "Squire" is the remote host compute endpoint, not a medieval attendant).
        council_context = None
        if not args.no_context:
            from .chat import _portfolio_summary
            from .capabilities import GLOSSARY
            council_context = ("The operator's project portfolio (priority-ordered):\n"
                               + _portfolio_summary(store)
                               + "\n\nKey subsystems:\n" + GLOSSARY)
        try:
            report = run_live_council(args.question, context=council_context)
        except ProviderNotConfigured as exc:
            print(f"ERROR: {exc}")
            return 2
        cost = estimate_cost(report)
        rec = store.record_council_session(report, cost_usd=cost)
        report["session_id"] = rec["session_id"]
        report["cost_usd"] = cost

        # M5a — Fable tie-break escalation. Never auto-spends Fable: raise a human-gated card unless
        # COUNCIL_AUTOTIEBREAK is opted in.
        from .council.escalation import autotiebreak_enabled, estimate_tiebreak_cost_usd, MIN_MEAN_CONFIDENCE
        esc = report.get("escalation") or {}
        esc_note = ""
        if esc.get("escalate"):
            contested = [c.get("text", "") for c in (report.get("verified_claims") or [])
                         if c.get("verdict") == "refuted" or float(c.get("confidence") or 1) < MIN_MEAN_CONFIDENCE]
            if autotiebreak_enabled():
                verdict = store.run_tiebreak_now(rec["session_id"], question=report.get("question"),
                                                 contested=contested, trigger=esc.get("trigger"),
                                                 reason=esc.get("reason"))
                if verdict.get("status") == "success":
                    report["final"]["final_answer"] = verdict["answer"]
                    esc_note = f"\n[Fable auto-tie-break applied ({esc['trigger']}); +${verdict['cost_usd']}]"
                else:
                    esc_note = f"\n[Fable tie-break failed: {verdict.get('error')}]"
            else:
                card = store.create_approval_request({
                    "harness": "bigboss", "run_title": "Council escalation",
                    "title": f"Council split ({esc['trigger']}) — bring in Fable to tie-break?",
                    "summary": (f"{esc['reason']}\n\nCouncil's tentative answer:\n"
                                f"{(report['final']['final_answer'] or '')[:600]}\n\n"
                                f"Est. Fable cost ~${estimate_tiebreak_cost_usd(report)} (approve to spend)."),
                    "proposed_action": {"kind": "fable_escalation", "session_id": rec["session_id"],
                                        "question": report.get("question"), "contested": contested,
                                        "trigger": esc.get("trigger"), "reason": esc.get("reason")},
                })
                esc_note = (f"\n⚠ Council unsure ({esc['trigger']}): {esc['reason']}\n"
                            f"  Raised escalation card {card['id']} — approve to bring in Fable "
                            f"(~${estimate_tiebreak_cost_usd(report)}).")

        if args.json:
            print(_json.dumps(report, indent=2))
        else:
            print(f"\n=== Council answer (session {rec['session_id']}) ===\n")
            print(report["final"]["final_answer"])
            print(f"\nWinner: {report['winner']} | seats: {', '.join(report['seats'])}")
            print("Per-model accuracy (verified-claim rate — the throne's track record):")
            for mid, s in report["per_model_scores"].items():
                print(f"  {mid:8} {s['score']}%  ({s['verified']}+{s['partial']}*0.5 / {s['total']} claims)")
            u = report["usage"]
            print(f"Tokens: {u['input']} in / {u['output']} out  |  est. cost ${cost}")
            if esc_note:
                print(esc_note)
        return 0
    if args.command == "council-prioritize":
        import json as _json
        from .council.cost import estimate_cost
        from .council.engine import ProviderNotConfigured
        from .council.prioritize import run_prioritization

        print("Convening the Council to prioritize the portfolio (live)...")
        try:
            result = run_prioritization(store, top_n=args.top)
        except (ProviderNotConfigured, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        order = result["consensus"]
        reasons = result["reasons"]
        ideation = result.get("ideation") or {}
        summary = "Council priority proposal. Approve to set the pin order (replaces existing pins).\n" + (
            ideation.get("brief") or result.get("overall") or ""
        )
        approval = store.create_approval_request({
            "harness": "bigboss",
            "run_title": "Council prioritization",
            "title": f"Prioritize portfolio — top {len(order)} (approve to set pin order)",
            "summary": summary,
            "proposed_action": {"kind": "council_prioritization", "order": order, "reasons": reasons,
                                "rationale": result.get("overall"), "dissent": result.get("dissent"),
                                "ideation": ideation},
        })
        store.record_council_session({
            "mode": "prioritization", "question": "Prioritize the portfolio",
            "seats": result["seats"], "winner": None,
            "final": {"final_answer": ideation.get("brief") or result.get("overall") or ""},
            "verified_claims": [], "per_model_scores": {}, "usage": result["usage"],
            "ideation": ideation,
        }, cost_usd=estimate_cost(result))
        if args.json:
            print(_json.dumps({**result, "approval_id": approval["id"]}, indent=2))
            return 0
        print(f"\n=== Proposed priority shortlist (approval card {approval['id']}) ===\n")
        for i, slug in enumerate(order, start=1):
            print(f"  {i}. {slug} — {reasons.get(slug, '')}")
        if result.get("dissent"):
            print(f"\nContested (some seats ranked, didn't make consensus): {', '.join(result['dissent'])}")
        syns = ideation.get("synergies") or []
        if syns:
            print("\n--- Synergies (the Council's ideation) ---")
            for s in syns:
                print(f"  • {' + '.join(s.get('projects', []))}: {s.get('insight', '')}")
        opps = ideation.get("opportunities") or []
        if opps:
            print("\n--- Opportunities ---")
            for o in opps:
                tag = f" [{', '.join(o.get('projects', []))}]" if o.get("projects") else ""
                print(f"  • {o.get('idea', '')}{tag}")
        tensions = ideation.get("tensions") or []
        if tensions:
            print("\n--- Tensions / trade-offs ---")
            for t in tensions:
                print(f"  • {t.get('topic', '')}: {t.get('detail', '')}")
        brief = ideation.get("brief") or result.get("overall") or ""
        if brief:
            print(f"\n{brief}")
        u = result["usage"]
        synth = ideation.get("synthesized_by")
        synth_note = f" | ideation by {synth}" if synth else (" | ideation degraded (raw synergies only)" if ideation.get("degraded") else "")
        print(f"\nSeats: {', '.join(result['seats'])} | tokens {u['input']} in / {u['output']} out{synth_note}")
        for d in result.get("dropped_seats") or []:
            print(f"  ! seat dropped: {d['seat']} — {d['reason']}")
        print(f"Approve on the dashboard or: it sets pin_rank 1..{len(order)} and unpins the rest.")
        return 0
    if args.command == "council-research":
        import json as _json
        from .council.research import run_project_research

        print(f"Fable is researching '{args.slug}' from all angles (live)...")
        try:
            result = run_project_research(store, args.slug, model=args.model)
        except KeyError as exc:
            print(str(exc))
            return 2
        if result["status"] == "error":
            print(f"ERROR: {result.get('error')}")
            return 1
        if result["status"] == "unparsed":
            print("Model returned unparseable output (first 500 chars):")
            print(result.get("raw"))
            return 1
        brief = result["brief"]
        from .council.escalation import _fable_cost_usd
        research_cost = _fable_cost_usd(result["usage"], result["model"])
        store.record_project_research(result["project_id"], args.slug, brief, result["model"],
                                      result["usage"], cost_usd=research_cost)
        if args.json:
            print(_json.dumps(result, indent=2))
            return 0
        print(f"\n=== Deep research: {args.slug}  (by {result['model']}, {result['docs_used']} docs read) ===\n")
        if brief.get("summary"):
            print(brief["summary"])
        if brief.get("current_landscape"):
            print(f"\n-- Current landscape / what's newly feasible --\n{brief['current_landscape']}")
        for label, key in (("Angles", "angles"), ("Reuse from lineage", "reuse_from_lineage"),
                           ("Risks", "risks"), ("Open questions", "open_questions")):
            items = brief.get(key) or []
            if items:
                print(f"\n-- {label} --")
                for it in items:
                    print(f"  • {it}")
        if brief.get("poc_sprint"):
            print(f"\n-- POC sprint (attempt first) --\n  {brief['poc_sprint']}")
        roadmap = brief.get("roadmap") or []
        if roadmap:
            print("\n-- Phased roadmap --")
            for ph in roadmap:
                print(f"  [{ph.get('phase','')}] {ph.get('goal','')}")
                for t in (ph.get("tasks") or []):
                    print(f"      - {t}")
        u = result["usage"]
        print(f"\ntokens {u.get('input', 0)} in / {u.get('output', 0)} out | persisted (project_research).")
        return 0
    if args.command == "ps":
        from . import procman
        from .ops import pids_on_port

        procs = procman.list_processes()
        if not procs:
            print("Could not enumerate processes on this platform.")
            return 1
        protect = procman.current_protect_pids(procs)
        orphans = {p.pid for p in procman.find_orphans(procs, protect_pids=protect)}
        reapable = [p for p in procs if p.name in procman.REAP_ALLOWLIST]
        shown = procs if args.all else reapable
        groups = procman.group_by_role(shown)
        print(f"{len(procs)} processes total; {len(reapable)} reapable server hosts; {len(orphans)} orphaned.\n")
        for role in sorted(groups, key=lambda r: (-len(groups[r]), r)):
            members = groups[role]
            n_orph = sum(1 for p in members if p.pid in orphans)
            dup = f"  ×{len(members)}" if len(members) > 1 else ""
            orph = f"  [{n_orph} orphaned]" if n_orph else ""
            print(f"  {role:<28}{dup}{orph}")
            for p in members:
                tag = " (orphan)" if p.pid in orphans else (" (self/ancestor)" if p.pid in protect else "")
                print(f"       pid {p.pid:<7} ppid {p.ppid:<7}{tag}")
        # port-conflict check on known ecosystem ports
        conflicts = [(port, pids) for port in (8787, 4000, 1235, 8765, 8501)
                     if len(pids := pids_on_port(port)) > 1]
        if conflicts:
            print("\n  PORT CONFLICTS:")
            for port, pids in conflicts:
                print(f"    :{port} has {len(pids)} listeners -> {pids}")
        if orphans:
            print(f"\n  {len(orphans)} orphan(s) — preview `bigboss reap`, then `bigboss reap --yes`.")
        return 0
    if args.command == "reap":
        from . import procman

        procs = procman.list_processes()
        protect = procman.current_protect_pids(procs)
        orphans = procman.find_orphans(procs, protect_pids=protect)
        if args.role:
            orphans = [p for p in orphans if args.role.lower() in procman._role_key(p).lower()]
        if not orphans:
            print("No orphaned server processes to reap.")
            return 0
        results = procman.reap(orphans, dry_run=not args.yes)
        if not args.yes:
            print(f"DRY RUN — would reap {len(results)} orphan(s) (re-run with --yes to terminate):")
            for r in results:
                print(f"  pid {r['pid']:<7} {r['role']} ({r['name']})")
            return 0
        ok = sum(1 for r in results if r["reaped"])
        print(f"Reaped {ok}/{len(results)} orphan(s):")
        for r in results:
            print(f"  pid {r['pid']:<7} {r['role']} — {'terminated' if r['reaped'] else 'FAILED'}")
        return 0
    if args.command == "daemons":
        import json as _json
        from .daemon_registry import status
        st = status()
        if args.json:
            print(_json.dumps(st, indent=2))
            return 0
        print("BigBoss service daemons:")
        for s in st["bigboss_services"]:
            if not s["listening"]:
                mark = "DOWN (not listening)"
            elif s["health_ok"] is False:
                mark = "listening, health FAILED"
            elif s["health_ok"]:
                mark = "UP (health ok)"
            else:
                mark = "listening (no health endpoint)"
            print(f"  {s['name']:<14} {s['endpoint']:<20} {mark}")
            print(f"                 {s['role']}")
        remote = st.get("remote_services") or []
        if remote:
            print("\nWatched remote endpoints (other machines — reachability only):")
            for s in remote:
                mark = "reachable" if s["listening"] else "unreachable"
                print(f"  {s['name']:<14} {s['endpoint']:<22} {mark}")
                print(f"                 {s['role']}")
        reg = st["registered_daemons"]
        if reg:
            print("\nRegistered ecosystem daemons (metadata — probe via the /daemon skill):")
            for d in reg:
                elev = " [elevated restart]" if d["restart_elevated"] else ""
                print(f"  {d['name']:<14} {d.get('project') or '?'}{elev}")
                if d.get("healthy_when"):
                    print(f"                 healthy when: {d['healthy_when'][:100]}")
        print(f"\n  {st['note']}")
        return 0
    if args.command == "daemon":
        from . import daemon_registry
        if args.action == "register":
            written = daemon_registry.register()
            print(f"Registered {len(written)} BigBoss daemon(s) into ~/.claude/daemons.json (foreign entries preserved):")
            for k in written:
                print(f"  {k}")
            return 0
        # restart <name> — human-at-terminal path. Non-elevated runs directly; elevated prints a paste-block.
        if not args.name:
            print("Usage: bigboss daemon restart <name>  (see `bigboss daemons` for names)")
            return 2
        entry = daemon_registry.get_entry(args.name)
        if entry is None or not entry.get("restart_cmd"):
            print(f"No registered daemon {args.name!r} with a restart_cmd. Run `bigboss daemon register` first.")
            return 2
        if entry.get("restart_elevated"):
            proj = entry.get("project") or ""
            paste = f"cd '{proj}'; {entry['restart_cmd']}" if proj else entry["restart_cmd"]
            print(f"'{args.name}' needs an ELEVATED restart — BigBoss runs unelevated. Run this in an elevated PowerShell:\n")
            print(f"  {paste}\n")
            print("Then re-check with: bigboss daemons")
            return 0
        import subprocess
        print(f"Restarting '{args.name}' ...")
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", entry["restart_cmd"]],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            print(f"Restarted '{args.name}' (exit 0). Verify with: bigboss daemons")
            return 0
        print(f"Restart of '{args.name}' exited {proc.returncode}. stderr:\n{(proc.stderr or '')[:800]}")
        return 1
    if args.command == "security":
        import json as _json
        from .security_posture import posture
        p = posture(store)
        if args.json:
            print(_json.dumps(p, indent=2))
            return 0
        print("SECURITY POSTURE (read-only evidence — not a verdict)\n")
        print("LAN-exposed on this box (0.0.0.0) — notable ports:")
        for e in p["local_lan_exposure"] or []:
            print(f"  :{e['port']:<6} {e['label']:<22} {'GATED' if e['gated'] else 'UNGATED'} — {e['detail']}")
        if not p["local_lan_exposure"]:
            print("  (none)")
        other = p.get("other_lan_ports") or {}
        if other.get("count"):
            print(f"  (+ {other['count']} generic Windows/service ports: {', '.join(str(x) for x in other['ports'][:12])}{' …' if other['count'] > 12 else ''})")
        print("\nUnauthenticated compute / MCP endpoints:")
        for e in p["unauthenticated_endpoints"] or []:
            extra = " [CODE-EXEC]" if e.get("code_exec") else ""
            exp = f" | exposure: {e['exposure_channel']}" if e.get("exposure_channel") else ""
            print(f"  {e['name']:<14} {e['endpoint']:<24} auth: {e['auth']}{exp}{extra}")
        eg = p["egress_audit"]
        if eg.get("available"):
            print(f"\nEgress (30d): {eg['sends_30d']} sends; channels: {eg['exposure_channels'] or ['(none tagged)']}")
        print("\nInter-project MCP traffic: UNGOVERNED (no identity/allowlist/per-call audit) — gateway unbuilt.")
        print("\nStanding rulings:")
        for r in p["rulings"]:
            print(f"  - {r}")
        print(f"\n{p['summary']}")
        return 0
    if args.command == "chat":
        from .chat import run_chat
        return run_chat(store, args)
    if args.command == "brainz-status":
        from .brainz import BrainzClient, BrainzError
        try:
            client = BrainzClient()
        except BrainzError as exc:
            print(f"ERROR: {exc}")
            return 2
        h = client.ping()
        print(f"Brainz {client.base_url}: {'reachable' if h['ok'] else 'UNREACHABLE'} "
              f"({h['latency_ms']}ms){'' if h['ok'] else ' — ' + h['detail']}")
        return 0 if h["ok"] else 1
    if args.command == "brainz-recall":
        from .brainz import BrainzClient, BrainzError, format_recall
        try:
            client = BrainzClient()
        except BrainzError as exc:
            print(f"ERROR: {exc}")
            return 2
        h = client.ping()
        if not h["ok"]:
            print(f"Brainz not reachable at {client.base_url}. ({h['detail']})")
            return 1
        try:
            rec = client.recall(args.query, limit=args.limit)
        except BrainzError as exc:
            print(f"ERROR: {exc}")
            return 1
        import hashlib
        store.append_event("brainz.recall", "cli", {
            "query_hash": hashlib.sha256(args.query.encode("utf-8")).hexdigest()[:16],
            "results": len(rec["results"]), "mode": rec["mode"], "destination": "display_local"})
        print(format_recall(rec))
        return 0
    if args.command == "council-eval":
        s = store.council_eval_stats(days=args.days)
        print(f"Council eval (last {s['days']}d) — {s['sessions']} session(s):")
        if not s["sessions"]:
            print("  No council runs yet. Run: bigboss council-ask \"<question>\"")
            return 0
        print(f"  Flagged unsure:     {s['flagged_unsure']} ({s['escalation_rate']*100:.0f}% — target ~10-15%)")
        print(f"  Escalated to Fable: {s['resolved_by_fable']}")
        if s["triggers"]:
            print("  Triggers: " + ", ".join(f"{k}×{v}" for k, v in sorted(s["triggers"].items(), key=lambda x: -x[1])))
        print(f"  Cost: ${s['total_cost_usd']} total | ${s['fable_cost_usd']} on Fable tie-breaks")
        print("  (Escalation precision/recall + council-vs-Fable accuracy delta accrue as you run council-ask + ratify cards.)")
        return 0
    if args.command == "council-leaderboard":
        rows = store.council_model_leaderboard(days=args.days)
        if not rows:
            print("No council runs yet. Run: bigboss council-ask \"<question>\"")
            return 0
        print(f"Council model track record (last {args.days}d) — throne-election signal:")
        for r in rows:
            print(
                f"  {r['model_id']:8} avg {r['avg_score']:5.1f}%  "
                f"over {r['sessions']} session(s), {r['verified']}+{r['partial']}*0.5 / {r['total']} claims"
            )
        return 0
    if args.command == "digest-pending":
        pending = store.pending_digest_projects()
        if not pending:
            print("No projects awaiting digest approval.")
            return 0
        print(f"{len(pending)} project(s) gated from off-box digest (approve to allow):")
        for p in pending:
            print(f"  {p['slug']:<28} {p['canonical_path']}")
        print("Approve: bigboss digest-approve --all   (or per slug)")
        return 0
    if args.command == "digest-approve":
        if not args.slug and not args.all:
            print("Specify slugs or --all. See: bigboss digest-pending")
            return 2
        n = store.clear_digest_pending(None if args.all else args.slug)
        print(f"Cleared digest gate for {n} project(s); they digest on the next cycle.")
        return 0
    if args.command == "registry-list":
        projects = store.list_projects(include_ambiguous=args.all)
        if not projects:
            print("No projects yet. Run: bigboss registry-refresh")
            return 0
        for p in projects:
            pin = "*" if p["pinned"] else " "
            activity = (p["last_activity_at"] or "-")[:10]
            aliases = f" (+{p['alias_count']} aliases)" if p["alias_count"] else ""
            klass = " ".join(t for t in (p.get("lifecycle"), p.get("ownership")) if t)
            tags = (f" [{klass}]" if klass else "") + (" [track-it]" if p.get("track_it") else "") + \
                   (" [no-crawl]" if p["no_crawl"] else "") + (" [excluded]" if p.get("excluded") else "")
            print(
                f"{pin} [{activity}] {p['slug']:<24} {p['kind']:<7} {p['purpose'][:60]}{aliases}{tags}"
            )
        return 0
    if args.command == "registry-no-crawl":
        try:
            project = store.set_no_crawl(args.slug, not args.allow)
        except KeyError as exc:
            print(str(exc))
            return 2
        if project["no_crawl"]:
            print(
                f"{project['slug']}: crawl-excluded on every path "
                f"({project['egress_rows_redacted']} stored egress row(s) redacted, intel snapshot dropped)."
            )
        else:
            print(f"{project['slug']}: crawling re-allowed.")
        return 0
    if args.command == "registry-exclude":
        try:
            project = store.set_excluded(args.slug, not args.include)
        except KeyError as exc:
            print(str(exc))
            return 2
        if project["excluded"]:
            print(f"{project['slug']}: excluded from portfolio prioritization (intel kept).")
        else:
            print(f"{project['slug']}: re-included in portfolio prioritization.")
        return 0
    if args.command == "registry-baseline":
        import json as _json
        from pathlib import Path as _Path

        try:
            records = _json.loads(_Path(args.path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Could not read audit JSON: {exc}")
            return 2
        if isinstance(records, dict):
            records = records.get("projects") or records.get("records") or [records]
        applied, skipped = 0, []
        for rec in records:
            slug = rec.get("slug")
            if not slug:
                continue
            try:
                proj = store.set_baseline(
                    slug,
                    purpose=rec.get("purpose"),
                    lifecycle=rec.get("lifecycle"),
                    ownership=rec.get("ownership"),
                    domain=rec.get("domain"),
                )
            except (KeyError, ValueError) as exc:
                skipped.append(f"{slug}: {exc}")
                continue
            # the operator's own context (portfolio v2): notes / lineage / track-it
            ctx = {k: rec[k] for k in ("notes", "supersedes", "evolved_into", "track_it", "track_note")
                   if k in rec and rec[k] is not None}
            if ctx:
                try:
                    store.set_context(slug, **ctx)
                except (KeyError, ValueError) as exc:
                    skipped.append(f"{slug} (context): {exc}")
            intel = rec.get("intel")
            if intel:
                store.upsert_project_intel(proj["id"], {
                    "status_line": intel.get("status_line", ""),
                    "blockers": intel.get("blockers", []),
                    "roadmap_now": intel.get("roadmap_now", ""),
                    "roadmap_next": intel.get("roadmap_next", ""),
                    "source_hash": intel.get("source_hash", ""),
                    "source_files": intel.get("source_files", []),
                    "model": intel.get("model", "frontier-agent-baseline"),
                })
            applied += 1
        print(f"Baseline applied to {applied} project(s).")
        for s in skipped:
            print(f"  skipped {s}")
        print("Review: bigboss registry-classify   (excluded flags are NOT changed by import)")
        return 0
    if args.command == "registry-classify":
        projects = store.list_projects(include_ambiguous=args.all)
        if not projects:
            print("No projects yet. Run: bigboss registry-refresh")
            return 0
        print(f"{'slug':<24} {'lifecycle':<10} {'ownership':<12} excl track lineage")
        for p in projects:
            lin = []
            if p.get("supersedes"):
                lin.append(f"<-{p['supersedes']}")
            if p.get("evolved_into"):
                lin.append(f"->{p['evolved_into']}")
            print(
                f"{p['slug']:<24} {(p.get('lifecycle') or '-'):<10} {(p.get('ownership') or '-'):<12} "
                f"{'X' if p.get('excluded') else ' '}    {'T' if p.get('track_it') else ' '}     {' '.join(lin)}"
            )
        return 0
    if args.command == "crawl":
        import json as json_mod

        from .crawler import crawl_portfolio, crawl_project, refuse_no_crawl_path

        if args.path:
            try:
                refuse_no_crawl_path(store, args.path)
            except KeyError as exc:
                print(str(exc))
                return 2
            payload = {
                "bundle": crawl_project(
                    args.path, include_text=args.full_text, total_chars=args.total_chars
                )
            }
        else:
            payload = {
                "bundles": crawl_portfolio(
                    store,
                    slugs=args.project,
                    include_text=args.full_text,
                    total_chars=args.total_chars,
                )
            }
        print(json_mod.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "intel-refresh":
        from .intel import refresh_intel
        from .intel.endpoints import get_endpoint

        try:
            ep = get_endpoint(args.endpoint)
        except KeyError as exc:
            print(str(exc))
            return 2
        summary = refresh_intel(store, slugs=args.project, force=args.force, endpoint=ep)
        if not summary["squire_up"]:
            print(f"Endpoint '{ep.name}' ({ep.base_url}) is unreachable; intel left as-is.")
            return 1
        print(
            f"Intel refreshed via {ep.name}: {summary['digested']} digested, "
            f"{summary['skipped_unchanged']} unchanged, {summary['skipped_no_sources']} without docs, "
            f"{summary['skipped_no_crawl']} crawl-excluded, "
            f"{summary['failed']} failed ({summary['projects']} projects considered)."
        )
        return 0
    if args.command == "squire-endpoints":
        from .intel.endpoints import list_endpoints

        for ep in list_endpoints():
            auto = "auto-ok" if ep.auto_ok else "opt-in (never auto)"
            throttle_note = f", min {ep.min_interval_s:g}s/call" if ep.min_interval_s else ""
            exposure_note = f", exposure: {ep.exposure} (egress audited)" if ep.exposure else ""
            print(f"{ep.name:<10} {ep.base_url}  [{auto}{throttle_note}{exposure_note}]")
            print(f"           model={ep.model}  {ep.note}")
        return 0
    if args.command == "intel-list":
        projects = store.list_projects()
        if not projects:
            print("No projects yet. Run: bigboss registry-refresh")
            return 0
        for p in projects:
            intel = p.get("intel")
            pin = "*" if p["pinned"] else " "
            if not intel or not intel.get("generated_at"):
                print(f"{pin} {p['slug']:<24} (no intel yet)")
                continue
            blockers = intel["blockers"]
            print(f"{pin} {p['slug']:<24} {intel['status_line']}")
            if intel["roadmap_now"] or intel["roadmap_next"]:
                print(f"      now: {intel['roadmap_now']}  next: {intel['roadmap_next']}")
            for b in blockers:
                print(f"      ! {b}")
            if intel.get("error"):
                print(f"      (last digest failed: {intel['error']})")
        return 0
    if args.command == "squire-status":
        status = store.squire_status(days=args.days)
        up = {True: "UP", False: "DOWN", None: "UNKNOWN"}[status["up"]]
        print(f"Squire: {up} (last probe: {status['last_health_at'] or 'never'})")
        if status["last_health_error"]:
            print(f"Last health error: {status['last_health_error']}")
        totals = status["totals"]
        print(
            f"Last {status['window_days']}d: {totals['calls']} calls, "
            f"{totals['prompt_tokens']} prompt / {totals['completion_tokens']} completion tokens, "
            f"{totals['errors']} errors."
        )
        for c in status["clients"]:
            print(
                f"  {c['client']:<24} {c['calls']:>5} calls  {c['prompt_tokens'] or 0:>9} in "
                f"{c['completion_tokens'] or 0:>9} out  ~{c['avg_latency_ms'] or 0}ms  {c['errors'] or 0} errors"
            )
        return 0
    if args.command == "squire-egress":
        import json as json_mod

        rows = store.list_egress(days=args.days, endpoint=args.endpoint, limit=args.limit)
        if args.json:
            print(json_mod.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print("No egress recorded in the window. Nothing has been sent to compute endpoints.")
            return 0
        preview = None if args.full else 800

        def _preview(text: str) -> str:
            if not text:
                return "(empty)"
            if preview is None or len(text) <= preview:
                return text
            return text[:preview] + f"\n... [{len(text) - preview} more chars, use --full]"

        for r in rows:
            exposure = f" [exposure: {r['exposure']}]" if r["exposure"] else ""
            status = "ok" if r["ok"] else "FAILED"
            what = r["project_slug"] or r["client"] or "?"
            print(f"{r['created_at']}  {r['endpoint']:<10}{exposure}  {what}  ({status})")
            print("    PROMPT:")
            print(_preview(r.get("prompt_text") or ""))
            print("    RESPONSE:")
            print(_preview(r.get("response_text") or ""))
            print()
        return 0
    if args.command == "squire-proxy":
        return serve_squire_proxy(store, host=args.host, port=args.port, endpoint=args.endpoint)
    if args.command == "session-report":
        import json as json_mod
        import sys as sys_mod

        payload: dict = {}
        if args.json_file:
            try:
                payload = json_mod.loads(Path(args.json_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"Could not read --json-file: {exc}")
                return 2
        elif args.stdin:
            raw = sys_mod.stdin.read()
            try:
                payload = json_mod.loads(raw) if raw.strip() else {}
            except ValueError as exc:
                print(f"Could not parse stdin JSON: {exc}")
                return 2
        if not isinstance(payload, dict):
            print("Payload must be a JSON object.")
            return 2
        # Flags override / supplement the JSON payload.
        for key in ("host", "harness", "vendor", "project", "workspace", "title", "summary", "session_ref"):
            value = getattr(args, key)
            if value is not None:
                payload[key] = value
        if args.files is not None:
            payload["files_touched"] = [f.strip() for f in args.files.split(",") if f.strip()]
        if args.decisions is not None:
            payload["decisions"] = args.decisions
        if args.next_steps is not None:
            payload["next_steps"] = args.next_steps
        # Self-tag the local machine so cross-machine views can tell boxes apart.
        if not payload.get("host"):
            import socket
            payload["host"] = socket.gethostname().lower()
        if not payload.get("harness"):
            print("A harness is required (pass --harness or include it in the JSON payload).")
            return 2
        result = store.record_harness_session(payload)
        if args.json:
            print(json_mod.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"Recorded session {result['session_id']} ({payload['harness']}).")
        return 0
    if args.command == "sessions":
        import json as json_mod

        rows = store.list_harness_sessions(
            days=args.days, project=args.project, harness=args.harness,
            host=args.host, limit=args.limit
        )
        if args.json:
            print(json_mod.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print(f"No harness sessions in the last {args.days}d. Report one: bigboss session-report --harness ...")
            return 0
        for r in rows:
            when = (r["created_at"] or "")[:16].replace("T", " ")
            who = r["harness"] + (f"/{r['vendor']}" if r["vendor"] else "")
            host = f"@{r['host']}" if r.get("host") else ""
            proj = r["project"] or "-"
            summary = (r["summary"] or "")[:70]
            print(f"[{when}] {who:<20}{host:<10} {proj:<20} {r['title']}")
            if summary:
                print(f"    {summary}")
            if r["next_steps"]:
                print(f"    next: {'; '.join(r['next_steps'][:3])}")
        return 0
    if args.command == "commands":
        import json as json_mod

        rows = store.list_remote_commands(host=args.host, limit=args.limit)
        if args.json:
            print(json_mod.dumps(rows, indent=2, sort_keys=True))
            return 0
        if not rows:
            print("No cross-machine commands issued yet.")
            return 0
        for r in rows:
            when = (r["created_at"] or "")[:16].replace("T", " ")
            args_str = f" {r['args']}" if r["args"] else ""
            print(f"[{when}] @{r['host']:<8} {r['status']:<8} {r['action']}{args_str}")
        return 0
    parser.error("Unknown command.")
    return 2


def serve_router(store: Store, port: int, harness: str, hard_cap: str | None) -> int:
    from .router import RouterHTTPServer

    # Loopback-only: the router forwards a live provider credential and must never be
    # LAN-reachable (unlike the phone UI). Host is fixed to 127.0.0.1 by design.
    server = RouterHTTPServer(("127.0.0.1", port), store, harness=harness, hard_cap=hard_cap)
    budget = store.check_budget()
    print("BigBoss router (metering-only v1) is running.")
    print(f"Point programmatic clients at: ANTHROPIC_BASE_URL=http://127.0.0.1:{port}")
    print(
        "Use a DEDICATED prepaid API key for programmatic traffic; keep interactive Claude Code on OAuth, unrouted."
    )
    print(
        f"Period {budget['period']}: spent ${budget['spent_usd']} / cap ${budget['hard_cap_usd']} (cap is ADVISORY until billing is verified)."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BigBoss router.")
    finally:
        server.server_close()
    return 0


def ensure_paths(data_dir: Path) -> dict[str, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir = data_dir / "secrets"
    paths = {
        "db": data_dir / "bigboss.sqlite3",
        "admin": secrets_dir / "admin-token.txt",
        "adapter": secrets_dir / "adapter-token.txt",
        "lan_pin": secrets_dir / "lan-pin.txt",
        "pid": data_dir / "serve.pid",
        "state": data_dir / "serve.state.json",
    }
    read_or_create_secret(paths["admin"], "admin")
    read_or_create_secret(paths["adapter"], "adapter")
    read_or_create_lan_pin(paths["lan_pin"])
    return paths


def desk_url(port: int, *, autoshow: bool = True) -> str:
    suffix = "?autoshow=1" if autoshow else ""
    return f"http://127.0.0.1:{port}/desk{suffix}"


WORKSTATION_DEVICE_NAME = "Workstation"
AUTO_CLAIM_TTL_SECONDS = 300


def dashboard_url(port: int, *, pair_code: str | None = None) -> str:
    # ?pair= is the SPA's existing auto-claim hook (app.js readPairFromUrl).
    suffix = f"?pair={pair_code}" if pair_code else ""
    return f"http://127.0.0.1:{port}/{suffix}"


def host_is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def open_dashboard(paths: dict[str, Path], port: int) -> int:
    """Open the local dashboard, minting a single-use auto-claim code.

    This is the re-pair path after a browser cache clear: the code enrolls the
    browser as the "Workstation" device. Requires a running serve (we mint via
    the loopback admin API rather than writing the DB under a live server).
    """
    import json as _json
    from urllib import error as urlerror
    from urllib import request as urlrequest

    state = read_serve_state(paths["state"])
    target_port = state.port if state else port
    if not wait_for_health(target_port, timeout_seconds=1.5):
        print("ERROR: no running BigBoss server found.")
        print("Start one first: uv run python -m bigboss serve")
        return 1
    admin_token = paths["admin"].read_text(encoding="utf-8").strip()
    req = urlrequest.Request(
        f"http://127.0.0.1:{target_port}/api/admin/pair-codes",
        data=_json.dumps(
            {"device_name": WORKSTATION_DEVICE_NAME, "ttl_seconds": AUTO_CLAIM_TTL_SECONDS}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Admin-Token": admin_token},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=5) as response:
            pair = _json.loads(response.read().decode("utf-8"))
    except (urlerror.URLError, OSError, ValueError) as exc:
        print(f"ERROR: could not mint an auto-claim code: {exc}")
        return 1
    url = dashboard_url(target_port, pair_code=pair["code"])
    open_browser(url)
    print(f"Dashboard: {dashboard_url(target_port)}")
    print("Opened with a single-use auto-claim code (pairs this browser as 'Workstation').")
    return 0


def spawn_serve_detached(paths: dict[str, Path], host: str, port: int) -> None:
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_path
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bigboss",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--no-open-pair",
            "--data-dir",
            str(paths["db"].parent),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        creationflags=creationflags,
        close_fds=(sys.platform != "win32"),
    )


def pair_phone_ui(paths: dict[str, Path], host: str, port: int) -> int:
    state = read_serve_state(paths["state"])
    if state and wait_for_health(state.port, timeout_seconds=1.5):
        open_browser(desk_url(state.port))
        print(f"Pairing desk: {desk_url(state.port)}")
        return 0
    reclaimed = reclaim_previous_serve(pid_path=paths["pid"], state_path=paths["state"], port=port)
    if reclaimed:
        print(f"Reclaimed stale BigBoss listener(s): {', '.join(str(pid) for pid in reclaimed)}")
    spawn_serve_detached(paths, host, port)
    if not wait_for_health(port):
        print("ERROR: BigBoss did not start in time.")
        print("Try: uv run python -m bigboss serve")
        return 1
    url = desk_url(port)
    open_browser(url)
    print(f"Pairing desk: {url}")
    print(f"Phone URL:    http://{local_ip_hint()}:{port}/")
    return 0


def serve_app(
    store: Store,
    paths: dict[str, Path],
    host: str,
    port: int,
    intel_interval_hours: float = 6.0,
    squire_health_interval: float = 300.0,
    open_pair: bool = True,
    tunnel: bool = False,
) -> int:
    from .hosts import get_mdns_hostname
    from .ops import is_port_blocked, add_firewall_rule_uac, IPChangeMonitor, SSHTunnelManager

    local_only = host_is_loopback(host) and not tunnel

    # Reuse an already-healthy instance instead of killing + respawning it. A
    # double-clicked Dashboard icon (or a second `serve`) should open the running
    # server, not nuke the tab the user is using. Only reclaim genuinely dead or
    # wedged listeners below.
    if not tunnel and wait_for_health(port, timeout_seconds=1.5):
        url = desk_url(port)
        if open_pair:
            open_browser(url)
        print(f"BigBoss already running on port {port}.")
        print(f"Dashboard: {url}")
        if not local_only:
            print(f"Phone URL: http://{local_ip_hint()}:{port}/")
        return 0

    reclaimed = reclaim_previous_serve(pid_path=paths["pid"], state_path=paths["state"], port=port)
    if reclaimed:
        print(f"Reclaimed stale BigBoss listener(s): {', '.join(str(pid) for pid in reclaimed)}")

    # 1. Firewall check & auto UAC configuration (moot on a loopback-only bind)
    if not local_only and is_port_blocked(port):
        print(f"[!] BigBoss port {port} is blocked by Windows Defender Firewall.")
        try:
            choice = input(
                "[?] Would you like to automatically create an inbound rule on the Private profile (requires Administrator UAC)? (y/N): "
            )
            if choice.strip().lower() in {"y", "yes"}:
                if add_firewall_rule_uac(port):
                    print("[✓] Firewall rule created successfully.")
                else:
                    print("[✗] Failed to create firewall rule. Please check UAC elevation.")
            else:
                print("Skipping firewall rule creation. You may need to open port 8787 manually.")
        except Exception:
            print("Non-interactive session: skipping firewall configuration.")

    assert_serve_port_available(host, port)
    acquire_pidfile(paths["pid"])
    admin_token = paths["admin"].read_text(encoding="utf-8").strip()
    adapter_token = paths["adapter"].read_text(encoding="utf-8").strip()
    lan_pin = read_or_create_lan_pin(paths["lan_pin"])

    tunnel_mgr = None
    public_host = None

    # 2. Setup Tunnel if requested
    if tunnel:

        def on_tunnel_ready(url: str) -> None:
            nonlocal public_host
            public_host = url.replace("https://", "").replace("http://", "")
            server.public_host = public_host
            try:
                write_serve_state(
                    paths["state"],
                    ServeState(
                        pid=os.getpid(),
                        port=port,
                        host=host,
                        phone_url=url + "/",
                        desk_url=desk_url(port, autoshow=False),
                        started_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as exc:
                print(f"[Network] Failed to update serve state on tunnel ready: {exc}")

        tunnel_mgr = SSHTunnelManager(local_port=port, on_url_ready=on_tunnel_ready)
        tunnel_mgr.start()

    server = ApprovalHTTPServer(
        (host, port),
        store,
        token_hash(admin_token),
        token_hash(adapter_token),
        token_hash(lan_pin),
        public_host=public_host,
    )

    # 3. Setup IP Change Monitor
    def on_ip_change(old_ip: str, new_ip: str) -> None:
        print(f"\n[Network] LAN IP changed: {old_ip} -> {new_ip}")
        if not tunnel:
            new_phone_url = f"http://{new_ip}:{port}/"
            new_desk_url = desk_url(port, autoshow=False)
            try:
                write_serve_state(
                    paths["state"],
                    ServeState(
                        pid=os.getpid(),
                        port=port,
                        host=host,
                        phone_url=new_phone_url,
                        desk_url=new_desk_url,
                        started_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as exc:
                print(f"[Network] Failed to update serve state file on IP change: {exc}")

            try:
                store.append_event(
                    event_type="network.ip_changed",
                    aggregate_id="server",
                    payload={
                        "old_ip": old_ip,
                        "new_ip": new_ip,
                        "phone_url": new_phone_url,
                    },
                )
            except Exception as exc:
                print(f"[Network] Failed to record ip_changed event: {exc}")

    ip_monitor = None
    if not local_only:
        ip_monitor = IPChangeMonitor(on_ip_change, interval_seconds=15.0)
        ip_monitor.start()

    scheduler = None
    if intel_interval_hours > 0 or squire_health_interval > 0:
        from .intel.scheduler import IntelScheduler

        scheduler = IntelScheduler(
            store,
            digest_interval_s=intel_interval_hours * 3600,
            health_interval_s=squire_health_interval,
        )
        scheduler.start()

    if local_only:
        phone_url = dashboard_url(port)
    else:
        mdns_hostname = get_mdns_hostname()
        phone_url = f"http://{mdns_hostname}:{port}/"
    desk = desk_url(port, autoshow=open_pair)

    write_serve_state(
        paths["state"],
        ServeState(
            pid=os.getpid(),
            port=port,
            host=host,
            phone_url=phone_url,
            desk_url=desk_url(port, autoshow=False),
            started_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    print("BigBoss approval plane is running.")
    if local_only:
        print(f"Dashboard:   {dashboard_url(port)}  (local only — use --lan for phone access)")
        print(f"Re-open:     uv run python -m bigboss open")
    else:
        print(
            f"Pair phone:  {desk}  (QR desk — opening browser)"
            if open_pair
            else f"Pair phone:  {desk}"
        )
        print(f"LAN PIN:     {lan_pin}  (enter on phone to re-pair)")
        print(f"Phone URL:   http://{local_ip_hint()}:{port}/  (raw IP)")
        print(f"Phone URL:   {phone_url}  (mDNS hostname)")
        print(f"Quick pair:  uv run python -m bigboss pair")
    if scheduler is not None:
        print(
            f"Intel scheduler: digest every {intel_interval_hours}h, "
            f"Squire health every {squire_health_interval:.0f}s."
        )
    if open_pair:
        if local_only:
            # Auto-claim: mint a single-use code (in-process; server thread not
            # yet serving) and open the dashboard, which pairs itself silently
            # as "Workstation". Deliberately NOT loopback auto-trust: governed
            # harnesses run on this box and must not be able to self-approve.
            pair = store.create_pair_code(
                WORKSTATION_DEVICE_NAME, ttl_seconds=AUTO_CLAIM_TTL_SECONDS
            )
            open_browser(dashboard_url(port, pair_code=pair["code"]))
        else:
            open_browser(desk)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BigBoss approval plane.")
    finally:
        if scheduler is not None:
            scheduler.stop()
        if ip_monitor is not None:
            ip_monitor.stop()
        if tunnel_mgr is not None:
            tunnel_mgr.stop()
        server.server_close()
        release_pidfile(paths["pid"])
        clear_serve_state(paths["state"])
    return 0


def serve_squire_proxy(store: Store, host: str, port: int, endpoint: str = "squire") -> int:
    from .intel.endpoints import get_endpoint
    from .intel.proxy import SquireProxyServer

    ep = get_endpoint(endpoint)
    server = SquireProxyServer((host, port), store, upstream=ep.base_url)
    print(f"BigBoss metering proxy is running (fronting endpoint: {ep.name}).")
    print(
        f"Point clients at: SQUIRE_BASE_URL=http://{host if host != '0.0.0.0' else local_ip_hint()}:{port}/v1"
    )
    print(
        "Set each client's SQUIRE_API_KEY to its project name; it becomes the ledger's client label."
    )
    print(f"Upstream: {server.upstream}")
    if ep.exposure:
        print(
            f"Known exposure on this box: {ep.exposure}. Every request is written to the egress audit."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Squire metering proxy.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
