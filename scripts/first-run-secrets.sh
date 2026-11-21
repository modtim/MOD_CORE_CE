#!/usr/bin/env bash
# =============================================================================
# first-run-secrets.sh — CE host-side first-run secrets generator (brief 1601).
#
# Run this BEFORE `docker compose up`, from open-core/overlay/ (safe to re-run;
# idempotent per key). It
# generates random values for every infra credential the compose file now
# requires (POSTGRES_PASSWORD, REDIS_PASSWORD, VERSITYGW_ROOT_ACCESS_KEY,
# VERSITYGW_ROOT_SECRET_KEY, KEYCLOAK_DB_PASSWORD, KEYCLOAK_ADMIN_PASSWORD,
# KEYCLOAK_REALM_ADMIN_PASSWORD — the seeded `modtex` realm's "admin" user,
# brief 1620, NOT the Keycloak master-realm bootstrap admin above)
# plus PLATFORM_SERVICE_API_KEY and SECRET_ENCRYPTION_KEY (a Fernet key, matching
# CORE/API/utils/encryption.py's generate_key()) and AUDIT_HMAC_KEY (a 32-byte
# hex key used by CORE/API/audit.py to HMAC-sign the audit-log hash chain —
# CMMC AU-9 integrity verification; brief 1622), and writes them to a local
# `.env` file at mode 0600 that `docker compose` auto-loads.
#
# Idempotent, PER KEY (2400CR1x): for each infra var this script owns it
# generates a value ONLY if that var is absent from `.env` (present-but-empty
# counts as absent); an existing value is never rewritten, so a retry after
# `keycloak/ensure-client-secrets.sh` — which writes only the 4 KC client
# secrets — fills the remaining infra keys without rotating the ones already
# in use. It never deletes or reorders lines: the existing bytes of `.env` are
# kept verbatim and the missing keys are appended. Every run also refreshes the
# 2400RES2b sizing block (CE_SIZE_PROFILE + the CE_* rails from
# open-core/overlay/profiles/<p>.env) exactly once — a re-run never duplicates
# a line, and any CE_* you already set stays yours. Added/kept key NAMES are
# printed; values never are.
# This is a SEPARATE, host-side, pre-`up` step from the in-stack
# `openbao`/`openbao-init` services (brief 1605) and does not read or write
# anything under their volumes.
#
# Never prints generated values to stdout/stderr.
# SIDECAR-J2: every run also calls scripts/first-run-sidecar.sh, which creates
# the two ciphertext dirs (VERSITYGW_CIPHER_PATH/CE_TENANT_SLUG with 0700,
# VALKEY_CIPHER_PATH/data with 0700) and the 0600 passphrase file ONCE. The
# passphrase is never printed and never goes into .env; the store entrypoint
# (not this script) runs `gocryptfs -init` on first start.
# 2400LR-fr1 (gate A pre-audit #5, #7): every run also appends DOCKER_GID (from
# /var/run/docker.sock) if absent — compose's worker runs `user: 0:${DOCKER_GID:-
# 1001}` and Ubuntu's docker gid is usually 988/999 — and WARNs (never exits
# non-zero) on host ports the compose publishes that `ss` says are in use.
# =============================================================================
# 2400CR1x: fail loud on a non-bash shell. `docker:dind` / Alpine have no bash,
# so `sh scripts/first-run-secrets.sh` used to die at the BASH_SOURCE line below
# ("bad substitution") — and, worse, SCRIPT_DIR came out EMPTY, so OVERLAY_DIR
# was the PARENT of the work dir and the script wrote .env THERE and still
# printed success. POSIX syntax only on the guard line, and it is the FIRST
# executable line so nothing bash-only runs before it.
[ -n "${BASH_VERSION:-}" ] || { echo "first-run-secrets.sh: needs bash (try: apk add bash / apt install bash)" >&2; exit 2; }

set -euo pipefail

die() {
  echo "first-run-secrets: ERROR: $1" >&2
  exit 1
}

