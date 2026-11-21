#!/usr/bin/env bash
# =============================================================================
# ce-resize.sh — 2400RES2c: switch the CE sizing profile and/or pin exact
# per-service amounts, without ever touching your secrets.
#
#   ce-resize.sh show
#   ce-resize.sh <small|medium|large|auto>
#   ce-resize.sh set <service>=<amount> [<service>=<amount> ...]
#   ce-resize.sh unset <service> [<service> ...]
#
# Exit: 0 ok; 1 = fatal error; 2 = usage error
#
# `.env` layout (see first-run-secrets.sh, which writes the same block):
#
#   # >>> ce-size-profile (managed by scripts/ce-resize.sh — do not edit; put
#       your own values below) >>>
#   CE_SIZE_PROFILE=small
#   CE_API_MEM=1.5g
#   ...
#   # <<< ce-size-profile <<<
#   # manual per-service overrides (win over the profile; kept across resizes)
#   CE_API_MEM=6g
#
# Docker Compose's `.env` reader keeps the LAST occurrence of a duplicated key
# (verified on the test VM: `docker compose config` on a two-occurrence .env
# prints the later value), so the manual line below the block wins over the
# profile line inside it. ce-resize.sh also omits a profile line whenever that
# key has a manual override, so the result is identical even on a reader that
# kept the FIRST occurrence.
#
# Every write backs `.env` up to `.env.bak.<epoch>` first, rewrites ONLY the
# managed block, keeps manual overrides across resizes, migrates a pre-block
# (2400RES2b) `.env` idempotently, and never prints or rewrites a non-CE_*
# line — your secrets stay byte-identical.
#
# This script never runs `docker compose`. After it prints the new rails, apply
# them yourself with: docker compose up -d
#
# CE_MEMINFO_PATH is a test hook (where to read MemTotal from); CE_CGROUP_ROOT
# (2400CR1y) is the same kind of hook for the cgroup tree. Never set either in
# production.
# =============================================================================
# 2400CR1x: fail loud on a non-bash shell (this script uses ${BASH_SOURCE[0]}
# below; busybox/dind `sh` chokes on it and SCRIPT_DIR used to come out empty,
# pointing OVERLAY_DIR at the wrong dir). POSIX-only guard, first executable
# line.
[ -n "${BASH_VERSION:-}" ] || { echo "ce-resize.sh: needs bash (try: apk add bash / apt install bash)" >&2; exit 2; }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$OVERLAY_DIR/.env"
MEMINFO_PATH="${CE_MEMINFO_PATH:-/proc/meminfo}"

# 2400CR1y: sizing is cgroup-aware — RAM/CPUs come from ONE shared helper
# (scripts/lib/ce-host-resources.sh) sourced by first-run-secrets.sh, ce-resize.sh
# and ce-doctor.sh. No lib = no sizing guess: say so, don't use host numbers.
if [ ! -r "$SCRIPT_DIR/lib/ce-host-resources.sh" ]; then
  echo "ce-resize: ERROR: scripts/lib/ce-host-resources.sh missing next to this script — copy the whole scripts/ tree (including scripts/lib/), not just the .sh files." >&2
  exit 2
fi
# shellcheck source=./lib/ce-host-resources.sh
. "$SCRIPT_DIR/lib/ce-host-resources.sh"

BEGIN_MARK='# >>> ce-size-profile (managed by scripts/ce-resize.sh — do not edit; put your own values below) >>>'
END_MARK='# <<< ce-size-profile <<<'
MANUAL_MARK='# manual per-service overrides (win over the profile; kept across resizes)'

die() { echo "ce-resize: ERROR: $1" >&2; exit 1; }
note() { echo "ce-resize: $1" >&2; }

