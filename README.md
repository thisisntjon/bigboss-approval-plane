# BigBoss Approval Plane

Local-first control plane for coordinating multiple AI coding harnesses: an **approval/governance dashboard** plus a **portfolio brain** — a multi-model Council that classifies, prioritizes, ideates over, and deep-researches your projects. The primary surface is the **local desktop dashboard** (loopback); a LAN/phone mode is opt-in via `--lan`.

This MVP is dependency-light on purpose: it uses Python's standard library, SQLite, Server-Sent Events, and a static web UI. It runs locally today. Harness integration that exists in this repo: a stdio **MCP facade** (`src/bigboss/mcp_stdio.py`, usable by any MCP-capable harness — project configs for Claude Code, Codex, Gemini CLI, and Grok are checked in), a generic stdin-JSON **hook adapter** (`src/bigboss/hook_adapter.py`, wired up for Codex project hooks; the MCP facade's `bigboss_permission_prompt` tool serves Claude Code's `--permission-prompt-tool`), and an enforced **Codex app-server bridge** (`src/bigboss/codex_app_bridge.py`). There are no Cursor, Antigravity, OpenHands, or ACP adapters yet.

**Status:** working local MVP. The core governance guarantee (an LLM can *propose* but never *commit* without your approval) is enforced in code and covered by the test suite in `tests/`. The CLI is UTF-8-safe on a default Windows console (no `PYTHONIOENCODING` needed).

## What It Does

