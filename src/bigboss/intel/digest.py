"""Digest crawled project docs into structured intel via Squire.

The crawl itself is the shared, general-purpose `bigboss.crawler` tool (also
exposed as MCP tools + `bigboss crawl` for Squire and other consumers); this
module is one consumer of it. Cost discipline: the crawl is free (local fs),
the extraction is free (Squire on the remote host), and the crawler's content hash skips
the Squire call entirely when the source docs have not changed since the last
digest. Fable 5 never sees raw docs - only the digested snapshot stored in
project_intel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..crawler import (  # re-exported for consumers/tests of this module
    DOC_FILES as SOURCE_FILES,
    PER_FILE_CHARS,
    TOTAL_CHARS,
    collect_sources,
    source_hash,
)
from .endpoints import Endpoint, get_endpoint, throttle
from .squire import SquireClient, SquireError


# Squire's loaded context window is not under our control (LM Studio config on
# the remote host). On a "context exceeded" error the digest retries down these budgets.
BUDGET_TIERS = (TOTAL_CHARS, TOTAL_CHARS // 2, TOTAL_CHARS // 4)

INTEL_CLIENT_LABEL = "bigboss-intel"

_SYSTEM_PROMPT = (
    "You are Squire, the project-intel extractor for the BigBoss portfolio tracker. "
    "You read a project's planning documents and answer ONLY with a single JSON object, "
    "no markdown fences, no prose, matching exactly this schema:\n"
    '{"status_line": "one sentence: where the project is right now", '
    '"blockers": ["each concrete blocker or open dependency, empty list if none"], '
    '"roadmap_now": "the current phase or milestone being worked", '
    '"roadmap_next": "the next planned phase or milestone"}\n'
    "Be concrete and terse. Use only facts present in the documents; never invent."
)


def messages_to_text(messages: list[dict[str, str]]) -> str:
    """Flatten chat messages into one readable prompt string for the egress audit."""
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


def build_messages(name: str, path: str, sources: list[tuple[str, str]]) -> list[dict[str, str]]:
    parts = [f"Project: {name}\nPath: {path}\n"]
    for rel, text in sources:
        parts.append(f"===== {rel} =====\n{text}\n")
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_intel(text: str) -> dict[str, Any]:
    """Coerce a small-model reply into the intel shape. Tolerates fences and preamble."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise SquireError(f"Squire reply contained no JSON object: {text[:200]!r}")
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise SquireError(f"Squire reply was not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SquireError("Squire reply JSON was not an object.")
    blockers = data.get("blockers")
    if not isinstance(blockers, list):
        blockers = [str(blockers)] if blockers else []
    return {
        "status_line": str(data.get("status_line") or "").strip()[:300],
        "blockers": [str(b).strip()[:300] for b in blockers if str(b).strip()],
        "roadmap_now": str(data.get("roadmap_now") or "").strip()[:300],
        "roadmap_next": str(data.get("roadmap_next") or "").strip()[:300],
    }


def digest_project(
    client: SquireClient,
    name: str,
    path: str,
    project_path: Path,
    sources: list[tuple[str, str]],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """One Squire extraction, shrinking the doc budget if the remote host's loaded context
    window is smaller than ours. Returns (intel, call_meta, prompt_text). Raises SquireError."""
    last_error: SquireError | None = None
    last_prompt = ""
    for tier in BUDGET_TIERS:
        tier_sources = (
            sources if tier == TOTAL_CHARS else collect_sources(project_path, total_chars=tier)
        )
        messages = build_messages(name, path, tier_sources)
        last_prompt = messages_to_text(messages)
        try:
            reply = client.chat(messages)
            return parse_intel(reply["content"]), reply, last_prompt
        except SquireError as exc:
            last_error = exc
            if "context" not in str(exc).lower():
                raise
    raise last_error if last_error else SquireError("Digest failed with no attempt made.")


def refresh_intel(
    store,
    client: SquireClient | None = None,
    slugs: list[str] | None = None,
    force: bool = False,
    endpoint: str | Endpoint | None = None,
) -> dict[str, Any]:
    """Digest every registered project whose docs changed (or all with force=True).

    `endpoint` selects which local-compute box does the extraction (default:
    Squire). Guarded endpoints like Beastmode are rate-limited per their config
    and ledgered under their own endpoint/client label. Skips cleanly when the
    endpoint is down: the last good snapshots stay in place and the health
    transition is ledgered/evented.
    """
    ep = endpoint if isinstance(endpoint, Endpoint) else get_endpoint(endpoint)
    if client is None:
        client = ep.client()
    client_label = INTEL_CLIENT_LABEL if ep.name == "squire" else f"{INTEL_CLIENT_LABEL}-{ep.name}"
    wanted = {s.casefold() for s in slugs} if slugs else None

    throttle(ep)
    probe = client.ping()
    store.record_squire_health(
        probe["ok"], detail=probe["detail"], latency_ms=probe["latency_ms"], endpoint=ep.name
    )
    summary = {
        "endpoint": ep.name,
        "squire_up": probe["ok"],
        "digested": 0,
        "skipped_unchanged": 0,
        "skipped_no_sources": 0,
        "skipped_no_crawl": 0,
        "skipped_pending": 0,
        "failed": 0,
        "projects": 0,
    }
    if not probe["ok"]:
        return summary

    for project in store.list_projects():
        if wanted is not None and project["slug"].casefold() not in wanted:
            continue
        summary["projects"] += 1
        # no_crawl is a privacy exclusion, not a preference: it holds even when
        # the slug is requested explicitly (the project's own egress policy wins).
        if project.get("no_crawl"):
            summary["skipped_no_crawl"] += 1
            continue
        # digest_pending: a newly-discovered project is not sent off-box until the
        # human approves its discovery batch (E4a.1). Holds even for explicit slug
        # requests, like no_crawl.
        if project.get("digest_pending"):
            summary["skipped_pending"] += 1
            continue
        path = Path(project["canonical_path"])
        sources = collect_sources(path)
        if not sources:
            summary["skipped_no_sources"] += 1
            continue
        h = source_hash(sources)
        existing = project.get("intel")
        if (
            not force
            and existing
            and existing.get("source_hash") == h
            and not existing.get("error")
        ):
            summary["skipped_unchanged"] += 1
            continue
        egress = dict(
            endpoint=ep.name,
            exposure=ep.exposure,
            base_url=ep.base_url,
            client=client_label,
            purpose="digest",
            project_id=project["id"],
            project_slug=project["slug"],
            source_files=[rel for rel, _ in sources],
            content_hash=h,
            chars_sent=sum(len(text) for _, text in sources),
        )
        prompt_text = messages_to_text(
            build_messages(project["name"], project["canonical_path"], sources)
        )
        try:
            throttle(ep)
            intel, call, prompt_text = digest_project(
                client,
                project["name"],
                project["canonical_path"],
                path,
                sources,
            )
        except SquireError as exc:
            store.mark_intel_error(project["id"], str(exc))
            store.record_squire_call(
                client=client_label,
                purpose="digest",
                endpoint=ep.name,
                model=client.model,
                ok=False,
                error=str(exc)[:500],
            )
            store.record_egress(
                model=client.model,
                ok=False,
                prompt_text=prompt_text,
                response_text=str(exc)[:2000],
                **egress,
            )
            summary["failed"] += 1
            continue
        intel["source_hash"] = h
        intel["source_files"] = [rel for rel, _ in sources]
        intel["model"] = call["model"]
        store.upsert_project_intel(project["id"], intel)
        store.record_squire_call(
            client=client_label,
            purpose="digest",
            endpoint=ep.name,
            model=call["model"],
            prompt_tokens=call["usage"]["prompt_tokens"],
            completion_tokens=call["usage"]["completion_tokens"],
            latency_ms=call["latency_ms"],
            ok=True,
        )
        store.record_egress(
            model=call["model"],
            ok=True,
            tokens_sent=call["usage"]["prompt_tokens"],
            tokens_received=call["usage"]["completion_tokens"],
            prompt_text=prompt_text,
            response_text=call["content"],
            **egress,
        )
        summary["digested"] += 1
    return summary
