#!/bin/sh
# Dashboard Config Injection Script
# Injects runtime configuration into the Dashboard

set -e

echo "🔧 Injecting runtime configuration into Dashboard..."

# Construct full URLs.
#
# Multi-domain (SaaS): every surface is a `<sub>.<base>` vhost, so the
# subdomains are prefixed to BASE_DOMAIN and the URLs are exactly as
# composed below (byte-identical to the pre-1900BR2b behaviour whenever a
# real subdomain is set).
#
# Single-host (CE): the CE compose ships EMPTY subdomains, and the pre-fix
# defaults shipped them equal to BASE_DOMAIN; either way composing `sub.base`
# produced the unreachable `localhost.localhost` (1900BR2b) that Chromium
# cannot resolve. When a subdomain is empty, or equals BASE_DOMAIN, there is
# no vhost: the surfaces are the plain host ports the CE compose publishes
# (open-core/overlay/docker-compose.yml — API "127.0.0.1:8000:80",
# Dashboard "127.0.0.1:3000:80", WorkflowBuilder "127.0.0.1:3001:3000").
if [ -n "$API_SUBDOMAIN" ] && [ "$API_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    API_URL="${PROTOCOL}://${API_SUBDOMAIN}.${BASE_DOMAIN}/v1"
else
    API_URL="${PROTOCOL}://${BASE_DOMAIN}:8000/v1"
fi
if [ -n "$DASHBOARD_SUBDOMAIN" ] && [ "$DASHBOARD_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    DASHBOARD_URL="${PROTOCOL}://${DASHBOARD_SUBDOMAIN}.${BASE_DOMAIN}"
else
    DASHBOARD_URL="${PROTOCOL}://${BASE_DOMAIN}:3000"
fi
if [ -n "$WORKFLOWS_SUBDOMAIN" ] && [ "$WORKFLOWS_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    WORKFLOWS_URL="${PROTOCOL}://${WORKFLOWS_SUBDOMAIN}.${BASE_DOMAIN}"
else
    WORKFLOWS_URL="${PROTOCOL}://${BASE_DOMAIN}:3001"
fi
# FIX-CE-DASHBOARD-DOCS-URL (11-11, from results-1110/S-compose-mod-dashboard.md R18):
# the docs site is NOT a vhost in single-host (CE) mode - mod-docs is published on
# the plain host port 3002 (compose service `mod-docs`), so the
# unconditional `docs.<base>` form shipped a dead host (`http://docs.localhost`)
# to every Help/docs link (js/help.js:68, js/runbook-links.js:21, js/ui.js:3006,
# analytics.html). Three shapes, keyed off what the compose actually passes:
#   1. a real subdomain (paid/SaaS: the nginx, mod-dashboard and caddy-front
#      services of CORE/startup/docker-compose.yml pass ${DOCS_SUBDOMAIN:-docs},
#      and .env.example ships dev-docs) -> vhost,
#      byte-identical to the pre-fix output.
#   2. SET but EMPTY (the CE compose line in the mod-dashboard service of the overlay,
#      `- DOCS_SUBDOMAIN=${DOCS_SUBDOMAIN:-}`) -> the published docs port.
#   3. UNSET (a deployment that never passes the var) -> the pre-fix default, so
#      paid behaviour is unchanged byte-for-byte.
# A subdomain equal to BASE_DOMAIN is not a vhost either (the pre-1900BR2b
# `localhost.localhost` Chromium cannot resolve) -> the port form, same rule the
# API/DASHBOARD/WORKFLOWS blocks above already use.
if [ -n "$DOCS_SUBDOMAIN" ] && [ "$DOCS_SUBDOMAIN" != "$BASE_DOMAIN" ]; then
    DOCS_URL="${PROTOCOL}://${DOCS_SUBDOMAIN}.${BASE_DOMAIN}"
elif [ "${DOCS_SUBDOMAIN+set}" = "set" ]; then
    DOCS_URL="${PROTOCOL}://${BASE_DOMAIN}:3002"
else
    DOCS_URL="${PROTOCOL}://docs.${BASE_DOMAIN}"
fi

# Resolve the Keycloak URL the browser must reach.
#
# Order of precedence:
#   1. KEYCLOAK_EXTERNAL_URL  — a full scheme://host URL (authoritative).
#   2. KEYCLOAK_HOSTNAME      — a bare hostname; prefix with ${PROTOCOL}.
#   3. keycloak.${BASE_DOMAIN} — a *subdomain* fallback.
#
# The apex/base domain is deliberately NEVER used: on this deployment it
# serves the public marketing site, so falling back to it sent the login
# and logout flows to the cloud page instead of Keycloak (issues #8/#10).
if [ -n "$KEYCLOAK_EXTERNAL_URL" ]; then
    KEYCLOAK_URL="${KEYCLOAK_EXTERNAL_URL}"
elif [ -n "$KEYCLOAK_HOSTNAME" ]; then
    KEYCLOAK_URL="${PROTOCOL}://${KEYCLOAK_HOSTNAME}"
else
    KEYCLOAK_URL="${PROTOCOL}://keycloak.${BASE_DOMAIN}"
    echo "⚠️  Neither KEYCLOAK_EXTERNAL_URL nor KEYCLOAK_HOSTNAME set; defaulting Keycloak to ${KEYCLOAK_URL}"
fi

# Create config.js file that will be loaded by the dashboard
cat > /usr/share/nginx/html/js/config.js << EOF
// Runtime configuration - Generated at container startup
window.APP_CONFIG = {
  DASHBOARD_URL: "${DASHBOARD_URL}",
  WORKFLOWS_URL: "${WORKFLOWS_URL}",
  API_URL: "${API_URL}",
  KEYCLOAK_URL: "${KEYCLOAK_URL}",
  BASE_DOMAIN: "${BASE_DOMAIN}",
  PROTOCOL: "${PROTOCOL}",
  KEYCLOAK_CLIENT_ID: "${KEYCLOAK_DASHBOARD_CLIENT_ID}",
  STRIPE_PUBLISHABLE_KEY: "${STRIPE_PUBLISHABLE_KEY}",
  DOCS_URL: "${DOCS_URL}",
  DEPLOYMENT_MODE: "${DEPLOYMENT_MODE:-onprem}"
};
console.log('✅ Runtime configuration loaded:', window.APP_CONFIG);
EOF

echo "✅ Configuration injected successfully"
echo "   Dashboard: ${DASHBOARD_URL}"
echo "   Workflows: ${WORKFLOWS_URL}"
echo "   API: ${API_URL}"
echo "   Keycloak: ${KEYCLOAK_URL}"

# Start nginx
echo "🚀 Starting nginx..."
exec nginx -g 'daemon off;'