- Serves an approval queue at `http://127.0.0.1:8787/` (local by default; `--lan` exposes it on the LAN for a phone).
- Auto-pairs the local browser as device "Workstation" on `serve`; pairs a phone with a one-time code / LAN PIN in `--lan` mode. Per-device tokens are stored locally.
- Accepts approval requests from local harness adapters over HTTP.
- Accepts non-approval harness status updates over HTTP.
- Exposes a local stdio MCP facade for harnesses that can call MCP tools.
- Classifies requests as `auto_allowed`, `pending`, or `blocked`.
- Pushes live updates to the phone while the web app is open.
- Persists requests, decisions, runs, pair codes, and audit events in SQLite.
- Auto-derives a **project portfolio registry** and runs a multi-model **Council** over it — prioritization, cross-project ideation, and Fable-led per-project deep research (see [The Council](#the-council-portfolio-brain)).

Strict-local iPhone note: iOS does not provide reliable closed-app LAN-only lock-screen web push. For v1, immediate alerts require the web page/PWA to stay open. Approval content and decisions remain local.

## Run (local dashboard)

PowerShell:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss serve --port 8787
```

Add `--no-open` to skip launching the browser (useful headless or in CI). The stdin hook adapter (`src/bigboss/hook_adapter.py`) finds the server via `BIGBOSS_URL` (default `http://127.0.0.1:8787`), or `--url`.

**Run from anywhere:** run `scripts\Add-BigBossToPath.ps1` once, then `bigboss <command>` (e.g. `bigboss ps`, `bigboss serve --port 8787`) works in any directory via the self-locating `scripts/bigboss.cmd` launcher — no `PYTHONPATH` needed, and state stays repo-pinned in `<repo>\.bigboss`.

`serve` binds `127.0.0.1` by default, mints a single-use auto-claim code, and opens the dashboard in your browser — it pairs itself silently as device **"Workstation"**, no code entry. After a browser cache clear, re-open with:

```powershell
uv run python -m bigboss open
```

Auto-claim (not blanket loopback trust) is deliberate: governed harnesses run on this same machine, so decisions stay bound to a token and attributable to a device — a rogue local process cannot self-approve.

## Pair your phone (opt-in LAN mode)

Phone access is opt-in. Start the server with `--lan` (binds `0.0.0.0`, opens the Pairing Desk instead of the dashboard):

```powershell
uv run python -m bigboss serve --lan
```

Then use the one-click pairing flow below.

## Pair your phone (one click)

**Pin `scripts\pair-phone.bat` to your taskbar** (or run `uv run python -m bigboss pair`).

That command:
1. Starts BigBoss automatically if it is not already running (reclaims stale listeners on port 8787 — no manual `netstat`)
2. Opens the **Pairing Desk** in your browser
3. Shows a **QR code popup** — scan it with your iPhone camera

First-time bootstrap is scan-only. After that, re-pair with the **LAN PIN** printed when the server starts (phone → PIN tab).

```powershell
cd <repo>
$env:PYTHONPATH='src'
uv run python -m bigboss pair
```

Or run the server directly in LAN mode (also opens the desk):

```powershell
uv run python -m bigboss serve --lan
```

Manage devices:

```powershell
uv run python -m bigboss devices
uv run python -m bigboss lan-pin
uv run python -m bigboss lan-pin --rotate
```

Create a demo request:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss demo-request
```

Run the stdio MCP server:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss mcp-stdio
```

## Harness Adapter Contract

Adapters post local approval requests to:

```text
POST /api/harness/approval-requests
X-Adapter-Token: <.bigboss/secrets/adapter-token.txt>
```

Adapter and admin endpoints — plus pair-code minting (`/api/pair/codes`, `/pair`) — are only accepted from localhost. The dashboard is loopback-only by default (`serve`); `--lan` exposes the dashboard on the LAN while machine-control APIs stay local.

Example body:

```json
{
  "run_id": "run_codex_001",
  "run_title": "Codex on client-app",
  "harness": "codex",
  "workspace": "C:\\repos\\client-app",
  "title": "Approve dependency install",
  "summary": "The harness wants to install dependencies before running tests.",
  "proposed_action": {
    "kind": "shell_command",
    "command": "uv sync",
    "cwd": "C:\\repos\\client-app"
  },
  "diff_summary": {
    "files_changed": 0,
    "insertions": 0,
    "deletions": 0
  }
}
```

Use `?wait=true&timeout=600` when the harness needs the HTTP call to block until the phone decision is made.

Waiting responses include:

- `action_hash`: deterministic hash of the exact proposed action, workspace, and policy version.
- `latest_decision.decision`: `approve_once`, `approve_for_run`, `reject`, or `request_changes`.
- `latest_decision.note`: instruction text entered from the phone UI.

Adapters must execute only the action that matches the returned `action_hash`.

Non-approval updates:

```text
POST /api/harness/updates
X-Adapter-Token: <.bigboss/secrets/adapter-token.txt>
```

## MCP Tools

The stdio MCP server is named `bigboss` and exposes:

- `bigboss_request_approval`
- `bigboss_wait_for_decision`
- `bigboss_get_approval`
- `bigboss_list_pending_approvals`
- `bigboss_submit_update`
- `bigboss_session_report`
- `bigboss_permission_prompt`
- `registry_list`
- `registry_refresh`
- `crawl_project`
- `crawl_portfolio`
- `portfolio_intel`
- `intel_refresh`
- `squire_status`
- `squire_egress`

It does not expose approve/reject tools. Paired phones remain the decision authority.

For Claude-style permission prompts, use `bigboss_permission_prompt` as the MCP tool name. It accepts flexible hook fields such as `tool_name`, `tool_input`, `cwd`, and `session_id`, creates a BigBoss approval card, and returns conservative `allow` / `permissionDecision` fields.

When passing this to Claude's `--permission-prompt-tool` flag, use the **namespaced** name Claude assigns to project MCP tools: `mcp__bigboss__bigboss_permission_prompt` (the bare `bigboss_permission_prompt` fails with "MCP tool ... not found"). Note that Claude's headless `-p` mode auto-allows safe Bash (e.g. `echo`, simple writes) without consulting the permission-prompt tool; to force a BigBoss round-trip, have Claude call `mcp__bigboss__bigboss_request_approval` directly (with `--allowedTools`) or trigger a genuinely gated action.

Verified live command shape:

```powershell
claude -p "<task>" --allowedTools "mcp__bigboss__bigboss_request_approval" `
  --strict-mcp-config --mcp-config .mcp.json --output-format json
```

## Hook Adapter

Harness hooks can call BigBoss directly over localhost:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
Get-Content hook-payload.json | uv run python -m bigboss hook-approval --harness codex --no-wait
```

The hook adapter reads JSON from stdin, normalizes common fields like `tool_name`, `tool_input.command`, and `tool_input.file_path`, posts to `/api/harness/approval-requests`, and fails closed if BigBoss is unavailable.

### Codex Project Hooks

This repo includes a Codex project hook config at `.codex/hooks.json`.

- `PermissionRequest` routes `Bash`, `apply_patch`/`Edit`/`Write`, and MCP tool approval requests through `codex_hook.py`.
- `PostToolUse` records non-blocking BigBoss run updates after supported tools complete.
- `codex_hook.py` resolves the repo-local `src` package and `.bigboss/secrets/adapter-token.txt`, so Codex can start from a subdirectory.
- Codex requires project hooks to be reviewed/trusted in `/hooks`. For one-off automation, Codex also exposes `--dangerously-bypass-hook-trust`.

The Codex hook returns the documented `PermissionRequest` shape:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow"
    }
  }
}
```

## Codex App-Server Bridge (enforced Codex approvals)

Codex's project hooks (`.codex/hooks.json`) do **not** drive approvals under `codex exec`:
`codex exec` returns `command execution approval is not supported in exec mode`, and `PostToolUse`
does not fire there. The enforced Codex path is instead the **app-server** (`codex app-server`,
experimental), which sends the client JSON-RPC approval requests. `src/bigboss/codex_app_bridge.py`
drives it and routes each request through BigBoss to the phone:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss codex-run "Create a file X, then stop." --sandbox workspace-write --timeout 600
```

