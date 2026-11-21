#!/usr/bin/env bash
# =============================================================================
# pg-version-guard.sh - refuse to (re)create postgres-app on a data volume that
# holds a DIFFERENT PostgreSQL major than the target image.
#
#   pg-version-guard.sh [--image IMG] [-f COMPOSE_FILE]... [-p PROJECT]
#                       [--container NAME] [--volume NAME] [--data-path DIR]
#                       [--env-file FILE] [--reader-image IMG] [--service NAME] [-h]
#
# Why it exists. A PostgreSQL server cannot open a data directory written by another
# major version ("database files are incompatible with server"). The Community Edition
# postgres-app image is PostgreSQL 17 (pg_tle build); other MOD installs run stock
# postgres:13. A volume left by a different major (e.g. a PG13 install moved to the CE
# image) would make `docker compose up -d --force-recreate` (update.sh STEP 8 / install.sh
# STEP 9) take the database down and leave it in a restart loop. This guard runs BEFORE
# that, read-only, and stops the update first. A PG17 volume (e.g. one created by the
# earlier postgres:17.11 image) passes against the PG17 image.
#
# What it does (strictly read-only: never mutates an existing volume, container or image;
# it creates only transient `--rm` reader containers, with the data mount `:ro`):
#   1. finds the postgres data dir source, first match wins:
#        --volume / --data-path
#        the mount of /var/lib/postgresql/data on the existing container (--container,
#          default my_postgres_db): a named volume or a bind-mount path
#        POSTGRES_DATA_PATH (environment, else --env-file): a bind-mount path
#        volumes labelled com.docker.compose.volume=postgres_data (narrowed by
#          com.docker.compose.project when -p / COMPOSE_PROJECT_NAME is given)
#      nothing found -> fresh install -> PASS.
#   2. reads PG_VERSION from it:  docker run --rm --pull never --network none
#        -v <src>:/d:ro --entrypoint sh <image> -c '...cat /d/PG_VERSION...'
#      empty directory -> fresh install -> PASS (the new image will initdb it).
#   3. reads the target image major:  docker run --rm --pull never --network none
#        --entrypoint postgres <image> --version
#      target image = --image, else $MOD_PG_GUARD_IMAGE, else services.<service>.image
#      from `docker compose -f ... config`.
#   4. equal majors -> PASS; different -> REFUSE (exit 1) and print both versions.
#
# Exit codes: 0 pass; 1 REFUSED (major mismatch); 2 usage error; 3 cannot decide
# (no readable image locally, a non-empty directory without PG_VERSION, docker error).
# Callers must treat any non-zero exit as fatal. Emergency override, at your own risk:
# MOD_PG_GUARD_SKIP=1 (prints a warning and returns 0).
#
# Images are never pulled (--pull never): pull/build the release images first, which
# update.sh and install.sh already do before this runs.
#
# Env: MOD_PG_GUARD_DOCKER  docker binary (default docker) [PATH-stubbable]
#      MOD_PG_GUARD_IMAGE   target image ref (same as --image)
#      MOD_PG_GUARD_SKIP    1 = skip the check (warns)
# One copy lives in CORE/startup/scripts/ and a byte-identical copy in the CE overlay
# (open-core/overlay/scripts/); CORE/startup/tests/pg_version_guard_test.sh asserts
# the two never drift.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO="${MOD_PG_GUARD_DOCKER:-docker}"
PGDATA_DEST="/var/lib/postgresql/data"

