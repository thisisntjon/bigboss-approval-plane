"""M5a — Fable tie-break escalation. The cheap 4-seat council deliberates; Fable (the expensive
throne) is brought in ONLY when the council is genuinely unsure — and NEVER auto-spends: an
escalation raises a human-gated `fable_escalation` approval card (locked design). Efficient Fable use is the core BigBoss motivator.

Two research-backed design choices:
- Trigger on cross-model DISAGREEMENT + VERIFICATION FAILURE, never on self-reported confidence
  (unreliable). Agreement alone is insufficient — correlated cheap models can be confidently wrong.
- Fable re-answers BLIND (never shown the cheap answers → no anchoring / position / self-preference
  bias), then we reconcile + log for human review. This also overrides the silently-arbitrary
  first-in-tie winner the vote produces today.

`should_escalate(report)` is a PURE function over the deliberation/prioritization report dict, so it
is unit-testable with zero spend. Thresholds are conservative starting points (aim ~10-15% escalation)
and are the calibration targets the eval harness tunes.
"""

from __future__ import annotations

import os
from statistics import mean
from typing import Any

from .providers import call_anthropic_model
from .research import research_model  # reuses COUNCIL_RESEARCH_MODEL / claude-fable-5

# Calibration targets (see council-eval): tune these against a labeled set, don't trust intuition.
MIN_MEAN_CONFIDENCE = 0.5    # mean verified-claim confidence below this = council unsure
MAX_REFUTED_FRACTION = 0.34  # >a third of extracted claims refuted = real conflict
_TIEBREAK_TOKENS = 1500

TIEBREAK_SYSTEM = (
    "You are Fable, the throne of The Council — the most capable model, brought in to resolve a question "
    "the cheaper council could not settle. Answer INDEPENDENTLY and from scratch: give your own best answer, "
    "reasoned carefully. You are deliberately NOT shown the council's answers or votes, to avoid anchoring on "
    "a possibly-wrong majority. Be decisive but honest about genuine uncertainty."
)


def should_escalate(report: dict[str, Any]) -> dict[str, Any]:
    """Decide whether the council is unsure enough to warrant a Fable tie-break.
    Returns {escalate: bool, trigger: str, reason: str}. Pure — no I/O."""
    if (report.get("mode") or "") == "prioritization":
        return _escalate_prioritization(report)
    return _escalate_deliberation(report)