# 2400LR-fr1 (#7): DOCKER_GID only if absent (same idiom as the infra keys: a
# present value is never rewritten); #5: WARN only, never exits non-zero.
note_docker_gid() {
  grep -qE '^DOCKER_GID=.' "$ENV_FILE" 2>/dev/null && return 0
  # CE_DOCKER_SOCK is a test hook (like ce-doctor's CE_DOCKER_BIN and ce-resize's
  # CE_MEMINFO_PATH): the socket path to inspect; never set it in production.
  docker_sock="${CE_DOCKER_SOCK:-/var/run/docker.sock}"
  if gid="$(stat -c %g "$docker_sock" 2>/dev/null)"; then
    printf 'DOCKER_GID=%s\n' "$gid" >> "$ENV_FILE"
    echo "docker gid: $gid (from /var/run/docker.sock)"
    # FIX-INSTALL-DOCKER-GROUP-WARN (S-inst-1): the socket EXISTS but this user
    # cannot write it (not in the docker group) and there is no usable sudo ->
    # docker compose will fail LATE (install.sh step 5b) with a raw error. WARN
    # here, early, with the fix (never exits non-zero: same policy as the WARNs
    # above, #5). `stat` above needs only directory permissions, so it succeeds
    # for a non-group user; the writability test is what catches that case.
    if [ ! -w "$docker_sock" ] && ! sudo -n true 2>/dev/null; then
      echo "first-run-secrets: WARN: /var/run/docker.sock is not readable by this user and there is no usable sudo — docker compose will fail. Fix: sudo usermod -aG docker \$USER then newgrp docker (or log out), then re-run this script."
    fi
  else
    echo "first-run-secrets: WARN: no /var/run/docker.sock — set DOCKER_GID in $ENV_FILE yourself (rootless Docker / userns-remap)."
  fi
}
# FIX-FIRST-RUN-SECRETS-OWN-PORT-WARN-SKIP: a re-run over a healthy install must
# not WARN about host ports THIS project's own containers already publish. Same
# logic as install.sh compose_project()/own_ports() (copied, not sourced). Every
# docker failure yields an EMPTY own list, so the foreign-port WARN still prints,
# and the `|| true` guards keep set -euo pipefail from aborting on it.
compose_project_name() { # COMPOSE_PROJECT_NAME (env, then .env), else docker-compose.yml `name:`, else the overlay dir name
  local n; n="${COMPOSE_PROJECT_NAME:-}"
  [ -n "$n" ] || n="$(grep "^COMPOSE_PROJECT_NAME=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  [ -n "$n" ] || n="$(grep -m1 '^name:' "$OVERLAY_DIR/docker-compose.yml" 2>/dev/null | cut -d: -f2- | tr -d ' ' || true)"
  [ -n "$n" ] || n="$(basename "$OVERLAY_DIR")"
  printf '%s' "${n:-mod-core-ce}"
}
own_ports() { # host ports published by THIS project's containers (running or not); empty on any docker failure
  local p; p="$(compose_project_name)"
  docker ps -a --format '{{.Ports}}' --filter "label=com.docker.compose.project=$p" 2>/dev/null \
    | tr ',' '\n' | grep -o '[0-9]\+->' | grep -o '[0-9]\+' | sort -u || true
}
note_host_ports() {
  # FIX-INSTALL-PORT-CHECKS (S-inst-2 fix 2): no `ss` used to be a SILENT skip -
  # the operator got no hint at all. Now a visible WARN (still advisory: returns
  # 0, same WARN-only policy as the DOCKER_GID / port WARNs above).
  command -v ss >/dev/null 2>&1 || {
    echo "first-run-secrets: WARN: no ss on this system — host-port check skipped; check with lsof -i -n -P before docker compose up"
    return 0
  }
  kcport="${KEYCLOAK_HOST_PORT:-$(grep -m1 '^KEYCLOAK_HOST_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)}"
  case "${kcport:-}" in (*[!0-9]*|'') kcport=8080 ;; esac
  # FIX-INSTALL-PORT-CHECKS (S-inst-2 fix 3): 3002 (the docs site, compose:1187)
  # was never scanned even though the README lists it among the ports that must
  # be free.
  local ours p
  ours="$(own_ports)"
  for p in 3000 3001 3002 5432 6379 7070 8000 "$kcport" 8200 8025; do
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q . || continue
    # FIX-FIRST-RUN-SECRETS-OWN-PORT-WARN-SKIP: held by THIS project's own
    # containers (a re-run over a healthy install) -> not a clash, no WARN.
    printf '%s\n' "$ours" | grep -q "^${p}$" && continue
    # FIX-INSTALL-PORT-CHECKS (S-inst-2 fix 1): the WARN named no fix and never
    # pointed at --keycloak-port.
    echo "WARN: host port $p is already in use; docker compose up will fail to bind it (stop that process; Keycloak: scripts/install.sh --keycloak-port N)"
  done
}
# SIDECAR-J2: the two ciphertext dirs and the 0600 passphrase file are created
# by scripts/first-run-sidecar.sh, which reads VERSITYGW_CIPHER_PATH /
# CE_TENANT_SLUG from the environment then from .env. Non-fatal: if the
# operator is not root and the paths need privilege, that script prints the exact
# one-line `sudo` command and exits non-zero — .env is still written, so the
# operator can run that line and re-run this script. Never prints the passphrase.
note_sidecar_paths() {
  [ -f "$SCRIPT_DIR/first-run-sidecar.sh" ] || {
    echo "first-run-secrets: WARN: scripts/first-run-sidecar.sh is missing — the cipher dirs and the 0600 passphrase file will not be created; copy the whole scripts/ tree."
    return 0
  }
  bash "$SCRIPT_DIR/first-run-sidecar.sh" || echo "first-run-secrets: WARN: the sidecar host paths are not ready — run the sudo line it printed, then re-run this script."
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$OVERLAY_DIR/.env"

# 2400CR1x: no silent wrong dir. If the bash-isms above ever resolve SCRIPT_DIR
# empty again, OVERLAY_DIR becomes the PARENT of the work dir and this script
# used to happily write .env there and print success. The overlay dir is the
# one that holds docker-compose.yml, so assert that before writing anything.
if [ ! -f "$OVERLAY_DIR/docker-compose.yml" ]; then
  echo "first-run-secrets: ERROR: no docker-compose.yml in $OVERLAY_DIR — this script must sit in the overlay's scripts/ directory (the one whose parent holds docker-compose.yml), not in a copy or a parent dir." >&2
  exit 2
fi

# =============================================================================
# 2400RES2b — CE sizing profiles + first-run auto-detect.
#
# The compose file's CE_* defaults are the SMALL profile, which is right for a
# 4 GB box but caps a big install (thousands of workers) far too low. So the
# first-run step also picks a profile — small / medium / large — from the host's
# RAM and CPU count, and appends that profile's CE_* lines to `.env`.
#
# Thresholds: < 7 GB -> small, < 24 GB -> medium, else large.
# `CE_SIZE=small|medium|large` in the environment overrides the pick (use it
# under Docker Desktop, where /proc/meminfo reports the HOST's RAM but the
# Docker VM's — usually smaller — is what the rails actually have to fit).
#
# User overrides win: any CE_* var already present in `.env` is skipped, never
# rewritten. The append is idempotent — a re-run adds nothing.
#
# CE_MEMINFO_PATH is a test hook: where to read MemTotal from (default
# /proc/meminfo). CE_CGROUP_ROOT (2400CR1y) is the same kind of hook for the
# cgroup tree (default /sys/fs/cgroup). Never set either in production.
# =============================================================================
MEMINFO_PATH="${CE_MEMINFO_PATH:-/proc/meminfo}"

# 2400CR1y: sizing is cgroup-aware — RAM/CPUs come from ONE shared helper,
# scripts/lib/ce-host-resources.sh (min(host, cgroup limit)), sourced here by
# first-run-secrets.sh, ce-resize.sh and ce-doctor.sh. No lib = no sizing
# guess: say so instead of silently using host numbers on a tighter box.
if [ ! -r "$SCRIPT_DIR/lib/ce-host-resources.sh" ]; then
  echo "first-run-secrets: ERROR: scripts/lib/ce-host-resources.sh missing next to this script — copy the whole scripts/ tree (including scripts/lib/), not just the .sh files." >&2
  exit 2
fi
# shellcheck source=./lib/ce-host-resources.sh
. "$SCRIPT_DIR/lib/ce-host-resources.sh"

apply_sizing() {
  # 2400CR1y: RAM/CPUs from the shared cgroup-aware helper (min of host
  # numbers and the cgroup limit), so a 4 GB / 2-CPU dind sizes `small`, not
  # the host's `large`.
  detect_host_resources "$MEMINFO_PATH"
  ram_gb="$HOST_RAM_GB"
  cpus="$HOST_CPUS"

  # Docker Desktop hint: /proc/meminfo on the macOS/Windows host reports the
  # HOST's RAM, but the rails live in the Docker VM. Say so, don't guess.
  if command -v docker >/dev/null 2>&1 && docker info --format '{{.MemoryTotal}}' >/dev/null 2>&1; then
    vm_bytes="$(docker info --format '{{.MemoryTotal}}' 2>/dev/null | tr -dc '0-9')"
    if [ -n "$vm_bytes" ] && [ "$((vm_bytes / 1073741824))" -ne "$ram_gb" ]; then
      echo "first-run-secrets: note — Docker reports $((vm_bytes / 1073741824)) GB for the container VM vs $ram_gb GB from /proc/meminfo; the VM RAM is what the CE_* rails must fit. Set CE_SIZE to pick the profile for the VM."
    fi
  fi

  # 2400RES2b/FIX-FIRST-RUN-SECRETS: an invalid CE_SIZE must WARN (the old
  # second case was unreachable — the first `*)` already set a valid profile).
  case "${CE_SIZE:-}" in
    small|medium|large) profile="$CE_SIZE"; picked="CE_SIZE override" ;;
    *)
      if [ -n "${CE_SIZE:-}" ]; then
        echo "first-run-secrets: CE_SIZE='${CE_SIZE:-}' is not small|medium|large — using auto-pick." >&2
      fi
      if [ "$ram_gb" -lt 7 ]; then profile=small
      elif [ "$ram_gb" -lt 24 ]; then profile=medium
      else profile=large; fi
      picked="auto"
      ;;
  esac

  PROFILE_FILE="$OVERLAY_DIR/profiles/$profile.env"
  [ -r "$PROFILE_FILE" ] || { echo "first-run-secrets: no profile file at $PROFILE_FILE — skipping sizing." >&2; return 0; }

  grep -q '^CE_SIZE_PROFILE=' "$ENV_FILE" 2>/dev/null && {
    echo "first-run-secrets: sizing already in $ENV_FILE — leaving it alone."
    return 0
  }

  # 2400RES2c: the profile lines live inside a marked block that scripts/ce-
  # resize.sh rewrites; anything you put BELOW the block is a manual override
  # and wins (compose keeps the LAST occurrence of a duplicate key) and
  # survives every resize.
  {
    echo ""
    echo "# >>> ce-size-profile (managed by scripts/ce-resize.sh — do not edit; put your own values below) >>>"
    echo "CE_SIZE_PROFILE=$profile"
    # Only KEY=VALUE lines; comments and blanks are dropped so .env stays flat.
    # FIX-FIRST-RUN-SECRETS: a profile with no CE_ lines must not abort the
    # script under set -euo pipefail (grep rc 1 in a pipeline).
    { grep -E '^CE_[A-Z_]+=' "$PROFILE_FILE" || true; } | while IFS= read -r line; do
      key="${line%%=*}"
      grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null && continue
      printf '%s\n' "$line"
    done
    echo "# <<< ce-size-profile <<<"
    echo "# manual per-service overrides (win over the profile; kept across resizes)"
  } >> "$ENV_FILE"

  [ -n "$HOST_CGROUP_NOTE" ] && echo "$HOST_CGROUP_NOTE"
  echo "sizing: $profile (RAM ${ram_gb} GB, ${cpus} CPUs) — edit CE_* in .env to override"
}

