#!/usr/bin/env bash
# =============================================================================
# recover-after-reboot.sh — bring a CE stack back after a host reboot.
#
#   scripts/recover-after-reboot.sh [-f <compose-file>]... [-p <project>]
#                                   [-x <service>]... [-t <seconds>] [-n|--dry-run]
#
# Why it exists. After a reboot dockerd restores the containers before the host
# is fully up. A container whose published port is bound to an address that does
# not exist yet (e.g. `192.168.x.y:8000:80` on a Wi-Fi/DHCP host: the lease
# arrives ~8 s AFTER dockerd restores containers) fails to start with
# "failed to bind host port ...: cannot assign requested address". dockerd does
# NOT retry that start (the restart policy only re-launches containers that ran
# and then exited), the failed start also tears down the container's network
# endpoint, so the container stays Exited (255) with NO network endpoint. Separately
# `openbao-init` is a one-shot (restart: "no"), so OpenBao comes back SEALED.
#
# What it does (idempotent; a healthy stack is a no-op that prints "nothing to do"):
#   1. waits for the docker daemon;
#   2. lists the compose project's containers (label com.docker.compose.project);
#   3. a container with ZERO network endpoints is re-attached to its network with
#      --alias <container_name> --alias <compose service> (restarted if running);
#   4. starts exited infra (openbao, postgres, keycloak-db, redis, ...) and waits
#      for health;
#   5. ONLY if `bao status` says Initialized+Sealed, re-runs the openbao-init
#      one-shot (`docker start -a`, which unseals from the openbao-init-state
#      volume and never rotates anything);
#   6. starts the remaining exited services in dependency order
#      (keycloak, versitygw, api, worker, dashboard, workflow-builder, ...),
#      retrying a failed start until the timeout (the LAN IP may still be coming up);
#   7. waits for health (bounded, per container) and prints a summary table.
#
# Exit codes: 0 = everything healthy (or nothing to do); 1 = still unhealthy or
# sealed, docker unreachable, or no container matches the project; 2 = usage error.
#
# Options
#   -f, --file FILE      compose file of this install (repeatable). Only used to
#                        recreate a MISSING openbao-init container via
#                        `docker compose ... up --no-deps openbao-init`; the script
#                        never runs `docker compose up` without the files given.
#   -p, --project NAME   compose project (default: $COMPOSE_PROJECT_NAME, else the
#                        project label of container my_openbao, else the `name:`
#                        of the first -f file, else mod-core-ce).
#   -x, --exclude SVC    do not start/judge this service (repeatable). Docker's
#                        `unless-stopped` cannot tell a reboot from a deliberate
#                        stop, so use this for services you keep stopped.
#   -t, --timeout SECS   per-container wait/retry budget (default 180).
#   -n, --dry-run        read-only: print what WOULD be done, change nothing.
#   -h, --help           show this help and exit 0
#
# Running it from systemd after boot (example, adjust paths):
#   [Unit]  After=docker.service network-online.target   Wants=network-online.target
#   [Service] Type=oneshot
#             ExecStart=/opt/ce/scripts/recover-after-reboot.sh -p mod-core-ce -f /opt/ce/docker-compose.yml
#   [Install] WantedBy=multi-user.target
#
# Secrets: only fixed inspect fields (name/state/health/networks) are read; no
# environment, no env file, no unseal key or token is ever read or printed. The
# openbao-init output is kept in a temp file and only its tail is shown on failure.
#
# Test hooks (NOT settings: an install never sets these, and nothing here configures
# behaviour through them). The test suites under open-core/tests use them to point the
# script at temp files and a fast poll; the defaults are used when they are unset:
#   RECOVER_POLL_SECS          wait-loop poll interval in seconds (default 2)
#   MOD_CE_RECOVER_WAIT_SECONDS       bounded wait for the project's containers to exist at boot (default 180)
#   MOD_CE_RECOVER_WAIT_POLL_SECONDS  poll interval of that wait (default 5)
#   SIDECAR_PASS_FILE          encrypted-store key copy (default /run/secrets/gocryptfs-pass)
#   SIDECAR_PERSIST_PASS_FILE  persistent key file (default /var/lib/mod/gocryptfs-pass - PINNED, never derived from VERSITYGW_CIPHER_PATH)
#     The two SIDECAR_*_PASS_FILE redirects above work ONLY under SIDECAR_TEST_MODE=1
#     AND a non-root euid (the same gate first-run-sidecar.sh honours): a plain run, or
#     even `sudo -E`, ignores both hooks and keeps the pinned paths.
#   VERSITYGW_CIPHER_PATH      versitygw ciphertext dir (default /var/lib/mod/versitygw-ciphertext)
# CE_TENANT_SLUG is read by first-run-sidecar.sh (which this script runs), not by this script.
# =============================================================================
set -u

