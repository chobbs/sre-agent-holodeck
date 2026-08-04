"""
Mock ServiceNow API server — SERVICENOW ONLY, KNOWLEDGE ONLY.

This codebase has no knowledge of Grafana, Arize, Splunk, or Elasticsearch
(no shared app.py, no shared fixtures). It exists as its own independent
service so PagerDuty's ServiceNow OAuth connector always talks to a host
that only ever answers ServiceNow-shaped requests.

*** WHY THIS MAY SUCCEED WHERE DYNATRACE FAILED ***
ServiceNow's OAuth token endpoint is ALWAYS per-instance:
    https://<instance>.service-now.com/oauth_token.do
There is no shared global ServiceNow host the way sso.dynatrace.com is
shared across every Dynatrace tenant. Since the connector's URL field is
genuine free text and PagerDuty has no other way to know which ServiceNow
instance to authenticate against, the token endpoint is very likely derived
from that same URL field.

That is a STRUCTURAL reason for optimism, not a guarantee. It remains
UNVERIFIED until live traffic proves it. The Dynatrace attempt failed
precisely because PagerDuty never sent the token request to our host at
all — so verify that first, before trusting anything downstream:

    # watch for an inbound POST /oauth_token.do while saving the connector
    docker logs -f --since 1m holodeck-servicenow

If no request arrives, this joins Dynatrace as a confirmed dead end and
nothing downstream matters.

Endpoints:
  POST /oauth_token.do                     - OAuth2 PASSWORD grant.
                                             CONFIRMED shape from
                                             ServiceNow's public docs.
                                             Note: password grant, NOT
                                             client_credentials like
                                             Dynatrace used.
  GET  /api/now/table/kb_knowledge         - CONFIRMED Table API shape.
                                             Generic table search, real and
                                             well documented.
  GET  /api/now/table/kb_knowledge/<id>    - CONFIRMED. One article by sys_id.
  GET  /sn_km_api/knowledge/articles       - BEST GUESS shape. This is
                                             ServiceNow's real dedicated
                                             Knowledge Management API
                                             (confirmed to exist), but its
                                             exact response JSON was not
                                             confirmed from available docs.
  GET  /sn_km_api/knowledge/articles/<id>  - BEST GUESS. One article by id.
  POST /admin/reload                       - Hot-reload fixtures from S3.

BOTH Knowledge endpoint families are implemented because it is unconfirmed
which one PagerDuty's "Knowledge" tool actually calls — the same category of
ambiguity as Arize's /v2/spaces vs /v2/projects earlier in this project.
Whichever one shows up in the logs is the real one; the other can be dropped.

Auth for the Knowledge endpoints: `Authorization: Bearer <token>`. This mock
does not validate the token cryptographically, it only checks that some
bearer token is present.

Run locally:
    ./holodeck-local.sh servicenow      # flask + ngrok on port 3005

PagerDuty "Add ServiceNow OAuth Connector":
    URL:                             https://servicenow.holodeck.scsandbox.net
    ServiceNow Username:             demo-user
    ServiceNow Password:             demo-pass
    ServiceNow OAuth Client ID:      demo-client
    ServiceNow OAuth Client Secret:  demo-secret
"""

import json
import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "demo-client")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "demo-secret")
USERNAME = os.environ.get("MOCK_USERNAME", "demo-user")
PASSWORD = os.environ.get("MOCK_PASSWORD", "demo-pass")
PORT = int(os.environ.get("PORT", "3005"))
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


def index_by_sys_id(scenarios: dict) -> dict:
    """sys_id -> scenario, for the fetch-one-article endpoints."""
    return {v["sys_id"]: v for v in scenarios.values() if isinstance(v, dict) and "sys_id" in v}


app = Flask(__name__)

SCENARIOS = load_fixtures(FIXTURES_PATH)
ARTICLES_BY_SYS_ID = index_by_sys_id(SCENARIOS)


def check_bearer():
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return jsonify({
            "error": {
                "message": "User Not Authenticated",
                "detail": "Required to provide Auth information",
            }
        }), 401
    return None


def pick_scenario(text: str):
    """Substring-match the request against scenario keys, same convention as
    the other mocks: the SRE Agent embeds incident custom_details (service
    name, host) into its queries, so the service name lands in the query."""
    q = (text or "").lower()
    for key, scenario in SCENARIOS.items():
        if key == "default":
            continue
        candidates = [key, scenario.get("short_description", "")]
        if any(c and c.lower() in q for c in candidates):
            return key, scenario
    return "default", SCENARIOS["default"]


