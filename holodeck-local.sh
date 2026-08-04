#!/usr/bin/env bash
# Holodeck — local dev runner for the SRE Agent PoV mock services
# (grafana-mock, arize-mock, splunk-mock, elasticsearch-mock, servicenow-mock)
# — runs one service (flask + ngrok) at a time for local connector testing.
#
# "Computer, run a payments outage simulation." Runs ONLY ONE service
# (flask + ngrok) at a time, since that's all you need for testing one
# connector against the same static ngrok domain. Switching to a
# different service automatically stops whichever was running before.
#
# Usage:
#   ./holodeck.sh                 # interactive menu
#   ./holodeck.sh grafana         # switch to grafana-mock, no menu
#   ./holodeck.sh arize           # switch to arize-mock, no menu
#   ./holodeck.sh splunk          # switch to splunk-mock, no menu
#   ./holodeck.sh elasticsearch   # switch to elasticsearch-mock, no menu
#   ./holodeck.sh servicenow      # switch to servicenow-mock, no menu
#   ./holodeck.sh restart         # restart current service (picks up code edits)
#   ./holodeck.sh stop            # stop whichever is currently running
#   ./holodeck.sh status          # show what's currently running

set -uo pipefail

BASE_DIR="${BASE_DIR:-/Users/chobbs/Tools/sre-conn-simulator}"
NGROK_DOMAIN="${NGROK_DOMAIN:-series-punctured-submersed.ngrok-free.dev}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/holodeck-runtime}"
mkdir -p "$RUNTIME_DIR"

CURRENT_FILE="$RUNTIME_DIR/current_service"
FLASK_PID_FILE="$RUNTIME_DIR/flask.pid"
NGROK_PID_FILE="$RUNTIME_DIR/ngrok.pid"
FLASK_LOG_FILE="$RUNTIME_DIR/flask.log"
NGROK_LOG_FILE="$RUNTIME_DIR/ngrok.log"

port_for() {
  case "$1" in
    grafana)       echo 3000 ;;
    arize)         echo 3001 ;;
    splunk)        echo 3002 ;;
    elasticsearch) echo 3003 ;;
    servicenow)    echo 3005 ;;
    *) echo "" ;;
  esac
}

dir_for() {
  case "$1" in
    grafana)       echo "$BASE_DIR/grafana-mock" ;;
    arize)         echo "$BASE_DIR/arize-mock" ;;
    splunk)        echo "$BASE_DIR/splunk-mock" ;;
    elasticsearch) echo "$BASE_DIR/elasticsearch-mock" ;;
    servicenow)    echo "$BASE_DIR/servicenow-mock" ;;
    *) echo "" ;;
  esac
}