PROG="$(basename "$0")"
COMPOSE_FILES=()
EXCLUDE=()
PROJECT=""
DRY=0
TIMEOUT=180
POLL="${RECOVER_POLL_SECS:-2}"
CONTAINER_WAIT="${MOD_CE_RECOVER_WAIT_SECONDS:-180}"
WAIT_POLL="${MOD_CE_RECOVER_WAIT_POLL_SECONDS:-5}"
ACTIONS=0
PROBLEMS=0

INFRA_ORDER="openbao postgres-app keycloak-db redis versitygw"
APP_ORDER="keycloak versitygw api mod-worker mod-dashboard workflow-builder mod-docs caddy"

usage() { awk 'NR>2 && /^# ====/{exit} NR>2{sub(/^# ?/,""); print}' "$0"; }
log() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die_usage() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    -f|--file)    [ $# -ge 2 ] || die_usage "$1 needs a value"; COMPOSE_FILES+=("$2"); shift 2 ;;
    -p|--project) [ $# -ge 2 ] || die_usage "$1 needs a value"; PROJECT="$2"; shift 2 ;;
    -x|--exclude) [ $# -ge 2 ] || die_usage "$1 needs a value"; EXCLUDE+=("$2"); shift 2 ;;
    -t|--timeout) [ $# -ge 2 ] || die_usage "$1 needs a value"; TIMEOUT="$2"; shift 2 ;;
    -n|--dry-run) DRY=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die_usage "unknown option: $1 (try --help)" ;;
  esac
done
case "$CONTAINER_WAIT" in ''|*[!0-9]*) CONTAINER_WAIT=180 ;; esac
case "$WAIT_POLL" in ''|*[!0-9]*|0) WAIT_POLL=5 ;; esac
case "$TIMEOUT" in ''|*[!0-9]*) die_usage "--timeout must be a whole number of seconds" ;; esac
for f in ${COMPOSE_FILES[@]+"${COMPOSE_FILES[@]}"}; do
  [ -r "$f" ] || die_usage "compose file not readable: $f"
done

now() { date +%s; }
nap() { sleep "$POLL"; }

# Mutating commands go through act(): printed instead of run under --dry-run.
act() {
  ACTIONS=$((ACTIONS + 1))
  if [ "$DRY" = 1 ]; then log "  [dry-run] would run: $*"; return 0; fi
  "$@" >/dev/null
}

excluded() {
  local s
  for s in ${EXCLUDE[@]+"${EXCLUDE[@]}"}; do [ "$s" = "$1" ] && return 0; done
  return 1
}

# The encrypted stores cannot run without their key: versitygw and redis bind
# /run/secrets/gocryptfs-pass, so when the restore failed they must NOT be started
# (starting them with no key would mount an empty passfile and make the data
# unreadable). The restore failure itself is counted once, in the key step.
key_svc() { case " $1 " in *" versitygw "*|*" redis "*) return 0 ;; esac; return 1; }
key_svc_blocked() { # idx -> 0 if this service must be skipped because the key is gone
  local i="$1"
  [ "$KEY_BROKEN" = 1 ] || return 1
  key_svc "${C_SVC[$i]}"
}

# ---- 1. docker daemon -------------------------------------------------------
end=$(( $(now) + TIMEOUT ))
until docker info >/dev/null 2>&1; do
  if [ "$DRY" = 1 ] || [ "$(now)" -ge "$end" ]; then
    echo "ERROR: docker daemon is not reachable." >&2; exit 1
  fi
  log "waiting for the docker daemon ..."; nap
done

# ---- project ----------------------------------------------------------------
if [ -z "$PROJECT" ]; then PROJECT="${COMPOSE_PROJECT_NAME:-}"; fi
if [ -z "$PROJECT" ]; then
  PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' my_openbao 2>/dev/null || true)"
