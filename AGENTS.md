# AGENTS.md

Guidance for Codex when working in this repository (BigBoss).

## Project

BigBoss is a local-first approval/governance control plane for coordinating multiple
AI coding harnesses (Claude Code, Codex, Gemini, Grok). Pure Python **stdlib only** —
no third-party runtime dependencies, by design. SQLite + `http.server` + SSE + a
static web UI.

**Surface order:** the LOCAL desktop dashboard is the primary control surface. The
phone/LAN remote track (pairing, QR/PIN, tunnels) is built but TABLED: do not push phone
verification or invest in phone UX until the remote-connection track reopens.

Source of truth for behavior: the code and the `tests/` suite; this file and `README.md`
describe the surfaces. Run the tests before calling any step done.

## Commands

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"

# Tests (run before calling any step done)
uv run python -m unittest discover -s tests

# Approval-plane server (local dashboard; add --lan for phone pairing)
uv run python -m bigboss serve --port 8787

# Cost router (loopback-only metering proxy)
uv run python -m bigboss router --port 4000 --harness agent-sdk

# Portfolio registry
uv run python -m bigboss registry-refresh
uv run python -m bigboss registry-list

# Doc crawler (general-purpose tool: Squire / any harness / BigBoss digester)
uv run python -m bigboss crawl --project BigBoss --full-text

# Project intel (Squire-digested status/blockers/roadmap) + Squire monitoring
uv run python -m bigboss intel-refresh                       # default: squire (auto). --endpoint beastmode = opt-in 5090 box
uv run python -m bigboss intel-list
uv run python -m bigboss squire-status
uv run python -m bigboss squire-endpoints
uv run python -m bigboss squire-egress               # data-egress audit: what was sent to which box
uv run python -m bigboss squire-proxy --port 1235

# Stdio MCP server
uv run python -m bigboss mcp-stdio
```

## Rules

- **Stdlib only.** Do not add third-party dependencies; this constraint is a locked
  framing decision shared with a sibling project.
- Secrets live in `.bigboss/secrets/` (gitignored). Never commit tokens or `.bigboss/`.
- The MCP stdio transport is **newline-delimited JSON**, not LSP `Content-Length`
  framing (a past defect — regression test exists in the suite).
- Adapter/admin HTTP endpoints are loopback-only; the dashboard is loopback by default and
  LAN-accessible only under `serve --lan`.
 The cost router must never bind beyond `127.0.0.1` (it forwards a live credential).
 The Squire metering proxy forwards no credential, so a LAN bind is permitted there.
- New state follows the event-sourced idiom: append to the `events` log + project into
  tables in `store.py` via the additive `_migrate()` pattern.
- Compute endpoints are **accountability, not gating**: no API-key enforcement on
  Beastmode (owner decision). Every payload sent to an endpoint is written to the
  `egress_log` audit (files, hash, sizes), tagged with the box's known exposure
  channel — `managed-edr` on the guarded endpoint. Failed sends are audited too.

## Layout

- `src/bigboss/server.py` — HTTP API, static serving, SSE
- `src/bigboss/store.py` — SQLite schema, approval lifecycle, ledger + registry tables
- `src/bigboss/policy.py` — risk classification
- `src/bigboss/mcp_stdio.py` — stdio MCP facade (`bigboss_*` tools)
- `src/bigboss/codex_app_bridge.py` — enforced Codex approvals via `codex app-server`
- `src/bigboss/router/` — metering-only Anthropic passthrough proxy (Phase E1)
- `src/bigboss/registry/` — auto-derived portfolio registry (Phase E3)
- `src/bigboss/crawler.py` — general-purpose project doc crawler (module + MCP tools + `crawl` CLI)
- `src/bigboss/intel/` — project intel digester + Squire metering proxy + scheduler (Phase E4a)
- `src/bigboss/intel/endpoints.py` — named compute endpoints: `squire` (auto) vs `beastmode` (opt-in, rate-limited 5090 box)
- `src/bigboss/static/` — web UI (local desktop dashboard primary; same UI serves the phone in `--lan` mode)
- `tests/` — `unittest` suite; run via the command above
