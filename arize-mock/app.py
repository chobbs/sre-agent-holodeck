"""
Mock Arize API server — ARIZE ONLY.

This codebase has no knowledge of Grafana whatsoever (no shared app.py, no
shared fixtures, no Grafana routes defined anywhere). It exists as its own
independent service so PagerDuty's Arize connector always talks to a host
that only ever answers Arize-shaped requests.

Endpoints:
  GET  /v2/spaces    - CONFIRMED via live SRE Agent traffic (re-added after
                       being wrongly removed — the agent calls BOTH this
                       AND /v2/projects, matching Arize's real Space >
                       Project > Model resource hierarchy)
  GET  /v2/projects  - CONFIRMED via live SRE Agent traffic
  POST /v2/spans     - CONFIRMED against Arize's real public REST API
  POST /graphql      - CONFIRMED via live SRE Agent traffic. Metrics come
                       through Arize's real GraphQL API, querying its
                       Monitors feature (getMonitorCalculations), NOT a
                       plain REST /v2/metrics endpoint.

Auth: the agent has been observed using TWO different header styles across
these endpoints — `Authorization: Bearer <token>` for /v2/spans and
`X-Api-Key: <token>` for /graphql. Both are accepted here, checked against
the same MOCK_API_TOKEN.

Run:
    pip install -r requirements.txt
    MOCK_API_TOKEN=demo-token python app.py     # defaults to port 3001

Then:
    ngrok http 3001

PagerDuty "Add Arize Connector":
    Host:    your ngrok https URL
    API Key: demo-token
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

APP_TOKEN = os.environ.get("MOCK_API_TOKEN", "demo-token")
PORT = int(os.environ.get("PORT", "3001"))
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "scenarios.json"


def load_fixtures(local_path: Path, s3_key_env: str = "FIXTURES_S3_KEY") -> dict:
    """Load fixture JSON from S3 when FIXTURES_S3_BUCKET + s3_key_env are set,
    otherwise fall back to the local file.  boto3 is imported lazily so the
    service starts fine with no AWS credentials configured."""
    bucket = os.environ.get("FIXTURES_S3_BUCKET", "")
    key = os.environ.get(s3_key_env, "")
    if bucket and key:
        import boto3  # noqa: PLC0415
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        print(f"[holodeck] loaded fixtures from s3://{bucket}/{key}", flush=True)
        return data
    with open(local_path) as f:
        data = json.load(f)
    print(f"[holodeck] loaded fixtures from {local_path}", flush=True)
    return data


app = Flask(__name__)

SCENARIOS = load_fixtures(FIXTURES_PATH)


def check_auth():
    """
    Live traffic showed the agent using two different header styles across
    endpoints: `Authorization: Bearer <token>` for /v2/spans, and
    `X-Api-Key: <token>` for /graphql. Accept either, checked against the
    same token, rather than assuming one style everywhere.
    """
    authorization = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-Api-Key", "")

    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token == APP_TOKEN:
            return None
        return jsonify({"message": "Invalid token"}), 401

    if api_key_header:
        if api_key_header.strip() == APP_TOKEN:
            return None
        return jsonify({"message": "Invalid token"}), 401

    return jsonify({"message": "Missing Authorization or X-Api-Key header"}), 401


def body_match_text(body: dict) -> str:
    """Search the WHOLE request body for a scenario keyword — more robust
    than checking only a couple of guessed field names."""
    try:
        return json.dumps(body).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def pick_scenario(text: str):
    q = (text or "").lower()
    for key, scenario in SCENARIOS.items():
        if key == "default":
            continue
        if key.lower() in q:
            return key, scenario
    return "default", SCENARIOS["default"]


def parse_time(value):
    """Best-effort parse of ISO8601 string or epoch seconds/millis."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value) / 1000.0 if value > 1e12 else float(value)
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def extract_reference_time(body: dict) -> float:
    """
    Anchor synthetic timestamps to the request's actual time window (e.g.
    the incident's time, which can be well in the past) rather than
    wall-clock 'now' — otherwise synthetic data can fall outside whatever
    window the caller filters against and looks like "no results".
    """
    for key in ("end_time", "to", "endTime"):
        parsed = parse_time(body.get(key))
        if parsed is not None:
            return parsed
    for outer in ("range", "dataset", "time_range", "timeRange"):
        nested = body.get(outer)
        if isinstance(nested, dict):
            for key in ("end_time", "to", "endTime", "end"):
                parsed = parse_time(nested.get(key))
                if parsed is not None:
                    return parsed
    return time.time()