fi
if [ -z "$PROJECT" ] && [ "${#COMPOSE_FILES[@]}" -gt 0 ]; then
  PROJECT="$(sed -n 's/^name:[[:space:]]*\([A-Za-z0-9_.-]*\).*/\1/p' "${COMPOSE_FILES[0]}" | head -n1)"
fi
[ -n "$PROJECT" ] || PROJECT="mod-core-ce"
log "project: $PROJECT$([ "$DRY" = 1 ] && printf '   (dry-run: read-only)')"

# ---- 2. inventory -----------------------------------------------------------
C_NAME=(); C_SVC=(); C_ONEOFF=(); C_STATE=(); C_EXIT=(); C_POL=(); C_HEALTH=(); C_NETS=(); C_NM=()
FMT='{{.Name}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.oneoff"}}|{{.State.Status}}|{{.State.ExitCode}}|{{.HostConfig.RestartPolicy.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{len .NetworkSettings.Networks}}|{{.HostConfig.NetworkMode}}'

load_one() { # idx  — (re)read one container's fixed fields
  local i="$1" line n
  line="$(docker inspect -f "$FMT" "${C_NAME[$i]}" 2>/dev/null)" || return 1
  IFS='|' read -r n C_SVC[$i] C_ONEOFF[$i] C_STATE[$i] C_EXIT[$i] C_POL[$i] C_HEALTH[$i] C_NETS[$i] C_NM[$i] <<EOF
$line
EOF
  return 0
}

list_project_containers() {
  docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}' 2>/dev/null | sort
}
names="$(list_project_containers)"
# Boot race: this unit can run before dockerd has restored the containers (docker answers
# `info` first, the containers appear a little later). Wait, bounded, for the project's
# containers to exist before deciding there is nothing to recover. --dry-run never waits.
if [ -z "$names" ] && [ "$DRY" != 1 ]; then
  wait_end=$(( $(now) + CONTAINER_WAIT ))
  while [ -z "$names" ] && [ "$(now)" -lt "$wait_end" ]; do
    log "waiting for the containers of compose project '$PROJECT' to appear (docker has not restored them yet) ..."
    sleep "$WAIT_POLL"
    names="$(list_project_containers)"
  done
fi
if [ -z "$names" ]; then
  if [ "$DRY" = 1 ]; then
    echo "ERROR: no containers found for compose project '$PROJECT' (use -p <project>)." >&2
    exit 1
  fi
  # A missing compose file is an error (every -f is checked readable above). No containers
  # after the bounded wait means nothing to recover: not a failure.
  log "nothing to recover: no containers exist for compose project '$PROJECT' after ${CONTAINER_WAIT}s (use -p <project> if the name is wrong)."
  exit 0
