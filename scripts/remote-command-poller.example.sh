#!/usr/bin/env bash
# Cross-machine CONTROL — reference poller for the remote-host side (BigBoss → remote host, PULL).
#
# BigBoss (the BigBoss host) exposes two adapter-token-gated routes under `serve --lan`. This poller
# fetches human-APPROVED commands, executes reversible Squire-queue ops against your queue,
# and acks the outcome. BigBoss is source-of-truth; nothing runs here that the operator didn't approve.
# Everything is stdlib curl + jq — no dependencies beyond those.
#
# Setup (once): copy BigBoss's adapter token to the remote host and point at the BigBoss host's LAN IP.
#   BigBoss side: `bigboss secrets` prints .bigboss/secrets/adapter-token.txt; run `serve --lan`.
set -euo pipefail

BIGBOSS_URL="${BIGBOSS_URL:-http://<this-machine-lan-ip>:8787}"   # the BigBoss host LAN address
ADAPTER_TOKEN="${BIGBOSS_ADAPTER_TOKEN:?set BIGBOSS_ADAPTER_TOKEN to the copied token}"
HOST_LABEL="${HOST_LABEL:-remote}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

# --- Your local action allowlist. REJECT anything not here (never execute unknown). ----
# Implement each against your real Squire queue; these are stubs to wire up.
execute_action() {  # execute_action <action> <args_json> -> prints a result JSON, returns 0/1
  local action="$1" args="$2"
  case "$action" in
    queue.pause)        squire_queue_pause;              echo '{"paused":true}' ;;
    queue.resume)       squire_queue_resume;             echo '{"resumed":true}' ;;
    job.reprioritize)   local jid rank
                        jid=$(jq -r '.job_id' <<<"$args"); rank=$(jq -r '.rank' <<<"$args")
                        squire_job_reprioritize "$jid" "$rank"; echo "{\"job_id\":\"$jid\",\"rank\":$rank}" ;;
    job.cancel)         local jid; jid=$(jq -r '.job_id' <<<"$args")
                        squire_job_cancel "$jid";        echo "{\"job_id\":\"$jid\",\"cancelled\":true}" ;;
    *)                  echo "{\"error\":\"unknown action: $action\"}"; return 1 ;;
  esac
}

# Idempotency: remember applied command ids so a redelivery acks without re-applying.
APPLIED_DIR="${TMPDIR:-/tmp}/bigboss-applied"; mkdir -p "$APPLIED_DIR"

poll_once() {
  local resp; resp=$(curl -sS -H "X-Adapter-Token: ${ADAPTER_TOKEN}" \
    "${BIGBOSS_URL}/api/commands?host=${HOST_LABEL}") || return 0
  echo "$resp" | jq -c '.commands[]?' | while read -r cmd; do
    local id action args status result
    id=$(jq -r '.id' <<<"$cmd"); action=$(jq -r '.action' <<<"$cmd"); args=$(jq -c '.args' <<<"$cmd")
    if [[ -f "$APPLIED_DIR/$id" ]]; then
      status="acked"; result=$(cat "$APPLIED_DIR/$id")      # already applied → ack again only
    elif result=$(execute_action "$action" "$args"); then
      status="acked"; echo "$result" >"$APPLIED_DIR/$id"
    else
      status="failed"
    fi
    curl -sS -o /dev/null -X POST -H "X-Adapter-Token: ${ADAPTER_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"status\":\"${status}\",\"result\":${result:-'{}'}}" \
      "${BIGBOSS_URL}/api/commands/${id}/ack"
  done
}

# Replace these stubs with real Squire-queue calls:
squire_queue_pause()        { :; }
squire_queue_resume()       { :; }
squire_job_reprioritize()   { :; }  # $1=job_id $2=rank
squire_job_cancel()         { :; }  # $1=job_id

while true; do poll_once; sleep "$POLL_INTERVAL"; done
