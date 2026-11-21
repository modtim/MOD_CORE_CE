#!/usr/bin/env bash
set -euo pipefail

# MOD Worker Install Script (macOS)
#
# This script is served by the API at /workers/agent/install-script (for a
# download token minted with os_name=darwin) — the macOS sibling of
# worker_install.sh (Linux/systemd) and worker_install.ps1 (Windows/SCM),
# using launchd instead of systemd/SCM. A platform admin generates a
# one-time install command from the Dashboard (Workers -> Add Remote Worker,
# macOS tab):
#
#   curl -fsSL 'https://api.example.com/workers/agent/install-script?token=TOKEN' | bash -s -- \
#     --api-url https://api.example.com \
#     --token TOKEN \
#     --bootstrap-token ENROLLMENT_TOKEN
#
# The download token is short-lived (15 min) and single-use, same as the
# Linux/Windows installers.
#
# Install location: root (sudo) vs per-user.
#   - Running as root (sudo, matching the Linux script's `sudo mv .../bin`
#     and the Windows script's Administrator requirement) installs
#     SYSTEM-WIDE to /usr/local/mod-worker/ and registers a LaunchDaemon
#     (/Library/LaunchDaemons) — starts at boot, before any user logs in,
#     survives logout. This is the parity choice with the other two
#     platforms' device-wide autostart and is what's recommended for a
#     dedicated render Mac.
#   - Running as a normal user (no sudo available/desired — e.g. someone
#     contributing their personal Mac's idle capacity without admin rights)
#     falls back to a PER-USER install under
#     "$HOME/Library/Application Support/MODWorker" and a LaunchAgent
#     (~/Library/LaunchAgents) in the `gui/<uid>` domain — starts on login,
#     stops on logout, no admin privileges required.
# Both paths write the same env vars and the same plist label
# (com.example.modworker) so `--uninstall` doesn't need to guess which
# mode installed it (it re-derives the same paths from the current EUID).
#
# SERVICE MODEL (MAC.1.s15, owner decisions R38.1/R38.3): the per-user LaunchAgent
# is the model of record. Metal (GPU) access and GUI/screen access require the
# user's graphical login session, so the job is registered in the `gui/<uid>`
# launchd domain with LimitLoadToSessionType=Aqua — a system LaunchDaemon in the
# `root`/`system` domain has no such session and the worker would lose exactly
# the capabilities a Mac worker is for. The root (sudo) LaunchDaemon path above
# is kept only as the device-wide parity option for a dedicated render Mac.
#
# The plist itself is not inline any more — it is the template file
# templates/macos/com.example.modworker.plist.tmpl (next to this script in
# the repo / inlined into this script by the API when it is served piped from
# curl), rendered placeholder-by-placeholder into $PLIST_PATH. That ONE template
# carries both launchd shapes: the installer keeps the AGENT-ONLY KEYS block
# (LimitLoadToSessionType=Aqua) for the per-user LaunchAgent and swaps in the
# DAEMON-ONLY KEYS block (UserName) for the root LaunchDaemon, so there is a
# single source of truth for the service model either way.
#
# SIGNING / NOTARIZATION: this is an unsigned development build. Gatekeeper
# will refuse to run it until the quarantine attribute is cleared — this
# script does that with `xattr -d com.apple.quarantine` after download. A
# properly codesigned + notarized release (Apple Developer account) is
# owner-gated and NOT part of this script.
#
# NON-ROOT (GSEC Q21.5): the per-user (LaunchAgent) path is already
# user-scoped — install dir, logs and env file all live under $HOME and the
# job runs as the launching user, so it is non-root by construction. The
# system-wide (LaunchDaemon) path runs as root by design (boot-time,
# pre-login) and is documented, not changed, in this release; the env file
# is 0600 either way.

SERVICE_LABEL="com.example.modworker"
BINARY_NAME="mod-worker"
OS="darwin"

# Detect arch
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  ARCH="amd64" ;;
  arm64)   ARCH="arm64" ;;
  *)       echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

