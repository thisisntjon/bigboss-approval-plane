"""Stdlib client for Squire - an LM Studio instance (local or on a LAN box), OpenAI-compatible /v1 API.

Defaults mirror docs/squire.md and the ecosystem-wide env names
(SQUIRE_BASE_URL / SQUIRE_MODEL). No third-party dependencies by design.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"  # LM Studio default; override with SQUIRE_BASE_URL
DEFAULT_MODEL = "google/gemma-4-e4b"


class SquireError(RuntimeError):
    """Squire was unreachable or returned an unusable response."""


class SquireClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        api_key: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("SQUIRE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("SQUIRE_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("SQUIRE_API_KEY") or ""

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 700,
    ) -> dict[str, Any]:
        """One chat completion. Returns {content, model, usage, latency_ms}."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.monotonic()
        data = self._post_json("/chat/completions", body)
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SquireError(f"Squire returned an unexpected completion shape: {exc!r}")
        usage = data.get("usage") or {}
        return {
            "content": str(content or ""),
            "model": str(data.get("model") or self.model),
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            },
            "latency_ms": latency_ms,
        }

    def models(self) -> list[str]:
        data = self._get_json("/models")
        return [str(m.get("id") or "") for m in data.get("data") or []]

    def ping(self) -> dict[str, Any]:
        """Cheap reachability probe against /models. Never raises."""
        started = time.monotonic()
        try:
            self.models()
            return {"ok": True, "detail": "", "latency_ms": int((time.monotonic() - started) * 1000)}
        except SquireError as exc:
            return {"ok": False, "detail": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._send(req)

    def _get_json(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.base_url + path, headers=self._headers(), method="GET")
        return self._send(req)

    def _send(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                pass
            raise SquireError(f"Squire HTTP {exc.code}: {detail or exc.reason}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SquireError(f"Squire unreachable: {exc}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SquireError(f"Squire returned non-JSON: {exc}")
