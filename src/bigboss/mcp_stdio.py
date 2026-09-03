from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

from .adapters import decision_response, normalize_permission_payload
from .crawler import crawl_portfolio, crawl_project, refuse_no_crawl_path, resolve_project_path
from .intel import refresh_intel
from .registry import refresh as registry_refresh
from .store import Store


PROTOCOL_VERSION = "2025-06-18"


class MCPStdioServer:
    def __init__(self, store: Store, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None):
        self.store = store
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer

    def run(self) -> int:
        while True:
            message = self.read_message()
            if message is None:
                return 0
            response = self.handle_message(message)
            if response is not None:
                self.write_message(response)

    def read_message(self) -> dict[str, Any] | None:
        # MCP stdio transport: messages are newline-delimited JSON-RPC, one per line.
        while True:
            line = self.stdin.readline()
            if line == b"":
                return None
            line = line.strip()
            if not line:
                continue
            return json.loads(line.decode("utf-8"))

    def write_message(self, message: dict[str, Any]) -> None:
        # Compact, single-line JSON (no embedded literal newlines) terminated by "\n".
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.stdout.write(payload + b"\n")
        self.stdout.flush()

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        message_id = message.get("id")
        method = message.get("method")
        if message_id is None:
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bigboss", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(
                    str(params.get("name")), dict(params.get("arguments") or {})
                )
            else:
                return self.error(message_id, -32601, f"Unsupported method: {method}")
            return {"jsonrpc": "2.0", "id": message_id, "result": result}
        except Exception as exc:  # MCP clients should receive a JSON-RPC error, not a broken pipe.
            print(f"BigBoss MCP error: {exc}", file=sys.stderr)
            return self.error(message_id, -32000, str(exc))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "bigboss_request_approval":
            wait = bool(arguments.pop("wait", False))
            timeout = int(arguments.pop("timeout_seconds", 30))
            approval = self.store.create_approval_request(arguments)
            if wait and approval["status"] == "pending":
                approval = self.store.wait_for_resolution(approval["id"], timeout)
            return tool_result({"approval": approval})

        if name == "bigboss_wait_for_decision":
            approval_id = str(arguments["approval_id"])
            timeout = int(arguments.get("timeout_seconds") or 30)
            approval = self.store.wait_for_resolution(approval_id, timeout)
            return tool_result({"approval": approval})

        if name == "bigboss_get_approval":
            approval = self.store.get_approval(str(arguments["approval_id"]))
            if approval is None:
                raise KeyError("Approval not found.")
            return tool_result({"approval": approval})

        if name == "bigboss_list_pending_approvals":
            status = str(arguments.get("status") or "pending")
            return tool_result({"approvals": self.store.list_approvals(status=status)})

        if name == "bigboss_submit_update":
            update = self.store.submit_update(arguments)
            return tool_result({"update": update})

        if name == "bigboss_session_report":
            session = self.store.record_harness_session(arguments)
            return tool_result({"session": session})

        if name == "bigboss_permission_prompt":
            wait = bool(arguments.pop("wait", True))
            timeout = int(arguments.pop("timeout_seconds", 600))
            harness = str(arguments.pop("harness", "claude-code"))
            approval_payload = normalize_permission_payload(arguments, harness=harness)
            approval = self.store.create_approval_request(approval_payload)
            if wait and approval["status"] == "pending":
                approval = self.store.wait_for_resolution(approval["id"], timeout)
            return tool_result(decision_response(approval))

        if name == "registry_list":
            include_ambiguous = bool(arguments.get("include_ambiguous", False))
            return tool_result(
                {"projects": self.store.list_projects(include_ambiguous=include_ambiguous)}
            )

        if name == "registry_refresh":
            summary = registry_refresh(self.store)
            return tool_result({"summary": summary})

        if name == "portfolio_intel":
            return tool_result(
                {
                    "projects": self.store.list_projects(
                        include_ambiguous=bool(arguments.get("include_ambiguous", False))
                    ),
                    "squire": self.store.squire_status(),
                }
            )

        if name == "intel_refresh":
            slugs = arguments.get("slugs")
            summary = refresh_intel(
                self.store,
                slugs=[str(s) for s in slugs] if slugs else None,
                force=bool(arguments.get("force", False)),
                endpoint=str(arguments.get("endpoint") or "squire"),
            )
            return tool_result({"summary": summary})

        if name == "squire_status":
            days = int(arguments.get("days") or 7)
            return tool_result({"squire": self.store.squire_status(days=days)})

        if name == "squire_egress":
            return tool_result(
                {
                    "egress": self.store.list_egress(
                        days=int(arguments.get("days") or 7),
                        endpoint=str(arguments["endpoint"]) if arguments.get("endpoint") else None,
                        limit=int(arguments.get("limit") or 100),
                    )
                }
            )

        if name == "crawl_project":
            path = arguments.get("path")
            if not path:
                slug = str(arguments.get("slug") or "")
                if not slug:
                    raise ValueError("crawl_project needs a slug or a path.")
                path = resolve_project_path(self.store, slug)
            else:
                refuse_no_crawl_path(self.store, str(path))
            bundle = crawl_project(
                str(path),
                include_text=bool(arguments.get("include_text", True)),
                total_chars=int(arguments.get("total_chars") or 22_000),
            )
            return tool_result({"bundle": bundle})

        if name == "crawl_portfolio":
            slugs = arguments.get("slugs")
            bundles = crawl_portfolio(
                self.store,
                slugs=[str(s) for s in slugs] if slugs else None,
                include_text=bool(arguments.get("include_text", False)),
                total_chars=int(arguments.get("total_chars") or 22_000),
            )
            return tool_result({"bundles": bundles})

        raise KeyError(f"Unknown tool: {name}")

    def error(self, message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}],
        "structuredContent": payload,
    }