log()  { printf '[pg-version-guard] %s\n' "$*" >&2; }
die()  { printf '[pg-version-guard] ERROR: %s\n' "$*" >&2; exit "${2:-3}"; }
usage() { awk 'NR>2 && /^# ====/{exit} NR>2{sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"; exit "${1:-0}"; }

IMAGE="${MOD_PG_GUARD_IMAGE:-}"
COMPOSE_FILES=()
PROJECT="${COMPOSE_PROJECT_NAME:-}"
CONTAINER="my_postgres_db"
VOLUME=""
DATA_PATH=""
ENV_FILE=""
READER_IMAGE=""
SERVICE="postgres-app"

while [ $# -gt 0 ]; do
  case "$1" in
    --image)        [ $# -ge 2 ] || usage 2; IMAGE="$2"; shift 2 ;;
    -f|--file)      [ $# -ge 2 ] || usage 2; COMPOSE_FILES+=("$2"); shift 2 ;;
    -p|--project)   [ $# -ge 2 ] || usage 2; PROJECT="$2"; shift 2 ;;
    --container)    [ $# -ge 2 ] || usage 2; CONTAINER="$2"; shift 2 ;;
    --volume)       [ $# -ge 2 ] || usage 2; VOLUME="$2"; shift 2 ;;
    --data-path)    [ $# -ge 2 ] || usage 2; DATA_PATH="$2"; shift 2 ;;
    --env-file)     [ $# -ge 2 ] || usage 2; ENV_FILE="$2"; shift 2 ;;
    --reader-image) [ $# -ge 2 ] || usage 2; READER_IMAGE="$2"; shift 2 ;;
    --service)      [ $# -ge 2 ] || usage 2; SERVICE="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *) printf '[pg-version-guard] unknown option: %s\n' "$1" >&2; usage 2 ;;
  esac
done

if [ "${MOD_PG_GUARD_SKIP:-0}" = "1" ]; then
  log "WARNING: MOD_PG_GUARD_SKIP=1 - PostgreSQL major-version guard SKIPPED; a major mismatch will take the database down"
  exit 0
fi

# Where the human-readable procedure lives (CE layout: scripts/ + docs/; startup layout:
# startup/scripts/ + startup/RUNBOOK_*).
if [ -f "${SCRIPT_DIR}/../docs/postgres-major-version.md" ]; then
  DOC_HINT="docs/postgres-major-version.md"
elif [ -f "${SCRIPT_DIR}/../RUNBOOK_PG_MAJOR_VERSION.md" ]; then
  DOC_HINT="CORE/startup/RUNBOOK_PG_MAJOR_VERSION.md"
else
  DOC_HINT="docs/postgres-major-version.md (CE) / CORE/startup/RUNBOOK_PG_MAJOR_VERSION.md"
fi

# major_of <text> : first version-looking token -> major. PG >= 10: one number ("13");
# PG < 10: two ("9.6").
major_of() {
  local v
  v="$(printf '%s' "$1" | grep -oE '[0-9]+(\.[0-9]+)?' | head -n 1)" || true
  [ -n "${v}" ] || return 1
  if [ "${v%%.*}" -ge 10 ] 2>/dev/null; then printf '%s' "${v%%.*}"; else printf '%s' "${v}"; fi
}

# ---------------------------------------------------------------- 1. locate data
# SOURCES holds "kind|value|label" lines: kind volume|bind.
SOURCES=()
if [ -n "${VOLUME}" ]; then
  SOURCES+=("volume|${VOLUME}|${VOLUME}")
elif [ -n "${DATA_PATH}" ]; then
  # fail closed: docker would take a relative path as a NAMED volume (auto-created empty)
  case "${DATA_PATH}" in /*) ;; *) die "--data-path must be an absolute path (got '${DATA_PATH}')" 2 ;; esac
  SOURCES+=("bind|${DATA_PATH}|${DATA_PATH}")
else
  mnt="$("$DO" inspect -f '{{range .Mounts}}{{if eq .Destination "'"${PGDATA_DEST}"'"}}{{.Type}}|{{.Name}}|{{.Source}}{{end}}{{end}}' "${CONTAINER}" 2>/dev/null || true)"
  if [ -n "${mnt}" ]; then
    mtype="${mnt%%|*}"; rest="${mnt#*|}"; mname="${rest%%|*}"; msrc="${rest#*|}"
    case "${mtype}" in
      volume) [ -n "${mname}" ] && SOURCES+=("volume|${mname}|${mname}") ;;
      bind)   [ -n "${msrc}" ]  && SOURCES+=("bind|${msrc}|${msrc}") ;;
    esac
  fi
  if [ "${#SOURCES[@]}" -eq 0 ]; then
    pdp="${POSTGRES_DATA_PATH:-}"
    if [ -z "${pdp}" ] && [ -n "${ENV_FILE}" ] && [ -f "${ENV_FILE}" ]; then
      pdp="$(grep -E '^POSTGRES_DATA_PATH=' "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' || true)"
    fi
    case "${pdp}" in
      /*) SOURCES+=("bind|${pdp}|${pdp}") ;;
    esac
  fi
  if [ "${#SOURCES[@]}" -eq 0 ]; then
    lsargs=(volume ls -q --filter "label=com.docker.compose.volume=postgres_data")
    [ -n "${PROJECT}" ] && lsargs+=(--filter "label=com.docker.compose.project=${PROJECT}")
    while IFS= read -r v; do
      [ -n "${v}" ] && SOURCES+=("volume|${v}|${v}")
    done < <("$DO" "${lsargs[@]}" 2>/dev/null || true)
  fi
fi

if [ "${#SOURCES[@]}" -eq 0 ]; then
  log "no postgres data volume found for this install (fresh install) - PASS"
  exit 0
fi

# ---------------------------------------------------------------- image helpers
# resolve_image: --image / env, else services.<SERVICE>.image from the compose config.
resolve_image() {
  [ -n "${IMAGE}" ] && return 0
  [ "${#COMPOSE_FILES[@]}" -gt 0 ] || return 1
  local fargs=() f cfg
  for f in "${COMPOSE_FILES[@]}"; do fargs+=(-f "$f"); done
  cfg="$("$DO" compose "${fargs[@]}" --profile all config 2>/dev/null)" || return 1
  IMAGE="$(printf '%s\n' "${cfg}" | awk -v s="  ${SERVICE}:" '
      $0 == s { f = 1; next }
      f && /^  [A-Za-z0-9_.-]+:/ { exit }
      f && $1 == "image:" { print $2; exit }')"
  [ -n "${IMAGE}" ]
}

image_present() { "$DO" image inspect "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 2. read PG_VERSION
READ_SCRIPT='if [ -f /d/PG_VERSION ]; then cat /d/PG_VERSION; elif ! ls -A /d >/dev/null 2>&1; then echo __UNREADABLE__; elif [ -z "$(ls -A /d 2>/dev/null)" ]; then echo __EMPTY__; else echo __NO_PG_VERSION__; fi'

ensure_reader() {
  [ -n "${READER_IMAGE}" ] && return 0
  local c cimg
  resolve_image || true
  for c in "${IMAGE}"; do
    [ -n "${c}" ] && image_present "${c}" && { READER_IMAGE="${c}"; return 0; }
  done
  cimg="$("$DO" inspect -f '{{.Config.Image}}' "${CONTAINER}" 2>/dev/null || true)"
  if [ -n "${cimg}" ] && image_present "${cimg}"; then READER_IMAGE="${cimg}"; return 0; fi
  return 1
}

declare -a VOL_VERSIONS=()
for entry in "${SOURCES[@]}"; do
  kind="${entry%%|*}"; rest="${entry#*|}"; src="${rest%%|*}"; label="${rest#*|}"
  if [ "${kind}" = volume ]; then
    "$DO" volume inspect "${src}" >/dev/null 2>&1 || { log "volume ${src} does not exist (fresh install) - PASS for it"; continue; }
  else
    [ -d "${src}" ] || { log "data path ${src} does not exist (fresh install) - PASS for it"; continue; }
  fi
  ensure_reader || die "cannot read PG_VERSION of ${label}: no local image to run the read-only probe with (pass --image / --reader-image, and pull/build the release images first)"
  raw="$("$DO" run --rm --pull never --network none -v "${src}:/d:ro" --entrypoint sh "${READER_IMAGE}" -c "${READ_SCRIPT}" 2>/dev/null)" \
    || die "could not read ${label} with ${READER_IMAGE} (docker run failed)"
  raw="$(printf '%s' "${raw}" | tr -d '\r' | head -n 1)"
  case "${raw}" in
    __EMPTY__)
      log "${label} is empty (fresh install) - PASS for it"; continue ;;
    __UNREADABLE__)
      die "${label} cannot be read by the probe (permission or userns): refusing to treat it as empty" 3 ;;
    __NO_PG_VERSION__)
      die "${label} is not empty but has no PG_VERSION: not a PostgreSQL data directory the new image can initialise or open. Inspect it by hand; refusing to proceed." ;;
    "")
      die "PG_VERSION of ${label} is empty or unreadable" ;;
  esac
  vmaj="$(major_of "${raw}")" || die "PG_VERSION of ${label} is not a version: '${raw}'"
  VOL_VERSIONS+=("${label}|${vmaj}")
done

if [ "${#VOL_VERSIONS[@]}" -eq 0 ]; then
  log "no initialised postgres data found (fresh install) - PASS"
  exit 0
fi

# ---------------------------------------------------------------- 3. target major
resolve_image || die "cannot determine the target postgres image (pass --image, or -f <compose-file> with a '${SERVICE}' service whose config renders)"
image_present "${IMAGE}" || die "target image ${IMAGE} is not present locally; pull/build it first (the guard never pulls)"
vraw="$("$DO" run --rm --pull never --network none --entrypoint postgres "${IMAGE}" --version 2>/dev/null)" \
  || die "could not run '${IMAGE}' to read the postgres version"
imaj="$(major_of "${vraw}")" || die "unparseable 'postgres --version' output from ${IMAGE}: '${vraw}'"

# ---------------------------------------------------------------- 4. compare
rc=0
for vv in "${VOL_VERSIONS[@]}"; do
  label="${vv%%|*}"; vmaj="${vv#*|}"
  if [ "${vmaj}" = "${imaj}" ]; then
    log "OK: ${label} holds PG${vmaj}, image ${IMAGE} is PG${imaj}"
  else
    rc=1
    {
      echo "[pg-version-guard] REFUSING: the postgres data volume ${label} holds PG${vmaj} data, but the target image ${IMAGE} is PG${imaj}."
      echo "[pg-version-guard]   A PG${imaj} server cannot open a PG${vmaj} data directory, and recreating postgres-app on it would take the database down."
      echo "[pg-version-guard]   Nothing was changed. Dump first with pg_dump (while the PG${vmaj} database is still running), then restore into a fresh PG${imaj} volume,"
      echo "[pg-version-guard]   or keep a PG${vmaj} image. Procedure: ${DOC_HINT}"
    } >&2
  fi
done
exit "${rc}"
