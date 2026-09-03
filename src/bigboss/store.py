from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from .contracts import action_hash as compute_action_hash
from .policy import classify_action
from .security import new_pair_code, new_token, token_hash, token_matches


VALID_DECISIONS = {"approve_once", "approve_for_run", "reject", "request_changes"}

# Router (Ecosystem Phase E1) budget ledger constants.
DEFAULT_HARD_CAP_USD = "200"
# Ordered escalation of the per-period alert banner; each fires once as spend crosses it.
_ALERT_ORDER = ["none", "p50", "p75", "p90", "over"]
_ALERT_LABELS = {"p50": "50%", "p75": "75%", "p90": "90%", "over": "100%"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


EGRESS_TEXT_CAP = 50_000


def cap_egress_text(text: str) -> str:
    """Keep the local audit readable without bloating SQLite."""
    if len(text) <= EGRESS_TEXT_CAP:
        return text
    return text[:EGRESS_TEXT_CAP] + f"\n... [truncated, {len(text)} chars total]"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS pair_codes (
                    code_hash TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS stream_tokens (
                    token_hash TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    FOREIGN KEY(device_id) REFERENCES devices(id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    harness TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    action_hash TEXT NOT NULL,
                    proposed_action_json TEXT NOT NULL,
                    policy_reasons_json TEXT NOT NULL,
                    diff_summary_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    resolved_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    note TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(approval_id) REFERENCES approval_requests(id),
                    FOREIGN KEY(device_id) REFERENCES devices(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- Router (Ecosystem Phase E1): one row per proxied /v1/messages call.
                -- This is the spend ledger and the source of truth for cost + reroute rate.
                CREATE TABLE IF NOT EXISTS router_calls (
                    id                          TEXT PRIMARY KEY,
                    run_id                      TEXT,
                    harness                     TEXT NOT NULL,
                    requested_model             TEXT NOT NULL,
                    served_model                TEXT NOT NULL,
                    tier_reason                 TEXT NOT NULL,
                    input_tokens                INTEGER NOT NULL DEFAULT 0,
                    output_tokens               INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
                    cost_usd                    TEXT NOT NULL,
                    streamed                    INTEGER NOT NULL DEFAULT 0,
                    stop_reason                 TEXT,
                    created_at                  TEXT NOT NULL
                );

                -- Router: monthly budget periods with a denormalized running total for
                -- O(1) gate checks. In E1 the cap is ADVISORY (alert, never block).
                CREATE TABLE IF NOT EXISTS budget_periods (
                    period        TEXT PRIMARY KEY,
                    hard_cap_usd  TEXT NOT NULL DEFAULT '200',
                    spent_usd     TEXT NOT NULL DEFAULT '0',
                    alert_state   TEXT NOT NULL DEFAULT 'none',
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );

                -- Project registry (Ecosystem Phase E3): one row per canonical project.
                CREATE TABLE IF NOT EXISTS projects (
                    id                 TEXT PRIMARY KEY,
                    slug               TEXT NOT NULL,
                    name               TEXT NOT NULL,
                    canonical_path     TEXT NOT NULL UNIQUE,
                    kind               TEXT NOT NULL DEFAULT 'unknown',
                    purpose            TEXT NOT NULL DEFAULT '',
                    domain             TEXT NOT NULL DEFAULT '',
                    status             TEXT NOT NULL DEFAULT 'active',
                    pinned             INTEGER NOT NULL DEFAULT 0,
                    pin_rank           INTEGER,
                    is_daemonized      INTEGER NOT NULL DEFAULT 0,
                    git_commit_count   INTEGER NOT NULL DEFAULT 0,
                    last_activity_at   TEXT,
                    last_git_commit_at TEXT,
                    last_transcript_at TEXT,
                    last_handoff_at    TEXT,
                    markers_json       TEXT NOT NULL DEFAULT '{}',
                    -- E4a.1: privacy exclusion. A no_crawl project is never crawled,
                    -- digested, or sent to a compute endpoint - by any path (scheduler,
                    -- CLI, MCP). Set for projects with their own egress policy (brainz).
                    no_crawl           INTEGER NOT NULL DEFAULT 0,
                    -- E4a.1: a newly-discovered project is digest-gated until a human
                    -- approves the batch, so a registry expansion never auto-sends new
                    -- docs off-box. Existing rows default 0 (already digested/trusted).
                    digest_pending     INTEGER NOT NULL DEFAULT 0,
                    first_seen_at      TEXT NOT NULL,
                    created_at         TEXT NOT NULL,
                    updated_at         TEXT NOT NULL,
                    archived_at        TEXT
                );

                -- Every non-canonical path/slug that maps to a project (dedup record).
                CREATE TABLE IF NOT EXISTS project_aliases (
                    id           TEXT PRIMARY KEY,
                    project_id   TEXT NOT NULL,
                    alias_value  TEXT NOT NULL,
                    alias_kind   TEXT NOT NULL,
                    source       TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    UNIQUE(alias_value, alias_kind),
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );

                -- Append-only raw harvest log; projects is a projection built from these.
                CREATE TABLE IF NOT EXISTS project_signals (
                    id           TEXT PRIMARY KEY,
                    project_id   TEXT,
                    source       TEXT NOT NULL,
                    signal_type  TEXT NOT NULL,
                    value_json   TEXT NOT NULL,
                    observed_at  TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                );

                -- Project intel (Ecosystem Phase E4a): one Squire-digested snapshot per
                -- project - status, blockers, roadmap - refreshed when source docs change.
                CREATE TABLE IF NOT EXISTS project_intel (
                    project_id        TEXT PRIMARY KEY,
                    status_line       TEXT NOT NULL DEFAULT '',
                    blockers_json     TEXT NOT NULL DEFAULT '[]',
                    roadmap_now       TEXT NOT NULL DEFAULT '',
                    roadmap_next      TEXT NOT NULL DEFAULT '',
                    source_hash       TEXT NOT NULL DEFAULT '',
                    source_files_json TEXT NOT NULL DEFAULT '[]',
                    model             TEXT NOT NULL DEFAULT '',
                    error             TEXT,
                    generated_at      TEXT,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );

                -- Squire usage ledger (Ecosystem Phase E4a): one row per call through the
                -- Squire metering proxy / intel digester / health probe. Squire is free,
                -- so this ledger tracks load + reliability per client, not spend.
                CREATE TABLE IF NOT EXISTS squire_calls (
                    id                TEXT PRIMARY KEY,
                    endpoint          TEXT NOT NULL DEFAULT 'squire',
                    client            TEXT NOT NULL DEFAULT 'unknown',
                    purpose           TEXT NOT NULL DEFAULT 'chat',
                    model             TEXT NOT NULL DEFAULT '',
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms        INTEGER NOT NULL DEFAULT 0,
                    ok                INTEGER NOT NULL DEFAULT 1,
                    error             TEXT,
                    created_at        TEXT NOT NULL
                );

                -- Data-egress audit (Ecosystem Phase E4a): one row per payload BigBoss
                -- sends OUT to a compute endpoint on another machine. This is not a gate -
                -- it is the accountability record of what data left, so exposure via a
                -- machine's known off-box channel (e.g. an endpoint-security (EDR) agent on the guarded box) is auditable.
                CREATE TABLE IF NOT EXISTS egress_log (
                    id                TEXT PRIMARY KEY,
                    endpoint          TEXT NOT NULL,
                    exposure          TEXT NOT NULL DEFAULT '',
                    base_url          TEXT NOT NULL DEFAULT '',
                    model             TEXT NOT NULL DEFAULT '',
                    client            TEXT NOT NULL DEFAULT '',
                    purpose           TEXT NOT NULL DEFAULT '',
                    project_id        TEXT,
                    project_slug      TEXT NOT NULL DEFAULT '',
                    source_files_json TEXT NOT NULL DEFAULT '[]',
                    content_hash      TEXT NOT NULL DEFAULT '',
                    chars_sent        INTEGER NOT NULL DEFAULT 0,
                    tokens_sent       INTEGER NOT NULL DEFAULT 0,
                    tokens_received   INTEGER NOT NULL DEFAULT 0,
                    prompt_text       TEXT NOT NULL DEFAULT '',
                    response_text     TEXT NOT NULL DEFAULT '',
                    ok                INTEGER NOT NULL DEFAULT 1,
                    created_at        TEXT NOT NULL
                );

                -- Council (M1): one row per deliberation session (the audit + the answer).
                CREATE TABLE IF NOT EXISTS council_sessions (
                    id                  TEXT PRIMARY KEY,
                    mode                TEXT NOT NULL DEFAULT 'live',   -- live | fixture
                    question            TEXT NOT NULL DEFAULT '',       -- redacted
                    seats_json          TEXT NOT NULL DEFAULT '[]',
                    winner              TEXT,
                    final_answer        TEXT NOT NULL DEFAULT '',
                    per_model_scores_json TEXT NOT NULL DEFAULT '{}',
                    verified_claims_json  TEXT NOT NULL DEFAULT '[]',
                    tokens_in           INTEGER NOT NULL DEFAULT 0,
                    tokens_out          INTEGER NOT NULL DEFAULT 0,
                    cost_usd            TEXT NOT NULL DEFAULT '0',
                    created_at          TEXT NOT NULL
                );

                -- Council (M1): append-only per-model verified-claim accuracy — the THRONE's
                -- election track record (M4 aggregates these).
                CREATE TABLE IF NOT EXISTS council_model_scores (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL,
                    model_id    TEXT NOT NULL,
                    model       TEXT NOT NULL DEFAULT '',
                    verified    INTEGER NOT NULL DEFAULT 0,
                    partial     INTEGER NOT NULL DEFAULT 0,
                    total       INTEGER NOT NULL DEFAULT 0,
                    score       REAL NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL
                );

                -- Track B: Fable-led deep-research-per-project (brief + phased roadmap).
                CREATE TABLE IF NOT EXISTS project_research (
                    id           TEXT PRIMARY KEY,
                    project_id   TEXT NOT NULL,
                    slug         TEXT NOT NULL DEFAULT '',
                    model        TEXT NOT NULL DEFAULT '',
                    brief_json   TEXT NOT NULL DEFAULT '{}',
                    tokens_in    INTEGER NOT NULL DEFAULT 0,
                    tokens_out   INTEGER NOT NULL DEFAULT 0,
                    cost_usd     TEXT NOT NULL DEFAULT '0',
                    created_at   TEXT NOT NULL
                );

                -- P-ops.3: cross-vendor harness fleet session-reports (what each
                -- session accomplished). Written by CLI / MCP / the handoff wrapup;
                -- one fleet log for every harness across every project. M6.5-Full added
                -- `host` (which machine) — '' means this box.
                CREATE TABLE IF NOT EXISTS harness_sessions (
                    id           TEXT PRIMARY KEY,
                    harness      TEXT NOT NULL,
                    vendor       TEXT NOT NULL DEFAULT '',
                    project      TEXT NOT NULL DEFAULT '',
                    workspace    TEXT NOT NULL DEFAULT '',
                    title        TEXT NOT NULL DEFAULT '',
                    summary      TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    session_ref  TEXT NOT NULL DEFAULT '',
                    host         TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL
                );

                -- M6.5-Full: latest cross-machine activity snapshot per (host, kind) —
                -- e.g. the remote host's live Squire job + queue. Current-state (upsert), NOT an
                -- append log. Rendered generically with provenance + staleness; BigBoss
                -- never asserts remote truth it can't independently verify.
                CREATE TABLE IF NOT EXISTS remote_snapshots (
                    host         TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    reported_at  TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL,
                    PRIMARY KEY (host, kind)
                );

                -- Cross-machine CONTROL: a human-approved command for another machine to
                -- execute (BigBoss → the remote host, PULL). BigBoss is source-of-truth; the remote
                -- polls for pending, executes, and acks. Written ONLY by resolve_approval
                -- on human approval — never by the model directly.
                CREATE TABLE IF NOT EXISTS remote_commands (
                    id          TEXT PRIMARY KEY,
                    host        TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    args_json   TEXT NOT NULL DEFAULT '{}',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    issued_at   TEXT NOT NULL,
                    acked_at    TEXT NOT NULL DEFAULT '',
                    created_at  TEXT NOT NULL
                );
                """
            )
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(approval_requests)").fetchall()
        }
        if "action_hash" not in columns:
            conn.execute(
                "ALTER TABLE approval_requests ADD COLUMN action_hash TEXT NOT NULL DEFAULT ''"
            )
            rows = conn.execute(
                "SELECT id, workspace, proposed_action_json FROM approval_requests"
            ).fetchall()
            for row in rows:
                try:
                    proposed_action = json.loads(row[2])
                except json.JSONDecodeError:
                    proposed_action = {"kind": "unknown"}
                conn.execute(
                    "UPDATE approval_requests SET action_hash = ? WHERE id = ?",
                    (compute_action_hash(proposed_action, row[1] or ""), row[0]),
                )

        # squire_calls.endpoint added when Beastmode (a second endpoint) landed;
        # pre-existing rows were all the default Squire box.
        squire_cols = {row[1] for row in conn.execute("PRAGMA table_info(squire_calls)").fetchall()}
        if squire_cols and "endpoint" not in squire_cols:
            conn.execute(
                "ALTER TABLE squire_calls ADD COLUMN endpoint TEXT NOT NULL DEFAULT 'squire'"
            )

        egress_cols = {row[1] for row in conn.execute("PRAGMA table_info(egress_log)").fetchall()}
        if egress_cols and "prompt_text" not in egress_cols:
            conn.execute("ALTER TABLE egress_log ADD COLUMN prompt_text TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE egress_log ADD COLUMN response_text TEXT NOT NULL DEFAULT ''")

        # projects.no_crawl added by E4a.1 (brainz boundary hygiene); pre-existing
        # registries default every project to crawlable, matching prior behavior.
        project_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if project_cols and "no_crawl" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN no_crawl INTEGER NOT NULL DEFAULT 0")
        # projects.digest_pending added by E4a.1 (digest-egress gating); pre-existing
        # rows default 0 so already-known projects keep digesting — only future
        # discoveries are gated.
        if project_cols and "digest_pending" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN digest_pending INTEGER NOT NULL DEFAULT 0")
        # projects.excluded added 2026-07-03: a project the owner hand-excluded from portfolio
        # prioritization (done/submitted, or work-not-home). Durable across registry-refresh —
        # reconciliation never touches it, unlike status which reverts to 'active' while on disk.
        if project_cols and "excluded" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        # projects.lifecycle + ownership added 2026-07-03 (deep re-baseline): solid ground-truth
        # classification a frontier agent establishes, that the FS-derived `status`/`kind` cannot
        # express. Durable — reconciliation never lists them in its UPDATE set. lifecycle ∈
        # {active,done,archived,dormant,experiment}; ownership ∈ {personal,work,third-party}.
        if project_cols and "lifecycle" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN lifecycle TEXT NOT NULL DEFAULT ''")
        if project_cols and "ownership" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN ownership TEXT NOT NULL DEFAULT ''")
        # projects.notes/lineage/track_it added 2026-07-03 (portfolio v2 — the operator's own context):
        # notes = the operator's authoritative per-project context; supersedes/evolved_into = lineage (comma-sep
        # slugs) so the Council sees evolution and never ranks an ancestor against its successor;
        # track_it = "complicated/sensitive — keep track of it" (work machines, legal
        # matters). All refresh-durable (reconciliation never lists them in its UPDATE set).
        for col, decl in (("notes", "TEXT NOT NULL DEFAULT ''"),
                          ("supersedes", "TEXT NOT NULL DEFAULT ''"),
                          ("evolved_into", "TEXT NOT NULL DEFAULT ''"),
                          ("track_it", "INTEGER NOT NULL DEFAULT 0"),
                          ("track_note", "TEXT NOT NULL DEFAULT ''")):
            if project_cols and col not in project_cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")

        # council_sessions.ideation_json added by M2.1 (ideation harvest): the consolidated
        # synergies/opportunities/tensions/brief blob for a prioritization session. Pre-existing
        # rows (M1 asks, M2 prioritizations) default to '{}' — no ideation captured for those.
        council_cols = {row[1] for row in conn.execute("PRAGMA table_info(council_sessions)").fetchall()}
        if council_cols and "ideation_json" not in council_cols:
            conn.execute("ALTER TABLE council_sessions ADD COLUMN ideation_json TEXT NOT NULL DEFAULT '{}'")
        # M5a: the Fable tie-break escalation record (decision signal + verdict) per session.
        if council_cols and "escalation_json" not in council_cols:
            conn.execute("ALTER TABLE council_sessions ADD COLUMN escalation_json TEXT NOT NULL DEFAULT '{}'")

        # M6.5-Full: harness_sessions gains `host` (which machine reported the session).
        # Existing (local) rows default to '' = this box, matching prior implicit semantics.
        hs_cols = {row[1] for row in conn.execute("PRAGMA table_info(harness_sessions)").fetchall()}
        if hs_cols and "host" not in hs_cols:
            conn.execute("ALTER TABLE harness_sessions ADD COLUMN host TEXT NOT NULL DEFAULT ''")

    def create_pair_code(self, device_name: str, ttl_seconds: int = 600) -> dict[str, Any]:
        code = new_pair_code()
        now = utc_now()
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pair_codes (code_hash, device_name, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_hash(code), device_name, _iso(now), _iso(expires)),
            )
        return {"code": code, "device_name": device_name, "expires_at": _iso(expires)}

    def claim_pair_code(self, code: str, fallback_name: str = "Phone") -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pair_codes
                WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (token_hash(code.strip().upper()), now),
            ).fetchone()
            if row is None:
                raise ValueError("Pair code is invalid or expired.")
            name = row["device_name"] or fallback_name
            conn.execute(
                "UPDATE pair_codes SET used_at = ? WHERE code_hash = ?", (now, row["code_hash"])
            )
            return self._enroll_device_conn(conn, name=name, method="pair_code", now=now)

    def enroll_device(self, name: str, *, method: str = "lan_pin") -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            return self._enroll_device_conn(conn, name=name, method=method, now=now)

    def _enroll_device_conn(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        method: str,
        now: str,
    ) -> dict[str, Any]:
        device_id = new_token("dev")
        auth_token = new_token("phone")
        csrf_token = new_token("csrf")
        conn.execute(
            """
            INSERT INTO devices (id, name, token_hash, csrf_hash, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (device_id, name, token_hash(auth_token), token_hash(csrf_token), now, now),
        )
        self._append_event_conn(
            conn,
            "device.paired",
            device_id,
            {"device_id": device_id, "name": name, "method": method},
            now,
        )
        return {
            "device_id": device_id,
            "device_name": name,
            "auth_token": auth_token,
            "csrf_token": csrf_token,
        }

    def list_devices(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if include_revoked:
                rows = conn.execute(
                    "SELECT id, name, created_at, last_seen_at, revoked_at FROM devices ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, name, created_at, last_seen_at, revoked_at
                    FROM devices
                    WHERE revoked_at IS NULL
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            return [dict(row) for row in rows]

    def revoke_device(self, device_id: str) -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            if row is None:
                raise KeyError(f"Device not found: {device_id}")
            if row["revoked_at"] is not None:
                return dict(row)
            conn.execute("UPDATE devices SET revoked_at = ? WHERE id = ?", (now, device_id))
            self._append_event_conn(
                conn,
                "device.revoked",
                device_id,
                {"device_id": device_id, "name": row["name"]},
                now,
            )
            updated = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            return dict(updated)

    def authenticate_device(self, auth_token: str | None) -> dict[str, Any] | None:
        if not auth_token:
            return None
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM devices WHERE revoked_at IS NULL").fetchall()
            for row in rows:
                if token_matches(auth_token, row["token_hash"]):
                    now = iso_now()
                    conn.execute(
                        "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, row["id"])
                    )
                    return dict(row)
        return None

    def csrf_is_valid(self, device: dict[str, Any], csrf_token: str | None) -> bool:
        return bool(csrf_token and token_matches(csrf_token, device["csrf_hash"]))

    def create_stream_token(self, device_id: str, ttl_seconds: int = 300) -> str:
        token = new_token("stream")
        now = utc_now()
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO stream_tokens (token_hash, device_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_hash(token), device_id, _iso(now), _iso(expires)),
            )
        return token

    def claim_stream_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = iso_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT stream_tokens.*, devices.name, devices.revoked_at
                FROM stream_tokens
                JOIN devices ON devices.id = stream_tokens.device_id
                WHERE stream_tokens.used_at IS NULL AND stream_tokens.expires_at > ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                if token_matches(token, row["token_hash"]) and row["revoked_at"] is None:
                    conn.execute(
                        "UPDATE stream_tokens SET used_at = ? WHERE token_hash = ?",
                        (now, row["token_hash"]),
                    )
                    return {"id": row["device_id"], "name": row["name"]}
        return None

    def create_approval_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        harness = str(payload.get("harness") or "unknown")
        workspace = str(payload.get("workspace") or "")
        run_id = str(payload.get("run_id") or new_token("run"))
        run_title = str(payload.get("run_title") or f"{harness} session")
        title = str(payload.get("title") or "Approval required")
        summary = str(payload.get("summary") or "A harness requested approval for an action.")
        proposed_action = dict(payload.get("proposed_action") or {"kind": "unknown"})
        diff_summary = dict(payload.get("diff_summary") or {})
        expires_at = payload.get("expires_at")
        policy = classify_action(proposed_action, workspace=workspace)
        action_hash = compute_action_hash(proposed_action, workspace)
        approval_id = str(payload.get("id") or new_token("apr"))
        now = iso_now()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs (id, harness, workspace, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, harness, workspace, run_title, "running", now, now),
            )
            conn.execute(
                "UPDATE runs SET updated_at = ?, status = ? WHERE id = ?",
                (now, "waiting" if policy.status == "pending" else "running", run_id),
            )
            conn.execute(
                """
                INSERT INTO approval_requests (
                    id, run_id, harness, workspace, title, summary, risk_level,
                    action_hash, proposed_action_json, policy_reasons_json, diff_summary_json,
                    status, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    harness,
                    workspace,
                    title,
                    summary,
                    policy.risk_level,
                    action_hash,
                    json.dumps(proposed_action, sort_keys=True),
                    json.dumps(policy.reasons),
                    json.dumps(diff_summary, sort_keys=True),
                    policy.status,
                    now,
                    expires_at,
                ),
            )
            approval = self._get_approval_conn(conn, approval_id)
            self._append_event_conn(conn, "approval.created", approval_id, approval, now)
        return approval

    def submit_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        harness = str(payload.get("harness") or "unknown")
        workspace = str(payload.get("workspace") or "")
        run_id = str(payload.get("run_id") or new_token("run"))
        title = str(payload.get("title") or f"{harness} update")
        summary = str(payload.get("summary") or "")
        severity = str(payload.get("severity") or "info")
        now = iso_now()
        update = {
            "run_id": run_id,
            "harness": harness,
            "workspace": workspace,
            "title": title,
            "summary": summary,
            "severity": severity,
            "details": payload.get("details") or {},
            "created_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs (id, harness, workspace, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, harness, workspace, title, "running", now, now),
            )
            conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (now, run_id))
            self._append_event_conn(conn, "run.updated", run_id, update, now)
        return update

    def list_approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        query = "SELECT * FROM approval_requests"
        params: tuple[Any, ...] = ()
        if status != "all":
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self.connect() as conn:
            return [
                self._row_to_approval(row, conn) for row in conn.execute(query, params).fetchall()
            ]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._get_approval_conn(conn, approval_id)

    def resolve_approval(
        self, approval_id: str, decision: str, note: str, device: dict[str, Any]
    ) -> dict[str, Any]:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"Unsupported decision: {decision}")
        now = iso_now()
        status = {
            "approve_once": "approved",
            "approve_for_run": "approved",
            "reject": "rejected",
            "request_changes": "changes_requested",
        }[decision]
        with self.connect() as conn:
            approval = self._get_approval_conn(conn, approval_id)
            if approval is None:
                raise KeyError("Approval not found.")
            if approval["status"] != "pending":
                raise ValueError(
                    f"Approval is not pending; current status is {approval['status']}."
                )

            decision_id = new_token("dec")
            conn.execute(
                """
                INSERT INTO decisions (id, approval_id, decision, note, approved_by, device_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, approval_id, decision, note, device["name"], device["id"], now),
            )
            conn.execute(
                "UPDATE approval_requests SET status = ?, resolved_at = ? WHERE id = ?",
                (status, now, approval_id),
            )
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                ("running", now, approval["run_id"]),
            )

            # E4a.1 digest-batch effect: approving a "new projects discovered" card
            # clears the digest gate for those slugs (they digest next cycle); rejecting
            # excludes them (no_crawl). Inlined on this conn to avoid a nested connection.
            action = approval.get("proposed_action") or {}
            if action.get("kind") == "digest_batch":
                slugs = [str(s) for s in (action.get("slugs") or [])]
                if slugs:
                    marks = ",".join("?" for _ in slugs)
                    if status == "approved":
                        conn.execute(
                            f"UPDATE projects SET digest_pending = 0, updated_at = ? "
                            f"WHERE slug IN ({marks}) COLLATE NOCASE",
                            (now, *slugs),
                        )
                        effect = "digest_approved"
                    elif status == "rejected":
                        conn.execute(
                            f"UPDATE projects SET no_crawl = 1, digest_pending = 0, updated_at = ? "
                            f"WHERE slug IN ({marks}) COLLATE NOCASE",
                            (now, *slugs),
                        )
                        effect = "digest_excluded"
                    else:
                        effect = None
                    if effect:
                        self._append_event_conn(
                            conn, f"project.{effect}", approval_id, {"slugs": slugs}, now
                        )

            # M2 council-prioritization effect: approving a ranking makes it the new pin
            # order (pin_rank 1..K, pinned=1) and unpins everything else — the ratified
            # ranking is the priority truth. Reject leaves pins unchanged.
            if action.get("kind") == "council_prioritization" and status == "approved":
                order = [str(s) for s in (action.get("order") or [])]
                if order:
                    marks = ",".join("?" for _ in order)
                    conn.execute(
                        f"UPDATE projects SET pinned = 0, pin_rank = NULL, updated_at = ? "
                        f"WHERE slug NOT IN ({marks}) COLLATE NOCASE",
                        (now, *order),
                    )
                    for rank, slug in enumerate(order, start=1):
                        conn.execute(
                            "UPDATE projects SET pinned = 1, pin_rank = ?, updated_at = ? "
                            "WHERE slug = ? COLLATE NOCASE",
                            (rank, now, slug),
                        )
                    self._append_event_conn(
                        conn, "priority.reprioritized", approval_id, {"order": order}, now
                    )

            # M6.4 agent-proposed reprioritize: the chat model can only PROPOSE this card; the pin
            # mutation happens HERE, on human approval, never inside the model tool loop. Single
            # project (low blast radius) vs the portfolio-wide council_prioritization rewrite above.
            if action.get("kind") == "agent_reprioritize" and status == "approved":
                slug = str(action.get("slug") or "").strip()
                rank = action.get("rank")
                if slug:
                    conn.execute(
                        "UPDATE projects SET pinned = 1, pin_rank = ?, updated_at = ? "
                        "WHERE slug = ? COLLATE NOCASE",
                        (int(rank) if rank is not None else None, now, slug),
                    )
                    self._append_event_conn(
                        conn, "priority.reprioritized", approval_id,
                        {"slug": slug, "rank": rank, "source": "agent"}, now
                    )

            resolved = self._get_approval_conn(conn, approval_id)
            self._append_event_conn(
                conn,
                "approval.resolved",
                approval_id,
                {
                    "approval": resolved,
                    "decision": decision,
                    "note": note,
                    "approved_by": device["name"],
                },
                now,
            )
        # Post-transaction: a Fable tie-break does network I/O — run it OUTSIDE the DB transaction
        # so a ~30s model call never holds the write lock. Only fires on human approval.
        if action.get("kind") == "fable_escalation":
            if status == "approved":
                try:
                    self._run_fable_escalation(action)
                except Exception as exc:  # accounting/side-effect — never break the approval
                    self.append_event("council.escalation.failed",
                                      str(action.get("session_id") or approval_id), {"error": str(exc)})
            elif status == "rejected":
                self.append_event("council.escalation.declined",
                                  str(action.get("session_id") or approval_id), {"approval_id": approval_id})

        # M6.4b agent-proposed SAFE writes: applied post-transaction (each store method opens its own
        # connection) via the validated methods. Model can only PROPOSE; these run only on human approval.
        if status == "approved" and action.get("kind") in ("agent_exclude", "agent_set_context"):
            try:
                if action["kind"] == "agent_exclude":
                    self.set_excluded(str(action.get("slug") or ""), bool(action.get("excluded")))
                else:
                    self.set_context(str(action.get("slug") or ""), notes=action.get("notes"))
            except Exception as exc:  # side-effect — never break the approval; audit the failure
                self.append_event("agent.write.failed", approval_id,
                                  {"kind": action.get("kind"), "error": str(exc)})

        # Cross-machine CONTROL: on approval, write a command row the remote host polls for.
        # Nothing executes here — BigBoss only records intent; the remote host pulls + acts + acks.
        if status == "approved" and action.get("kind") == "remote_command":
            try:
                self.record_remote_command({
                    "host": action.get("host"), "action": action.get("action"),
                    "args": action.get("args") or {},
                })
            except Exception as exc:
                self.append_event("agent.write.failed", approval_id,
                                  {"kind": "remote_command", "error": str(exc)})

        # M6.4b-reap: kill orphaned processes ON APPROVAL — post-txn, and RE-VALIDATED here
        # (a PID could have been reused between propose and approve). Kill a proposed target
        # only if it is STILL an orphan now AND its create_time still matches (PID-reuse guard).
        if status == "approved" and action.get("kind") == "reap_process":
            try:
                self._apply_reap(approval_id, action.get("targets") or [])
            except Exception as exc:
                self.append_event("agent.write.failed", approval_id,
                                  {"kind": "reap_process", "error": str(exc)})

        # P-ops.2b: restart a daemon ON APPROVAL. BigBoss runs UNELEVATED — an elevated daemon is
        # NOT restarted here (and success is never faked); its intent + paste-block are recorded for
        # the operator to run in an elevated shell. A non-elevated daemon's restart_cmd runs directly.
        if status == "approved" and action.get("kind") == "daemon_restart":
            try:
                self._apply_daemon_restart(approval_id, action)
            except Exception as exc:
                self.append_event("agent.write.failed", approval_id,
                                  {"kind": "daemon_restart", "error": str(exc)})
        return resolved

    def _apply_daemon_restart(self, approval_id: str, action: dict[str, Any]) -> None:
        name = str(action.get("name") or "")
        restart_cmd = str(action.get("restart_cmd") or "")
        if bool(action.get("restart_elevated")):
            # Cannot self-elevate — record the approved intent + the exact paste-block for the operator.
            project = str(action.get("project") or "")
            paste = (f"cd '{project}'; {restart_cmd}" if project else restart_cmd)
            self.append_event("daemon.restart.elevation_required", approval_id,
                              {"name": name, "paste_block": paste})
            return
        import subprocess
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", restart_cmd],
                                  capture_output=True, text=True, timeout=60)
            self.append_event("daemon.restarted", approval_id,
                              {"name": name, "returncode": proc.returncode,
                               "stderr": (proc.stderr or "")[:500]})
        except (subprocess.SubprocessError, OSError) as exc:
            self.append_event("agent.write.failed", approval_id,
                              {"kind": "daemon_restart", "name": name, "error": str(exc)})

    def _apply_reap(self, approval_id: str, targets: list[dict[str, Any]]) -> None:
        """Terminate proposed orphans, re-validating orphan-ness + create_time at approval time."""
        from . import procman
        procs = procman.list_processes()
        protect = procman.current_protect_pids(procs)
        fresh = {p.pid: p for p in procman.find_orphans(procs, protect_pids=protect)}
        for t in targets:
            pid = int(t.get("pid") or 0)
            cur = fresh.get(pid)
            # Skip (never kill) unless the PID is still an orphan with the SAME create_time.
            if cur is None or (t.get("create_time") and cur.create_time
                               and int(t["create_time"]) != int(cur.create_time)):
                self.append_event("reap.skipped", approval_id,
                                  {"pid": pid, "reason": "no longer an orphan / PID reused"})
                continue
            from .ops import pid_is_alive, terminate_pid
            ok = terminate_pid(pid) if pid_is_alive(pid) else True
            self.append_event("reap.executed", approval_id,
                              {"pid": pid, "name": t.get("name"), "reaped": bool(ok)})

    def _run_fable_escalation(self, action: dict[str, Any]) -> dict[str, Any]:
        """Approved a `fable_escalation` card → run Fable BLIND + fold the verdict into the session."""
        return self.run_tiebreak_now(
            action.get("session_id"), question=action.get("question"),
            contested=action.get("contested"), trigger=action.get("trigger"), reason=action.get("reason"),
        )

    def run_tiebreak_now(self, session_id: str | None, *, question: str | None,
                         contested: list | None, trigger: str | None = None,
                         reason: str | None = None) -> dict[str, Any]:
        """Run the Fable tie-break and fold its verdict + exact cost into the council session.
        Shared by the approval-card path and the opt-in inline (autotiebreak) path. Lazy-imports the
        council so store keeps no hard dependency on it. Returns the verdict dict."""
        from .council.escalation import run_tiebreak

        report = {
            "question": question or "",
            "verified_claims": [{"text": t, "verdict": "refuted"} for t in (contested or [])],
        }
        verdict = run_tiebreak(report)
        now = iso_now()
        if verdict.get("status") != "success":
            self.append_event("council.escalation.failed", str(session_id or ""),
                              {"error": verdict.get("error")})
            return verdict
        with self.connect() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT cost_usd FROM council_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is not None:
                    try:
                        new_cost = float(row["cost_usd"] or 0) + float(verdict.get("cost_usd") or 0)
                    except (TypeError, ValueError):
                        new_cost = float(verdict.get("cost_usd") or 0)
                    conn.execute(
                        "UPDATE council_sessions SET final_answer = ?, cost_usd = ?, escalation_json = ? "
                        "WHERE id = ?",
                        (verdict.get("answer") or "", str(new_cost),
                         json.dumps({"resolved": True, "trigger": trigger, "reason": reason,
                                     "model": verdict.get("model"), "cost_usd": verdict.get("cost_usd")}),
                         session_id),
                    )
            self._append_event_conn(
                conn, "council.escalation.resolved", str(session_id or ""),
                {"model": verdict.get("model"), "cost_usd": verdict.get("cost_usd")}, now,
            )
        return verdict

    def council_eval_stats(self, days: int = 30) -> dict[str, Any]:
        """Escalation + cost aggregates over a window (feeds `council-eval`): how often the council was
        flagged unsure, the trigger mix, how many actually escalated to Fable, and the Fable/total cost."""
        since = _iso(utc_now() - timedelta(days=days))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT mode, escalation_json, cost_usd FROM council_sessions WHERE created_at >= ?",
                (since,),
            ).fetchall()
        flagged = resolved = 0
        triggers: dict[str, int] = {}
        fable_cost = total_cost = 0.0
        for r in rows:
            try:
                esc = json.loads(r["escalation_json"] or "{}")
            except (TypeError, ValueError):
                esc = {}
            try:
                total_cost += float(r["cost_usd"] or 0)
            except (TypeError, ValueError):
                pass
            if esc.get("escalate") or esc.get("resolved"):
                flagged += 1
                if esc.get("trigger"):
                    triggers[esc["trigger"]] = triggers.get(esc["trigger"], 0) + 1
            if esc.get("resolved"):
                resolved += 1
                try:
                    fable_cost += float(esc.get("cost_usd") or 0)
                except (TypeError, ValueError):
                    pass
        n = len(rows)
        return {
            "days": days, "sessions": n,
            "flagged_unsure": flagged, "escalation_rate": (flagged / n if n else 0.0),
            "resolved_by_fable": resolved, "triggers": triggers,
            "fable_cost_usd": round(fable_cost, 4), "total_cost_usd": round(total_cost, 4),
        }

    def wait_for_resolution(self, approval_id: str, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            approval = self.get_approval(approval_id)
            if approval and approval["status"] != "pending":
                return approval
            time.sleep(0.5)
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError("Approval not found.")
        return approval

    def append_event(self, event_type: str, aggregate_id: str, payload: dict[str, Any]) -> None:
        now = iso_now()
        with self.connect() as conn:
            self._append_event_conn(conn, event_type, aggregate_id, payload, now)

    def events_after(self, last_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (last_id, limit),
            ).fetchall()
            return [self._row_to_event(row) for row in rows]

    # -- Router budget ledger (Ecosystem Phase E1) ----------------------------

    def _current_period_conn(
        self, conn: sqlite3.Connection, now_dt: datetime | None = None, hard_cap: str | None = None
    ) -> sqlite3.Row:
        now_dt = now_dt or utc_now()
        period = now_dt.strftime("%Y-%m")
        cap = str(hard_cap) if hard_cap is not None else DEFAULT_HARD_CAP_USD
        ts = iso_now()
        # INSERT OR IGNORE is idempotent and race-safe (two threads creating the
        # first-of-month period row cannot collide on the UNIQUE period key).
        conn.execute(
            """
            INSERT OR IGNORE INTO budget_periods (period, hard_cap_usd, spent_usd, alert_state, created_at, updated_at)
            VALUES (?, ?, '0', 'none', ?, ?)
            """,
            (period, cap, ts, ts),
        )
        return conn.execute("SELECT * FROM budget_periods WHERE period = ?", (period,)).fetchone()

    def check_budget(self, hard_cap: str | None = None) -> dict[str, Any]:
        """Current-period spend snapshot. `blocked` is informational in E1 (advisory cap)."""
        with self.connect() as conn:
            row = self._current_period_conn(conn, hard_cap=hard_cap)
            spent = Decimal(row["spent_usd"])
            cap = Decimal(row["hard_cap_usd"])
            return {
                "period": row["period"],
                "spent_usd": str(spent),
                "hard_cap_usd": str(cap),
                "remaining_usd": str(cap - spent),
                "alert_state": row["alert_state"],
                "blocked": cap > 0 and spent >= cap,
            }

    def set_hard_cap(self, cap: str) -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            row = self._current_period_conn(conn)
            conn.execute(
                "UPDATE budget_periods SET hard_cap_usd = ?, updated_at = ? WHERE period = ?",
                (str(cap), now, row["period"]),
            )
        return self.check_budget()

    def record_router_call(
        self,
        *,
        requested_model: str,
        served_model: str,
        usage: dict[str, Any] | None,
        cost_usd: Any,
        harness: str = "claude-code",
        tier_reason: str = "passthrough",
        streamed: bool = False,
        stop_reason: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one proxied call to the ledger, bump the period total, and emit
        routing.decided / spend.recorded (+ budget.alert on a threshold crossing)."""
        usage = usage or {}
        cost = Decimal(str(cost_usd))
        rc_id = new_token("rc")
        now = iso_now()
        with self.connect() as conn:
            period_row = self._current_period_conn(conn)
            conn.execute(
                """
                INSERT INTO router_calls (
                    id, run_id, harness, requested_model, served_model, tier_reason,
                    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                    cost_usd, streamed, stop_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rc_id,
                    run_id,
                    harness,
                    requested_model,
                    served_model,
                    tier_reason,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                    int(usage.get("cache_read_input_tokens") or 0),
                    str(cost),
                    1 if streamed else 0,
                    stop_reason,
                    now,
                ),
            )
            prev_total = Decimal(period_row["spent_usd"])
            new_total = prev_total + cost
            cap = Decimal(period_row["hard_cap_usd"])
            conn.execute(
                "UPDATE budget_periods SET spent_usd = ?, updated_at = ? WHERE period = ?",
                (str(new_total), now, period_row["period"]),
            )
            self._append_event_conn(
                conn,
                "routing.decided",
                rc_id,
                {
                    "router_call_id": rc_id,
                    "requested_model": requested_model,
                    "served_model": served_model,
                    "tier_reason": tier_reason,
                    "harness": harness,
                    "rerouted": bool(
                        served_model and requested_model and served_model != requested_model
                    ),
                },
                now,
            )
            self._append_event_conn(
                conn,
                "spend.recorded",
                rc_id,
                {
                    "router_call_id": rc_id,
                    "cost_usd": str(cost),
                    "usage": usage,
                    "period": period_row["period"],
                    "period_spent_usd": str(new_total),
                    "hard_cap_usd": str(cap),
                },
                now,
            )
            crossed, new_state, label = self._alert_crossing(
                period_row["alert_state"], new_total, cap
            )
            if crossed:
                conn.execute(
                    "UPDATE budget_periods SET alert_state = ?, updated_at = ? WHERE period = ?",
                    (new_state, now, period_row["period"]),
                )
                self._append_event_conn(
                    conn,
                    "budget.alert",
                    period_row["period"],
                    {
                        "period": period_row["period"],
                        "threshold": label,
                        "spent_usd": str(new_total),
                        "hard_cap_usd": str(cap),
                    },
                    now,
                )
        return {
            "router_call_id": rc_id,
            "cost_usd": str(cost),
            "period": period_row["period"],
            "period_spent_usd": str(new_total),
            "hard_cap_usd": str(cap),
            "alert": label if crossed else None,
        }

    @staticmethod
    def _alert_crossing(
        current_state: str, new_total: Decimal, cap: Decimal
    ) -> tuple[bool, str, str | None]:
        if cap <= 0:
            return (False, current_state, None)
        frac = new_total / cap
        if new_total >= cap:
            target = "over"
        elif frac >= Decimal("0.90"):
            target = "p90"
        elif frac >= Decimal("0.75"):
            target = "p75"
        elif frac >= Decimal("0.50"):
            target = "p50"
        else:
            target = "none"
        if _ALERT_ORDER.index(target) > _ALERT_ORDER.index(current_state):
            return (True, target, _ALERT_LABELS.get(target))
        return (False, current_state, None)

    # -- Project registry (Ecosystem Phase E3) --------------------------------

    def apply_reconciled(
        self, resolved: list[Any], prune_local: bool = False, prune_host: str | None = None
    ) -> dict[str, Any]:
        """Persist a list of canonical.ResolvedProject: upsert projects (preserving pin
        state, first-seen, and curated purpose/domain), refresh aliases, emit events.

        prune_local=True tombstones LOCAL projects (canonical_path not starting with '//')
        that were absent from this reconcile — used by the local harvest refresh.
        prune_host='<host>' tombstones only that host's remote rows ('//<host>/...') absent
        from this reconcile — used by a full-sync remote bundle ingest. The two scopes are
        disjoint, so neither can retire the other's rows. Pinned rows are never tombstoned."""
        now = iso_now()
        discovered = updated = 0
        with self.connect() as conn:
            for r in resolved:
                markers = getattr(r, "markers", {}) or {}
                kind = _infer_kind(markers)
                row = conn.execute(
                    "SELECT * FROM projects WHERE canonical_path = ?", (r.canonical_path,)
                ).fetchone()
                if row is None:
                    project_id = new_token("prj")
                    conn.execute(
                        # digest_pending = 1: a newly-discovered project is gated from
                        # off-box digestion until a human approves the batch (E4a.1).
                        """
                        INSERT INTO projects (
                            id, slug, name, canonical_path, kind, purpose, domain, status,
                            pinned, pin_rank, is_daemonized, git_commit_count, last_activity_at,
                            last_git_commit_at, last_transcript_at, last_handoff_at, markers_json,
                            digest_pending, first_seen_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            project_id,
                            r.slug,
                            r.slug,
                            r.canonical_path,
                            kind,
                            r.purpose,
                            r.domain,
                            r.status,
                            1 if r.is_daemonized else 0,
                            r.git_commit_count,
                            r.last_activity_at,
                            r.last_git_commit_at,
                            r.last_transcript_at,
                            r.last_handoff_at,
                            json.dumps(markers, sort_keys=True),
                            now,
                            now,
                            now,
                        ),
                    )
                    self._append_event_conn(
                        conn,
                        "project.discovered",
                        project_id,
                        {"canonical_path": r.canonical_path, "slug": r.slug, "status": r.status},
                        now,
                    )
                    discovered += 1
                else:
                    project_id = row["id"]
                    purpose = row["purpose"] or r.purpose  # keep curated text
                    domain = row["domain"] or r.domain
                    conn.execute(
                        """
                        UPDATE projects SET slug=?, kind=?, purpose=?, domain=?, status=?,
                            is_daemonized=?, git_commit_count=?, last_activity_at=?, last_git_commit_at=?,
                            last_transcript_at=?, last_handoff_at=?, markers_json=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            r.slug,
                            kind,
                            purpose,
                            domain,
                            r.status,
                            1 if r.is_daemonized else 0,
                            r.git_commit_count,
                            r.last_activity_at,
                            r.last_git_commit_at,
                            r.last_transcript_at,
                            r.last_handoff_at,
                            json.dumps(markers, sort_keys=True),
                            now,
                            project_id,
                        ),
                    )
                    self._append_event_conn(
                        conn,
                        "project.updated",
                        project_id,
                        {"canonical_path": r.canonical_path},
                        now,
                    )
                    updated += 1

                # Refresh this project's aliases (idempotent).
                conn.execute("DELETE FROM project_aliases WHERE project_id = ?", (project_id,))
                for alias in getattr(r, "aliases", []) or []:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO project_aliases (id, project_id, alias_value, alias_kind, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_token("als"),
                            project_id,
                            alias["alias_value"],
                            alias["alias_kind"],
                            alias.get("source", "fs-scan"),
                            now,
                        ),
                    )

            pruned = 0
            if prune_local or prune_host:
                # Both sides are already canonical.normalize()d (resolved from
                # ResolvedProject, DB rows stored from the same), so compare directly.
                seen = {r.canonical_path for r in resolved}
                if prune_host:
                    prefix = f"//{prune_host}/"
                    # Only this host's remote rows — disjoint from the local scope and
                    # from other hosts.
                    stale = conn.execute(
                        """
                        SELECT id, canonical_path FROM projects
                        WHERE substr(canonical_path, 1, ?) = ?
                          AND pinned = 0 AND status != 'gone'
                        """,
                        (len(prefix), prefix),
                    ).fetchall()
                    reason = "absent-from-bundle"
                else:
                    # Only local rows (canonical_path not starting with '//').
                    stale = conn.execute(
                        """
                        SELECT id, canonical_path FROM projects
                        WHERE substr(canonical_path, 1, 2) != '//'
                          AND pinned = 0 AND status != 'gone'
                        """
                    ).fetchall()
                    reason = "absent-from-refresh"
                for srow in stale:
                    if srow["canonical_path"] in seen:
                        continue
                    conn.execute(
                        "UPDATE projects SET status = 'gone', updated_at = ? WHERE id = ?",
                        (now, srow["id"]),
                    )
                    self._append_event_conn(
                        conn,
                        "project.retired",
                        srow["id"],
                        {"canonical_path": srow["canonical_path"], "reason": reason},
                        now,
                    )
                    pruned += 1

            summary = {"discovered": discovered, "updated": updated, "total": len(resolved), "pruned": pruned}
            self._append_event_conn(conn, "registry.refreshed", "registry", summary, now)
        return summary

    def list_projects(self, include_ambiguous: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM projects
                ORDER BY pinned DESC, pin_rank IS NULL, pin_rank ASC, last_activity_at DESC, slug ASC
                """
            ).fetchall()
            return [
                self._row_to_project(row, conn)
                for row in rows
                if include_ambiguous or row["status"] not in ("ambiguous", "gone")
            ]

    def set_pin(self, project_id: str, pinned: bool, pin_rank: int | None = None) -> dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            if (
                conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
                is None
            ):
                raise KeyError("Project not found.")
            conn.execute(
                "UPDATE projects SET pinned = ?, pin_rank = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, pin_rank, now, project_id),
            )
            self._append_event_conn(
                conn,
                "priority.set",
                project_id,
                {"pinned": bool(pinned), "pin_rank": pin_rank},
                now,
            )
            return self._row_to_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone(), conn
            )

    def set_excluded(self, slug: str, excluded: bool) -> dict[str, Any]:
        """Hand-exclude a project from portfolio prioritization (or re-include it). Unlike
        no_crawl this keeps intel/egress intact — it only removes the project from the Council's
        prioritization context (build_portfolio_context). Durable across registry-refresh."""
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM projects WHERE slug = ? COLLATE NOCASE", (slug,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Project not found: {slug}")
            conn.execute(
                "UPDATE projects SET excluded = ?, updated_at = ? WHERE id = ?",
                (1 if excluded else 0, now, row["id"]),
            )
            self._append_event_conn(
                conn,
                "priority.excluded" if excluded else "priority.included",
                row["id"],
                {"slug": slug},
                now,
            )
            return self._row_to_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone(), conn
            )

    LIFECYCLE_VALUES = ("", "active", "done", "archived", "dormant", "experiment")
    OWNERSHIP_VALUES = ("", "personal", "work", "third-party")

    def set_baseline(
        self,
        slug: str,
        *,
        purpose: str | None = None,
        lifecycle: str | None = None,
        ownership: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """Establish an agent-derived ground-truth baseline on a project (deep re-baseline). Writes the
        durable descriptive fields — purpose/domain (authoritative, overwrites), lifecycle, ownership —
        none of which registry-refresh reconciliation clobbers. Stamps archived_at when
        lifecycle marks completion. Emits a `baseline.set` event. Does NOT touch `excluded` (the ratified
        prioritization filter stays a separate, human-confirmed decision)."""
        if lifecycle is not None and lifecycle not in self.LIFECYCLE_VALUES:
            raise ValueError(f"lifecycle must be one of {self.LIFECYCLE_VALUES}, got {lifecycle!r}")
        if ownership is not None and ownership not in self.OWNERSHIP_VALUES:
            raise ValueError(f"ownership must be one of {self.OWNERSHIP_VALUES}, got {ownership!r}")
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE slug = ? COLLATE NOCASE", (slug,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Project not found: {slug}")
            sets: list[str] = []
            params: list[Any] = []
            for col, val in (("purpose", purpose), ("domain", domain),
                             ("lifecycle", lifecycle), ("ownership", ownership)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            eff_lifecycle = lifecycle if lifecycle is not None else row["lifecycle"]
            if eff_lifecycle in ("done", "archived") and not row["archived_at"]:
                sets.append("archived_at = ?")
                params.append(now)
            sets.append("updated_at = ?")
            params.append(now)
            params.append(row["id"])
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
            self._append_event_conn(
                conn, "baseline.set", row["id"],
                {"slug": slug, "lifecycle": eff_lifecycle,
                 "ownership": ownership if ownership is not None else row["ownership"]}, now,
            )
            return self._row_to_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone(), conn
            )

    def set_context(
        self,
        slug: str,
        *,
        notes: str | None = None,
        supersedes: Any = None,
        evolved_into: Any = None,
        track_it: bool | None = None,
        track_note: str | None = None,
    ) -> dict[str, Any]:
        """Attach the operator's own project context (portfolio v2): authoritative notes, lineage
        (supersedes/evolved_into — accept a list or comma-string of slugs), and the
        complicated/sensitive track-it flag. All refresh-durable. Emits `project.context.set`."""
        def _slugs(v: Any) -> str | None:
            if v is None:
                return None
            if isinstance(v, (list, tuple)):
                return ",".join(str(x).strip() for x in v if str(x).strip())
            return str(v).strip()

        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM projects WHERE slug = ? COLLATE NOCASE", (slug,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Project not found: {slug}")
            sets: list[str] = []
            params: list[Any] = []
            for col, val in (("notes", notes), ("supersedes", _slugs(supersedes)),
                             ("evolved_into", _slugs(evolved_into)), ("track_note", track_note)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            if track_it is not None:
                sets.append("track_it = ?")
                params.append(1 if track_it else 0)
            if not sets:
                raise ValueError("set_context called with no fields to update.")
            sets.append("updated_at = ?")
            params.append(now)
            params.append(row["id"])
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)
            self._append_event_conn(
                conn, "project.context.set", row["id"],
                {"slug": slug, "track_it": bool(track_it) if track_it is not None else None}, now,
            )
            return self._row_to_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone(), conn
            )

    def set_no_crawl(self, slug: str, no_crawl: bool) -> dict[str, Any]:
        """Mark a project crawl-excluded (or re-allow it). Excluding also redacts
        any brainz-style data already at rest: stored egress prompt/response text
        for the project is blanked (audit metadata kept) and its digested intel
        snapshot is dropped, so exclusion is retroactive, not just forward-looking."""
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE slug = ? COLLATE NOCASE", (slug,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No registered project with slug {slug!r}.")
            project_id = row["id"]
            conn.execute(
                "UPDATE projects SET no_crawl = ?, updated_at = ? WHERE id = ?",
                (1 if no_crawl else 0, now, project_id),
            )
            redacted = 0
            if no_crawl:
                redacted = conn.execute(
                    """
                    UPDATE egress_log
                    SET prompt_text = '', response_text = ''
                    WHERE project_id = ? AND (prompt_text != '' OR response_text != '')
                    """,
                    (project_id,),
                ).rowcount
                conn.execute("DELETE FROM project_intel WHERE project_id = ?", (project_id,))
            self._append_event_conn(
                conn,
                "project.crawl_excluded" if no_crawl else "project.crawl_allowed",
                project_id,
                {"slug": row["slug"], "no_crawl": bool(no_crawl), "egress_rows_redacted": redacted},
                now,
            )
            project = self._row_to_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone(), conn
            )
        project["egress_rows_redacted"] = redacted
        return project

    # -- Digest gating (E4a.1): new discoveries wait for approval before off-box digest --

    def pending_digest_projects(self) -> list[dict[str, Any]]:
        """Projects gated from off-box digestion, awaiting batch approval."""
        return [
            p for p in self.list_projects(include_ambiguous=True)
            if p.get("digest_pending") and not p.get("no_crawl")
        ]

    def create_digest_batch_card(self) -> dict[str, Any] | None:
        """Raise ONE approval card for the current digest-gated projects, unless there
        is nothing pending or a digest-batch card is already open. Approving it clears
        the gate (they digest next cycle); rejecting excludes them (see resolve_approval)."""
        pending = self.pending_digest_projects()
        if not pending:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT proposed_action_json FROM approval_requests WHERE status = 'pending'"
            ).fetchall()
        for r in rows:
            try:
                if json.loads(r["proposed_action_json"]).get("kind") == "digest_batch":
                    return None  # a batch card is already open
            except (ValueError, TypeError):
                pass
        slugs = sorted(p["slug"] for p in pending)
        return self.create_approval_request(
            {
                "harness": "bigboss",
                "run_title": "Registry discovery",
                "title": f"{len(slugs)} new project(s) discovered — approve off-box digest?",
                "summary": (
                    "New projects were auto-discovered. Approving allows their planning docs "
                    "to be digested off-box (Squire). Rejecting excludes them (no_crawl). "
                    f"Projects: {', '.join(slugs)}"
                ),
                "proposed_action": {"kind": "digest_batch", "slugs": slugs},
            }
        )

    def clear_digest_pending(self, slugs: list[str] | None = None) -> int:
        """Manually clear the digest gate (allow off-box digest) for the given slugs, or
        all pending if None. Returns the count cleared. CLI fallback for the approval card."""
        now = iso_now()
        with self.connect() as conn:
            if slugs:
                marks = ",".join("?" for _ in slugs)
                cur = conn.execute(
                    f"UPDATE projects SET digest_pending = 0, updated_at = ? "
                    f"WHERE digest_pending = 1 AND slug IN ({marks}) COLLATE NOCASE",
                    (now, *slugs),
                )
            else:
                cur = conn.execute(
                    "UPDATE projects SET digest_pending = 0, updated_at = ? WHERE digest_pending = 1",
                    (now,),
                )
            count = cur.rowcount
            if count:
                self._append_event_conn(
                    conn, "project.digest_approved", "registry", {"slugs": slugs or "all"}, now
                )
        return count

    # -- Council (Ecosystem Phase M1) -----------------------------------------

    def record_council_session(self, report: dict[str, Any], cost_usd: str = "0") -> dict[str, Any]:
        """Persist a council deliberation: one council_sessions row + one
        council_model_scores row per model (the throne's track record), + council.* events."""
        now = iso_now()
        session_id = new_token("cnc")
        usage = report.get("usage") or {}
        scores = report.get("per_model_scores") or {}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO council_sessions (
                    id, mode, question, seats_json, winner, final_answer, per_model_scores_json,
                    verified_claims_json, tokens_in, tokens_out, cost_usd, created_at, ideation_json,
                    escalation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(report.get("mode") or "live"),
                    str(report.get("question") or ""),
                    json.dumps(report.get("seats") or []),
                    report.get("winner"),
                    str((report.get("final") or {}).get("final_answer") or ""),
                    json.dumps(scores, sort_keys=True),
                    json.dumps(report.get("verified_claims") or []),
                    int(usage.get("input") or 0),
                    int(usage.get("output") or 0),
                    str(cost_usd),
                    now,
                    json.dumps(report.get("ideation") or {}),
                    json.dumps(report.get("escalation") or {}),
                ),
            )
            for model_id, s in scores.items():
                conn.execute(
                    """
                    INSERT INTO council_model_scores (
                        id, session_id, model_id, model, verified, partial, total, score, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_token("cms"),
                        session_id,
                        model_id,
                        str(s.get("model") or ""),
                        int(s.get("verified") or 0),
                        int(s.get("partial") or 0),
                        int(s.get("total") or 0),
                        float(s.get("score") or 0),
                        now,
                    ),
                )
            self._append_event_conn(
                conn, "council.session.answered", session_id,
                {"winner": report.get("winner"), "seats": report.get("seats") or [],
                 "tokens": usage, "mode": report.get("mode")}, now,
            )
            self._append_event_conn(
                conn, "council.score.recorded", session_id, {"scores": scores}, now
            )
        return {"session_id": session_id}

    def record_project_research(self, project_id: str, slug: str, brief: dict[str, Any],
                                model: str, usage: dict[str, Any], cost_usd: str = "0") -> dict[str, Any]:
        """Persist a Fable-led deep-research brief (+ phased roadmap) for one project."""
        now = iso_now()
        rid = new_token("rsr")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO project_research (
                    id, project_id, slug, model, brief_json, tokens_in, tokens_out, cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, project_id, slug, model, json.dumps(brief),
                 int(usage.get("input") or 0), int(usage.get("output") or 0), str(cost_usd), now),
            )
            self._append_event_conn(
                conn, "project.research.recorded", project_id, {"slug": slug, "model": model}, now
            )
        return {"research_id": rid}

    def record_harness_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a cross-vendor harness session-report (what a session accomplished).

        Defensive about input (any harness/skill/CLI can call it). If ``session_ref``
        is supplied and already recorded, the prior row is updated in place so a
        re-report is idempotent; otherwise a new row is inserted.
        """
        harness = str(payload.get("harness") or "unknown")
        vendor = str(payload.get("vendor") or "")
        project = str(payload.get("project") or "")
        workspace = str(payload.get("workspace") or "")
        title = str(payload.get("title") or f"{harness} session")
        summary = str(payload.get("summary") or "")
        session_ref = str(payload.get("session_ref") or "")
        host = str(payload.get("host") or "")

        def _as_list(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value] if value else []
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value if str(v)]
            return [str(value)]

        # Known structured fields land in the blob; any extras are preserved too.
        reserved = {"harness", "vendor", "project", "workspace", "title", "summary",
                    "session_ref", "host"}
        detail = {
            "files_touched": _as_list(payload.get("files_touched")),
            "decisions": _as_list(payload.get("decisions")),
            "next_steps": _as_list(payload.get("next_steps")),
        }
        for key, value in payload.items():
            if key not in reserved and key not in detail:
                detail[key] = value
        payload_json = json.dumps(detail, sort_keys=True)

        now = iso_now()
        with self.connect() as conn:
            existing = None
            if session_ref:
                existing = conn.execute(
                    "SELECT id FROM harness_sessions WHERE session_ref = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (session_ref,),
                ).fetchone()
            if existing:
                sid = existing["id"]
                conn.execute(
                    """
                    UPDATE harness_sessions
                    SET harness = ?, vendor = ?, project = ?, workspace = ?, title = ?,
                        summary = ?, payload_json = ?, host = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (harness, vendor, project, workspace, title, summary, payload_json,
                     host, now, sid),
                )
            else:
                sid = new_token("hs")
                conn.execute(
                    """
                    INSERT INTO harness_sessions (
                        id, harness, vendor, project, workspace, title, summary,
                        payload_json, session_ref, host, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sid, harness, vendor, project, workspace, title, summary,
                     payload_json, session_ref, host, now),
                )
            self._append_event_conn(
                conn,
                "harness.session.reported",
                sid,
                {"harness": harness, "vendor": vendor, "project": project,
                 "title": title, "host": host},
                now,
            )
        return {"session_id": sid}

    def list_harness_sessions(
        self,
        days: int | None = None,
        project: str | None = None,
        harness: str | None = None,
        host: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Recent harness session-reports, newest first, optionally filtered."""
        query = "SELECT * FROM harness_sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if days is not None:
            clauses.append("created_at >= ?")
            params.append(_iso(utc_now() - timedelta(days=days)))
        if project:
            clauses.append("project = ?")
            params.append(project)
        if harness:
            clauses.append("harness = ?")
            params.append(harness)
        if host:
            clauses.append("host = ?")
            params.append(host)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_session(row) for row in rows]

    def _row_to_session(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            detail = json.loads(item.pop("payload_json"))
        except (json.JSONDecodeError, TypeError):
            detail = {}
        item["files_touched"] = detail.get("files_touched", [])
        item["decisions"] = detail.get("decisions", [])
        item["next_steps"] = detail.get("next_steps", [])
        item["detail"] = detail
        item.setdefault("host", "")
        return item

    def record_remote_snapshot(
        self, host: str, kind: str, payload: dict[str, Any], reported_at: str = ""
    ) -> dict[str, Any]:
        """Upsert the latest cross-machine activity snapshot for (host, kind).

        Current-state, not an append log: a newer snapshot replaces the prior one for
        the same host+kind. The payload is a free-form, schema-tolerant blob (e.g.
        the remote host's live Squire job + queue) — capped to keep SQLite lean. Emits
        `remote.snapshot.reported` so the dashboard can live-refresh.
        """
        host = str(host or "unknown")
        kind = str(kind or "unknown")
        payload_json = cap_egress_text(json.dumps(payload or {}, sort_keys=True))
        now = iso_now()
        reported = str(reported_at or now)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_snapshots (host, kind, payload_json, reported_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host, kind) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    reported_at  = excluded.reported_at,
                    created_at   = excluded.created_at
                """,
                (host, kind, payload_json, reported, now),
            )
            self._append_event_conn(
                conn, "remote.snapshot.reported", f"{host}:{kind}",
                {"host": host, "kind": kind}, now,
            )
        return {"host": host, "kind": kind, "reported_at": reported}

    def get_remote_snapshots(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Latest snapshot per (host, kind), newest first, with computed staleness.

        `age_seconds` is how long ago BigBoss received it. The caller MUST surface this
        provenance — a stale snapshot is not live truth (the M6.5-Light honesty rule).
        """
        query = "SELECT * FROM remote_snapshots"
        params: list[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY reported_at DESC, host ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        now = utc_now()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (json.JSONDecodeError, TypeError):
                item["payload"] = {}
            try:
                item["age_seconds"] = max(0, int((now - parse_time(item["created_at"])).total_seconds()))
            except (ValueError, TypeError):
                item["age_seconds"] = None
            out.append(item)
        return out

    # --- Cross-machine CONTROL (BigBoss → the remote host, PULL). Commands are written ONLY by
    # resolve_approval on human approval; the remote polls, executes, and acks. -------

    def record_remote_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a human-approved command for a remote host to execute. Called from
        resolve_approval, never from the model. Emits `remote.command.issued`."""
        host = str(payload.get("host") or "unknown")
        action = str(payload.get("action") or "")
        args = payload.get("args") or {}
        now = iso_now()
        cmd_id = new_token("cmd")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO remote_commands (id, host, action, args_json, status, issued_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (cmd_id, host, action, json.dumps(args, sort_keys=True), now, now),
            )
            self._append_event_conn(
                conn, "remote.command.issued", cmd_id, {"host": host, "action": action}, now
            )
        return {"command_id": cmd_id, "host": host, "action": action}

    def claim_pending_commands(self, host: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return this host's approved-but-unacked commands and mark them `claimed`.
        Delivery is at-least-once — v1 actions are idempotent, so a redelivered command
        the remote already applied is acked again without re-applying."""
        now = iso_now()
        out: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_commands WHERE host = ? AND status IN ('pending','claimed') "
                "ORDER BY created_at ASC LIMIT ?",
                (host, int(limit)),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE remote_commands SET status = 'claimed' WHERE id = ? AND status = 'pending'",
                    (row["id"],),
                )
                out.append({
                    "id": row["id"], "action": row["action"],
                    "args": json.loads(row["args_json"]) if row["args_json"] else {},
                    "issued_at": row["issued_at"],
                })
            self._append_event_conn(conn, "remote.commands.claimed", host, {"count": len(out)}, now)
        return out

    def ack_command(self, command_id: str, status: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the remote's outcome for a command. Acking a non-open command is a safe
        no-op (idempotent). `status` is 'acked' or 'failed'. Emits `remote.command.acked`."""
        status = "failed" if str(status).lower() == "failed" else "acked"
        now = iso_now()
        result_json = cap_egress_text(json.dumps(result or {}, sort_keys=True))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM remote_commands WHERE id = ?", (command_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "unknown command"}
            if row["status"] in ("acked", "failed"):
                return {"ok": True, "already": True, "status": row["status"]}
            conn.execute(
                "UPDATE remote_commands SET status = ?, result_json = ?, acked_at = ? WHERE id = ?",
                (status, result_json, now, command_id),
            )
            self._append_event_conn(
                conn, "remote.command.acked", command_id, {"status": status}, now
            )
        return {"ok": True, "status": status}

    def list_remote_commands(self, host: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Recent commands (read view for CLI/dashboard), newest first."""
        query = "SELECT * FROM remote_commands"
        params: list[Any] = []
        if host:
            query += " WHERE host = ?"
            params.append(host)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["args"] = json.loads(item.pop("args_json")) if item.get("args_json") else {}
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else {}
            out.append(item)
        return out

    def council_model_leaderboard(self, days: int = 90) -> list[dict[str, Any]]:
        """Aggregate the accuracy track record per model (M4 throne-election input)."""
        since = _iso(utc_now() - timedelta(days=days))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT model_id,
                       COUNT(*) AS sessions,
                       SUM(verified) AS verified, SUM(partial) AS partial, SUM(total) AS total,
                       AVG(score) AS avg_score
                FROM council_model_scores
                WHERE created_at >= ?
                GROUP BY model_id
                ORDER BY avg_score DESC
                """,
                (since,),
            ).fetchall()
        return [
            {
                "model_id": r["model_id"],
                "sessions": r["sessions"],
                "verified": r["verified"] or 0,
                "partial": r["partial"] or 0,
                "total": r["total"] or 0,
                "avg_score": round(r["avg_score"] or 0.0, 1),
            }
            for r in rows
        ]

    # -- Project intel + Squire ledger (Ecosystem Phase E4a) -------------------

    def upsert_project_intel(self, project_id: str, intel: dict[str, Any]) -> dict[str, Any]:
        """Persist one digested intel snapshot for a project and emit intel.updated.
        A successful digest clears any previous error."""
        now = iso_now()
        with self.connect() as conn:
            if (
                conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
                is None
            ):
                raise KeyError("Project not found.")
            conn.execute(
                """
                INSERT INTO project_intel (
                    project_id, status_line, blockers_json, roadmap_now, roadmap_next,
                    source_hash, source_files_json, model, error, generated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status_line = excluded.status_line,
                    blockers_json = excluded.blockers_json,
                    roadmap_now = excluded.roadmap_now,
                    roadmap_next = excluded.roadmap_next,
                    source_hash = excluded.source_hash,
                    source_files_json = excluded.source_files_json,
                    model = excluded.model,
                    error = NULL,
                    generated_at = excluded.generated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    str(intel.get("status_line") or ""),
                    json.dumps(list(intel.get("blockers") or []), sort_keys=True),
                    str(intel.get("roadmap_now") or ""),
                    str(intel.get("roadmap_next") or ""),
                    str(intel.get("source_hash") or ""),
                    json.dumps(list(intel.get("source_files") or []), sort_keys=True),
                    str(intel.get("model") or ""),
                    now,
                    now,
                    now,
                ),
            )
            stored = self._get_intel_conn(conn, project_id)
            self._append_event_conn(
                conn,
                "intel.updated",
                project_id,
                {
                    "project_id": project_id,
                    "status_line": stored["status_line"],
                    "blockers": stored["blockers"],
                },
                now,
            )
        return stored

    def mark_intel_error(self, project_id: str, error: str) -> None:
        """Record a digest failure without discarding the last good snapshot."""
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO project_intel (project_id, error, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET error = excluded.error, updated_at = excluded.updated_at
                """,
                (project_id, error[:500], now, now),
            )

    def _get_intel_conn(self, conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM project_intel WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "status_line": row["status_line"],
            "blockers": json.loads(row["blockers_json"]),
            "roadmap_now": row["roadmap_now"],
            "roadmap_next": row["roadmap_next"],
            "source_hash": row["source_hash"],
            "source_files": json.loads(row["source_files_json"]),
            "model": row["model"],
            "error": row["error"],
            "generated_at": row["generated_at"],
            "updated_at": row["updated_at"],
        }

    def record_squire_call(
        self,
        *,
        client: str,
        purpose: str,
        endpoint: str = "squire",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        ok: bool = True,
        error: str | None = None,
    ) -> str:
        """Append one Squire call to the ledger. Health probes are ledgered but do
        not emit per-call events (they would flood the phone SSE feed)."""
        call_id = new_token("sq")
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO squire_calls (
                    id, endpoint, client, purpose, model, prompt_tokens, completion_tokens,
                    latency_ms, ok, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    endpoint or "squire",
                    client or "unknown",
                    purpose,
                    model,
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(latency_ms),
                    1 if ok else 0,
                    error,
                    now,
                ),
            )
            if purpose != "health":
                self._append_event_conn(
                    conn,
                    "squire.call.recorded",
                    call_id,
                    {
                        "endpoint": endpoint or "squire",
                        "client": client or "unknown",
                        "purpose": purpose,
                        "model": model,
                        "prompt_tokens": int(prompt_tokens),
                        "completion_tokens": int(completion_tokens),
                        "ok": bool(ok),
                    },
                    now,
                )
        return call_id

    def record_egress(
        self,
        *,
        endpoint: str,
        exposure: str = "",
        base_url: str = "",
        model: str = "",
        client: str = "",
        purpose: str = "",
        project_id: str | None = None,
        project_slug: str = "",
        source_files: list[str] | None = None,
        content_hash: str = "",
        chars_sent: int = 0,
        tokens_sent: int = 0,
        tokens_received: int = 0,
        prompt_text: str = "",
        response_text: str = "",
        ok: bool = True,
    ) -> str:
        """Append one row to the data-egress audit: the readable prompt and response
        that crossed to a compute endpoint on another machine."""
        egress_id = new_token("eg")
        now = iso_now()
        prompt = cap_egress_text(prompt_text or "")
        response = cap_egress_text(response_text or "")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO egress_log (
                    id, endpoint, exposure, base_url, model, client, purpose,
                    project_id, project_slug, source_files_json, content_hash,
                    chars_sent, tokens_sent, tokens_received, prompt_text, response_text,
                    ok, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    egress_id,
                    endpoint,
                    exposure or "",
                    base_url,
                    model,
                    client,
                    purpose,
                    project_id,
                    project_slug,
                    json.dumps(source_files or []),
                    content_hash,
                    int(chars_sent),
                    int(tokens_sent),
                    int(tokens_received),
                    prompt,
                    response,
                    1 if ok else 0,
                    now,
                ),
            )
            self._append_event_conn(
                conn,
                "egress.recorded",
                egress_id,
                {
                    "endpoint": endpoint,
                    "exposure": exposure or "",
                    "project_slug": project_slug,
                    "ok": bool(ok),
                },
                now,
            )
        return egress_id

    def list_egress(
        self, days: int = 7, endpoint: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Recent egress audit rows, newest first, optionally filtered to one endpoint."""
        since = _iso(utc_now() - timedelta(days=days))
        query = "SELECT * FROM egress_log WHERE created_at >= ?"
        params: list[Any] = [since]
        if endpoint:
            query += " AND endpoint = ?"
            params.append(endpoint)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["source_files"] = json.loads(item.pop("source_files_json"))
            item["ok"] = bool(item["ok"])
            out.append(item)
        return out

    def record_squire_health(
        self, ok: bool, detail: str = "", latency_ms: int = 0, endpoint: str = "squire"
    ) -> dict[str, Any]:
        """Ledger a health probe and emit squire.health.changed on a per-endpoint up/down transition."""
        now = iso_now()
        with self.connect() as conn:
            prev = conn.execute(
                "SELECT ok FROM squire_calls WHERE purpose = 'health' AND endpoint = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (endpoint,),
            ).fetchone()
            changed = prev is None or bool(prev["ok"]) != bool(ok)
            conn.execute(
                """
                INSERT INTO squire_calls (id, endpoint, client, purpose, latency_ms, ok, error, created_at)
                VALUES (?, ?, 'bigboss', 'health', ?, ?, ?, ?)
                """,
                (
                    new_token("sq"),
                    endpoint,
                    int(latency_ms),
                    1 if ok else 0,
                    None if ok else detail[:500],
                    now,
                ),
            )
            if changed:
                self._append_event_conn(
                    conn,
                    "squire.health.changed",
                    endpoint,
                    {"endpoint": endpoint, "up": bool(ok), "detail": detail, "at": now},
                    now,
                )
        return {"up": bool(ok), "changed": changed}

    def squire_status(self, days: int = 7) -> dict[str, Any]:
        """Health + usage rollup over the trailing window, per endpoint and per client."""
        since = _iso(utc_now() - timedelta(days=days))
        with self.connect() as conn:
            endpoint_names = [
                r["endpoint"]
                for r in conn.execute(
                    "SELECT DISTINCT endpoint FROM squire_calls ORDER BY endpoint"
                ).fetchall()
            ]
            endpoints: list[dict[str, Any]] = []
            for name in endpoint_names:
                endpoints.append(self._endpoint_status_conn(conn, name, since))

            # Back-compat top-level view is the default squire endpoint (or the
            # first seen), so existing callers/UI keep working unchanged.
            primary = next(
                (e for e in endpoints if e["endpoint"] == "squire"),
                endpoints[0] if endpoints else None,
            )
            clients = conn.execute(
                """
                SELECT client,
                       COUNT(*) AS calls,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms,
                       SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors,
                       MAX(created_at) AS last_call_at
                FROM squire_calls
                WHERE purpose != 'health' AND created_at >= ?
                GROUP BY client
                ORDER BY calls DESC
                """,
                (since,),
            ).fetchall()
        client_rows = [dict(c) for c in clients]
        return {
            "up": primary["up"] if primary else None,
            "last_health_at": primary["last_health_at"] if primary else None,
            "last_health_error": primary["last_health_error"] if primary else None,
            "last_call_at": primary["last_call_at"] if primary else None,
            "window_days": days,
            "endpoints": endpoints,
            "clients": client_rows,
            "totals": {
                "calls": sum(c["calls"] for c in client_rows),
                "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in client_rows),
                "completion_tokens": sum(c["completion_tokens"] or 0 for c in client_rows),
                "errors": sum(c["errors"] or 0 for c in client_rows),
            },
        }

    def _endpoint_status_conn(
        self, conn: sqlite3.Connection, endpoint: str, since_iso: str
    ) -> dict[str, Any]:
        health = conn.execute(
            "SELECT ok, error, created_at, latency_ms FROM squire_calls "
            "WHERE purpose = 'health' AND endpoint = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
        last_call = conn.execute(
            "SELECT ok, error, created_at, client, purpose FROM squire_calls "
            "WHERE purpose != 'health' AND endpoint = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
        agg = conn.execute(
            """
            SELECT COUNT(*) AS calls,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors
            FROM squire_calls
            WHERE purpose != 'health' AND endpoint = ? AND created_at >= ?
            """,
            (endpoint, since_iso),
        ).fetchone()
        egress = conn.execute(
            "SELECT COUNT(*) AS sends, SUM(chars_sent) AS chars_sent, MAX(created_at) AS last_at "
            "FROM egress_log WHERE endpoint = ? AND created_at >= ?",
            (endpoint, since_iso),
        ).fetchone()
        up: bool | None = None
        if health is not None:
            up = bool(health["ok"])
        elif last_call is not None:
            up = bool(last_call["ok"])
        return {
            "endpoint": endpoint,
            "up": up,
            "last_health_at": health["created_at"] if health else None,
            "last_health_error": health["error"] if health else None,
            "last_call_at": last_call["created_at"] if last_call else None,
            "calls": agg["calls"] or 0,
            "prompt_tokens": agg["prompt_tokens"] or 0,
            "completion_tokens": agg["completion_tokens"] or 0,
            "avg_latency_ms": agg["avg_latency_ms"] or 0,
            "errors": agg["errors"] or 0,
            "egress_sends": egress["sends"] or 0,
            "egress_chars_sent": egress["chars_sent"] or 0,
            "last_egress_at": egress["last_at"],
        }

    def _row_to_project(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        aliases = [
            {"alias_value": a["alias_value"], "alias_kind": a["alias_kind"], "source": a["source"]}
            for a in conn.execute(
                "SELECT alias_value, alias_kind, source FROM project_aliases WHERE project_id = ? ORDER BY alias_value",
                (row["id"],),
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "canonical_path": row["canonical_path"],
            "kind": row["kind"],
            "purpose": row["purpose"],
            "domain": row["domain"],
            "status": row["status"],
            "pinned": bool(row["pinned"]),
            "pin_rank": row["pin_rank"],
            "no_crawl": bool(row["no_crawl"]),
            "digest_pending": bool(row["digest_pending"]),
            "excluded": bool(row["excluded"]),
            "lifecycle": row["lifecycle"],
            "ownership": row["ownership"],
            "archived_at": row["archived_at"],
            "notes": row["notes"],
            "supersedes": row["supersedes"],
            "evolved_into": row["evolved_into"],
            "track_it": bool(row["track_it"]),
            "track_note": row["track_note"],
            "is_daemonized": bool(row["is_daemonized"]),
            "git_commit_count": row["git_commit_count"],
            "last_activity_at": row["last_activity_at"],
            "last_git_commit_at": row["last_git_commit_at"],
            "last_transcript_at": row["last_transcript_at"],
            "last_handoff_at": row["last_handoff_at"],
            "markers": json.loads(row["markers_json"]),
            "aliases": aliases,
            "alias_count": len(aliases),
            "first_seen_at": row["first_seen_at"],
            "updated_at": row["updated_at"],
            "intel": self._get_intel_conn(conn, row["id"]),
        }

    def _get_approval_conn(
        self, conn: sqlite3.Connection, approval_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
        return self._row_to_approval(row, conn) if row else None

    def _row_to_approval(
        self, row: sqlite3.Row, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "harness": row["harness"],
            "workspace": row["workspace"],
            "title": row["title"],
            "summary": row["summary"],
            "risk_level": row["risk_level"],
            "action_hash": row["action_hash"],
            "proposed_action": json.loads(row["proposed_action_json"]),
            "policy_reasons": json.loads(row["policy_reasons_json"]),
            "diff_summary": json.loads(row["diff_summary_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "resolved_at": row["resolved_at"],
            "latest_decision": self._latest_decision_for_row(row, conn),
        }

    def _latest_decision_for_row(
        self, row: sqlite3.Row, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        if conn is not None:
            decision = conn.execute(
                """
                SELECT id, decision, note, approved_by, device_id, created_at
                FROM decisions
                WHERE approval_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            return dict(decision) if decision else None
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            decision = conn.execute(
                """
                SELECT id, decision, note, approved_by, device_id, created_at
                FROM decisions
                WHERE approval_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            return dict(decision) if decision else None

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (event_type, aggregate_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, aggregate_id, json.dumps(payload, sort_keys=True), created_at),
        )

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "aggregate_id": row["aggregate_id"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _infer_kind(markers: dict[str, Any]) -> str:
    """Rough project kind from marker files (v1 heuristic; human-editable later)."""
    if markers.get("pyproject.toml") or markers.get("package.json"):
        return "code"
    if markers.get(".git") or markers.get(".mcp.json"):
        return "code"
    if markers.get("CLAUDE.md") or markers.get("README.md"):
        return "docs"
    return "unknown"
