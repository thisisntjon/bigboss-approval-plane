"""Pure canonical-path / dedup logic for the project registry (no DB, no I/O).

The hard problem in a portfolio registry is *identity*, not storage: the same
project appears under 3-5 paths (webshop x4, legacyproj x5, thecouncil x4) plus
transcript/note/handoff slugs. This module resolves a flat list of discovered
candidates into canonical projects + aliases, deterministically and testably.

Dedup rules:
- A candidate nested strictly under another candidate is an embedded copy.
- Candidates are grouped by a normalized family key (handles the-council-capstone
  vs thecouncil, legacyproj vs legacyproj_files) so same-project copies collapse.
- Canonical within a family: own git-with-commits wins, then marker/activity, then
  not-nested, then shortest path. Losers become aliases (embedded/archive/fs-path).
- A dir that merely *contains* project roots and has no commits of its own is
  'ambiguous' (e.g. the projects-parent dir), not a real project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


STRONG_MARKERS = (".git", "pyproject.toml", "package.json", ".mcp.json")
WEAK_MARKERS = ("CLAUDE.md", "README.md")

# Trailing tokens stripped when computing a family key so copies group together.
_FAMILY_SUFFIXES = ("capstone", "files", "superseded", "documentation", "archive")
_ARCHIVE_HINTS = ("dump 2026", "/archive/", "\\archive\\", "_superseded", "_old", "_backup")


def normalize(path: str) -> str:
    """Canonical string form for storage/compare: forward slashes, no trailing sep.
    Casing is preserved; use ckey() for case-insensitive comparison."""
    p = str(path).replace("\\", "/").rstrip("/")
    return p


def ckey(path: str) -> str:
    return normalize(path).casefold()


def basename(path: str) -> str:
    return normalize(path).rsplit("/", 1)[-1]


def family_key(name: str) -> str:
    """Normalized project-family key: lowercased, non-alphanumerics removed, and
    known trailing tokens stripped so 'the-council-capstone' == 'thecouncil' and
    'legacyproj_files' == 'legacyproj'."""
    stem = re.sub(r"[^a-z0-9]", "", name.lower())
    changed = True
    while changed:
        changed = False
        for suffix in _FAMILY_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)]
                changed = True
    return stem or name.lower()


def is_archive_path(path: str) -> bool:
    low = normalize(path).lower()
    return any(hint in low for hint in _ARCHIVE_HINTS)


def is_project_root(markers: dict[str, bool], has_slug_ref: bool) -> bool:
    """>=1 strong marker, OR >=1 weak marker together with an external slug reference
    (a transcript/note/handoff/daemon pointing at it)."""
    if any(markers.get(m) for m in STRONG_MARKERS):
        return True
    if has_slug_ref and any(markers.get(m) for m in WEAK_MARKERS):
        return True
    return False


def _marker_score(markers: dict[str, bool]) -> int:
    return sum(1 for m in (*STRONG_MARKERS, *WEAK_MARKERS) if markers.get(m))


@dataclass
class Candidate:
    path: str                                   # normalized by __post_init__
    markers: dict[str, bool] = field(default_factory=dict)
    git_commit_count: int = 0
    last_activity_at: str | None = None
    last_git_commit_at: str | None = None
    last_transcript_at: str | None = None
    last_handoff_at: str | None = None
    is_daemonized: bool = False
    has_slug_ref: bool = False
    sources: list[str] = field(default_factory=list)
    purpose: str = ""
    domain: str = ""
    aliases: list[dict[str, str]] = field(default_factory=list)  # pre-known slug aliases

    def __post_init__(self) -> None:
        self.path = normalize(self.path)


@dataclass
class ResolvedProject:
    canonical_path: str
    slug: str
    family: str
    markers: dict[str, bool]
    git_commit_count: int
    last_activity_at: str | None
    status: str                                 # active | ambiguous
    purpose: str
    domain: str
    sources: list[str]
    aliases: list[dict[str, str]]               # {alias_value, alias_kind, source}
    last_git_commit_at: str | None = None
    last_transcript_at: str | None = None
    last_handoff_at: str | None = None
    is_daemonized: bool = False


def reconcile_candidates(candidates: list[Candidate]) -> list[ResolvedProject]:
    """Fold a flat candidate list into canonical projects + aliases."""
    # 1. Dedupe exact paths (merge sources/markers).
    by_key: dict[str, Candidate] = {}
    for cand in candidates:
        key = ckey(cand.path)
        if key in by_key:
            existing = by_key[key]
            existing.markers.update(cand.markers)
            existing.git_commit_count = max(existing.git_commit_count, cand.git_commit_count)
            existing.has_slug_ref = existing.has_slug_ref or cand.has_slug_ref
            existing.is_daemonized = existing.is_daemonized or cand.is_daemonized
            existing.sources = sorted(set(existing.sources) | set(cand.sources))
            existing.aliases.extend(cand.aliases)
            existing.last_activity_at = _max_iso(existing.last_activity_at, cand.last_activity_at)
            existing.last_git_commit_at = _max_iso(existing.last_git_commit_at, cand.last_git_commit_at)
            existing.last_transcript_at = _max_iso(existing.last_transcript_at, cand.last_transcript_at)
            existing.last_handoff_at = _max_iso(existing.last_handoff_at, cand.last_handoff_at)
            if not existing.purpose:
                existing.purpose = cand.purpose
            if not existing.domain:
                existing.domain = cand.domain
        else:
            by_key[key] = cand
    roots = list(by_key.values())

    # 2. Nesting: nearest ancestor among candidates (by path prefix).
    keys = sorted((c.path for c in roots), key=len)
    nested_under: dict[str, str | None] = {}
    for cand in roots:
        ancestor = None
        for other in keys:
            if other == cand.path:
                continue
            if ckey(cand.path).startswith(ckey(other) + "/"):
                if ancestor is None or len(other) > len(ancestor):
                    ancestor = other
        nested_under[cand.path] = ancestor

    # 3. Ambiguous parents: contains another candidate AND has no commits of its own.
    contains_others = {c.path: False for c in roots}
    for _, anc in nested_under.items():
        if anc is not None:
            contains_others[anc] = True

    # 4. Group by family key.
    groups: dict[str, list[Candidate]] = {}
    for cand in roots:
        groups.setdefault(family_key(basename(cand.path)), []).append(cand)

    resolved: list[ResolvedProject] = []
    for family, members in groups.items():
        canonical = _choose_canonical(members, nested_under)
        aliases: list[dict[str, str]] = []
        # pre-known slug aliases carried on any member attach to the canonical
        for m in members:
            aliases.extend(m.aliases)
        for m in members:
            if m.path == canonical.path:
                continue
            aliases.append({
                "alias_value": m.path,
                "alias_kind": _alias_kind(m.path, nested_under.get(m.path)),
                "source": ",".join(m.sources) or "fs-scan",
            })

        ambiguous = (
            contains_others.get(canonical.path, False)
            and canonical.git_commit_count == 0
        )
        resolved.append(
            ResolvedProject(
                canonical_path=canonical.path,
                slug=basename(canonical.path),
                family=family,
                markers=canonical.markers,
                git_commit_count=canonical.git_commit_count,
                last_activity_at=_max_over(members),
                status="ambiguous" if ambiguous else "active",
                purpose=canonical.purpose or _first_purpose(members),
                domain=canonical.domain or _first_domain(members),
                sources=sorted({s for m in members for s in m.sources}),
                aliases=_dedupe_aliases(aliases),
                last_git_commit_at=_max_field(members, "last_git_commit_at"),
                last_transcript_at=_max_field(members, "last_transcript_at"),
                last_handoff_at=_max_field(members, "last_handoff_at"),
                is_daemonized=any(m.is_daemonized for m in members),
            )
        )
    resolved.sort(key=lambda r: (r.last_activity_at or "", r.canonical_path), reverse=True)
    return resolved


def _choose_canonical(members: list[Candidate], nested_under: dict[str, str | None]) -> Candidate:
    def rank(c: Candidate) -> tuple:
        return (
            c.git_commit_count > 0,          # own git with commits wins
            c.git_commit_count,
            not is_archive_path(c.path),     # non-archive preferred
            nested_under.get(c.path) is None,  # not nested preferred
            _marker_score(c.markers),
            -len(c.path),                    # shorter path preferred
        )

    return max(members, key=rank)


def _alias_kind(path: str, ancestor: str | None) -> str:
    if is_archive_path(path):
        return "superseded" if "_superseded" in path.lower() else "archive"
    if ancestor is not None:
        return "embedded"
    return "fs-path"


def _dedupe_aliases(aliases: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for a in aliases:
        key = (a["alias_value"].casefold(), a["alias_kind"])
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def _first_purpose(members: list[Candidate]) -> str:
    for m in members:
        if m.purpose:
            return m.purpose
    return ""


def _first_domain(members: list[Candidate]) -> str:
    for m in members:
        if m.domain:
            return m.domain
    return ""


def _max_over(members: list[Candidate]) -> str | None:
    result: str | None = None
    for m in members:
        result = _max_iso(result, m.last_activity_at)
    return result


def _max_field(members: list[Candidate], attr: str) -> str | None:
    result: str | None = None
    for m in members:
        result = _max_iso(result, getattr(m, attr))
    return result


def _max_iso(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b
