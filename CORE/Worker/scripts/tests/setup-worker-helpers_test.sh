#!/usr/bin/env bash
# Unit tests for the pure helpers in setup-worker.sh:
#   - derive_worker_name / _is_generic_hostname (unique worker name derivation)
#   - probe_site_reachable        (site-anchor reachability gate)
#   - login_server_host           (URL -> bare host)
#
# The script's main body cannot be sourced (it enrolls + installs), so we pull
# the contiguous block of helper definitions out of the real file and eval just
# those, so these tests exercise the actual shipped code, not a copy.
#
# Run:  bash scripts/tests/setup-worker-helpers_test.sh   (exit 0 = pass)

set -u
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/setup-worker.sh"
[ -f "$SCRIPT" ] || { echo "FAIL: cannot find $SCRIPT"; exit 1; }

# Pull from _is_generic_hostname up to (not including) ask() — that block is
# exactly the helper functions under test, contiguous and side-effect free.
FUNCS="$(awk '/^_is_generic_hostname\(\) *\{/{f=1} /^ask\(\) *\{/{f=0} f' "$SCRIPT")"
[ -n "$FUNCS" ] || { echo "FAIL: could not extract helper functions"; exit 1; }
eval "$FUNCS"

pass=0; fail=0
check() { # desc expected actual
    if [ "$2" = "$3" ]; then pass=$((pass+1));
    else fail=$((fail+1)); echo "FAIL: $1 -> expected [$2] got [$3]"; fi
}
check_rc() { # desc expected_rc actual_rc
    if [ "$2" = "$3" ]; then pass=$((pass+1));
    else fail=$((fail+1)); echo "FAIL: $1 -> expected rc=$2 got rc=$3"; fi
}

MID="$(mktemp)"
trap 'rm -f "$MID"' EXIT
printf 'abcdef12-34567890-1234-5678\n' > "$MID"   # hex chars: abcdef12...

# --- derive_worker_name ----------------------------------------------------
export MOD_MACHINE_ID_FILE="$MID"
check "non-generic hostname keeps worker-<host>" "worker-gpu-box-01" \
    "$(derive_worker_name gpu-box-01)"
check "localhost derives from machine-id (first 8 hex)" "worker-abcdef12" \
    "$(derive_worker_name localhost)"
check "localhost.localdomain derives from machine-id" "worker-abcdef12" \
    "$(derive_worker_name localhost.localdomain)"
check "empty hostname derives from machine-id" "worker-abcdef12" \
    "$(derive_worker_name '')"
check "generic distro name (ubuntu) derives from machine-id" "worker-abcdef12" \
    "$(derive_worker_name ubuntu)"
MID2="$(mktemp)"; trap 'rm -f "$MID" "$MID2"' EXIT
printf '00000000000000000000000000000000\n' > "$MID2"
check "two boxes, same hostname, different machine-id -> different names" \
    "worker-00000000" "$(MOD_MACHINE_ID_FILE="$MID2" derive_worker_name localhost)"

# no machine-id available -> fall back to the hostname (not blank)
export MOD_MACHINE_ID_FILE="/nonexistent/path/machine-id"
check "generic hostname, no machine-id -> worker-<hostname>" "worker-localhost" \
    "$(derive_worker_name localhost)"
check "generic empty hostname, no machine-id -> worker-unknown" "worker-unknown" \
    "$(derive_worker_name '')"

# --- is_reserved_worker_name (manager-node worker identity) ----------------
check_rc "mod-worker-embedded is reserved" 0 "$(is_reserved_worker_name mod-worker-embedded; echo $?)"
check_rc "legacy mod-worker-ce is reserved" 0 "$(is_reserved_worker_name mod-worker-ce; echo $?)"
check_rc "reserved check is case-insensitive" 0 "$(is_reserved_worker_name Mod-Worker-Embedded; echo $?)"
check_rc "normal derived name is not reserved" 1 "$(is_reserved_worker_name worker-gpu-box-01; echo $?)"
check_rc "similar name is not reserved" 1 "$(is_reserved_worker_name mod-worker-embedded2; echo $?)"
check_rc "empty name is not reserved" 1 "$(is_reserved_worker_name ''; echo $?)"

# --- login_server_host -----------------------------------------------------
check "https URL -> bare host (port dropped)" "203.0.113.243" \
    "$(login_server_host 'https://203.0.113.243:8443')"
check "http URL, no port -> host" "mod-manager.local" \
    "$(login_server_host 'http://mod-manager.local')"
