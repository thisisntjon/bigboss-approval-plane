"""BigBoss project registry (Ecosystem Phase E3): an auto-derived, read-only
Portfolio View over the operator's projects.

v1 harvests cheap signals (fs markers, git, transcript/handoff slugs, daemons),
reconciles them to canonical projects (dedup by canonical-path rule), and surfaces
heat / staleness / where-I-left-off + manual pin on the phone. Synergy detectors,
opportunities, embeddings, and hook/nightly automation are intentionally deferred.
"""

from __future__ import annotations

from .canonical import Candidate, ResolvedProject, family_key, normalize, reconcile_candidates
from .harvest import refresh  # entry point; the `harvest` submodule stays accessible unshadowed

__all__ = [
    "Candidate", "ResolvedProject", "family_key", "normalize", "reconcile_candidates",
    "refresh",
]
