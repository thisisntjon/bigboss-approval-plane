"""Deterministic scoring — faithful port of the-council-capstone's decision scoring
(`lib/fixtureCouncil.mjs`), per-model accuracy (`computeConfidenceScores`), and the
vote tally (`server/index.js`). Pure functions; the same math offline and live.

`model_accuracy` is the signal that becomes the throne's election track record (M4):
each model's verified-claim rate, accumulated across sessions.
"""

from __future__ import annotations

import math
from typing import Any


def _finite(value: Any, default: float) -> float:
    """JS Number.isFinite(x) ? x : default — reject None/NaN/Inf/non-numbers."""
    if isinstance(value, bool):  # bool is an int subclass in Python; treat as non-numeric
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return default


def verdict_multiplier(verdict: str) -> float:
    """Verdict polarity for decision scoring. Refuted counts AGAINST (-1), not merely
    zero; unresolved/unknown contribute 0."""
    if verdict == "supported":
        return 1.0
    if verdict == "partially_supported":
        return 0.5
    if verdict == "refuted":
        return -1.0
    return 0.0


def compute_decision_scores(verified_claims: list[dict]) -> dict[str, float]:
    """Weighted decision scoring: each claim contributes
    verdict polarity x confidence x roleWeight x claimWeight x effect to each option in
    its `supports` map. Missing weights default to 1. Rounded to 2 decimals."""
    scores: dict[str, float] = {}
    for claim in verified_claims:
        supports = claim.get("supports") or {}
        multiplier = verdict_multiplier(claim.get("verdict"))
        confidence = _finite(claim.get("confidence"), 0.0)
        role_weight = _finite(claim.get("roleWeight"), 1.0)
        claim_weight = _finite(claim.get("claimWeight"), 1.0)
        for option, effect in supports.items():
            numeric_effect = _finite(effect, math.nan)
            if math.isnan(numeric_effect):
                continue
            scores[option] = scores.get(option, 0.0) + (
                multiplier * confidence * role_weight * claim_weight * numeric_effect
            )
    # toFixed(2)-style: for the fixtures' value range round-half-to-even and JS
    # round-half-up agree (no exact .xx5 ties at the 2nd decimal).
    return {option: round(score, 2) for option, score in scores.items()}


def model_accuracy(claims: list[dict]) -> dict[str, Any]:
    """Per-model verified-claim accuracy over a model's claims (port of
    computeConfidenceScores). score = round(((verified + partial*0.5)/total)*1000)/10 %.
    A claim with `verifiable` False is excluded from the total."""
    total = sum(1 for c in claims if c.get("verifiable") is not False)
    verified = sum(1 for c in claims if c.get("verdict") == "supported")
    partial = sum(1 for c in claims if c.get("verdict") == "partially_supported")
    refuted = sum(1 for c in claims if c.get("verdict") == "refuted")
    unverifiable = sum(
        1 for c in claims if (not c.get("verdict")) or c.get("verdict") == "unverifiable"
    )
    score = round(((verified + partial * 0.5) / total) * 1000) / 10 if total > 0 else 0.0
    return {
        "total": total,
        "verified": verified,
        "partial": partial,
        "refuted": refuted,
        "unverifiable": unverifiable,
        "score": score,
    }


def tally_votes(votes: list[dict], answer_ids: list[str]) -> dict[str, Any]:
    """Tally peer votes into a winner. `votes` = [{voter, winner}]. First-in-tie winner.
    Port of server vote-tally (:1353)."""
    tally: dict[str, dict[str, Any]] = {aid: {"votes": 0, "voters": []} for aid in answer_ids}
    for vote in votes:
        winner = vote.get("winner")
        if winner in tally:
            tally[winner]["votes"] += 1
            tally[winner]["voters"].append(vote.get("voter"))
    max_votes = max((t["votes"] for t in tally.values()), default=0)
    winners = [aid for aid in answer_ids if tally[aid]["votes"] == max_votes]
    winner = winners[0] if winners else None
    voted_ids = [aid for aid in answer_ids if tally[aid]["votes"] > 0]
    return {
        "tally": tally,
        "winner": winner,
        "tie": len(winners) > 1,
        "unanimous": len(voted_ids) == 1 and max_votes == len(answer_ids),
    }