fi
while IFS= read -r nm; do
  [ -n "$nm" ] || continue
  C_NAME+=("${nm#/}")
  load_one $(( ${#C_NAME[@]} - 1 )) || { unset 'C_NAME[${#C_NAME[@]}-1]'; continue; }
done <<EOF
$names
EOF

N=${#C_NAME[@]}
idx_of_service() { local i; for ((i = 0; i < N; i++)); do [ "${C_SVC[$i]}" = "$1" ] && { echo "$i"; return 0; }; done; return 1; }
# A "managed" container is a long-running service the stack expects to be up.
managed() { # idx
  local i="$1"
  [ "${C_ONEOFF[$i]}" = "True" ] && return 1
  [ "${C_POL[$i]}" = "no" ] && return 1
  [ -z "${C_POL[$i]}" ] && return 1
  excluded "${C_SVC[$i]}" && return 1
  return 0
}

# ---- network endpoints ------------------------------------------------------
fix_nets() { # idx — re-attach a container that has no network endpoint
  local i="$1" name="${C_NAME[$i]}" svc="${C_SVC[$i]}" nm="${C_NM[$i]}" aliases=()
  [ "${C_NETS[$i]}" = "0" ] || return 0
  case "$nm" in host|none|default|bridge|container:*|'') return 0 ;; esac
  aliases=(--alias "$name")
  [ -n "$svc" ] && [ "$svc" != "$name" ] && aliases+=(--alias "$svc")
  log "  $name: no network endpoint -> docker network connect ${aliases[*]} $nm $name"
  if ! act docker network connect "${aliases[@]}" "$nm" "$name"; then
    warn "$name: network connect failed"; return 1
  fi
  if [ "${C_STATE[$i]}" = "running" ]; then
    log "  $name: restarting so it picks up the network"
    act docker restart "$name" || return 1
  fi
  [ "$DRY" = 1 ] || load_one "$i"
  return 0
}

# ---- starting + health ------------------------------------------------------
start_ct() { # idx — retry (bind address may not exist yet) until the timeout
  local i="$1" name="${C_NAME[$i]}" e err stop
  stop=$(( $(now) + TIMEOUT ))
  if [ "$DRY" = 1 ]; then act docker start "$name"; return 0; fi
  ACTIONS=$((ACTIONS + 1))
  while :; do
    if err="$(docker start "$name" 2>&1 >/dev/null)"; then break; fi
    if [ "$(now)" -ge "$stop" ]; then
      warn "$name: start failed: ${err%%$'\n'*}"; return 1
    fi
    nap
  done
  load_one "$i"
  if [ "${C_NETS[$i]}" = "0" ]; then fix_nets "$i" || return 1; fi
  return 0
}

wait_ready() { # idx — bounded wait for running + (healthy|no healthcheck)
  local i="$1" stop stable=0
  [ "$DRY" = 1 ] && return 0
  stop=$(( $(now) + TIMEOUT ))
  while :; do
    load_one "$i" || return 1
    if [ "${C_STATE[$i]}" = "running" ]; then
      case "${C_HEALTH[$i]}" in
        healthy) return 0 ;;
        none) stable=$((stable + 1)); [ "$stable" -ge 2 ] && return 0 ;;
        *) stable=0 ;;
      esac
    else
      stable=0
      case "${C_STATE[$i]}" in exited|dead) return 1 ;; esac
    fi
    [ "$(now)" -ge "$stop" ] && return 1
    nap
  done
}

ensure_up() { # idx
  local i="$1"
  managed "$i" || return 0
  fix_nets "$i" || true
  case "${C_STATE[$i]}" in
    running|restarting|paused) ;;
    *)
      log "  ${C_NAME[$i]}: ${C_STATE[$i]} (exit ${C_EXIT[$i]}) -> start"
      start_ct "$i" || return 0
      ;;
  esac
  wait_ready "$i" || warn "${C_NAME[$i]}: not ready within ${TIMEOUT}s"
  return 0
}

# ---- OpenBao seal -----------------------------------------------------------
OB_SEALED="n/a"
bao_status() { # sets OB_INIT / OB_SEALED from `bao status`
  local out i
  OB_INIT=""; OB_SEALED="unknown"
  i="$(idx_of_service openbao)" || { OB_SEALED="n/a"; return 1; }
  out="$(docker exec "${C_NAME[$i]}" bao status 2>&1)"
  OB_INIT="$(printf '%s\n' "$out" | awk '$1=="Initialized"{print $2; exit}')"
  local s; s="$(printf '%s\n' "$out" | awk '$1=="Sealed"{print $2; exit}')"
  case "$s" in true) OB_SEALED="SEALED" ;; false) OB_SEALED="unsealed" ;; *) OB_SEALED="unknown" ;; esac
  return 0
}

unseal_if_needed() {
  local i j initname log_f rc
  idx_of_service openbao >/dev/null || return 0
  bao_status
  if [ "$OB_SEALED" != "SEALED" ]; then
    log "  openbao: $OB_SEALED - no unseal needed"
    return 0
  fi
  if [ "$OB_INIT" != "true" ]; then
    warn "openbao is sealed but not initialized - this is a first run, use 'docker compose up -d' (not touched here)"
    return 0
  fi
  log "  openbao: SEALED -> re-running the openbao-init one-shot (unseal-only, no rotation)"
  if j="$(idx_of_service openbao-init)"; then
    initname="${C_NAME[$j]}"
    if [ "$DRY" = 1 ]; then act docker start -a "$initname"; return 0; fi
    ACTIONS=$((ACTIONS + 1))
    log_f="$(mktemp "${TMPDIR:-/tmp}/recover-init.XXXXXX")"
    docker start -a "$initname" >"$log_f" 2>&1; rc=$?
    if [ "$rc" -ne 0 ]; then
      warn "openbao-init exited $rc; last lines of its log:"; tail -n 8 "$log_f" >&2
    fi
    rm -f "$log_f"
  elif [ "${#COMPOSE_FILES[@]}" -gt 0 ]; then
    local fargs=() f
    for f in "${COMPOSE_FILES[@]}"; do fargs+=(-f "$f"); done
    log "  openbao-init container missing -> docker compose -p $PROJECT ${fargs[*]} up --no-deps openbao-init"
    if [ "$DRY" = 1 ]; then act docker compose -p "$PROJECT" "${fargs[@]}" up --no-deps openbao-init; return 0; fi
    ACTIONS=$((ACTIONS + 1))
    docker compose -p "$PROJECT" "${fargs[@]}" up --no-deps openbao-init >/dev/null 2>&1 \
      || warn "compose run of openbao-init failed"
  else
    warn "no openbao-init container in project '$PROJECT' and no -f compose file given; cannot unseal"
    return 0
  fi
  bao_status
  log "  openbao: $OB_SEALED after unseal run"
}

