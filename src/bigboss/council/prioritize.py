"""M2 — project prioritization via the Council. A rank-and-aggregate flow (NOT the M1
verify-swarm): each seat ranks a top-N shortlist from the portfolio context; a Borda count
aggregates them into a consensus shortlist with dissent preserved. Reuses `providers`.

M2.1 — ideation harvest: the Council is not only a ranker, it is an ideation engine. Each seat
also names cross-project **synergies**; a single consolidation pass (reusing the engine's synthesis
idiom) turns all four seats' rankings + reasons + synergies into an **ideation brief** (deduped
synergies + portfolio opportunities + reasoned tensions). Per-seat reasoning and dissent reasons are
preserved rather than flattened away.

Input is the free scout context (`list_projects` + embedded `project_intel`); output is a ratifiable
ranking (becomes `pin_rank` on approval — see store.resolve_approval) plus the ideation block.
"""

from __future__ import annotations

import os
from typing import Any

from .engine import _accumulate_usage, _parallel
from .parse import parse_json
from .providers import call_seat, configured_seats

DEFAULT_TOP_N = 10
_RANK_TOKENS = 1900  # richer prompt (notes/lineage/synergies) needs headroom or seats truncate + drop
_IDEATION_TOKENS = 1300

PRIORITIZE_SYSTEM = (
    "You are a strategic portfolio advisor on The Council. Respond ONLY with valid JSON, no markdown, no code "
    "fences."
)
IDEATION_SYSTEM = (
    "You are the Council's synthesis lead. Four advisors independently ranked a solo developer's project "
    "portfolio and noted cross-project synergies. Consolidate their thinking into portfolio-level IDEATION — "
    "the connections and opportunities that only emerge from seeing every project at once. Respond ONLY with "
    "valid JSON, no markdown, no code fences."
)


def build_portfolio_context(store) -> list[dict[str, Any]]:
    """Compact per-project context for prioritization. Skips ambiguous/gone; folds in the
    Squire-digested intel when present."""
    out: list[dict[str, Any]] = []
    for p in store.list_projects(include_ambiguous=False):
        if p.get("excluded"):
            continue  # owner hand-excluded (done/submitted, or work-not-home)
        intel = p.get("intel") or {}
        out.append({
            "slug": p["slug"],
            "kind": p.get("kind"),
            "lifecycle": p.get("lifecycle") or "",
            "ownership": p.get("ownership") or "",
            "notes": (p.get("notes") or "")[:240],
            "evolved_into": p.get("evolved_into") or "",
            "supersedes": p.get("supersedes") or "",
            "track_it": bool(p.get("track_it")),
            "purpose": (p.get("purpose") or "")[:200],
            "status": intel.get("status_line") or "",
            "blockers": intel.get("blockers") or [],
            "roadmap_now": intel.get("roadmap_now") or "",
            "last_activity": (p.get("last_activity_at") or "")[:10],
            "pinned": bool(p.get("pinned")),
            "pin_rank": p.get("pin_rank"),
        })
    return out