# 2400CR1x: per-key idempotence (was: "skip everything if .env exists"). README
# step 2, keycloak/ensure-client-secrets.sh, creates .env holding ONLY the 4
# KC client secrets; if step 1 failed and is retried after it, the old "already
# exists" early-exit left POSTGRES_PASSWORD / REDIS_PASSWORD / VERSITYGW_* /
# KEYCLOAK_DB_PASSWORD unset and compose kept failing. Now: for each infra var
# this script owns, generate it ONLY if the var is absent from .env (a present
# but EMPTY value counts as absent); an existing value is never rewritten.
# Prints which keys were added and which were kept — key NAMES only, never
# values.
# 2400GX-n3j: MOD_INGRESS_ASK_TOKEN is the stack-ingress edge's shared ask token
# (consumed by the stack-ingress caddy service (profile stack-ingress) and by the
# always-on openbao-init job (seeds the OpenBao ingress slot); the default is empty,
# so it is harmless when unset); plain hex via the default rand_hex path.
# FIX-REGISTRY-PASSWORD-GENERATED-PER-INSTALL (owner 11-12): the optional `registry`
# compose profile used to need a hand-written password, so a fixed example phrase
# from the docs could be a working password. It is now generated per install like
# every other infra secret (same rand_hex path, 64 hex chars - bcrypt in
# registry-init accepts it, and the registry's basic-auth password has no charset
# or length limits). An existing value in .env is kept on re-run, exactly as the
# neighbours. Key NAME only is ever printed.
INFRA_KEYS="POSTGRES_PASSWORD REDIS_PASSWORD VERSITYGW_ROOT_ACCESS_KEY VERSITYGW_ROOT_SECRET_KEY KEYCLOAK_DB_PASSWORD KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_REALM_ADMIN_PASSWORD SECRET_ENCRYPTION_KEY AUDIT_HMAC_KEY PLATFORM_SERVICE_API_KEY MOD_INGRESS_ASK_TOKEN REGISTRY_PASSWORD"

