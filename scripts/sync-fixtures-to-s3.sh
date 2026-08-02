#!/usr/bin/env bash
# sync-fixtures-to-s3.sh
#
# Uploads all fixture JSON files to S3 using the key layout expected by
# the mock services:
#
#   s3://<bucket>/grafana/scenarios.json
#   s3://<bucket>/grafana/metric_scenarios.json
#   s3://<bucket>/arize/scenarios.json
#   s3://<bucket>/splunk/scenarios.json
#   s3://<bucket>/elasticsearch/scenarios.json
#
# Usage:
#   ./scripts/sync-fixtures-to-s3.sh <bucket-name>
#   # or set FIXTURES_S3_BUCKET in your environment / .env and run without args
#
# After uploading, hit the reload endpoint on each running service to pick
# up the new scenarios without a container restart:
#   ./holodeck.sh reload-fixtures       (once Phase 5 holodeck.sh is in place)
#   # or manually:
#   curl -sf -X POST http://<host>:8080/admin/reload | jq .
#   curl -sf -X POST http://<host>:8081/admin/reload | jq .
#   curl -sf -X POST http://<host>:8082/admin/reload | jq .
#   curl -sf -X POST http://<host>:8083/admin/reload | jq .

set -euo pipefail

BUCKET="${1:-${FIXTURES_S3_BUCKET:-}}"
if [ -z "$BUCKET" ]; then
  echo "Error: no bucket specified."
  echo "Usage: $0 <bucket-name>"
  echo "       or set FIXTURES_S3_BUCKET in your environment"
  exit 1
fi

BASE="$(cd "$(dirname "$0")/.." && pwd)"

upload() {
  local local_path="$1"
  local s3_key="$2"
  echo "  uploading s3://${BUCKET}/${s3_key} ..."
  aws s3 cp "$local_path" "s3://${BUCKET}/${s3_key}"
}

echo "Syncing fixtures to s3://${BUCKET}/ ..."
upload "${BASE}/grafana-mock/fixtures/scenarios.json"        "grafana/scenarios.json"
upload "${BASE}/grafana-mock/fixtures/metric_scenarios.json" "grafana/metric_scenarios.json"
upload "${BASE}/arize-mock/fixtures/scenarios.json"          "arize/scenarios.json"
upload "${BASE}/splunk-mock/fixtures/scenarios.json"         "splunk/scenarios.json"
upload "${BASE}/elasticsearch-mock/fixtures/scenarios.json"  "elasticsearch/scenarios.json"
upload "${BASE}/dynatrace-mock/fixtures/scenarios.json"      "dynatrace/scenarios.json"

echo ""
echo "Done. All 6 fixture files synced to s3://${BUCKET}/"
echo ""
echo "To hot-reload without restarting containers, POST to /admin/reload on each service."