def table_api_record(scenario: dict) -> dict:
    """One record shaped like the real /api/now/table/kb_knowledge response."""
    return {
        "sys_id": scenario["sys_id"],
        "number": scenario["number"],
        "short_description": scenario["short_description"],
        "text": scenario["text"],
        "kb_category": scenario["kb_category"],
        "workflow_state": scenario["workflow_state"],
    }


def km_api_article(scenario: dict) -> dict:
    """Best-guess shape for the dedicated sn_km_api/knowledge/articles endpoint."""
    return {
        "id": scenario["sys_id"],
        "number": scenario["number"],
        "title": scenario["short_description"],
        "snippet": scenario["text"][:200],
        "content": scenario["text"],
        "fields": {
            "category": {"display_value": scenario["kb_category"]},
            "workflow_state": {"display_value": scenario["workflow_state"]},
        },
    }


# ── OAuth ─────────────────────────────────────────────────────────────────────

@app.route("/oauth_token.do", methods=["POST"])
def oauth_token():
    grant_type = request.form.get("grant_type")
    client_id = request.form.get("client_id")
    client_secret = request.form.get("client_secret")
    username = request.form.get("username")
    password = request.form.get("password")
    scope = request.form.get("scope")

    if grant_type != "password":
        return jsonify({
            "error": "unsupported_grant_type",
            "error_description": "grant type is not supported",
        }), 400
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        return jsonify({
            "error": "invalid_client",
            "error_description": "client credentials are invalid",
        }), 401
    if username != USERNAME or password != PASSWORD:
        return jsonify({
            "error": "invalid_grant",
            "error_description": "username/password combination invalid",
        }), 401

    return jsonify({
        "access_token": f"mock-access-token-{uuid.uuid4().hex[:20]}",
        "refresh_token": f"mock-refresh-token-{uuid.uuid4().hex[:20]}",
        "scope": scope or "useraccount",
        "token_type": "Bearer",
        "expires_in": 3000,
    })


# ── Knowledge: Table API (confirmed shape) ────────────────────────────────────

@app.route("/api/now/table/kb_knowledge", methods=["GET"])
def table_api_search():
    err = check_bearer()
    if err:
        return err

    query_text = request.args.get("sysparm_query", "")
    limit = request.args.get("sysparm_limit")

    _, scenario = pick_scenario(query_text)
    records = [table_api_record(scenario)]
    if limit:
        try:
            records = records[: int(limit)]
        except ValueError:
            pass

    return jsonify({"result": records})


@app.route("/api/now/table/kb_knowledge/<sys_id>", methods=["GET"])
def table_api_get_one(sys_id):
    err = check_bearer()
    if err:
        return err

    scenario = ARTICLES_BY_SYS_ID.get(sys_id)
    if scenario is None:
        return jsonify({
            "error": {
                "message": "No Record found",
                "detail": f"Record with sys_id '{sys_id}' not found",
            }
        }), 404

    return jsonify({"result": table_api_record(scenario)})


# ── Knowledge: dedicated KM API (best-guess shape) ────────────────────────────

@app.route("/sn_km_api/knowledge/articles", methods=["GET"])
def km_api_search():
    err = check_bearer()
    if err:
        return err

    query_text = request.args.get("sysparm_search") or request.args.get("query") or ""
    _, scenario = pick_scenario(query_text)

    return jsonify({
        "result": {
            "meta": {"count": 1, "queryTime": 8},
            "articles": [km_api_article(scenario)],
        }
    })


@app.route("/sn_km_api/knowledge/articles/<sys_id>", methods=["GET"])
def km_api_get_one(sys_id):
    err = check_bearer()
    if err:
        return err

    scenario = ARTICLES_BY_SYS_ID.get(sys_id)
    if scenario is None:
        return jsonify({"error": {"message": "Article not found"}}), 404

    return jsonify({"result": km_api_article(scenario)})


# ── Ops ───────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "mock servicenow api running (servicenow-only codebase)"})


@app.route("/admin/reload", methods=["POST"])
def admin_reload():
    """Re-pull fixture JSON from S3 (or local file) without restarting the container."""
    global SCENARIOS, ARTICLES_BY_SYS_ID
    try:
        SCENARIOS = load_fixtures(FIXTURES_PATH)
        ARTICLES_BY_SYS_ID = index_by_sys_id(SCENARIOS)
        source = "s3" if (os.environ.get("FIXTURES_S3_BUCKET") and os.environ.get("FIXTURES_S3_KEY")) else "local"
        return jsonify({"status": "ok", "source": source, "scenarios": sorted(SCENARIOS.keys())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