- Handles `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, and
  `item/permissions/requestApproval`; responds `{"decision": "accept"|"acceptForSession"|"decline"|"cancel"}`.
- Maps BigBoss phone decisions: `approve_once -> accept`, `approve_for_run -> acceptForSession`,
  `reject`/`request_changes -> decline`; fail-safe `decline` on timeout.
- Resolves the vendored `codex.exe` automatically (the npm `codex` shim is a `.ps1` and cannot be
  spawned directly); override with the `BIGBOSS_CODEX_BIN` environment variable.

The `.codex/hooks.json` `PostToolUse` update path is retained as a best-effort supplement for
interactive Codex.

## Cost Router (Ecosystem Phase E1, metering-only)

BigBoss can act as a loopback-only proxy that meters programmatic Anthropic spend against a monthly
budget. v1 is **metering-only**: faithful passthrough of the Messages API plus a cost ledger — no model
rewriting, no local triage, and the cap is **advisory (alerts, never blocks)** until the billing regime is
verified.

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss router --port 4000 --harness agent-sdk
```

Then point **programmatic** clients at it (leave interactive Claude Code on your Max OAuth, unrouted):

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_API_KEY  = "<dedicated prepaid Anthropic API key>"
```

Billing lane (important): relaying a Max OAuth token through any proxy is a stated Consumer-Terms
violation and is fingerprint-fragile. The clean lane is a **dedicated prepaid Anthropic API key, hard-capped**,
used only for programmatic traffic. Before trusting the ledger's dollars, run `/status`, `/usage`, and check
Console Billing to confirm which pool programmatic traffic is actually drawing from.

How it works:

- Meters `POST /v1/messages` (stream + non-stream); every other path (`/v1/messages/count_tokens`,
  `/v1/models`, ...) is transparent passthrough and is not metered.
- Cost is priced against the **served** model (read off `message_start`), so Fable->Opus content reroutes
  are billed correctly and detected in the ledger (`served_model != requested_model`, `stop_reason:"refusal"`).
- Records each call to `router_calls`, bumps `budget_periods`, and appends `routing.decided` /
  `spend.recorded` / `budget.alert` events to the same event log the phone UI already streams — so burn-rate
  and 50/75/90% alerts surface with no new plumbing.
- Loopback-only by design (`127.0.0.1`): it forwards a live provider credential and must never be
  LAN-reachable.

## Portfolio View (Ecosystem Phase E3)

BigBoss maintains an **auto-derived, read-only registry** of every local project: fs markers (`.git`,
`pyproject.toml`, `package.json`, ...), git activity, `~/.claude/projects` transcript slugs (including
D:-drive paths decoded from slugs), handoffs, and `daemons.json`. Copies dedupe to one canonical project
per family (others become aliases); a marker-less parent that merely contains projects is flagged as ambiguous and hidden.

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss registry-refresh          # harvest + reconcile (default roots: ~/Desktop, ~/Desktop/Python; extend via BIGBOSS_EXTRA_ROOTS)
uv run python -m bigboss registry-refresh --prune  # + tombstone vanished LOCAL projects (never remote/pinned)
uv run python -m bigboss registry-list             # print the portfolio (--all includes ambiguous/gone)
```

**Remote hosts (E3.6).** A machine BigBoss can't walk (e.g. a GPU box on the LAN) produces a *crawl bundle*
(`bundle_version 1`, contract documented in `src/bigboss/registry/ingest.py`) of its marker-bearing project
dirs. Two ways in, both reusing the local reconcile/dedup path (stored as `//<host>/<path>`, tagged
`remote:<host>`):

