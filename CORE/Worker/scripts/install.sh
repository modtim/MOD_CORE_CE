#!/usr/bin/env bash
set -euo pipefail

# MOD Worker Install Script
#
# This script is served by the API at /workers/agent/install-script.
# A platform admin generates a one-time install command from the Dashboard:
#
#   curl -fsSL 'https://api.example.com/workers/agent/install-script?token=TOKEN' | bash -s -- \
#     --api-url https://api.example.com \
#     --token TOKEN \
#     --bootstrap-token ENROLLMENT_TOKEN
#
# The download token is short-lived (15 min) and single-use.

INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="/etc/mod-worker"
SERVICE_NAME="mod-worker"
BINARY_NAME="mod-worker"

# Detect OS and arch
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  ARCH="amd64" ;;
  aarch64) ARCH="arm64" ;;
  arm64)   ARCH="arm64" ;;
  *)       echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

echo "==> Detected ${OS}/${ARCH}"

# Parse arguments
MODE="remote"
API_URL=""
DOWNLOAD_TOKEN=""
BOOTSTRAP_TOKEN=""
VAULT_ADDR=""
VAULT_ROLE_ID=""
VAULT_SECRET_ID=""
REDIS_URL=""
WORKER_NAME=$(hostname)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)            MODE="$2"; shift 2 ;;
    --api-url)         API_URL="$2"; shift 2 ;;
    --token)           DOWNLOAD_TOKEN="$2"; shift 2 ;;
    --bootstrap-token) BOOTSTRAP_TOKEN="$2"; shift 2 ;;
    --vault-addr)      VAULT_ADDR="$2"; shift 2 ;;
    --vault-role-id)   VAULT_ROLE_ID="$2"; shift 2 ;;
    --vault-secret-id) VAULT_SECRET_ID="$2"; shift 2 ;;
    --redis-url)       REDIS_URL="$2"; shift 2 ;;
    --worker-name)     WORKER_NAME="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [ -z "$API_URL" ]; then
    echo "Error: --api-url is required"
    exit 1
fi
if [ -z "$DOWNLOAD_TOKEN" ]; then
    echo "Error: --token is required"
    echo "  Generate one from the Dashboard (Workers → Add Remote Worker)"
    echo "  or via API: POST /workers/agent/download-token (platform admin)"
    exit 1
fi

# Download binary using the short-lived token
echo "==> Downloading worker agent for ${OS}/${ARCH}..."
HTTP_CODE=$(curl -sSL -w "%{http_code}" \
    "${API_URL}/workers/agent/download?token=${DOWNLOAD_TOKEN}&os_name=${OS}&arch=${ARCH}" \
    -o "/tmp/${BINARY_NAME}")

if [ "$HTTP_CODE" != "200" ]; then
    echo "Error: Download failed (HTTP ${HTTP_CODE})"
    cat "/tmp/${BINARY_NAME}" 2>/dev/null  # Show error message from API
    rm -f "/tmp/${BINARY_NAME}"
    exit 1
fi

chmod +x "/tmp/${BINARY_NAME}"
sudo mv "/tmp/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"

echo "==> Installed to ${INSTALL_DIR}/${BINARY_NAME}"

# Create config directory
sudo mkdir -p "${CONFIG_DIR}"

# Write environment file for systemd
cat <<EOF | sudo tee "${CONFIG_DIR}/worker.env" > /dev/null
WORKER_NAME=${WORKER_NAME}
WORKER_MODE=${MODE}
API_URL=${API_URL}
BOOTSTRAP_TOKEN=${BOOTSTRAP_TOKEN}
VAULT_ADDR=${VAULT_ADDR}
VAULT_APPROLE_ROLE_ID=${VAULT_ROLE_ID}
VAULT_APPROLE_SECRET_ID=${VAULT_SECRET_ID}
REDIS_URL=${REDIS_URL}
EOF
sudo chmod 600 "${CONFIG_DIR}/worker.env"

# Create systemd service
cat <<EOF | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null
[Unit]
Description=MOD Worker Agent
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
EnvironmentFile=${CONFIG_DIR}/worker.env
ExecStart=${INSTALL_DIR}/${BINARY_NAME}
Restart=always
RestartSec=10
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo ""
echo "==> ${SERVICE_NAME} installed and started"
echo "    Check status: systemctl status ${SERVICE_NAME}"
echo "    View logs:    journalctl -u ${SERVICE_NAME} -f"