is_running() {
  local pid_file="$1"
  [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null
}

# Whatever is actually LISTENING on a port, regardless of what we recorded.
#
# This exists because `$!` captures the setsid/env wrapper, not the python
# process it execs. Killing the recorded pid could therefore leave python alive
# still holding the port — which silently serves stale code on the next start
# (observed: "flask FAILED to start / Address already in use" while the old
# build kept answering requests).
listener_pid() {
  local port="$1"
  [ -n "$port" ] || return 0
  lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1
}

current_service() {
  [ -f "$CURRENT_FILE" ] && cat "$CURRENT_FILE" || echo ""
}

stop_current() {
  local current
  current=$(current_service)

  if [ -z "$current" ] && ! is_running "$FLASK_PID_FILE" && ! is_running "$NGROK_PID_FILE"; then
    echo "Nothing currently running."
    return
  fi

  if is_running "$FLASK_PID_FILE"; then
    local f_pid
    f_pid=$(cat "$FLASK_PID_FILE")
    kill "$f_pid" 2>/dev/null
    echo "Stopped flask (${current:-unknown}, pid $f_pid)"
  fi
  rm -f "$FLASK_PID_FILE"

  # Belt and braces: kill anything still bound to the port. Without this, a
  # wrapper/child pid mismatch leaves an orphan serving stale code.
  local port orphan
  port=$(port_for "${current:-}")
  orphan=$(listener_pid "$port")
  if [ -n "$orphan" ]; then
    kill "$orphan" 2>/dev/null
    sleep 1
    orphan=$(listener_pid "$port")
    [ -n "$orphan" ] && kill -9 "$orphan" 2>/dev/null
    echo "Stopped orphaned listener on port $port"
  fi

  if is_running "$NGROK_PID_FILE"; then
    local n_pid
    n_pid=$(cat "$NGROK_PID_FILE")
    kill "$n_pid" 2>/dev/null
    echo "Stopped ngrok (${current:-unknown}, pid $n_pid)"
  fi
  rm -f "$NGROK_PID_FILE"

  rm -f "$CURRENT_FILE"
}

start_service() {
  local name="$1"
  local port dir
  port=$(port_for "$name")
  dir=$(dir_for "$name")

  if [ -z "$port" ]; then
    echo "Unknown service: $name (expected grafana, arize, splunk, elasticsearch, or servicenow)"
    return 1
  fi
  if [ ! -d "$dir" ]; then
    echo "Directory not found: $dir"
    return 1
  fi

  local existing
  existing=$(current_service)
  if [ "$existing" = "$name" ] && is_running "$FLASK_PID_FILE" && is_running "$NGROK_PID_FILE"; then
    echo "$name is already running (flask pid $(cat "$FLASK_PID_FILE"), ngrok pid $(cat "$NGROK_PID_FILE"))"
    return
  fi

  if [ -n "$existing" ]; then
    echo "Switching from $existing to $name — stopping $existing first..."
    stop_current
  fi

  echo "Starting $name (flask on port $port + ngrok)..."

  # Most mocks just need MOCK_API_TOKEN. ServiceNow uses an OAuth2 password
  # grant, so it needs client id/secret plus username/password.
  local -a env_vars=(MOCK_API_TOKEN=demo-token)
  if [ "$name" = "servicenow" ]; then
    env_vars+=(
      MOCK_CLIENT_ID=demo-client
      MOCK_CLIENT_SECRET=demo-secret
      MOCK_USERNAME=demo-user
      MOCK_PASSWORD=demo-pass
    )
  fi

  if command -v setsid >/dev/null 2>&1; then
    ( cd "$dir" && setsid env "${env_vars[@]}" python3 app.py < /dev/null > "$FLASK_LOG_FILE" 2>&1 & echo $! > "$FLASK_PID_FILE" ) < /dev/null > /dev/null 2>&1
  else
    ( cd "$dir" && env "${env_vars[@]}" nohup python3 app.py < /dev/null > "$FLASK_LOG_FILE" 2>&1 & echo $! > "$FLASK_PID_FILE" ) < /dev/null > /dev/null 2>&1
  fi
  sleep 2
  # Replace the wrapper pid with the pid actually holding the port, so `stop`
  # can reliably kill the real server later.
  local real_pid
  real_pid=$(listener_pid "$port")
  if [ -n "$real_pid" ]; then
    echo "$real_pid" > "$FLASK_PID_FILE"
    echo "  flask started (pid $real_pid, port $port)"
  else
    echo "  flask FAILED to start — check $FLASK_LOG_FILE"
    grep -iE "address already in use|error|traceback" "$FLASK_LOG_FILE" 2>/dev/null | tail -3 | sed 's/^/    /'
  fi

  if command -v setsid >/dev/null 2>&1; then
    ( setsid ngrok http --url="$NGROK_DOMAIN" "$port" < /dev/null > "$NGROK_LOG_FILE" 2>&1 & echo $! > "$NGROK_PID_FILE" ) < /dev/null > /dev/null 2>&1
  else
    ( nohup ngrok http --url="$NGROK_DOMAIN" "$port" < /dev/null > "$NGROK_LOG_FILE" 2>&1 & echo $! > "$NGROK_PID_FILE" ) < /dev/null > /dev/null 2>&1
  fi
  sleep 1
  if is_running "$NGROK_PID_FILE"; then
    echo "  ngrok started -> $NGROK_DOMAIN (pid $(cat "$NGROK_PID_FILE"))"
  else
    echo "  ngrok FAILED to start — check $NGROK_LOG_FILE"
  fi

  echo "$name" > "$CURRENT_FILE"
}

status() {
  local current
  current=$(current_service)
  if [ -z "$current" ]; then
    echo "Holodeck is idle — nothing currently running."
    return
  fi
  echo "Current simulation: $current (port $(port_for "$current"))"
  if is_running "$FLASK_PID_FILE"; then
    echo "  flask: RUNNING (pid $(cat "$FLASK_PID_FILE"))"
  else
    echo "  flask: stopped (unexpectedly? pid file present but process gone)"
  fi
  if is_running "$NGROK_PID_FILE"; then
    echo "  ngrok:  RUNNING (pid $(cat "$NGROK_PID_FILE"))"
  else
    echo "  ngrok:  stopped (unexpectedly? pid file present but process gone)"
  fi
}

show_menu() {
  echo ""
  echo "Holodeck — SRE Connector Mock Runner"
  echo "====================================="
  echo "Currently running: $(current_service || echo 'none')"
  echo ""
  echo "1) Grafana        (port 3000)"
  echo "2) Arize          (port 3001)"
  echo "3) Splunk         (port 3002)"
  echo "4) Elasticsearch  (port 3003)"
  echo "5) ServiceNow     (port 3005)"
  echo "6) Stop"
  echo "7) Status"
  echo "8) Quit"
  echo ""
  read -rp "Choose an option [1-8]: " choice
  case "$choice" in
    1) start_service grafana ;;
    2) start_service arize ;;
    3) start_service splunk ;;
    4) start_service elasticsearch ;;
    5) start_service servicenow ;;
    6) stop_current ;;
    7) status ;;
    8) echo "Exiting the holodeck."; exit 0 ;;
    *) echo "Invalid option: $choice" ;;
  esac
}

run_main() {
  if [ "${1:-}" != "" ]; then
    case "$1" in
      grafana|arize|splunk|elasticsearch|servicenow) start_service "$1" ;;
      restart)
        # Needed after editing a mock's app.py: starting the same service is a
        # no-op when it is already running, so it would keep serving old code.
        svc=$(current_service)
        if [ -z "$svc" ]; then
          echo "Nothing running to restart."
        else
          stop_current
          sleep 1
          start_service "$svc"
        fi ;;
      stop)   stop_current ;;
      status) status ;;
      *) echo "Usage: $0 [grafana|arize|splunk|elasticsearch|servicenow|restart|stop|status]"; exit 1 ;;
    esac
    exit 0
  fi
  while true; do
    show_menu
  done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  run_main "$@"
fi