```powershell
uv run python -m bigboss registry-ingest bundle.json   # file drop
```

Or **HTTP push** (mode 2): the remote machine `POST`s the bundle body to `POST /api/registry/ingest`. This route
is LAN-accepted and adapter-token gated, so it only exists when you run `serve --lan`:
- Header `X-Adapter-Token: <.bigboss/secrets/adapter-token.txt>`; `Content-Type: application/json`.
- Returns `201` on success, `4xx` on a bad token/bundle (clients should fall back to file-drop only on
  connection errors, not `4xx`). 2 MB body cap.
- Optional source-IP allowlist: `BIGBOSS_INGEST_HOSTS=<lan-box-ip>` (comma-separated).
- **Full sync:** add `?prune=1` (or `registry-ingest --prune`) so projects that vanished from that host's
  bundle are retired (tombstoned `status='gone'`, hidden from `registry-list`). Scoped to that host only —
  never touches local or other-host rows, never pinned. Default off, so rows otherwise only accrue.

The phone dashboard has a **Portfolio** tab: projects sorted pinned-first then by recent activity, each
showing kind, purpose, last activity, a dormant badge (>30 days quiet), "where I left off"
(latest handoff/session/commit), and a one-tap pin toggle. HTTP surface (device-authed like approvals):
`GET /api/registry/projects`, `POST /api/registry/pin`, `POST /api/registry/refresh`. MCP surface:
`registry_list` / `registry_refresh` on the stdio facade.

Priority is no longer manual-pin-only: the multi-model **Council** (below) auto-prioritizes the portfolio
into a ratifiable card that, on approval, sets `pin_rank`. Projects also carry an agent-derived ground-truth
baseline — `lifecycle` (active/done/archived/dormant/experiment), `ownership` (personal/work/third-party),
authoritative `notes`, and lineage (`supersedes`/`evolved_into`) — surfaced via `registry-classify` and
honored by prioritization (work/third-party/done/archived are excluded; dormant *personal* projects stay as a
revival backlog).

## The Council (portfolio brain)

