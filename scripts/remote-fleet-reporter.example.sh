#!/usr/bin/env bash
# M6.5-Full — reference pusher for the remote-host-side fleet reporter.
#
# BigBoss (the BigBoss host) exposes two LAN ingest routes under `serve --lan` (0.0.0.0:8787),
# gated by an adapter token (NOT loopback-only). A reporter running ON the remote host mirrors the
# two POSTs below to make its harness sessions + live Squire job/queue visible in BigBoss.
# This script is the contract; the real reporter reads LM Studio logs / the queue and
# POSTs on a timer. Everything here is stdlib curl — no dependencies.
#
# Setup (once): copy BigBoss's adapter token to the remote host and point this at the BigBoss host's LAN IP.
#   BigBoss side: `bigboss secrets` prints .bigboss/secrets/adapter-token.txt
#   BigBoss side: run `serve --lan`; optionally set BIGBOSS_INGEST_HOSTS=<remote-ip>
set -euo pipefail

BIGBOSS_URL="${BIGBOSS_URL:-http://<this-machine-lan-ip>:8787}"   # the BigBoss host LAN address
ADAPTER_TOKEN="${BIGBOSS_ADAPTER_TOKEN:?set BIGBOSS_ADAPTER_TOKEN to the copied token}"
HOST_LABEL="${HOST_LABEL:-remote}"                          # short kebab/lowercase label

post() {  # post <path> <json>
  curl -sS -o /dev/null -w "%{http_code}\n" \
    -X POST "${BIGBOSS_URL}$1" \
    -H "X-Adapter-Token: ${ADAPTER_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$2"
}

# 1) A harness session-report (what a harness running on the remote host accomplished).
#    `session_ref` makes a re-report idempotent (updates in place). `host` is required
#    to attribute it to this machine; `harness` is required.
post /api/harness/session '{
  "host": "'"${HOST_LABEL}"'",
  "harness": "codex",
  "vendor": "openai",
  "project": "the remote-side helper",
  "title": "queue coordinator",
  "summary": "wired the Squire queue scheduler",
  "files_touched": ["scheduler.py", "queue.py"],
  "next_steps": ["expose depth metric"],
  "session_ref": "remote-codex-2026-07-06-1"
}'

# 2) A Squire live-activity/queue snapshot (current state, upserted per host).
#    All fields are OPTIONAL and schema-tolerant — send what the queue system knows.
#    BigBoss stamps receive-time and shows staleness; never claim live truth if stale.
post /api/squire/activity '{
  "host": "'"${HOST_LABEL}"'",
  "reported_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
  "live_job": {"label": "TRIAGE", "started_at": "...", "elapsed_ms": 8200},
  "queue": [
    {"client": "job-fit", "purpose": "classify", "status": "running", "enqueued_at": "..."},
    {"client": "brainz",  "purpose": "embed",    "status": "queued",  "enqueued_at": "..."}
  ],
  "depth": 2,
  "models": ["google/gemma-4-e4b"]
}'