# Friendly service name -> CE_* var.
svc_var() {
  case "$1" in
    api)              echo CE_API_MEM ;;
    api-workers)      echo CE_API_WORKERS ;;
    keycloak)         echo CE_KC_MEM ;;
    keycloak-heap)    echo CE_KC_HEAP ;;
    postgres)         echo CE_PG_MEM ;;
    postgres-buffers) echo CE_PG_SHARED_BUFFERS ;;
    kc-postgres)      echo CE_KCDB_MEM ;;
    worker)           echo CE_WORKER_MEM ;;
    storage)          echo CE_VGW_MEM ;;
    openbao)          echo CE_OPENBAO_MEM ;;
    valkey)           echo CE_VALKEY_MEM ;;
    dashboard)        echo CE_DASH_MEM ;;
    docs|mod-docs)    echo CE_DOCS_MEM ;;
    buildkitd)        echo CE_BUILDKIT_MEM ;;
    caddy)            echo CE_CADDY_MEM ;;
    registry)         echo CE_REGISTRY_MEM ;;
    workflowbuilder)  echo CE_WB_MEM ;;
    mailpit)          echo CE_MAILPIT_MEM ;;
    # buildkitd-net has NO knob: its mem_limit is a fixed literal (docker-compose.yml
    # mem_limit: 0.03g). Do not map it to CE_BUILDKIT_MEM — that sizes the OTHER
    # buildkit container. The doctor says "no resize variable for buildkitd-net".
    init|registry-init|openbao-init|keycloak-admin-init) echo CE_INIT_MEM ;;
    migrate|mod-migrate)          echo CE_API_MEM ;;
    node-repair|ce-node-repair)   echo CE_API_MEM ;;
    db-owner-repair)              echo CE_PG_MEM ;;
    *)                return 1 ;;
  esac
}

# Shared rails: one CE_* var sizes more than one container, so one `set` line
# moves several. Never hide that — ce-resize prints it and the doctor puts it in
# the fix line. Keyed by BOTH the friendly name and the compose service name.
rail_partners() { # rail_partners <service> -> other services on the same var
  case "$1" in
    api)                            echo "mod-migrate, ce-node-repair" ;;
    migrate|mod-migrate)            echo "api, ce-node-repair" ;;
    node-repair|ce-node-repair)     echo "api, mod-migrate" ;;
    postgres|postgres-app)          echo "db-owner-repair" ;;
    db-owner-repair)                echo "postgres (postgres-app)" ;;
    init)                           echo "openbao-init, keycloak-admin-init, registry-init" ;;
    registry-init)                  echo "openbao-init, keycloak-admin-init" ;;
    openbao-init)                   echo "keycloak-admin-init, registry-init" ;;
    keycloak-admin-init)            echo "openbao-init, registry-init" ;;
    *)                              return 0 ;;
  esac
}

MEM_RE='^[0-9]+(\.[0-9]+)?[mg]$'

valid_value() { # valid_value <service> <value>
  local value="$2"
  case "$1" in
    api-workers) [[ "$value" =~ ^[0-9]+$ ]] ;;
    # -Xmx640m is what the profile ships; a bare 640m is accepted too and
    # gets the -Xmx prefix added.
    keycloak-heap) [[ "$value" =~ ^(-Xmx)?[0-9]+(\.[0-9]+)?[mg]$ ]] ;;
    postgres-buffers) [[ "$value" =~ ^[0-9]+(\.[0-9]+)?([mMgG][bB]?|B)$ ]] ;;
    *) [[ "$value" =~ $MEM_RE ]] ;;
  esac
}

