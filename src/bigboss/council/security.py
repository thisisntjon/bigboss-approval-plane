"""Security boundary for the Council pipeline — faithful port of the-council-capstone
`lib/security.mjs`. Redact-before-use, classify-before-run, frozen tool allowlist.
Dependency-free, deterministic pattern matching; identical offline and live.
"""

from __future__ import annotations

import re

# Each regex targets one concrete leak shape (not a general PII engine). JS /gi -> re.I;
# JS \b word boundaries port directly. Python re.search is stateless, so unlike the JS
# original there is no lastIndex to reset.
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
# Provider key prefixes this project's vendors use: sk- (OpenAI/Anthropic), the Google key prefix, xai- (xAI).
# The Google prefix is written as a bracketed class so the literal prefix never trips secret scanners on this file.
API_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|[A]Iza[0-9A-Za-z_-]{20,}|xai-[A-Za-z0-9_-]{12,})\b")
# These only flag (raise the risk level); they never rewrite text.
SECRET_WORD_RE = re.compile(r"\b(password|api[_-]?key|secret|token|credential|private key)\b", re.I)
PROMPT_INJECTION_RE = re.compile(
    r"\b(ignore previous|system prompt|developer message|exfiltrate|reveal secrets|bypass|jailbreak)\b",
    re.I,
)

# Least-authority contract: the fixture pipeline may only use these read-only/report
# tools. A tuple (immutable) so nothing can widen it at runtime; every audit trail embeds it.
TOOL_ALLOWLIST = (
    "load_fixture",
    "redact_input",
    "extract_claims",
    "verify_claims_against_fixture",
    "write_audit_report",
)


def redact_sensitive_text(text: object) -> str:
    """Rewrite sensitive spans with typed placeholders. Order matters: API keys first,
    so a key is never partially consumed by the looser email/phone patterns."""
    s = "" if text is None else str(text)
    s = API_KEY_RE.sub("[REDACTED:api-key]", s)
    s = EMAIL_RE.sub("[REDACTED:email]", s)
    s = PHONE_RE.sub("[REDACTED:phone]", s)
    return s


def classify_input_risk(text: object) -> dict:
    """Classify input risk before the pipeline runs. Returns {level, warnings,
    redacted_preview}. Severity: credential/injection -> high; other PII -> medium; clean -> low."""
    source = "" if text is None else str(text)
    warnings: list[str] = []
    if API_KEY_RE.search(source):
        warnings.append("possible API key")
    if EMAIL_RE.search(source):
        warnings.append("email address")
    if PHONE_RE.search(source):
        warnings.append("phone number")
    if SECRET_WORD_RE.search(source):
        warnings.append("secret-related wording")
    if PROMPT_INJECTION_RE.search(source):
        warnings.append("prompt-injection wording")

    if not warnings:
        level = "low"
    elif any(("API key" in w) or ("prompt" in w) for w in warnings):
        level = "high"
    else:
        level = "medium"

    return {
        "level": level,
        "warnings": warnings,
        "redacted_preview": redact_sensitive_text(source)[:400],
    }
