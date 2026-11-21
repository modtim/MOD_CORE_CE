#!/usr/bin/env bash
# sz-mac-worker.5 — MOD Remote Worker installer for macOS (LaunchAgent, per-user, no sudo)
#
# macOS sibling of setup-worker.sh (Linux/systemd) and install.ps1 (Windows/SCM).
# Downloads the darwin binary from the SAME public release route setup-worker.sh
# uses (${API_URL}/v1/workers/download/<os>/<arch>), enrolls it with the SAME
# two-call flow (/v1/workers/enroll/validate-token then /v1/workers/enroll/start),
# writes the SAME worker.yaml config, and registers the launchd job from the
# plist template next to this script (com.example.worker.plist).
#
#   ./install-macos.sh --api-url https://api.example.com --token <enroll-token>
#   ./install-macos.sh --dry-run --api-url ... --token X   (prints, touches nothing)
#   ./install-macos.sh --uninstall [--purge]
#
# The enrollment token is consumed by the worker's existing enroll endpoints; it
# is never written to the plist, the config's only secret field is the bootstrap
# token the enroll response returns (0600 worker.yaml), and it is never echoed.

set -euo pipefail

LABEL="com.example.worker"
SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]:-$0}")"
PLIST_TEMPLATE="${SCRIPT_DIR}/com.example.worker.plist"
HOME_DIR="${HOME:-}"
INSTALL_DIR="${HOME_DIR}/Library/Application Support/MOD/worker"
CONFIG_FILE="worker.yaml"
BINARY_NAME="mod-worker"
PLIST_PATH="${HOME_DIR}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME_DIR}/Library/Logs"
OUT_LOG="${LOG_DIR}/mod-worker.out.log"
ERR_LOG="${LOG_DIR}/mod-worker.err.log"
UID_NUM="$(id -u)"
LAUNCH_DOMAIN="gui/${UID_NUM}"
LAUNCH_TARGET="${LAUNCH_DOMAIN}/${LABEL}"

DRY_RUN=false
UNINSTALL=false
PURGE=false
INSECURE_HTTP="${INSECURE_HTTP:-}"
API_URL=""
ENROLLMENT_TOKEN=""
WORKER_NAME="${WORKER_NAME:-}"

usage() {
    cat <<EOF
Usage: install-macos.sh [--api-url URL] [--token TOKEN] [--worker-name NAME]
                        [--dry-run] [--uninstall [--purge]]
  --api-url URL       MOD API base URL (bare, no /v1)
  --token TOKEN       enrollment token from Dashboard > Workers > Add Worker
  --worker-name NAME  worker name (default: derived from hostname)
  --dry-run           print every planned action + the rendered plist path, touch nothing
  --uninstall         bootout the job and remove the plist (binary + logs kept)
  --purge             with --uninstall: also remove the binary and worker.yaml
  --insecure-http     allow a non-https API URL (default: https only; env INSECURE_HTTP=1)
EOF
}

xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
    printf '%s' "$s"
}

sed_value() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//|/\\|}"; s="${s//&/\\&}"
    printf '%s' "$s"
}

json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    printf '%s' "$s" | tr -d '[:cntrl:]'
}

plan() { printf 'PLAN: %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --api-url)          API_URL="${2:-}"; shift 2 ;;
        --api-url=*)        API_URL="${1#*=}"; shift ;;
        --token|--enrollment-token)
            ENROLLMENT_TOKEN="${2:-}"; shift 2 ;;
        --token=*|--enrollment-token=*)
            ENROLLMENT_TOKEN="${1#*=}"; shift ;;
        --worker-name)      WORKER_NAME="${2:-}"; shift 2 ;;
        --worker-name=*)    WORKER_NAME="${1#*=}"; shift ;;
        --dry-run)          DRY_RUN=true; shift ;;
        --uninstall)        UNINSTALL=true; shift ;;
        --purge)            PURGE=true; shift ;;
        --insecure-http)    INSECURE_HTTP=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)                  printf 'Unknown argument: %s\n' "$1"; usage; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Platform gate (skippable with --dry-run so the plan is reviewable anywhere)
# ---------------------------------------------------------------------------
OS_RAW="$(uname -s)"
if [ "$OS_RAW" != "Darwin" ] && [ "$DRY_RUN" != true ]; then
    printf 'Error: this installer is for macOS only (detected %s).\n' "$OS_RAW"
    printf '       Use CORE/Worker/scripts/setup-worker.sh on Linux, install.ps1 on Windows.\n'
    printf '       Pass --dry-run to preview what this script would do.\n'
    exit 1
fi

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
    arm64|aarch64) ARCH="arm64" ;;
    x86_64|amd64)  ARCH="amd64" ;;
    *)             ARCH="amd64"; printf 'Note: unrecognised arch %s, assuming amd64\n' "$ARCH_RAW" ;;
