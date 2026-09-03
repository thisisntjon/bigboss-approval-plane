"""The Council — BigBoss's multi-model deliberation engine (Ecosystem Phase M1).

A stdlib-only port of `the-council-capstone`: independent answers -> peer critique ->
cross-vendor verification -> dissent-preserving synthesis, plus a deterministic offline
fixture mode. M1a ships the deterministic core (this package's pure logic + fixture
engine); M1b adds live `urllib` model callers, persistence, and the `council_ask` surface.
No third-party dependencies, by design.
"""