def _build_prompt(context: list[dict], top_n: int) -> str:
    lines = []
    for c in context:
        blockers = "; ".join(c["blockers"][:2]) if c["blockers"] else "none noted"
        pin = f" [currently pinned #{c['pin_rank']}]" if c["pinned"] else ""
        tags = ", ".join(t for t in (c.get("lifecycle"), c.get("ownership")) if t)
        tag_str = f" [{tags}]" if tags else ""
        lin = ""
        if c.get("evolved_into"):
            lin += f" | evolved into: {c['evolved_into']}"
        if c.get("supersedes"):
            lin += f" | evolved from: {c['supersedes']}"
        owner_note = f" | OWNER NOTE: {c['notes']}" if c.get("notes") else ""
        lines.append(
            f"- {c['slug']} ({c['kind']}){tag_str}{pin}: {c['purpose']} "
            f"| status: {c['status'] or 'n/a'} | blockers: {blockers} "
            f"| now: {c['roadmap_now'] or 'n/a'} | last active: {c['last_activity'] or 'n/a'}"
            f"{lin}{owner_note}"
        )
    portfolio = "\n".join(lines)
    return (
        "You are prioritizing a solo developer's project portfolio. From the projects below, choose "
        f"the TOP {top_n} to focus on RIGHT NOW, ranked most-important first.\n\n"
        "How the operator works (respect this):\n"
        "- Nothing is truly abandoned. As models improve each year and his skill compounds, 'impossible' "
        "projects become realistic and get revisited, so DORMANT = a waiting room, not a graveyard. Do not "
        "dismiss a dormant project as dead; weigh whether NOW is its moment.\n"
        "- He evolves rather than rebuilds, harvesting the best parts forward along lineages. If a project "
        "'evolved into' a successor, treat it as an ancestor whose value now lives in the successor - do NOT "
        "rank an ancestor against its own successor; credit the active successor.\n"
        "- Method is deep-research-first then a fast POC sprint; the POC is easy, the development around it is "
        "the long tail. Value research-readiness and a clear roadmap.\n"
        "- OWNER NOTES are the operator's own authoritative words - trust them over doc inference.\n\n"
        "Prioritize by: momentum and recent activity, unblocking value, strategic leverage (a project that "
        "unlocks others), and closeness to a meaningful outcome. A dormant project with a clear revival path "
        "or newly-viable-because-of-better-AI can outrank a busy but low-leverage one.\n\n"
        "SEPARATELY, identify cross-project SYNERGIES: pairs (or small groups) of projects that reinforce each "
        "other — one unlocks or feeds another, they share infrastructure, or combining them multiplies value. "
        "These are the ideation payoff of seeing the whole portfolio at once; name the real ones, don't force "
        "them.\n\n"
        f"Projects:\n{portfolio}\n\n"
        "Respond in this exact JSON format (use the exact slugs above):\n"
        "{\n"
        '  "ranking": [\n'
        '    {"slug": "<slug>", "reason": "<1-2 sentences: the dominant driver (momentum / unblock / leverage '
        '/ closeness) and why it ranks here>"}\n'
        "  ],\n"
        '  "synergies": [\n'
        '    {"projects": ["<slugA>", "<slugB>"], "insight": "<how they reinforce: one unlocks the other, '
        'shared infra, or combined leverage>"}\n'
        "  ],\n"
        '  "overall": "<2-3 sentences on the strategic shape of these priorities>"\n'
        "}\n"
        f"Include exactly {top_n} projects in ranking (or fewer only if the portfolio is smaller), best first. "
        "Include 0-5 synergies — quality over quantity."
    )


def _borda(per_seat: dict[str, list[str]], top_n: int) -> list[str]:
    """Borda count over each seat's ranked slug list. Rank i (0-based) -> (top_n - i) points.
    Sort by total points desc; tie-break by how many seats ranked it, then slug for determinism."""
    points: dict[str, float] = {}
    seat_count: dict[str, int] = {}
    for ranking in per_seat.values():
        for i, slug in enumerate(ranking[:top_n]):
            points[slug] = points.get(slug, 0.0) + (top_n - i)
            seat_count[slug] = seat_count.get(slug, 0) + 1
    ordered = sorted(points, key=lambda s: (-points[s], -seat_count[s], s))
    return ordered[:top_n]


def _clean_synergies(raw: Any, valid_slugs: set[str]) -> list[dict]:
    """Keep synergies whose projects are >=2 known slugs; normalize shape."""
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        projects = [str(s) for s in (item.get("projects") or []) if s in valid_slugs]
        projects = list(dict.fromkeys(projects))  # de-dup, preserve order
        insight = (item.get("insight") or "").strip()
        if len(projects) >= 2 and insight:
            out.append({"projects": projects, "insight": insight})
    return out


def _collect_reasons(seat_results: list[dict], slugs: list[str]) -> dict[str, list[dict]]:
    """Preserve EVERY seat's take per slug (not just the first) — the detail M2 flattened away."""
    detail: dict[str, list[dict]] = {}
    for slug in slugs:
        takes = []
        for s in seat_results:
            r = (s.get("reasons") or {}).get(slug)
            if r:
                takes.append({"seat": s["seat"], "reason": r})
        if takes:
            detail[slug] = takes
    return detail


def _merge_reasons(seat_results: list[dict], consensus: list[str]) -> dict[str, str]:
    """One representative reason per consensus slug (first seat that gave one) — kept for the
    compact card line; the full per-seat detail lives in reasons_detail."""
    reasons: dict[str, str] = {}
    for slug in consensus:
        for s in seat_results:
            r = (s.get("reasons") or {}).get(slug)
            if r:
                reasons[slug] = r
                break
    return reasons