esac

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [ "$UNINSTALL" = true ]; then
    plan "launchctl bootout ${LAUNCH_TARGET}"
    plan "rm -f ${PLIST_PATH}"
    if [ "$PURGE" = true ]; then
        plan "rm -f ${INSTALL_DIR}/${BINARY_NAME} ${INSTALL_DIR}/${CONFIG_FILE}"
    else
        plan "keep ${INSTALL_DIR}/${BINARY_NAME}, ${INSTALL_DIR}/${CONFIG_FILE} and ${LOG_DIR}/mod-worker.*.log"
    fi
    if [ "$DRY_RUN" = true ]; then
        printf 'DRY RUN: nothing removed.\n'
        exit 0
    fi
    launchctl bootout "$LAUNCH_TARGET" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    if [ "$PURGE" = true ]; then
        rm -f "${INSTALL_DIR}/${BINARY_NAME}" "${INSTALL_DIR}/${CONFIG_FILE}"
    fi
    printf 'Uninstalled %s (logs and binary kept unless --purge).\n' "$LABEL"
    printf 'Check: launchctl print gui/%s/%s 2>/dev/null\n' "$UID_NUM" "$LABEL"
    exit 0
fi

# ---------------------------------------------------------------------------
# Configuration (same prompts/flags as setup-worker.sh; token never echoed)
# ---------------------------------------------------------------------------
if [ -z "$API_URL" ]; then
    printf 'API URL (e.g. https://api.yourcompany.com): '
    read -r API_URL || API_URL=""
fi
API_URL="${API_URL%/}"
if [ -z "$API_URL" ]; then
    printf 'Error: --api-url is required\n'
    exit 1
fi
case "$API_URL" in
    https://*) ;;
    http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*) ;;
    *)
        if [ "${INSECURE_HTTP:-}" != "1" ]; then
            printf 'Error: API URL must be https (pass --insecure-http or INSECURE_HTTP=1 to allow plain http).\n'
            exit 1
        fi
        printf 'Warning: plain http API URL accepted via --insecure-http\n'
        ;;
esac
BINARY_URL="${API_URL}/v1/workers/download/darwin/${ARCH}"

if [ -z "$ENROLLMENT_TOKEN" ]; then
    printf 'Enrollment token (from Dashboard > Workers > Add Worker): '
    read -r ENROLLMENT_TOKEN || ENROLLMENT_TOKEN=""
fi

if [ -z "$WORKER_NAME" ]; then
    HOST_NAME_VAL="$(hostname)"
    MACHINE_ID=""
    [ -r "/etc/machine-id" ] && MACHINE_ID="$(tr -dc 'a-f0-9' < /etc/machine-id 2>/dev/null | head -c 8)" || true
    if { [ "$HOST_NAME_VAL" = "localhost" ] || [ "$HOST_NAME_VAL" = "localhost.localdomain" ]; } \
        && [ -n "$MACHINE_ID" ]; then
        WORKER_NAME="worker-${MACHINE_ID}"
    else
        WORKER_NAME="worker-${HOST_NAME_VAL:-unknown}"
    fi
fi

printf 'Platform:  darwin/%s\n' "$ARCH"
printf 'API URL:   %s\n' "$API_URL"
printf 'Worker:    %s\n' "$WORKER_NAME"
printf 'Binary:    %s\n' "${INSTALL_DIR}/${BINARY_NAME}"
printf 'Config:    %s\n' "${INSTALL_DIR}/${CONFIG_FILE}"
printf 'Plist:     %s\n' "$PLIST_PATH"
printf 'Logs:      %s / %s\n' "$OUT_LOG" "$ERR_LOG"

# ---------------------------------------------------------------------------
# Docker runtime check (Linux nodes need a docker socket; macOS runs native)
# ---------------------------------------------------------------------------
docker_present() {
    [ -n "${DOCKER_SOCK:-}" ] && [ -S "$DOCKER_SOCK" ] && return 0
    [ -S /var/run/docker.sock ] && return 0
    [ -S "${HOME_DIR}/.docker/run/docker.sock" ] && return 0
    return 1
}
DOCKER_SOCK="/var/run/docker.sock"
if docker_present; then
    printf 'Docker socket: found (Docker Desktop / OrbStack / Colima compatible)\n'
else
    printf 'Docker socket: NOT found.\n'
    printf '  A macOS worker runs its nodes NATIVELY (Metal/GPU, no docker socket needed).\n'
    printf '  Docker Desktop, OrbStack or Colima are only needed if you also want Linux\n'
    printf '  container nodes from this Mac - install one of those and re-run, or ignore\n'
    printf '  this warning for native-only work.\n'
fi