# Parse arguments — before OS check so --dry-run can bypass it
MODE="remote"
API_URL=""
DOWNLOAD_TOKEN=""
BOOTSTRAP_TOKEN=""
VAULT_ADDR=""
VAULT_ROLE_ID=""
VAULT_SECRET_ID=""
REDIS_URL=""
WORKER_NAME=$(hostname)
UNINSTALL=0
DRY_RUN=0
PURGE=0
HOST_EXEC=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)            MODE="$2"; shift 2 ;;
    --api-url)         API_URL="$2"; shift 2 ;;
    --token|--enrollment-token) DOWNLOAD_TOKEN="$2"; shift 2 ;;
    --token=*|--enrollment-token=*) DOWNLOAD_TOKEN="${1#*=}"; shift ;;
    --bootstrap-token) BOOTSTRAP_TOKEN="$2"; shift 2 ;;
    --vault-addr)      VAULT_ADDR="$2"; shift 2 ;;
    --vault-role-id)   VAULT_ROLE_ID="$2"; shift 2 ;;
    --vault-secret-id) VAULT_SECRET_ID="$2"; shift 2 ;;
    --redis-url)       REDIS_URL="$2"; shift 2 ;;
    --worker-name)     WORKER_NAME="$2"; shift 2 ;;
    --uninstall)        UNINSTALL=1; shift ;;
    --purge)            PURGE=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --host-exec)        HOST_EXEC=1; shift ;;
    -h|--help)
      echo "Usage: $0 [options]"
      echo "  --api-url URL       MOD API base URL (bare, no /v1)"
      echo "  --token TOKEN       download / enrollment token from Dashboard"
      echo "  --bootstrap-token   bootstrap token (for enrolled worker)"
      echo "  --vault-addr URL    Vault address"
      echo "  --vault-role-id ID  Vault AppRole role ID"
      echo "  --vault-secret-id ID Vault AppRole secret ID"
      echo "  --redis-url URL     Redis connection string"
      echo "  --worker-name NAME  worker name (default: hostname)"
      echo "  --mode MODE         worker mode (default: remote)"
      echo "  --uninstall         bootout the job and remove the plist"
      echo "  --purge             with --uninstall: also remove binary + config + logs"
      echo "  --dry-run           print full plan, touch nothing, exit 0"
      echo "  --host-exec         opt in to the host-exec helper (root install only; fails closed otherwise)"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [ "$PURGE" = "1" ] && [ "$UNINSTALL" != "1" ]; then
    echo "Error: --purge is only valid with --uninstall"
    exit 1
fi

OS_RAW="$(uname -s)"
if [ "$OS_RAW" != "Darwin" ] && [ "$DRY_RUN" != 1 ]; then
    echo "Error: this script is for macOS only (detected ${OS_RAW}). Use worker_install.sh on Linux."
    echo "       Pass --dry-run to preview what this script would do."
    exit 1
fi

echo "==> Detected ${OS}/${ARCH}"

# Root vs per-user install paths. Resolved once, up front, so both the
# install and --uninstall code paths agree.
if [ "$(id -u)" = "0" ]; then
    IS_ROOT=1
    INSTALL_DIR="/usr/local/mod-worker"
    LOG_DIR="${INSTALL_DIR}/logs"
    PLIST_PATH="/Library/LaunchDaemons/${SERVICE_LABEL}.plist"
    LAUNCHCTL_DOMAIN="system"
    LAUNCHCTL_TARGET="system/${SERVICE_LABEL}"
else
    IS_ROOT=0
    INSTALL_DIR="${HOME}/Library/Application Support/MODWorker"
    LOG_DIR="${HOME}/Library/Logs/MODWorker"
    PLIST_PATH="${HOME}/Library/LaunchAgents/${SERVICE_LABEL}.plist"
    UID_NUM=$(id -u)
    LAUNCHCTL_DOMAIN="gui/${UID_NUM}"
    LAUNCHCTL_TARGET="gui/${UID_NUM}/${SERVICE_LABEL}"
fi
BINARY_PATH="${INSTALL_DIR}/${BINARY_NAME}"
ENV_FILE="${INSTALL_DIR}/worker.env"

unload_service() {
    # `launchctl bootout` is the modern (10.11+) equivalent of `unload -w`;
    # tolerate "no such process" if it isn't loaded.
    launchctl bootout "$LAUNCHCTL_TARGET" >/dev/null 2>&1 || true
}