def tool_definitions() -> list[dict[str, Any]]:
    common_request_schema = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "run_title": {"type": "string"},
            "harness": {"type": "string"},
            "workspace": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "proposed_action": {"type": "object"},
            "diff_summary": {"type": "object"},
            "wait": {"type": "boolean", "default": False},
            "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 3600, "default": 30},
        },
        "required": ["harness", "workspace", "title", "summary", "proposed_action"],
    }
    return [
        {
            "name": "bigboss_request_approval",
            "description": "Create a BigBoss approval card. Optionally wait for the paired phone decision.",
            "inputSchema": common_request_schema,
        },
        {
            "name": "bigboss_wait_for_decision",
            "description": "Wait for a BigBoss approval decision by approval ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3600,
                        "default": 30,
                    },
                },
                "required": ["approval_id"],
            },
        },
        {
            "name": "bigboss_get_approval",
            "description": "Read one BigBoss approval by ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"approval_id": {"type": "string"}},
                "required": ["approval_id"],
            },
        },
        {
            "name": "bigboss_list_pending_approvals",
            "description": "List BigBoss approvals. Defaults to pending approvals.",
            "inputSchema": {
                "type": "object",
                "properties": {"status": {"type": "string", "default": "pending"}},
            },
        },
        {
            "name": "bigboss_submit_update",
            "description": "Submit a non-approval harness status update to BigBoss.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "harness": {"type": "string"},
                    "workspace": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "severity": {"type": "string"},
                    "details": {"type": "object"},
                },
                "required": ["harness", "workspace", "title"],
            },
        },
        {
            "name": "bigboss_session_report",
            "description": (
                "Record an end-of-session harness report into BigBoss's cross-vendor fleet log "
                "(what this session accomplished). Use at wrap-up so Big Boss can see what the whole fleet did."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "harness": {"type": "string", "description": "claude-code, codex, gemini-cli, grok, cursor..."},
                    "vendor": {"type": "string", "description": "anthropic, openai, google, xai..."},
                    "project": {"type": "string"},
                    "workspace": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "files_touched": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "next_steps": {"type": "array", "items": {"type": "string"}},
                    "session_ref": {"type": "string", "description": "External session id (idempotency key)."},
                },
                "required": ["harness"],
            },
        },
        {
            "name": "registry_list",
            "description": "List the auto-derived project portfolio (Portfolio View): pinned first, then by most recent activity.",
            "inputSchema": {
                "type": "object",
                "properties": {"include_ambiguous": {"type": "boolean", "default": False}},
            },
        },
        {
            "name": "registry_refresh",
            "description": "Re-harvest local signals (fs markers, git, transcripts, handoffs, daemons) and reconcile the project registry.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "portfolio_intel",
            "description": (
                "The BigBoss portfolio brain payload: every registered project with its "
                "Squire-digested intel (status, blockers, roadmap now/next) plus Squire health "
                "and per-client usage. Read this instead of crawling project docs yourself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"include_ambiguous": {"type": "boolean", "default": False}},
            },
        },
        {
            "name": "intel_refresh",
            "description": (
                "Crawl registered projects' planning docs and re-digest changed ones through a compute "
                "endpoint (default: Squire, the free always-on box). Unchanged docs are skipped by content "
                "hash. Set endpoint='beastmode' to use the guarded RTX 5090 box (rate-limited, opt-in)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slugs": {"type": "array", "items": {"type": "string"}},
                    "force": {"type": "boolean", "default": False},
                    "endpoint": {"type": "string", "default": "squire"},
                },
            },
        },
        {
            "name": "squire_status",
            "description": "Squire (the remote host LM Studio) health + per-client usage rollup from the BigBoss ledger.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}
                },
            },
        },
        {
            "name": "squire_egress",
            "description": (
                "Data-egress audit: what data BigBoss sent to each compute endpoint (project, source "
                "files, content hash, sizes), tagged with the box's known exposure channel (e.g. "
                "managed-edr on the guarded endpoint). Accountability record - there is no key gating."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
                    "endpoint": {
                        "type": "string",
                        "description": "Filter to one endpoint (e.g. beastmode).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
            },
        },
        {
            "name": "crawl_project",
            "description": (
                "General-purpose project doc crawler: read one project's planning docs "
                "(PLAN/ROADMAP/TODO/CLAUDE/AGENTS/README) into a budgeted bundle with a content hash. "
                "Give a registry slug or an absolute path. The raw-material tool for Squire or any model."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Registry project slug."},
                    "path": {
                        "type": "string",
                        "description": "Absolute project path (overrides slug).",
                    },
                    "include_text": {"type": "boolean", "default": True},
                    "total_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 200000,
                        "default": 22000,
                    },
                },
            },
        },
        {
            "name": "crawl_portfolio",
            "description": (
                "Crawl every registered project (or the given slugs) into doc bundles with content "
                "hashes and registry metadata. Text is omitted by default; set include_text for payloads."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slugs": {"type": "array", "items": {"type": "string"}},
                    "include_text": {"type": "boolean", "default": False},
                    "total_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 200000,
                        "default": 22000,
                    },
                },
            },
        },
        {
            "name": "bigboss_permission_prompt",
            "description": "Bridge a harness permission-prompt payload into BigBoss and return a conservative allow/deny response.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "harness": {"type": "string", "default": "claude-code"},
                    "tool_name": {"type": "string"},
                    "tool_input": {"type": "object"},
                    "cwd": {"type": "string"},
                    "session_id": {"type": "string"},
                    "wait": {"type": "boolean", "default": True},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3600,
                        "default": 600,
                    },
                },
            },
        },
    ]