# Same thresholds as first-run-secrets.sh apply_sizing(): < 7 GB -> small,
# < 24 GB -> medium, else large. Sets DETECT_PROFILE / DETECT_RAM_GB.
detect_profile() {
  # 2400CR1y: min(host, cgroup limit) via the shared helper, so a 4 GB / 2-CPU
  # dind gets `small` (and ce-resize's ram_warn sees 4 GB, not the host's 125).
  # DETECT_CGROUP_NOTE is the one-line "cgroup limit ... (host ...)" note, empty
  # when no cgroup limit won.
  detect_host_resources "$MEMINFO_PATH"
  local ram_gb="$HOST_RAM_GB"
  if [ "$ram_gb" -eq 0 ]; then
    echo "ce-resize: WARN: RAM detection failed (HOST_RAM_GB=0); chose small" >&2
    echo "ce-resize:   override: set CE_SIZE_PROFILE=medium|large in .env before resizing" >&2
  fi
  if [ "$ram_gb" -lt 7 ]; then DETECT_PROFILE=small
  elif [ "$ram_gb" -lt 24 ]; then DETECT_PROFILE=medium
  else DETECT_PROFILE=large; fi
  DETECT_RAM_GB="$ram_gb"
  DETECT_CGROUP_NOTE="$HOST_CGROUP_NOTE"
}

# profile_lines <profile> -> KEY=VALUE lines only, in file order.
profile_lines() {
  local f="$OVERLAY_DIR/profiles/$1.env"
  [ -r "$f" ] || return 1
  grep -E '^CE_[A-Z_]+=' "$f"
}

gb_of() { # gb_of <memvalue> -> GB as an integer thousandths (0 if not a mem)
  local num mult int frac
  case "$1" in
    *g) num="${1%g}"; mult=1000 ;;
    *m) num="${1%m}"; mult=1 ;;
    *) echo 0; return ;;
  esac
  int="${num%%.*}"
  if [ "$int" = "$num" ]; then frac=0; else frac="${num#*.}"; fi
  frac="${frac}000"; frac="${frac:0:3}"
  echo $(( ${int:-0} * mult + frac * mult / 1000 ))
}

# ---------------------------------------------------------------------------
# Reading .env: HEAD_LINES = everything outside the managed block and the
# manual region (secrets live here, copied verbatim); MANUAL_LINES = KEY=VALUE
# manual overrides in order. A pre-block (2400RES2b) file is migrated here:
# CE_* lines that still equal the old profile's value are dropped (they move
# into the regenerated block); anything else is a manual override and stays.
# ---------------------------------------------------------------------------
declare -a HEAD_LINES=() MANUAL_LINES=()
HAS_BLOCK=0
OLD_PROFILE=""

# Old-profile value lookup: value_of_old <KEY> <old_profile> -> value or empty.
# Replaces the bash 3.2-incompatible `declare -A` used in 2400RES2c initial draft.
value_of_old() {
  local key="$1" oldprof="$2" line lk lv
  if [ -r "$OVERLAY_DIR/profiles/$oldprof.env" ]; then
    while IFS= read -r line; do
      lk="${line%%=*}"; lv="${line#*=}"
      if [ "$lk" = "$key" ]; then printf '%s' "$lv"; return 0; fi
    done < <(profile_lines "$oldprof")
  fi
  return 1
}

# Fail closed BEFORE any rewrite. The managed block must be 0 or 1 BEGIN and 0 or
# 1 END marker, with END after BEGIN. A lone or duplicated marker makes the read
# loop in load_env swallow everything after it, which drops non-CE secrets from
# the rewrite (audit row 5, S-script-ce-resize-leg1). Refusing here writes nothing.
check_managed_markers() {
  local nb ne bl el src
  src="$(tr -d '\r' < "$ENV_FILE")"
  nb="$(printf '%s\n' "$src" | grep -c -F -x -- "$BEGIN_MARK" || true)"
  ne="$(printf '%s\n' "$src" | grep -c -F -x -- "$END_MARK" || true)"
  if [ "$nb" -gt 1 ] || [ "$ne" -gt 1 ]; then
    die "refusing to rewrite .env: duplicated managed-block marker (BEGIN=$nb END=$ne); .env untouched, fix it by hand: find them with grep -n 'ce-size-profile' $ENV_FILE, then run scripts/ce-resize.sh again"
  fi
  if [ "$nb" != "$ne" ]; then
    die "refusing to rewrite .env: managed-block markers unbalanced (BEGIN=$nb END=$ne); .env untouched, fix it by hand: find them with grep -n 'ce-size-profile' $ENV_FILE, then run scripts/ce-resize.sh again"
  fi
  [ "$nb" = 1 ] || return 0
  bl="$(printf '%s\n' "$src" | grep -n -F -x -- "$BEGIN_MARK" | cut -d: -f1)"
  el="$(printf '%s\n' "$src" | grep -n -F -x -- "$END_MARK" | cut -d: -f1)"
  [ "$el" -gt "$bl" ] || die "refusing to rewrite .env: managed-block END marker precedes BEGIN; .env untouched, fix it by hand: find them with grep -n 'ce-size-profile' $ENV_FILE, then run scripts/ce-resize.sh again"
}

