from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_permission_payload(
    raw: dict[str, Any],
    *,
    harness: str,
    workspace: str | None = None,
) -> dict[str, Any]:
    tool_name = _first_string(raw, "tool_name", "tool", "name", "toolName") or "unknown"
    tool_input = _first_dict(raw, "tool_input", "input", "arguments", "toolInput") or {}
    cwd = _first_string(raw, "cwd", "current_working_directory", "workspace") or workspace or str(Path.cwd())
    command = _first_string(tool_input, "command", "cmd")
    path = _first_string(tool_input, "path", "file_path", "target")

    if command:
        proposed_action = {"kind": "shell_command", "command": command, "cwd": cwd}
        title = f"Approve {harness} command"
        summary = f"{harness} wants to run `{command}`."
    elif path and tool_name.lower() in {"write", "edit", "multiedit", "notebookedit"}:
        proposed_action = {"kind": "file_write", "path": path, "tool": tool_name}
        title = f"Approve {harness} file write"
        summary = f"{harness} wants to modify `{path}`."
    elif path and tool_name.lower() in {"read", "glob", "grep", "ls"}:
        proposed_action = {"kind": "file_read", "path": path, "tool": tool_name}
        title = f"{harness} read request"
        summary = f"{harness} wants to read `{path}`."
    else:
        proposed_action = {"kind": "harness_tool", "tool": tool_name, "input": tool_input}
        title = f"Approve {harness} tool use"
        summary = f"{harness} wants to use `{tool_name}`."

    return {
        "run_id": _first_string(raw, "run_id", "session_id", "sessionId") or f"run_{harness}",
        "run_title": _first_string(raw, "run_title", "session_title") or f"{harness} session",
        "harness": harness,
        "workspace": cwd,
        "title": title,
        "summary": summary,
        "proposed_action": proposed_action,
        "diff_summary": {},
    }


def decision_response(approval: dict[str, Any]) -> dict[str, Any]:
    status = approval.get("status")
    latest = approval.get("latest_decision") or {}
    decision = latest.get("decision")
    note = latest.get("note") or f"BigBoss approval status: {status}"

    allow = status in {"approved", "auto_allowed"} and decision != "reject"
    permission_decision = "allow" if allow else "deny"
    return {
        "allow": allow,
        "decision": "approve" if allow else "deny",
        "permissionDecision": permission_decision,
        "reason": note,
        "approval_id": approval.get("id"),
        "action_hash": approval.get("action_hash"),
        "approval": approval,
    }


def codex_permission_response(approval: dict[str, Any]) -> dict[str, Any]:
    generic = decision_response(approval)
    behavior = "allow" if generic["allow"] else "deny"
    hook_decision: dict[str, Any] = {"behavior": behavior}
    reason = str(generic["reason"] or "")
    if behavior == "deny":
        hook_decision["message"] = reason

    response = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": hook_decision,
        }
    }
    if reason:
        response["systemMessage"] = reason
    return response


def normalize_update_payload(
    raw: dict[str, Any],
    *,
    harness: str,
    workspace: str | None = None,
) -> dict[str, Any]:
    tool_name = _first_string(raw, "tool_name", "tool", "name", "toolName") or "unknown"
    tool_input = _first_dict(raw, "tool_input", "input", "arguments", "toolInput") or {}
    tool_response = _first_dict(raw, "tool_response", "response", "toolResponse") or {}
    cwd = _first_string(raw, "cwd", "current_working_directory", "workspace") or workspace or str(Path.cwd())
    command = _first_string(tool_input, "command", "cmd")
    status = _tool_status(tool_response)

    if command:
        title = f"{harness} {tool_name} completed"
        summary = f"{harness} finished `{command}`."
    else:
        title = f"{harness} {tool_name} completed"
        summary = f"{harness} finished `{tool_name}`."
    if status:
        summary = f"{summary} Status: {status}."

    return {
        "run_id": _first_string(raw, "run_id", "session_id", "sessionId") or f"run_{harness}",
        "harness": harness,
        "workspace": cwd,
        "title": title,
        "summary": summary,
        "severity": "warning" if _looks_failed(tool_response) else "info",
        "details": {
            "hook_event_name": raw.get("hook_event_name"),
            "turn_id": raw.get("turn_id"),
            "tool_name": tool_name,
            "tool_input": _truncate_json_value(tool_input),
            "tool_response": _truncate_json_value(tool_response),
        },
    }


def codex_post_tool_response(update: dict[str, Any]) -> dict[str, Any]:
    return {"systemMessage": f"BigBoss recorded update: {update['title']}"}


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_dict(source: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return None


def _tool_status(tool_response: dict[str, Any]) -> str | None:
    for key in ("status", "exit_code", "returncode", "return_code", "code"):
        value = tool_response.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _looks_failed(tool_response: dict[str, Any]) -> bool:
    for key in ("exit_code", "returncode", "return_code", "code"):
        value = tool_response.get(key)
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str) and value.isdigit():
            return int(value) != 0
    status = str(tool_response.get("status") or "").lower()
    return status in {"failed", "failure", "error"}


def _truncate_json_value(value: Any, max_chars: int = 4000) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        encoded = json.dumps(str(value))
    if len(encoded) <= max_chars:
        return value
    return {"truncated_json": encoded[:max_chars], "truncated": True}
