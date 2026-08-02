"""
Mock Dynatrace API server — DYNATRACE ONLY, LOGS ONLY.

This codebase has no knowledge of Grafana, Arize, Splunk, or Elasticsearch
(no shared app.py, no shared fixtures). It exists as its own independent
service so PagerDuty's Dynatrace connector always talks to a host that only
ever answers Dynatrace-shaped requests.

*** CRITICAL UNCONFIRMED QUESTION — READ BEFORE TRUSTING THIS MOCK ***
PagerDuty's "Add Dynatrace Connector" form takes an Environment URL (free
text) plus OAuth2 Client ID/Secret — no separate "token endpoint" field.
This mock ASSUMES PagerDuty derives the OAuth token endpoint from that same
Environment URL (i.e. calls {environment-url}/sso/oauth2/token on YOUR
ngrok host). That assumption is UNVERIFIED. If PagerDuty's backend instead
always calls Dynatrace's real global SSO host (sso.dynatrace.com)
regardless of what Environment URL you enter, this integration cannot be
mocked at all — same dead end as Datadog and Confluence earlier in this
project. The only way to know for sure: set Environment URL to this mock's
URL, fill in dummy Client ID/Secret, save the connector, and watch the
request logs to see where the token request actually goes.

Implements the real Dynatrace Grail API surface behind PagerDuty's
"Dynatrace: Search Logs" action:
  POST /sso/oauth2/token                          - OAuth2 client_credentials
                                                     token exchange
  POST /platform/storage/query/v1/query:execute    - DQL query ("fetch logs")
  GET  /platform/storage/query/v1/query:poll       - async poll for
                                                     long-running queries

Auth for the query endpoints: `Authorization: Bearer <access_token>` using
whatever token this mock's own /sso/oauth2/token endpoint issued.

Run:
    pip install -r requirements.txt
    MOCK_CLIENT_ID=demo-client MOCK_CLIENT_SECRET=demo-secret python app.py
    # defaults to port 3004

PagerDuty "Add Dynatrace Connector":
    Environment Type:    SaaS
    Environment URL:     https://dynatrace.holodeck.scsandbox.net
    OAuth2 Client ID:     demo-client
    OAuth2 Client Secret: demo-secret
    OAuth2 Account UUID:  (leave blank — optional per the form)
"""

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "demo-client")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "demo-secret")
PORT = int(os.environ.get("PORT", "3004"))
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "scenarios.json"

# Set SIMULATE_ASYNC=true to force one RUNNING poll cycle before SUCCEEDED,
# exercising the SRE Agent's polling path instead of always returning inline.
SIMULATE_ASYNC = os.environ.get("SIMULATE_ASYNC", "false").lower() == "true"


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

# In-memory store of pending async queries: request_token -> result payload.
PENDING_QUERIES: dict = {}


def check_bearer():
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return jsonify({"message": "Missing or malformed Authorization header"}), 401
    return None


def body_match_text(raw_body: str) -> str:
    return (raw_body or "").lower()


def pick_scenario(text: str):
    q = (text or "").lower()
    for key, scenario in SCENARIOS.items():
        if key == "default":
            continue
        candidates = [key, scenario.get("labels", {}).get("service", "")]
        if any(c and c.lower() in q for c in candidates):
            return key, scenario
    return "default", SCENARIOS["default"]


def extract_reference_time(raw_body: str) -> float:
    """
    Best-effort: DQL queries embed time bounds inside the query string
    itself (e.g. a `timeframe` clause or absolute timestamps), not in a
    separate structured field. Scan for the latest ISO8601 timestamp or
    plausible epoch number in the raw body; fall back to wall-clock time.
    """
    iso_matches = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw_body or "")
    parsed = []
    for m in iso_matches:
        try:
            parsed.append(datetime.fromisoformat(m).timestamp())
        except ValueError:
            continue
    if parsed:
        return max(parsed)

    epoch_matches = re.findall(r"\b(\d{10,13})\b", raw_body or "")
    epoch_values = [float(m) / 1000.0 if float(m) > 1e12 else float(m) for m in epoch_matches]
    if epoch_values:
        return max(epoch_values)

    return time.time()


