"""
Mock ServiceNow API server — SERVICENOW ONLY, KNOWLEDGE ONLY.

This codebase has no knowledge of Grafana, Arize, Splunk, or Elasticsearch
(no shared app.py, no shared fixtures). It exists as its own independent
service so PagerDuty's ServiceNow OAuth connector always talks to a host
that only ever answers ServiceNow-shaped requests.

*** CONFIRMED WORKING WITH PAGERDUTY ***
Unlike the Dynatrace attempt, PagerDuty genuinely calls this mock. Observed
live (2026-08-04) with `User-Agent: PagerDuty-Workflow-Automation`:

  POST /oauth_token.do                              (from 44.242.69.192)
  GET  /api/now/table/sn_km_mr_st_kb_knowledge?...  (from 54.213.187.133)

ServiceNow's OAuth token endpoint is always per-instance, so there is no
shared global host for PagerDuty to shortcut to, the way sso.dynatrace.com is
shared across every Dynatrace tenant. That is why this one works.

*** PAGERDUTY USES TWO DIFFERENT TABLES ***
Confirmed from captured traffic:

  SEARCH        GET /api/now/table/sn_km_mr_st_kb_knowledge?sysparm_query=...
  FETCH ONE     GET /api/now/table/kb_knowledge/<sys_id>

Both are required. Searching hits the Knowledge Management search table, but
fetching a single article by sys_id uses the GENERIC kb_knowledge table. Do
not delete either one — dropping `kb_knowledge` as "unused" would break the
direct-fetch path that the SRE Agent relies on after it picks an article.

The search table's query shape:

  GET /api/now/table/sn_km_mr_st_kb_knowledge
        ?sysparm_fields=number,short_description,content,sys_updated_on,
                        author,embedded_media,sys_id
        &sysparm_display_value=true
        &sysparm_limit=10
        &sysparm_query=number=<free text search terms>

Two things learned the hard way from a 404 in the SRE Agent:
  1. Search uses `sn_km_mr_st_kb_knowledge`, NOT the generic `kb_knowledge`
     table (which is used for fetch-by-sys_id) and not the dedicated
     `/sn_km_api/knowledge/articles` endpoint (never observed in use, kept
     only as a low-cost alias).
  2. PagerDuty packs free-text search terms into `sysparm_query=number=...`,
     which is not a real equality match on the number field. So matching
     scores how many query terms appear in each article rather than comparing
     fields. An explicit `KB…` number in the query still wins outright.

Field names matter: PagerDuty asks for `content` (not `text`), plus `author`,
`sys_updated_on` and `embedded_media`. All are returned, and `sysparm_fields`
is honored so the response contains exactly the requested keys.

Search never returns an empty list. An empty result reads to the SRE Agent as
"no runbook exists", which is worse for a demo than the general handbook.

*** KEEP ANGLE BRACKETS ESCAPED IN FIXTURE CONTENT ***
Real ServiceNow KB `content` is HTML, and PagerDuty parses it as such. A
runbook containing a literal placeholder like `<pod>` is read as an unknown
HTML tag: the article fetch failed in the SRE Agent ("fetch failed") for the
ONLY article that had raw angle brackets, while every other article worked and
our own logs showed a clean 200 for it. Write `&lt;pod&gt;` in fixtures
instead. `scripts/verify-servicenow.sh` lints for this.

Endpoints:
  POST /oauth_token.do                                 OAuth2 password grant
  GET  /api/now/table/sn_km_mr_st_kb_knowledge[/<id>]  CONFIRMED — PD searches here
  GET  /api/now/table/kb_knowledge[/<id>]              CONFIRMED — PD fetches
                                                       single articles here
  GET  /sn_km_api/knowledge/articles[/<id>]            alias, never observed
  POST /admin/reload                                   hot-reload from S3

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
import re
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "demo-client")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "demo-secret")
USERNAME = os.environ.get("MOCK_USERNAME", "demo-user")
PASSWORD = os.environ.get("MOCK_PASSWORD", "demo-pass")
PORT = int(os.environ.get("PORT", "3005"))
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "scenarios.json"

DEFAULT_AUTHOR = "SRE Platform Team"

# Most articles a search will return. Real ServiceNow would return many, but
# flooding the SRE Agent is actively harmful: it was observed receiving 7
# articles — with the correct runbook ranked FIRST — and still replying
# "returned 7 articles but none matched". The agent does not treat array order
# as relevance, so weak matches have to be pruned rather than merely ranked.
MAX_RESULTS = 3

# Fields always returned by a single-article fetch, even when the caller's
# sysparm_fields does not ask for them.
#
# Real ServiceNow honors sysparm_fields strictly, and so did we — which meant
# PagerDuty's fetch (sysparm_fields=kb_knowledge_base,workflow_state,
# sys_updated_on,sys_updated_by) got metadata and no article body, and the SRE
# Agent reported "the automated fetch failed" despite a clean HTTP 200. Being a
# useful mock beats being a byte-exact one: always include enough to identify
# and render the article. Extra JSON keys are harmless to any sane consumer.
CORE_FETCH_FIELDS = ("sys_id", "number", "short_description", "content")

# NOTE: display-value translation is deliberately NOT implemented right now.
#
# PagerDuty sends sysparm_display_value=true, and real ServiceNow would then
# return each field's LABEL rather than its internal value — workflow_state
# would come back "Published" rather than the stored "published". That was
# implemented and then reverted so the fix above (always returning the article
# body on a single-article fetch) could be tested in isolation. If the SRE
# Agent now retrieves runbooks successfully, the body was the whole problem and
# the casing is irrelevant. If it still fails, restore the mapping — see the
# revert commit for the exact code.

# Dropped when scoring: PagerDuty prefixes the search with "number=", and
# generic words match everything so they carry no signal.
STOPWORDS = {
    "number", "the", "and", "for", "with", "from", "that", "this", "any",
    "are", "was", "not", "but", "how", "what", "why", "does", "did", "has",
    "have", "you", "your", "please", "find", "search", "article",
}


def load_fixtures(local_path: Path, s3_key_env: str = "FIXTURES_S3_KEY") -> dict:
    """Load fixture JSON from S3 when FIXTURES_S3_BUCKET + s3_key_env are set,
    otherwise fall back to the local file.  boto3 is imported lazily so the
    service starts fine with no AWS credentials configured."""
    bucket = os.environ.get("FIXTURES_S3_BUCKET", "")
    key = os.environ.get(s3_key_env, "")
    if bucket and key:
        try:
            import boto3  # noqa: PLC0415
            s3 = boto3.client("s3")
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = json.loads(obj["Body"].read().decode("utf-8"))
            print(f"[holodeck] loaded fixtures from s3://{bucket}/{key}", flush=True)
            return data
        except Exception as exc:  # noqa: BLE001
            # Do NOT die here. On first deploy of a new mock the S3 object does
            # not exist yet, and raising turns that into a container crash-loop
            # (observed: NoSuchKey restarting every few seconds) instead of a
            # service that simply serves its baked-in fixtures. Fail loudly,
            # then carry on with the local copy.
            print(f"[holodeck] WARNING: could not load s3://{bucket}/{key} ({exc.__class__.__name__}: {exc})", flush=True)
            print(f"[holodeck] falling back to local fixtures at {local_path}", flush=True)
    with open(local_path) as f:
        data = json.load(f)
    print(f"[holodeck] loaded fixtures from {local_path}", flush=True)
    return data


def index_by_sys_id(scenarios: dict) -> dict:
    return {v["sys_id"]: v for v in scenarios.values()
            if isinstance(v, dict) and "sys_id" in v}


def index_by_number(scenarios: dict) -> dict:
    return {v["number"].upper(): v for v in scenarios.values()
            if isinstance(v, dict) and "number" in v}


app = Flask(__name__)
# Real ServiceNow returns fields in the order requested via sysparm_fields.
# Flask alphabetizes by default, so turn that off to mirror the request.
app.json.sort_keys = False

SCENARIOS = load_fixtures(FIXTURES_PATH)
ARTICLES_BY_SYS_ID = index_by_sys_id(SCENARIOS)
ARTICLES_BY_NUMBER = index_by_number(SCENARIOS)


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


# ── Article shaping ───────────────────────────────────────────────────────────

def kb_record(scenario: dict) -> dict:
    """Full record.

    PagerDuty requests two different field sets depending on the call:
      search    number, short_description, content, sys_updated_on, author,
                embedded_media, sys_id
      fetch one kb_knowledge_base, workflow_state, sys_updated_on,
                sys_updated_by
    Everything either set asks for is populated here. `text` is kept alongside
    `content` for the generic kb_knowledge alias.
    """
    body = scenario.get("text", "")
    author = scenario.get("author", DEFAULT_AUTHOR)
    return {
        "sys_id": scenario["sys_id"],
        "number": scenario["number"],
        "short_description": scenario["short_description"],
        "content": body,
        "text": body,
        "kb_category": scenario.get("kb_category", "General"),
        "kb_knowledge_base": scenario.get("kb_knowledge_base", "SRE Runbooks"),
        "workflow_state": scenario.get("workflow_state", "published"),
        "author": author,
        "sys_updated_by": scenario.get("sys_updated_by", author),
        "sys_updated_on": scenario.get(
            "sys_updated_on",
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 86400)),
        ),
        "embedded_media": scenario.get("embedded_media", []),
    }


def project_fields(record: dict, sysparm_fields: str, always=()) -> dict:
    """Honor ?sysparm_fields=a,b,c — return those keys, plus anything in
    `always`. Unknown requested fields come back empty rather than missing,
    which is what ServiceNow does and stops the caller tripping over absent
    keys."""
    if not sysparm_fields:
        return record
    wanted = [f.strip() for f in sysparm_fields.split(",") if f.strip()]
    if not wanted:
        return record
    out = {f: record.get(f, "") for f in wanted}
    for f in always:
        if f not in out:
            out[f] = record.get(f, "")
    return out


def km_api_article(scenario: dict) -> dict:
    """Shape for the dedicated sn_km_api alias. Still a best guess —
    PagerDuty has not been observed calling this endpoint."""
    body = scenario.get("text", "")
    return {
        "id": scenario["sys_id"],
        "number": scenario["number"],
        "title": scenario["short_description"],
        "snippet": body[:200],
        "content": body,
        "fields": {
            "category": {"display_value": scenario.get("kb_category", "General")},
            "workflow_state": {"display_value": scenario.get("workflow_state", "published")},
        },
    }


# ── Search ────────────────────────────────────────────────────────────────────

def tokenize(text: str):
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(t) > 2 and t not in STOPWORDS]


def score_scenario(key: str, scenario: dict, tokens) -> int:
    """Weighted term overlap: the scenario key and title are stronger signals
    than the article body."""
    key_blob = key.lower()
    title_blob = scenario.get("short_description", "").lower()
    body_blob = scenario.get("text", "").lower()
    score = 0
    for t in tokens:
        if t in key_blob:
            score += 3
        elif t in title_blob:
            score += 2
        elif t in body_blob:
            score += 1
    return score


def search_articles(raw_query: str, limit: int = 10):
    """Rank articles for a PagerDuty-style query.

    PagerDuty sends `sysparm_query=number=<free text>`, so strip any leading
    `field=` operator and treat the remainder as search terms.
    """
    q = raw_query or ""
    q = re.sub(r"^\s*[a-z_]+(=|!=|LIKE|STARTSWITH|CONTAINS)", " ", q, flags=re.IGNORECASE)

    # An explicit KB number wins outright.
    for m in re.findall(r"KB\d+", q, flags=re.IGNORECASE):
        hit = ARTICLES_BY_NUMBER.get(m.upper())
        if hit:
            return [hit]

    tokens = tokenize(q)
    scored = []
    for key, scenario in SCENARIOS.items():
        if not isinstance(scenario, dict):
            continue
        scored.append((score_scenario(key, scenario, tokens), key, scenario))

    matched = sorted([s for s in scored if s[0] > 0], key=lambda s: -s[0])
    if matched:
        # Drop weak matches. Sharing one common token ("api") is not a match,
        # and including such articles buries the real answer.
        top = matched[0][0]
        cutoff = max(top * 0.5, 2)
        ordered = [s[2] for s in matched if s[0] >= cutoff][:MAX_RESULTS]
    else:
        # Nothing matched: return only the general handbook. That reads as
        # "no specific runbook exists, here is the fallback", which is a much
        # clearer signal than a long list of unrelated articles.
        ordered = [SCENARIOS["default"]] if "default" in SCENARIOS else []

    return ordered[: max(limit, 1)]


def parse_limit(default: int = 10) -> int:
    raw = request.args.get("sysparm_limit")
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


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


# ── Knowledge: Table API ──────────────────────────────────────────────────────
# sn_km_mr_st_kb_knowledge is the table PagerDuty actually queries (confirmed
# from live traffic). kb_knowledge is served identically as an alias.

def _table_search():
    err = check_bearer()
    if err:
        return err

    results = search_articles(request.args.get("sysparm_query", ""), parse_limit())
    fields = request.args.get("sysparm_fields", "")
    return jsonify({"result": [project_fields(kb_record(s), fields) for s in results]})


def _table_get_one(sys_id):
    err = check_bearer()
    if err:
        return err

    scenario = ARTICLES_BY_SYS_ID.get(sys_id) or ARTICLES_BY_NUMBER.get((sys_id or "").upper())
    if scenario is None:
        return jsonify({
            "error": {
                "message": "No Record found",
                "detail": f"Record with sys_id '{sys_id}' not found",
            }
        }), 404

    fields = request.args.get("sysparm_fields", "")
    record = project_fields(kb_record(scenario), fields, always=CORE_FETCH_FIELDS)
    return jsonify({"result": record})


app.add_url_rule("/api/now/table/sn_km_mr_st_kb_knowledge",
                 "snkm_search", _table_search, methods=["GET"])
app.add_url_rule("/api/now/table/sn_km_mr_st_kb_knowledge/<sys_id>",
                 "snkm_get_one", _table_get_one, methods=["GET"])
app.add_url_rule("/api/now/table/kb_knowledge",
                 "kb_search", _table_search, methods=["GET"])
app.add_url_rule("/api/now/table/kb_knowledge/<sys_id>",
                 "kb_get_one", _table_get_one, methods=["GET"])


# ── Knowledge: dedicated KM API (alias, unconfirmed shape) ────────────────────

@app.route("/sn_km_api/knowledge/articles", methods=["GET"])
def km_api_search():
    err = check_bearer()
    if err:
        return err

    query_text = (request.args.get("sysparm_search")
                  or request.args.get("query")
                  or request.args.get("sysparm_query")
                  or "")
    results = search_articles(query_text, parse_limit())
    return jsonify({
        "result": {
            "meta": {"count": len(results), "queryTime": 8},
            "articles": [km_api_article(s) for s in results],
        }
    })


@app.route("/sn_km_api/knowledge/articles/<sys_id>", methods=["GET"])
def km_api_get_one(sys_id):
    err = check_bearer()
    if err:
        return err

    scenario = ARTICLES_BY_SYS_ID.get(sys_id) or ARTICLES_BY_NUMBER.get((sys_id or "").upper())
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
    global SCENARIOS, ARTICLES_BY_SYS_ID, ARTICLES_BY_NUMBER
    try:
        SCENARIOS = load_fixtures(FIXTURES_PATH)
        ARTICLES_BY_SYS_ID = index_by_sys_id(SCENARIOS)
        ARTICLES_BY_NUMBER = index_by_number(SCENARIOS)
        source = "s3" if (os.environ.get("FIXTURES_S3_BUCKET") and os.environ.get("FIXTURES_S3_KEY")) else "local"
        return jsonify({"status": "ok", "source": source, "scenarios": sorted(SCENARIOS.keys())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
