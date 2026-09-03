"""Harvest project signals from the machine and reconcile them into the registry.

Cheap, stdlib-only signal sources (Ecosystem Phase E3 v1; D: root added E3.6):
- fs-scan of Desktop + Desktop/Python + the D: drive (shallow) for marker-bearing project roots
- git last-commit + commit count per repo
- ~/.claude/projects transcript slugs (activity + slug reference)
- ~/.claude/handoffs (where-I-left-off)
- ~/.claude/daemons.json (is_daemonized)
- D: drive projects also discovered via D-- transcript slugs, validated on disk (catches nested/opened repos)

Slug matching uses *encoding* (path -> slug), which round-trips deterministically even for
hyphenated names like the-council-capstone - unlike decoding (slug -> path), which is ambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from .canonical import Candidate, STRONG_MARKERS, WEAK_MARKERS, normalize


ALL_MARKERS = (*STRONG_MARKERS, *WEAK_MARKERS)
# Slug substrings that indicate a game play-session, not a project (excluded).
_GAME_HINTS = ("slaythespire", "mewgenics", "steam", "caves of qud")


# Extra roots (e.g. a second drive) scanned shallowly (top level + one level).
# Set BIGBOSS_EXTRA_ROOTS to an os.pathsep-separated list of paths. The marker
# filter + canonical dedup reject junk and cross-drive duplicates, so a stale
# copy on another drive collapses into the canonical one. (E3.6)
def extra_roots() -> list[Path]:
    raw = os.environ.get("BIGBOSS_EXTRA_ROOTS", "")
    return [Path(p.strip()) for p in raw.split(os.pathsep) if p.strip()]


def default_roots(home: Path) -> list[Path]:
    desktop = home / "Desktop"
    return [desktop, desktop / "Python", *extra_roots()]


def detect_markers(path: Path) -> dict[str, bool]:
    return {m: (path / m).exists() for m in ALL_MARKERS}


def _has_any_marker(markers: dict[str, bool]) -> bool:
    return any(markers.values())


def _iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def git_info(path: Path) -> tuple[int, str | None]:
    """(commit_count, last_commit_iso) or (0, None) if not a repo / no commits."""
    if not (path / ".git").exists():
        return (0, None)
    try:
        count = subprocess.run(
            ["git", "-C", str(path), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        last = subprocess.run(
            ["git", "-C", str(path), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return (0, None)
    n = int(count.stdout.strip()) if count.returncode == 0 and count.stdout.strip().isdigit() else 0
    iso = None
    if last.returncode == 0 and last.stdout.strip():
        iso = last.stdout.strip().replace("+00:00", "Z")
    return (n, iso)


def read_purpose(path: Path) -> str:
    """First substantive prose line of CLAUDE.md/README - skipping bare title headers
    (# CLAUDE.md, # <project name>) and boilerplate so the row says something useful."""
    project_name = path.name.lower()
    for name in ("CLAUDE.md", "README.md"):
        f = path / name
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("```", "---", "|", ">")):
                continue
            stripped = line.lstrip("#").strip().strip("*").strip()
            low = stripped.lower()
            if not stripped:
                continue
            if low.startswith(("claude.md", "readme", "agents.md")) or low == project_name:
                continue
            if low.startswith(("this file provides guidance", "this document provides", "this repo hosts")):
                continue  # generic CLAUDE.md boilerplate - prefer the real overview line
            # Skip a short bare title header; prefer the first real sentence beneath it.
            if raw.lstrip().startswith("#") and len(stripped.split()) <= 3:
                continue
            return stripped[:200]
    return ""


def encode_transcript_slug(path: str) -> str:
    # "C:/dev/Python/BigBoss" -> "C--Users-<user>-Desktop-Python-BigBoss"
    return normalize(path).replace(":", "-").replace("/", "-")