if [ "$UNINSTALL" = "1" ]; then
    if [ "$PURGE" = "1" ] && [ "$DRY_RUN" = "1" ]; then
        echo "DRY RUN: would uninstall ${SERVICE_LABEL}"
        echo "PLAN: launchctl bootout ${LAUNCHCTL_TARGET}"
        echo "PLAN: rm -f ${PLIST_PATH}"
        echo "PLAN: rm -rf -- ${INSTALL_DIR}"
        echo "PLAN: rm -rf -- ${LOG_DIR}"
        echo "DRY RUN: nothing was removed."
        exit 0
    fi
    echo "==> Uninstalling ${SERVICE_LABEL}"
    unload_service
    if [ -f "$PLIST_PATH" ]; then
        rm -f "$PLIST_PATH"
    fi
    echo "==> ${SERVICE_LABEL} unloaded and plist removed"
    if [ "$PURGE" = "1" ]; then
        echo "==> Purging install directory and logs"
        if [ -n "${INSTALL_DIR:-}" ] && [ "${INSTALL_DIR:-}" != "/" ]; then
            case "${INSTALL_DIR:-}" in
                *MODWorker*|*mod-worker*) rm -rf -- "${INSTALL_DIR}"; echo "    Removed ${INSTALL_DIR}";;
                *)                        echo "    Skipping ${INSTALL_DIR}: does not match a known MOD worker path";;
            esac
        fi
        if [ -n "${LOG_DIR:-}" ] && [ "${LOG_DIR:-}" != "/" ]; then
            case "${LOG_DIR:-}" in
                *MOD*|*mod-worker*) rm -rf -- "${LOG_DIR}"; echo "    Removed ${LOG_DIR}";;
                *)                  echo "    Skipping ${LOG_DIR}: does not match a known MOD worker path";;
            esac
        fi
    else
        echo "    State dir kept: ${INSTALL_DIR} (binary, config, logs) — remove manually if no longer needed"
    fi
    exit 0
fi

if [ -z "$API_URL" ]; then
    echo "Error: --api-url is required"
    exit 1
fi
if [ -z "$DOWNLOAD_TOKEN" ]; then
    echo "Error: --token is required"
    echo "  Generate one from the Dashboard (Workers → Add Remote Worker → macOS tab)"
    echo "  or via API: POST /workers/agent/download-token?os_name=darwin (platform admin)"
    exit 1
fi

if [ "$HOST_EXEC" = "1" ] && [ "$IS_ROOT" != "1" ]; then
    echo "FAIL: host-exec needs a system-wide (root) install"
    echo "reason_code=not_root"
    echo "  Re-run with sudo for a LaunchDaemon install."
    exit 1
fi

if [ "$IS_ROOT" = "1" ]; then
    echo "==> Installing system-wide (root) to ${INSTALL_DIR}"
else
    echo "==> Installing for current user (no root) to ${INSTALL_DIR}"
    echo "    Run with sudo for a system-wide LaunchDaemon install instead."
fi

# Docker runtime hint — macOS worker runs native nodes without a docker socket;
# Docker Desktop / OrbStack / Colima are only needed for Linux-container nodes.
for _sock in /var/run/docker.sock "${HOME:-}/.docker/run/docker.sock" "${HOME:-}/.orbstack/run/docker.sock" "${HOME:-}/.colima/default/docker.sock"; do
    if [ -S "${_sock:-}" ]; then
        echo "==> Docker socket found at ${_sock}"
        break
    fi
done

# Plist template path and rendering helpers — needed early for --dry-run plist display
PLIST_TEMPLATE_FILE="$(dirname "${BASH_SOURCE[0]:-$0}")/templates/macos/com.example.modworker.plist.tmpl"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

get_plist_template() {
    if [ -f "$PLIST_TEMPLATE_FILE" ]; then
        cat "$PLIST_TEMPLATE_FILE"
    else
        cat <<'MOD_PLIST_TMPL'
__MOD_PLIST_TEMPLATE__
MOD_PLIST_TMPL
    fi
}

render_plist() {
    local target="${1:-$PLIST_PATH}"
    local tmpl line skip=0
    tmpl="$(get_plist_template)"
    : > "$target"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *'<!-- BEGIN AGENT-ONLY KEYS'*)
                if [ "$IS_ROOT" = "1" ]; then skip=1; else skip=0; fi
                continue ;;
            *'<!-- END AGENT-ONLY KEYS'*)
                skip=0
                continue ;;
            *'<!-- BEGIN DAEMON-ONLY KEYS'*)
                if [ "$IS_ROOT" = "1" ]; then skip=0; else skip=1; fi
                continue ;;
            *'<!-- END DAEMON-ONLY KEYS'*)
                skip=0
                continue ;;
        esac
        if [ "$skip" = "1" ]; then continue; fi
        line="${line//__MOD_LABEL__/$(xml_escape "$SERVICE_LABEL")}"
        line="${line//__MOD_BINARY__/$(xml_escape "$BINARY_PATH")}"
        line="${line//__MOD_ENVFILE__/$(xml_escape "$ENV_FILE")}"
        line="${line//__MOD_OUTLOG__/$(xml_escape "$LOG_DIR")}"
        line="${line//__MOD_ERRLOG__/$(xml_escape "$LOG_DIR")}"
        printf '%s\n' "$line" >> "$target"
    done <<EOF
