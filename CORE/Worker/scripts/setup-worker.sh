#!/usr/bin/env bash
# =============================================================================
# MOD Remote Worker — Full Deploy Script
# =============================================================================
# One-shot script to take a bare Linux machine and turn it into a running
# remote worker: prompts for config, installs Docker, detects/installs GPU
# drivers (NVIDIA, AMD, Intel), downloads the worker binary, writes config,
# installs a systemd service, and starts it.
#
# Usage:
#   curl -sSL https://your-api/workers/setup-script | sudo bash
#   OR
#   sudo ./setup-worker.sh
#
# Non-interactive (all env vars pre-set):
#   API_URL=https://... WORKER_NAME=w1 BOOTSTRAP_TOKEN=tok sudo -E ./setup-worker.sh
# =============================================================================

set -euo pipefail

# Piped runs (`curl ... | sudo bash`) have no tty, so any command that prompts on
# stdin hangs forever. Force package managers non-interactive for the whole script
# (this also covers the tailscale installer's own apt calls).
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/mod-worker"
BINARY_NAME="mod-worker"
CONFIG_FILE="worker.yaml"
SERVICE_NAME="mod-worker"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "  ${GREEN}[OK]${NC}  $1"; }
warn()    { echo -e "  ${YELLOW}[!!]${NC}  $1"; }
note()    { echo -e "  ${YELLOW}[--]${NC}  $1"; }
error()   { echo -e "  ${RED}[ERR]${NC} $1"; }
header()  { echo -e "\n${CYAN}${BOLD}── $1${NC}"; }

# ---------------------------------------------------------------------------
# Worker-name derivation
# ---------------------------------------------------------------------------
# A usable worker name comes from the box's hostname; generic/empty hostnames
# (fresh cloud images default to `localhost`, `localhost.localdomain`, plain
# `ubuntu`/`debian`/... or nothing at all) collide across every box built the
# same way — which is how a second install collided with an old enrollment row
# and took the name out of the picture. When the hostname is one of those,
# fall back to the box's /etc/machine-id (first 8 hex chars, stable per install)
# so the name is unique to THIS machine.
_is_generic_hostname() {
    case "$1" in
        ""|localhost|localhost.localdomain|localhost6-*|\
        ubuntu|ubuntu-*|debian|debian-*|fedora|fedora-*|centos|centos-*|\
        rhel|rhel-*|rocky|rocky-*|almalinux|almalinux-*|arch|arch-*|\
        pop-os|pop-os-*|kali|kali-*|raspberrypi|raspberrypi-*|\
        server-|server|machine|machine-*|node-|node)
            return 0 ;;
    esac
    return 1
}

# derive_worker_name HOSTNAME  ->  worker-<something-unique>
derive_worker_name() {
    local host="$1" mid midfile="${MOD_MACHINE_ID_FILE:-/etc/machine-id}"
    if _is_generic_hostname "$host"; then
        mid=""
        [ -r "$midfile" ] && mid="$(tr -dc 'a-f0-9' < "$midfile" 2>/dev/null | head -c 8)"
        if [ -n "$mid" ]; then
            echo "worker-${mid}"
            return 0
        fi
        # No machine-id (shouldn't happen on systemd boxes): keep the hostname
        # so the name is at least as informative as before.
        [ -n "$host" ] || host="unknown"
    fi
    echo "worker-${host}"
}

# is_reserved_worker_name NAME  ->  0 when NAME is the manager-node (embedded)
# worker's identity. `mod-worker-embedded` (and the legacy CE alias
# `mod-worker-ce`) are seeded with the embedded worker's own bootstrap token, so
# a second machine enrolled under one of them would rotate that token and knock
# the manager's worker offline. A machine added through this script is always a
# normal remote worker.
is_reserved_worker_name() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        mod-worker-embedded|mod-worker-ce) return 0 ;;
    esac
    return 1
}

# login_server_host URL ->  bare host (no scheme, no port, no path). The local
# CA is always served on plain HTTP port 80, so the port is deliberately dropped.
login_server_host() {
    echo "$1" | sed -E 's#^https?://##; s#[:/].*##'
}

# probe_site_reachable URL  ->  0 if a TCP connect (and the plain-HTTP CA path)
# succeeds within ~4s, else 1. Used to decide whether a remote box can actually
# reach the manager's PRIVATE site anchor before we commit to it.
probe_site_reachable() {
    local url="$1" host
    [ -n "$url" ] || return 1
    host="$(login_server_host "$url")"
    [ -n "$host" ] || return 1
    if curl -sk -m 4 -o /dev/null "$url"; then
        return 0
    fi
    # The control plane may answer only on its TLS port; also try the CA path.
    if curl -s -m 4 -o /dev/null "http://${host}/mod-local-ca.crt"; then
        return 0
    fi
    return 1
}
ask()     {
    local prompt="$1" default="$2" var="$3"
    if [ -n "${!var:-}" ]; then return; fi
    if [ -t 0 ]; then
        if [ -n "$default" ]; then
            read -rp "  $prompt [$default]: " input
            eval "$var=\"\${input:-$default}\""
        else
            read -rp "  $prompt: " input
            eval "$var=\"\$input\""
        fi
    else
        if [ -n "$default" ]; then
            eval "$var=\"$default\""
        fi
    fi
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
}

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_LIKE="${ID_LIKE:-$DISTRO_ID}"
        DISTRO_VERSION="${VERSION_ID:-}"
    else
        DISTRO_ID="unknown"
        DISTRO_LIKE="unknown"
        DISTRO_VERSION=""
    fi
}

pkg_install() {
    if command -v apt-get &>/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf &>/dev/null; then
        dnf install -y -q "$@"
    elif command -v yum &>/dev/null; then
        yum install -y -q "$@"
    elif command -v zypper &>/dev/null; then
        zypper install -y "$@"
    else
        error "No supported package manager found (apt/dnf/yum/zypper)"
        return 1
    fi
}

pkg_update() {
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
    elif command -v dnf &>/dev/null; then
        dnf makecache -q
    elif command -v yum &>/dev/null; then
        yum makecache -q
    fi
}

# ---------------------------------------------------------------------------
# mDNS resolution — resolve the manager by its STABLE name (mod-manager.local)
# ---------------------------------------------------------------------------
# The site login server is now addressed by a stable mDNS name
# (mod-manager.local) instead of the manager's sliding DHCP IP, so a manager IP
# change on a flat switch needs NO re-enrollment. For that this box must resolve
# *.local link-locally, which means wiring an mDNS resolver into NSS:
#   apt/deb : avahi-daemon (responder/resolver) + libnss-mdns (the NSS plugin
#             that makes getaddrinfo answer *.local), with an `mdns4` entry on
#             the `hosts:` line of /etc/nsswitch.conf.
#   dnf/yum : avahi + nss-mdns (its postinst wires nsswitch).
# Complementary: when systemd-resolved is the active resolver, its own
# MulticastDNS=yes is also turned on so *.local still works where NSS routes
# through resolved.
#
# Idempotent: re-running installs nothing already present and leaves an
# already-correct nsswitch untouched. Best-effort — a failure here only degrades
# to "must dial an IP", it never aborts enrollment.
#
# Offline: the .debs are pre-downloaded into $MDNS_DEB_CACHE so an air-gapped
# re-run (or a box that loses its network after first setup) can reinstall from
# the cache with `dpkg -i` when the online path is unavailable.
MDNS_DEB_CACHE="/var/cache/mod-worker/mdns-debs"

# 0 if the hosts: line already carries an mdns entry (mdns4 / mdns / *_minimal).
_nsswitch_has_mdns() {
    [ -f /etc/nsswitch.conf ] || return 1
    grep -E '^hosts:' /etc/nsswitch.conf | grep -qE 'mdns(4)?(_minimal)?'
}

# Ensure the hosts: line carries an mdns4 entry. libnss-mdns's postinst normally
# does this; we only add it when it is still missing (e.g. a minimal image),
# placed right after `files` so a local/static name still wins and *.local is
# answered link-locally before falling through to unicast dns.
_ensure_nsswitch_mdns() {
    [ -f /etc/nsswitch.conf ] || return 0
    _nsswitch_has_mdns && return 0
    if grep -qE '^hosts:.*\bfiles\b' /etc/nsswitch.conf; then
        sed -i -E 's/^(hosts:[[:space:]]*[^#]*\bfiles\b)/\1 mdns4_minimal [NOTFOUND=return]/' /etc/nsswitch.conf
    elif grep -qE '^hosts:' /etc/nsswitch.conf; then
        sed -i -E 's/^(hosts:[[:space:]]*)/\1mdns4_minimal [NOTFOUND=return] /' /etc/nsswitch.conf
    else
        echo 'hosts: files mdns4_minimal [NOTFOUND=return] dns' >> /etc/nsswitch.conf
    fi
}

