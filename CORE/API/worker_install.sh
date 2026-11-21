#!/usr/bin/env bash
set -euo pipefail

# MOD Worker Install Script (Linux / systemd)
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
#
# NON-ROOT (GSEC Q21.5): the worker now runs as the dedicated system user
# `mod-worker` (system account, no login shell) instead of root. The binary is
# installed to /opt/mod-worker (owned by mod-worker so SELF-UPDATE keeps
# overwriting it in place) and state lives in /var/lib/mod-worker (owned by
# mod-worker). Docker daemon access is via docker-group membership — GPU
# passthrough stays daemon-side, so membership is sufficient. The systemd
# unit is hardened (NoNewPrivileges / ProtectSystem=strict / PrivateTmp /
# CAP_CHOWN ambient+bounding only). Re-running this script on an existing
# install migrates root -> mod-worker idempotently (chown state, rewrite unit,
# daemon-reload). `--run-as-root` is a documented escape hatch for hosts where
# non-root cannot work.
#
# `--print-unit` previews the systemd unit that would be written (dry run,
# no download / no side effects) — useful to confirm the hardening before
# installing.

INSTALL_DIR="/opt/mod-worker"
STATE_DIR="/var/lib/mod-worker"
CONFIG_DIR="/etc/mod-worker"
SERVICE_NAME="mod-worker"
BINARY_NAME="mod-worker"
WORKER_USER="mod-worker"

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
# When the API rendered per-worker AppRole creds into the served script (a
# download-token minted with a worker_name), the server substitutes the
# __MOD_VAULT_ROLE_ID__ / __MOD_VAULT_SECRET_ID__ placeholders below with the
# actual values before the script is served — so the operator no longer pastes
# a shared pair by hand. An explicit --vault-role-id / --vault-secret-id
# always wins over the rendered defaults.
VAULT_ROLE_ID="__MOD_VAULT_ROLE_ID__"
VAULT_SECRET_ID="__MOD_VAULT_SECRET_ID__"
REDIS_URL=""
WORKER_NAME=$(hostname)
RUN_AS_ROOT=0
PRINT_UNIT=0

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
    --run-as-root)     RUN_AS_ROOT=1; shift ;;
    --print-unit)      PRINT_UNIT=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# If the server did NOT render per-worker creds (a download-token minted
# without a worker_name), the placeholders are still here — treat them as
# empty so the operator can still pass --vault-role-id/--vault-secret-id.
if [ "$VAULT_ROLE_ID" = "__MOD_VAULT_ROLE_ID__" ]; then VAULT_ROLE_ID=""; fi
if [ "$VAULT_SECRET_ID" = "__MOD_VAULT_SECRET_ID__" ]; then VAULT_SECRET_ID=""; fi

# render_unit: print the systemd unit that matches the current RUN_AS_ROOT /
# path state, to stdout. The main flow pipes it to the unit file; --print-unit
# just prints it. Keeping this in ONE place is what the render test asserts
# against, so the test can never drift from what actually gets installed.
render_unit() {
  local service_block
  if [ "$RUN_AS_ROOT" = "1" ]; then
    service_block="[Service]
Type=simple
EnvironmentFile=${CONFIG_DIR}/worker.env
ExecStart=${INSTALL_DIR}/${BINARY_NAME}
Restart=always
RestartSec=10
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=300"
  else
    service_block="[Service]
Type=simple
User=${WORKER_USER}
Group=${WORKER_USER}
SupplementaryGroups=docker video render
EnvironmentFile=${CONFIG_DIR}/worker.env
Environment=MOD_WORKER_STATE_DIR=${STATE_DIR}
ExecStart=${INSTALL_DIR}/${BINARY_NAME}
Restart=always
RestartSec=10
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=300
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ReadWritePaths=${STATE_DIR} ${INSTALL_DIR} ${CONFIG_DIR}
StateDirectory=mod-worker
CapabilityBoundingSet=CAP_CHOWN
AmbientCapabilities=CAP_CHOWN"
  fi
  cat <<EOF
[Unit]
Description=MOD Worker Agent
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
${service_block}
[Install]
WantedBy=multi-user.target
EOF
}

# Dry run: print the unit that would be installed, then stop. No download, no
# user/dir creation, no systemd contact.
if [ "$PRINT_UNIT" = "1" ]; then
    if [ "$RUN_AS_ROOT" = "1" ]; then
        INSTALL_DIR="/usr/local/bin"
    fi
    echo "==> Previewing ${SERVICE_NAME}.service (run-as-root=${RUN_AS_ROOT})"
    render_unit
    exit 0
fi

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

# Root vs non-root. `sudo` is only used when the caller is not already root
# (also lets the installer run unattended as root inside a container/CI).
if [ "$(id -u)" = "0" ]; then
    SUDO=""
else
    SUDO="sudo"
fi

if [ "$RUN_AS_ROOT" = "1" ]; then
    echo "WARNING: --run-as-root — installing the worker to run as ROOT."
    echo "         This is an escape hatch for hosts where non-root cannot"
    echo "         work; the unit will NOT be hardened and the worker keeps"
    echo "         full privileges. The default (recommended) is non-root."
fi

