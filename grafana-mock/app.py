"""
Mock Grafana API server — GRAFANA ONLY.

This codebase has no knowledge of Arize whatsoever (no shared app.py, no
shared fixtures, no Arize routes defined anywhere). It exists as its own
independent service so PagerDuty's Grafana connector always talks to a host
that only ever answers Grafana-shaped requests.

Endpoints (mirrors real Grafana API):
  GET  /api/health
  GET  /api/datasources
  GET  /api/datasources/uid/{uid}
  POST /api/ds/query

Auth: `Authorization: Bearer <token>` — matches PagerDuty's "Service Account
Token" field on the Grafana connector. Set via MOCK_API_TOKEN.

Run:
    pip install -r requirements.txt
    MOCK_API_TOKEN=demo-token python app.py     # defaults to port 3000

Then:
    ngrok http 3000

PagerDuty "Add Grafana Connector":
    Grafana URL:           your ngrok https URL
    Service Account Token: demo-token
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

APP_TOKEN = os.environ.get("MOCK_API_TOKEN", "demo-token")
PORT = int(os.environ.get("PORT", "3000"))
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "scenarios.json"
METRICS_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "metric_scenarios.json"
DATASOURCE_UID = "mock-loki-uid"
DATASOURCE_NAME = "Mock Loki (SRE Agent Demo)"
METRICS_DATASOURCE_UID = "mock-prometheus-uid"
METRICS_DATASOURCE_NAME = "Mock Prometheus (SRE Agent Demo)"


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
METRIC_SCENARIOS = load_fixtures(METRICS_FIXTURES_PATH, "METRICS_FIXTURES_S3_KEY")


def check_auth():
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return jsonify({"message": "Missing or malformed Authorization header"}), 401
    token = authorization.removeprefix("Bearer ").strip()
    if token != APP_TOKEN:
        return jsonify({"message": "Invalid token"}), 401
    return None


def body_match_text(body: dict) -> str:
    """Search the WHOLE request body for a scenario keyword — more robust
    than checking only a couple of guessed field names."""
    try:
        return json.dumps(body).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def pick_scenario(scenarios: dict, text: str):
    q = (text or "").lower()
    for key, scenario in scenarios.items():
        if key == "default":
            continue
        if key.lower() in q:
            return key, scenario
    return "default", scenarios["default"]


def _build_metric_name_index(scenarios: dict) -> dict:
    return {
        scenario["metric_name"].lower(): (key, scenario)
        for key, scenario in scenarios.items()
        if "metric_name" in scenario
    }


METRIC_NAME_INDEX = _build_metric_name_index(METRIC_SCENARIOS)


def pick_metric_scenario(text: str):
    """
    For metrics specifically, an explicit known metric name in the query
    is authoritative and takes priority over service/host substring
    matching. Without this, a query like
    connection_pool_used{service="checkout-api"} would match the
    checkout-api SCENARIO via the service filter text, but then return
    checkout-api's own metric (heap_usage_percent) mislabeled as
    connection_pool_used — the exact bug real Prometheus wouldn't have,
    since checkout-api never emits connection_pool_used at all. This
    surfaced when the SRE Agent discovered all metric names via
    /label/__name__/values and then queried each one filtered by the same
    service, and every single metric came back showing the SAME curve.
    """
    q = (text or "").lower()
    for metric_name, (key, scenario) in METRIC_NAME_INDEX.items():
        if metric_name in q:
            return key, scenario
    # No explicit metric name found (e.g. a discovery-only call) — fall
    # back to the general service/host/key substring matcher.
    return pick_scenario(METRIC_SCENARIOS, text)


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


def build_loki_dataframe(scenario: dict, ref_id: str, reference_time: float) -> dict:
    logs = scenario["logs"]
    times_ns = [int((reference_time + entry["offset_seconds"]) * 1000) for entry in logs]
    lines = [entry["line"] for entry in logs]
    levels = [entry.get("level", "info") for entry in logs]
    labels = scenario.get("labels", {})

    return {
        "schema": {
            "refId": ref_id,
            "meta": {"preferredVisualisationType": "logs"},
            "fields": [
                {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                {"name": "Line", "type": "string", "typeInfo": {"frame": "string"}},
                {"name": "labels", "type": "string", "typeInfo": {"frame": "json.RawMessage"}},
                {"name": "level", "type": "string", "typeInfo": {"frame": "string"}},
            ],
        },
        "data": {
            "values": [times_ns, lines, [json.dumps(labels)] * len(lines), levels]
        },
    }


def build_metric_dataframe(scenario: dict, ref_id: str, reference_time: float) -> dict:
    points = scenario["points"]
    times_ms = [int((reference_time + p["offset_seconds"]) * 1000) for p in points]
    values = [p["value"] for p in points]
    labels = scenario.get("labels", {})
    metric_name = scenario.get("metric_name", "value")

    return {
        "schema": {
            "refId": ref_id,
            "meta": {"preferredVisualisationType": "graph"},
            "fields": [
                {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                {
                    "name": metric_name,
                    "type": "number",
                    "typeInfo": {"frame": "float64"},
                    "labels": labels,
                },
            ],
        },
        "data": {"values": [times_ms, values]},
    }
def all_metric_names() -> list:
    return sorted({s.get("metric_name", "value") for s in METRIC_SCENARIOS.values()})


def all_service_labels() -> list:
    return sorted({
        s["labels"]["service"]
        for s in METRIC_SCENARIOS.values()
        if "labels" in s and "service" in s["labels"]
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"commit": "mock", "database": "ok", "version": "mock-9.9.9"})


@app.route("/api/datasources", methods=["GET"])
def list_datasources():
    err = check_auth()
    if err:
        return err
    return jsonify([
        {
            "id": 1,
            "uid": DATASOURCE_UID,
            "name": DATASOURCE_NAME,
            "type": "loki",
            "access": "proxy",
            "isDefault": True,
        },
        {
            "id": 2,
            "uid": METRICS_DATASOURCE_UID,
            "name": METRICS_DATASOURCE_NAME,
            "type": "prometheus",
            "access": "proxy",
            "isDefault": False,
        },
    ])


@app.route("/api/datasources/uid/<uid>", methods=["GET"])
def get_datasource(uid):
    err = check_auth()
    if err:
        return err
    if uid == DATASOURCE_UID:
        return jsonify({
            "id": 1,
            "uid": DATASOURCE_UID,
            "name": DATASOURCE_NAME,
            "type": "loki",
            "access": "proxy",
            "jsonData": {"maxLines": 1000},
        })
    if uid == METRICS_DATASOURCE_UID:
        return jsonify({
            "id": 2,
            "uid": METRICS_DATASOURCE_UID,
            "name": METRICS_DATASOURCE_NAME,
            "type": "prometheus",
            "access": "proxy",
            "jsonData": {},
        })
    return jsonify({"message": "datasource not found"}), 404


def is_metrics_query(q: dict) -> bool:
    """
    Grafana's real /api/ds/query is shared by all datasource types — a query
    targets Loki (logs) or Prometheus (metrics) via its `datasource` object
    or `datasourceId`. Detect metrics queries that way; anything unspecified
    defaults to logs (the original/only behavior before this was added).
    """
    ds = q.get("datasource")
    if isinstance(ds, dict):
        if ds.get("uid") == METRICS_DATASOURCE_UID:
            return True
        if str(ds.get("type", "")).lower() == "prometheus":
            return True
    if q.get("datasourceUid") == METRICS_DATASOURCE_UID:
        return True
    if q.get("datasourceId") == 2:
        return True
    return False


@app.route("/api/ds/query", methods=["POST"])
def ds_query():
    err = check_auth()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    queries = body.get("queries", [])
    reference_time = extract_reference_time(body)

    results = {}
    for q in queries:
        ref_id = q.get("refId", "A")
        match_text = body_match_text(q)
        if is_metrics_query(q):
            _, scenario = pick_metric_scenario(match_text)
            frame = build_metric_dataframe(scenario, ref_id, reference_time)
        else:
            _, scenario = pick_scenario(SCENARIOS, match_text)
            frame = build_loki_dataframe(scenario, ref_id, reference_time)
        results[ref_id] = {"status": 200, "frames": [frame]}

    return jsonify({"results": results})


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "mock grafana api running (grafana-only codebase)"})


@app.route("/admin/reload", methods=["POST"])
def admin_reload():
    """Re-pull both fixture files from S3 (or local) without restarting the container.
    Also rebuilds METRIC_NAME_INDEX so new metric scenarios are visible immediately."""
    global SCENARIOS, METRIC_SCENARIOS, METRIC_NAME_INDEX
    try:
        SCENARIOS = load_fixtures(FIXTURES_PATH)
        METRIC_SCENARIOS = load_fixtures(METRICS_FIXTURES_PATH, "METRICS_FIXTURES_S3_KEY")
        METRIC_NAME_INDEX = _build_metric_name_index(METRIC_SCENARIOS)
        source = "s3" if (os.environ.get("FIXTURES_S3_BUCKET") and os.environ.get("FIXTURES_S3_KEY")) else "local"
        return jsonify({
            "status": "ok",
            "source": source,
            "scenarios": sorted(SCENARIOS.keys()),
            "metric_scenarios": sorted(METRIC_SCENARIOS.keys()),
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Grafana datasource-proxy routes — Prometheus's native HTTP API
#
# Discovered via live SRE Agent traffic: the agent doesn't only use Grafana's
# unified /api/ds/query endpoint for metrics — it also (or instead) calls
# Grafana's datasource-proxy pass-through, which forwards straight to the
# underlying Prometheus HTTP API:
#
#   GET /api/datasources/proxy/uid/{uid}/api/v1/label/__name__/values
#
# This is Prometheus's real, publicly documented "get label values" endpoint
# (https://prometheus.io/docs/prometheus/latest/querying/api/#getting-label-values)
# used to discover available metric names before running an actual query.
# Implemented here with high confidence since it's a stable public API, not
# a guess — unlike some of the other best-effort endpoints in this project.
#
# Also implements /api/v1/query_range and /api/v1/query (the standard
# Prometheus range/instant query endpoints) since metric-name discovery is
# almost always followed by an actual query call.
# ---------------------------------------------------------------------------

@app.route("/api/datasources/proxy/uid/<uid>/api/v1/<path:subpath>", methods=["GET"])
def prometheus_proxy(uid, subpath):
    err = check_auth()
    if err:
        return err
    if uid != METRICS_DATASOURCE_UID:
        return jsonify({"status": "error", "errorType": "not_found", "error": "unknown datasource"}), 404

    # Discovery: available metric names
    if subpath == "label/__name__/values":
        return jsonify({"status": "success", "data": all_metric_names()})

    # Discovery: available label names
    if subpath == "labels":
        return jsonify({"status": "success", "data": ["__name__", "service", "env"]})

    # Discovery: values for a specific label (only "service" is populated here)
    if subpath.startswith("label/") and subpath.endswith("/values"):
        label_name = subpath.split("/")[1]
        if label_name == "service":
            return jsonify({"status": "success", "data": all_service_labels()})
        return jsonify({"status": "success", "data": []})

    # Series discovery
    if subpath == "series":
        series = [
            {"__name__": s.get("metric_name", "value"), **s.get("labels", {})}
            for s in METRIC_SCENARIOS.values()
            if "labels" in s
        ]
        return jsonify({"status": "success", "data": series})

    # Actual range or instant query
    if subpath in ("query_range", "query"):
        query_text = request.args.get("query", "")
        end_param = parse_time(request.args.get("end"))
        reference_time = end_param if end_param is not None else time.time()

        _, scenario = pick_metric_scenario(query_text.lower())
        metric_name = scenario.get("metric_name", "value")
        labels = scenario.get("labels", {})
        points = scenario["points"]

        if subpath == "query_range":
            values = [[reference_time + p["offset_seconds"], str(p["value"])] for p in points]
            data = {
                "resultType": "matrix",
                "result": [{"metric": {"__name__": metric_name, **labels}, "values": values}],
            }
        else:
            last = points[-1]
            data = {
                "resultType": "vector",
                "result": [{"metric": {"__name__": metric_name, **labels}, "value": [reference_time, str(last["value"])]}],
            }
        return jsonify({"status": "success", "data": data})

    # Unknown subpath under the proxy — respond with empty success rather
    # than a hard 404, so an unanticipated discovery call doesn't break the
    # agent's flow outright. If you see this happening in ngrok's inspector,
    # add explicit handling for that subpath above.
    return jsonify({"status": "success", "data": []})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
