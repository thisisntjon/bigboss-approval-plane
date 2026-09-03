"""Deterministic offline replay of the Council pipeline — faithful port of
the-council-capstone `lib/fixtureCouncil.mjs`. Replays the exact pipeline shape (redact ->
independent answers -> peer critique -> claim verification -> weighted decision scoring ->
synthesis -> audit export) from pre-authored fixture JSON, with no keys and no network.
Every report is labeled `simulated: True` so fixture output can never pass as live.

Determinism contract: no randomness. The same question always produces the same report
(modulo the `generated_at` timestamp), so reports are diffable and tests assert exact values.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scoring import compute_decision_scores
from .security import TOOL_ALLOWLIST, classify_input_risk, redact_sensitive_text
from .verify import is_reasoning_claim

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_FIXTURE = FIXTURES_DIR / "council_fixture.json"
DEFAULT_ALTERNATE_QUESTIONS = (
    "Should a small team use a single powerful AI agent or a council of specialized agents "
    "for high-stakes research?",
)

# Scenario registry — only the fixtures actually shipped in BigBoss. Others from the
# capstone (car_wash_price_check, car_already_at_wash, ...) can be copied in later.
SCENARIO_FIXTURES = (
    {
        "scenario": "car_wash_50m",
        "fixture_path": FIXTURES_DIR / "car_wash_fixture.json",
        "question": "I want to wash my car. The car wash is 50 meters away. Should I walk or drive?",
    },
)

# Verbatim default synthesis when a fixture pins no finalAnswer (council_fixture.json).
_DEFAULT_FINAL_ANSWER = " ".join([
    "A small team should start with a single strong agent when speed, simplicity, and low "
    "coordination cost matter.",
    "For high-stakes research or AI-assisted decisions, The Council pattern is stronger: "
    "independent agents answer first, peer critique exposes weak assumptions, a verification "
    "swarm checks claims against evidence, and final synthesis preserves unresolved claims "
    "instead of hiding them.",
    "The practical recommendation is progressive escalation: use one agent for low-risk work, "
    "then trigger a council when the decision needs traceability, disagreement analysis, or an "
    "audit trail.",
])


class FixtureError(ValueError):
    """No offline fixture matches the request."""


def load_fixture(fixture_path: Path | str = DEFAULT_FIXTURE) -> dict:
    return json.loads(Path(fixture_path).read_text(encoding="utf-8"))


def normalize_question(question: object) -> str:
    import re

    s = str(question if question is not None else "").lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def select_fixture_path(
    scenario: str | None = None, question: str | None = None, fixture_path: Path | str | None = None
) -> Path:
    """Route a question to its fixture by EXACT normalized match only. Fuzzy matching is
    deliberately rejected. Unknown questions raise (point the user at live mode)."""
    if fixture_path:
        return Path(fixture_path)
    if scenario:
        for item in SCENARIO_FIXTURES:
            if item["scenario"] == scenario:
                return item["fixture_path"]
    nq = normalize_question(question)
    if not nq:
        return DEFAULT_FIXTURE
    if nq == normalize_question(load_fixture(DEFAULT_FIXTURE)["question"]):
        return DEFAULT_FIXTURE
    if any(nq == normalize_question(q) for q in DEFAULT_ALTERNATE_QUESTIONS):
        return DEFAULT_FIXTURE
    for item in SCENARIO_FIXTURES:
        if nq == normalize_question(item["question"]):
            return item["fixture_path"]
    raise FixtureError(
        "No offline fixture is available for that question. Choose a listed fixture scenario, "
        "or use live council mode for arbitrary questions."
    )


def _summarize_disagreements(peer_reviews: list[dict]) -> list[dict]:
    return [
        {
            "reviewer": r.get("reviewer"),
            "target": r.get("target"),
            "score": r.get("score"),
            "strength": r.get("strength"),
            "weakness": r.get("weakness"),
            "would_revise": bool(r.get("wouldRevise")),
        }
        for r in peer_reviews
    ]


def verify_claims(fixture: dict) -> list[dict]:
    evidence_by_id = {item["id"]: item for item in fixture.get("evidence", [])}
    verification = fixture.get("verification", {})
    out: list[dict] = []
    for claim_id, text in fixture["claims"].items():
        check = verification.get(claim_id) or {
            "verdict": "unresolved",
            "confidence": 0.25,
            "evidence": [],
            "reasoning": "No fixture evidence was mapped to this claim.",
        }
        claim = {
            "id": claim_id,
            "text": text,
            "verdict": check.get("verdict"),
            "confidence": check.get("confidence"),
            "evidence": [evidence_by_id[e] for e in check.get("evidence", []) if e in evidence_by_id],
            "reasoning": check.get("reasoning"),
            "roleId": check.get("roleId"),
            "claimType": check.get("claimType"),
            "verification_method": (
                "independent_re_derivation" if is_reasoning_claim(check) else "evidence_check"
            ),
            "roleWeight": check.get("roleWeight"),
            "claimWeight": check.get("claimWeight"),
            "supports": check.get("supports"),
            "appliesWhen": check.get("appliesWhen"),
        }
        if check.get("category"):
            claim["category"] = check["category"]
        out.append(claim)
    return out


def build_synthesis(verified_claims: list[dict], fixture: dict) -> dict:
    supported = [c for c in verified_claims if c["verdict"] == "supported"]
    partial = [c for c in verified_claims if c["verdict"] == "partially_supported"]
    unresolved = [c for c in verified_claims if c["verdict"] not in ("supported", "partially_supported")]
    avg = sum(c["confidence"] for c in verified_claims) / len(verified_claims) if verified_claims else 0.0
    fx = fixture.get("synthesis") or {}
    decision_scores = fx.get("decisionScores") or compute_decision_scores(verified_claims)

    out: dict[str, Any] = {
        "final_answer": fx.get("finalAnswer") or _DEFAULT_FINAL_ANSWER,
        "confidence_summary": {
            "average_confidence": round(avg, 2),
            "supported_claims": len(supported),
            "partially_supported_claims": len(partial),
            "unresolved_claims": len(unresolved),
        },
        "unresolved_claims": unresolved,
    }
    if decision_scores:
        out["decision_scores"] = decision_scores
    if fixture.get("scenarioInterpretation"):
        out["scenario_interpretation"] = fixture["scenarioInterpretation"]
    if fx.get("assumptionsAndCaveats"):
        out["assumptions_and_caveats"] = fx["assumptionsAndCaveats"]
    return out


def run_fixture_council(
    question: str | None = None, scenario: str | None = None, fixture_path: Path | str | None = None
) -> dict:
    """Run the whole offline pipeline; return the full report (mirrors the live report shape)."""
    path = select_fixture_path(scenario=scenario, question=question, fixture_path=fixture_path)
    fixture = load_fixture(path)
    raw_question = question or fixture["question"]
    redacted = redact_sensitive_text(raw_question)
    input_risk = classify_input_risk(raw_question)
    verified_claims = verify_claims(fixture)
    synthesis = build_synthesis(verified_claims, fixture)
    re_derived = sum(1 for c in verified_claims if c["verification_method"] == "independent_re_derivation")

    return {
        "project": "The Council: Multi-Agent Verification Swarm",
        "mode": "fixture",
        "simulated": True,
        "fixture": path.name,
        "scenario_id": fixture.get("scenarioId"),
        "label": fixture.get("label"),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "question": redacted,
        "input_risk": input_risk,
        "tool_allowlist": list(TOOL_ALLOWLIST),
        "stages": [
            "independent_council_answers",
            "peer_critique",
            "hidden_verification_swarm",
            "evidence_backed_synthesis",
            "audit_export",
        ],
        "agents": [
            {
                "id": a["id"],
                "name": a["name"],
                "role": a.get("role"),
                "role_weight": a.get("roleWeight"),
                "answer": a["answer"],
                "claim_ids": a.get("claims", []),
            }
            for a in fixture["agents"]
        ],
        "swarm_roles": fixture.get("swarmRoles", []),
        "scenario_interpretation": fixture.get("scenarioInterpretation"),
        "peer_reviews": _summarize_disagreements(fixture.get("peerReviews", [])),
        "verified_claims": verified_claims,
        "final": synthesis,
        "audit_trail": [
            {"step": "load_fixture", "detail": f"Loaded public fixture data from {path.name}."},
            {"step": "redact_input", "detail": f"Input risk level: {input_risk['level']}."},
            {"step": "extract_claims", "detail": f"Loaded {len(verified_claims)} fixture claims."},
            {
                "step": "verify_claims_against_fixture",
                "detail": (
                    f"Checked {len(verified_claims) - re_derived} claims against local fixture "
                    f"evidence; routed {re_derived} reasoning claims to independent re-derivation."
                    if re_derived
                    else "Checked each claim against local fixture evidence."
                ),
            },
            {"step": "write_audit_report", "detail": "Report can be exported as JSON and Markdown."},
        ],
    }


def report_to_markdown(report: dict) -> str:
    lines = ["# The Council Fixture Report", "", f"Mode: {report['mode']} (simulated/offline)",
             f"Generated: {report['generated_at']}", "", "## Question", "", report["question"], ""]
    si = report.get("scenario_interpretation")
    if si:
        lines += ["## Scenario Interpretation", "", f"Likely intent: {si.get('likelyIntent')}",
                  f"Primary recommendation: {si.get('primaryRecommendation')}",
                  f"Ambiguity: {si.get('ambiguity')}", ""]
    lines.append("## Council Agents")
    for a in report["agents"]:
        w = f" (weight: {a['role_weight']})" if a.get("role_weight") else ""
        lines += [f"### {a['name']}", "", f"Role: {a.get('role')}{w}", "", a["answer"], ""]
    lines.append("## Peer Review")
    for r in report["peer_reviews"]:
        lines.append(
            f"- {r['reviewer']} -> {r['target']}: {r['score']}/100. "
            f"Strength: {r['strength']} Weakness: {r['weakness']}"
        )
    lines += ["", "## Verified Claims"]
    for c in report["verified_claims"]:
        method = " [re-derived independently]" if c["verification_method"] == "independent_re_derivation" else ""
        lines.append(f"- {c['id']}: {c['verdict']} ({round(c['confidence'] * 100)}%){method} - {c['text']}")
    lines.append("")
    if report["final"].get("decision_scores"):
        lines.append("## Decision Scores")
        for option, score in report["final"]["decision_scores"].items():
            lines.append(f"- {option}: {score}")
        lines.append("")
    lines += ["## Final Synthesis", "", report["final"]["final_answer"], ""]
    caveats = report["final"].get("assumptions_and_caveats")
    if caveats:
        lines.append("## Assumptions And Caveats")
        lines += [f"- {c}" for c in caveats] + [""]
    lines.append("## Audit Trail")
    lines += [f"- {i['step']}: {i['detail']}" for i in report["audit_trail"]] + [""]
    return "\n".join(lines)
