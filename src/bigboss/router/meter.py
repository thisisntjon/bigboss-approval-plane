"""Extract the Anthropic ``usage`` object from a proxied response, streamed or not.

Streaming: usage appears in ``message_start`` (input side + the served model) and
in the final, **cumulative** ``message_delta`` (output + final input/cache). The
final ``message_delta`` also carries the ``stop_reason`` (``"refusal"`` flags a
Fable->Opus content reroute). Non-stream: usage/model/stop_reason are on the
top-level Message object.

The meter watches the stream fly past and only parses the two usage-bearing frame
types - it never needs content deltas, so it ignores ~99% of the bytes and is
robust to SSE frames split across TCP chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    served_model: str = ""
    stop_reason: str | None = None
    cache_creation: dict[str, int] | None = None  # {ephemeral_5m/1h_input_tokens} if present

    def as_pricing_usage(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }
        if self.cache_creation:
            data["cache_creation"] = self.cache_creation
        return data


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class UsageMeter:
    """Feed streamed SSE chunks via feed(), or a full non-stream body via
    feed_full(); read the accumulated result via result()."""

    def __init__(self) -> None:
        self._buf = b""
        self.usage = Usage()

    def feed(self, chunk: bytes) -> None:
        """Absorb a raw chunk of the SSE byte stream (may contain partial frames)."""
        if not chunk:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                event = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            self._absorb(event)

    def feed_full(self, body: bytes) -> None:
        """Absorb a complete non-streamed Message JSON body."""
        try:
            self._absorb(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            pass

    def result(self) -> Usage:
        return self.usage

    # -- internals --------------------------------------------------------

    def _absorb(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self._absorb_message(message)
        elif etype == "message_delta":
            # usage is cumulative -> overwrite, don't sum.
            self._merge_usage(event.get("usage") or {})
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.usage.stop_reason = delta["stop_reason"]
        elif etype == "message" or ("usage" in event and "model" in event):
            # Non-stream Message object.
            self._absorb_message(event)

    def _absorb_message(self, message: dict[str, Any]) -> None:
        if message.get("model"):
            self.usage.served_model = message["model"]
        if message.get("stop_reason"):
            self.usage.stop_reason = message["stop_reason"]
        self._merge_usage(message.get("usage") or {})

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        for field in _TOKEN_FIELDS:
            value = usage.get(field)
            if value is not None:
                setattr(self.usage, field, int(value))
        breakdown = usage.get("cache_creation")
        if isinstance(breakdown, dict):
            self.usage.cache_creation = {
                "ephemeral_5m_input_tokens": int(breakdown.get("ephemeral_5m_input_tokens") or 0),
                "ephemeral_1h_input_tokens": int(breakdown.get("ephemeral_1h_input_tokens") or 0),
            }