check "URL with path+port -> bare host" "10.0.0.5" \
    "$(login_server_host 'https://10.0.0.5:8443/headscale')"

# --- probe_site_reachable (the reachability gate the skip depends on) ------
check_rc "empty URL is unreachable" 1 "$(probe_site_reachable '' >/dev/null 2>&1; echo $?)"
# An unresolvable name fails fast (DNS) -> reported unreachable (rc=1). This is
# the branch the installer takes for a private anchor a remote box cannot reach,
# and it must not print [OK] upstream.
check_rc "unresolvable site anchor is unreachable" 1 \
    "$(probe_site_reachable 'https://no-such-mod-anchor-zz.invalid:8443' >/dev/null 2>&1; echo $?)"

# --- _pin_nvidia_driver_packages (isolated extract) --------------------------
# This function sits between ask() and _setup_nvidia_container_toolkit so it
# is not included in the awk extraction above. We extract it separately.
PIN_FUNCS="$(awk '/^_pin_nvidia_driver_packages\(\) *\{/{f=1} /^_setup_nvidia_container_toolkit\(\) *\{/{f=0} f' "$SCRIPT")"
[ -n "$PIN_FUNCS" ] || { echo "FAIL: could not extract _pin_nvidia_driver_packages"; exit 1; }

pass_pin=0; fail_pin=0
pin_check() { # desc expected actual
    if [ "$2" = "$3" ]; then pass_pin=$((pass_pin+1));
    else fail_pin=$((fail_pin+1)); echo "FAIL: $1 -> expected [$2] got [$3]"; fi
}

# 1. Early-return guard for non-apt systems
pin_check "has apt-get guard" "1" "$(echo "$PIN_FUNCS" | grep -c 'command -v apt-get')"

# 2. Blacklist file path present
pin_check "writes 50mod-nvidia-hold" "0" "$(echo "$PIN_FUNCS" | grep -q '/etc/apt/apt.conf.d/50mod-nvidia-hold' 2>&1; echo $?)"

# 3. Package-Blacklist stanza exists
pin_check "has Package-Blacklist" "1" "$(echo "$PIN_FUNCS" | grep -c 'Package-Blacklist')"

# Isolate just the heredoc stanza (the actual apt-config entries written to
# disk) so checks below aren't confused by comment text mentioning the same
# substrings.
HEREDOC="$(echo "$PIN_FUNCS" | awk '/<<.?EOF.?$/{f=1;next} /^EOF$/{f=0} f')"
[ -n "$HEREDOC" ] || { echo "FAIL: could not extract blacklist heredoc"; exit 1; }

# 4. Blacklist entries present in heredoc are driver-specific, not blanket
#    nvidia-.* / libnvidia-.* (those would also freeze nvidia-container-toolkit
#    and libnvidia-container security updates).
pin_check "blacklist has nvidia-driver-.*" "0" "$(echo "$HEREDOC" | grep -q '"nvidia-driver-\.\*"'; echo $?)"
pin_check "blacklist has narrowed libnvidia entry (not container)" "0" "$(echo "$HEREDOC" | grep -q 'libnvidia-(?!container)'; echo $?)"
pin_check "blacklist has cuda-drivers.*" "0" "$(echo "$HEREDOC" | grep -q '"cuda-drivers\.\*"'; echo $?)"
pin_check "blacklist does not have bare nvidia-.*" "1" "$(echo "$HEREDOC" | grep -q '"nvidia-\.\*"'; echo $?)"
pin_check "blacklist does not have bare libnvidia-.*" "1" "$(echo "$HEREDOC" | grep -q '"libnvidia-\.\*"'; echo $?)"

# 5. dpkg + xargs apt-mark hold pipeline exists, and explicitly excludes
#    nvidia-container-toolkit / libnvidia-container from the hold pattern.
pin_check "has apt-mark hold pipeline" "0" "$(echo "$PIN_FUNCS" | grep -q 'xargs -r apt-mark hold'; echo $?)"
pin_check "excludes nvidia-container-toolkit from hold" "0" "$(echo "$PIN_FUNCS" | grep -q "_exclude_pattern=.*nvidia-container-toolkit"; echo $?)"
pin_check "excludes libnvidia-container from hold" "0" "$(echo "$PIN_FUNCS" | grep -q "_exclude_pattern=.*libnvidia-container"; echo $?)"

# Summary
echo "----"
echo "setup-worker helpers: ${pass} passed, ${fail} failed"
echo "pin_nvidia: ${pass_pin} passed, ${fail_pin} failed"
[ "$fail" -eq 0 ] && [ "$fail_pin" -eq 0 ]
