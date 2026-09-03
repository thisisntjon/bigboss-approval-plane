"""Live vendor callers for the Council — stdlib `urllib` (M1b). Reuses the transport
idiom from `intel/squire.py` (OpenAI-compatible) and `router/upstream.py` (Anthropic).

Each seat maps to a current, verified 2026 model (probed 2026-07-02). Model IDs are
env-overridable so a seat's tier can be swapped without code changes. Every call returns
the uniform result shape the engine relies on:
    {id, name, provider, model, answer|None, latency_ms, status, usage:{input,output}, error?}
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Vendor keys live in the repo `.env` (gitignored). BigBoss is stdlib-only (no python-dotenv),
# so load them into the environment once, without clobbering anything already set.
_ENV_LOADED = False
_VENDOR_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "GROK_API_KEY", "XAI_API_KEY", "GOOGLEAISTUDIO_API_KEY",
)


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    envfile = Path(__file__).resolve().parents[3] / ".env"  # <repo>/.env
    if not envfile.exists():
        return
    try:
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _VENDOR_KEYS and not os.environ.get(k):
                os.environ[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
XAI_URL = "https://api.x.ai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 90
RETRIES = 2  # total attempts = RETRIES + 1


class ProviderError(RuntimeError):
    pass


def _seat(env_model: str, default_model: str, env_key: str) -> dict[str, Any]:
    return {"model_env": env_model, "default_model": default_model, "key_env": env_key}


# The council seats. Cheap-tier current models (probed 2026-07-02); env-overridable.
COUNCIL_SEATS: dict[str, dict[str, Any]] = {
    "claude": {"name": "Claude", "provider": "anthropic",
               **_seat("COUNCIL_CLAUDE_MODEL", "claude-haiku-4-5-20251001", "ANTHROPIC_API_KEY")},
    "gpt": {"name": "GPT", "provider": "openai",
            **_seat("COUNCIL_GPT_MODEL", "gpt-5.4-mini", "OPENAI_API_KEY")},
    "gemini": {"name": "Gemini", "provider": "google",
               **_seat("COUNCIL_GEMINI_MODEL", "gemini-3.5-flash", "GOOGLE_API_KEY")},
    "grok": {"name": "Grok", "provider": "xai",
             **_seat("COUNCIL_GROK_MODEL", "grok-4.3", "GROK_API_KEY")},
}


def seat_model(seat_id: str) -> str:
    seat = COUNCIL_SEATS[seat_id]
    return os.environ.get(seat["model_env"]) or seat["default_model"]


def seat_key(seat_id: str) -> str:
    _ensure_env()
    seat = COUNCIL_SEATS[seat_id]
    key = os.environ.get(seat["key_env"]) or ""
    if seat_id == "grok" and not key:
        key = os.environ.get("XAI_API_KEY") or ""
    return key


def configured_seats() -> list[str]:
    """Seat ids whose API key is present in the environment."""
    return [sid for sid in COUNCIL_SEATS if seat_key(sid)]


def _http_post(url: str, headers: dict[str, str], body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        raise ProviderError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProviderError(f"unreachable: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(f"non-JSON response: {exc}") from exc


def _with_retry(fn: Callable[[], Any], retries: int = RETRIES) -> Any:
    """Retry on transient errors (HTTP 429/5xx, network), backoff+jitter. Deterministic
    jitter (attempt-based, no RNG) to keep tests reproducible."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            msg = str(exc)
            transient = ("unreachable" in msg) or any(f"HTTP {c}" in msg for c in (429, 500, 502, 503, 504))
            if not transient or attempt == retries:
                raise
            time.sleep(0.5 * (2 ** attempt))
    raise last  # unreachable


def _call_anthropic(seat_id: str, system: str, user: str, max_tokens: int, timeout: int) -> dict:
    data = _http_post(
        ANTHROPIC_URL,
        {"x-api-key": seat_key(seat_id), "anthropic-version": "2023-06-01"},
        {"model": seat_model(seat_id), "max_tokens": max_tokens, "system": system,
         "messages": [{"role": "user", "content": user}]},
        timeout,
    )
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    usage = data.get("usage") or {}
    return {"answer": text, "model": data.get("model") or seat_model(seat_id),
            "usage": {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0)}}


