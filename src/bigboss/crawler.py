"""General-purpose project doc crawler: the shared "eyes" tool.

Walks a project directory, reads its planning/overview docs under a character
budget, and returns a machine-consumable bundle (files + text + content hash).
It is deliberately a standalone tool, not intel-digester internals, so any
consumer can use it:

- BigBoss's intel digester (`intel/digest.py`) feeds bundles to Squire.
- Squire (general purpose) or any harness can call it via the `crawl_project` /
  `crawl_portfolio` MCP tools on the bigboss stdio facade.
- Anything else can shell out to `bigboss crawl --json`.

The content hash is computed over the clipped text and is stable whether or not
text is included in the bundle, so consumers can cheaply detect "docs changed"
without carrying the payload.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Ordered by signal density; planning docs first, general docs as fallback context.
DOC_FILES = (
    "workflow/PLAN.md",
    "PLAN.md",
    "ROADMAP.md",
    "docs/ROADMAP.md",
    "TODO.md",
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
)
PER_FILE_CHARS = 9_000
TOTAL_CHARS = 22_000


def crawl_project(
    project_path: Path | str,
    per_file_chars: int = PER_FILE_CHARS,
    total_chars: int = TOTAL_CHARS,
    include_text: bool = True,
) -> dict[str, Any]:
    """Crawl one project directory into a doc bundle."""
    root = Path(project_path)
    files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    budget = total_chars
    for rel in DOC_FILES:
        if budget <= 0:
            break
        f = root / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()
        if not text:
            continue
        clipped = text[: min(per_file_chars, budget)]
        budget -= len(clipped)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(clipped.encode("utf-8"))
        digest.update(b"\x00")
        entry: dict[str, Any] = {
            "rel": rel,
            "chars": len(clipped),
            "truncated": len(clipped) < len(text),
        }
        if include_text:
            entry["text"] = clipped
        files.append(entry)
    return {
        "path": str(root).replace("\\", "/"),
        "exists": root.is_dir(),
        "files": files,
        "source_hash": digest.hexdigest() if files else "",
        "total_chars": sum(f["chars"] for f in files),
        "crawled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def collect_sources(
    project_path: Path | str, total_chars: int = TOTAL_CHARS
) -> list[tuple[str, str]]:
    """Bundle as [(rel, text)] pairs - the shape the intel digester prompts with."""
    bundle = crawl_project(project_path, total_chars=total_chars, include_text=True)
    return [(f["rel"], f["text"]) for f in bundle["files"]]


def source_hash(sources: list[tuple[str, str]]) -> str:
    """Hash of [(rel, text)] pairs; identical to the bundle's source_hash."""
    digest = hashlib.sha256()
    for rel, text in sources:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def crawl_portfolio(
    store,
    slugs: list[str] | None = None,
    include_text: bool = False,
    per_file_chars: int = PER_FILE_CHARS,
    total_chars: int = TOTAL_CHARS,
) -> list[dict[str, Any]]:
    """Crawl every registered project (or the given slugs) into bundles, each
    carrying enough registry metadata to be useful standalone. Text is omitted
    by default: hashes + file inventory stay small; opt in for full payloads."""
    wanted = {s.casefold() for s in slugs} if slugs else None
    out: list[dict[str, Any]] = []
    for project in store.list_projects():
        if wanted is not None and project["slug"].casefold() not in wanted:
            continue
        if project.get("no_crawl"):
            # Refusal is visible, never silent: explicit requests get a stub so
            # the caller sees the exclusion instead of an absent project.
            if wanted is not None:
                out.append(
                    {
                        "path": project["canonical_path"],
                        "exists": True,
                        "excluded": True,
                        "files": [],
                        "source_hash": "",
                        "total_chars": 0,
                        "project": {"slug": project["slug"], "name": project["name"]},
                    }
                )
            continue
        bundle = crawl_project(
            project["canonical_path"],
            per_file_chars=per_file_chars,
            total_chars=total_chars,
            include_text=include_text,
        )
        bundle["project"] = {
            "id": project["id"],
            "slug": project["slug"],
            "name": project["name"],
            "kind": project["kind"],
            "pinned": project["pinned"],
            "last_activity_at": project["last_activity_at"],
        }
        out.append(bundle)
    return out


def resolve_project_path(store, slug: str) -> str:
    """Registry slug -> canonical path (case-insensitive). Raises KeyError,
    including for crawl-excluded projects (their egress policy wins)."""
    for project in store.list_projects(include_ambiguous=True):
        if project["slug"].casefold() == slug.casefold():
            if project.get("no_crawl"):
                raise KeyError(
                    f"Project {slug!r} is crawl-excluded (no_crawl); it is never crawled or digested."
                )
            return project["canonical_path"]
    raise KeyError(f"No registered project with slug {slug!r}. Run registry-refresh?")


def refuse_no_crawl_path(store, path: Path | str) -> None:
    """Raise if a raw path points into a crawl-excluded project's tree. This is
    the guard for path-based crawls that bypass slug resolution entirely."""
    try:
        target = Path(path).resolve()
    except OSError:
        return
    for project in store.list_projects(include_ambiguous=True):
        if not project.get("no_crawl"):
            continue
        try:
            canonical = Path(project["canonical_path"]).resolve()
        except OSError:
            continue
        if target == canonical or canonical in target.parents:
            raise KeyError(
                f"Path {str(path)!r} is inside crawl-excluded project "
                f"{project['slug']!r} (no_crawl)."
            )
