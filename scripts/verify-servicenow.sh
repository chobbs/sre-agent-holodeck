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
#   3. GET  /api/now/table/sn_km_mr_st_kb_knowledge/<KB>  (fetch by number)
#      -> asserts 200 and non-empty content
#   4. GET  /api/now/table/kb_knowledge/<sys_id>          (PD's REAL fetch shape,
#                                                          different field set)
#      -> asserts the fields PD asks for come back populated
#
# It also lints the fixtures for raw angle brackets. ServiceNow KB content is
# HTML and PagerDuty parses it as such, so a literal `<pod>` placeholder reads
# as an unknown tag. Write `&lt;pod&gt;` instead. (This was once thought to cause
# the agent's "fetch failed" reports; it did not — that was a runbook_url
# missing sys_id. The lint is kept because escaping is correct regardless.)
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

# PagerDuty's real field lists, verbatim from captured traffic. It uses a
# DIFFERENT set for search vs single-article fetch.
PD_FIELDS="number,short_description,content,sys_updated_on,author,embedded_media,sys_id"
PD_FETCH_FIELDS="kb_knowledge_base,workflow_state,sys_updated_on,sys_updated_by"

echo "Target: $BASE"
echo ""

# ── 0. lint fixture content for raw angle brackets ───────────────────────────
echo "Fixture HTML safety"
LINT=$(python3 - "$FIXTURES" <<'PY'
import json, re, sys
bad = []
for key, art in json.load(open(sys.argv[1])).items():
    hits = re.findall(r"<[^>]{1,24}>", art.get("text", ""))
    if hits:
        bad.append("%s: %s" % (key, ", ".join(sorted(set(hits)))))
print("\n".join(bad))
PY
)
if [ -n "$LINT" ]; then
  echo "  FAIL — raw angle brackets found (PagerDuty parses content as HTML):"
  printf '%s\n' "$LINT" | sed 's/^/    /'
  echo "  Escape them as &lt; and &gt; in the fixture."
  exit 1
fi
echo "  ok — no raw angle brackets in article content"
echo ""

# ── 1. token ─────────────────────────────────────────────────────────────────
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
    print("%s\t%s\t%s\t%s" % (key, art["number"], art["sys_id"], " ".join([key] + words)))
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

while IFS=$'\t' read -r key kb sysid terms; do
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

  # PagerDuty's actual single-article fetch: generic kb_knowledge table, by
  # sys_id, with a DIFFERENT field set. This is the call that silently returned
  # empty fields before kb_knowledge_base / sys_updated_by were populated.
  real=$(curl -s -m 20 -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/now/table/kb_knowledge/${sysid}?sysparm_fields=${PD_FETCH_FIELDS}&sysparm_display_value=true" \
    -w '\n%{http_code}')
  rcode=$(printf '%s' "$real" | tail -1)
  rbody=$(printf '%s' "$real" | sed '$d')

  if [ "$rcode" = "200" ]; then
    verdict=$(printf '%s' "$rbody" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin).get('result', {})
except Exception:
    print('ERR|unparseable'); raise SystemExit
blank = [k for k, v in r.items() if v in ('', None, [])]
if blank:
    print('ERR|empty fields: ' + ','.join(blank)); raise SystemExit
# The agent needs the article BODY from this call, not just metadata. Returning
# only the requested metadata fields is what produced 'the automated fetch
# failed' in the SRE Agent despite a clean 200.
if not (r.get('content') or '').strip():
    print('ERR|200 but no article content'); raise SystemExit
if not r.get('number'):
    print('ERR|200 but no article number'); raise SystemExit
# workflow_state casing is intentionally NOT asserted: display-value mapping is
# currently reverted so the content fix can be tested in isolation. The value is
# printed so a change in it is still visible.
print('OK|content %d chars, %s, state=%s' % (len(r['content']), r['number'], r.get('workflow_state')))")
    if [ "${verdict%%|*}" = "OK" ]; then
      echo "   pd-fetch: sys_id 200 — ${verdict#*|}"
      PASS=$((PASS+1))
    else
      echo "   pd-fetch: FAIL — ${verdict#*|}"
      FAIL=$((FAIL+1))
    fi
  else
    echo "   pd-fetch: FAIL — HTTP $rcode for sys_id $sysid"
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