def _call_openai_compatible(seat_id: str, url: str, system: str, user: str, max_tokens: int, timeout: int) -> dict:
    data = _http_post(
        url,
        {"Authorization": f"Bearer {seat_key(seat_id)}"},
        {"model": seat_model(seat_id),
         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
         "max_completion_tokens": max_tokens},
        timeout,
    )
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    usage = data.get("usage") or {}
    return {"answer": text, "model": data.get("model") or seat_model(seat_id),
            "usage": {"input": int(usage.get("prompt_tokens") or 0), "output": int(usage.get("completion_tokens") or 0)}}


def _call_gemini(seat_id: str, system: str, user: str, max_tokens: int, timeout: int) -> dict:
    model = seat_model(seat_id)
    url = GEMINI_URL.format(model=model) + f"?key={seat_key(seat_id)}"
    data = _http_post(
        url, {},
        {"system_instruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": user}]}],
         # Gemini 3.x defaults to a "thinking" phase that consumes maxOutputTokens and
         # truncates the visible answer; disable it so the council seat answers directly.
         "generationConfig": {"maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}},
        timeout,
    )
    text = ""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        text = ""
    um = data.get("usageMetadata") or {}
    return {"answer": text, "model": model,
            "usage": {"input": int(um.get("promptTokenCount") or 0), "output": int(um.get("candidatesTokenCount") or 0)}}


def call_anthropic_model(
    model: str, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Call a specific Anthropic model by id (e.g. Fable, the research 'throne') outside the
    fixed council seats. Same uniform result shape as call_seat; never raises."""
    _ensure_env()
    started = time.monotonic()
    base = {"id": "research", "name": "Fable", "provider": "anthropic", "model": model}
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not key:
        return {**base, "answer": None, "status": "error", "usage": {"input": 0, "output": 0},
                "latency_ms": 0, "error": "no API key (ANTHROPIC_API_KEY)"}
    try:
        data = _with_retry(lambda: _http_post(
            ANTHROPIC_URL,
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {"model": model, "max_tokens": max_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
            timeout,
        ))
    except ProviderError as exc:
        return {**base, "answer": None, "status": "error", "usage": {"input": 0, "output": 0},
                "latency_ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    usage = data.get("usage") or {}
    return {**base, "answer": text, "model": data.get("model") or model, "status": "success",
            "usage": {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0)},
            "latency_ms": int((time.monotonic() - started) * 1000)}


def call_seat(
    seat_id: str, system: str, user: str, *, max_tokens: int = DEFAULT_MAX_TOKENS, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Call one council seat. Never raises — errors are captured in the uniform result."""
    seat = COUNCIL_SEATS[seat_id]
    started = time.monotonic()
    base = {"id": seat_id, "name": seat["name"], "provider": seat["provider"], "model": seat_model(seat_id)}
    if not seat_key(seat_id):
        return {**base, "answer": None, "status": "error", "usage": {"input": 0, "output": 0},
                "latency_ms": 0, "error": f"no API key ({seat['key_env']})"}
    provider = seat["provider"]
    try:
        if provider == "anthropic":
            r = _with_retry(lambda: _call_anthropic(seat_id, system, user, max_tokens, timeout))
        elif provider == "openai":
            r = _with_retry(lambda: _call_openai_compatible(seat_id, OPENAI_URL, system, user, max_tokens, timeout))
        elif provider == "xai":
            r = _with_retry(lambda: _call_openai_compatible(seat_id, XAI_URL, system, user, max_tokens, timeout))
        elif provider == "google":
            r = _with_retry(lambda: _call_gemini(seat_id, system, user, max_tokens, timeout))
        else:
            raise ProviderError(f"unknown provider {provider!r}")
    except ProviderError as exc:
        return {**base, "answer": None, "status": "error", "usage": {"input": 0, "output": 0},
                "latency_ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
    return {**base, "answer": r["answer"], "model": r["model"], "status": "success",
            "usage": r["usage"], "latency_ms": int((time.monotonic() - started) * 1000)}
