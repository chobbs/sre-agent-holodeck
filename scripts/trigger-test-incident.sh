#!/usr/bin/env bash
# trigger-test-incident.sh
#
# Fires a test incident into PagerDuty via the Events API v2 so the SRE Agent
# has something to investigate against the Holodeck mocks.
#
# The scenario key is embedded in several custom_details fields. Every mock
# matches scenarios by substring against the request body, and the SRE Agent
# builds its queries from custom_details — so naming the service here is what
# makes all the mocks return the *same* incident's data.
#
# Usage:
#   export PD_ROUTING_KEY=...            # integration key of the target service
#   ./scripts/trigger-test-incident.sh                       # payments-api-gateway
#   ./scripts/trigger-test-incident.sh service-mesh-mtls
#   ./scripts/trigger-test-incident.sh payments-api-gateway https://xxx.ngrok-free.dev
#
# The optional 2nd arg is the ServiceNow mock's base URL. When given, the
# payload gets a runbook_url.servicenow pointing at that scenario's KB article
# — PagerDuty documents this as the way to point the SRE Agent at a specific
# ServiceNow runbook when it cannot infer one from the payload.
#
# THE URL MUST CARRY sys_id AS A QUERY PARAMETER. The SRE Agent validates the
# shape and refuses anything else CLIENT-SIDE, without issuing a request:
#   "could not be fetched — a valid URL with sys_id is required"
# An API path ending in the KB number (which is what this script used to emit)
# is rejected, which looks like a server failure but never reaches the server.
# Hence /kb_view.do?sys_id=…, the real ServiceNow article-view shape.
#
# The KB number and sys_id are ALSO included as plain custom_details fields, so
# the agent has them even if it ignores runbook_url.
#
# Scenario keys (shared with the elasticsearch/grafana mocks where applicable):
#   payments-api-gateway  payments-orchestrator-sync  idempotency-token-service
#   service-mesh-mtls     edge-waf-cdn                checkout-api
#
# To resolve the incident afterwards, re-run with the printed dedup key:
#   PD_EVENT_ACTION=resolve PD_DEDUP_KEY=<key> ./scripts/trigger-test-incident.sh

set -uo pipefail

if [ -z "${PD_ROUTING_KEY:-}" ]; then
  cat <<'MSG'
Error: PD_ROUTING_KEY is not set.

Get it from PagerDuty: Services -> <your service> -> Integrations ->
an "Events API v2" integration -> Integration Key.

Then, without echoing it into your shell history:
  read -rs "PD_ROUTING_KEY?PagerDuty routing key: " && export PD_ROUTING_KEY   # zsh
  read -rsp "PagerDuty routing key: " PD_ROUTING_KEY && export PD_ROUTING_KEY  # bash
MSG
  exit 1
fi

SCENARIO="${1:-payments-api-gateway}"
SNOW_BASE="${2:-}"
EVENT_ACTION="${PD_EVENT_ACTION:-trigger}"
DEDUP_KEY="${PD_DEDUP_KEY:-holodeck-${SCENARIO}-$(date -u +%Y%m%dT%H%M%SZ)}"

# Read the KB number and sys_id straight from the fixtures, so this script
# cannot drift out of sync with the articles the mock actually serves.
FIXTURES="$(cd "$(dirname "$0")/.." && pwd)/servicenow-mock/fixtures/scenarios.json"
read -r KB SYS_ID <<<"$(python3 - "$FIXTURES" "$SCENARIO" <<'PY'
import json, sys
try:
    art = json.load(open(sys.argv[1])).get(sys.argv[2]) or {}
except Exception:
    art = {}
print("%s %s" % (art.get("number", ""), art.get("sys_id", "")))
PY
)"