missing_keys=""
kept_keys=""
for _k in $INFRA_KEYS; do
  if [ -e "$ENV_FILE" ] && grep -qE "^${_k}=." "$ENV_FILE" 2>/dev/null; then
    kept_keys="$kept_keys $_k"
  else
    missing_keys="$missing_keys $_k"
  fi
done

if [ -z "$missing_keys" ]; then
  apply_sizing
  note_docker_gid
  note_host_ports
  note_sidecar_paths
  echo "first-run-secrets: $ENV_FILE already has every infra key — nothing generated."
  echo "first-run-secrets: kept (never rewritten):$kept_keys"
  exit 0
fi

command -v openssl >/dev/null 2>&1 || die "openssl is required but was not found on PATH. Install openssl (e.g. 'apt-get install openssl' or 'apk add openssl') and re-run this script."

# No Python dependency: every secret below is generated with openssl alone.
rand_hex() {
  openssl rand -hex 32
}

# A Fernet key is 32 random bytes, URL-safe base64 WITH padding (44 chars,
# ending in '='). This must stay byte-for-byte compatible with what
# CORE/API/utils/encryption.py's generate_key() (python cryptography's
# Fernet.generate_key()) produces, without requiring python3/cryptography
# on the host.
fernet_key() {
  if base64 --help 2>&1 | grep -q -- '-w'; then
    openssl rand 32 | base64 -w0 | tr '+/' '-_'
  else
    openssl rand 32 | base64 | tr -d '\n' | tr '+/' '-_'
  fi
}