$tmpl
EOF
}

if [ "$DRY_RUN" = "1" ]; then
    echo "DRY RUN: plan follows (no files will be written or downloaded)"
    echo "PLAN: Detected ${OS}/${ARCH}"
    echo "PLAN: Download URL: ${API_URL}/workers/agent/download?token=<token provided>&os_name=${OS}&arch=${ARCH}"
    echo "PLAN: Install dir:  ${INSTALL_DIR}"
    echo "PLAN: Binary path: ${BINARY_PATH}"
    echo "PLAN: Plist path:  ${PLIST_PATH}"
    echo "PLAN: Env file:    ${ENV_FILE}"
    echo "PLAN: Log dir:     ${LOG_DIR}"
    echo "PLAN: launchctl domain: ${LAUNCHCTL_DOMAIN}"
    echo "PLAN: launchctl target: ${LAUNCHCTL_TARGET}"
    if [ "$HOST_EXEC" = "1" ] && [ "$IS_ROOT" = "1" ]; then
        echo "PLAN: host-exec helper LaunchDaemon: /Library/LaunchDaemons/com.example.modhostexec.plist (system domain, root only)"
        echo "PLAN: host-exec helper socket dir: /run/mod-host-exec (0750, group-restricted)"
        echo "PLAN: host-exec helper binary: not shipped for darwin yet (reason_code=helper_missing)"
    fi
    echo ""
    echo "----- rendered plist (preview only; would be written to ${PLIST_PATH}) -----"
    if [ -f "$PLIST_TEMPLATE_FILE" ]; then
        DRY_RUN_PLIST_PREVIEW="$(mktemp -t mod-worker-dryrun-plist)"
        if render_plist "$DRY_RUN_PLIST_PREVIEW" 2>/dev/null; then
            cat "$DRY_RUN_PLIST_PREVIEW"
        else
            echo "(could not render plist preview)"
        fi
        rm -f "$DRY_RUN_PLIST_PREVIEW"
    else
        echo "(template file not available at ${PLIST_TEMPLATE_FILE}; sentinel not inlined — this is a dry-run from the repo, not a served script)"
    fi
    echo "---------------------------------------------"
    echo "DRY RUN: nothing was created, downloaded, or registered."
    exit 0
fi

mkdir -p "$INSTALL_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_PATH")"

# Download binary using the short-lived token — same two-step token flow as
# worker_install.sh / worker_install.ps1 (POST /agent/download-token mints
# Token, then GET /agent/download?token=... redeems it once). arch is
# passed explicitly so a token minted with a default arch still fetches the
# binary that actually matches this machine (Intel vs Apple Silicon).
TMP_BINARY="$(mktemp -t mod-worker).download"
echo "==> Downloading worker agent for ${OS}/${ARCH}..."
DOWNLOAD_URL="${API_URL}/workers/agent/download?token=<token provided>&os_name=${OS}&arch=${ARCH}"
DOWNLOAD_TRY=0
MAX_DOWNLOAD_ATTEMPTS=5
while [ "$DOWNLOAD_TRY" -lt "$MAX_DOWNLOAD_ATTEMPTS" ]; do
    DOWNLOAD_TRY=$((DOWNLOAD_TRY + 1))
    HTTP_CODE=$(curl -sS -L -w "%{http_code}" "${API_URL}/workers/agent/download?token=${DOWNLOAD_TOKEN}&os_name=${OS}&arch=${ARCH}" \
        -o "$TMP_BINARY" 2>/dev/null) || HTTP_CODE=000
    case "$HTTP_CODE" in
        200) break ;;
        000|5*)
            if [ "$DOWNLOAD_TRY" -lt "$MAX_DOWNLOAD_ATTEMPTS" ]; then
                echo "Download attempt ${DOWNLOAD_TRY}/${MAX_DOWNLOAD_ATTEMPTS} failed (HTTP ${HTTP_CODE}), retrying in $((DOWNLOAD_TRY * 2))s..."
                sleep $((DOWNLOAD_TRY * 2))
            else
                echo "Error: Download failed after ${MAX_DOWNLOAD_ATTEMPTS} attempts (HTTP ${HTTP_CODE})"
                cat "$TMP_BINARY" 2>/dev/null
                rm -f "$TMP_BINARY"
                exit 1
            fi ;;
        *)
            echo "Error: Download failed (HTTP ${HTTP_CODE})"
            cat "$TMP_BINARY" 2>/dev/null
            rm -f "$TMP_BINARY"
            exit 1 ;;
    esac
