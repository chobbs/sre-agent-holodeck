"""
Mock Splunk API server — SPLUNK ONLY, LOGS ONLY.

This codebase has no knowledge of Grafana or Arize whatsoever (no shared
app.py, no shared fixtures). It exists as its own independent service so
PagerDuty's Splunk connector always talks to a host that only ever answers
Splunk-shaped requests.

Implements Splunk's real REST search job flow (the actual API the
"Splunk: Search Logs" PagerDuty action uses under the hood):
  GET  /services/server/info                        - credential/health check
  POST /services/search/jobs                         - create a search job (SPL)
  GET  /services/search/jobs/<sid>                   - poll job status
  GET  /services/search/jobs/<sid>/results            - fetch results (JSON)

This mock always completes jobs immediately (DONE on first poll) rather
than simulating Splunk's real async dispatch — fine for a scripted demo,
since the real behavior the PagerDuty action cares about is "eventually
DONE", not the exact timing.

Auth: Splunk's real scheme is `Authorization: Splunk <token>` (confirmed
directly from PagerDuty's own troubleshooting docs, which give this as the
credential-check curl example) — NOT Bearer, NOT X-Api-Key. Set the
expected token via MOCK_API_TOKEN.

Stateless job design: rather than keeping an in-memory job store (which
would break across app restarts and add complexity), the search job's
scenario match and reference time are encoded directly into the `sid`
returned at job-creation time, then decoded back out on poll/results calls.

Run:
    pip install -r requirements.txt
    MOCK_API_TOKEN=demo-token python app.py     # defaults to port 3002

Then:
    ngrok http 3002

PagerDuty "Add Splunk Connector":
    Splunk URL:            your ngrok https URL
    Authentication Token:  demo-token
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
PORT = int(os.environ.get("PORT", "3002"))
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
    Splunk's real auth scheme: `Authorization: Splunk <token>`. Confirmed
    directly from PagerDuty's own docs (the credential-check curl example
    under Troubleshooting uses exactly this header format).
    """
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Splunk "):
        return jsonify({"message": "Missing or malformed Authorization header (expected 'Splunk <token>')"}), 401
    token = authorization.removeprefix("Splunk ").strip()
    if token != APP_TOKEN:
        return jsonify({"message": "Invalid token"}), 401
    return None


def pick_scenario(text: str):
    """
    Live traffic showed the SRE Agent builds its own structured query from
    the incident's custom_details fields (e.g.
    `host:fw-edge-02 destination_ip:... sourcetype:pan:traffic`), rather
    than passing through any literal scenario-name text. Matching only
    against the scenario's dict key (e.g. "firewall-policy-violation")
    never hit, since that string never appears in the agent's actual query
    — only its host and sourcetype do. Match against all three now.
    """
    q = (text or "").lower()
    for key, scenario in SCENARIOS.items():
        if key == "default":
            continue
        candidates = [key, scenario.get("host", ""), scenario.get("sourcetype", "")]
        if any(candidate and candidate.lower() in q for candidate in candidates):
            return key, scenario
    return "default", SCENARIOS["default"]


def extract_reference_time_from_spl(spl: str) -> float:
    """
    Best-effort: PagerDuty's Splunk action doesn't inject a time window —
    callers are expected to embed time bounds directly in the SPL (e.g.
    `earliest=-1h` or absolute epoch seconds like `earliest=1752459600`).
    Look for an absolute `latest=<epoch>` first (closest to "now" for the
    query), falling back to wall-clock time if nothing absolute is found —
    relative modifiers like `-1h` or `-24h@h` aren't resolved here since
    they don't carry an absolute anchor on their own.
    """
    match = re.search(r"latest=(\d{9,13})", spl)
    if not match:
        match = re.search(r"earliest=(\d{9,13})", spl)
    if match:
        value = float(match.group(1))
        return value / 1000.0 if value > 1e12 else value
    return time.time()


def make_sid(scenario_key: str, reference_time: float) -> str:
    return f"mocksid__{scenario_key}__{int(reference_time)}__{uuid.uuid4().hex[:8]}"


def parse_sid(sid: str):
    """Returns (scenario_key, reference_time) decoded from a sid, or (None, None) if unrecognized."""
    parts = sid.split("__")
    if len(parts) != 4 or parts[0] != "mocksid":
        return None, None
    scenario_key = parts[1]
    try:
        reference_time = float(parts[2])
    except ValueError:
        return None, None
    return scenario_key, reference_time


def build_results(scenario: dict, reference_time: float) -> list:
    host = scenario.get("host", "unknown-host")
    sourcetype = scenario.get("sourcetype", "generic_log")
    results = []
    for entry in scenario.get("logs", []):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000%z", time.localtime(reference_time + entry["offset_seconds"]))
        results.append({
            "_time": ts,
            "_raw": entry["line"],
            "host": host,
            "source": "mock_source",
            "sourcetype": sourcetype,
            "level": entry.get("level", "info"),
        })
    return results


@app.route("/services/server/info", methods=["GET"])
def server_info():
    err = check_auth()
    if err:
        return err
    return jsonify({
        "entry": [
            {
                "content": {
                    "version": "9.9.9-mock",
                    "serverName": "mock-splunk",
                    "build": "mock",
                }
            }
        ]
    })


@app.route("/services/search/jobs", methods=["POST"])
def create_job():
    err = check_auth()
    if err:
        return err

    # Splunk's real job-creation endpoint takes form-encoded data, with the
    # SPL in a `search` field. Accept form data, but fall back to JSON body
    # in case the caller sends it that way instead.
    spl = request.form.get("search")
    if spl is None:
        body = request.get_json(force=True, silent=True) or {}
        spl = body.get("search", "")

    _, scenario_match = pick_scenario(spl)
    scenario_key = next((k for k, v in SCENARIOS.items() if v is scenario_match), "default")
    reference_time = extract_reference_time_from_spl(spl or "")
    sid = make_sid(scenario_key, reference_time)

    return jsonify({"sid": sid})


@app.route("/services/search/jobs/<sid>", methods=["GET"])
def job_status(sid):
    err = check_auth()
    if err:
        return err

    scenario_key, reference_time = parse_sid(sid)
    if scenario_key is None:
        return jsonify({"messages": [{"type": "FATAL", "text": "Unknown search job ID"}]}), 404

    scenario = SCENARIOS.get(scenario_key, SCENARIOS["default"])
    result_count = len(scenario.get("logs", []))

    # Always DONE immediately — see module docstring.
    return jsonify({
        "entry": [
            {
                "content": {
                    "dispatchState": "DONE",
                    "isDone": True,
                    "doneProgress": 1.0,
                    "resultCount": result_count,
                }
            }
        ]
    })


@app.route("/services/search/jobs/<sid>/results", methods=["GET"])
def job_results(sid):
    err = check_auth()
    if err:
        return err

    scenario_key, reference_time = parse_sid(sid)
    if scenario_key is None:
        return jsonify({"messages": [{"type": "FATAL", "text": "Unknown search job ID"}]}), 404

    scenario = SCENARIOS.get(scenario_key, SCENARIOS["default"])
    results = build_results(scenario, reference_time)

    # Max Results is enforced by the PagerDuty action itself per its docs,
    # but honor a `count` query param here too in case it's passed through.
    count = request.args.get("count")
    if count:
        try:
            results = results[: int(count)]
        except ValueError:
            pass

    return jsonify({"results": results})


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "mock splunk api running (splunk-only codebase)"})


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