# ---- order ------------------------------------------------------------------
in_list() { case " $2 " in *" $1 "*) return 0 ;; esac; return 1; }
PHASE_A=(); PHASE_C=()
for s in $INFRA_ORDER; do i="$(idx_of_service "$s")" && PHASE_A+=("$i"); done
for s in $APP_ORDER;   do i="$(idx_of_service "$s")" && PHASE_C+=("$i"); done
for ((i = 0; i < N; i++)); do
  in_list "${C_SVC[$i]}" "$INFRA_ORDER $APP_ORDER" && continue
  [ "${C_SVC[$i]}" = "openbao-init" ] && continue
  PHASE_C+=("$i")
done

# ---- 3..6 run ---------------------------------------------------------------
# ---- encrypted-store key: /run is wiped by a reboot, the persistent key file is not.
# The stores bind /run/secrets/gocryptfs-pass; if it is gone, rebuild it from the
# persistent copy (first-run-sidecar.sh does that, and refuses - printing why - when
# ciphertext exists but no key file does; it never generates a key over data).
SIDECAR_SCRIPT="$(dirname "$0")/first-run-sidecar.sh"
# Production paths are PINNED; the SIDECAR_PASS_FILE / SIDECAR_PERSIST_PASS_FILE
# redirects below are TEST hooks only, gated EXACTLY like first-run-sidecar.sh:109
# (SIDECAR_TEST_MODE=1 AND a non-root euid), so a plain run or `sudo -E` can never
# move the key files off these paths.
SIDECAR_PASS="/run/secrets/gocryptfs-pass"
# The persistent key file is the sidecar's source of truth: next to (never inside)
# the versitygw cipher base. Same test hooks the sidecar honours (env only - this
# script never reads .env).
SIDECAR_PERSIST="/var/lib/mod/gocryptfs-pass"
if [ "${SIDECAR_TEST_MODE:-}" = 1 ] && [ "$(id -u)" -ne 0 ]; then
  SIDECAR_PASS="${SIDECAR_PASS_FILE:-$SIDECAR_PASS}"
  SIDECAR_PERSIST="${SIDECAR_PERSIST_PASS_FILE:-$SIDECAR_PERSIST}"
fi
KEY_BROKEN=0
NEEDS_ROOT=0
key_stale=0
# FIX-XEON-RECOVER-SCRIPT-P1: a healthy stack already holds its key mounts, so the key
# restore is skipped for it (the restore can need root and must never turn a healthy
# stack into exit 1). Healthy = every managed container running, attached, and
# healthy-or-no-healthcheck.
stack_healthy() {
  local i
  for ((i = 0; i < N; i++)); do
    managed "$i" || continue
    [ "${C_STATE[$i]}" = "running" ] || return 1
    # NETS-OK: a container sharing another's network (network_mode: service:/
    # container:, e.g. mod_buildkitd on buildkitd-net) has an EMPTY .NetworkSettings.Networks
    # map - NETS=0 is normal there, not a torn endpoint (fix_nets already skips it).
    { [ "${C_NETS[$i]}" != "0" ] || case "${C_NM[$i]}" in container:*|service:*) true;; *) false;; esac; } || return 1
    case "${C_HEALTH[$i]}" in healthy|none) ;; *) return 1 ;; esac
  done
  return 0
}
KEY_SKIP=0
if stack_healthy; then
  KEY_SKIP=1
  log "encrypted-store key: every managed container is running and healthy - key restore skipped"
