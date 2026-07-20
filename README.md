# Holodeck — SRE Connector Simulator

Mock API services for demoing PagerDuty's SRE Agent connectors without real observability infrastructure. All four mocks run simultaneously as independent HTTPS services on EC2, backed by scenario fixtures stored in S3.

## Live endpoints

| Service | URL | PagerDuty connector |
|---|---|---|
| Grafana | `https://grafana.holodeck.scsandbox.net` | Grafana |
| Arize | `https://arize.holodeck.scsandbox.net` | Arize |
| Splunk | `https://splunk.holodeck.scsandbox.net` | Splunk |
| Elasticsearch | `https://elasticsearch.holodeck.scsandbox.net` | Elasticsearch |

**Auth token for all services:** `demo-token`

## Architecture

```
*.holodeck.scsandbox.net  →  Elastic IP (35.88.248.161)
                                       ↓
                            EC2 / Amazon Linux 2023
                                       ↓
                            Caddy (port 443, auto-TLS)
              ┌────────────┬───────────┬──────────────────┐
           grafana       arize       splunk        elasticsearch
            :3000         :3001       :3002             :3003
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
```

## Scenario matching

Each service matches incoming requests against scenario keys using substring matching on the full request body. To trigger a specific scenario, include its key in the request — the SRE Agent does this automatically by embedding incident `custom_details` fields (service name, host, etc.) in its queries.

**Available scenarios per service:**

| Grafana (logs) | Grafana (metrics) | Arize | Splunk | Elasticsearch |
|---|---|---|---|---|
| checkout-api | checkout-api | checkout-agent | suspicious-login | payments-api-gateway |
| payments-svc | payments-svc | fraud-detection-model | privilege-escalation | payments-orchestrator-sync |
| orders-db-proxy | orders-db-proxy | rag-retrieval-agent | data-exfiltration | idempotency-token-service |
| auth-service | auth-service | hallucination | malware-detection | payments-rules-engine |
| search-api | search-api | support-chatbot | firewall-policy-violation | service-mesh-mtls |
| *(default)* | *(default)* | *(default)* | *(default)* | *(default)* |

## Project structure

```
sre-conn-simulator/
├── holodeck.sh              # EC2 stack manager (docker compose wrapper)
├── holodeck-local.sh        # Local dev runner (Flask + ngrok, one service at a time)
├── docker-compose.yml       # All 4 mocks + Caddy
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
└── scripts/
    ├── bootstrap-ec2.sh     # Fresh instance setup (Docker + Buildx + Compose)
    └── sync-fixtures-to-s3.sh  # Upload all fixture JSON to S3
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
| Security group | `Solution-Consulting-BVA` (inbound: 443 from 0.0.0.0/0, 22 from trusted IPs) |
| S3 bucket | `sc-holodeck-demo` (fixtures) |
| Project path | `~/holodeck/` |
| Key | `solutions-consulting.pem` |