load_env() {
  HEAD_LINES=(); MANUAL_LINES=(); HAS_BLOCK=0
  [ -f "$ENV_FILE" ] || return 0
  check_managed_markers
  # Strip CRLF so marks and KEY=VALUE comparisons work on Windows-saved files.
  local tmpf; tmpf="$(mktemp "${OVERLAY_DIR}/.env.strip.XXXXXX")"
  tr -d '\r' < "$ENV_FILE" > "$tmpf"
  local line key value i
  local oldprof=""
  oldprof="$(grep -m1 '^CE_SIZE_PROFILE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "$oldprof" ] || oldprof="$(detect_profile 2>/dev/null; echo "$DETECT_PROFILE")"
  OLD_PROFILE="$oldprof"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$BEGIN_MARK" ]; then
      HAS_BLOCK=1
      while IFS= read -r line || [ -n "$line" ]; do
        [ "$line" = "$END_MARK" ] && break
      done
      continue
    fi
    [ "$line" = "$MANUAL_MARK" ] && continue
    if [[ "$line" =~ ^CE_[A-Z_]+= ]]; then
      key="${line%%=*}"; value="${line#*=}"
      [ "$key" = CE_SIZE_PROFILE ] && continue
      if [ "$HAS_BLOCK" = 0 ]; then
        old_val="$(value_of_old "$key" "$oldprof" || true)"
        [ "$old_val" = "$value" ] && continue  # matches old profile -> belongs in block
      fi
      for i in $(manual_indices); do
        if [ "${MANUAL_LINES[$i]%%=*}" = "$key" ]; then MANUAL_LINES[$i]="$key=$value"; continue 2; fi
      done
      MANUAL_LINES+=("$key=$value")
    else
      HEAD_LINES+=("$line")
    fi
  done < "$tmpf"
  rm -f "$tmpf"
}

manual_indices() { # indices of MANUAL_LINES, safe when empty
  [ "${#MANUAL_LINES[@]}" -gt 0 ] || return 0
  printf '%s\n' "${!MANUAL_LINES[@]}"
}

manual_get() { # manual_get <KEY> -> value or empty
  local i
  for i in $(manual_indices); do
    [ "${MANUAL_LINES[$i]%%=*}" = "$1" ] && { echo "${MANUAL_LINES[$i]#*=}"; return 0; }
  done
  return 1
}

manual_drop() { # manual_drop <KEY>
  local i out=()
  for i in "${MANUAL_LINES[@]}"; do [ "${i%%=*}" = "$1" ] || out+=("$i"); done
  MANUAL_LINES=("${out[@]+"${out[@]}"}")
}

manual_set() { # manual_set <KEY> <value>
  local i
  for i in $(manual_indices); do
    if [ "${MANUAL_LINES[$i]%%=*}" = "$1" ]; then MANUAL_LINES[$i]="$1=$2"; return; fi
  done
  MANUAL_LINES+=("$1=$2")
}