fi
# FIX-CE-REBOOT-RECOVERY-ENCSTORE-UNIT-BEFORE-DOCKER-AND-DIR-GUARD: a reboot leaves an
# EMPTY DIRECTORY at the /run copy (dockerd auto-creates the bind SOURCE on tmpfs before
# it mounts, and `-s` is TRUE for a 40-byte directory), so the old code read the wiped
# key as "present", skipped the restore, and `docker start` failed with exit 127.
# A directory at the key path is a boot artifact: rmdir it (never a recursive delete),
# then the restore below rebuilds the 0600 FILE from the persistent key file.
if [ "$KEY_SKIP" = 0 ] && [ -d "$SIDECAR_PASS" ]; then
  log "encrypted-store key: ${SIDECAR_PASS} is a DIRECTORY (a boot artifact of the bind mount, not a key) -> removing it so the copy is restored from ${SIDECAR_PERSIST}"
  if [ "$DRY" = 1 ]; then
    log "  [dry-run] would run: rmdir ${SIDECAR_PASS}"
  else
    rmdir "$SIDECAR_PASS" 2>/dev/null || log "  could not rmdir ${SIDECAR_PASS} (not empty or in use) - the restore below reports the cause"
  fi
  key_stale=1
fi
# key-blocked services are counted EXACTLY ONCE (the loop counts them, the summary
# then skips them) so a dead store can never leave this script at exit 0.
key_blocked_counted=()
note_key_blocked() { # idx -> log + count a key-blocked store once
  local i="$1"
  log "  ${C_NAME[$i]}: NOT started - the encrypted-store key is missing and could not be restored"
  [ -n "${key_blocked_counted[$i]:-}" ] && return 0
  key_blocked_counted[$i]=1
  [ "$DRY" = 1 ] || PROBLEMS=$((PROBLEMS + 1))
  return 0
}
# "cannot read" is NOT "differs": both files are 0600 root-owned, so a normal-user
# run gets cmp rc 2 (cannot open). The old code read that as "differs", which set
# key_stale=1 and then deleted the /run copy - a destructive action on a false
# premise. Only a cmp that PROVES they differ (rc 1) counts; an unreadable pair is
# reported in plain words and left alone (the sidecar prints the sudo line itself).
cmp_differs() { # cmp_differs <a> <b> -> 0 only when cmp proves they differ (rc 1)
  local a="$1" b="$2" r
  if [ -r "$a" ] && [ -r "$b" ]; then
    r=0; cmp -s "$a" "$b" || r=$?
    [ "$r" = 1 ] && return 0
    return 1
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    r=0; sudo -n cmp -s "$a" "$b" || r=$?
    [ "$r" = 1 ] && return 0
    return 1
  fi
  log "encrypted-store key: cannot read ${SIDECAR_PASS} / ${SIDECAR_PERSIST} as this user (they are 0600 root-owned) - not assuming they differ; if the copy must be rebuilt run: sudo bash ${SIDECAR_SCRIPT}"
  return 1
}
if [ "$KEY_SKIP" = 0 ] && [ -s "$SIDECAR_PASS" ] && [ -s "$SIDECAR_PERSIST" ] && cmp_differs "$SIDECAR_PASS" "$SIDECAR_PERSIST"; then
  key_stale=1
  log "encrypted-store key: the ${SIDECAR_PASS} copy differs from the persistent key file ${SIDECAR_PERSIST} -> the persistent one wins; rebuilding the copy (the value is never printed)"