def _pick_synth_seat(seat_ids: list[str]) -> str:
    """Which seat consolidates the ideation. Env-overridable; default first configured seat.
    (No throne yet — M4 makes this the elected throne.)"""
    override = os.environ.get("COUNCIL_SYNTH_SEAT", "").strip()
    if override and override in seat_ids:
        return override
    return seat_ids[0]


def _build_ideation_prompt(consensus: list[str], reasons_detail: dict[str, list[dict]],
                           all_synergies: list[dict], dissent: list[str],
                           dissent_reasons: dict[str, list[dict]]) -> str:
    blocks = []
    blocks.append("CONSENSUS PRIORITY ORDER (with each advisor's reasoning):")
    for i, slug in enumerate(consensus, start=1):
        blocks.append(f"{i}. {slug}")
        for take in reasons_detail.get(slug, []):
            blocks.append(f"     - {take['seat']}: {take['reason']}")
    if all_synergies:
        blocks.append("\nSYNERGIES THE ADVISORS NAMED (raw, may overlap or conflict):")
        for syn in all_synergies:
            blocks.append(f"     - {' + '.join(syn['projects'])}: {syn['insight']}  [{syn.get('seat', '?')}]")
    if dissent:
        blocks.append("\nCONTESTED PROJECTS (some advisors ranked, didn't reach consensus):")
        for slug in dissent:
            takes = dissent_reasons.get(slug, [])
            note = "; ".join(f"{t['seat']}: {t['reason']}" for t in takes) or "no reason given"
            blocks.append(f"     - {slug}: {note}")
    body = "\n".join(blocks)
    return (
        "Here is what four Council advisors produced when they independently prioritized the portfolio:\n\n"
        f"{body}\n\n"
        "Consolidate this into portfolio-level ideation. Respond in this exact JSON format:\n"
        "{\n"
        '  "synergies": [\n'
        '    {"projects": ["<slug>", "..."], "insight": "<the strongest cross-project connection, deduped and '
        'sharpened from the advisors\' notes>"}\n'
        "  ],\n"
        '  "opportunities": [\n'
        '    {"idea": "<a portfolio-level opportunity or move that only emerges from seeing everything at '
        'once>", "projects": ["<slug>", "..."]}\n'
        "  ],\n"
        '  "tensions": [\n'
        '    {"topic": "<where advisors disagreed or a real trade-off>", "detail": "<the substance of the '
        'disagreement and what it hinges on>"}\n'
        "  ],\n"
        '  "brief": "<3-5 sentences: the strategic story of this portfolio — how the priorities and synergies '
        'fit together into a plan>"\n'
        "}\n"
        "Deduplicate overlapping synergies. Prefer 2-5 sharp items per list over many weak ones. Ground every "
        "item in the advisors' input; do not invent projects not listed above."
    )


def run_ideation_pass(consensus: list[str], reasons_detail: dict[str, list[dict]],
                      all_synergies: list[dict], dissent: list[str],
                      dissent_reasons: dict[str, list[dict]], valid_slugs: set[str],
                      synth_seat: str, usage_total: dict) -> dict:
    """One consolidation call (reuse the engine synthesis idiom). Degrades gracefully: on parse/seat
    failure, still returns the raw deduped per-seat synergies so ideation is never wholly lost."""
    prompt = _build_ideation_prompt(consensus, reasons_detail, all_synergies, dissent, dissent_reasons)
    result = call_seat(synth_seat, IDEATION_SYSTEM, prompt, max_tokens=_IDEATION_TOKENS)
    _accumulate_usage(usage_total, result.get("usage", {}))
    parsed = parse_json(result.get("answer")) if result.get("status") == "success" else None
    if not parsed:
        # Fallback: surface the raw synergies the seats already produced.
        return {
            "synergies": _clean_synergies(all_synergies, valid_slugs),
            "opportunities": [],
            "tensions": [],
            "brief": "",
            "synthesized_by": None,
            "degraded": True,
        }
    return {
        "synergies": _clean_synergies(parsed.get("synergies"), valid_slugs),
        "opportunities": [
            {"idea": (o.get("idea") or "").strip(),
             "projects": [s for s in (o.get("projects") or []) if s in valid_slugs]}
            for o in (parsed.get("opportunities") or []) if isinstance(o, dict) and (o.get("idea") or "").strip()
        ],
        "tensions": [
            {"topic": (t.get("topic") or "").strip(), "detail": (t.get("detail") or "").strip()}
            for t in (parsed.get("tensions") or []) if isinstance(t, dict) and (t.get("topic") or "").strip()
        ],
        "brief": (parsed.get("brief") or "").strip(),
        "synthesized_by": synth_seat,
        "degraded": False,
    }


