"""BigBoss <-> Codex app-server bridge.

`codex exec` cannot perform command-execution approval ("command execution approval is
not supported in exec mode"), and its hooks do not fire there. The Codex **app-server**
(`codex app-server`, experimental) instead exposes a JSON-RPC protocol where the server
sends the client approval requests and the client returns a decision - fully
non-interactive and programmatic.

This module drives `codex app-server` as a client and routes each approval request to a
decision function. In production that function creates a BigBoss approval card (so the
paired phone decides) and maps the phone decision back to a Codex decision.

Transport: newline-delimited JSON-RPC over the subprocess stdio.

Server -> client approval requests handled:
  - item/commandExecution/requestApproval
  - item/fileChange/requestApproval
  - item/permissions/requestApproval
Decision response shape: {"decision": "accept"|"acceptForSession"|"decline"|"cancel"}
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from typing import Any, BinaryIO, Callable

from .store import Store


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}

# BigBoss phone decision -> Codex app-server decision.
DECISION_MAP = {
    "approve_once": "accept",
    "approve_for_run": "acceptForSession",
    "reject": "decline",
    "request_changes": "decline",
}
# Fail-safe used for timeouts / unknown / still-pending decisions.
DEFAULT_DECISION = "decline"


def resolve_codex_bin() -> str:
    """Locate a directly-spawnable codex executable.

    The npm entry point on Windows is a `.ps1`/`.cmd` shim that `subprocess` cannot exec
    directly, so prefer the vendored platform binary.
    """
    override = os.environ.get("BIGBOSS_CODEX_BIN")
    if override:
        return override
    patterns = [
        os.path.expanduser(
            r"~\AppData\Roaming\npm\node_modules\@openai\codex\node_modules"
            r"\@openai\codex-*\vendor\**\bin\codex.exe"
        ),
        os.path.expanduser(
            r"~\AppData\Roaming\npm\node_modules\@openai\codex\**\vendor\**\bin\codex.exe"
        ),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    raise FileNotFoundError(
        "Could not locate the codex executable. Set BIGBOSS_CODEX_BIN to the codex binary."
    )


class CodexAppBridge:
    """Drives one Codex app-server turn over injected byte streams.

    `stdin`/`stdout` are the app-server's stdin (we write) and stdout (we read). Streams
    are injectable so the protocol logic is testable without spawning a subprocess.
    `decision_fn` receives a normalized approval dict and returns a Codex decision string
    (or a BigBoss decision name, which is mapped).
    """

    def __init__(
        self,
        server_stdin: BinaryIO,
        server_stdout: BinaryIO,
        decision_fn: Callable[[dict[str, Any]], str],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.stdin = server_stdin
        self.stdout = server_stdout
        self.decision_fn = decision_fn
        self.on_event = on_event
        self._next_id = 0
        self.approvals: list[dict[str, Any]] = []
        self.final_message: str | None = None
        self.turn_status: str | None = None

    def _write(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=True).encode("utf-8")
        self.stdin.write(payload + b"\n")
        self.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> int:
        message_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        return message_id

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _read(self) -> dict[str, Any] | None:
        while True:
            line = self.stdout.readline()
            if line == b"":
                return None
            line = line.strip()
            if not line:
                continue
            return json.loads(line.decode("utf-8"))

    def run(self, prompt: str, cwd: str, model: str | None = None, timeout_turns: int = 1) -> dict[str, Any]:
        """Initialize, start a thread + turn, and process messages until the turn ends."""
        init_id = self._request(
            "initialize",
            {"clientInfo": {"name": "bigboss", "title": "BigBoss", "version": "0.1.0"}},
        )
        thread_id: str | None = None
        turn_started = False

        while True:
            message = self._read()
            if message is None:
                break
            self._dispatch_event(message)

            msg_id = message.get("id")
            method = message.get("method")

            # Responses to our own requests.
            if method is None and msg_id is not None:
                result = message.get("result") or {}
                if msg_id == init_id:
                    self._notify("initialized")
                    thread_params: dict[str, Any] = {"cwd": cwd}
                    if model:
                        thread_params["model"] = model
                    self._thread_id_req = self._request("thread/start", thread_params)
                elif getattr(self, "_thread_id_req", None) == msg_id:
                    thread = result.get("thread") or {}
                    thread_id = thread.get("id") or result.get("threadId")
                    self._request(
                        "turn/start",
                        {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
                    )
                    turn_started = True
                continue

            # Server-initiated requests (approvals, etc.).
            if method is not None and msg_id is not None:
                self._handle_server_request(msg_id, method, message.get("params") or {}, cwd, thread_id)
                continue

            # Notifications.
            if method is not None and msg_id is None:
                if turn_started and method in ("turn/completed", "turn/failed", "turn/aborted"):
                    self.turn_status = method.split("/", 1)[1]
                    self._capture_final_message(message)
                    break
                self._maybe_capture_agent_message(message)

        return {
            "thread_id": thread_id,
            "turn_status": self.turn_status,
            "final_message": self.final_message,
            "approvals": self.approvals,
        }

    def _handle_server_request(
        self, msg_id: Any, method: str, params: dict[str, Any], cwd: str, thread_id: str | None
    ) -> None:
        if method in APPROVAL_METHODS:
            normalized = self._normalize_approval(method, params, cwd, thread_id)
            raw = self.decision_fn(normalized)
            decision = DECISION_MAP.get(raw, raw) if raw in DECISION_MAP else raw
            if decision not in ("accept", "acceptForSession", "decline", "cancel"):
                decision = DEFAULT_DECISION
            self.approvals.append({**normalized, "decision": decision})
            self._write({"jsonrpc": "2.0", "id": msg_id, "result": {"decision": decision}})
            return
        # Unhandled server request: respond with an error so the turn can proceed/fail cleanly.
        self._write(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unsupported server request: {method}"},
            }
        )

    def _normalize_approval(
        self, method: str, params: dict[str, Any], cwd: str, thread_id: str | None
    ) -> dict[str, Any]:
        command = params.get("command")
        return {
            "method": method,
            "command": command,
            "cwd": params.get("cwd") or cwd,
            "thread_id": params.get("threadId") or thread_id,
            "turn_id": params.get("turnId"),
            "item_id": params.get("itemId"),
            "file_changes": params.get("fileChanges") or params.get("changes"),
            "raw_params": params,
        }

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        if self.on_event:
            self.on_event(message)

    def _maybe_capture_agent_message(self, message: dict[str, Any]) -> None:
        if message.get("method") != "item/completed":
            return
        item = (message.get("params") or {}).get("item") or {}
        if item.get("type") == "agentMessage" and item.get("text"):
            self.final_message = item["text"]

    def _capture_final_message(self, message: dict[str, Any]) -> None:
        turn = (message.get("params") or {}).get("turn") or {}
        for item in turn.get("items") or []:
            if item.get("type") == "agentMessage" and item.get("text"):
                self.final_message = item["text"]


def _store_decision_fn(store: Store, timeout_seconds: int) -> Callable[[dict[str, Any]], str]:
    """Build a decision function that creates a BigBoss card and waits for the phone."""

    def decide(approval: dict[str, Any]) -> str:
        command = approval.get("command")
        if command:
            title = f"Codex: {str(command)[:60]}"
            summary = f"Codex wants to run:\n{command}"
            proposed_action = {"kind": "shell_command", "command": command, "cwd": approval.get("cwd")}
        else:
            title = "Codex: file change"
            summary = "Codex wants to apply file changes."
            proposed_action = {"kind": "file_change", "changes": approval.get("file_changes")}

        payload = {
            "harness": "codex",
            "run_id": approval.get("thread_id") or "codex_app_server",
            "run_title": "Codex app-server",
            "workspace": approval.get("cwd"),
            "title": title,
            "summary": summary,
            "proposed_action": proposed_action,
        }
        created = store.create_approval_request(payload)
        if created["status"] != "pending":
            # Policy auto-allowed or blocked without human input.
            return "accept" if created["status"] == "auto_allowed" else "decline"
        resolved = store.wait_for_resolution(created["id"], timeout_seconds)
        decision = (resolved.get("latest_decision") or {}).get("decision")
        return DECISION_MAP.get(decision, DEFAULT_DECISION)

    return decide


def run_codex_turn(
    prompt: str,
    store: Store,
    cwd: str | None = None,
    model: str | None = None,
    approval_policy: str = "untrusted",
    sandbox_mode: str = "workspace-write",
    timeout_seconds: int = 900,
    codex_bin: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Spawn `codex app-server`, run one turn, route approvals through BigBoss."""
    cwd = cwd or os.getcwd()
    codex_bin = codex_bin or resolve_codex_bin()
    args = [
        codex_bin,
        "app-server",
        "-c",
        f'approval_policy="{approval_policy}"',
        "-c",
        f'sandbox_mode="{sandbox_mode}"',
    ]
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
    )
    try:
        bridge = CodexAppBridge(
            proc.stdin, proc.stdout, _store_decision_fn(store, timeout_seconds), on_event=on_event
        )
        result = bridge.run(prompt, cwd=cwd, model=model)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return result


def main(argv: list[str], store: Store) -> int:
    """CLI entry used by `bigboss codex-run`. argv is the already-parsed arg namespace-free list."""
    # argv: [prompt, cwd, model, timeout, sandbox, approval]
    prompt, cwd, model, timeout, sandbox, approval = argv
    result = run_codex_turn(
        prompt,
        store=store,
        cwd=cwd,
        model=model or None,
        approval_policy=approval,
        sandbox_mode=sandbox,
        timeout_seconds=int(timeout),
        on_event=None,
    )
    print(f"Turn status: {result['turn_status']}")
    print(f"Approvals handled: {len(result['approvals'])}")
    for a in result["approvals"]:
        print(f"  - {a.get('command') or a.get('method')} -> {a['decision']}")
    if result.get("final_message"):
        print("\nCodex final message:\n" + result["final_message"])
    return 0 if result.get("turn_status") == "completed" else 1
