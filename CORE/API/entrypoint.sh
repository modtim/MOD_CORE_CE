#!/bin/bash
# API/entrypoint.sh

set -a
if [ -f /run/secrets/secret_encryption_key.env ]; then
  source /run/secrets/secret_encryption_key.env
fi
# Disable 'allexport' after sourcing to avoid side effects
set +a

# OpenBao AppRole role_id/secret_id (brief 1617) — openbao-init writes these
# as raw file content (not KEY=value) into the openbao-init-state volume, so
# this is a cat+export, not a `source` (same file-then-env shape as the
# encryption-key secret above). VAULT_ADDR itself is a plain compose
# env var, not a file. Missing files are tolerated here (empty string) —
# database.py/secret_cud.py raise their own clear errors if Vault is required
# but unconfigured.
if [ -f /run/openbao/approle_role_id ]; then
  export VAULT_APPROLE_ROLE_ID="$(cat /run/openbao/approle_role_id)"
fi
if [ -f /run/openbao/approle_secret_id ]; then
  export VAULT_APPROLE_SECRET_ID="$(cat /run/openbao/approle_secret_id)"
fi

set -e

if [ -z "${KEYCLOAK_REALM_NAME}" ]; then
  echo "❌ ERROR: KEYCLOAK_REALM_NAME environment variable is not set."
  exit 1
fi

# --- KEYCLOAK OIDC URL begin ---
# Host comes from KEYCLOAK_SERVER_URL: compose sets http://keycloak:8080/
# (CORE/startup/docker-compose.yml), the helm chart sets the cluster Service host
# (deploy/helm/mod/templates/api-deployment.yaml via _helpers.tpl). Fallback = compose host.
if [ -n "${KEYCLOAK_SERVER_URL:-}" ]; then
  KC_BASE="${KEYCLOAK_SERVER_URL%/}"
else
  KC_BASE="http://keycloak:8080"
fi
WELL_KNOWN_URL="${KC_BASE}/realms/${KEYCLOAK_REALM_NAME}/.well-known/openid-configuration"
# --- KEYCLOAK OIDC URL end ---
MAX_RETRIES=40
RETRY_INTERVAL=5

echo "--> Waiting for Keycloak OIDC configuration at: ${WELL_KNOWN_URL}"

# Loop until the endpoint is available and returns a success (2xx) code
for i in $(seq 1 $MAX_RETRIES); do
  # Use --insecure or a proper CA setup if you encounter TLS issues with Keycloak in the future
  if curl -s --head --fail "$WELL_KNOWN_URL" > /dev/null; then
    echo "✅ Keycloak is ready."
    break
  else
    echo "--> Attempt ${i}/${MAX_RETRIES}: Keycloak not ready. Retrying in ${RETRY_INTERVAL}s..."
    sleep $RETRY_INTERVAL
  fi
done

# Final check after the loop
if ! curl -s --head --fail "$WELL_KNOWN_URL" > /dev/null; then
  echo "❌ ERROR: Timed out waiting for Keycloak service."
  exit 1
fi

# If the check was successful, initialize platform tenant
echo "--> Creating platform tenant (ID=1)..."
python3 create_tenant.py || echo "⚠️  Platform tenant creation failed (may already exist)"

# Subscription plans are admin-managed (launch-day-followup-2 #15);
# no auto-seed here. Pre-existing plan rows are preserved by virtue
# of not being touched. Fresh deployments expect a platform admin
# to populate plans via Dashboard → Admin → Plans.

# Validate platform license (for on-prem deployments).
# In CE the license_client module is stripped by the manifest, so guard
# the check with a presence probe; the API always starts and the middleware
# enforces entitlements (no license = CE features only).
echo "--> Checking platform license..."
if python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('utils.license_client') else 1)"; then
  python3 -c "
from utils.license_client import validate_license

result = validate_license()

if not result['valid']:
    print()
    print('=' * 60)
    print('⚠️  NO VALID LICENSE')
    print('=' * 60)
    print()
    print(f\"Status: {result['message']}\")
    print()
    print('The API will start with the features available in this build.')
    print('Licensing is configured in the Dashboard Admin section.')
    print()
    print('=' * 60)
else:
    # License is valid - show info
    from_cache = ' (cached)' if result.get('from_cache') else ''

    print(f'✅ License valid{from_cache}')
    if not result.get('development_mode'):
        print(f\"   Type: {result.get('license_type', 'unknown')}\")
        print(f\"   Max Workers: {result['max_workers']}\")
        print(f\"   Expires: {result.get('expires_at', 'N/A')}\")

        if result.get('in_grace_period'):
            days_left = result.get('days_until_cutoff', 0)
            print(f'   ⚠️  GRACE PERIOD: {days_left} days until shutdown')

        if result.get('from_cache'):
            cached_at = result.get('cached_at', 'unknown')
            print(f\"   Cached Since: {cached_at}\")
"
else
  echo "--> Community Edition: no license check"
fi

# scale-tiered-workloads D1 (tasks §2.1) — one image, two roles.
#   MOD_API_ROLE=web         (default) uvicorn + HTTP routes, as always.
#   MOD_API_ROLE=background  scheduler + orchestration queue consumer + ML/
#                            ingest loops via run_background_worker(); NO uvicorn,
#                            so it serves no HTTP request path.
# Unset/unknown -> "web", so a default deploy (no env changes) is byte-identical
# to today: it never reaches the background branch and runs uvicorn exactly as
# before. The shared setup above (Keycloak wait, tenant, license) runs for both
# roles because it is idempotent and the background role needs the same context.
MOD_API_ROLE_NORM="$(printf '%s' "${MOD_API_ROLE:-web}" | tr '[:upper:]' '[:lower:]')"
if [ "$MOD_API_ROLE_NORM" = "background" ]; then
  echo "--> Starting API in BACKGROUND role (scheduler + queue consumer + ML; no HTTP listener)..."
  exec python3 -c "import main; main.run_background_worker()"
fi

# Size the worker pool to this machine rather than to the one it was tuned on.
# Prints the count on stdout and its reasoning (all three ceilings, and which
# one bound the result) on stderr, so the container log says WHY. Falls back to
# a conservative 4 if the probe cannot run at all — a wrong-but-serving API
# beats one that will not start.
#
# `|| true` is load-bearing: this script runs under `set -e`, so without it a
# probe that raised would abort the entrypoint and the container would never
# start uvicorn at all — the fallback below would be unreachable.
echo "--> Sizing uvicorn worker pool..."
API_WORKERS="$(python3 -m utils.worker_sizing || true)"
case "$API_WORKERS" in
    ''|*[!0-9]*)
        echo "--> WARNING: worker sizing failed, falling back to 4"
        API_WORKERS=4
        ;;
esac

# Execute the original command to start the FastAPI server
echo "--> Starting FastAPI application with ${API_WORKERS} workers..."
exec uvicorn main:app --host 0.0.0.0 --port 80 --workers "$API_WORKERS"