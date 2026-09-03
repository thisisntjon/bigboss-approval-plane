from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .adapters import (
    codex_permission_response,
    codex_post_tool_response,
    decision_response,
    normalize_permission_payload,
    normalize_update_payload,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bigboss-hook", description="Bridge harness hook payloads into BigBoss.")
    parser.add_argument("--harness", required=True, help="Harness identity, e.g. claude-code or codex.")
    parser.add_argument("--url", default=os.environ.get("BIGBOSS_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--adapter-token", default=os.environ.get("BIGBOSS_ADAPTER_TOKEN"))
    parser.add_argument(
        "--adapter-token-file",
        default=os.environ.get("BIGBOSS_ADAPTER_TOKEN_FILE", ".bigboss/secrets/adapter-token.txt"),
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--mode", choices=["approval", "update", "auto"], default="approval")
    args = parser.parse_args(argv)

    raw: dict[str, Any] = {}
    try:
        raw = json.load(sys.stdin)
        token = args.adapter_token or read_token(args.adapter_token_file)
        mode = resolve_mode(args.mode, raw)
        if mode == "update":
            update_payload = normalize_update_payload(raw, harness=args.harness)
            update = post_update(args.url, token, update_payload)
            if is_codex_hook(args.harness, raw, "PostToolUse"):
                print(json.dumps(codex_post_tool_response(update), sort_keys=True))
            return 0

        approval_payload = normalize_permission_payload(raw, harness=args.harness)
        approval = post_approval(args.url, token, approval_payload, wait=not args.no_wait, timeout=args.timeout)
        if is_codex_hook(args.harness, raw, "PermissionRequest"):
            print(json.dumps(codex_permission_response(approval), sort_keys=True))
            return 0
        response = decision_response(approval)
        print(json.dumps(response, sort_keys=True))
        return 0 if response["allow"] else 2
    except Exception as exc:
        message = f"BigBoss hook failed closed: {exc}"
        if resolve_mode(args.mode, raw) == "update":
            print(json.dumps({"systemMessage": message}, sort_keys=True))
            return 0
        if is_codex_hook(args.harness, raw, "PermissionRequest"):
            print(json.dumps(codex_permission_response({"status": "rejected", "latest_decision": {"note": message}})))
            return 0
        print(json.dumps({"allow": False, "decision": "deny", "permissionDecision": "deny", "reason": message}, sort_keys=True))
        return 2


def read_token(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def post_approval(url: str, token: str, payload: dict[str, Any], *, wait: bool, timeout: int) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/api/harness/approval-requests"
    if wait:
        endpoint += f"?wait=true&timeout={timeout}"
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "X-Adapter-Token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout + 5) as response:
            return json.loads(response.read().decode("utf-8"))["approval"]
    except HTTPError as exc:
        raise RuntimeError(f"BigBoss returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"BigBoss is unreachable: {exc.reason}") from exc


def post_update(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/api/harness/updates"
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "X-Adapter-Token": token},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))["update"]
    except HTTPError as exc:
        raise RuntimeError(f"BigBoss returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"BigBoss is unreachable: {exc.reason}") from exc


def resolve_mode(mode: str, raw: dict[str, Any]) -> str:
    if mode != "auto":
        return mode
    if raw.get("hook_event_name") == "PostToolUse":
        return "update"
    return "approval"


def is_codex_hook(harness: str, raw: dict[str, Any], event_name: str) -> bool:
    return harness.lower() == "codex" or raw.get("hook_event_name") == event_name


if __name__ == "__main__":
    raise SystemExit(main())