done

if [ ! -s "$TMP_BINARY" ]; then
    echo "Error: Download failed: empty response body"
    rm -f "$TMP_BINARY"
    exit 1
fi

chmod +x "$TMP_BINARY"

# Idempotent re-run: unload before replacing a binary the running process
# has open.
unload_service
mv -f "$TMP_BINARY" "$BINARY_PATH"

# Clear the quarantine attribute Gatekeeper set on download (com.apple.quarantine,
# added by curl/LaunchServices to anything that touched the network). Without
# this, first execution pops "cannot be opened because the developer cannot
# be verified" / "is damaged and can't be opened" and launchd's job silently
# fails to spawn. This is a stand-in for real codesigning + notarization
# (owner-gated, Apple Developer account) — see the file header note.
xattr -d com.apple.quarantine "$BINARY_PATH" 2>/dev/null || true

echo "==> Installed to ${BINARY_PATH}"

# Write env file (informational / for anything that wants to source it by
# hand — the values that actually reach the running process are the plist's
# EnvironmentVariables dict below, the launchd-native equivalent of
# systemd's EnvironmentFile= / the Windows service's per-service registry
# Environment value).
cat > "$ENV_FILE" <<EOF
WORKER_NAME=${WORKER_NAME}
WORKER_MODE=${MODE}
API_URL=${API_URL}
BOOTSTRAP_TOKEN=${BOOTSTRAP_TOKEN}
VAULT_ADDR=${VAULT_ADDR}
VAULT_APPROLE_ROLE_ID=${VAULT_ROLE_ID}
VAULT_APPROLE_SECRET_ID=${VAULT_SECRET_ID}
REDIS_URL=${REDIS_URL}
EOF
chmod 600 "$ENV_FILE"

render_plist
if [ ! -s "$PLIST_PATH" ]; then
    echo "Error: could not render the launchd plist template (${PLIST_TEMPLATE_FILE})"
    exit 1
fi
if grep -q '__MOD_' "$PLIST_PATH"; then
    # A placeholder survived rendering — launchd would load a broken job.
    echo "Error: plist template not fully rendered; refusing to register the job"
    exit 1
fi

if [ "$IS_ROOT" = "1" ]; then
    chown root:wheel "$PLIST_PATH"
    chmod 644 "$PLIST_PATH"
else
    chmod 644 "$PLIST_PATH"
fi

echo "==> Registering launchd job (${LAUNCHCTL_TARGET})"
# bootstrap (10.11+ replacement for `load -w`) is idempotent-safe here: any
# previously loaded copy of the job was booted out above before the binary was
# replaced, so this never hits "launchd already has a job named ...".
if ! launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$PLIST_PATH"; then
    echo "Error: launchctl bootstrap failed — the plist was written at ${PLIST_PATH}"
    echo "       but the job is not registered. Re-run this script, or register by hand:"
    echo "       launchctl bootstrap ${LAUNCHCTL_DOMAIN} ${PLIST_PATH}"
    exit 1
fi
launchctl enable "$LAUNCHCTL_TARGET" >/dev/null 2>&1 || true
# kickstart -k: start the (re)installed job immediately — same behaviour as
# before the template move; KeepAlive SuccessfulExit=false is the declarative
# half (restart on crash, stop on a clean exit).
launchctl kickstart -k "$LAUNCHCTL_TARGET"

echo ""
echo "==> ${SERVICE_LABEL} installed and started"
echo "    Check status: launchctl print ${LAUNCHCTL_TARGET}"
echo "    View logs:    tail -f ${LOG_DIR}/worker.out.log ${LOG_DIR}/worker.err.log"
echo "    Uninstall:    curl -fsSL '<install-script-url>' | bash -s -- --uninstall"
echo "                  (or re-run this script locally with --uninstall)"