def build_result(scenario: dict, reference_time: float) -> dict:
    records = []
    for entry in scenario.get("logs", []):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time + entry["offset_seconds"]))
        records.append({
            "timestamp": ts,
            "content": entry["line"],
            "loglevel": entry.get("level", "info").upper(),
        })

    return {
        "records": records,
        "types": [
            {
                "mappings": {
                    "timestamp": {"type": "timestamp"},
                    "content": {"type": "string"},
                    "loglevel": {"type": "string"},
                },
                "indexRange": [0, len(records)],
            }
        ],
        "metadata": {
            "grail": {
                "analysisTimeframe": {
                    "start": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time - 900)),
                    "end": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(reference_time)),
                }
            }
        },
    }


@app.route("/sso/oauth2/token", methods=["POST"])
def oauth_token():
    grant_type = request.form.get("grant_type")
    client_id = request.form.get("client_id")
    client_secret = request.form.get("client_secret")
    scope = request.form.get("scope")

    if grant_type != "client_credentials":
        return jsonify({"error": "unsupported_grant_type"}), 400
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        return jsonify({"error": "invalid_client"}), 401

    return jsonify({
        "access_token": f"mock-access-token-{uuid.uuid4().hex[:16]}",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope or "storage:logs:read storage:buckets:read",
    })


@app.route("/platform/storage/query/v1/query:execute", methods=["POST"])
def query_execute():
    err = check_bearer()
    if err:
        return err

    raw_body = request.get_data(as_text=True) or ""

    _, scenario = pick_scenario(body_match_text(raw_body))
    reference_time = extract_reference_time(raw_body)
    result = build_result(scenario, reference_time)

    if SIMULATE_ASYNC:
        request_token = f"req-{uuid.uuid4().hex[:16]}"
        PENDING_QUERIES[request_token] = {"polls_left": 1, "result": result}
        return jsonify({"requestToken": request_token, "state": "RUNNING"}), 202

    return jsonify({
        "requestToken": f"req-{uuid.uuid4().hex[:16]}",
        "state": "SUCCEEDED",
        "result": result,
    })


@app.route("/platform/storage/query/v1/query:poll", methods=["GET"])
def query_poll():
    err = check_bearer()
    if err:
        return err

    request_token = request.args.get("request-token")
    pending = PENDING_QUERIES.get(request_token)
    if pending is None:
        return jsonify({"message": "Unknown or expired request token"}), 404

    if pending["polls_left"] > 0:
        pending["polls_left"] -= 1
        return jsonify({"requestToken": request_token, "state": "RUNNING", "progress": 60, "ttlSeconds": 55})

    result = PENDING_QUERIES.pop(request_token)["result"]
    return jsonify({"requestToken": request_token, "state": "SUCCEEDED", "result": result})


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "mock dynatrace api running (dynatrace-only codebase)"})


# ── Managed/ActiveGate path-prefixed routes ──────────────────────────────────
# Dynatrace Managed constructs endpoints as {environment-url}/sso/oauth2/token
# and {environment-url}/platform/storage/query/v1/... where the environment
# URL you enter in PD is something like https://yourdomain.com/e/{env-id}.
# These delegates catch that pattern so both SaaS and Managed formats work.

@app.route("/e/<path:env_id>/sso/oauth2/token", methods=["POST"])
def oauth_token_managed(env_id):
    return oauth_token()


@app.route("/e/<path:env_id>/platform/storage/query/v1/query:execute", methods=["POST"])
def query_execute_managed(env_id):
    return query_execute()


@app.route("/e/<path:env_id>/platform/storage/query/v1/query:poll", methods=["GET"])
def query_poll_managed(env_id):
    return query_poll()


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
