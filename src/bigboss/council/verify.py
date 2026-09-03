"""Claim classification + cross-vendor verifier selection — faithful port of
the-council-capstone `lib/claims.mjs` + the live server's `pickVerifier` round-robin.

Pure logic (no I/O): the actual verification model calls live in the M1b engine; these
functions decide HOW a claim is routed (reasoning re-derivation vs evidence) and WHO
verifies it (an independent vendor, never the author).
"""

from __future__ import annotations

# Claim categories verified by re-derivation, not evidence lookup — a deduction's
# correctness is logical validity, not external corroboration.
REASONING_CATEGORIES = frozenset({"computation", "logical_deduction", "derivation"})

# The council's standing vendor order (also the verifier rotation order).
VERIFIER_ORDER = ("claude", "gpt", "gemini", "grok")


def is_reasoning_claim(claim: dict | None) -> bool:
    """True when a claim's correctness is logical/mathematical, decided by its `category`."""
    return bool(claim) and claim.get("category") in REASONING_CATEGORIES


def claim_author_id(claim: dict | None) -> str | None:
    """The model that authored a claim, from its id ("gpt-3" -> "gpt"). Used to pick an
    INDEPENDENT verifier so verification isn't self-grading."""
    if not claim:
        return None
    cid = claim.get("id")
    if not isinstance(cid, str):
        return None
    dash = cid.find("-")
    return cid[:dash] if dash > 0 else None


def pick_verifier(exclude_id: str | None, available: list[str], cursor: int) -> tuple[str | None, int]:
    """Round-robin over `available` vendors, excluding the claim's author. Returns
    (verifier_id, next_cursor). Falls back to the full pool if excluding the author would
    empty it. Mirrors server `pickVerifier` (cursor increments on every pick)."""
    pool = [v for v in available if v != exclude_id]
    if not pool:
        pool = list(available)
    if not pool:
        return (None, cursor)
    chosen = pool[cursor % len(pool)]
    return (chosen, cursor + 1)