def _escalate_deliberation(report: dict[str, Any]) -> dict[str, Any]:
    claims = report.get("verified_claims") or []
    refuted = [c for c in claims if c.get("verdict") == "refuted"]
    confs = [float(c.get("confidence") or 0) for c in claims]
    mean_conf = mean(confs) if confs else 1.0
    seats = report.get("seats") or []
    errored = report.get("errored_seats") or []
    unanimous = bool(report.get("unanimous"))

    # Fast-path: a clean unanimous vote with confident verification never escalates.
    if unanimous and mean_conf >= MIN_MEAN_CONFIDENCE and not refuted:
        return _no("unanimous + confident verification")

    if (report.get("input_risk") or {}).get("level") == "high":
        return _yes("high_risk", "input flagged high-risk/strategic — warrants the throne")
    if report.get("vote_tie"):
        return _yes("vote_tie", "the seats tied — the winner is otherwise an arbitrary first-in-tie pick")
    if claims and (len(refuted) / len(claims)) > MAX_REFUTED_FRACTION:
        return _yes("verification_conflict",
                    f"{len(refuted)}/{len(claims)} synthesis claims were refuted by cross-vendor verification")
    if confs and mean_conf < MIN_MEAN_CONFIDENCE:
        return _yes("low_confidence", f"mean verified-claim confidence {mean_conf:.2f} < {MIN_MEAN_CONFIDENCE}")
    # Half or more of the configured seats fell out — thin, unreliable deliberation.
    if seats and len(errored) >= max(1, (len(seats) + len(errored)) // 2):
        return _yes("seat_shrinkage", f"{len(errored)} seat(s) errored; deliberation too thin to trust")
    return _no("council reached a confident, verified consensus")


def _escalate_prioritization(report: dict[str, Any]) -> dict[str, Any]:
    dissent = report.get("dissent") or []
    dropped = report.get("dropped_seats") or []
    degraded = bool((report.get("ideation") or {}).get("degraded"))
    if len(dropped) >= 2:
        return _yes("seat_shrinkage", f"{len(dropped)} seat(s) returned no usable ranking")
    if len(dissent) >= 3:
        return _yes("high_dissent", f"{len(dissent)} contested projects the seats couldn't reconcile")
    if degraded:
        return _yes("ideation_degraded", "the ideation consolidation pass failed")
    return _no("consensus ranking with limited dissent")


def _yes(trigger: str, reason: str) -> dict[str, Any]:
    return {"escalate": True, "trigger": trigger, "reason": reason}


def _no(reason: str) -> dict[str, Any]:
    return {"escalate": False, "trigger": None, "reason": reason}


def _fable_cost_usd(usage: dict[str, Any], model: str) -> str:
    """Exact Fable pricing via the router ledger's rate table (cost.py is blended + underprices Fable)."""
    try:
        from ..router.pricing import cost_breakdown
        br = cost_breakdown(model, {"input_tokens": usage.get("input") or 0,
                                    "output_tokens": usage.get("output") or 0})
        return str(br["cost_usd"])
    except Exception:  # pricing is best-effort accounting, never fatal
        return "0"


def estimate_tiebreak_cost_usd(report: dict[str, Any], model: str | None = None) -> str:
    """Rough pre-approval cost estimate for the card: Fable re-reads the question + emits a brief answer."""
    mdl = model or research_model()
    q_tokens = len((report.get("question") or "")) // 4
    return _fable_cost_usd({"input": q_tokens + 400, "output": _TIEBREAK_TOKENS}, mdl)


def _build_tiebreak_prompt(report: dict[str, Any]) -> str:
    question = report.get("question") or ""
    claims = report.get("verified_claims") or []
    # Include the CONTESTED points (refuted/low-confidence claim TEXT only) so Fable focuses — but
    # never who said what or the vote counts, which would anchor it (research: judge-mode bias).
    contested = [c.get("text", "") for c in claims
                 if c.get("verdict") == "refuted" or float(c.get("confidence") or 1) < MIN_MEAN_CONFIDENCE]
    body = f"Question:\n{question}\n"
    if contested:
        body += ("\nSome sub-points were contested / uncertain during analysis (resolve them in your answer, "
                 "no need to reference this list):\n" + "\n".join(f"- {t}" for t in contested[:8]) + "\n")
    body += "\nGive your own independent, decisive best answer."
    return body


def run_tiebreak(report: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """Invoke Fable BLIND (question only, never the cheap answers) to resolve. Returns the verdict +
    exact cost. Only called AFTER a human approves the escalation card (or the opt-in auto path)."""
    mdl = model or research_model()
    result = call_anthropic_model(mdl, TIEBREAK_SYSTEM, _build_tiebreak_prompt(report), max_tokens=_TIEBREAK_TOKENS)
    if result["status"] != "success":
        return {"status": "error", "error": result.get("error"), "model": mdl,
                "usage": result.get("usage", {}), "cost_usd": "0"}
    usage = result.get("usage", {})
    # We cannot cheaply cluster answers semantically here, so we always surface the tie-break for
    # human review (the card shows both council + Fable). standalone flagging is a follow-on.
    return {"status": "success", "model": result.get("model") or mdl, "answer": result.get("answer") or "",
            "usage": usage, "cost_usd": _fable_cost_usd(usage, result.get("model") or mdl),
            "standalone": None, "reviewed": False}


def autotiebreak_enabled() -> bool:
    """Opt-in: skip the approval card and run Fable inline. Off by default — Fable never auto-spends."""
    return os.environ.get("COUNCIL_AUTOTIEBREAK", "").strip() in ("1", "true", "yes")
