#!/bin/sh
# R-078: CE Keycloak import path — substitute ${KEYCLOAK_*} client-secret
# placeholders in open-core/overlay/keycloak/core-realm.json before Keycloak
# reads it (Keycloak's own --import-realm does NO substitution).
#
# The realm file ships placeholders only; this wrapper renders it into
# /opt/keycloak/data/import/core-realm.json and execs the stock entrypoint.
# Substitution is done with sed (the keycloak image ships no gettext/envsubst)
# and is limited to the four client-secret vars, the seeded realm admin
# user's password (KEYCLOAK_REALM_ADMIN_PASSWORD, brief 1620) and
# KEYCLOAK_FASTAPI_CLIENT_ID — every other ${...} stays literal.
#
# An unset client-secret var is a hard error (the script exits 1). Run
# ./keycloak/ensure-client-secrets.sh BEFORE `docker compose up`, which writes
# ./.env so Keycloak and the apps get the same values.

set -eu

# Fixed-name lookup (no indirect expansion or dynamic code).
env_value() {
    case "$1" in
        KEYCLOAK_FASTAPI_CLIENT_SECRET)       printf '%s' "${KEYCLOAK_FASTAPI_CLIENT_SECRET:-}" ;;
        KEYCLOAK_FASTAPI_ADMIN_SECRET)        printf '%s' "${KEYCLOAK_FASTAPI_ADMIN_SECRET:-}" ;;
        KEYCLOAK_DASHBOARD_CLIENT_SECRET)     printf '%s' "${KEYCLOAK_DASHBOARD_CLIENT_SECRET:-}" ;;
        KEYCLOAK_SERVICE_CLIENT_SECRET)       printf '%s' "${KEYCLOAK_SERVICE_CLIENT_SECRET:-}" ;;
        KEYCLOAK_REALM_ADMIN_PASSWORD)        printf '%s' "${KEYCLOAK_REALM_ADMIN_PASSWORD:-}" ;;
        KEYCLOAK_FASTAPI_CLIENT_ID)           printf '%s' "${KEYCLOAK_FASTAPI_CLIENT_ID:-}" ;;
        *) printf '' ;;
    esac
}

TEMPLATE="${TEMPLATE:-/opt/keycloak/data/import-template/core-realm.json}"
# Keycloak's `start --import-realm` (the CE path, docker-compose.yml keycloak service) enforces
# the import filename: with a file whose name is not "<realm>-realm.json" it aborts startup with
# "File name / realm name mismatch. core-realm.json, contains realm modtex. File name should be
# modtex-realm.json" (proven on the pinned 26.3 image, gpu5 probe leg2, 2026-11-12). NOTE: the
# standalone `import --dir` entrypoint does NOT enforce this (it imports either name) - do not
# "fix" CE by renaming to core-realm.json, that breaks the start path.
OUTPUT="${OUTPUT:-/opt/keycloak/data/import/modtex-realm.json}"
OUTPUT_DIR="$(dirname "$OUTPUT")"

# The "realm" field baked into core-realm.json is the literal string
# "modtex" (not templated), so OUTPUT above is only correct as long as
# nothing has overridden the realm name elsewhere (KEYCLOAK_REALM_NAME is
# read by other services in docker-compose.yml). Fail loudly instead of
# silently importing under the wrong filename.
if [ "${KEYCLOAK_REALM_NAME:-modtex}" != "modtex" ]; then
    echo "render-realm.sh: KEYCLOAK_REALM_NAME=${KEYCLOAK_REALM_NAME:-} does not match the realm name baked into core-realm.json (modtex); update core-realm.json's \"realm\" field and this script's OUTPUT together, or unset KEYCLOAK_REALM_NAME." >&2
    exit 1
fi

# Fail fast: all client secrets must be set by ensure-client-secrets.sh before compose start.
for v in KEYCLOAK_FASTAPI_CLIENT_SECRET KEYCLOAK_FASTAPI_ADMIN_SECRET \
         KEYCLOAK_DASHBOARD_CLIENT_SECRET KEYCLOAK_SERVICE_CLIENT_SECRET; do
    if [ -z "$(env_value "$v")" ]; then
        echo "render-realm.sh: $v is unset — run ./keycloak/ensure-client-secrets.sh before docker compose up" >&2
        exit 1
    fi
done

# 1620: the seeded realm admin user's password is NOT given a random
# fallback here (unlike the client secrets above) — it is meant to be the
# SAME value scripts/first-run-secrets.sh wrote to .env (so the owner can
# read it back out of .env and actually log in with it). Fail loudly
# instead of silently minting a value the owner could never recover.
if [ -z "${KEYCLOAK_REALM_ADMIN_PASSWORD:-}" ]; then
    echo "render-realm.sh: KEYCLOAK_REALM_ADMIN_PASSWORD is unset — run scripts/first-run-secrets.sh; if the realm was already imported, reset the realm admin password in the Keycloak admin console or re-create the keycloak DB volume." >&2
    exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "render-realm.sh: template not found: $TEMPLATE" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
if [ ! -w "$OUTPUT_DIR" ]; then
    echo "render-realm.sh: output dir not writable by current user (uid $(id -u)): $OUTPUT_DIR" >&2
    exit 1
fi

# Build a sed program substituting only the placeholders above. Values are
# escaped for the replacement side (\ & and the | delimiter).
prog=""
# 1781: KEYCLOAK_FASTAPI_CLIENT_ID is in the sed loop below, but core-realm.json
# has NO ${KEYCLOAK_FASTAPI_CLIENT_ID} token: the fastapi-app clientId and the
# audiences are literal in the realm JSON. That substitution is a no-op, and
# changing the env var does NOT change the realm (the realm JSON is not edited).
# The default only keeps the sed program well-formed when the var is unset.
: "${KEYCLOAK_FASTAPI_CLIENT_ID:=fastapi-app}"
for v in KEYCLOAK_FASTAPI_CLIENT_SECRET KEYCLOAK_FASTAPI_ADMIN_SECRET \
         KEYCLOAK_DASHBOARD_CLIENT_SECRET KEYCLOAK_SERVICE_CLIENT_SECRET \
         KEYCLOAK_REALM_ADMIN_PASSWORD KEYCLOAK_FASTAPI_CLIENT_ID; do
    val=$(env_value "$v")
    esc=$(printf '%s' "$val" | sed 's/[\\&|]/\\&/g')
    prog="$prog s|\${$v}|$esc|g;"
done

# The output holds plaintext client secrets and the admin password: create it
# owner-only (umask 077) and force mode 600 after the write (belt and braces).
old_umask=$(umask)
umask 077
sed "$prog" < "$TEMPLATE" > "$OUTPUT"
chmod 600 "$OUTPUT"
umask "$old_umask"

# Fail closed: a placeholder that survived means the value was empty.
if grep -qE '"(secret|value)": "\$\{' "$OUTPUT"; then
    echo "render-realm.sh: unresolved \${...} credential placeholder in $OUTPUT" >&2
    exit 1
fi

exec /opt/keycloak/bin/kc.sh "$@"