def build_spans_response(scenario: dict, reference_time: float) -> dict:
    spans = []
    for entry in scenario["spans"]:
        start = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time + entry["offset_seconds"]))
        end = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z",
            time.gmtime(reference_time + entry["offset_seconds"] + entry["duration_ms"] / 1000),
        )
        spans.append({
            "name": entry["name"],
            "context": {"trace_id": entry["trace_id"], "span_id": entry["span_id"]},
            "kind": entry["kind"],
            "status_code": entry["status_code"],
            "start_time": start,
            "end_time": end,
            "attributes": entry["attributes"],
        })
    return {"spans": spans, "pagination": {"next_cursor": None, "has_more": False}}


@app.route("/v2/spaces", methods=["GET"])
def list_spaces():
    """
    Re-added after being wrongly removed — live traffic showed the agent
    calls BOTH /v2/spaces and /v2/projects, not one or the other. Matches
    Arize's real resource hierarchy (Space > Project > Model): the agent
    likely lists spaces first, then projects within a space.
    """
    err = check_auth()
    if err:
        return err
    return jsonify({
        "spaces": [{"id": "mock-space-id", "name": "SRE Agent Demo Space"}],
        "pagination": {"next_cursor": None, "has_more": False},
    })


@app.route("/v2/projects", methods=["GET"])
def list_projects():
    err = check_auth()
    if err:
        return err
    name_filter = (request.args.get("name") or "").lower()
    projects = [
        {"id": key, "name": key}
        for key in SCENARIOS.keys()
        if key != "default" and (not name_filter or name_filter in key.lower())
    ]
    return jsonify({
        "projects": projects,
        "pagination": {"next_cursor": None, "has_more": False},
    })


@app.route("/v2/spans", methods=["POST"])
def list_spans():
    err = check_auth()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    _, scenario = pick_scenario(body_match_text(body))
    reference_time = extract_reference_time(body)
    return jsonify(build_spans_response(scenario, reference_time))


def build_monitor_calculations_response(monitor_id: str, scenario: dict, reference_time: float) -> dict:
    """
    Shapes a response matching the exact GraphQL query observed in live
    traffic:

        query getMonitorCalculations($monitorId: ID!, ...) {
          node(id: $monitorId) {
            ... on Monitor {
              id
              name
              calculationsWithinTimeRange(...) {
                computedValue
                evaluatedAt
                computedThreshold
                calculationStatus
              }
            }
          }
        }
    """
    monitor = scenario.get("monitor", {"name": "unknown monitor", "threshold": 0, "calculations": []})
    calculations = []
    for calc in monitor.get("calculations", []):
        evaluated_at = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time + calc["offset_seconds"])
        )
        calculations.append({
            "computedValue": calc["computed_value"],
            "evaluatedAt": evaluated_at,
            "computedThreshold": monitor.get("threshold", 0),
            "calculationStatus": calc.get("status", "ok").upper(),
        })

    return {
        "data": {
            "node": {
                "id": monitor_id,
                "name": monitor.get("name", monitor_id),
                "calculationsWithinTimeRange": calculations,
            }
        }
    }


@app.route("/graphql", methods=["POST"])
def graphql():
    err = check_auth()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    query_text = body.get("query", "")
    variables = body.get("variables", {}) or {}

    # Only one operation observed in live traffic so far — extend this
    # if/elif chain (matching on a substring of the query text) as more
    # GraphQL operations get discovered via ngrok's inspector.
    if "calculationsWithinTimeRange" in query_text:
        monitor_id = variables.get("monitorId", "")
        _, scenario = pick_scenario(str(monitor_id).lower())
        end_time = parse_time(variables.get("endTime"))
        reference_time = end_time if end_time is not None else time.time()
        return jsonify(build_monitor_calculations_response(monitor_id, scenario, reference_time))

    # Unknown GraphQL operation — return a GraphQL-shaped error rather than
    # a plain 404/500, since that's what a real GraphQL server would do.
    # If you see this in ngrok's inspector, that's the signal to add
    # explicit handling for the new operation above.
    return jsonify({
        "errors": [{"message": "Unrecognized query in mock — no matching operation implemented"}],
        "data": None,
    }), 200


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "mock arize api running (arize-only codebase)"})


@app.route("/admin/reload", methods=["POST"])
def admin_reload():
    """Re-pull fixture JSON from S3 (or local file) without restarting the container.
    Useful after pushing updated scenario files to S3 mid-demo."""
    global SCENARIOS
    try:
        SCENARIOS = load_fixtures(FIXTURES_PATH)
        source = "s3" if (os.environ.get("FIXTURES_S3_BUCKET") and os.environ.get("FIXTURES_S3_KEY")) else "local"
        return jsonify({"status": "ok", "source": source, "scenarios": sorted(SCENARIOS.keys())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)