Beyond governing approvals, BigBoss runs a **multi-model Council** (Claude, GPT, Gemini, Grok — a stdlib port
of [thecouncil](https://github.com/thisisntjon/thecouncil)) that reasons over the project portfolio. It can:

- **Deliberate** on a question — independent answers → peer critique → cross-vendor verification → synthesis,
  recording per-model verified-claim accuracy (`council-ask`, `council-leaderboard`).
- **Prioritize** the portfolio — rank the active projects into a top-N shortlist with cross-project
  **synergies, opportunities, and tensions** (ideation), delivered as a ratifiable card (`council-prioritize`).
- **Deep-research one project** — a **Fable-led** (`COUNCIL_RESEARCH_MODEL`, default `claude-fable-5`) study
  from all angles → what's newly feasible, reuse-from-lineage, risks, a POC sprint, and a phased roadmap
  (`council-research <slug>`, persisted to `project_research`).

```bash
uv run python -m bigboss council-prioritize --top 8    # rank the portfolio -> ratifiable card -> pin_rank
uv run python -m bigboss council-research <slug>    # Fable-led deep research -> brief + phased roadmap
uv run python -m bigboss registry-classify             # lifecycle / ownership / excluded / lineage table
uv run python -m bigboss registry-exclude <slug>       # drop a project from prioritization (--include to undo)
uv run python -m bigboss registry-baseline audit.json  # apply an agent-derived ground-truth baseline
```

Vendor keys live in a gitignored `<repo>/.env` (copy `.env.example` and fill in what you have; seats without a key are skipped). The Council is stdlib-only (`urllib` + `concurrent.futures`). `src/bigboss/council/` is a stdlib port of [github.com/thisisntjon/thecouncil](https://github.com/thisisntjon/thecouncil).

## Project Intel + Squire Watch (Ecosystem Phase E4a)

The tracker that gives the portfolio brain its eyes. The crawl itself is a **standalone general-purpose
tool** (`src/bigboss/crawler.py`) shared by every consumer — Squire (general purpose, under active
development), BigBoss's own digester, or any harness: it walks a project's planning docs
(`workflow/PLAN.md`, `PLAN.md`, `ROADMAP.md`, `TODO.md`, `CLAUDE.md`, `AGENTS.md`, `README.md`) under a
character budget and returns a bundle with file inventory, optional text, and a content hash for cheap
"docs changed?" checks. It is exposed as the `crawl_project` / `crawl_portfolio` MCP tools on the
`bigboss` stdio facade and as a JSON CLI:

```powershell
$env:PYTHONPATH='src'
$env:UV_CACHE_DIR="$PWD\.uv-cache"
uv run python -m bigboss crawl                       # all registered projects (hashes + inventory)
uv run python -m bigboss crawl --project BigBoss --full-text   # one project, with doc text
uv run python -m bigboss crawl --path <path-to-squire> --full-text    # any directory, registry not required
```

On top of the crawler, the intel pipeline: **Squire** — a free local model served by LM Studio on a LAN box (`docs/squire.md`)
— extracts one structured snapshot per project: a status line, concrete blockers, and the current/next
roadmap phase. The crawler's content hash skips unchanged projects, so repeat refreshes cost nothing. The
expensive brain (Fable 5 in a harness session) reads the whole pre-digested portfolio through one MCP
tool, `portfolio_intel`, instead of crawling raw docs with paid tokens.

```powershell
uv run python -m bigboss intel-refresh              # digest changed projects via Squire (--project SLUG, --force)
uv run python -m bigboss intel-list                 # portfolio with status/blockers/roadmap
uv run python -m bigboss squire-status              # per-endpoint health + per-client usage rollup
uv run python -m bigboss squire-endpoints           # list configured compute endpoints
uv run python -m bigboss squire-egress              # data-egress audit: what was sent to which box
uv run python -m bigboss squire-proxy --port 1235   # metering proxy for the shared Squire endpoint
```

### Compute endpoints (Squire vs Beastmode)

BigBoss talks to named local-compute endpoints, which are deliberately **not** interchangeable:

| Endpoint | Machine | Auto-routed? | Guardrails |
|---|---|---|---|
| `squire` | LM Studio on a LAN box (`SQUIRE_BASE_URL`) | yes — scheduler default | always-on, zero-cost triage |
| `beastmode` | a second, larger GPU workstation (`BEASTMODE_BASE_URL`) | **never** — opt-in only | rate-limited (min 6s/call), ledgered under its own `beastmode` endpoint + `bigboss-intel-beastmode` client label |

Beastmode is guarded compute: the background scheduler is hard-coded to `squire` and will never touch it. Use it explicitly when you want the bigger box:

```powershell
uv run python -m bigboss intel-refresh --endpoint beastmode --project BigBoss --force
```

Hosts, models, and keys are env-overridable (`BEASTMODE_BASE_URL`, `BEASTMODE_MODEL`, `BEASTMODE_API_KEY`,
`BEASTMODE_MIN_INTERVAL_S`), so nothing sensitive lives in git. Default model is an **instruct** model
(`qwen3-coder-30b-a3b-instruct`) — reasoning models spend the token budget on hidden thinking and return
empty content for the JSON-extraction task. The MCP `intel_refresh` tool takes the same `endpoint` argument.

**Data-egress audit (accountability, not gating).** There is deliberately **no API-key enforcement** on
Beastmode. Instead, every payload BigBoss sends to a compute endpoint is written to the `egress_log`
audit: project, exact source files, content hash, sizes, and tokens in/out — tagged with the machine's
known exposure channel (`exposure: managed-edr` on Beastmode, because that box runs a managed endpoint-security agent that can carry
anything processed there off-box). Failed sends are audited too: the data still left this machine.
Inspect it with:

```powershell
uv run python -m bigboss squire-egress --endpoint beastmode   # what has been sent to the guarded box, exactly
uv run python -m bigboss squire-egress --days 30 --json       # full machine-readable audit
```

Also available as `GET /api/squire/egress` and the `squire_egress` MCP tool; the phone Squire tab shows
an amber `exposure: managed-edr` pill with the audited-send count on the Beastmode card.

`bigboss serve` runs the loop automatically: a Squire health probe every 5 minutes (event on up/down
transitions only) and a registry+intel refresh every 6 hours (`--intel-interval-hours`,
`--squire-health-interval`; 0 disables). The phone Portfolio tab shows each project's digested status,
red blocker pills, and roadmap now/next; the new **Squire** tab shows health and per-client usage.

Squire monitoring: multiple projects share the Squire endpoint and LM Studio has no auth or logging, so
`squire-proxy` is the accounting point. Point any Squire client at it and label it with its project name:

```powershell
$env:SQUIRE_BASE_URL = "http://127.0.0.1:1235/v1"
$env:SQUIRE_API_KEY  = "<project-name>"   # LM Studio ignores the key; the proxy reads it as the client label
```

Every call is ledgered to `squire_calls` (client, tokens, latency, failures) and surfaces via
`squire-status`, `GET /api/squire/status`, and the `squire_status` MCP tool. Unlike the cost router, no
credential is forwarded, so `--host 0.0.0.0` is permitted when other machines need metered access.
The proxy also writes each metered request into the egress audit (size + payload hash per client), and
`squire-proxy --endpoint beastmode` fronts the guarded box with the same metering + exposure tagging.

## Recovering After Moving The Repo

Moving the repo does not break the code (it is pure-stdlib; `.venv`/paths are self-resolving), but it
resets **interactive harness trust**, which no CLI restores non-interactively:

1. **Claude project MCP.** After a move, `claude mcp get bigboss` may show `Failed to connect` or
   `pending`. Re-approve the project `.mcp.json` server (`/mcp` -> enable `bigboss`, or accept the
   project trust dialog). Confirm with `claude mcp get bigboss` -> `Connected`.
   - The MCP stdio server must speak newline-delimited JSON (MCP transport), not LSP `Content-Length`
     framing, or the client reports `Failed to connect`.
2. **Codex hooks.** Re-trust `.codex/hooks.json` in Codex `/hooks` (or use the app-server bridge,
   which does not need hook trust).
3. **Gemini / Grok.** Project-config based; no action needed once their sessions reopen the project.

Note: the phone dashboard's live push (SSE) reliably shows cards present at page load, but may not
push cards created after a long-idle page; refresh the page if a new card does not appear.

## Current MCP Harness Setup

Project/user registrations created by Phase 2 setup:

- Claude Code: `.mcp.json` project MCP server `bigboss`. Claude reports it as pending approval until you open/approve the project MCP server in Claude.
- Gemini: `.gemini/settings.json` project MCP server `bigboss`.
- Grok: `.grok/config.toml` project MCP server `bigboss`.
- Codex: global Codex MCP server `bigboss` was added with `codex mcp add`; a repo-local `.codex/config.toml` snippet and `.codex/hooks.json` hook config are present.

The launcher for all of these is `mcp_dev.py`, which runs `python -m bigboss mcp-stdio` with the repo `src` path.

## Definition Of Done

- The local dashboard opens auto-paired on `serve` (device "Workstation"); a phone on the same LAN can pair and open the dashboard in `--lan` mode.
- Approval cards update live while the page is open.
- Approve, approve-for-run, reject, and instruction decisions persist to SQLite.
- Harness adapters can create approval requests and optionally wait for decisions.
- Policy auto-allows structured read-only actions, requires approval for shell/tests/installs/network changes, and blocks destructive/secret-exposure patterns.
- Shell commands, including test commands, require approval unless a later sandbox policy explicitly changes this.
- Device auth, CSRF header checks, adapter token checks, admin token checks, and Origin validation are implemented.
- Pending approvals and audit events survive server restart.
- Tests cover policy classification, device pairing, decision persistence, HTTP auth, MCP tools, action hashes, updates, and end-to-end request flow.

## Useful Files

- `src/bigboss/server.py`: HTTP API, static serving, SSE event stream.
- `src/bigboss/store.py`: SQLite schema, approval lifecycle, cost ledger, project registry.
- `src/bigboss/policy.py`: risk classification defaults.
- `src/bigboss/mcp_stdio.py`: local stdio MCP facade.
- `src/bigboss/hook_adapter.py`: stdin JSON hook bridge for harness permission/update events.
- `src/bigboss/codex_app_bridge.py`: enforced Codex approvals via `codex app-server` JSON-RPC.
- `src/bigboss/router/`: metering-only Anthropic passthrough proxy (Phase E1).
- `src/bigboss/registry/`: auto-derived portfolio registry (Phase E3).
- `src/bigboss/static/`: phone-first web UI (approvals + portfolio).
- `docs/squire.md`: Squire local-inference endpoint (LM Studio on a LAN box) + `squire` MCP.
- `.bigboss/secrets/`: generated local admin and adapter tokens.
