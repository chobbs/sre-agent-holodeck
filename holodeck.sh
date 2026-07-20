#!/usr/bin/env bash
# holodeck.sh — EC2 stack manager for the Holodeck SRE connector mocks.
#
# Runs ON the EC2 instance. For local dev with ngrok, use holodeck-local.sh.
#
# Usage:
#   ./holodeck.sh up                    # start all containers
#   ./holodeck.sh down                  # stop all containers
#   ./holodeck.sh restart [service]     # restart one or all containers
#   ./holodeck.sh status                # show container status + URLs
#   ./holodeck.sh logs [service]        # tail logs (Ctrl+C to exit)
#   ./holodeck.sh reload-fixtures       # hot-reload fixture JSON from S3
#   ./holodeck.sh sync-fixtures         # upload local JSON to S3 + reload

set -uo pipefail

BASE_DIR="${BASE_DIR:-$(cd "$(dirname "$0")" && pwd)}"

SERVICES=(grafana arize splunk elasticsearch)
DOMAIN="${HOLODECK_DOMAIN:-holodeck.scsandbox.net}"

# Use sudo only if docker isn't accessible directly
if docker info > /dev/null 2>&1; then
  DC="docker compose -f ${BASE_DIR}/docker-compose.yml"
  DOCKER="docker"
else
  DC="sudo docker compose -f ${BASE_DIR}/docker-compose.yml"
  DOCKER="sudo docker"
fi

port_for() {
  case "$1" in
    grafana)       echo 3000 ;;
    arize)         echo 3001 ;;
    splunk)        echo 3002 ;;
    elasticsearch) echo 3003 ;;
  esac
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_up() {
  echo "Starting Holodeck stack..."
  $DC up -d
  echo ""
  cmd_status
}

cmd_down() {
  echo "Stopping Holodeck stack..."
  $DC down
}

cmd_restart() {
  local svc="${1:-}"
  if [ -n "$svc" ]; then
    echo "Restarting ${svc}..."
    $DC restart "$svc"
  else
    echo "Restarting all services..."
    $DC restart
  fi
  sleep 2
  cmd_status
}

cmd_status() {
  $DC ps
  echo ""
  echo "Endpoints:"
  for svc in "${SERVICES[@]}"; do
    echo "  https://${svc}.${DOMAIN}"
  done
}

cmd_logs() {
  local svc="${1:-}"
  if [ -n "$svc" ]; then
    $DC logs -f "$svc"
  else
    $DC logs -f
  fi
}

cmd_reload_fixtures() {
  echo "Reloading fixtures from S3..."
  local failed=0
  for svc in "${SERVICES[@]}"; do
    local port container result
    port=$(port_for "$svc")
    container="holodeck-${svc}"
    echo -n "  ${svc}: "
    if result=$($DOCKER exec "$container" curl -sf -X POST "http://localhost:${port}/admin/reload" 2>&1); then
      echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
src = d.get('source', '?')
s = d.get('scenarios', [])
ms = d.get('metric_scenarios', [])
parts = [f'source={src}', f'{len(s)} scenarios']
if ms:
    parts.append(f'{len(ms)} metric_scenarios')
print('ok (' + ', '.join(parts) + ')')
" 2>/dev/null || echo "ok"
    else
      echo "FAILED — is the container running? (docker logs ${container})"
      failed=1
    fi
  done
  return $failed
}

cmd_sync_fixtures() {
  echo "Uploading fixture JSON to S3..."
  bash "${BASE_DIR}/scripts/sync-fixtures-to-s3.sh"
  echo ""
  cmd_reload_fixtures
}

show_usage() {
  cat <<EOF
Holodeck — EC2 stack manager
Usage: $0 <command> [args]

Commands:
  up                   Build (if needed) and start all containers
  down                 Stop and remove all containers
  restart [service]    Restart one service or all (omit service for all)
  status               Show container health + endpoint URLs
  logs [service]       Tail logs for one service or all (Ctrl+C to exit)
  reload-fixtures      Pull fresh fixture JSON from S3 into running containers
  sync-fixtures        Upload local fixtures/ dirs to S3, then reload all

Services: ${SERVICES[*]}

Endpoints:
$(for svc in "${SERVICES[@]}"; do echo "  https://${svc}.${DOMAIN}"; done)
EOF
}

case "${1:-}" in
  up)               cmd_up ;;
  down)             cmd_down ;;
  restart)          cmd_restart "${2:-}" ;;
  status)           cmd_status ;;
  logs)             cmd_logs "${2:-}" ;;
  reload-fixtures)  cmd_reload_fixtures ;;
  sync-fixtures)    cmd_sync_fixtures ;;
  *)                show_usage; exit 1 ;;
esac
