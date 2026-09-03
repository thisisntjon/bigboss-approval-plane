"""Tolerant JSON extraction — faithful port of the-council-capstone's `parseJSON`
(shadow-council). Models wrap JSON in ```json fences or prose; strip the fence and parse,
else regex-extract the first {...} and parse, else return None. Used at every stage that
expects a JSON answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.I)
_FENCE_CLOSE = re.compile(r"\s*```$", re.I)
_OBJECT = re.compile(r"\{[\s\S]*\}")


def parse_json(text: str | None) -> Any | None:
    if not text:
        return None
    cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text.strip())).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        match = _OBJECT.search(text)
        if match:
            try:
                return json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                pass
        # Truncated JSON (model hit its output-token cap mid-object) — salvage the largest
        # valid prefix by cutting at a structural boundary and closing open brackets/strings.
        return _repair_truncated(cleaned if cleaned.startswith("{") else text)


def _close_open(chunk: str) -> str | None:
    """Close any unterminated string/brackets in `chunk` so it can parse. Drops a dangling
    trailing comma. Returns None if structurally hopeless (no open object)."""
    stack: list[str] = []
    in_str = esc = False
    for ch in chunk:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
    body = chunk
    if in_str:
        body += '"'
    body = body.rstrip()
    if body.endswith(","):
        body = body[:-1]
    return body + "".join(reversed(stack)) if stack or in_str else body


def _repair_truncated(text: str | None) -> Any | None:
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    frag = text[start:]
    # Try the full fragment, then each '}'/']' boundary from the end back — the largest
    # prefix that closes to valid JSON wins (e.g. a ranking array cut mid-item keeps its
    # complete items).
    cuts = [len(frag)] + [i + 1 for i, ch in enumerate(frag) if ch in "}]"][::-1]
    seen: set[int] = set()
    for cut in cuts:
        if cut in seen:
            continue
        seen.add(cut)
        closed = _close_open(frag[:cut])
        if not closed:
            continue
        try:
            return json.loads(closed)
        except (json.JSONDecodeError, ValueError):
            continue
    return None
