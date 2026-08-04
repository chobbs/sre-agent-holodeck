#!/usr/bin/env bash
# verify-servicenow.sh
#
# Exercises the ServiceNow mock exactly the way PagerDuty's SRE Agent does, so
# you can confirm article retrieval works WITHOUT going through PagerDuty.
#
# For each scenario it performs the full round trip:
#   1. POST /oauth_token.do                              (password grant)
#   2. GET  /api/now/table/sn_km_mr_st_kb_knowledge?...   (PD's real query shape)
#      -> asserts the expected KB article is the TOP hit
#      -> reports how many articles came back (should be few; a long list is
#         what made the agent say "returned 7 articles but none matched")
#   3. GET  /api/now/table/sn_km_mr_st_kb_knowledge/<KB>  (direct fetch)
#      -> asserts 200 and non-empty content
#
# Expected article numbers are read from the fixtures, so this cannot drift out
# of sync with the data.
#
# Usage:
#   ./scripts/verify-servicenow.sh                          # all scenarios, localhost
#   ./scripts/verify-servicenow.sh service-mesh-mtls         # one scenario
#   ./scripts/verify-servicenow.sh all https://xxx.ngrok-free.dev
#   ./scripts/verify-servicenow.sh all https://servicenow.holodeck.scsandbox.net
#
# Exits non-zero if any check fails.

set -uo pipefail

SCENARIO="${1:-all}"
BASE="${2:-http://localhost:3005}"
BASE="${BASE%/}"

FIXTURES="$(cd "$(dirname "$0")/.." && pwd)/servicenow-mock/fixtures/scenarios.json"
if [ ! -f "$FIXTURES" ]; then
  echo "Fixtures not found: $FIXTURES"
  exit 1
fi

CLIENT_ID="${MOCK_CLIENT_ID:-demo-client}"
CLIENT_SECRET="${MOCK_CLIENT_SECRET:-demo-secret}"
SNOW_USER="${MOCK_USERNAME:-demo-user}"
SNOW_PASS="${MOCK_PASSWORD:-demo-pass}"

# PagerDuty's real field list, verbatim from captured traffic.
PD_FIELDS="number,short_description,content,sys_updated_on,author,embedded_media,sys_id"

echo "Target: $BASE"
echo ""

# ── 1. token ──────────────────────────────────────────────────────────────────
echo "OAuth password grant"
TOKEN=$(curl -s -m 20 -X POST "$BASE/oauth_token.do" \
  -d grant_type=password -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET" \
  -d username="$SNOW_USER" -d password="$SNOW_PASS" \
  | python3 -c "import sys,json
try:
    print(json.load(sys.stdin).get('access_token',''))
except Exception:
    print('')")

if [ -z "$TOKEN" ]; then
  echo "  FAIL — no access_token returned. Is the mock running and reachable?"
  echo "         try: ./holodeck-local.sh servicenow"
  exit 1
fi
echo "  ok — token ${TOKEN:0:26}..."
echo ""

# ── 2. build the scenario list from the fixtures ──────────────────────────────
# Emits: key<TAB>KBnumber<TAB>search terms
PLAN=$(python3 - "$FIXTURES" "$SCENARIO" <<'PY'
import json, sys, re
fixtures, only = sys.argv[1], sys.argv[2]
data = json.load(open(fixtures))
for key, art in data.items():
    if key == "default":
        continue
    if only not in ("all", "", key):
        continue
    # Search terms an agent would plausibly use: the service name plus the
    # meaningful words from the article title.
    title = re.sub(r"^Runbook:\s*", "", art.get("short_description", ""))
    words = [w for w in re.split(r"[^A-Za-z0-9]+", title) if len(w) > 3][:5]
    print("%s\t%s\t%s" % (key, art["number"], " ".join([key] + words)))
PY
)

if [ -z "$PLAN" ]; then
  echo "No scenarios matched '$SCENARIO'. Available:"
  python3 -c "
import json,sys
d=json.load(open('$FIXTURES'))
print('  ' + ', '.join(k for k in d if k != 'default'))
"
  exit 1
fi

# ── 3. search + fetch per scenario ───────────────────────────────────────────
PASS=0
FAIL=0

while IFS=$'\t' read -r key kb terms; do
  [ -z "$key" ] && continue
  echo "── $key (expect $kb)"

  # URL-encode the search terms.
  q=$(python3 -c "
import urllib.parse, sys
print(urllib.parse.quote(sys.argv[1]))
" "$terms")

  body=$(curl -s -m 20 -H "Authorization: Bearer $TOKEN" -H "Accept: application/json" \
    "$BASE/api/now/table/sn_km_mr_st_kb_knowledge?sysparm_fields=${PD_FIELDS}&sysparm_display_value=true&sysparm_limit=10&sysparm_query=number=${q}")

  result=$(printf '%s' "$body" | python3 -c "
import sys, json
want = sys.argv[1]
try:
    res = json.load(sys.stdin).get('result', [])
except Exception as e:
    print('ERR|0|could not parse response: %s' % e); raise SystemExit
if not res:
    print('ERR|0|empty result list'); raise SystemExit
top = res[0]
status = 'OK' if top.get('number') == want else 'ERR'
print('%s|%d|%s|%s' % (status, len(res), top.get('number',''), (top.get('short_description') or '')[:52]))
" "$kb")

  st=$(printf '%s' "$result" | cut -d'|' -f1)
  n=$(printf '%s' "$result" | cut -d'|' -f2)
  got=$(printf '%s' "$result" | cut -d'|' -f3)
  desc=$(printf '%s' "$result" | cut -d'|' -f4)

  if [ "$st" = "OK" ]; then
    echo "   search: top hit $got ($n returned) — $desc"
    PASS=$((PASS+1))
  else
    echo "   search: FAIL — expected $kb, got '${got:-none}' ($n returned)"
    FAIL=$((FAIL+1))
  fi

  # Direct fetch by KB number — this is what custom_details.runbook_url
  # points at, so it must work on its own.
  fetch=$(curl -s -m 20 -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/now/table/sn_km_mr_st_kb_knowledge/${kb}?sysparm_fields=${PD_FIELDS}" \
    -w '\n%{http_code}')
  code=$(printf '%s' "$fetch" | tail -1)
  fbody=$(printf '%s' "$fetch" | sed '$d')

  if [ "$code" = "200" ]; then
    excerpt=$(printf '%s' "$fbody" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin).get('result', {})
    c = (r.get('content') or '').strip().replace('\n',' ')
    print(('%s | %s' % (r.get('author',''), c[:64])) if c else 'EMPTY CONTENT')
except Exception:
    print('unparseable')")
    if [ "$excerpt" = "EMPTY CONTENT" ]; then
      echo "   fetch:  FAIL — 200 but content empty"
      FAIL=$((FAIL+1))
    else
      echo "   fetch:  $kb 200 — $excerpt..."
      PASS=$((PASS+1))
    fi
  else
    echo "   fetch:  FAIL — HTTP $code for $kb"
    FAIL=$((FAIL+1))
  fi
  echo ""
done <<< "$PLAN"

echo "──────────────────────────────"
echo "passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo ""
echo "All good. To drive the same path through PagerDuty:"
echo "  ./scripts/trigger-test-incident.sh ${SCENARIO/all/service-mesh-mtls} $BASE"
