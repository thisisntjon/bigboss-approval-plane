"""Council engine — M1a wires the deterministic FIXTURE mode; M1b adds LIVE mode:
fan-out independent answers -> peer critique -> vote -> cross-vendor verification swarm ->
per-model accuracy -> dissent-preserving synthesis. Stdlib only (urllib + ThreadPoolExecutor).

Cost note: a live run is ~40-50 small model calls across the four cheap-tier seats. Verify
calls return tiny JSON. Round-1 web search is deferred; factual claims use knowledge-fallback
verification, reasoning claims use independent re-derivation — the cross-vendor accuracy signal
(the throne's future track record) is produced either way.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import prompts
from .fixture import FixtureError, run_fixture_council
from .parse import parse_json
from .providers import call_seat, configured_seats
from .scoring import model_accuracy, tally_votes
from .security import classify_input_risk, redact_sensitive_text
from .verify import VERIFIER_ORDER, claim_author_id, is_reasoning_claim, pick_verifier

# Per-stage token budgets — keep verify/vote small (tiny JSON), answers/synthesis larger.
_ANSWER_TOKENS = 800
_CRITIQUE_TOKENS = 600
_VOTE_TOKENS = 200
_EXTRACT_TOKENS = 700
_VERIFY_TOKENS = 250
_SYNTH_TOKENS = 900
_MAX_CLAIMS_PER_MODEL = 6  # cap the verification fan-out (cost)
_MAX_WORKERS = 6


def deliberate(question: str | None = None, *, mode: str = "fixture", scenario: str | None = None,
               fixture_path: Any = None, seats: list[str] | None = None) -> dict:
    """Run one council deliberation. mode='fixture' (offline, deterministic) or 'live' (real models)."""
    if mode == "fixture":
        return run_fixture_council(question=question, scenario=scenario, fixture_path=fixture_path)
    if mode == "live":
        if not question:
            raise ValueError("Live council mode requires a question.")
        return run_live_council(question, seats=seats)
    raise ValueError(f"Unknown council mode: {mode!r}")


def _parallel(items: list, fn) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def _accumulate_usage(total: dict, usage: dict) -> None:
    total["input"] += int(usage.get("input") or 0)
    total["output"] += int(usage.get("output") or 0)


def run_live_council(question: str, seats: list[str] | None = None, context: str | None = None) -> dict:
    seat_ids = seats if seats is not None else configured_seats()
    if len(seat_ids) < 2:
        raise ProviderNotConfigured(
            f"Live council needs >=2 configured seats; found {seat_ids or 'none'}. "
            "Set the vendor API keys."
        )
    redacted_q = redact_sensitive_text(question)
    input_risk = classify_input_risk(question)
    usage_total = {"input": 0, "output": 0}
    # M6.1b — optional Big Boss self/portfolio context, so the Council isn't ecosystem-blind
    # (without it, seats treat "Squire" as a medieval attendant). Injected only into the
    # answer-generating rounds (round 1 + synthesis); critique/vote/verify operate on the seats'
    # own answers, so re-injecting there would just burn tokens.
    ctx_block = (
        "\n\nEcosystem context (Big Boss self-knowledge — ground your answer in it; do not invent "
        f"projects):\n{context.strip()}"
        if context and context.strip() else ""
    )

    # Round 1 — independent answers (parallel fan-out).
    answers = _parallel(
        seat_ids,
        lambda sid: call_seat(
            sid,
            prompts.build_council_system_prompt(sid, base_system=prompts.BASE_ROUND1_SYSTEM + ctx_block),
            question,
            max_tokens=_ANSWER_TOKENS,
        ),
    )
    for a in answers:
        _accumulate_usage(usage_total, a.get("usage", {}))
    ok_answers = [a for a in answers if a["status"] == "success" and (a.get("answer") or "").strip()]
    if len(ok_answers) < 2:
        raise ProviderNotConfigured(
            f"Only {len(ok_answers)} seat(s) returned an answer; need >=2. "
            f"Errors: {[(a['id'], a.get('error')) for a in answers if a['status'] != 'success']}"
        )
    answered_ids = [a["id"] for a in ok_answers]

    # Round 2 — peer critique (each rates the others).
    def _critique(sid: str) -> dict:
        r = call_seat(
            sid,
            prompts.EVALUATE_SYSTEM,
            prompts.build_evaluate_prompt(question, ok_answers, sid, _name(sid, ok_answers)),
            max_tokens=_CRITIQUE_TOKENS,
        )
        _accumulate_usage(usage_total, r.get("usage", {}))
        parsed = parse_json(r.get("answer")) or {}
        return {"evaluator": sid, "ratings": parsed.get("ratings", []),
                "reflection": parsed.get("reflection"), "would_change": bool(parsed.get("would_change"))}

    critiques = _parallel(answered_ids, _critique)

    # Round 2.5 — vote (each votes for the best; cannot vote self; first-in-tie winner).
    eval_summary = _eval_summary(critiques)

    def _vote(sid: str) -> dict:
        r = call_seat(
            sid, prompts.VOTE_SYSTEM,
            prompts.build_vote_prompt(question, ok_answers, eval_summary, sid, _name(sid, ok_answers)),
            max_tokens=_VOTE_TOKENS,
        )
        _accumulate_usage(usage_total, r.get("usage", {}))
        parsed = parse_json(r.get("answer")) or {}
        winner = parsed.get("winner")
        others = [i for i in answered_ids if i != sid]
        if winner not in others:  # self-vote / invalid guard
            winner = others[0] if others else None
        return {"voter": sid, "winner": winner, "justification": parsed.get("justification")}

    votes = _parallel(answered_ids, _vote)
    vote_result = tally_votes(votes, answered_ids)

    # Round 3 — verification swarm: extract claims per model, then a DIFFERENT vendor verifies each.
    def _extract(a: dict) -> list[dict]:
        r = call_seat(a["id"], prompts.EXTRACT_SYSTEM,
                      prompts.build_extract_prompt(a["id"], a["name"], a["answer"]), max_tokens=_EXTRACT_TOKENS)
        _accumulate_usage(usage_total, r.get("usage", {}))
        parsed = parse_json(r.get("answer")) or {}
        claims = [c for c in (parsed.get("claims") or []) if c.get("text")][:_MAX_CLAIMS_PER_MODEL]
        for i, c in enumerate(claims):
            c.setdefault("id", f"{a['id']}-{i + 1}")
        return claims

    all_claims: list[dict] = [c for claims in _parallel(ok_answers, _extract) for c in claims]

    verifier_pool = [s for s in VERIFIER_ORDER if s in answered_ids] or answered_ids

    def _verify(indexed: tuple[int, dict]) -> dict:
        idx, claim = indexed
        author = claim_author_id(claim)
        verifier, _ = pick_verifier(author, verifier_pool, idx)  # deterministic rotation by index
        if is_reasoning_claim(claim):
            system, prompt = prompts.REASON_SYSTEM, prompts.build_reason_prompt(claim["text"])
        else:
            system, prompt = prompts.KNOWLEDGE_SYSTEM, prompts.build_knowledge_prompt(claim["text"])
        r = call_seat(verifier or verifier_pool[0], system, prompt, max_tokens=_VERIFY_TOKENS)
        _accumulate_usage(usage_total, r.get("usage", {}))
        parsed = parse_json(r.get("answer")) or {}
        verdict = parsed.get("verdict")
        if verdict not in ("supported", "partially_supported", "refuted", "unverifiable"):
            verdict = "unverifiable"
        conf = parsed.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.3
        return {**claim, "verdict": verdict, "confidence": conf, "verified_by": verifier,
                "reasoning": parsed.get("reasoning")}

    verified_claims = _parallel(list(enumerate(all_claims)), _verify)

    # Per-model accuracy (the throne's track-record signal): each author's verified-claim rate.
    per_model_scores: dict[str, dict] = {}
    for sid in answered_ids:
        own = [c for c in verified_claims if claim_author_id(c) == sid]
        per_model_scores[sid] = model_accuracy(own)

    # Synthesis — the vote winner writes the final answer from verified (non-refuted) claims.
    supported = [c for c in verified_claims if c["verdict"] in ("supported", "partially_supported")]
    refuted = [c for c in verified_claims if c["verdict"] == "refuted"]
    verified_block = "\n".join(f"- {c['text']}" for c in supported)
    refuted_block = "\n".join(f"- {c['text']} (Reason: {c.get('reasoning') or 'n/a'})" for c in refuted)
    synth_seat = vote_result["winner"] or answered_ids[0]
    synth = call_seat(synth_seat, prompts.SYNTHESIS_SYSTEM + ctx_block,
                      prompts.build_synthesis_prompt(question, verified_block, refuted_block),
                      max_tokens=_SYNTH_TOKENS)
    _accumulate_usage(usage_total, synth.get("usage", {}))

    report = {
        "project": "The Council: Multi-Agent Verification Swarm",
        "mode": "live",
        "simulated": False,
        "question": redacted_q,
        "input_risk": input_risk,
        "seats": answered_ids,
        "agents": [{"id": a["id"], "name": a["name"], "provider": a["provider"], "model": a["model"],
                    "answer": a["answer"]} for a in ok_answers],
        "errored_seats": [{"id": a["id"], "error": a.get("error")} for a in answers if a["status"] != "success"],
        "critiques": critiques,
        "votes": votes,
        "winner": vote_result["winner"],
        "vote_tie": vote_result["tie"],
        "unanimous": vote_result.get("unanimous", False),  # the cheap "definitely don't escalate" fast-path
        "verified_claims": verified_claims,
        "per_model_scores": per_model_scores,
        "final": {"final_answer": synth.get("answer") or "", "synthesized_by": synth_seat},
        "usage": usage_total,
    }
    # M5a — the Fable tie-break signal. This only DECIDES (pure); it never spends. The caller
    # (CLI/approval flow) raises the human-gated card or runs the opt-in auto path.
    from .escalation import should_escalate
    report["escalation"] = should_escalate(report)
    return report


class ProviderNotConfigured(RuntimeError):
    pass


def _name(seat_id: str, answers: list[dict]) -> str:
    for a in answers:
        if a["id"] == seat_id:
            return a["name"]
    return seat_id


def _eval_summary(critiques: list[dict]) -> str:
    lines = []
    for c in critiques:
        for r in c.get("ratings", []):
            lines.append(f"  - {r.get('model_id')}: {r.get('score')}/100 — {r.get('reasoning')}")
    return "\n".join(lines)


__all__ = ["deliberate", "run_live_council", "FixtureError", "ProviderNotConfigured"]