backup_env() {
  local bak="$ENV_FILE.bak.$(date +%s)"
  [ -e "$bak" ] && bak="$bak.$$"
  cp -p "$ENV_FILE" "$bak"
  note "backed up .env -> $(basename "$bak")"
}

write_env() { # write_env <profile> — rebuild head + block + manual region
  local profile="$1" line key tmp
  tmp="$ENV_FILE.tmp.$$"
  {
    for line in ${HEAD_LINES[@]+"${HEAD_LINES[@]}"}; do printf '%s\n' "$line"; done
    printf '%s\n' "$BEGIN_MARK"
    printf 'CE_SIZE_PROFILE=%s\n' "$profile"
    while IFS= read -r line; do
      key="${line%%=*}"
      manual_get "$key" >/dev/null && continue   # the manual line below wins
      printf '%s\n' "$line"
    done < <(profile_lines "$profile")
    printf '%s\n' "$END_MARK"
    printf '%s\n' "$MANUAL_MARK"
    for line in ${MANUAL_LINES[@]+"${MANUAL_LINES[@]}"}; do printf '%s\n' "$line"; done
  } > "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

effective_value() { # effective_value <KEY> <profile>
  local v
  if v="$(manual_get "$1")"; then printf '%s' "$v"; return; fi
  profile_lines "$2" | grep -m1 "^$1=" | cut -d= -f2-
}

show_table() { # show_table <profile>
  local profile="$1" key manual eff prof
  printf '%-18s %-14s %-14s %s\n' 'service' 'profile' 'manual' 'effective'
  local seen=' '
  while IFS= read -r line; do
    key="${line%%=*}"
    prof="${line#*=}"
    manual="$(manual_get "$key" || true)"
    eff="${manual:-$prof}"
    printf '%-18s %-14s %-14s %s\n' "$(var_svc "$key")" "$prof" "${manual:--}" "$eff"
    seen="$seen$key "
  done < <(profile_lines "$profile")
  local i
  for i in $(manual_indices); do
    key="${MANUAL_LINES[$i]%%=*}"
    [[ "$seen" == *" $key "* ]] && continue
    printf '%-18s %-14s %-14s %s\n' "$(var_svc "$key")" '-' "$(manual_get "$key")" "$(manual_get "$key")"
  done
}

var_svc() { # inverse of svc_var, for the table
  local k
  for k in api api-workers keycloak keycloak-heap postgres postgres-buffers kc-postgres worker storage openbao valkey dashboard docs workflowbuilder init; do
    [ "$(svc_var "$k")" = "$1" ] && { printf '%s' "$k"; return; }
  done
  printf '%s' "${1#CE_}"
}

ram_warn() { # warn (never block) when the effective mem caps exceed RAM - 0.5 GB
  local profile="$1" total=0 key line v ram_gb
  detect_profile; ram_gb="${DETECT_RAM_GB:-0}"
  # 2400CR1y: say once when a cgroup limit (not the host) drove the pick.
  if [ -n "${DETECT_CGROUP_NOTE:-}" ]; then note "${DETECT_CGROUP_NOTE#sizing: }"; fi
  [ "$ram_gb" = 0 ] && return 0
  while IFS= read -r line; do
    key="${line%%=*}"
    case "$key" in CE_API_WORKERS|CE_KC_HEAP|CE_PG_SHARED_BUFFERS|CE_SIZE_PROFILE) continue ;; esac
    v="$(effective_value "$key" "$profile")"
    total=$((total + $(gb_of "$v")))
  done < <(profile_lines "$profile")
  local i k2 v2
  for i in $(manual_indices); do
    k2="${MANUAL_LINES[$i]%%=*}"
    case "$k2" in CE_API_WORKERS|CE_KC_HEAP|CE_PG_SHARED_BUFFERS) continue ;; esac
    v2="$(manual_get "$k2")"
    total=$((total + $(gb_of "$v2")))
  done
  local limit=$(( ram_gb * 1000 - 500 ))
  if [ "$total" -gt "$limit" ]; then
    note "warn: effective mem rails sum to $((total / 1000)).$((total % 1000)) GB but the box reports ${ram_gb} GB (minus 0.5 GB headroom = ${limit} GB thousandths). Not blocking — just watch for OOMs."
  fi
}