# 2400CR1x: generate ONLY the keys that are missing from .env (see missing_keys
# above), so a retry after ensure-client-secrets.sh fills the gaps without
# touching the KC values it already wrote.
# 2400CR2g: the platform-service API key. database.py's seed_service_user() only
# needs a non-empty value (it stores key_prefix=<first 8 chars> + a hash), and
# crud/api_key_cud.py:get_api_key_by_key_prefix() resolves an `sk_...` key by its
# first 8 characters, so the literal prefix must stay `sk_` and the body must be
# url-safe (no '/' or '+', which would break the compose `${VAR}` interpolation
# and the .env quoting). Shape: sk_ + 43 chars
# requested, 42 in practice (base64 of 32 bytes is 42 payload chars + "==", and
# the tr strips the padding) — exactly what `openssl rand -base64 32 | tr -d
# '/+=' | cut -c1-43` yields.
platform_service_api_key() {
  if base64 --help 2>&1 | grep -q -- '-w'; then
    printf 'sk_%s' "$(openssl rand 32 | base64 -w0 | tr -d '/+=' | cut -c1-43)"
  else
    printf 'sk_%s' "$(openssl rand 32 | base64 | tr -d '/+=\n' | cut -c1-43)"
  fi
}

gen_value() { # gen_value <KEY> -> random value on stdout
  case "$1" in
    SECRET_ENCRYPTION_KEY) fernet_key ;;
    PLATFORM_SERVICE_API_KEY) platform_service_api_key ;;
    *) rand_hex ;;
  esac
}

