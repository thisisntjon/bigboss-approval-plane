"""Ingest a remote crawl bundle into the registry (E3.6, the remote host coverage).

A remote host (e.g. the remote host) cannot be walked by the local harvester, so it produces
a *crawl bundle* — a JSON list of marker-bearing project dirs on that host — and
ships it here (file drop over the SMB share, or a future authed LAN POST). This
module turns that bundle into `Candidate`s and reuses the exact same
`reconcile_candidates` -> `apply_reconciled` path as the local harvest, so remote
projects dedup, classify, and persist identically to local ones.

Remote projects are stored under a UNC-style canonical path `//<host>/<path>` so
they can never collide with a local drive, and tagged `source=remote:<host>`.

Bundle contract (bundle_version 1):
    {
      "bundle_version": 1,
      "host": "lanbox",                         # short host label, kebab/lowercase
      "generated_at": "2026-07-02T12:00:00Z",  # ISO 8601, optional
      "projects": [
        {
          "path": "V:/myproject",              # drive-qualified path ON the remote host
          "markers": {".git": true, "CLAUDE.md": true, ...},  # subset of KNOWN_MARKERS
          "git_commit_count": 42,              # 0 if not a repo
          "last_commit_at": "2026-06-30T...Z", # ISO, optional
          "purpose": "First prose line of CLAUDE.md/README",  # optional
          "is_daemonized": false               # optional
        }, ...
      ]
    }
"""

from __future__ import annotations

import re
from typing import Any

from .canonical import Candidate, is_project_root, normalize, reconcile_candidates

BUNDLE_VERSION = 1
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class BundleError(ValueError):
    """Malformed crawl bundle."""


def _remote_canonical(host: str, path: str) -> str:
    # UNC-style so a remote V: can never collide with a local drive path.
    return normalize(f"//{host}/{normalize(path)}")


def bundle_to_candidates(bundle: dict[str, Any]) -> list[Candidate]:
    if not isinstance(bundle, dict):
        raise BundleError("bundle must be a JSON object")
    version = bundle.get("bundle_version")
    if version != BUNDLE_VERSION:
        raise BundleError(f"unsupported bundle_version {version!r} (expected {BUNDLE_VERSION})")
    host = str(bundle.get("host", "")).strip().lower()
    if not _HOST_RE.match(host):
        raise BundleError("bundle.host must be a short lowercase label (e.g. 'lanbox')")
    projects = bundle.get("projects")
    if not isinstance(projects, list):
        raise BundleError("bundle.projects must be a list")

    candidates: list[Candidate] = []
    for i, proj in enumerate(projects):
        if not isinstance(proj, dict):
            raise BundleError(f"projects[{i}] must be an object")
        path = str(proj.get("path", "")).strip()
        if not path:
            raise BundleError(f"projects[{i}] missing 'path'")
        markers = {str(k): bool(v) for k, v in (proj.get("markers") or {}).items()}
        # The remote crawl IS the external reference, so a weak-marker-only remote
        # dir still qualifies (we cannot mine transcript/note slugs for a remote host).
        if not is_project_root(markers, has_slug_ref=True):
            continue
        last_commit = proj.get("last_commit_at") or None
        candidates.append(
            Candidate(
                path=_remote_canonical(host, path),
                markers=markers,
                git_commit_count=int(proj.get("git_commit_count") or 0),
                last_git_commit_at=last_commit,
                last_activity_at=last_commit or bundle.get("generated_at"),
                is_daemonized=bool(proj.get("is_daemonized")),
                has_slug_ref=True,
                sources=[f"remote:{host}"],
                purpose=str(proj.get("purpose") or "")[:200],
            )
        )
    return candidates


def ingest_bundle(store, bundle: dict[str, Any], prune: bool = False) -> dict[str, Any]:
    """Reconcile a remote crawl bundle and upsert it into the registry.

    prune=True gives full-sync semantics: this host's rows absent from the bundle are
    tombstoned (a bundle is the host's complete inventory). Scoped to '//<host>/...', so
    it never touches local or other-host rows."""
    candidates = bundle_to_candidates(bundle)
    host = str(bundle.get("host", "")).strip().lower()
    resolved = reconcile_candidates(candidates)
    summary = store.apply_reconciled(resolved, prune_host=host if prune else None)
    summary["host"] = host
    summary["candidates"] = len(candidates)
    summary["resolved"] = len(resolved)
    # E4a.1: gate off-box digestion of any newly-ingested remote projects behind an
    # approval card (idempotent).
    card = store.create_digest_batch_card()
    summary["digest_batch_card"] = card["id"] if card else None
    return summary
