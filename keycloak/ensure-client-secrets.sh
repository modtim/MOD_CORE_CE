#!/bin/sh
# R-078: CE install helper — make the Keycloak client secrets RANDOM when unset.
#
# Run this BEFORE `docker compose up` (from open-core/overlay):
#   ./keycloak/ensure-client-secrets.sh          # writes ./.env
#   KEYCLOAK_ENV_FILE=.env.local ./keycloak/ensure-client-secrets.sh
#
# It generates a random value for every KEYCLOAK_*_CLIENT_SECRET that is not
# already set in the env file / environment, and never writes a fixed default.
# The same values then feed both Keycloak (render-realm.sh) and the apps
# (compose `${VAR}` interpolation), so clients keep working across restarts.

set -e
umask 077   # sec2 L1: a newly created .env is 0600, never world-readable

ENV_FILE="${KEYCLOAK_ENV_FILE:-.env}"

# Dependency guard (mirrors first-run-secrets.sh): prefer openssl, fall back to
# head + od; refuse to run (and write nothing) when neither is available.
if command -v openssl >/dev/null 2>&1; then
    RAND_MODE=openssl
elif command -v od >/dev/null 2>&1 && command -v head >/dev/null 2>&1; then
    RAND_MODE=od
else
    echo "ensure-client-secrets.sh: need 'openssl' (or both 'od' and 'head') on PATH to generate random secrets; none found. Install openssl and re-run." >&2
    exit 1
fi

random_hex() {
    if [ "$RAND_MODE" = openssl ]; then
        openssl rand -hex 32
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

VARS="KEYCLOAK_FASTAPI_CLIENT_SECRET KEYCLOAK_FASTAPI_ADMIN_SECRET KEYCLOAK_DASHBOARD_CLIENT_SECRET KEYCLOAK_SERVICE_CLIENT_SECRET"

# Exported-environment lookup for the four fixed names (a plain case, no
# indirect expansion or dynamic code).
env_value() {
    case "$1" in
        KEYCLOAK_FASTAPI_CLIENT_SECRET)   printf '%s' "${KEYCLOAK_FASTAPI_CLIENT_SECRET:-}" ;;
        KEYCLOAK_FASTAPI_ADMIN_SECRET)    printf '%s' "${KEYCLOAK_FASTAPI_ADMIN_SECRET:-}" ;;
        KEYCLOAK_DASHBOARD_CLIENT_SECRET) printf '%s' "${KEYCLOAK_DASHBOARD_CLIENT_SECRET:-}" ;;
        KEYCLOAK_SERVICE_CLIENT_SECRET)   printf '%s' "${KEYCLOAK_SERVICE_CLIENT_SECRET:-}" ;;
    esac
}

# An existing env file keeps whatever mode it had (umask only covers new
# files): normalise it to 0600 before anything is appended.
if [ -f "$ENV_FILE" ]; then
    chmod 600 "$ENV_FILE"
fi

# DOCKER_GID (compose worker runs `user: 0:${DOCKER_GID}`): derived here so the
# operator never types it. Order: DOCKER_GID already in the environment (an
# override) > stat of the docker socket > `getent group docker`. The value is
# never guessed and never defaulted to 0; if none resolves, exit 1 with one line
# BEFORE any secret is written. A DOCKER_GID line already in ENV_FILE is kept.
docker_gid_value() {
    if [ -n "${DOCKER_GID:-}" ]; then
        printf '%s' "$DOCKER_GID"
        return 0
    fi
    _sock="${DOCKER_SOCKET_PATH:-/var/run/docker.sock}"
    _gid=$(stat -c %g "$_sock" 2>/dev/null || true)
    if [ -z "$_gid" ]; then
        _gid=$(getent group docker 2>/dev/null | cut -d: -f3 || true)
    fi
    printf '%s' "$_gid"
}

if [ -f "$ENV_FILE" ] && grep -qE '^DOCKER_GID=.' "$ENV_FILE" 2>/dev/null; then
    echo "ensure-client-secrets.sh: DOCKER_GID already set in $ENV_FILE; kept."
else
    gid=$(docker_gid_value)
    case "$gid" in
        ''|*[!0-9]*)
            echo "ensure-client-secrets.sh: cannot derive DOCKER_GID (no usable /var/run/docker.sock and no 'docker' group); set DOCKER_GID=<gid> and re-run." >&2
            exit 1 ;;
    esac
    if [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ]; then
        printf '\n' >> "$ENV_FILE"
    fi
    printf 'DOCKER_GID=%s\n' "$gid" >> "$ENV_FILE"
    echo "ensure-client-secrets.sh: DOCKER_GID=$gid written into $ENV_FILE"
fi

changed=0
for v in $VARS; do
    cur=""
    if [ -f "$ENV_FILE" ]; then
        cur=$(grep -m1 "^${v}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
    fi
    if [ -z "$cur" ] && [ -z "$(env_value "$v")" ]; then
        new=$(random_hex)
        # Never write `KEY=` with an empty, short or non-hex value.
        case "$new" in
            ''|*[!0-9a-f]*)
                echo "ensure-client-secrets.sh: random generation failed for $v; nothing written for it." >&2
                exit 1 ;;
        esac
        if [ "${#new}" -ne 64 ]; then
            echo "ensure-client-secrets.sh: random generation returned an unexpected length for $v; nothing written for it." >&2
            exit 1
        fi
        # An existing file without a trailing newline would glue the new
        # KEY=value onto its last line; close that line first.
        if [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ]; then
            printf '\n' >> "$ENV_FILE"
        fi
        printf '%s=%s\n' "$v" "$new" >> "$ENV_FILE"
        changed=$((changed+1))
        echo "  generated random value for $v"
    fi
done

if [ "$changed" -eq 0 ]; then
    echo "ensure-client-secrets.sh: all client secrets already set; nothing written."
else
    echo "ensure-client-secrets.sh: wrote $changed random secret(s) into $ENV_FILE"
fi