# ---------------------------------------------------------------------------
# Plist rendering (template lives next to this script)
# ---------------------------------------------------------------------------
render_plist() {
    [ -f "$PLIST_TEMPLATE" ] || { printf 'Error: plist template missing: %s\n' "$PLIST_TEMPLATE"; return 1; }
    local home_esc tags
    home_esc="$(sed_value "$HOME_DIR")"
    tags="$(sed_value "$(xml_escape "${HARDWARE_TAGS:-}")")"
    sed -e "s|__HOME__|${home_esc}|g" -e "s|__HARDWARE_TAGS__|${tags}|g" "$PLIST_TEMPLATE"
}

if [ ! -f "$PLIST_TEMPLATE" ]; then
    printf 'Error: plist template missing: %s\n' "$PLIST_TEMPLATE"
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    plan "uname -s must be Darwin (detected ${OS_RAW}) — refusing to run outside macOS unless --dry-run"
    plan "detect arch: ${ARCH} (darwin/${ARCH})"
    plan "GET ${BINARY_URL} -> ${INSTALL_DIR}/${BINARY_NAME}"
    plan "chmod +x ${INSTALL_DIR}/${BINARY_NAME}"
    plan "xattr -d com.apple.quarantine ${INSTALL_DIR}/${BINARY_NAME} (ignore failure)"
    plan "POST ${API_URL}/v1/workers/enroll/validate-token (token sent, never printed)"
    plan "POST ${API_URL}/v1/workers/enroll/start -> bootstrap_token into ${INSTALL_DIR}/${CONFIG_FILE} (mode 0600)"
    plan "mkdir -p ${INSTALL_DIR} ${LOG_DIR} $(dirname "${PLIST_PATH}")"
    plan "render ${PLIST_TEMPLATE} -> ${PLIST_PATH}"
    plan "launchctl bootout gui/${UID_NUM}/com.example.worker 2>/dev/null || true"
    plan "launchctl bootstrap gui/${UID_NUM} ${PLIST_PATH}"
    plan "launchctl kickstart -k gui/${UID_NUM}/com.example.worker"
    echo ""
    echo "----- rendered plist (to ${PLIST_PATH}) -----"
    render_plist
    echo "---------------------------------------------"
    echo "DRY RUN: nothing was created, downloaded, or registered."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1 — validate the enrollment token (same call as setup-worker.sh)
# ---------------------------------------------------------------------------
printf '==> Checking enrollment token...\n'
VALIDATE_BODY_FILE="$(mktemp)"
(umask 077; cat > "$VALIDATE_BODY_FILE") <<EOF
{"enrollment_token": "$(json_escape "$ENROLLMENT_TOKEN")", "worker_name": "$(json_escape "$WORKER_NAME")"}
EOF
VALIDATE_RESPONSE=$(curl -sf -X POST "${API_URL}/v1/workers/enroll/validate-token" \
    -H "Content-Type: application/json" \
    --data @"$VALIDATE_BODY_FILE" 2>&1) || true
rm -f "$VALIDATE_BODY_FILE"
TOKEN_VALID=$(printf '%s' "${VALIDATE_RESPONSE:-}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('valid', False))" 2>/dev/null || echo "False")
if [ "$TOKEN_VALID" != "True" ]; then
    printf 'Error: enrollment token rejected (unknown worker name or expired token).\n'
    exit 1
fi
printf 'Token accepted\n'

# ---------------------------------------------------------------------------
# Step 2 — Download the darwin binary (public route, same as setup-worker.sh)
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR" "$(dirname "$PLIST_PATH")"
mkdir -p -m 700 "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

if [ -x "${INSTALL_DIR}/${BINARY_NAME}" ]; then
    printf '==> Replacing a running worker binary: unloading first...\n'
    launchctl bootout "$LAUNCH_TARGET" 2>/dev/null || true
fi

TMP_BINARY="$(mktemp -t mod-worker)"
printf '==> Downloading darwin/%s from %s...\n' "$ARCH" "$BINARY_URL"
if ! curl -fL --retry 3 --retry-delay 5 --max-time 300 -o "$TMP_BINARY" "$BINARY_URL"; then
    printf 'Error: download failed (HTTP non-2xx). The API may not publish darwin binaries yet.\n'
    rm -f "$TMP_BINARY"
    exit 1
fi
if [ ! -s "$TMP_BINARY" ]; then
    printf 'Error: download returned an empty body\n'
    rm -f "$TMP_BINARY"
    exit 1
fi
chmod +x "$TMP_BINARY"
mv -f "$TMP_BINARY" "${INSTALL_DIR}/${BINARY_NAME}"
xattr -d com.apple.quarantine "${INSTALL_DIR}/${BINARY_NAME}" 2>/dev/null || true
printf '==> Binary installed: %s\n' "${INSTALL_DIR}/${BINARY_NAME}"

