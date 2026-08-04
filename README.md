# Holodeck — SRE Connector Simulator

Mock API services for demoing PagerDuty's SRE Agent connectors without real observability infrastructure. All five mocks run simultaneously as independent HTTPS services on EC2, backed by scenario fixtures stored in S3.

## Live endpoints

| Service | URL | PagerDuty connector |
|---|---|---|
| Grafana | `https://grafana.holodeck.scsandbox.net` | Grafana |
| Arize | `https://arize.holodeck.scsandbox.net` | Arize |
| Splunk | `https://splunk.holodeck.scsandbox.net` | Splunk |
| Elasticsearch | `https://elasticsearch.holodeck.scsandbox.net` | Elasticsearch |
| ServiceNow | `https://servicenow.holodeck.scsandbox.net` | ServiceNow OAuth ¹ |

**Auth token for Grafana, Arize, Splunk, Elasticsearch:** `demo-token`

**ServiceNow auth:** OAuth2 *password* grant — username `demo-user`, password `demo-pass`, client ID `demo-client`, client secret `demo-secret`

¹ **Confirmed working with PagerDuty** (OAuth + Knowledge search both observed live). Requires a DNS A record for `servicenow.holodeck.scsandbox.net` before the EC2 endpoint works — see [ServiceNow](#servicenow--oauth-connector).

> Note: PagerDuty-managed laptops cannot reach these hostnames directly — corporate Cloudflare WARP blocks the domain at the device level and returns a `303` to `blocked.teams.cloudflare.com`. This affects your laptop only, not PagerDuty's cloud. To test locally, pin the IP: `curl --resolve grafana.holodeck.scsandbox.net:443:35.88.248.161 ...`, or run the request from the EC2 box.

## Architecture

```
<service>.holodeck.scsandbox.net  →  Elastic IP (35.88.248.161)
                                       ↓
                            EC2 / Amazon Linux 2023
                                       ↓
                            Caddy (port 443, auto-TLS)
              ┌────────────┬───────────┬──────────────┬─────────────┐
           grafana       arize       splunk    elasticsearch   servicenow
            :3000         :3001       :3002        :3003          :3005
                                       ↑
                          S3: sc-holodeck-demo
```

- **Caddy** handles TLS (Let's Encrypt, auto-renewing via TLS-ALPN-01 — port 80 not required)
- **Fixtures** are JSON files stored in S3, hot-reloadable without container restarts
- **Docker Compose** manages all containers with `restart: unless-stopped` (survives reboots)

## Connecting to PagerDuty

In PagerDuty → Integrations → connector setup, use:

| Connector | URL field | Token field | Token value |
|---|---|---|---|
| Grafana | `https://grafana.holodeck.scsandbox.net` | Service Account Token | `demo-token` |
| Arize | `https://arize.holodeck.scsandbox.net` | API Key | `demo-token` |
| Splunk | `https://splunk.holodeck.scsandbox.net` | Authentication Token | `demo-token` |
| Elasticsearch | `https://elasticsearch.holodeck.scsandbox.net` | API Key | `demo-token` |

ServiceNow takes four fields instead of a single token:

| Field | Value |
|---|---|
| URL | `https://servicenow.holodeck.scsandbox.net` |
| ServiceNow Username | `demo-user` |
| ServiceNow Password | `demo-pass` |
| ServiceNow OAuth Client ID | `demo-client` |
| ServiceNow OAuth Client Secret | `demo-secret` |

## Managing the stack (on EC2)

SSH in first:
```bash
ssh -i ~/.ssh/solutions-consulting.pem ec2-user@35.88.248.161
```

Then use `holodeck.sh`:
```bash
bash ~/holodeck/holodeck.sh status            # container health + endpoint URLs
bash ~/holodeck/holodeck.sh logs grafana      # tail logs for one service
bash ~/holodeck/holodeck.sh restart splunk    # restart one service
bash ~/holodeck/holodeck.sh restart           # restart everything
bash ~/holodeck/holodeck.sh reload-fixtures   # pull new JSON from S3, no restart
bash ~/holodeck/holodeck.sh sync-fixtures     # upload local JSON → S3 → reload
bash ~/holodeck/holodeck.sh up                # start the full stack
bash ~/holodeck/holodeck.sh down              # stop the full stack
bash ~/holodeck/holodeck.sh deploy            # git pull + rebuild changed containers
```

### Gotcha: Caddyfile changes need a container recreate
`Caddyfile` is mounted into the caddy container as a **single-file bind mount**, which binds to the file's inode. `git pull` writes a new file and renames it, so a running caddy container keeps serving the **old** config. Running `caddy reload` does not help — it validates and reloads the stale file and reports `Valid configuration`, which makes the failure silent.

`holodeck.sh deploy` now detects a changed `Caddyfile` and recreates caddy automatically. If you edit the file by hand on the box, do it yourself:
```bash
docker compose -f ~/holodeck/docker-compose.yml up -d --force-recreate caddy
```
Verify the container actually sees your version:
```bash
docker exec holodeck-caddy stat -c "%i %s" /etc/caddy/Caddyfile   # compare with host
docker exec holodeck-caddy curl -s localhost:2019/config/          # running config
```

## Updating scenario fixtures

Fixture JSON files live in each mock's `fixtures/` directory locally and are mirrored to S3.

**Workflow:**
1. Edit the relevant `fixtures/scenarios.json` (or `grafana-mock/fixtures/metric_scenarios.json`)
2. From EC2, upload and reload in one step:
   ```bash
   bash ~/holodeck/holodeck.sh sync-fixtures
   ```
3. Or upload from your Mac (requires AWS CLI + credentials):
   ```bash
   ./scripts/sync-fixtures-to-s3.sh sc-holodeck-demo
   ```
   Then reload the running containers:
   ```bash
   # On EC2:
   bash ~/holodeck/holodeck.sh reload-fixtures
   ```

**S3 key layout:**
```
s3://sc-holodeck-demo/
  grafana/scenarios.json
  grafana/metric_scenarios.json
  arize/scenarios.json
  splunk/scenarios.json
  elasticsearch/scenarios.json
  servicenow/scenarios.json
```

## Scenario matching

Each service matches incoming requests against scenario keys using substring matching on the full request body. To trigger a specific scenario, include its key in the request — the SRE Agent does this automatically by embedding incident `custom_details` fields (service name, host, etc.) in its queries.

**Available scenarios per service:**

| Grafana (logs) | Grafana (metrics) | Arize | Splunk | Elasticsearch | ServiceNow |
|---|---|---|---|---|---|
| checkout-api | checkout-api | checkout-agent | suspicious-login | payments-api-gateway | payments-api-gateway |
| payments-svc | payments-svc | fraud-detection-model | privilege-escalation | payments-orchestrator-sync | payments-orchestrator-sync |
| orders-db-proxy | orders-db-proxy | rag-retrieval-agent | data-exfiltration | idempotency-token-service | idempotency-token-service |
| auth-service | auth-service | hallucination | malware-detection | payments-rules-engine | service-mesh-mtls |
| search-api | search-api | support-chatbot | firewall-policy-violation | service-mesh-mtls | edge-waf-cdn |
| *(default)* | *(default)* | *(default)* | *(default)* | *(default)* | *(default)* |

ServiceNow returns **runbook KB articles** rather than logs or metrics — a different content type from the other four. The articles are written to pair with the existing Payments API scenarios, so the SRE Agent can surface a relevant runbook while investigating the same incident the other mocks are describing.

## Project structure

```
sre-conn-simulator/
├── holodeck.sh              # EC2 stack manager (docker compose wrapper)
├── holodeck-local.sh        # Local dev runner (Flask + ngrok, one service at a time)
├── docker-compose.yml       # All 5 mocks + Caddy
├── Caddyfile                # HTTPS reverse proxy config
├── requirements.txt         # Shared Python deps (Flask, boto3)
├── .env.example             # Environment variable template
├── grafana-mock/
│   ├── app.py               # Grafana API mock (Loki logs + Prometheus metrics)
│   ├── Dockerfile
│   └── fixtures/
│       ├── scenarios.json        # Log scenarios
│       └── metric_scenarios.json # Metric scenarios
├── arize-mock/
│   ├── app.py               # Arize API mock (spans + GraphQL monitors)
│   ├── Dockerfile
│   └── fixtures/scenarios.json
├── splunk-mock/
│   ├── app.py               # Splunk REST API mock (async search job flow)
│   ├── Dockerfile
│   └── fixtures/scenarios.json
├── elasticsearch-mock/
│   ├── app.py               # Elasticsearch API mock (Lucene/KQL/EQL search)
│   ├── Dockerfile
│   └── fixtures/scenarios.json
├── servicenow-mock/
│   ├── app.py               # ServiceNow mock (OAuth2 password grant + Knowledge)
│   ├── Dockerfile
│   └── fixtures/scenarios.json  # Runbook KB articles
└── scripts/
    ├── bootstrap-ec2.sh     # Fresh instance setup (Docker + Buildx + Compose)
    ├── sync-fixtures-to-s3.sh   # Upload all fixture JSON to S3
    ├── trigger-test-incident.sh # Fire a test incident at PagerDuty
    └── verify-servicenow.sh     # Round-trip check of the ServiceNow mock
```

## Firing a test incident

To give the SRE Agent something to investigate, send an event whose
`custom_details.service` is one of the shared scenario keys. Every mock keys off
that same name, so all of them describe the *same* incident.

```bash
# Set the routing key without putting it in shell history:
read -rs "PD_ROUTING_KEY?PagerDuty routing key: " && export PD_ROUTING_KEY   # zsh

./scripts/trigger-test-incident.sh payments-api-gateway
./scripts/trigger-test-incident.sh service-mesh-mtls https://<your-ngrok>.ngrok-free.dev
```

The optional second argument adds a `custom_details.runbook_url.servicenow`
hint, which is PagerDuty's documented way to point the agent at a specific
runbook when it cannot infer one from the payload.

Creating the incident does **not** by itself invoke the SRE Agent. Either open
the incident and use the **SRE Agent** tab, or configure an Incident Workflow
to engage it automatically. Also confirm under **AI Settings → SRE Agent
Configuration → Connectors** that ServiceNow is `Active` *and* the Knowledge
Base tool is checked — the connector existing is not the same as the tool being
enabled.

The script prints a resolve command so test incidents do not pile up.

## ServiceNow — OAuth connector

**This one works.** Unlike Dynatrace, PagerDuty genuinely calls this mock. Both halves were observed live on 2026-08-04 with `User-Agent: PagerDuty-Workflow-Automation`:

```
POST /oauth_token.do                              from 44.242.69.192
GET  /api/now/table/sn_km_mr_st_kb_knowledge?...  from 54.213.187.133
```

### Why this works where Dynatrace did not
The Dynatrace attempt failed because PagerDuty sends the OAuth token request to `sso.dynatrace.com` — a host shared by every Dynatrace SaaS tenant — rather than to the Environment URL you enter. No request from PagerDuty ever reached that mock.

ServiceNow's token endpoint is **always per-instance**: `https://<instance>.service-now.com/oauth_token.do`. There is no shared global ServiceNow host, and the connector's URL field is free text, so PagerDuty has no other way to know which instance to authenticate against. It therefore derives the token endpoint from that field — which is exactly what the captured traffic shows.

### The Knowledge table is NOT kb_knowledge
This cost a 404 and an SRE Agent reporting "no matching KB article was found". PagerDuty queries the Knowledge Management **search table**:

```
GET /api/now/table/sn_km_mr_st_kb_knowledge
      ?sysparm_fields=number,short_description,content,sys_updated_on,author,embedded_media,sys_id
      &sysparm_display_value=true
      &sysparm_limit=10
      &sysparm_query=number=<free text search terms>
```

Three non-obvious details, all of which the mock now handles:

1. **Table name.** `sn_km_mr_st_kb_knowledge`, not the generic `kb_knowledge` table and not the dedicated `/sn_km_api/knowledge/articles` endpoint. Both of those are still served as aliases in case other PagerDuty code paths use them, but neither is what gets called.
2. **`sysparm_query=number=<terms>` is not an equality match.** PagerDuty packs free-text search terms after `number=`. Comparing that against the `number` field matches nothing. The mock strips the `field=` prefix and ranks articles by weighted term overlap (scenario key > title > body); an explicit `KB…` number still wins outright.
3. **Field names.** PagerDuty asks for `content`, not `text`, plus `author`, `sys_updated_on` and `embedded_media`. `sysparm_fields` is honored, so the response contains exactly the requested keys in the requested order.

Search deliberately **never returns an empty list** — an empty result reads to the SRE Agent as "no runbook exists", so an unmatched query leads with the general handbook instead.

### DNS prerequisite
There is **no wildcard** on this zone — every service has its own A record. Before the HTTPS endpoint works you need:
```
servicenow.holodeck.scsandbox.net   A   35.88.248.161
```
Without it, Caddy cannot complete the TLS-ALPN-01 challenge and will retry cert issuance indefinitely (harmless to the other four vhosts, but this one will serve an untrusted cert).

If you would rather not wait on a DNS change, test via ngrok instead — it needs no DNS and no security group changes, and answers the same question:
```bash
./holodeck-local.sh servicenow      # flask on 3005 + ngrok
```
Then point the connector's URL at the ngrok HTTPS URL and watch ngrok's inspector at `http://127.0.0.1:4040`.

### Endpoints

| Endpoint | Status |
|---|---|
| `POST /oauth_token.do` | **Confirmed** — OAuth2 password grant, called by PD |
| `GET /api/now/table/sn_km_mr_st_kb_knowledge[/<id>]` | **Confirmed** — this is what PD queries |
| `GET /api/now/table/kb_knowledge[/<id>]` | Alias — generic Table API, not observed in use |
| `GET /sn_km_api/knowledge/articles[/<id>]` | Alias — dedicated KM API, shape is a guess, not observed in use |

### Verifying article retrieval without PagerDuty
Before blaming the connector, confirm the mock itself resolves the article. This runs the same round trip the SRE Agent does — token grant, PD's exact search query, then a direct fetch by KB number — and asserts the expected article is the top hit:

```bash
./scripts/verify-servicenow.sh                       # all scenarios, localhost:3005
./scripts/verify-servicenow.sh service-mesh-mtls     # one scenario
./scripts/verify-servicenow.sh all https://xxx.ngrok-free.dev
./scripts/verify-servicenow.sh all https://servicenow.holodeck.scsandbox.net
```

Expected article numbers are read from the fixtures, so the script cannot drift out of sync with the data. It also reports how many articles came back — a long list is what previously caused the agent to reply "returned 7 articles but none matched", so anything above 2–3 is a signal that relevance pruning has regressed.

### Verifying against live traffic
When debugging, always confirm what actually arrived rather than inferring from the agent's error message. Via ngrok:
```bash
curl -s http://127.0.0.1:4040/api/requests/http | python3 -m json.tool | grep '"uri"'
```
On EC2 (access logging is enabled on this vhost specifically, unlike the other four, so the real client IP and User-Agent are recorded):
```bash
docker logs -f --since 1m holodeck-servicenow
docker logs -f --since 1m holodeck-caddy 2>&1 | grep --line-buffered "handled request"
```

### Exercising it directly
```bash
# token (password grant — note: NOT client_credentials)
TOK=$(curl -s -X POST https://servicenow.holodeck.scsandbox.net/oauth_token.do \
  -d grant_type=password -d client_id=demo-client -d client_secret=demo-secret \
  -d username=demo-user -d password=demo-pass | jq -r .access_token)

# knowledge search (the table PagerDuty actually uses)
curl -s -H "Authorization: Bearer $TOK" \
  "https://servicenow.holodeck.scsandbox.net/api/now/table/sn_km_mr_st_kb_knowledge?sysparm_query=number=payments-api-gateway%20heap%20OOM" | jq .
```

## Admin endpoint

Each service exposes `POST /admin/reload` for hot-reloading fixtures from S3 without a container restart. This is called automatically by `holodeck.sh reload-fixtures`.

```bash
curl -sf -X POST https://grafana.holodeck.scsandbox.net/admin/reload | jq .
```

## Local development

For local testing with ngrok (one service at a time):
```bash
./holodeck-local.sh              # interactive menu
./holodeck-local.sh grafana      # start grafana-mock + ngrok
./holodeck-local.sh servicenow   # start servicenow-mock + ngrok
./holodeck-local.sh restart      # restart current service (picks up code edits)
./holodeck-local.sh stop
```

Use `restart` after editing a mock's `app.py`. Re-running `./holodeck-local.sh <same service>` is a no-op when it is already running, so it would keep serving the old code.

Requires: Python 3, `pip install -r requirements.txt`, ngrok configured with your static domain.

## EC2 instance details

| | |
|---|---|
| Instance | `ec2-35-88-248-161.us-west-2.compute.amazonaws.com` |
| Elastic IP | `35.88.248.161` |
| Region | `us-west-2` |
| OS | Amazon Linux 2023 |
| Security group | `Solution-Consulting-BVA` — `sg-075c3b95bf9657a5f` (inbound: 443 from 0.0.0.0/0, 22 and 4000 from specific /32s) |
| DNS | `*.holodeck.scsandbox.net` → `35.88.248.161` (not proxied). Corporate Cloudflare WARP blocks the domain on managed laptops |
| S3 bucket | `sc-holodeck-demo` (fixtures) |
| Project path | `~/holodeck/` |
| Key | `solutions-consulting.pem` |