ensure_mdns() {
    header "  mDNS — resolve mod-manager.local"
    if command -v apt-get &>/dev/null; then
        mkdir -p "$MDNS_DEB_CACHE"
        _ensure_apt_healthy
        # Pre-cache the .debs for offline reuse (best-effort), then install.
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            --download-only -o Dir::Cache::archives="$MDNS_DEB_CACHE" \
            avahi-daemon libnss-mdns >/dev/null 2>&1 || true
        if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq avahi-daemon libnss-mdns >/dev/null 2>&1; then
            warn "online mDNS install failed — trying pre-cached .debs"
            dpkg -i "$MDNS_DEB_CACHE"/*.deb >/dev/null 2>&1 \
                || warn "mDNS packages unavailable (online + cache) — mod-manager.local may not resolve"
        fi
    elif command -v dnf &>/dev/null; then
        dnf install -y -q avahi nss-mdns >/dev/null 2>&1 \
            || warn "mDNS packages unavailable — mod-manager.local may not resolve"
    elif command -v yum &>/dev/null; then
        yum install -y -q avahi nss-mdns >/dev/null 2>&1 \
            || warn "mDNS packages unavailable — mod-manager.local may not resolve"
    elif command -v zypper &>/dev/null; then
        zypper install -y avahi nss-mdns >/dev/null 2>&1 \
            || warn "mDNS packages unavailable — mod-manager.local may not resolve"
    fi

    _ensure_nsswitch_mdns

    # Bring up the mDNS responder/resolver. Unit name varies by packaging.
    systemctl enable --now avahi-daemon.service 2>/dev/null \
        || systemctl enable --now avahi-daemon.socket 2>/dev/null || true

    # Complementary path: if systemd-resolved is the active resolver, enable its
    # own multicast DNS so .local works even where NSS routes through resolved.
    if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/mod-mdns.conf << 'RESOLVED'
[Resolve]
MulticastDNS=yes
RESOLVED
        systemctl restart systemd-resolved 2>/dev/null || true
    fi

    if _nsswitch_has_mdns; then
        info "mDNS enabled (avahi + nss-mdns; *.local resolves via NSS)"
    else
        warn "mDNS NSS entry not confirmed — mod-manager.local resolution may be unavailable"
    fi
}

# ---------------------------------------------------------------------------
# Intel GPU apt repo — defensive add
# ---------------------------------------------------------------------------
# Intel publishes its GPU repo only for specific Ubuntu codenames. On a distro
# whose codename Intel has NOT published (e.g. 26.04 "resolute") the repo's
# InRelease returns 403/404 and — once written — POISONS every later
# `apt-get update`, including the one inside tailscale's installer, which would
# break the CRITICAL mesh anchor over an OPTIONAL GPU extra. So we PROBE the
# repo before trusting it, fall back to the newest known-good codename, or skip
# it entirely — and never leave an unusable Intel repo file on disk.
INTEL_GPU_KNOWN_GOOD_CODENAME="noble"

# Remove any Intel GPU apt source files so a stale/broken one can't poison apt.
_remove_intel_gpu_repos() {
    rm -f /etc/apt/sources.list.d/intel-gpu*.list \
          /etc/apt/sources.list.d/intel.gpu.*.list 2>/dev/null || true
}

# Return 0 if Intel's GPU repo publishes an InRelease for codename $1.
_intel_repo_available() {
    local codename="$1"
    [ -n "$codename" ] || return 1
    curl -fsI --max-time 15 \
        "https://repositories.intel.com/gpu/ubuntu/dists/${codename}/InRelease" \
        >/dev/null 2>&1
}

# Write a USABLE Intel GPU apt source, or none at all. Probes the requested
# codename ($1, usually the running distro's); on a miss falls back to the
# known-good codename; if neither is reachable it removes any Intel repo file
# and returns 1 so the caller skips the Intel install. The source file is named
# by the codename actually used (intel-gpu-<codename>.list) so its name always
# matches its contents. Also installs the signing keyring.
_add_intel_gpu_repo() {
    local want="$1" use=""
    mkdir -p /etc/apt/keyrings
    if _intel_repo_available "$want"; then
        use="$want"
    elif _intel_repo_available "$INTEL_GPU_KNOWN_GOOD_CODENAME"; then
        warn "Intel GPU repo has no '${want}' release — falling back to '${INTEL_GPU_KNOWN_GOOD_CODENAME}'"
        use="$INTEL_GPU_KNOWN_GOOD_CODENAME"
    else
        warn "Intel GPU repo unavailable for '${want}' (and fallback '${INTEL_GPU_KNOWN_GOOD_CODENAME}') — skipping Intel GPU repo"
        _remove_intel_gpu_repos
        return 1
    fi
    # Start clean so a differently-named stale file can't linger alongside ours.
    _remove_intel_gpu_repos
    curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --batch --yes --dearmor -o /etc/apt/keyrings/intel-graphics.gpg 2>/dev/null || true
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu ${use} unified" \
        > "/etc/apt/sources.list.d/intel-gpu-${use}.list"
    return 0
}

# Ensure `apt-get update` succeeds; if it fails, drop the optional Intel GPU
# repo (the usual poisoner) and retry so later critical steps — above all the
# tailscale/mesh install — get a clean apt state. Best-effort; never aborts.
_ensure_apt_healthy() {
    command -v apt-get &>/dev/null || return 0
    if apt-get update -qq >/dev/null 2>&1; then
        return 0
    fi
    warn "apt-get update failing — removing optional Intel GPU repo and retrying"
    _remove_intel_gpu_repos
    apt-get update -qq >/dev/null 2>&1 \
        || warn "apt-get update still failing — continuing with cached indexes"
}

# ---------------------------------------------------------------------------
# CLI arguments (fallback for env vars)
# ---------------------------------------------------------------------------
# Config normally arrives via env vars (ENROLLMENT_TOKEN, API_URL, WORKER_NAME).
# CLI flags are a fallback for hosts where env vars can't be passed to a piped
# `sudo bash` — e.g.  ... | sudo bash -s -- --enrollment-token X --api-url Y
# Any value set here pre-fills the corresponding var so `ask` won't re-prompt.
while [ $# -gt 0 ]; do
    case "$1" in
        --enrollment-token|--token)
            ENROLLMENT_TOKEN="${2:-}"; shift 2 ;;
        --enrollment-token=*|--token=*)
            ENROLLMENT_TOKEN="${1#*=}"; shift ;;
        --api-url)
            API_URL="${2:-}"; shift 2 ;;
        --api-url=*)
            API_URL="${1#*=}"; shift ;;
        --worker-name)
            WORKER_NAME="${2:-}"; shift 2 ;;
        --worker-name=*)
            WORKER_NAME="${1#*=}"; shift ;;
        *)
            shift ;;
    esac
done

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}=====================================================${NC}"
echo -e "${CYAN}${BOLD}       MOD Remote Worker — Full Deploy${NC}"
echo -e "${CYAN}${BOLD}=====================================================${NC}"
echo ""

require_root
detect_distro
info "Distro: ${DISTRO_ID} ${DISTRO_VERSION} (like: ${DISTRO_LIKE})"

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    arm64)   ARCH="arm64" ;;
esac
info "Platform: ${OS}/${ARCH}"

# ============================================================================
# Step 1 — Collect configuration
# ============================================================================
header "Step 1/8 — Configuration"

ask "API URL (e.g. https://api.yourcompany.com)" "" API_URL
API_URL="${API_URL%/}"
if [ -z "${API_URL:-}" ]; then
    error "API URL is required"
    exit 1
fi
# API_URL is kept BARE (no /v1) deliberately: it is written verbatim into the
# worker's own config below (Step 8, "api_url:") and read back as cfg.APIURL,
# which heartbeat.go's sendHeartbeat() unconditionally suffixes with
# "/v1/workers/heartbeat" (no idempotent guard) — baking /v1 in here would
# double it up once the worker is running. Every API call *this script*
# makes therefore adds /v1 itself at the call site instead.

# MOD_WORKER_NAME (and --worker-name) are explicit overrides; otherwise derive
# one that is unique to THIS box even when its hostname is a generic default
# (cloud images commonly ship as "localhost", which collides across installs).
# MOD_WORKER_NAME (and --worker-name) are explicit overrides. When neither is
# set, derive a default that is unique to THIS box even when its hostname is a
# generic default (cloud images commonly ship as "localhost", which collides
# across identical installs), but still let the operator confirm/override it.
WORKER_NAME="${MOD_WORKER_NAME:-${WORKER_NAME:-}}"
if [ -n "$WORKER_NAME" ]; then
    info "Worker name: ${WORKER_NAME}"
else
    ask "Worker name" "$(derive_worker_name "$(hostname)")" WORKER_NAME
fi

if is_reserved_worker_name "$WORKER_NAME"; then
    error "Worker name '${WORKER_NAME}' is reserved for the manager's built-in worker."
    error "Use a different name, e.g. $(derive_worker_name "$(hostname)") (set WORKER_NAME=... or --worker-name)."
    exit 1
fi

ask "Enrollment token (from Dashboard > Workers > Add Worker)" "" ENROLLMENT_TOKEN

if [ -z "${ENROLLMENT_TOKEN:-}" ]; then
    error "Enrollment token is required (get one from Dashboard > Workers > Add Worker)"
    exit 1
fi

echo ""
info "API URL:     $API_URL"
info "Worker name: $WORKER_NAME"
info "Token:       (set)"

# ============================================================================
# Step 2 — Validate token (check only — don't consume, fail fast)
# ============================================================================
header "Step 2/8 — Token validation"

info "Checking enrollment token..."
VALIDATE_RESPONSE=$(curl -sf --max-time 15 -X POST "${API_URL}/v1/workers/enroll/validate-token" \
    -H "Content-Type: application/json" \
    -d "{\"enrollment_token\": \"${ENROLLMENT_TOKEN}\", \"worker_name\": \"${WORKER_NAME}\"}" 2>&1) || CURL_RC=$?
CURL_RC=${CURL_RC:-0}
if [ "$CURL_RC" -ne 0 ]; then
    error "Cannot reach ${API_URL} (curl exit ${CURL_RC}). Check the address, that the API port is open from this machine, and that the install was started with --lan."
    exit 1
fi

TOKEN_VALID=$(echo "$VALIDATE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('valid', False))" 2>/dev/null || echo "False")

if [ "$TOKEN_VALID" = "True" ]; then
    info "Token is valid"
else
    error "Token is invalid, expired, or already used"
    if [ -n "$VALIDATE_RESPONSE" ]; then
        echo "  Response: $VALIDATE_RESPONSE"
    fi
    exit 1
fi

# ============================================================================
# Step 3 — Install Docker
# ============================================================================
header "Step 3/8 — Docker"

if command -v docker &>/dev/null; then
    info "Docker already installed: $(docker --version)"
else
    warn "Docker not found — installing..."

    if [[ "$DISTRO_LIKE" == *"debian"* ]] || [[ "$DISTRO_ID" == "ubuntu" ]] || [[ "$DISTRO_ID" == "debian" ]]; then
        pkg_update
        pkg_install ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" \
            | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
            https://download.docker.com/linux/${DISTRO_ID} \
            $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            | tee /etc/apt/sources.list.d/docker.list > /dev/null
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    elif [[ "$DISTRO_LIKE" == *"rhel"* ]] || [[ "$DISTRO_LIKE" == *"fedora"* ]] \
         || [[ "$DISTRO_ID" == "centos" ]] || [[ "$DISTRO_ID" == "fedora" ]] \
         || [[ "$DISTRO_ID" == "rocky" ]] || [[ "$DISTRO_ID" == "almalinux" ]]; then
        dnf install -y -q dnf-plugins-core || yum install -y -q yum-utils
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null \
            || yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
            || yum install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    elif [[ "$DISTRO_ID" == "opensuse"* ]] || [[ "$DISTRO_LIKE" == *"suse"* ]]; then
        zypper install -y docker docker-compose
    else
        warn "Unknown distro — falling back to Docker convenience script..."
        curl -fsSL https://get.docker.com | sh
    fi

    info "Docker installed: $(docker --version)"
fi

systemctl enable docker
systemctl start docker

if ! docker info &>/dev/null 2>&1; then
    sleep 3
    if ! docker info &>/dev/null 2>&1; then
        error "Docker daemon failed to start"
        exit 1
    fi
fi
info "Docker daemon: running"

SUDO_USER="${SUDO_USER:-}"
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    usermod -aG docker "$SUDO_USER" 2>/dev/null || true
    info "Added $SUDO_USER to docker group (re-login to take effect)"
fi

# ============================================================================
# Helper: configure Docker daemon for raw GPU device passthrough.
# Docker 25+ uses CDI (Container Device Interface) by default for GPU
# discovery. When vendor CDI specs aren't installed (AMD, Intel), Docker
# fails with "failed to discover GPU vendor from CDI." This configures
# the daemon to allow raw --device flags without CDI intercepting them.
# NVIDIA is excluded because nvidia-container-toolkit sets up CDI properly.
_configure_docker_gpu_passthrough() {
    local vendor="$1"
    local daemon_json="/etc/docker/daemon.json"

    # Check if CDI is causing issues by looking at Docker version
    local docker_major
    docker_major=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)

    if [ "${docker_major:-0}" -ge 25 ]; then
        info "${vendor}: Docker ${docker_major} detected — configuring GPU device passthrough"

        # Merge into existing daemon.json or create new
        if [ -f "$daemon_json" ]; then
            # Check if features.cdi is already configured
            if grep -q '"cdi"' "$daemon_json" 2>/dev/null; then
                info "${vendor}: Docker daemon.json already has CDI config"
            else
                # Add features.cdi:false using python for safe JSON merge
                python3 -c "
import json, sys
try:
    with open('$daemon_json') as f:
        cfg = json.load(f)
except:
    cfg = {}
cfg.setdefault('features', {})['cdi'] = False
with open('$daemon_json', 'w') as f:
    json.dump(cfg, f, indent=2)
print('Updated $daemon_json with CDI disabled')
" 2>/dev/null || {
                    # Fallback: write minimal config
                    echo '{"features":{"cdi":false}}' > "$daemon_json"
                }
                info "${vendor}: Disabled CDI in Docker daemon — restarting Docker..."
                systemctl restart docker
                for i in $(seq 1 30); do
                    if docker info &>/dev/null 2>&1; then break; fi
                    sleep 1
                done
                info "${vendor}: Docker restarted with raw device passthrough enabled"
            fi
        else
            echo '{"features":{"cdi":false}}' > "$daemon_json"
            info "${vendor}: Created Docker daemon.json with CDI disabled — restarting..."
            systemctl restart docker
            for i in $(seq 1 30); do
                if docker info &>/dev/null 2>&1; then break; fi
                sleep 1
            done
            info "${vendor}: Docker restarted with raw device passthrough enabled"
        fi
    else
        info "${vendor}: Docker ${docker_major:-<unknown} pre-25 — raw device passthrough works by default"
    fi
}


# Helper: hold NVIDIA driver packages so unattended-upgrades never bumps
# userspace while the old kernel module is still loaded. This prevents the
# "Driver/library version mismatch" that kills nvidia-persistenced and
# requires a full reboot to recover.
_pin_nvidia_driver_packages() {
    # Only runs on apt-based distros (deb/ubuntu/etc.)
    command -v apt-get &>/dev/null || return 0

    # Driver/kernel-module userspace only — deliberately narrower than
    # "nvidia-.*"/"libnvidia-.*" so it never touches nvidia-container-toolkit
    # or libnvidia-container (those still get normal security updates).
    local _driver_pattern='^(nvidia-driver-|nvidia-dkms-|nvidia-kernel-|nvidia-utils-|nvidia-compute-utils-|nvidia-firmware-|linux-modules-nvidia-|xserver-xorg-video-nvidia-|libnvidia-|cuda-drivers)'
    local _exclude_pattern='^(nvidia-container-toolkit|libnvidia-container)'

    # 1. Blacklist from unattended-upgrades — prevents future auto-upgrades
    #    of driver/kernel-module packages only. Uses the same tee pattern as
    #    99force-ipv4. File is overwritten (not appended) so re-runs are
    #    idempotent. The "libnvidia-(?!container).*" entry relies on
    #    unattended-upgrades matching blacklist entries with Python re
    #    (lookahead supported) to keep libnvidia-container* out.
    cat > /etc/apt/apt.conf.d/50mod-nvidia-hold 2>/dev/null <<'EOF'
Unattended-Upgrade::Package-Blacklist {
    "nvidia-driver-.*";
    "nvidia-dkms-.*";
    "nvidia-kernel-.*";
    "nvidia-utils-.*";
    "nvidia-compute-utils-.*";
    "nvidia-firmware-.*";
    "linux-modules-nvidia-.*";
    "xserver-xorg-video-nvidia-.*";
    "cuda-drivers.*";
    "libnvidia-(?!container).*";
};
EOF
    if [ -f /etc/apt/apt.conf.d/50mod-nvidia-hold ]; then
        info "apt hold: wrote /etc/apt/apt.conf.d/50mod-nvidia-hold (unattended-upgrades blacklist, driver pkgs only — nvidia-container-toolkit/libnvidia-container still auto-update)"
    else
        warn "apt hold: could not write /etc/apt/apt.conf.d/50mod-nvidia-hold — driver may still be auto-upgraded"
        return 0
    fi

    # 2. Mark currently-installed nvidia driver packages as held via apt-mark.
    #    This prevents apt from upgrading them even if unattended-upgrades
    #    somehow slips through or if the user runs apt upgrade manually.
    #    apt-mark hold is idempotent — holding an already-held package is a
    #    no-op, so repeat runs of this function are safe.
    local _driver_pkgs
    _driver_pkgs="$(dpkg --get-selections 2>/dev/null \
        | awk '{print $1}' \
        | grep -E "$_driver_pattern" \
        | grep -vE "$_exclude_pattern")"

    if [ -n "$_driver_pkgs" ]; then
        echo "$_driver_pkgs" | xargs -r apt-mark hold 2>/dev/null
        info "apt hold: $(echo "$_driver_pkgs" | tr '\n' ', ') now held — driver upgrades are manual from here (apt-mark unhold <pkg>, apt upgrade, reboot)"
    else
        info "apt hold: no nvidia driver packages found to hold (GPU driver may not be installed yet)"
    fi
    return 0
}

# Helper: install + register the NVIDIA Container Toolkit so Docker's --gpus
# path (DeviceRequests with Capabilities [[gpu]] — how the worker attaches
# NVIDIA GPUs) works WITHOUT a manual reboot. Identical steps on native Linux
# and WSL2: on WSL2 the kernel driver comes from the Windows host via GPU-PV,
# but the userspace toolkit + Docker runtime registration are the same and are
# what the `--gpus` path actually needs. Sets NVIDIA_CTK_OK=true iff a real
# --gpus container verifies. Never fatal — the worker still enrols on failure.
_setup_nvidia_container_toolkit() {
    header "  NVIDIA — container toolkit"

    if docker info 2>/dev/null | grep -q -i nvidia; then
        info "NVIDIA Container Toolkit runtime already registered in Docker"
    else
        warn "Installing NVIDIA Container Toolkit..."
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null || true
        curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null 2>/dev/null || true

        if command -v apt-get &>/dev/null; then
            apt-get update -qq || true
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit \
                || warn "nvidia-container-toolkit install failed — GPU containers may not work"
        elif command -v dnf &>/dev/null || command -v yum &>/dev/null; then
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
                | tee /etc/yum.repos.d/nvidia-container-toolkit.repo > /dev/null
            dnf install -y -q nvidia-container-toolkit 2>/dev/null \
                || yum install -y -q nvidia-container-toolkit \
                || warn "nvidia-container-toolkit install failed — GPU containers may not work"
        fi

        # Register the nvidia runtime in the LIVE Docker daemon. nvidia-ctk MERGES
        # into /etc/docker/daemon.json, preserving any features.cdi:false already
        # set for AMD/Intel raw passthrough, and adds runtimes.nvidia. The two
        # settings do NOT conflict: with cdi:false, --gpus uses the legacy nvidia
        # hook (not CDI), which is exactly the mode we want and the one that works
        # reliably on WSL2. The restart is what makes the runtime live so NO manual
        # reboot is needed for GPU containers.
        nvidia-ctk runtime configure --runtime=docker 2>/dev/null \
            || warn "nvidia-ctk runtime configure failed — is nvidia-container-toolkit installed?"
        info "Restarting Docker to activate the NVIDIA runtime..."
        systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
        for i in $(seq 1 30); do
            if docker info &>/dev/null 2>&1; then break; fi
            sleep 1
        done
        info "NVIDIA Container Toolkit installed and runtime registered"
    fi

    # Pin the NVIDIA driver packages so unattended-upgrades does not
    # bump userspace while the old kernel module stays loaded (prevents
    # "Driver/library version mismatch" → nvidia-persistenced dead).
    _pin_nvidia_driver_packages 2>/dev/null || true

    # VERIFY the exact path the worker uses (--gpus / DeviceRequests) actually
    # works before declaring success. Authoritative check: run a CUDA base image.
    header "  NVIDIA — verifying Docker GPU access"
    if docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi &>/dev/null; then
        info "NVIDIA GPU verified inside Docker (--gpus all works — no reboot needed)"
        NVIDIA_CTK_OK=true
    else
        NVIDIA_CTK_OK=false
        warn "GPU containers STILL failing on the --gpus path (Capabilities [[gpu]])."
        warn "The worker will enrol, but its NVIDIA GPU tasks will not run until this is fixed."
        warn "Remediation — run these, then re-test with the docker run at the end:"
        warn "    sudo nvidia-ctk runtime configure --runtime=docker"
        warn "    sudo systemctl restart docker"
        warn "    docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi"
        if $IS_WSL; then
            warn "  On WSL2: ensure the NVIDIA driver is installed on the WINDOWS host and that"
            warn "  /usr/lib/wsl/lib/libcuda.so.1 exists; if so, 'wsl --shutdown' from Windows, then re-run."
        fi
    fi
}


# Step 3 — GPU detection and driver install
# ============================================================================
header "Step 4/8 — GPU drivers"

GPU_NVIDIA=false
GPU_AMD=false
GPU_INTEL=false
GPU_NPU=false
NVIDIA_CTK_OK=false
AMD_INSTALL_FAILED=false
AMD_DRIVER_OK=false
PCI_OUT=""

# --- Detect WSL2 ---
IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null || [ -f /proc/sys/fs/binfmt_misc/WSLInterop ]; then
    IS_WSL=true
    warn "WSL2 detected — lspci is virtualized, using driver-based GPU detection"
fi

# --- Detect GPU hardware ---
# Method 1: lspci (works on bare metal / VMs, NOT reliable in WSL2)
if ! $IS_WSL; then
    if command -v lspci &>/dev/null; then
        PCI_OUT=$(lspci 2>/dev/null || true)
    else
        pkg_install pciutils 2>/dev/null || true
        PCI_OUT=$(lspci 2>/dev/null || true)
    fi

    if echo "$PCI_OUT" | grep -iq 'nvidia'; then
        GPU_NVIDIA=true
        info "NVIDIA GPU detected (lspci)"
    fi
    # Navi 48 / Radeon AI PRO R9000 (gfx1201): lspci prints the vendor as
    # "Advanced Micro Devices, Inc. [AMD/ATI]" and the product as "Radeon AI PRO
    # R9000". The old pattern needed "amd" and "radeon" on the same line, which
    # fails when lspci reports "ATI Technologies Inc" — so the R9000 was missed.
    if echo "$PCI_OUT" | grep -iq 'amd.*radeon\|amd.*instinct\|advanced micro.*display\|amd.*navi\|amd.*vega\|ati technologies\|radeon ai pro\|r9700\|gfx1201\|navi 48'; then
        GPU_AMD=true
        info "AMD GPU detected (lspci)"
    fi
    if echo "$PCI_OUT" | grep -iq 'intel.*arc\|intel.*graphics\|intel.*xe\|intel.*uhd\|intel.*iris'; then
        GPU_INTEL=true
        info "Intel GPU detected (lspci)"
    fi
fi

# Method 2: Driver probing (works in WSL2 and as supplement on bare metal)
# Temporarily disable pipefail — some detection commands use pipes that may fail
set +o pipefail 2>/dev/null || true

# NVIDIA: nvidia-smi is the definitive check
if ! $GPU_NVIDIA; then
    if command -v nvidia-smi &>/dev/null; then
        if nvidia-smi --query-gpu=name --format=csv,noheader -i 0 &>/dev/null; then
            GPU_NVIDIA=true
            info "NVIDIA GPU detected (nvidia-smi)"
        fi
    fi
fi

# Intel: check DRI device vendor IDs or tools
if ! $GPU_INTEL && [ -d /sys/class/drm ]; then
    for card in /sys/class/drm/card*/device/vendor; do
        [ -f "$card" ] || continue
        if [ "$(cat "$card" 2>/dev/null)" = "0x8086" ]; then
            GPU_INTEL=true
            info "Intel GPU detected (/sys/class/drm vendor ID)"
            break
        fi
    done
fi
if ! $GPU_INTEL && command -v intel_gpu_top &>/dev/null; then
    GPU_INTEL=true
    info "Intel GPU detected (intel_gpu_top)"
fi
if ! $GPU_INTEL && command -v clinfo &>/dev/null; then
    if clinfo 2>/dev/null | grep -qi intel; then
        GPU_INTEL=true
        info "Intel GPU detected (clinfo)"
    fi
fi

# AMD: check DRI device vendor IDs or tools
if ! $GPU_AMD && [ -d /sys/class/drm ]; then
    for card in /sys/class/drm/card*/device/vendor; do
        [ -f "$card" ] || continue
        if [ "$(cat "$card" 2>/dev/null)" = "0x1002" ]; then
            GPU_AMD=true
            info "AMD GPU detected (/sys/class/drm vendor ID)"
            break
        fi
    done
fi
if ! $GPU_AMD && command -v rocm-smi &>/dev/null; then
    if rocm-smi --showproductname 2>/dev/null | grep -qi 'amd\|radeon'; then
        GPU_AMD=true
        info "AMD GPU detected (rocm-smi)"
    fi
fi

# WSL2: GPUs are passed through via GPU-PV but tools/sysfs don't work.
# Use WORKER_HARDWARE_TAGS env var if set, otherwise auto-detect from
# /usr/lib/wsl/lib (DirectX libs = GPUs available) and DRI device count.
# WSL GPU detection via powershell — flat structure to avoid set -e nesting issues
if $IS_WSL && ! $GPU_NVIDIA && ! $GPU_INTEL && ! $GPU_AMD; then
    if [ -n "${WORKER_HARDWARE_TAGS:-}" ]; then
        info "WSL2: Using WORKER_HARDWARE_TAGS from environment: $WORKER_HARDWARE_TAGS"
        for tag in $(echo "$WORKER_HARDWARE_TAGS" | tr ',' ' '); do
            case "$tag" in
                cuda|nvidia) GPU_NVIDIA=true; info "  → NVIDIA/CUDA enabled" ;;
                intel) GPU_INTEL=true; info "  → Intel GPU enabled" ;;
                rocm|amd) GPU_AMD=true; info "  → AMD/ROCm enabled" ;;
                npu) GPU_NPU=true; info "  → NPU enabled" ;;
            esac
        done
    fi
