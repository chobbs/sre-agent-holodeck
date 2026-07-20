"""
Mock Elasticsearch API server — ELASTICSEARCH ONLY, LOGS ONLY.

This codebase has no knowledge of Grafana, Arize, or Splunk (no shared
app.py, no shared fixtures). It exists as its own independent service so
PagerDuty's Elasticsearch connector always talks to a host that only ever
answers Elasticsearch-shaped requests.

Implements the real Elasticsearch REST API surface behind PagerDuty's
"Elasticsearch: Search Logs" action, confirmed against PagerDuty's own
public docs (https://support.pagerduty.com/actions/docs/elasticsearch-search-logs):

  GET  /                                  - cluster info / credential check
  GET  /_cluster/health                   - added proactively alongside "/",
                                             in case the connector's health
                                             probe uses this instead — cheap
                                             insurance based on how often
                                             this project has hit a second,
                                             undocumented discovery call
  POST /<index_pattern>/_search           - Lucene, KQL, and Query DSL all
                                             go through this same endpoint
                                             per PD's docs; only the request
                                             body's shape differs
  POST /<index_pattern>/_eql/search       - EQL query type, real ES EQL API
                                             (distinct response shape: an
                                             "events" list, not "hits")

Auth: Elasticsearch's real scheme is `Authorization: ApiKey <token>`. Set
the expected token via MOCK_API_TOKEN.

Scenario matching: substring match against the ENTIRE request body (not
just a couple of guessed fields) plus the index_pattern and each
scenario's `namespace`/`node` values — lessons learned the hard way from
the Splunk mock, where the SRE Agent turned out to query by structured
field values rather than any literal scenario name.

Time handling: PagerDuty's own docs say explicitly that it injects NO
default time range — callers must embed time constraints in the query
itself (e.g. a Query DSL `range` filter on `@timestamp`). This mock does a
best-effort scan of the raw request body for ISO8601 timestamps or long
epoch numbers and uses the latest one found as the reference "now" for
generating synthetic log timestamps; falls back to wall-clock time if none
found.

Run:
    pip install -r requirements.txt
    MOCK_API_TOKEN=demo-token python app.py     # defaults to port 3003

Then:
    ngrok http 3003

PagerDuty "Add Elasticsearch Connector":
    Elasticsearch URL:  your ngrok https URL
    API Key:            demo-token
"""

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

APP_TOKEN = os.environ.get("MOCK_API_TOKEN", "demo-token")
PORT = int(os.environ.get("PORT", "3003"))
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
    """Elasticsearch's real scheme: `Authorization: ApiKey <token>`."""
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("ApiKey "):
        return jsonify({"message": "Missing or malformed Authorization header (expected 'ApiKey <token>')"}), 401
    token = authorization.removeprefix("ApiKey ").strip()
    if token != APP_TOKEN:
        return jsonify({"message": "Invalid token"}), 401
    return None


def body_match_text(raw_body: str, index_pattern: str) -> str:
    return f"{index_pattern} {raw_body}".lower()


def pick_scenario(text: str):
    q = (text or "").lower()
    for key, scenario in SCENARIOS.items():
        if key == "default":
            continue
        candidates = [key, scenario.get("namespace", ""), scenario.get("node", "")]
        if any(candidate and candidate.lower() in q for candidate in candidates):
            return key, scenario
    return "default", SCENARIOS["default"]


def extract_reference_time(raw_body: str) -> float:
    """
    Best-effort: no structured time field is guaranteed here (PD injects no
    default time range, and Query DSL/Lucene/KQL/EQL all shape time bounds
    differently). Scan for the latest ISO8601 timestamp or plausible epoch
    number in the raw body text; fall back to wall-clock time.
    """
    iso_matches = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw_body or "")
    parsed_times = []
    for m in iso_matches:
        try:
            parsed_times.append(datetime.fromisoformat(m).timestamp())
        except ValueError:
            continue
    if parsed_times:
        return max(parsed_times)

    epoch_matches = re.findall(r"\b(\d{10,13})\b", raw_body or "")
    epoch_values = []
    for m in epoch_matches:
        value = float(m)
        epoch_values.append(value / 1000.0 if value > 1e12 else value)
    if epoch_values:
        return max(epoch_values)

    return time.time()


def build_hits(scenario: dict, reference_time: float, index_pattern: str):
    hits = []
    for entry in scenario.get("logs", []):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time + entry["offset_seconds"]))
        hits.append({
            "_index": index_pattern,
            "_id": uuid.uuid4().hex[:12],
            "_score": 1.0,
            "_source": {
                "@timestamp": ts,
                "message": entry["line"],
                "level": entry.get("level", "info"),
                "kubernetes": {
                    "namespace": scenario.get("namespace", "unknown"),
                    "node": scenario.get("node", "unknown"),
                },
            },
        })
    return hits


@app.route("/", methods=["GET"])
def cluster_info():
    err = check_auth()
    if err:
        return err
    return jsonify({
        "name": "mock-node-1",
        "cluster_name": "mock-cluster",
        "version": {"number": "8.13.0"},
        "tagline": "You Know, for Search",
    })


@app.route("/_cluster/health", methods=["GET"])
def cluster_health():
    err = check_auth()
    if err:
        return err
    return jsonify({"cluster_name": "mock-cluster", "status": "green", "number_of_nodes": 1})


@app.route("/<path:index_pattern>/_search", methods=["POST"])
def search(index_pattern):
    err = check_auth()
    if err:
        return err

    raw_body = request.get_data(as_text=True) or ""
    body = request.get_json(force=True, silent=True) or {}
    size = body.get("size", 100)

    _, scenario = pick_scenario(body_match_text(raw_body, index_pattern))
    reference_time = extract_reference_time(raw_body)
    hits = build_hits(scenario, reference_time, index_pattern)[:size]

    return jsonify({
        "took": 12,
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
        "hits": {
            "total": {"value": len(hits), "relation": "eq"},
            "max_score": 1.0,
            "hits": hits,
        },
    })


@app.route("/<path:index_pattern>/_eql/search", methods=["POST"])
def eql_search(index_pattern):
    err = check_auth()
    if err:
        return err

    raw_body = request.get_data(as_text=True) or ""
    body = request.get_json(force=True, silent=True) or {}
    size = body.get("size", 100)

    _, scenario = pick_scenario(body_match_text(raw_body, index_pattern))
    reference_time = extract_reference_time(raw_body)
    events = build_hits(scenario, reference_time, index_pattern)[:size]

    # Real ES EQL search response shape uses "events", not "hits", inside
    # the hits object — genuinely different from _search's response.
    return jsonify({
        "is_partial": False,
        "is_running": False,
        "took": 15,
        "timed_out": False,
        "hits": {
            "total": {"value": len(events), "relation": "eq"},
            "events": events,
        },
    })


@app.route("/admin/reload", methods=["POST"])
def admin_reload():
    """Re-pull fixture JSON from S3 (or local file) without restarting the container."""
    global SCENARIOS
    try:
        SCENARIOS = load_fixtures(FIXTURES_PATH)
        source = "s3" if (os.environ.get("FIXTURES_S3_BUCKET") and os.environ.get("FIXTURES_S3_KEY")) else "local"
        return jsonify({"status": "ok", "source": source, "scenarios": sorted(SCENARIOS.keys())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
