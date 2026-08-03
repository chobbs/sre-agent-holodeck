# Holodeck — SRE Connector Simulator

Mock API services for demoing PagerDuty's SRE Agent connectors without real observability infrastructure. All five mocks run simultaneously as independent HTTPS services on EC2, backed by scenario fixtures stored in S3.

## Live endpoints

| Service | URL | PagerDuty connector |
|---|---|---|
| Grafana | `https://grafana.holodeck.scsandbox.net` | Grafana |
| Arize | `https://arize.holodeck.scsandbox.net` | Arize |
| Splunk | `https://splunk.holodeck.scsandbox.net` | Splunk |
| Elasticsearch | `https://elasticsearch.holodeck.scsandbox.net` | Elasticsearch |
| Dynatrace | `https://dynatrace.holodeck.scsandbox.net:9999` | Dynatrace ¹ |

**Auth token for Grafana, Arize, Splunk, Elasticsearch:** `demo-token`

**Dynatrace auth:** OAuth2 client credentials — Client ID `demo-client`, Client Secret `demo-secret`

¹ Deployed, healthy, and serving on `:9999` with a valid Let's Encrypt cert. Reachability depends on the source — inbound `9999` is source-restricted on the security group, and PagerDuty-managed laptops are blocked by Cloudflare WARP. See [Dynatrace — known limitations](#dynatrace--known-limitations).

## Architecture

```
*.holodeck.scsandbox.net  →  Elastic IP (35.88.248.161)
                                       ↓
                            EC2 / Amazon Linux 2023
                                       ↓
                     Caddy (ports 443 + 9999, auto-TLS)
              ┌────────────┬───────────┬──────────────┬──────────────┐
           grafana       arize       splunk    elasticsearch    dynatrace
            :3000         :3001       :3002        :3003          :3004
                                       ↑
                          S3: sc-holodeck-demo
```

- **Caddy** handles TLS (Let's Encrypt, auto-renewing via TLS-ALPN-01 — port 80 not required)
- **Dynatrace** is served on port `9999` instead of `443`, because PagerDuty's connector expects the Dynatrace Managed/ActiveGate URL shape `{domain}:9999/e/{env-id}`
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
| Dynatrace | `https://dynatrace.holodeck.scsandbox.net:9999/e/demo-env` | Client ID + Client Secret | `demo-client` / `demo-secret` |

The Dynatrace row is included for completeness — the connector will fail at the auth step regardless of what you enter. See below.

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
  dynatrace/scenarios.json
```

## Scenario matching

Each service matches incoming requests against scenario keys using substring matching on the full request body. To trigger a specific scenario, include its key in the request — the SRE Agent does this automatically by embedding incident `custom_details` fields (service name, host, etc.) in its queries.

**Available scenarios per service:**

| Grafana (logs) | Grafana (metrics) | Arize | Splunk | Elasticsearch | Dynatrace |
|---|---|---|---|---|---|
| checkout-api | checkout-api | checkout-agent | suspicious-login | payments-api-gateway | payments-api-gateway |
| payments-svc | payments-svc | fraud-detection-model | privilege-escalation | payments-orchestrator-sync | payments-orchestrator-sync |
| orders-db-proxy | orders-db-proxy | rag-retrieval-agent | data-exfiltration | idempotency-token-service | idempotency-token-service |
| auth-service | auth-service | hallucination | malware-detection | payments-rules-engine | service-mesh-mtls |
| search-api | search-api | support-chatbot | firewall-policy-violation | service-mesh-mtls | edge-waf-cdn |
| *(default)* | *(default)* | *(default)* | *(default)* | *(default)* | *(default)* |

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
├── dynatrace-mock/
│   ├── app.py               # Dynatrace Grail API mock (OAuth2 + DQL)
│   ├── Dockerfile
│   └── fixtures/scenarios.json
└── scripts/
    ├── bootstrap-ec2.sh     # Fresh instance setup (Docker + Buildx + Compose)
    └── sync-fixtures-to-s3.sh  # Upload all fixture JSON to S3
```

## Dynatrace — known limitations

The Dynatrace mock **is deployed** as part of the EC2 stack. The container is healthy, Caddy serves it on port `9999`, and it holds a valid Let's Encrypt certificate for `dynatrace.holodeck.scsandbox.net`. It implements the OAuth2 client-credentials token exchange, DQL query execution, and the async poll flow.

The DNS zone is **not** proxied — `*.holodeck.scsandbox.net` resolves straight to `35.88.248.161`. Three independent things affect reachability and PagerDuty integration:

**1. Cloudflare WARP blocks the domain on PagerDuty-managed machines.** Requests from a corporate laptop return `303 See Other` to `blocked.teams.cloudflare.com` ("blocked by PagerDuty IT"). This is a device-level Zero Trust DNS/HTTP filter and it affects **all five services**, not just Dynatrace — the other four appear to work only because PagerDuty's cloud reaches them directly, not through WARP.

To test from a corporate laptop, pin the IP so WARP's DNS interception is bypassed:
```bash
curl -s --resolve dynatrace.holodeck.scsandbox.net:9999:35.88.248.161 \
  -X POST https://dynatrace.holodeck.scsandbox.net:9999/sso/oauth2/token \
  -d grant_type=client_credentials \
  -d client_id=demo-client \
  -d client_secret=demo-secret | jq .
```

**2. Inbound 9999 is source-restricted on the security group.** Port `443` is open to `0.0.0.0/0`, but `9999` is not: connections succeed from some sources and are refused from others (verified — Caddy listens on both ports, yet the instance cannot reach its own Elastic IP on `9999` while `443` succeeds). If PagerDuty's egress IPs are outside the allowed range, the connector cannot reach the mock regardless of anything else. Widen the `9999` rule before concluding anything from a failed connector test.

**3. PagerDuty appears to hardcode the OAuth2 token endpoint.** Earlier testing suggested the connector uses `sso.dynatrace.com` regardless of the Environment URL entered, so the token request never reaches the mock. Treat this as **unconfirmed** until items 1 and 2 are ruled out — a token request blocked by the security group is indistinguishable from one that was never sent. Check the container log to see whether a request actually arrived:
```bash
docker logs holodeck-dynatrace --tail 50
```

To verify the mock itself independent of all networking, run it from the instance:
```bash
docker exec holodeck-dynatrace curl -s -X POST http://localhost:3004/sso/oauth2/token \
  -d grant_type=client_credentials -d client_id=demo-client -d client_secret=demo-secret | jq .
```

Set `SIMULATE_ASYNC=true` in `.env` to force the polling path (query returns `202 RUNNING`, then succeeds on the next poll).

The service is kept in the stack so the API surface stays exercised and deployable — useful for verifying DQL query shape and for the day PagerDuty makes the token endpoint configurable.

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
| Security group | `Solution-Consulting-BVA` (inbound: 443 from 0.0.0.0/0, 9999 source-restricted, 22 from trusted IPs) |
| DNS | `*.holodeck.scsandbox.net` → `35.88.248.161` (not proxied). Corporate Cloudflare WARP blocks the domain on managed laptops |
| S3 bucket | `sc-holodeck-demo` (fixtures) |
| Project path | `~/holodeck/` |
| Key | `solutions-consulting.pem` |
