#!/usr/bin/env bash
# bootstrap-ec2.sh
#
# Run this on a fresh Amazon Linux 2023 instance to install Docker,
# Docker Compose, Docker Buildx, copy the project, and start the stack.
#
# Assumes:
#   - You have already transferred the project to ~/holodeck/ via rsync
#   - You will create ~/holodeck/.env before running `docker compose up`
#   - AWS credentials are configured (aws configure) for S3 fixture access
#     (or an IAM instance profile is attached — preferred for production)
#
# Usage (from your Mac):
#   rsync -avz --exclude='.DS_Store' --exclude='__pycache__' \
#     --exclude='.env' -e "ssh -i ~/.ssh/your-key.pem" \
#     ./sre-conn-simulator/ ec2-user@<EC2-IP>:~/holodeck/
#   ssh -i ~/.ssh/your-key.pem ec2-user@<EC2-IP> "bash ~/holodeck/scripts/bootstrap-ec2.sh"

set -euo pipefail

echo "[bootstrap] Starting Holodeck EC2 setup..."

# ── Docker ───────────────────────────────────────────────────────────────────
echo "[bootstrap] Installing Docker..."
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
echo "[bootstrap] Docker $(docker --version | awk '{print $3}' | tr -d ',') installed."

# ── Docker Buildx ────────────────────────────────────────────────────────────
echo "[bootstrap] Installing Docker Buildx..."
BUILDX_VER=$(curl -fsSL https://api.github.com/repos/docker/buildx/releases/latest \
  | grep '"tag_name"' | cut -d'"' -f4)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL \
  "https://github.com/docker/buildx/releases/download/${BUILDX_VER}/buildx-${BUILDX_VER}.linux-amd64" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
echo "[bootstrap] Docker Buildx ${BUILDX_VER} installed."

# ── Docker Compose plugin ────────────────────────────────────────────────────
echo "[bootstrap] Installing Docker Compose plugin..."
COMPOSE_VER=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
  | grep '"tag_name"' | cut -d'"' -f4)
sudo curl -fsSL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VER}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
echo "[bootstrap] Docker Compose ${COMPOSE_VER} installed."

# ── .env file ────────────────────────────────────────────────────────────────
ENV_FILE="${HOME}/holodeck/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "[bootstrap] Creating .env from .env.example..."
  cp "${HOME}/holodeck/.env.example" "$ENV_FILE"
  echo "[bootstrap] IMPORTANT: edit ${ENV_FILE} and set:"
  echo "            MOCK_API_TOKEN, HOLODECK_DOMAIN, FIXTURES_S3_BUCKET"
  echo "            AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION"
  echo "            (or attach an IAM instance profile and omit the AWS_* vars)"
else
  echo "[bootstrap] .env already exists — skipping."
fi

# ── Start the stack ───────────────────────────────────────────────────────────
# ── Git ──────────────────────────────────────────────────────────────────
echo "[bootstrap] Installing git..."
sudo dnf install -y git -q
git config --global init.defaultBranch main
echo "[bootstrap] Git $(git --version) installed."

# ── Wire up git repo ────────────────────────────────────────────────────────
HOLODECK_DIR="${HOME}/holodeck"
if [ ! -d "${HOLODECK_DIR}/.git" ]; then
  echo "[bootstrap] Initializing git repo in ${HOLODECK_DIR}..."
  git -C "${HOLODECK_DIR}" init
  git -C "${HOLODECK_DIR}" remote add origin https://github.com/chobbs/sre-agent-holodeck.git
  git -C "${HOLODECK_DIR}" fetch origin main
  git -C "${HOLODECK_DIR}" reset origin/main        # mixed reset: index = origin/main, working tree unchanged
  git -C "${HOLODECK_DIR}" branch -m main 2>/dev/null || true
  git -C "${HOLODECK_DIR}" branch --set-upstream-to=origin/main main
  echo "[bootstrap] Git repo ready. Future deploys: bash ~/holodeck/holodeck.sh deploy"
else
  echo "[bootstrap] Git repo already initialized — skipping."
fi

echo "[bootstrap] Building and starting Holodeck stack..."
cd "${HOME}/holodeck"
sudo docker compose up --build -d

echo ""
echo "[bootstrap] Done. Stack status:"
sudo docker compose ps
echo ""
echo "[bootstrap] Next steps:"
echo "  1. If not already done, sync fixtures to S3:"
echo "       bash ~/holodeck/scripts/sync-fixtures-to-s3.sh <bucket-name>"
echo "  2. Point *.holodeck.yourdomain.com at this instance's Elastic IP"
echo "  3. Swap Caddyfile to the subdomain blocks and restart Caddy:"
echo "       sudo docker compose restart caddy"
