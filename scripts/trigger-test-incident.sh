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
# The optional 2nd arg is the ServiceNow mock's base URL. When given, a
# runbook_url.servicenow hint is added to custom_details — PagerDuty documents
# this as the way to point the SRE Agent at a specific ServiceNow runbook when
# it cannot infer one from the payload.
#
# Scenario keys shared by the elasticsearch + servicenow mocks:
#   payments-api-gateway  payments-orchestrator-sync  idempotency-token-service
#   service-mesh-mtls     edge-waf-cdn
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
  *)
    SUMMARY="${SCENARIO}: synthetic Holodeck test incident"
    SAMPLE_QUERY="service:${SCENARIO}"
    COMPONENT="unknown" ;;
esac

RUNBOOK_JSON=""
if [ -n "$SNOW_BASE" ]; then
  RUNBOOK_JSON=$(cat <<JSON
,
      "runbook_url": { "servicenow": "${SNOW_BASE%/}/api/now/table/kb_knowledge?sysparm_query=${SCENARIO}" }
JSON
)
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
      "note": "Synthetic incident from Holodeck. All mocks key off the service name above."${RUNBOOK_JSON}
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
[ -n "$SNOW_BASE" ] && echo "Runbook hint: ${SNOW_BASE%/} (servicenow)"
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