fi
if [ "$KEY_SKIP" = 0 ] && { [ ! -s "$SIDECAR_PASS" ] || [ "$key_stale" = 1 ]; } && [ -f "$SIDECAR_SCRIPT" ]; then
  [ "$key_stale" = 1 ] || log "encrypted-store key: ${SIDECAR_PASS} is missing (normal after a reboot) -> restoring it from the persistent key file"
  if [ "$DRY" = 1 ]; then
    log "  [dry-run] would run: bash $SIDECAR_SCRIPT"
  else
    ACTIONS=$((ACTIONS + 1))
    # a stale copy is only ever a tmpfs COPY: drop it so the sidecar restores it
    # byte-for-byte from the persistent file (0600, value never printed).
    [ "$key_stale" = 1 ] && rm -f "$SIDECAR_PASS"
    if [ "$(id -u)" -ne 0 ] && [ "${SIDECAR_TEST_MODE:-}" != 1 ]; then
      # pinned production paths are root-only: a normal user never runs the sidecar here
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        log "encrypted-store key: restoring it through passwordless sudo (the value is never printed)"
        sudo -n -E bash "$SIDECAR_SCRIPT" || { KEY_BROKEN=1; PROBLEMS=$((PROBLEMS + 1)); warn "could not restore the encrypted-store key (see the message above); versitygw and redis will not start until it is fixed"; }
      else
        NEEDS_ROOT=1; KEY_BROKEN=1; PROBLEMS=$((PROBLEMS + 1))
        log "encrypted-store key: restoring it needs root (this user has no passwordless sudo). Run exactly this one line, then run this script again:"
        log "  sudo -E bash ${SIDECAR_SCRIPT}"
      fi
    else
      bash "$SIDECAR_SCRIPT" || { KEY_BROKEN=1; PROBLEMS=$((PROBLEMS + 1)); warn "could not restore the encrypted-store key (see the message above); versitygw and redis will not start until it is fixed"; }
    fi
  fi
fi

log "step 1/3: infrastructure"
for i in ${PHASE_A[@]+"${PHASE_A[@]}"}; do
  key_svc_blocked "$i" && { note_key_blocked "$i"; continue; }
  ensure_up "$i"
done
log "step 2/3: openbao seal state"
unseal_if_needed
log "step 3/3: application services"
for i in ${PHASE_C[@]+"${PHASE_C[@]}"}; do
  key_svc_blocked "$i" && { note_key_blocked "$i"; continue; }
  ensure_up "$i"
done

# ---- 7. summary -------------------------------------------------------------
bao_status || true
printf '\n%-22s %-22s %-10s %-10s %-5s %s\n' SERVICE CONTAINER STATE HEALTH NETS RESULT
for ((i = 0; i < N; i++)); do
  [ "$DRY" = 1 ] || load_one "$i" || true
  res="oneshot"
  if key_svc_blocked "$i"; then
    res="FAIL"
    # counted once: the start loops already counted a blocked store (note_key_blocked)
    [ -n "${key_blocked_counted[$i]:-}" ] || { [ "$DRY" = 1 ] || PROBLEMS=$((PROBLEMS + 1)); }
  elif managed "$i"; then
    if [ "${C_STATE[$i]}" = "running" ] \
       && { [ "${C_NETS[$i]}" != "0" ] || case "${C_NM[$i]}" in container:*|service:*) true;; *) false;; esac; } \
       && { [ "${C_HEALTH[$i]}" = "healthy" ] || [ "${C_HEALTH[$i]}" = "none" ]; }; then
      res="ok"
    else
      res="FAIL"; PROBLEMS=$((PROBLEMS + 1))
    fi
  elif excluded "${C_SVC[$i]}"; then
    res="excluded"
  fi
  printf '%-22s %-22s %-10s %-10s %-5s %s\n' "${C_SVC[$i]}" "${C_NAME[$i]}" "${C_STATE[$i]}" "${C_HEALTH[$i]}" "${C_NETS[$i]}" "$res"
done
if [ "$OB_SEALED" = "SEALED" ]; then PROBLEMS=$((PROBLEMS + 1)); fi
printf 'openbao: %s\n' "$OB_SEALED"

if [ "$PROBLEMS" -gt 0 ]; then
  log "RESULT: $PROBLEMS problem(s) remain$([ "$DRY" = 1 ] && printf ' (dry-run: %s action(s) would be taken)' "$ACTIONS")."
  if [ "$NEEDS_ROOT" = 1 ]; then
    log "RESULT: needs root for the encrypted-store key - run the sudo line above, then run this script again (exit 2)."
    exit 2
  fi
  exit 1
fi
if [ "$ACTIONS" -eq 0 ]; then
  log "RESULT: nothing to do - stack is healthy."
elif [ "$DRY" = 1 ]; then
  log "RESULT: dry-run - $ACTIONS action(s) would be taken."
else
  log "RESULT: recovered ($ACTIONS action(s) taken) - stack is healthy."
fi
exit 0
