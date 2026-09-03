# CLAUDE.md

Guidance for Claude Code when working in this repository (BigBoss).

## Project

BigBoss is a local-first approval/governance control plane for coordinating multiple
AI coding harnesses (Claude Code, Codex, Gemini, Grok). Pure Python **stdlib only** —
no third-party runtime dependencies, by design. SQLite + `http.server` + SSE + a
static web UI.

**Surface order (2026-07-02):** the LOCAL desktop dashboard is the primary control
surface for now. The phone/LAN remote track (pairing, QR/PIN, tunnels — E3.4) is
built but TABLED: do not push phone verification or invest in phone UX until the
remote-connection track reopens.

Source of truth for behavior: the code and the `tests/` suite; this file and `README.md`
describe the surfaces. Run the tests before calling any step done.

The CLI forces
UTF-8 stdout (`cli.py:_force_utf8_streams`), so `bigboss` output is safe on a default
Windows console — no `PYTHONIOENCODING` workaround needed.

## Commands

Run from anywhere: `scripts\Add-BigBossToPath.ps1` once, then `bigboss <cmd>` resolves in any
directory (via the self-locating `scripts/bigboss.cmd`; state stays repo-pinned in `<repo>/.bigboss`).
Otherwise set the path yourself from the repo root:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"

# Tests (run before calling any step done)
uv run python -m unittest discover -s tests

# Approval-plane server (local dashboard by default — binds 127.0.0.1, auto-pairs "Workstation")
uv run python -m bigboss serve --port 8787
uv run python -m bigboss open            # re-open/re-pair the local dashboard (after a cache clear)
# SSE streams close at BIGBOSS_SSE_CAP_SECONDS (default 3600) then the client reconnects with
# Last-Event-ID (bounded replay); reconnects also fire on tab-visible + a staleness watchdog (E3.5).

# LAN/phone mode (opt-in): binds 0.0.0.0, opens the QR pairing desk
uv run python -m bigboss serve --lan
uv run python -m bigboss pair            # phone pairing (starts server if needed, opens QR desk)

# Cost router (loopback-only metering proxy)
uv run python -m bigboss router --port 4000 --harness agent-sdk

# Portfolio registry
uv run python -m bigboss registry-refresh          # local harvest (default roots: `~/Desktop`, `~/Desktop/Python`, `D:/`)
uv run python -m bigboss registry-refresh --prune  # + tombstone vanished LOCAL projects (never remote/pinned)
uv run python -m bigboss registry-list
uv run python -m bigboss registry-classify                     # lifecycle/ownership/excluded/lineage table
uv run python -m bigboss registry-exclude <slug> [--include]   # exclude a project from prioritization (durable `excluded` bit; keeps intel)
uv run python -m bigboss registry-no-crawl <slug> [--allow]    # exclude from ALL crawl/digest (egress) + redact at-rest intel (E4a.1)
uv run python -m bigboss registry-baseline audit.json          # apply agent-derived ground truth (purpose/lifecycle/ownership/notes/lineage + fresh intel)
uv run python -m bigboss registry-ingest bundle.json           # ingest a remote crawl bundle from a file — E3.6
uv run python -m bigboss registry-ingest bundle.json --prune   # + full-sync: retire that host's absent rows
# Remote push (mode 2): a LAN host (e.g. a GPU box on the LAN) POSTs a bundle to POST /api/registry/ingest[?prune=1].
# Requires `serve --lan` + header X-Adapter-Token (from .bigboss/secrets/adapter-token.txt).
# Optional source-IP allowlist: env BIGBOSS_INGEST_HOSTS=<lan-box-ip> (comma-sep).

# Doc crawler (general-purpose tool: Squire / any harness / BigBoss digester)
uv run python -m bigboss crawl --project BigBoss --full-text

# Digest gating (E4a.1): newly-discovered projects are NOT sent off-box until approved
uv run python -m bigboss digest-pending                      # list projects awaiting digest approval
uv run python -m bigboss digest-approve --all                # allow off-box digest (or approve the phone card)
# (a "N new projects discovered" approval card is also raised automatically on discovery)

# Project intel (Squire-digested status/blockers/roadmap) + Squire monitoring
uv run python -m bigboss intel-refresh                       # default: squire (auto). --endpoint beastmode = opt-in 5090 box
uv run python -m bigboss intel-list
uv run python -m bigboss squire-status
uv run python -m bigboss squire-endpoints
uv run python -m bigboss squire-egress               # data-egress audit: what was sent to which box
uv run python -m bigboss squire-proxy --port 1235

# Council (M1 — multi-model deliberation engine, stdlib port of the-council-capstone)
uv run python -m bigboss council-ask --fixture              # deterministic offline mode (no keys)
uv run python -m bigboss council-ask "<question>"           # LIVE: 4 seats deliberate/vote/verify/synthesize
uv run python -m bigboss council-leaderboard                # per-model verified-claim accuracy (the throne's track record)
uv run python -m bigboss council-prioritize --top 8         # M2: rank the portfolio → ratifiable card → pin_rank on approve
uv run python -m bigboss council-research <slug>            # deep-research one project (Fable-led) → brief + phased roadmap (COUNCIL_RESEARCH_MODEL, default claude-fable-5)
uv run python -m bigboss chat                               # interactive terminal chat (fast cheap seat; /council + /fable escalate a turn; /recall searches Brainz; BIGBOSS_CHAT_SEAT)
uv run python -m bigboss brainz-recall "<query>"           # M6.2: search Brainz memory (DISPLAY-ONLY — never sent off-box; loopback webapp on 127.0.0.1:8077)
uv run python -m bigboss brainz-status                     # probe the Brainz recall webapp
# Seats (env-overridable COUNCIL_{CLAUDE,GPT,GEMINI,GROK}_MODEL): claude-haiku-4-5-20251001,
# gpt-5.4-mini, gemini-3.5-flash, grok-4.3. Vendor keys read from <repo>/.env (gitignored).