def run_prioritization(store, top_n: int = DEFAULT_TOP_N, seats: list[str] | None = None) -> dict[str, Any]:
    """Run the prioritization council. Returns the consensus shortlist + per-seat rankings + preserved
    reasons + dissent + the consolidated ideation block + usage. Does NOT create the approval card."""
    from .engine import ProviderNotConfigured

    context = build_portfolio_context(store)
    if not context:
        raise ValueError("No projects to prioritize. Run registry-refresh first.")
    valid_slugs = {c["slug"] for c in context}
    top_n = min(top_n, len(context))
    seat_ids = seats if seats is not None else configured_seats()
    if len(seat_ids) < 2:
        raise ProviderNotConfigured(f"Prioritization needs >=2 configured seats; found {seat_ids or 'none'}.")

    prompt = _build_prompt(context, top_n)
    usage_total = {"input": 0, "output": 0}

    def _rank(sid: str) -> dict:
        r = call_seat(sid, PRIORITIZE_SYSTEM, prompt, max_tokens=_RANK_TOKENS)
        _accumulate_usage(usage_total, r.get("usage", {}))
        parsed = parse_json(r.get("answer")) or {}
        ranking = [str(x.get("slug")) for x in (parsed.get("ranking") or []) if x.get("slug") in valid_slugs]
        synergies = _clean_synergies(parsed.get("synergies"), valid_slugs)
        for syn in synergies:
            syn["seat"] = sid
        return {"seat": sid, "status": r["status"], "error": r.get("error"), "ranking": ranking,
                "reasons": {str(x.get("slug")): x.get("reason") for x in (parsed.get("ranking") or [])
                            if x.get("slug") in valid_slugs and x.get("reason")},
                "synergies": synergies, "overall": parsed.get("overall")}

    seat_results = _parallel(seat_ids, _rank)
    per_seat = {s["seat"]: s["ranking"] for s in seat_results if s["ranking"]}
    # Which seats fell out, and why (API error vs unparsable/empty) — no longer a silent drop.
    dropped_seats = [{"seat": s["seat"],
                      "reason": s.get("error") or (f"{s['status']}: no usable ranking")}
                     for s in seat_results if not s["ranking"]]
    if len(per_seat) < 2:
        raise ProviderNotConfigured(
            f"Only {len(per_seat)} seat(s) returned a usable ranking; need >=2. "
            f"Dropped: {dropped_seats or 'none'}"
        )

    consensus = _borda(per_seat, top_n)
    # Dissent: slugs a minority of seats ranked but that didn't make the consensus.
    ranked_by: dict[str, int] = {}
    for ranking in per_seat.values():
        for slug in ranking:
            ranked_by[slug] = ranked_by.get(slug, 0) + 1
    dissent = sorted(
        [s for s in ranked_by if s not in consensus], key=lambda s: (-ranked_by[s], s)
    )[:5]

    reasons_detail = _collect_reasons(seat_results, consensus)
    dissent_reasons = _collect_reasons(seat_results, dissent)
    all_synergies = [syn for s in seat_results for syn in s.get("synergies", [])]

    ideation = run_ideation_pass(
        consensus, reasons_detail, all_synergies, dissent, dissent_reasons,
        valid_slugs, _pick_synth_seat(list(per_seat.keys())), usage_total,
    )

    result = {
        "mode": "prioritization",
        "top_n": top_n,
        "consensus": consensus,
        "per_seat_rankings": per_seat,
        "dissent": dissent,
        "reasons": _merge_reasons(seat_results, consensus),
        "reasons_detail": reasons_detail,
        "dissent_reasons": dissent_reasons,
        "ideation": ideation,
        "overall": next((s["overall"] for s in seat_results if s.get("overall")), ""),
        "seats": list(per_seat.keys()),
        "dropped_seats": dropped_seats,
        "usage": usage_total,
    }
    from .escalation import should_escalate
    result["escalation"] = should_escalate(result)
    return result