fi

# WSL2 PowerShell detection — always run on WSL2 to discover ALL GPUs.
# Tool-based probes (clinfo, rocm-smi) may find one GPU but miss others.
set +e
if $IS_WSL; then
    if [ -d /usr/lib/wsl/lib ] || [ -d /dev/dri ]; then
        info "WSL2: GPU passthrough detected — querying Windows for GPU info..."

        # Find powershell — may not be on PATH
        _PS_CMD=""
        command -v powershell.exe &>/dev/null && _PS_CMD="powershell.exe"
        [ -z "$_PS_CMD" ] && [ -x /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe ] && _PS_CMD="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

        # ALL powershell output goes to temp files — NO PIPES
        # Pipes steal stdin when script is run via curl|bash
        _GPU_TMP="/tmp/.mod-gpu-detect.$$"
        _NPU_TMP="/tmp/.mod-npu-detect.$$"

        if [ -n "$_PS_CMD" ]; then
            # GPU detection — close stdin (/dev/null) so powershell can't read from curl pipe
            "$_PS_CMD" -NoProfile -Command "Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name" < /dev/null > "$_GPU_TMP" 2>/dev/null || true

            # NPU detection
            "$_PS_CMD" -NoProfile -Command 'Get-WmiObject Win32_PnPEntity | Where-Object { $_.Name -match "NPU|Neural|AI Accelerator|AI Boost" } | Select-Object -ExpandProperty Name' < /dev/null > "$_NPU_TMP" 2>/dev/null || true
        fi

        # Parse GPU results from temp file (no pipes needed)
        if [ -s "$_GPU_TMP" ]; then
            WIN_GPUS=$(tr -d '\r' < "$_GPU_TMP")
            info "WSL2: Windows reports these GPUs:"
            while IFS= read -r gpu; do
                [ -n "$gpu" ] && info "    $gpu"
            done <<< "$WIN_GPUS"

            case "${WIN_GPUS,,}" in
                *nvidia*|*geforce*|*quadro*|*tesla*) GPU_NVIDIA=true; info "  → NVIDIA GPU detected" ;;&
                *intel*|*iris*|*uhd*|*arc*) GPU_INTEL=true; info "  → Intel GPU detected" ;;&
                *amd*|*radeon*|*instinct*) GPU_AMD=true; info "  → AMD GPU detected" ;;&
            esac
        else
            warn "WSL2: Could not query Windows GPU info."
        fi

        # Parse NPU results from temp file
        if [ -s "$_NPU_TMP" ]; then
            GPU_NPU=true
            info "  → NPU detected: $(tr -d '\r' < "$_NPU_TMP")"
        fi

        rm -f "$_GPU_TMP" "$_NPU_TMP"
    fi