def encode_note_slug(path: str) -> str:
    # lowercased, ':' dropped -> "c-users-<user>-desktop-python-bigboss"
    return normalize(path).lower().replace(":", "").replace("/", "-")


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _scan_root(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    # root.name is empty for a drive root (e.g. "D:/") — never treat the whole
    # drive as one project; we only want its marker-bearing children.
    if root.name and _has_any_marker(detect_markers(root)):
        out.append(root)
    try:
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith((".", "__")):
                if _has_any_marker(detect_markers(child)):
                    out.append(child)
    except OSError:
        pass
    return out


def _transcript_slugs(home: Path) -> dict[str, str]:
    """slug -> newest *.jsonl mtime iso (or dir mtime)."""
    base = home / ".claude" / "projects"
    result: dict[str, str] = {}
    if not base.exists():
        return result
    for d in base.iterdir():
        if not d.is_dir():
            continue
        newest = d.stat().st_mtime
        try:
            for f in d.glob("*.jsonl"):
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
        result[d.name] = _iso_from_mtime(newest)
    return result


def _note_slugs(home: Path) -> set[str]:
    base = home / ".claude" / "local-memory" / "notes"
    if not base.exists():
        return set()
    return {d.name for d in base.iterdir() if d.is_dir()}


def _handoffs(home: Path) -> list[tuple[str, str]]:
    """[(normalized-filename, mtime iso)] for where-I-left-off matching."""
    base = home / ".claude" / "handoffs"
    out: list[tuple[str, str]] = []
    if not base.exists():
        return out
    for f in base.glob("*.md"):
        out.append((_norm_name(f.stem), _iso_from_mtime(f.stat().st_mtime)))
    return out


def _daemon_targets(home: Path) -> set[str]:
    f = home / ".claude" / "daemons.json"
    if not f.exists():
        return set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    targets: set[str] = set()
    entries = list(data.values()) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("project", "cwd", "path", "directory", "dir"):
                if entry.get(key):
                    targets.add(normalize(str(entry[key])).casefold())
    return targets


def _decode_d_drive_slug(slug: str) -> str | None:
    # Only single-segment D-- slugs decode unambiguously (D--foo -> D:/foo).
    if not slug.startswith("D--"):
        return None
    rest = slug[3:]
    if "-" in rest:  # ambiguous (hyphen in name vs separator) - skip
        return None
    return f"D:/{rest}"


def harvest(roots: list[Path], home: Path) -> list[Candidate]:
    transcripts = _transcript_slugs(home)
    notes = _note_slugs(home)
    handoffs = _handoffs(home)
    daemons = _daemon_targets(home)

    candidates: dict[str, Candidate] = {}

    def _add(path_obj: Path) -> None:
        markers = detect_markers(path_obj)
        if not _has_any_marker(markers):
            return
        path = normalize(str(path_obj))
        commits, last_commit = git_info(path_obj)
        cand = Candidate(
            path=path,
            markers=markers,
            git_commit_count=commits,
            last_git_commit_at=last_commit,
            last_activity_at=last_commit,
            is_daemonized=path.casefold() in daemons,
            sources=["fs-scan"] + (["git"] if commits else []),
            purpose=read_purpose(path_obj),
        )
        candidates[normalize(path).casefold()] = cand

    # 1. fs-scan the configured roots.
    for root in roots:
        for p in _scan_root(root):
            _add(p)

    # 2. D: drive projects via unambiguous D-- transcript slugs, validated on disk.
    for slug in transcripts:
        decoded = _decode_d_drive_slug(slug)
        if decoded and Path(decoded).exists():
            _add(Path(decoded))

    # 3. Enrich every candidate with slug references (encode-and-match).
    for cand in candidates.values():
        tslug = encode_transcript_slug(cand.path)
        nslug = encode_note_slug(cand.path)
        if any(hint in tslug.lower() for hint in _GAME_HINTS):
            continue
        if tslug in transcripts:
            cand.has_slug_ref = True
            cand.last_transcript_at = transcripts[tslug]
            cand.last_activity_at = _max(cand.last_activity_at, transcripts[tslug])
            cand.sources = sorted(set(cand.sources) | {"transcript"})
        if nslug in notes:
            cand.has_slug_ref = True
            cand.sources = sorted(set(cand.sources) | {"note"})
        norm = _norm_name(cand.path.rsplit("/", 1)[-1])
        for hname, hts in handoffs:
            if norm and hname.startswith(norm):
                cand.last_handoff_at = _max(cand.last_handoff_at, hts)
                cand.last_activity_at = _max(cand.last_activity_at, hts)
                cand.has_slug_ref = True
                cand.sources = sorted(set(cand.sources) | {"handoff"})

    return list(candidates.values())


def refresh(store, roots: list[Path] | None = None, home: Path | None = None, prune: bool = False) -> dict:
    """Harvest -> reconcile -> persist. Returns the apply summary.

    prune=True tombstones local projects that vanished since the last refresh
    (scope-safe: remote-ingested '//host/...' rows are never touched)."""
    from .canonical import reconcile_candidates

    home = home or Path.home()
    roots = roots or default_roots(home)
    candidates = harvest(roots, home)
    resolved = reconcile_candidates(candidates)
    summary = store.apply_reconciled(resolved, prune_local=prune)
    summary["candidates"] = len(candidates)
    summary["resolved"] = len(resolved)
    # E4a.1: if this discovered new projects, raise a batch card so a human gates
    # their off-box digestion (idempotent — no-op if nothing pending / card open).
    card = store.create_digest_batch_card()
    summary["digest_batch_card"] = card["id"] if card else None
    return summary


def _max(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b
