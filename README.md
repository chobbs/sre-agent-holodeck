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

¹ Requires a DNS A record for `servicenow.holodeck.scsandbox.net` (see [ServiceNow](#servicenow--oauth-connector)). Whether PagerDuty will actually authenticate against it is **unverified** — that is the first thing to test.

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
    └── sync-fixtures-to-s3.sh  # Upload all fixture JSON to S3
```

## ServiceNow — OAuth connector

This is the newest mock and the only one whose PagerDuty integration is **not yet proven**. Read this before spending time on it.

### Why it might work where Dynatrace did not
The Dynatrace attempt failed because PagerDuty appears to send the OAuth token request to `sso.dynatrace.com` — a host shared by every Dynatrace SaaS tenant — rather than to the Environment URL you enter. No request from PagerDuty ever reached the mock.

ServiceNow is structurally different: its token endpoint is **always per-instance**, `https://<instance>.service-now.com/oauth_token.do`. There is no shared global ServiceNow host, and the connector's URL field is free text, so PagerDuty has no other way to know which instance to authenticate against. That is a real reason for optimism — but it is still **unverified**.

### Test the one thing that matters first
Before trusting anything downstream, confirm a request actually arrives. Watch the mock while saving the connector:
```bash
# app-level: shows the request path and status
docker logs -f --since 1m holodeck-servicenow

# edge-level: shows the real client IP and User-Agent (access logging is on
# for this vhost specifically, unlike the other four)
docker logs -f --since 1m holodeck-caddy 2>&1 | grep --line-buffered "handled request"
```
- `POST /oauth_token.do ... 200` → PagerDuty honors the URL field. Everything downstream is already built and tested.
- Nothing but healthcheck traffic → same dead end as Dynatrace. Stop there; nothing downstream matters.
- `401 invalid_client` / `401 invalid_grant` → PagerDuty reached the mock but the credentials do not match. Check for stray whitespace.

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

### Two Knowledge endpoints, deliberately
It is unconfirmed which endpoint PagerDuty's "Knowledge" tool calls, so both are implemented:

| Endpoint | Confidence |
|---|---|
| `GET /api/now/table/kb_knowledge` | **Confirmed** — generic Table API, well documented |
| `GET /api/now/table/kb_knowledge/<sys_id>` | **Confirmed** — fetch one article |
| `GET /sn_km_api/knowledge/articles` | **Best guess** — the dedicated Knowledge Management API is real, but its exact response JSON was not confirmed |
| `GET /sn_km_api/knowledge/articles/<sys_id>` | **Best guess** |

Whichever one appears in the logs is the real one; the other can be deleted. Same approach as the Arize `/v2/spaces` vs `/v2/projects` ambiguity.

### Exercising it directly
```bash
# token (password grant — note: NOT client_credentials)
TOK=$(curl -s -X POST https://servicenow.holodeck.scsandbox.net/oauth_token.do \
  -d grant_type=password -d client_id=demo-client -d client_secret=demo-secret \
  -d username=demo-user -d password=demo-pass | jq -r .access_token)

# knowledge search
curl -s -H "Authorization: Bearer $TOK" \
  "https://servicenow.holodeck.scsandbox.net/api/now/table/kb_knowledge?sysparm_query=payments-api-gateway" | jq .
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
./holodeck-local.sh stop
```

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