fi
true  # Reset exit status before re-enabling set -e
set -e


# NPU detection (all platforms)
if ! $GPU_NPU && [ -d /dev/accel ]; then
    if ls /dev/accel/accel* 1>/dev/null 2>&1; then
        GPU_NPU=true
        info "  → NPU detected (/dev/accel)"
    fi
fi
if ! $GPU_NPU && [ -n "$PCI_OUT" ]; then
    if echo "$PCI_OUT" | grep -qi 'neural\|npu\|ai accelerator'; then
        GPU_NPU=true
        info "NPU detected (PCI)"
    fi
fi


# Override: WORKER_HARDWARE_TAGS env var always wins if set
if [ -n "${WORKER_HARDWARE_TAGS:-}" ]; then
    info "Applying WORKER_HARDWARE_TAGS override: $WORKER_HARDWARE_TAGS"
    for tag in $(echo "$WORKER_HARDWARE_TAGS" | tr ',' ' '); do
        case "$tag" in
            cuda|nvidia) GPU_NVIDIA=true; info "  → NVIDIA/CUDA enabled" ;;
            intel) GPU_INTEL=true; info "  → Intel GPU enabled" ;;
            rocm|amd) GPU_AMD=true; info "  → AMD/ROCm enabled" ;;
            npu) GPU_NPU=true; info "  → NPU enabled" ;;
        esac
    done
fi


# Re-enable pipefail after detection probes
set -o pipefail 2>/dev/null || true

if ! $GPU_NVIDIA && ! $GPU_AMD && ! $GPU_INTEL; then
    info "No discrete/compute GPU detected — CPU-only worker"
fi


