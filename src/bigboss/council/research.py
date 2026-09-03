"""Track B — deep-research-per-project (Council + Fable). The operator's method: every project gets deep
research from all angles → a clear phased roadmap, then a fast POC sprint (the POC is easy; the
development around it is the long tail). This is the throne's work — led by the most powerful model
(Fable), grounded in the project's own docs + the operator's authoritative notes + its lineage.

MVP = one project → a structured brief + roadmap. Follow-ons (see PLAN): inject Brainz historical
context, cheap-seat cross-verification, batch across the portfolio, auto-adopt the roadmap into the
project's own workflow/PLAN.md on approval.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..crawler import collect_sources
from .parse import parse_json
from .providers import call_anthropic_model

RESEARCH_MODEL_ENV = "COUNCIL_RESEARCH_MODEL"
DEFAULT_RESEARCH_MODEL = "claude-fable-5"
_RESEARCH_TOKENS = 8000
_DOC_BUDGET = 30_000

RESEARCH_SYSTEM = (
    "You are Fable, the throne of The Council — the most capable model, taking real time to think about a "
    "single project for a solo developer. You do DEEP research: survey the current methods and "
    "capabilities in the project's domain, judge what is newly feasible now that AI has advanced, and lay out "
    "a clear phased roadmap. Respect how the operator works: nothing is truly abandoned (dormant = waiting for the tech "
    "or skill to catch up); he evolves rather than rebuilds, so harvest the best parts from lineage ancestors "
    "instead of starting over; research-first then a fast POC sprint. The operator's OWNER NOTES are authoritative — "
    "trust them over inference. Respond ONLY with valid JSON, no markdown, no code fences."
)


def _find_project(store, slug: str) -> dict[str, Any] | None:
    for p in store.list_projects(include_ambiguous=True):
        if p["slug"].casefold() == slug.casefold():
            return p
    return None


def _gather_docs(project: dict[str, Any]) -> list[tuple[str, str]]:
    """Read the project's key docs (honoring no_crawl + local reachability). Empty if remote/no_crawl."""
    if project.get("no_crawl"):
        return []
    path = project.get("canonical_path") or ""
    if not path or not Path(path).is_dir():
        return []
    try:
        return collect_sources(Path(path), _DOC_BUDGET)
    except OSError:
        return []


def _build_prompt(project: dict[str, Any], docs: list[tuple[str, str]]) -> str:
    intel = project.get("intel") or {}
    lineage = []
    if project.get("supersedes"):
        lineage.append(f"evolved from: {project['supersedes']}")
    if project.get("evolved_into"):
        lineage.append(f"evolved into: {project['evolved_into']}")
    header = [
        f"PROJECT: {project['slug']}  ({project.get('kind') or 'unknown'})",
        f"purpose: {project.get('purpose') or 'n/a'}",
        f"lifecycle/ownership: {project.get('lifecycle') or '?'} / {project.get('ownership') or '?'}",
        f"lineage: {'; '.join(lineage) or 'none recorded'}",
        f"owner notes (authoritative): {project.get('notes') or 'none'}",
        f"current status: {intel.get('status_line') or 'n/a'}",
        f"known blockers: {'; '.join(intel.get('blockers') or []) or 'none noted'}",
        f"roadmap now: {intel.get('roadmap_now') or 'n/a'}",
        f"last active: {(project.get('last_activity_at') or '')[:10] or 'n/a'}",
    ]
    if project.get("track_it"):
        header.append(f"SENSITIVE (track-it): {project.get('track_note') or 'handle carefully'}")
    doc_block = "\n\n".join(f"===== {rel} =====\n{text}" for rel, text in docs) if docs else \
        "(no readable docs — reason from the metadata + owner notes above)"
    return (
        "\n".join(header)
        + "\n\n----- PROJECT DOCS -----\n" + doc_block
        + "\n\n----- YOUR TASK -----\n"
        "Do deep research on THIS project and return a structured plan. Respond in exactly this JSON:\n"
        "{\n"
        '  "summary": "<2-3 sentences: what it is + where it truly stands>",\n'
        '  "current_landscape": "<current methods/capabilities in this domain and what is newly feasible '
        'NOW because AI has advanced — this is why a dormant idea may be ready>",\n'
        '  "angles": ["<distinct approaches/framings worth considering>"],\n'
        '  "reuse_from_lineage": ["<what to harvest from ancestors/related projects instead of rebuilding>"],\n'
        '  "risks": ["<key risks, unknowns, or things that will eat time>"],\n'
        '  "poc_sprint": "<the single fastest impossible-feeling POC to attempt first to prove the core>",\n'
        '  "roadmap": [\n'
        '    {"phase": "Research", "goal": "<goal>", "tasks": ["<task>"]},\n'
        '    {"phase": "POC sprint", "goal": "<goal>", "tasks": ["<task>"]},\n'
        '    {"phase": "Development", "goal": "<goal>", "tasks": ["<task>"]}\n'
        "  ],\n"
        '  "open_questions": ["<questions for the operator>"]\n'
        "}\n"
        "Be concrete and specific to this project — no generic advice. Ground every claim in the docs or notes."
    )


def research_model() -> str:
    return os.environ.get(RESEARCH_MODEL_ENV) or DEFAULT_RESEARCH_MODEL


def run_project_research(store, slug: str, model: str | None = None) -> dict[str, Any]:
    """Fable-led deep research on one project → {slug, model, brief, usage, docs_used, status}.
    Raises KeyError if the slug isn't in the registry, ProviderError-ish status on model failure."""
    project = _find_project(store, slug)
    if project is None:
        raise KeyError(f"Project not found: {slug}")
    docs = _gather_docs(project)
    prompt = _build_prompt(project, docs)
    mdl = model or research_model()
    result = call_anthropic_model(mdl, RESEARCH_SYSTEM, prompt, max_tokens=_RESEARCH_TOKENS)
    if result["status"] != "success":
        return {"slug": slug, "model": mdl, "brief": None, "usage": result.get("usage", {}),
                "docs_used": len(docs), "status": "error", "error": result.get("error")}
    brief = parse_json(result.get("answer")) or {}
    return {"slug": slug, "project_id": project["id"], "model": result.get("model") or mdl,
            "brief": brief, "usage": result.get("usage", {}), "docs_used": len(docs),
            "status": "success" if brief else "unparsed",
            "raw": None if brief else (result.get("answer") or "")[:500]}
