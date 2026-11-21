#!/usr/bin/env bash
# Build + deploy the WorkflowBuilder from the deployment's OWN config — no
# hand-typed URLs. GT.B1: the domain is RUNTIME config now (docker-entrypoint.sh
# renders public/runtime-config.js from MOD_API_URL / MOD_DASHBOARD_URL at
# container start), so the URLs are resolved from CORE/startup/.env here and
# passed as container -e values — never build args. Changing the domain needs
# only a container recreate, not an image rebuild.
#
# Usage:  ./build-and-deploy.sh            # build + recreate the container
#         ./build-and-deploy.sh --build-only
set -eo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../startup/.env"
IMAGE="mod-workflow-builder:latest"
CONTAINER="workflow_builder"
NETWORK="mod_modtex-network"
ALIAS="workflow-builder"
HOST_PORT="3001"      # host:container = 3001:3000
DEPS_IMAGE_DEFAULT="node:20-alpine"
DEPS_IMAGE_OFFLINE="mod-workflow-builder-deps:offline"

[ -f "$ENV_FILE" ] || { echo "ERROR: env file not found: $ENV_FILE" >&2; exit 1; }

# Read specific keys out of the deployment config WITHOUT executing it — .env
# contains ${VAR} interpolations meant for docker-compose, so a full `source`
# would fail on unset vars. This just pulls the literal KEY="value" lines.
getenv() { grep -E "^${1}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'; }

PROTOCOL="$(getenv PROTOCOL)"
BASE_DOMAIN="$(getenv BASE_DOMAIN)"
API_SUBDOMAIN="$(getenv API_SUBDOMAIN)"
DASHBOARD_SUBDOMAIN="$(getenv DASHBOARD_SUBDOMAIN)"

# Prefer explicit NEXT_PUBLIC_* vars; fall back to composing from the subdomain
# vars so a deployment that only sets those still builds correctly.
API_URL="$(getenv NEXT_PUBLIC_API_URL)"
DASH_URL="$(getenv NEXT_PUBLIC_DASHBOARD_URL)"
# 1900BR2b: single-host (CE) installs ship empty subdomains (or subdomains
# equal to BASE_DOMAIN); composing `sub.base` there produced the unreachable
# localhost.localhost. When a subdomain is empty or equals BASE_DOMAIN there is
# no vhost — use the plain host ports the compose publishes (API 8000,
# Dashboard 3000). With real subdomains the URLs are byte-identical to before.
if [ -z "$API_URL" ]; then
  if [ -n "$API_SUBDOMAIN" ] && [ "$API_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    API_URL="${PROTOCOL}://${API_SUBDOMAIN}.${BASE_DOMAIN}"
  else
    API_URL="${PROTOCOL}://${BASE_DOMAIN}:8000"
  fi
fi
if [ -z "$DASH_URL" ]; then
  if [ -n "$DASHBOARD_SUBDOMAIN" ] && [ "$DASHBOARD_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    DASH_URL="${PROTOCOL}://${DASHBOARD_SUBDOMAIN}.${BASE_DOMAIN}"
  else
    DASH_URL="${PROTOCOL}://${BASE_DOMAIN}:3000"
  fi
fi

[ -n "${API_URL:-}" ]  || { echo "ERROR: could not resolve API URL (NEXT_PUBLIC_API_URL or subdomains) from $ENV_FILE" >&2; exit 1; }
[ -n "${DASH_URL:-}" ] || { echo "ERROR: could not resolve dashboard URL (NEXT_PUBLIC_DASHBOARD_URL or subdomains) from $ENV_FILE" >&2; exit 1; }

# v2 API: every route is served ONLY under /v1 now (no un-versioned
# fallback). Append it here — once — so it can never drift regardless of
# whether $ENV_FILE's NEXT_PUBLIC_API_URL was set bare or already carries
# /v1 from a previous run of this script.
case "$API_URL" in
  */v1|*/v1/) ;;
  *) API_URL="${API_URL%/}/v1" ;;
esac

# Use the offline deps image (node_modules baked) when it exists — faster and
# works air-gapped; otherwise the online default pulls deps.
if docker image inspect "$DEPS_IMAGE_OFFLINE" >/dev/null 2>&1; then
  DEPS_IMAGE="$DEPS_IMAGE_OFFLINE"
else
  DEPS_IMAGE="$DEPS_IMAGE_DEFAULT"
fi

echo "Building $IMAGE"
echo "  DEPS_IMAGE            = $DEPS_IMAGE"
# GT.B1: runtime config (no customer build args)
echo "  MOD_API_URL       = $API_URL   (from $ENV_FILE)"
echo "  MOD_DASHBOARD_URL = $DASH_URL   (from $ENV_FILE)"

docker build \
  --build-arg DEPS_IMAGE="$DEPS_IMAGE" \
  -t "$IMAGE" "$HERE"

if [ "${1:-}" = "--build-only" ]; then
  echo "Built $IMAGE (--build-only; container not recreated)."
  exit 0
fi

# Recreate the container by hand (compose is broken for this stack).
echo "Recreating container $CONTAINER"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --restart unless-stopped \
  --network "$NETWORK" \
  --network-alias "$ALIAS" \
  -p "${HOST_PORT}:3000" \
  # GT.B1: runtime customer config (was NEXT_PUBLIC_* build args)
  -e MOD_API_URL="$API_URL" \
  -e MOD_DASHBOARD_URL="$DASH_URL" \
  "$IMAGE" >/dev/null

echo "Deployed. Waiting for it to serve…"
for _ in $(seq 1 15); do
  if docker exec "$CONTAINER" sh -c 'wget -qO /dev/null -T 5 http://localhost:3000/ 2>/dev/null'; then
    echo "WorkflowBuilder is serving ✓"; exit 0
  fi
  sleep 3
done
echo "WARNING: container up but not serving yet — check 'docker logs $CONTAINER'." >&2