# Stdio MCP server
uv run python -m bigboss mcp-stdio
```

## Rules

- **Stdlib only.** Do not add third-party dependencies; this constraint is a locked
  framing decision shared with a sibling project.
- Secrets live in `.bigboss/secrets/` (gitignored). Never commit tokens or `.bigboss/`.
- The MCP stdio transport is **newline-delimited JSON**, not LSP `Content-Length`
  framing (a past defect — regression test exists in the suite).
- Adapter/admin HTTP endpoints — plus pair-code minting (`/api/pair/codes`, `/pair`) — are loopback-only.
 `serve` binds `127.0.0.1` by default (local dashboard, auto-claimed); `--lan` binds `0.0.0.0` for phone
 access. The cost router must never bind beyond `127.0.0.1` (it forwards a live credential).
 The Squire metering proxy forwards no credential, so a LAN bind is permitted there.
- **Local-dashboard auth is auto-claim, NOT loopback auto-trust:** a synthetic loopback-trusted device
 would let a governed harness (same box/user) self-approve via the API. Keep decisions token-bound.
- New state follows the event-sourced idiom: append to the `events` log + project into
  tables in `store.py` via the additive `_migrate()` pattern.
- Compute endpoints are **accountability, not gating**: no API-key enforcement on
  Beastmode (owner decision). Every payload sent to an endpoint is written to the
  `egress_log` audit (files, hash, sizes), tagged with the box's known exposure
  channel — `managed-edr` on the guarded endpoint. Failed sends are audited too.
- **Digest gating (E4a.1):** a newly-discovered project (`digest_pending=1` on INSERT) is
  never digested off-box until a human approves the discovery batch (an approval card, or
  `digest-approve`). `no_crawl` projects are excluded permanently and their at-rest egress
  text/intel is redacted (`registry-no-crawl <slug>`). This keeps a registry expansion
  (extra-root scan, remote ingest) from auto-sending new projects' docs to a compute endpoint.
- **`no_crawl` vs `excluded` are different levers** (easily confused): `no_crawl` is an EGRESS
  exclusion (never crawled/digested off-box; at-rest intel redacted). `excluded` is a
  PRIORITIZATION exclusion (dropped from the Council's `build_portfolio_context`; intel KEPT).
  A work project is typically `excluded`; a private one (e.g. personal memory or health data) is `no_crawl`.
- **Ground-truth baseline + backlog model:** an agent pass sets each project's durable
  `lifecycle` (active/done/archived/dormant/experiment), `ownership` (personal/work/third-party),
  `notes` (the operator's authoritative context), lineage (`supersedes`/`evolved_into`), and `track_it`
  (sensitive — legal/work-machine). These survive `registry-refresh`. **Dormant personal
  projects are a revival backlog — never auto-excluded**; only work/third-party/done/archived are.
  Set via `registry-baseline <json>` / `set_context`; review via `registry-classify`.

## Layout

- `src/bigboss/server.py` — HTTP API, static serving, SSE
- `src/bigboss/store.py` — SQLite schema, approval lifecycle, ledger + registry tables
- `src/bigboss/policy.py` — risk classification
- `src/bigboss/mcp_stdio.py` — stdio MCP facade (`bigboss_*` tools)
- `src/bigboss/codex_app_bridge.py` — enforced Codex approvals via `codex app-server`
- `src/bigboss/router/` — metering-only Anthropic passthrough proxy (Phase E1)
- `src/bigboss/registry/` — auto-derived portfolio registry (Phase E3)
- `src/bigboss/crawler.py` — general-purpose project doc crawler (module + MCP tools + `crawl` CLI)
- `src/bigboss/council/` — the portfolio brain (stdlib port of the-council-capstone, grown well past it):
  - M1 deliberation (`engine.py`/`providers.py`/`fixture.py`/`verify.py`/`scoring.py`/`parse.py`) — fan-out →
    critique → cross-vendor verify → synthesis; per-model accuracy track record + persistence.
  - M2 `prioritize.py` — portfolio ranking (Borda) + M2.1 ideation harvest + the operator's encoded philosophy
    (dormant≠dead / evolve-don't-rebuild / research-first); reads the lifecycle/ownership/notes/lineage baseline.
  - Track B `research.py` — Fable-led deep-research-per-project → brief + phased roadmap (`council-research`,
    `providers.call_anthropic_model`, `COUNCIL_RESEARCH_MODEL`). `parse.py` has truncation salvage (P-rel).
- `src/bigboss/intel/` — project intel digester + Squire metering proxy + scheduler (Phase E4a)
- `src/bigboss/intel/endpoints.py` — named compute endpoints: `squire` (auto) vs `beastmode` (opt-in, rate-limited 5090 box)
- `src/bigboss/static/` — web UI (local desktop dashboard primary; same UI serves the phone in `--lan` mode)
- `tests/` — `unittest` suite; run via the command above