# Per-scenario summary + a sample query. PagerDuty's own guidance is that log
# queries work best when a sample query is present in the alert payload.
case "$SCENARIO" in
  payments-api-gateway)
    SUMMARY="payments-api-gateway: heap usage 92%, GC pauses lengthening"
    SAMPLE_QUERY='service:payments-api-gateway AND (heap OR OutOfMemoryError)'
    COMPONENT="jvm-heap" ;;
  payments-orchestrator-sync)
    SUMMARY="payments-orchestrator-sync: circuit breaker OPEN to payments-rules-engine"
    SAMPLE_QUERY='service:payments-orchestrator-sync AND circuit_breaker'
    COMPONENT="circuit-breaker" ;;
  idempotency-token-service)
    SUMMARY="idempotency-token-service: DB connection pool exhausted"
    SAMPLE_QUERY='service:idempotency-token-service AND connection_pool'
    COMPONENT="db-pool" ;;
  service-mesh-mtls)
    SUMMARY="service-mesh-mtls: elevated mTLS handshake failures"
    SAMPLE_QUERY='service:service-mesh-mtls AND (mtls OR certificate)'
    COMPONENT="istio-mtls" ;;
  edge-waf-cdn)
    SUMMARY="edge-waf-cdn: cache miss ratio spike, WAF challenge surge"
    SAMPLE_QUERY='service:edge-waf-cdn AND (cache_miss OR waf_challenge)'
    COMPONENT="cdn-edge" ;;
  checkout-api)
    SUMMARY="checkout-api: heap usage 88%, intermittent 503s at checkout"
    SAMPLE_QUERY='service:checkout-api AND (heap OR OutOfMemoryError)'
    COMPONENT="jvm-heap" ;;
  *)
    SUMMARY="${SCENARIO}: synthetic Holodeck test incident"
    SAMPLE_QUERY="service:${SCENARIO}"
    COMPONENT="unknown" ;;
esac

# KB number and sys_id go in the payload regardless, so the agent has both even
# if it ignores runbook_url.
KB_JSON=""
if [ -n "$KB" ]; then
  KB_JSON=",
      \"kb_article\": \"${KB}\",
      \"kb_sys_id\": \"${SYS_ID}\",
      \"runbook\": \"ServiceNow ${KB}\""
fi

# runbook_url MUST contain sys_id as a query parameter or the agent rejects it
# client-side without ever calling the server.
RUNBOOK_JSON=""
if [ -n "$SNOW_BASE" ] && [ -n "$SYS_ID" ]; then
  RUNBOOK_JSON=",
      \"runbook_url\": { \"servicenow\": \"${SNOW_BASE%/}/kb_view.do?sys_id=${SYS_ID}&sysparm_article=${KB}\" }"
fi

if [ "$EVENT_ACTION" = "trigger" ]; then
  PAYLOAD=$(cat <<JSON
{
  "routing_key": "${PD_ROUTING_KEY}",
  "event_action": "trigger",
  "dedup_key": "${DEDUP_KEY}",
  "payload": {
    "summary": "${SUMMARY}",
    "severity": "critical",
    "source": "${SCENARIO}",
    "component": "${COMPONENT}",
    "group": "payments-platform",
    "class": "holodeck-simulation",
    "custom_details": {
      "service": "${SCENARIO}",
      "service_name": "${SCENARIO}",
      "environment": "prod",
      "sample_query": "${SAMPLE_QUERY}",
      "note": "Synthetic incident from Holodeck. All mocks key off the service name above."${KB_JSON}${RUNBOOK_JSON}
    }
  }
}
JSON
)
else
  PAYLOAD=$(cat <<JSON
{
  "routing_key": "${PD_ROUTING_KEY}",
  "event_action": "${EVENT_ACTION}",
  "dedup_key": "${DEDUP_KEY}"
}
JSON
)
fi

echo "Scenario:     ${SCENARIO}"
echo "Action:       ${EVENT_ACTION}"
echo "Dedup key:    ${DEDUP_KEY}"
[ -n "$KB" ] && echo "KB article:   ${KB}  sys_id ${SYS_ID}"
[ -n "$SNOW_BASE" ] && [ -n "$SYS_ID" ] && echo "Runbook URL:  ${SNOW_BASE%/}/kb_view.do?sys_id=${SYS_ID}&sysparm_article=${KB}"
[ -n "$SNOW_BASE" ] && [ -z "$SYS_ID" ] && echo "Runbook URL:  (skipped — no sys_id found for '${SCENARIO}')"
echo ""

RESP=$(curl -s -m 20 -X POST https://events.pagerduty.com/v2/enqueue \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD")

echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
echo ""
echo "Next: open the incident in PagerDuty, select the SRE Agent tab, and ask"
echo "it to investigate (or let an Incident Workflow engage it automatically)."
echo "Then check which endpoints the mock received:"
echo "  curl -s http://127.0.0.1:4040/api/requests/http | python3 -m json.tool | grep '\"uri\"'"
echo ""
echo "To resolve:"
echo "  PD_EVENT_ACTION=resolve PD_DEDUP_KEY='${DEDUP_KEY}' $0 ${SCENARIO}"