# Register the EXIT cleanup BEFORE any temp file exists, so an abort at any
# point after this line (including inside the generate loop) removes both temp
# files. Empty values make the rm a no-op until mktemp has set them.
NEW_KEYS_FILE=""
TMP_ENV=""
trap 'rm -f "$TMP_ENV" "$NEW_KEYS_FILE"' EXIT

NEW_KEYS_FILE="$(mktemp "${OVERLAY_DIR}/.env.new.XXXXXX")"
chmod 0600 "$NEW_KEYS_FILE"
for _k in $missing_keys; do
  v_val="$(gen_value "$_k")" || { rm -f "$NEW_KEYS_FILE"; die "failed to generate $_k - check that openssl is on PATH (command -v openssl) and there is free disk (df -h), then run scripts/first-run-secrets.sh again."; }
  [ -n "$v_val" ] || { rm -f "$NEW_KEYS_FILE"; die "generated $_k is empty - check openssl version and free disk, then run scripts/first-run-secrets.sh again."; }
  if [ "$_k" = SECRET_ENCRYPTION_KEY ]; then
    if [ "${#v_val}" -ne 44 ] || [ "${v_val: -1}" != "=" ]; then
      rm -f "$NEW_KEYS_FILE"
      die "generated SECRET_ENCRYPTION_KEY is not a valid 44-char Fernet key - run scripts/first-run-secrets.sh again (the key is regenerated each run); if it repeats, check openssl version (openssl version)."
    fi
  fi
  printf '%s=%s\n' "$_k" "$v_val" >> "$NEW_KEYS_FILE"
done

TMP_ENV="$(mktemp "${OVERLAY_DIR}/.env.XXXXXX")"

if [ -e "$ENV_FILE" ]; then
  # Keep the existing file byte-for-byte, then append the missing keys.
  cat "$ENV_FILE" > "$TMP_ENV"
else
  {
    echo "# Generated by first-run-secrets.sh — do not commit. Re-running this"
    echo "# script only fills in missing keys; existing values are never rewritten."
  } > "$TMP_ENV"
fi
if [ -s "$NEW_KEYS_FILE" ]; then cat "$NEW_KEYS_FILE" >> "$TMP_ENV"; fi

chmod 0600 "$TMP_ENV"
mv "$TMP_ENV" "$ENV_FILE"
rm -f "$NEW_KEYS_FILE"
trap - EXIT

apply_sizing

note_docker_gid
note_host_ports
note_sidecar_paths

if [ -z "$kept_keys" ]; then
  echo "first-run-secrets: generated $ENV_FILE (mode 0600). Now run: docker compose up"
else
  echo "first-run-secrets: filled the missing keys in $ENV_FILE (mode 0600). Now run: docker compose up"
fi
echo "first-run-secrets: added:$missing_keys"
[ -n "$kept_keys" ] && echo "first-run-secrets: kept (never rewritten):$kept_keys"
echo "first-run-secrets: your sign-in password for admin@core.local is stored in $ENV_FILE as KEYCLOAK_REALM_ADMIN_PASSWORD (you can change it later in the Keycloak account console)."