# --- Driver installation ---
# On WSL2, kernel drivers come from Windows via GPU-PV, but userspace compute
# runtimes (Level Zero, OpenCL ICD, HIP) must still be installed inside WSL.
if $IS_WSL; then
    info "WSL2: Kernel drivers provided by Windows host via GPU-PV"

    # WSL2 networking: HTTP (port 80) is often blocked by Windows firewall
    # but HTTPS (443) works. Switch all apt sources to HTTPS and force IPv4.
    echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 2>/dev/null || true
    for _sf in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$_sf" ] && sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; s|http://security.ubuntu.com|https://security.ubuntu.com|g' "$_sf" 2>/dev/null || true
    done
    for _sf in /etc/apt/sources.list.d/*.sources; do
        [ -f "$_sf" ] && sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; s|http://security.ubuntu.com|https://security.ubuntu.com|g' "$_sf" 2>/dev/null || true
    done

    if $GPU_INTEL; then
        header "  Intel — WSL2 compute runtime"
        # WSL2 has NO /dev/dri — GPUs are accessed through /dev/dxg only.
        # But the Intel compute runtime (Level Zero driver) must be installed
        # on the host so OpenCL/Level Zero can find GPUs through the DXG bridge.
        _HAS_INTEL_RT=$(dpkg -l libze-intel-gpu1 2>/dev/null | grep -c "^ii" || true)
        if [ "$_HAS_INTEL_RT" -gt 0 ]; then
            info "Intel compute runtime already installed"
        else
            info "Installing Intel compute runtime for WSL2..."
            _INTEL_LOG="/tmp/intel-gpu-install.log"
            (
                # Remove stale repos and old package names
                _remove_intel_gpu_repos
                apt-get remove -y intel-level-zero-gpu 2>/dev/null || true

                # Add the Intel GPU repo for the running codename — but only if
                # Intel actually publishes it. _add_intel_gpu_repo probes first,
                # falls back to a known-good codename, or skips (and cleans up)
                # so an unsupported codename can never poison apt for later steps.
                _CODENAME=$(lsb_release -cs 2>/dev/null || echo "noble")
                pkg_install gpg wget curl
                _add_intel_gpu_repo "$_CODENAME" || exit 3
                apt-get update -qq
                # New package names: libze-intel-gpu1 replaces intel-level-zero-gpu
                #                    libze1 replaces level-zero meta-package
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
                    intel-opencl-icd libze-intel-gpu1 libze1 libigc2 libigdfcl2 libigdgmm12
            ) > "$_INTEL_LOG" 2>&1 \
                && info "Intel compute runtime installed" \
                || warn "Intel compute runtime install failed or skipped — see $_INTEL_LOG"
        fi
    fi

    if $GPU_AMD; then
        if [ -f /opt/rocm/lib/librocdxg.so ] || [ -f /usr/local/lib/librocdxg.so ]; then
            info "librocdxg already installed"
        else
            header "  AMD — WSL2 librocdxg DXG bridge"
            WIN_SDK_BASE="/mnt/c/Program Files (x86)/Windows Kits/10/Include"
            WIN_SDK_VER=$(ls -1 "$WIN_SDK_BASE" 2>/dev/null | sort -V | tail -1 || true)
            if [ -z "${WIN_SDK_VER:-}" ]; then
                echo ""
                echo ""
                echo -e "  ${YELLOW}${BOLD}>>> Windows SDK is required for AMD GPU support on WSL2 <<<${NC}"
                echo ""
                echo "  Open a Windows PowerShell window and run this command:"
                echo ""
                echo -e "    ${BOLD}Invoke-WebRequest -Uri \"https://go.microsoft.com/fwlink/?linkid=2272610\" -OutFile \"\$env:TEMP\\winsdksetup.exe\"; Start-Process \"\$env:TEMP\\winsdksetup.exe\" -Wait${NC}"
                echo ""
                echo "  This downloads and installs the Windows SDK. The script will"
                echo "  wait here until you're done."
                echo ""
                if [ -t 0 ]; then
                    read -r -p "  Press Enter when installed (or 's' to skip AMD GPU support): " _SDK_RESPONSE
                else
                    _SDK_RESPONSE="s"
                fi
                if [ "${_SDK_RESPONSE:-}" != "s" ]; then
                    WIN_SDK_VER=$(ls -1 "$WIN_SDK_BASE" 2>/dev/null | sort -V | tail -1 || true)
                    if [ -z "${WIN_SDK_VER:-}" ]; then
                        warn "Windows SDK still not detected at $WIN_SDK_BASE"
                        warn "AMD GPU benchmarks will return zeros — re-run this script after installing"
                    else
                        info "Windows SDK found: ${WIN_SDK_VER}"
                    fi
                else
                    info "Skipped — AMD GPU benchmarks will return zeros on WSL2"
                fi
            fi
            if [ -n "${WIN_SDK_VER:-}" ]; then
                info "Building librocdxg (this takes about 30 seconds)..."
                pkg_install cmake g++ git make > /dev/null 2>&1
                _ROCDXG_LOG="/tmp/librocdxg-build.log"
                (
                    cd /tmp && rm -rf librocdxg
                    git clone --depth 1 https://github.com/ROCm/librocdxg.git
                    cd librocdxg && mkdir -p build && cd build
                    cmake .. -DWIN_SDK="${WIN_SDK_BASE}/${WIN_SDK_VER}/shared"
                    make -j"$(nproc)" && make install && ldconfig
                    rm -rf /tmp/librocdxg
                ) > "$_ROCDXG_LOG" 2>&1 && info "librocdxg installed" \
                  || { warn "librocdxg build failed — see $_ROCDXG_LOG for details"; }
            fi
        fi
    fi

    _configure_docker_gpu_passthrough "WSL2"

    # NVIDIA on WSL2: the kernel driver comes from the Windows host, but the
    # --gpus path STILL needs nvidia-container-toolkit installed AND registered
    # as a Docker runtime AND Docker restarted. This was previously only done on
    # the native path, so WSL2 NVIDIA workers hit
    #   "could not select device driver \"\" with capabilities: [[gpu]]".
    # NVIDIA takes priority on mixed (e.g. NVIDIA+Intel) boxes: it runs AFTER the
    # cdi:false passthrough above and merges the nvidia runtime alongside it.
    if $GPU_NVIDIA; then
        info "WSL2: NVIDIA present — installing container toolkit + registering Docker runtime"
        _setup_nvidia_container_toolkit
    fi
fi

if ! $IS_WSL && $GPU_NVIDIA; then
    header "  NVIDIA — drivers"

    if command -v nvidia-smi &>/dev/null && nvidia-smi --query-gpu=name --format=csv,noheader -i 0 &>/dev/null; then
        info "NVIDIA driver already installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
    else
        warn "NVIDIA driver not found — installing..."
        if [[ "$DISTRO_LIKE" == *"debian"* ]] || [[ "$DISTRO_ID" == "ubuntu" ]] || [[ "$DISTRO_ID" == "debian" ]]; then
            pkg_update
            pkg_install ubuntu-drivers-common 2>/dev/null || true
            RECOMMENDED=$(ubuntu-drivers devices 2>/dev/null | grep 'recommended' | awk '{print $3}')
            if [ -n "$RECOMMENDED" ]; then
                info "Installing recommended driver: $RECOMMENDED"
                DEBIAN_FRONTEND=noninteractive apt-get install -y "$RECOMMENDED"
            else
                info "No recommended driver found — trying server drivers..."
                DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-driver-560-server 2>/dev/null \
                    || DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-driver-550-server 2>/dev/null \
                    || DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-driver-535-server 2>/dev/null \
                    || { warn "Auto-install failed — install NVIDIA drivers manually and re-run"; }
            fi
        elif [[ "$DISTRO_LIKE" == *"rhel"* ]] || [[ "$DISTRO_LIKE" == *"fedora"* ]] \
             || [[ "$DISTRO_ID" == "centos" ]] || [[ "$DISTRO_ID" == "rocky" ]] || [[ "$DISTRO_ID" == "almalinux" ]]; then
            dnf install -y -q epel-release 2>/dev/null || true
            dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/rhel$(rpm -E %rhel)/x86_64/cuda-rhel$(rpm -E %rhel).repo 2>/dev/null || true
            dnf install -y -q nvidia-driver 2>/dev/null \
                || yum install -y -q nvidia-driver 2>/dev/null \
                || { warn "Auto-install failed — install NVIDIA drivers manually and re-run"; }
        else
            warn "Cannot auto-install NVIDIA drivers on ${DISTRO_ID} — install manually"
        fi

        if command -v nvidia-smi &>/dev/null && nvidia-smi --query-gpu=name --format=csv,noheader -i 0 &>/dev/null; then
            info "NVIDIA driver installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
        else
            warn "NVIDIA driver installed but requires a REBOOT to activate"
        fi
    fi

    # Install nvidia-container-toolkit, register the nvidia Docker runtime,
    # restart Docker, and verify the --gpus path — shared with the WSL2 path.
    _setup_nvidia_container_toolkit
fi

# ---------------------------------------------------------------------------
# ROCm apt repo — defensive add (mirrors the Intel GPU repo precedent above)
# ---------------------------------------------------------------------------
# repo.radeon.com publishes its apt tree only for specific Ubuntu codenames. On
# a distro whose codename it has NOT published (e.g. 26.04 "resolute" on the
# AlphaBox/Strix Halo) the repo has no Release file and — once written — POISONS
# every later `apt-get update`, including the one inside tailscale's installer,
# which would break the CRITICAL mesh anchor over an OPTIONAL GPU extra. So we
# PROBE the repo before trusting it, fall back to a known-good codename, or skip
# the host userspace entirely — and never leave an unusable rocm source on disk.
# The amdgpu KERNEL driver + /dev/kfd + /dev/dri are what a native Strix Halo
# box needs; Strata runs the ROCm userspace inside its own container image.
# The published-codename set is decided AT RUN TIME by probing — never assumed.
ROCM_KNOWN_GOOD_CODENAMES="noble jammy"

# Remove any ROCm apt source/pin files so a stale or broken one from an earlier
# run can't poison apt for the steps that follow.
_remove_rocm_repos() {
    rm -f /etc/apt/sources.list.d/rocm.list \
          /etc/apt/sources.list.d/rocm-*.list \
          /etc/apt/preferences.d/rocm-pin-600 2>/dev/null || true
}

# Return 0 if the ROCm apt repo for version $1 publishes a Release file for
# codename $2. Probes only — never writes anything.
_rocm_repo_available() {
    local version="$1" codename="$2"
    [ -n "$version" ] && [ -n "$codename" ] || return 1
    curl -fsI --max-time 15 \
        "https://repo.radeon.com/rocm/apt/${version}/dists/${codename}/Release" \
        >/dev/null 2>&1
}

# Write a USABLE ROCm apt source, or none at all. $1 = ROCm version, $2 = the
# running distro's codename. Probes the wanted codename, then each fallback in
# ROCM_KNOWN_GOOD_CODENAMES in order; on a total miss it removes any ROCm repo
# file and returns 1 so the caller SKIPS the host ROCm userspace and keeps going
# (never marks the run failed, never exits). The source file is named by the
# codename actually used (rocm-<codename>.list) so its name always matches its
# contents, and the pin is written only alongside a source that was written.
_add_rocm_repo() {
    local version="$1" want="$2" use=""
    _remove_rocm_repos
    if _rocm_repo_available "$version" "$want"; then
        use="$want"
    else
        local candidate
        for candidate in $ROCM_KNOWN_GOOD_CODENAMES; do
            if _rocm_repo_available "$version" "$candidate"; then
                warn "ROCm apt repo has no '${want}' release — falling back to '${candidate}'"
                use="$candidate"
                break
            fi
        done
    fi
    if [ -z "$use" ]; then
        warn "AMD ROCm apt repo unavailable for '${want}' (and fallbacks) — skipping host ROCm userspace; the amdgpu kernel driver + /dev/kfd + /dev/dri are what a native Strix Halo box needs, Strata runs ROCm inside its container"
        _remove_rocm_repos
        return 1
    fi
    mkdir -p /etc/apt/keyrings
    wget -q -O - https://repo.radeon.com/rocm/rocm.gpg.key \
        | gpg --batch --yes --dearmor -o /etc/apt/keyrings/rocm.gpg 2>/dev/null || true
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${version} ${use} main" \
        > "/etc/apt/sources.list.d/rocm-${use}.list"
    printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' \
        > /etc/apt/preferences.d/rocm-pin-600
    return 0
}

# --- AMD: ROCm + container support ---
if ! $IS_WSL && $GPU_AMD; then
    header "  AMD — ROCm drivers"

    # Native Linux needs the amdgpu KERNEL driver as well as the userspace ROCm
    # packages: rocm-dev/rocm-libs alone never creates /dev/kfd on a box where the
    # amdgpu dkms module was never installed. Navi 48 (Radeon AI PRO R9000,
    # gfx1201) needs ROCm 6.4+ — 6.3 has no gfx1201 support.
    ROCM_VERSION="6.3"
    if echo "$PCI_OUT" | grep -iq 'radeon ai pro\|r9700\|gfx1201\|navi 48'; then
        ROCM_VERSION="6.4"
        info "  Navi 48 / R9000 (gfx1201) — using ROCm ${ROCM_VERSION}"
    fi

    if command -v rocminfo &>/dev/null && rocminfo &>/dev/null; then
        info "ROCm already installed"
    else
        warn "Installing ROCm..."
        if [[ "$DISTRO_LIKE" == *"debian"* ]] || [[ "$DISTRO_ID" == "ubuntu" ]] || [[ "$DISTRO_ID" == "debian" ]]; then
            # Drop any stale/broken ROCm source BEFORE touching apt so a bad
            # rocm.list left by an earlier run can't abort this one, then refresh
            # indexes tolerantly (the keyring is fetched by _add_rocm_repo only
            # once a usable repo has actually been probed).
            _remove_rocm_repos
            pkg_update || { warn "apt-get update failing — removing ROCm repo and retrying"; _remove_rocm_repos; pkg_update || true; }
            pkg_install gpg wget curl
            # Probe the apt repo for the running codename (then the known-good
            # fallbacks) BEFORE writing anything: an unpublished codename (e.g.
            # 26.04 "resolute" on the AlphaBox/Strix Halo) has no Release file, and
            # a repo written on faith POISONS every later apt-get update —
            # including the one inside tailscale's installer — which would break
            # the CRITICAL mesh anchor over an OPTIONAL GPU extra. On a total
            # miss we skip the host userspace and keep going to Steps 5-8.
            ROCM_USERSPACE_SKIPPED=false
            _ROCM_CODENAME=$(lsb_release -cs 2>/dev/null || echo "noble")
            if _add_rocm_repo "$ROCM_VERSION" "$_ROCM_CODENAME"; then
                # If the freshly-added repo still breaks apt, drop it and retry
                # rather than leaving apt poisoned for the steps that follow.
                pkg_update || { warn "apt-get update failed after adding ROCm repo — removing it"; _remove_rocm_repos; pkg_update || true; }
                _ensure_apt_healthy || true
                # amdgpu kernel driver first — without it /dev/kfd never appears,
                # and rocm-dev/rocm-libs alone are only the userspace stack.
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
                    -o Dpkg::Options::="--force-overwrite" \
                    amdgpu-dkms "rocm-core" 2>/dev/null || warn "amdgpu-dkms install failed — kernel driver may be missing"
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rocm-dev rocm-libs hipblaslt 2>/dev/null \
                    || { warn "ROCm auto-install failed — install manually: https://rocm.docs.amd.com"; }
            else
                # No usable ROCm apt repo: skip the host userspace and CONTINUE.
                # The amdgpu kernel driver + /dev/kfd + /dev/dri verified below
                # are what a native Strix Halo box needs; Strata runs the ROCm
                # userspace inside its own container image. Never mark the run
                # failed, never reboot-gate, never exit non-zero from here.
                ROCM_USERSPACE_SKIPPED=true
                warn "ROCm userspace skipped — install manually from https://rocm.docs.amd.com if this box needs host-side ROCm"
            fi
        elif [[ "$DISTRO_LIKE" == *"rhel"* ]] || [[ "$DISTRO_LIKE" == *"fedora"* ]]; then
            ROCM_VERSION="6.3"
            cat > /etc/yum.repos.d/rocm.repo << REPOEOF
[rocm]
name=ROCm
baseurl=https://repo.radeon.com/rocm/rhel9/${ROCM_VERSION}/main
enabled=1
gpgcheck=1
gpgkey=https://repo.radeon.com/rocm/rocm.gpg.key
REPOEOF
            dnf install -y -q amdgpu-dkms rocm-dev rocm-libs hipblaslt 2>/dev/null \
                || { warn "ROCm auto-install failed — install manually"; }
        else
            warn "Cannot auto-install ROCm on ${DISTRO_ID} — install manually"
        fi

        if command -v rocminfo &>/dev/null; then
            info "ROCm installed"
        else
            error "ROCm userspace tools NOT installed — AMD compute path is not usable on this box"
            AMD_INSTALL_FAILED=true
        fi
    fi

    # Post-install verification (native Linux only — on WSL2 the kernel driver
    # lives on the Windows host and /dev/kfd is provided by GPU-PV, unchanged).
    AMD_DRIVER_OK=false
    if [ -e /dev/kfd ] && [ -d /dev/dri ] && ls /dev/dri/renderD* >/dev/null 2>&1; then
        AMD_DRIVER_OK=true
        info "AMD GPU device nodes present (/dev/kfd, /dev/dri/renderD*)"
    else
        error "/dev/kfd or /dev/dri render node missing — amdgpu kernel driver NOT live on this box"
    fi
    if $AMD_DRIVER_OK && command -v rocminfo &>/dev/null; then
        if rocminfo &>/dev/null; then
            info "rocminfo verifies the AMD GPU (agent visible)"
        else
            error "rocminfo present but reports no agent — driver not live; reboot and re-run"
            AMD_DRIVER_OK=false
        fi
    fi
    if ! $AMD_DRIVER_OK || $AMD_INSTALL_FAILED; then
        NEEDS_REBOOT=true
        error "AMD/ROCm path INCOMPLETE — fix the driver, then re-run worker_setup.sh on this box"
    fi

    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
        usermod -aG render "$SUDO_USER" 2>/dev/null || true
        usermod -aG video "$SUDO_USER" 2>/dev/null || true
        info "Added $SUDO_USER to render+video groups"
    fi

    # Docker container support for AMD GPUs.
    # Docker 25+ uses CDI by default which fails for AMD (no vendor CDI spec).
    # Configure Docker daemon to allow raw --device passthrough so the worker
    # can mount /dev/kfd and /dev/dri into benchmark + processing containers.
    header "  AMD — Docker container GPU support"
    _configure_docker_gpu_passthrough "AMD"
fi

# --- Intel: compute runtime + GPU tools ---
if ! $IS_WSL && $GPU_INTEL; then
    header "  Intel — compute runtime"

    if command -v clinfo &>/dev/null && clinfo 2>/dev/null | grep -q -i intel; then
        info "Intel compute runtime already installed"
    else
        warn "Installing Intel compute runtime..."
        _INTEL_LOG="/tmp/intel-gpu-install.log"
        if [[ "$DISTRO_LIKE" == *"debian"* ]] || [[ "$DISTRO_ID" == "ubuntu" ]] || [[ "$DISTRO_ID" == "debian" ]]; then
            # Retry once on transient mirror failure (e.g. security.ubuntu.com flap).
            pkg_update || { sleep 5; pkg_update || warn "apt-get update had warnings — continuing with cached indexes"; }
            pkg_install gpg wget curl
            # Add Intel's GPU repo defensively — probe the running codename, fall
            # back to a known-good one, or skip entirely (never poison apt for the
            # tailscale/mesh install that follows).
            if _add_intel_gpu_repo "$(lsb_release -cs 2>/dev/null || echo noble)"; then
                # If the freshly-added repo still breaks apt, drop it and retry
                # rather than leaving apt poisoned for later steps.
                pkg_update || { warn "apt-get update failed after adding Intel GPU repo — removing it"; _remove_intel_gpu_repos; pkg_update || true; }
                # Pre-remove obsolete packages — Intel's "unified" repo replaces
                # intel-level-zero-gpu → libze-intel-gpu1 and level-zero → libze1.
                # Without this, dpkg hits a file-conflict and prompts "Overwrite? (y/N)"
                # even with DEBIAN_FRONTEND=noninteractive (which doesn't suppress
                # dpkg file-overwrite prompts).
                apt-get remove -y intel-level-zero-gpu level-zero 2>/dev/null || true
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
                    -o Dpkg::Options::="--force-overwrite" \
                    intel-opencl-icd libze-intel-gpu1 libze1 \
                    libigc2 libigdfcl2 libigdgmm12 \
                    intel-media-va-driver-non-free intel-gpu-tools \
                    > "$_INTEL_LOG" 2>&1 \
                    || { warn "Intel GPU auto-install failed — see $_INTEL_LOG or install manually: https://dgpu-docs.intel.com"; }
            else
                warn "Skipped Intel GPU repo (unsupported codename) — compute runtime not installed"
            fi
        elif [[ "$DISTRO_LIKE" == *"rhel"* ]] || [[ "$DISTRO_LIKE" == *"fedora"* ]]; then
            dnf install -y -q intel-opencl intel-level-zero-gpu level-zero intel-gpu-tools 2>/dev/null \
                || { warn "Intel GPU auto-install failed — install manually"; }
        else
            warn "Cannot auto-install Intel GPU runtime on ${DISTRO_ID} — install manually"
        fi

        if command -v clinfo &>/dev/null; then
            info "Intel compute runtime installed"
        else
            warn "Intel compute runtime may need reboot or manual install"
        fi
    fi

    if [ -e /dev/dri ]; then
        info "Intel GPU device nodes present (/dev/dri)"
    else
        warn "/dev/dri missing — Intel GPU may not work in containers"
    fi

    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
        usermod -aG render "$SUDO_USER" 2>/dev/null || true
        usermod -aG video "$SUDO_USER" 2>/dev/null || true
        info "Added $SUDO_USER to render+video groups"
    fi

    # Docker container support for Intel GPUs — same CDI issue as AMD.
    header "  Intel — Docker container GPU support"
    _configure_docker_gpu_passthrough "Intel"
fi

# --- NPU detection (bare metal — WSL NPU already checked above) ---
if ! $GPU_NPU && [ -e /dev/accel/accel0 ]; then
    GPU_NPU=true
    info "NPU detected (/dev/accel)"
elif echo "$PCI_OUT" | grep -iq 'neural\|npu\|ai accelerator'; then
    GPU_NPU=true
    info "NPU detected (PCI)"
fi

# --- Hardware tags (ALL detected — a system can have multiple) ---
AUTO_HARDWARE_TAGS=""
if $GPU_NVIDIA; then AUTO_HARDWARE_TAGS="cuda"; fi
if $GPU_AMD;    then AUTO_HARDWARE_TAGS="${AUTO_HARDWARE_TAGS:+$AUTO_HARDWARE_TAGS,}rocm"; fi
if $GPU_INTEL;  then AUTO_HARDWARE_TAGS="${AUTO_HARDWARE_TAGS:+$AUTO_HARDWARE_TAGS,}intel"; fi
if $GPU_NPU;    then AUTO_HARDWARE_TAGS="${AUTO_HARDWARE_TAGS:+$AUTO_HARDWARE_TAGS,}npu"; fi
if [ -z "$AUTO_HARDWARE_TAGS" ]; then AUTO_HARDWARE_TAGS="cpu"; fi

info "Auto-detected hardware tags: $AUTO_HARDWARE_TAGS"
EXTRA_HARDWARE_TAGS=""
if [ -t 0 ]; then
    ask "Additional hardware tags (comma-separated, e.g. tensorrt,triton — leave blank to skip)" "" EXTRA_HARDWARE_TAGS
fi

# Combine auto + extra tags for the Go worker binary
ALL_HARDWARE_TAGS="$AUTO_HARDWARE_TAGS"
if [ -n "$EXTRA_HARDWARE_TAGS" ]; then
    ALL_HARDWARE_TAGS="${ALL_HARDWARE_TAGS},${EXTRA_HARDWARE_TAGS}"
fi

# ============================================================================
# Step 5 — Download worker binary (only after enrollment succeeds)
# ============================================================================
header "Step 5/8 — Worker binary"

mkdir -p "$INSTALL_DIR"

if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    warn "Stopping running worker before update..."
    systemctl stop "${SERVICE_NAME}"
    info "Worker stopped"
fi

BINARY_URL="${API_URL}/v1/workers/download/${OS}/${ARCH}"
info "Downloading from: $BINARY_URL"

if curl -fL --progress-bar --retry 3 --retry-delay 5 --max-time 300 -o "${INSTALL_DIR}/${BINARY_NAME}" "$BINARY_URL"; then
    chmod +x "${INSTALL_DIR}/${BINARY_NAME}"
    info "Binary installed: ${INSTALL_DIR}/${BINARY_NAME}"
else
    warn "The API has no worker build yet. On the manager run: docker compose restart mod-worker, wait for 'release published' in its log, then re-run this command."
    echo ""
    if [ -t 0 ]; then
        read -rp "  Press Enter once the binary is in place (or Ctrl-C to abort)..."
        if [ ! -x "${INSTALL_DIR}/${BINARY_NAME}" ]; then
            error "Binary not found at ${INSTALL_DIR}/${BINARY_NAME}"
            exit 1
        fi
        info "Binary found"
    else
        error "Binary not found at ${INSTALL_DIR}/${BINARY_NAME}"
        exit 1
    fi
fi

# ============================================================================
# Step 6 — Enroll (consume the token now that everything is set up)
# ============================================================================
header "Step 6/8 — Enrollment"

APPROVAL_CODE=""
BOOTSTRAP_TOKEN=""

info "Enrolling worker..."
# Retry connection errors and 5xx (API restart / proxy blip); 4xx is final.
ENROLL_BODY_FILE=$(mktemp)
ENROLL_CODE=000
ENROLL_RESPONSE=""
for ENROLL_TRY in 1 2 3 4 5; do
    ENROLL_CODE=$(curl -s -o "$ENROLL_BODY_FILE" -w "%{http_code}" -X POST "${API_URL}/v1/workers/enroll/start" \
        -H "Content-Type: application/json" \
        -d "{\"worker_name\": \"${WORKER_NAME}\", \"hostname\": \"$(hostname)\", \"enrollment_token\": \"${ENROLLMENT_TOKEN}\"}" 2>/dev/null) || ENROLL_CODE=000
    ENROLL_RESPONSE=$(cat "$ENROLL_BODY_FILE" 2>/dev/null || true)
    case "$ENROLL_CODE" in
        000|5??)
            [ "$ENROLL_TRY" -lt 5 ] && { warn "Enrollment attempt ${ENROLL_TRY} got HTTP ${ENROLL_CODE}, retrying..."; sleep $((ENROLL_TRY * 5)); } ;;
        *) break ;;
    esac
done
rm -f "$ENROLL_BODY_FILE"
case "$ENROLL_CODE" in 2??) ;; *) ENROLL_RESPONSE_ERR="$ENROLL_RESPONSE"; ENROLL_RESPONSE="" ;; esac

if [ -n "$ENROLL_RESPONSE" ]; then
    BOOTSTRAP_TOKEN=$(echo "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bootstrap_token',''))" 2>/dev/null || echo "")
    APPROVAL_CODE=$(echo "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('approval_code',''))" 2>/dev/null || echo "")
    ENROLL_STATUS=$(echo "$ENROLL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
fi

if [ -n "${BOOTSTRAP_TOKEN:-}" ]; then
    info "Enrolled successfully (status: ${ENROLL_STATUS:-unknown})"
    if [ "${ENROLL_STATUS}" = "pending" ] && [ -n "$APPROVAL_CODE" ]; then
        echo ""
        echo -e "  ${YELLOW}${BOLD}=== ACTION REQUIRED ===${NC}"
        echo -e "  Approve this worker in ${BOLD}Dashboard > Workers${NC}"
        echo -e "  Approval code: ${CYAN}${BOLD}${APPROVAL_CODE}${NC}"
        echo ""
    elif [ "${ENROLL_STATUS}" = "approved" ]; then
        info "Auto-approved — worker is ready to pull tasks"
    fi
else
    error "Enrollment failed (HTTP ${ENROLL_CODE})"
    if [ -n "${ENROLL_RESPONSE_ERR:-$ENROLL_RESPONSE}" ]; then
        echo "  Response: ${ENROLL_RESPONSE_ERR:-$ENROLL_RESPONSE}"
    fi
    exit 1
fi

# ============================================================================
# Step 6b — Mesh anchors (dual-homing). Homes this worker to the site and/or
# cloud control plane using the fields the enrollment response carried. A
# `local` worker gets a site key, a `remote` worker a cloud key, and a node
# meant to be dual-homed gets both; we bring up exactly the anchors we received.
# ============================================================================
header "Step 6b — Mesh anchors"

_mesh_field() { echo "$ENROLL_RESPONSE" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$1','') or '')" 2>/dev/null || echo ""; }
MESH_HOSTNAME="$(_mesh_field mesh_hostname)"; [ -z "$MESH_HOSTNAME" ] && MESH_HOSTNAME="$WORKER_NAME"
SITE_LOGIN_SERVER="$(_mesh_field site_login_server)";  SITE_AUTHKEY="$(_mesh_field site_authkey)"
CLOUD_LOGIN_SERVER="$(_mesh_field cloud_login_server)"; CLOUD_AUTHKEY="$(_mesh_field cloud_authkey)"

# The SITE plane is no longer a host daemon: the worker runs its own userspace
# site client container (mod-mesh-site) and enrolls it with a key the manager
# delivers on the heartbeat. A second kernel tailscaled beside the cloud one
# drops the other's inbound traffic (shared ts-input chain), so only the cloud
# anchor gets a host daemon here.
if [ -n "$CLOUD_AUTHKEY" ]; then
    if ! command -v tailscaled >/dev/null 2>&1; then
        # Preferred: the manager's pinned mirror (ensure-tailscale-mirror.sh) —
        # same version on every worker, and it needs only the manager reach we
        # already proved by downloading the worker binary. Official static
        # build, so no apt/repo involvement at all.
        TS_URL="${API_URL}/v1/workers/download/tailscale/linux/${ARCH}"
        TS_TMP="$(mktemp -d)"
        if curl -fsSL --retry 3 --retry-delay 5 --max-time 300 -o "${TS_TMP}/ts.tgz" "$TS_URL" \
                && tar -xzf "${TS_TMP}/ts.tgz" -C "$TS_TMP" \
                && install -m 0755 "$TS_TMP"/tailscale_*/tailscaled /usr/sbin/tailscaled \
                && install -m 0755 "$TS_TMP"/tailscale_*/tailscale /usr/bin/tailscale; then
            info "tailscale $(tailscale version 2>/dev/null | head -n1) installed from the manager mirror"
        else
            warn "manager tailscale mirror unavailable (${TS_URL}) — falling back to tailscale.com"
        fi
        rm -rf "$TS_TMP"
    fi
    if ! command -v tailscaled >/dev/null 2>&1; then
        info "Installing tailscale..."
        # The tailscale installer runs its own `apt-get update`, which a broken
        # optional third-party repo (e.g. an unsupported Intel GPU repo) would
        # fail. The mesh anchor is CRITICAL and the Intel repo is OPTIONAL, so
        # guarantee a clean apt state first; if the install still fails, drop the
        # optional repos and retry before giving up.
        _ensure_apt_healthy
        if ! { curl -fsSL https://tailscale.com/install.sh | sh; }; then
            warn "tailscale install failed — clearing optional third-party repos and retrying"
            _remove_intel_gpu_repos
            _ensure_apt_healthy
            curl -fsSL https://tailscale.com/install.sh | sh \
                || warn "tailscale install failed — mesh homing skipped"
        fi
    fi
    # We manage our own per-anchor daemons; stand down the distro default so it
    # cannot contend for the default tun/port. Only disables the stock unit.
    systemctl disable --now tailscaled.service 2>/dev/null || true
fi

# $1=plane(site|cloud) $2=login_server $3=authkey
bring_up_anchor() {
    local plane="$1" login="$2" authkey="$3"
    [ -n "$login" ] && [ -n "$authkey" ] || return 0
    command -v tailscaled >/dev/null 2>&1 || { warn "tailscaled missing — cannot home ${plane} anchor"; return 1; }
    local sock statedir tun port host
    if [ "$plane" = "site" ]; then
        sock=/run/tailscale/tailscaled-site.sock;  statedir=/var/lib/tailscale-site
        tun=mod-site;  port=41642; host="${MESH_HOSTNAME}-site"
    else
        sock=/run/tailscale/tailscaled-cloud.sock; statedir=/var/lib/tailscale-cloud
        tun=mod-cloud; port=41641; host="${MESH_HOSTNAME}"
    fi
    mkdir -p "$statedir" /run/tailscale
    cat > "/etc/systemd/system/mod-tailscaled-${plane}.service" << EOF
[Unit]
Description=MOD mesh tailscaled (${plane} anchor)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/sbin/tailscaled --state=${statedir}/tailscaled.state --statedir=${statedir} --socket=${sock} --port=${port} --tun=${tun}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "mod-tailscaled-${plane}.service"
    sleep 2
    # Bounded join: a dual-homed worker often can't reach one plane right now (a
    # remote worker's SITE anchor, or a local worker's cloud anchor behind a
    # firewall). tailscale up would BLOCK forever on an unreachable control server
    # and hang the whole setup — so time-box it. On timeout the daemon stays up
    # (Restart=always) and keeps retrying, so the anchor connects when it becomes
    # reachable (worker moves to the site LAN, or the manager roams into range).
    if timeout 45s tailscale --socket="${sock}" up --login-server="${login}" --authkey="${authkey}" \
            --hostname="${host}" --accept-dns=false --timeout=30s; then
        info "${plane} anchor joined (${login})"
    else
        warn "${plane} anchor not reachable yet — daemon stays up (Restart=always) and auto-reconnects when it is; re-run: tailscale --socket=${sock} up --login-server=${login} --authkey=<key>"
    fi
}

# A remote box (off-site, reached through a remote agent) often has NO path to
# the manager's PRIVATE site anchor — only the cloud anchor and the public API
# are reachable. Before we commit this worker to the site plane, PROBE whether
# the site login server is actually reachable from this box; if it is not, we
# skip the site-specific work (mDNS, local-CA fetch) and do NOT claim the anchor
# "will be joined" (that would be a false [OK]). The worker's managed site client
# will still pick it up automatically the first time the box CAN reach it.
SITE_ANCHOR_REACHABLE=false
if [ -n "$SITE_LOGIN_SERVER" ]; then
    if probe_site_reachable "$SITE_LOGIN_SERVER"; then
        SITE_ANCHOR_REACHABLE=true
    else
        note "Site anchor ${SITE_LOGIN_SERVER} not reachable from this host — remote worker, cloud anchor only"
    fi
fi

# Wire up mDNS so this box can resolve the manager by its stable name
# (mod-manager.local) — the site login server and the CA host below may both be
# expressed as that name rather than an IP, and the Go worker's automatic
# re-home dials it too. Only needed when there is a SITE anchor (a cloud-homed
# worker dials a fixed public name); harmless and idempotent when re-run.
if [ -n "$SITE_AUTHKEY" ] || [ -n "$SITE_LOGIN_SERVER" ]; then
    if [ "$SITE_ANCHOR_REACHABLE" = true ]; then
        ensure_mdns
    else
        note "Skipped mDNS (mod-manager.local) — site anchor not reachable from this host"
    fi
fi

# Trust the manager's local CA so tailscaled can validate the SITE control cert
# (headscale-tls, signed by the manager's internal CA). The CLOUD anchor uses a
# public cert and needs none. Fetched over plain HTTP from the site host — no
# trust is needed to obtain the trust anchor — and installed BEFORE the join so
# the site tailscaled (started below) reads it. Without this the site join fails
# TLS verification and blocks. No-op when there's no site anchor, and skipped
# (with one info line) when the site anchor is unreachable from this box.
if [ -n "$SITE_LOGIN_SERVER" ]; then
    if [ "$SITE_ANCHOR_REACHABLE" = true ]; then
        _ca_host="$(login_server_host "$SITE_LOGIN_SERVER")"
        if [ -n "$_ca_host" ] && curl -fsSL "http://${_ca_host}/mod-local-ca.crt" \
                -o /usr/local/share/ca-certificates/mod-local-ca.crt 2>/dev/null \
                && [ -s /usr/local/share/ca-certificates/mod-local-ca.crt ]; then
            update-ca-certificates >/dev/null 2>&1 || true
            info "Installed the manager's local CA (site control-plane trust)"
        else
            warn "Could not fetch the local CA from http://${_ca_host}/mod-local-ca.crt — the site anchor may fail TLS validation"
        fi
    else
        note "Skipped local-CA fetch — site anchor not reachable from this host"
    fi
fi

# Site anchor: handled by the worker itself (mod-mesh-site), not here. An older
# install's mod-tailscaled-site unit is retired by the worker on its first
# heartbeat, its node identity carried into the container. Only claim it "will be
# joined" when we actually verified reachability above — never print [OK] for
# something that was not verified.
if [ -n "$SITE_LOGIN_SERVER" ] && [ "$SITE_ANCHOR_REACHABLE" = true ]; then
    info "Site anchor (${SITE_LOGIN_SERVER}) will be joined by the worker's managed site client"
fi
bring_up_anchor cloud "$CLOUD_LOGIN_SERVER" "$CLOUD_AUTHKEY"

# ============================================================================
# Step 7 — Write config
# ============================================================================
header "Step 7/8 — Configuration file"

cat > "${INSTALL_DIR}/${CONFIG_FILE}" << EOF
worker_name: "${WORKER_NAME}"
mode: "remote"
api_url: "${API_URL}"
bootstrap_token: "${BOOTSTRAP_TOKEN}"
docker_network: "host"
heartbeat_interval: 15s
container_timeout: 30m
EOF

chmod 600 "${INSTALL_DIR}/${CONFIG_FILE}"
info "Config written: ${INSTALL_DIR}/${CONFIG_FILE}"

# ============================================================================
# Step 7 — systemd service
# ============================================================================
header "Step 8/8 — systemd service"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=MOD Worker Agent
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=${INSTALL_DIR}/${BINARY_NAME} --config ${INSTALL_DIR}/${CONFIG_FILE}
Environment=WORKER_HARDWARE_TAGS=${ALL_HARDWARE_TAGS}
Restart=always
RestartSec=10
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=300
WorkingDirectory=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
info "Service installed: ${SERVICE_NAME}"

systemctl start "${SERVICE_NAME}"
sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Worker is running!"
else
    error "Worker failed to start — check: journalctl -u ${SERVICE_NAME} -f"
    journalctl -u "${SERVICE_NAME}" --no-pager -n 20
    exit 1
fi

# ============================================================================
# Summary
# ============================================================================
NEEDS_REBOOT=false
# A reboot is only GENUINELY needed when the NVIDIA *kernel driver* isn't live
# yet — the native case where a freshly-installed driver leaves nvidia-smi unable
# to talk to the GPU until reboot. If the container toolkit already verified the
# --gpus path (NVIDIA_CTK_OK), GPU work is ready NOW — don't tell the user to
# reboot. On WSL2 the kernel driver lives on the Windows host, so a Linux reboot
# never helps (remediation there is Windows-side / 'wsl --shutdown'), and we
# never flag it here.
if $GPU_NVIDIA && ! $IS_WSL && ! $NVIDIA_CTK_OK \
   && ! (command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null); then
    NEEDS_REBOOT=true
fi

echo ""
echo -e "${CYAN}${BOLD}=====================================================${NC}"
echo -e "${CYAN}${BOLD}       Setup complete!${NC}"
echo -e "${CYAN}${BOLD}=====================================================${NC}"
echo ""
echo "  Worker:  $WORKER_NAME"
echo "  Mode:    remote"
echo "  API:     $API_URL"
echo "  Config:  ${INSTALL_DIR}/${CONFIG_FILE}"
echo "  Service: ${SERVICE_NAME}"
echo ""
if [ -n "$APPROVAL_CODE" ]; then
    echo -e "  ${YELLOW}${BOLD}>>> Approve in Dashboard > Workers (code: ${APPROVAL_CODE}) <<<${NC}"
    echo ""
fi
if $NEEDS_REBOOT; then
    echo -e "  ${YELLOW}${BOLD}>>> REBOOT REQUIRED for GPU drivers <<<${NC}"
    echo ""
fi
echo "  Commands:"
echo "    Status:   sudo systemctl status ${SERVICE_NAME}"
echo "    Logs:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "    Stop:     sudo systemctl stop ${SERVICE_NAME}"
echo "    Restart:  sudo systemctl restart ${SERVICE_NAME}"
echo "    Uninstall: sudo systemctl disable --now ${SERVICE_NAME} && sudo rm -rf ${INSTALL_DIR}"
echo ""