# Non-root identity: create the mod-worker system user and the device groups it
# needs, and make it a member of docker (daemon access) plus video/render
# (direct /dev/dri + /dev/kfd access if the daemon-side path is ever bypassed).
ensure_nonroot_identity() {
    local g
    if ! getent group docker >/dev/null 2>&1; then
        echo "==> Creating 'docker' group"
        $SUDO groupadd docker
    fi
    for g in video render; do
        if ! getent group "$g" >/dev/null 2>&1; then
            echo "==> Creating device group '$g'"
            $SUDO groupadd "$g"
        fi
    done
    # The unit sets Group=mod-worker and the env file is root:mod-worker 0640,
    # so the group must exist. Ubuntu's useradd -r does NOT create a user
    # group, so create it explicitly.
    if ! getent group "$WORKER_USER" >/dev/null 2>&1; then
        $SUDO groupadd -r "$WORKER_USER"
    fi
    if ! getent passwd "$WORKER_USER" >/dev/null 2>&1; then
        echo "==> Creating system user '$WORKER_USER'"
        $SUDO useradd -r -g "$WORKER_USER" -d /nonexistent -s /usr/sbin/nologin "$WORKER_USER"
    fi
    $SUDO usermod -aG docker,video,render "$WORKER_USER"
}

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

if [ "$RUN_AS_ROOT" = "1" ]; then
    # Escape hatch: preserve the legacy root install shape (binary in
    # /usr/local/bin, root-owned). Documented as non-recommended.
    INSTALL_DIR="/usr/local/bin"
    STATE_DIR="${CONFIG_DIR}"
    $SUDO install -d -m 0755 "${INSTALL_DIR}"
    $SUDO mv "/tmp/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"
else
    # Non-root: create the mod-worker identity, then make it the owner of the
    # install dir (self-update overwrites the binary in place) and the state
    # dir. This is idempotent — re-running on an existing install re-chowns.
    ensure_nonroot_identity
    $SUDO install -d -o "$WORKER_USER" -g "$WORKER_USER" -m 0755 "${INSTALL_DIR}"
    $SUDO install -d -o "$WORKER_USER" -g "$WORKER_USER" -m 0750 "${STATE_DIR}"
    $SUDO mv "/tmp/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"
    $SUDO chown "${WORKER_USER}:${WORKER_USER}" "${INSTALL_DIR}/${BINARY_NAME}"
    $SUDO chmod 0755 "${INSTALL_DIR}/${BINARY_NAME}"
    # Migration: pull any pre-existing root-owned state into the new state dir
    # and drop ownership to mod-worker so a root-era install keeps working.
    if [ -d "${CONFIG_DIR}" ] && [ -d "${STATE_DIR}" ]; then
        $SUDO chown -R "${WORKER_USER}:${WORKER_USER}" "${STATE_DIR}"
    fi
fi

echo "==> Installed to ${INSTALL_DIR}/${BINARY_NAME}"

# Create config directory (root-owned; the env file inside is group-readable
# by mod-worker under the non-root layout, so the unit's EnvironmentFile= can
# read it without granting world access).
$SUDO mkdir -p "${CONFIG_DIR}"
$SUDO chown root:root "${CONFIG_DIR}"
$SUDO chmod 0755 "${CONFIG_DIR}"

# Write environment file for systemd
cat <<EOF | $SUDO tee "${CONFIG_DIR}/worker.env" > /dev/null
WORKER_NAME=${WORKER_NAME}
WORKER_MODE=${MODE}
API_URL=${API_URL}
BOOTSTRAP_TOKEN=${BOOTSTRAP_TOKEN}
VAULT_ADDR=${VAULT_ADDR}
VAULT_APPROLE_ROLE_ID=${VAULT_ROLE_ID}
VAULT_APPROLE_SECRET_ID=${VAULT_SECRET_ID}
REDIS_URL=${REDIS_URL}
EOF

if [ "$RUN_AS_ROOT" = "1" ]; then
    # Root layout: the process is root, so a tight 0600 root:root file suffices.
    $SUDO chown root:root "${CONFIG_DIR}/worker.env"
    $SUDO chmod 0600 "${CONFIG_DIR}/worker.env"
else
    # Non-root: owner root, group mod-worker, mode 0640 — the unit (running as
    # mod-worker) reads it via group membership; nobody else can.
    if getent group "$WORKER_USER" >/dev/null 2>&1; then
        $SUDO chown root:"${WORKER_USER}" "${CONFIG_DIR}/worker.env"
    else
        $SUDO chown root:root "${CONFIG_DIR}/worker.env"
    fi
    $SUDO chmod 0640 "${CONFIG_DIR}/worker.env"
fi

render_unit | $SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null

# Load/refresh the unit only where systemd is actually running (a real host).
# In a container (no /run/systemd/system) this is skipped and the operator
# runs the equivalent commands at release time.
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "${SERVICE_NAME}"
    $SUDO systemctl restart "${SERVICE_NAME}"
else
    echo "==> systemd not detected (container?); unit written but not loaded."
    echo "    At release time run:"
    echo "      systemctl daemon-reload && systemctl enable --now ${SERVICE_NAME}"
fi

echo ""
echo "==> ${SERVICE_NAME} installed and started"
if [ "$RUN_AS_ROOT" = "1" ]; then
    echo "    (running as ROOT — see the --run-as-root warning above)"
fi
echo "    Check status: systemctl status ${SERVICE_NAME}"
echo "    View logs:    journalctl -u ${SERVICE_NAME} -f"