usage() {
  cat >&2 <<'USAGE'
usage:
  ce-resize.sh show
  ce-resize.sh <small|medium|large|auto>
  ce-resize.sh set <service>=<amount> [<service>=<amount> ...]
  ce-resize.sh unset <service> [<service> ...]
services: api api-workers keycloak keycloak-heap postgres postgres-buffers
          kc-postgres worker storage openbao valkey dashboard docs workflowbuilder
          init buildkitd caddy registry migrate node-repair db-owner-repair
          (compose names mod-migrate ce-node-repair mod-docs registry-init work too)
          buildkitd-net has no knob: its cap is a fixed literal (0.03g) in docker-compose.yml.
          Shared rails: CE_API_MEM sizes api + mod-migrate + ce-node-repair; CE_PG_MEM
          sizes postgres-app + db-owner-repair; CE_INIT_MEM sizes openbao-init +
          keycloak-admin-init + registry-init. One set line moves all of them.
USAGE
  exit 2
}

cur_profile() { # global `profile` = the profile already in .env, else auto-pick
  profile="$(grep -m1 '^CE_SIZE_PROFILE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "$profile" ] || { detect_profile; profile="$DETECT_PROFILE"; }
}

[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE — run scripts/first-run-secrets.sh first."
load_env

case "${1:-}" in
  show)
    cur_profile
    show_table "$profile"
    ram_warn "$profile"
    ;;
  set)
    [ "$#" -ge 2 ] || usage
    backup_env
    i=2
    while [ "$i" -le "$#" ]; do
      arg="${!i}"
      [[ "$arg" == *"="* ]] || die "expected service=value, got '$arg'"
      svc="${arg%%=*}"; val="${arg#*=}"
      key="$(svc_var "$svc")" || die "unknown service '$svc'; valid names are in the usage block: scripts/ce-resize.sh (run scripts/ce-resize.sh show first)"
      valid_value "$svc" "$val" || die "'$val' is not a valid amount for $svc (mem: 6g / 1.5g; api-workers: integer >= 0)"
      if [ "$svc" = keycloak-heap ] && [ "${val#-Xmx}" = "$val" ]; then val="-Xmx$val"; fi
      manual_set "$key" "$val"
      note "manual override: $svc = $val"
      partners="$(rail_partners "$svc" || true)"
      [ -n "$partners" ] && note "  shared rail $key also resizes: $partners"
      i=$((i + 1))
    done
    cur_profile
    write_env "$profile"
    ram_warn "$profile"
    echo "apply with: docker compose up -d"
    ;;
  unset)
    [ "$#" -ge 2 ] || usage
    backup_env
    i=2
    while [ "$i" -le "$#" ]; do
      svc="${!i}"
      key="$(svc_var "$svc")" || die "unknown service '$svc'; valid names are in the usage block: scripts/ce-resize.sh (run scripts/ce-resize.sh show first)"
      manual_get "$key" >/dev/null || note "no manual override for $svc — nothing to remove"
      manual_drop "$key"
      note "manual override removed: $svc"
      i=$((i + 1))
    done
    cur_profile
    write_env "$profile"
    ram_warn "$profile"
    echo "apply with: docker compose up -d"
    ;;
  small|medium|large)
    backup_env
    cur_profile
    write_env "$1"
    ram_warn "$1"
    echo "apply with: docker compose up -d"
    ;;
  auto)
    backup_env
    detect_profile
    write_env "$DETECT_PROFILE"
    ram_warn "$DETECT_PROFILE"
    echo "apply with: docker compose up -d"
    ;;
  *) usage ;;
esac
