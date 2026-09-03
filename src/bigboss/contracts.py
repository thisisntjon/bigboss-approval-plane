from __future__ import annotations

import hashlib
import json
from typing import Any


POLICY_VERSION = "2026-06-24.1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def action_hash(proposed_action: dict[str, Any], workspace: str) -> str:
    payload = {
        "policy_version": POLICY_VERSION,
        "workspace": workspace,
        "proposed_action": proposed_action,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

