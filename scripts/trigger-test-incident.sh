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
# payload gets a runbook_url.servicenow pointing directly at that scenario's KB
# article — PagerDuty documents this as the way to point the SRE Agent at a
# specific ServiceNow runbook when it cannot infer one from the payload.
#
# The KB number is ALSO included as a plain custom_details field. Observed
# behaviour: when the agent only has search results it may decide none match
# and ask the responder for an article number, but when the number is in the
# payload it fetches the article directly.
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

# Per-scenario summary + a sample query. PagerDuty's own guidance is that log
# queries work best when a sample query is present in the alert payload.
case "$SCENARIO" in
  payments-api-gateway)
    SUMMARY="payments-api-gateway: heap usage 92%, GC pauses lengthening"
    SAMPLE_QUERY='service:payments-api-gateway AND (heap OR OutOfMemoryError)'
    COMPONENT="jvm-heap"; KB="KB0010023" ;;
  payments-orchestrator-sync)
    SUMMARY="payments-orchestrator-sync: circuit breaker OPEN to payments-rules-engine"
    SAMPLE_QUERY='service:payments-orchestrator-sync AND circuit_breaker'
    COMPONENT="circuit-breaker"; KB="KB0010031" ;;
  idempotency-token-service)
    SUMMARY="idempotency-token-service: DB connection pool exhausted"
    SAMPLE_QUERY='service:idempotency-token-service AND connection_pool'
    COMPONENT="db-pool"; KB="KB0010045" ;;
  service-mesh-mtls)
    SUMMARY="service-mesh-mtls: elevated mTLS handshake failures"
    SAMPLE_QUERY='service:service-mesh-mtls AND (mtls OR certificate)'
    COMPONENT="istio-mtls"; KB="KB0010052" ;;
  edge-waf-cdn)
    SUMMARY="edge-waf-cdn: cache miss ratio spike, WAF challenge surge"
    SAMPLE_QUERY='service:edge-waf-cdn AND (cache_miss OR waf_challenge)'
    COMPONENT="cdn-edge"; KB="KB0010067" ;;
  checkout-api)
    SUMMARY="checkout-api: heap usage 88%, intermittent 503s at checkout"
    SAMPLE_QUERY='service:checkout-api AND (heap OR OutOfMemoryError)'
    COMPONENT="jvm-heap"; KB="KB0010012" ;;
  *)
    SUMMARY="${SCENARIO}: synthetic Holodeck test incident"
    SAMPLE_QUERY="service:${SCENARIO}"
    COMPONENT="unknown"; KB="" ;;
esac

# The KB number goes in the payload regardless, so the agent can fetch the
# article directly instead of guessing from search results.
KB_JSON=""
if [ -n "$KB" ]; then
  KB_JSON=",
      \"kb_article\": \"${KB}\",
      \"runbook\": \"ServiceNow ${KB}\""
fi

# When a base URL is supplied, point runbook_url.servicenow at that exact
# article (the mock resolves by KB number as well as sys_id).
RUNBOOK_JSON=""
if [ -n "$SNOW_BASE" ] && [ -n "$KB" ]; then
  RUNBOOK_JSON=",
      \"runbook_url\": { \"servicenow\": \"${SNOW_BASE%/}/api/now/table/sn_km_mr_st_kb_knowledge/${KB}\" }"
elif [ -n "$SNOW_BASE" ]; then
  RUNBOOK_JSON=",
      \"runbook_url\": { \"servicenow\": \"${SNOW_BASE%/}/api/now/table/sn_km_mr_st_kb_knowledge?sysparm_query=number=${SCENARIO}\" }"
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
[ -n "$KB" ] && echo "KB article:   ${KB} (in custom_details.kb_article)"
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