# ---------------------------------------------------------------------------
# Step 3 — Enroll (consume the token; same two-step as setup-worker.sh)
# ---------------------------------------------------------------------------
printf '==> Enrolling worker...\n'
ENROLL_BODY_FILE="$(mktemp)"
ENROLL_REQ_FILE="$(mktemp)"
(umask 077; cat > "$ENROLL_REQ_FILE") <<EOF
{"worker_name": "$(json_escape "$WORKER_NAME")", "hostname": "$(json_escape "$(hostname)")", "enrollment_token": "$(json_escape "$ENROLLMENT_TOKEN")"}
EOF
ENROLL_CODE=000
ENROLL_RESPONSE=""
for ENROLL_TRY in 1 2 3 4 5; do
    ENROLL_CODE=$(curl -s -o "$ENROLL_BODY_FILE" -w "%{http_code}" -X POST "${API_URL}/v1/workers/enroll/start" \
        -H "Content-Type: application/json" \
        --data @"$ENROLL_REQ_FILE" 2>/dev/null) || ENROLL_CODE=000
    ENROLL_RESPONSE=$(cat "$ENROLL_BODY_FILE" 2>/dev/null || true)
    case "$ENROLL_CODE" in
        000|5*) if [ "$ENROLL_TRY" -lt 5 ]; then
                    printf 'Enrollment attempt %s got HTTP %s, retrying...\n' "$ENROLL_TRY" "$ENROLL_CODE"
                    sleep $((ENROLL_TRY * 5))
                fi ;;
        *) break ;;
    esac
done
rm -f "$ENROLL_BODY_FILE" "$ENROLL_REQ_FILE"
case "$ENROLL_CODE" in 2??) ;; *) ENROLL_RESPONSE=""; printf 'Error: enrollment failed (HTTP %s)\n' "$ENROLL_CODE"; exit 1 ;; esac

BOOTSTRAP_TOKEN=$(printf '%s' "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bootstrap_token',''))" 2>/dev/null || echo "")
APPROVAL_CODE=$(printf '%s' "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('approval_code',''))" 2>/dev/null || echo "")
ENROLL_STATUS=$(printf '%s' "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
if [ -z "$BOOTSTRAP_TOKEN" ]; then
    printf 'Error: enrollment returned no bootstrap token (status: %s)\n' "${ENROLL_STATUS:-unknown}"
    exit 1
fi
printf 'Enrolled (status: %s)\n' "${ENROLL_STATUS:-unknown}"
if [ "$ENROLL_STATUS" = "pending" ] && [ -n "$APPROVAL_CODE" ]; then
    printf 'ACTION REQUIRED: approve this worker in Dashboard > Workers (approval code %s)\n' "$APPROVAL_CODE"
fi

# ---------------------------------------------------------------------------
# Step 4 — Write worker.yaml (identical shape to the Linux unit's config)
# ---------------------------------------------------------------------------
(umask 077; cat > "${INSTALL_DIR}/${CONFIG_FILE}") <<EOF
worker_name: "${WORKER_NAME}"
mode: "remote"
api_url: "${API_URL}"
bootstrap_token: "${BOOTSTRAP_TOKEN}"
docker_network: "host"
heartbeat_interval: 15s
container_timeout: 30m
EOF
chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}"
printf 'Config written: %s (0600)\n' "${INSTALL_DIR}/${CONFIG_FILE}"

# ---------------------------------------------------------------------------
# Step 5 — Install the LaunchAgent (no sudo: a LaunchAgent is per-user)
# ---------------------------------------------------------------------------
render_plist > "$PLIST_PATH"
if ! plutil -lint "$PLIST_PATH" >/dev/null 2>&1; then
    printf 'Error: plist at %s failed plutil -lint; refusing to register the job\n' "$PLIST_PATH"
    exit 1
fi
printf 'Plist installed: %s\n' "$PLIST_PATH"

launchctl bootout "$LAUNCH_TARGET" 2>/dev/null || true
if ! launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST_PATH"; then
    printf 'Error: launchctl bootstrap failed — plist is at %s\n' "$PLIST_PATH"
    printf '       Retry: launchctl bootstrap gui/%s %s\n' "$UID_NUM" "$PLIST_PATH"
    exit 1
fi
launchctl enable "$LAUNCH_TARGET" 2>/dev/null || true
launchctl kickstart -k "$LAUNCH_TARGET" 2>/dev/null || true

sleep 3
if launchctl print "$LAUNCH_TARGET" 2>/dev/null | grep -q "state = running"; then
    printf 'Worker is running.\n'
else
    printf 'Warning: job not detected as running — inspect:\n'
    printf '  launchctl print gui/%s/%s\n' "$UID_NUM" "$LABEL"
    printf '  tail -f %s\n' "$ERR_LOG"
fi

printf '\nDone. Logs: %s / %s\n' "$OUT_LOG" "$ERR_LOG"
printf 'Uninstall: ./install-macos.sh --uninstall [--purge]\n'
