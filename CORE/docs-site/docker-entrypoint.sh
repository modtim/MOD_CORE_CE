#!/bin/sh
# GT.B1 — rewrite the dashboard URL placeholder in the built static site from
# the container env ($DASHBOARD_URL) at container start.
#
# The Docusaurus build bakes in the placeholder (docusaurus.config.ts defaults
# DASHBOARD_URL to https://__MOD_DASHBOARD_URL__, a full https URL so Docusaurus
# classifies the navbar logo as an external link and skips broken-link
# validation under onBrokenLinks: 'throw'). This script substitutes the whole
# placeholder with the real per-container value, so a domain change needs only
# a container recreate, never an image rebuild.
set -eu

# The exact token the build bakes in (scheme included) — replaced atomically.
TOKEN='https://__MOD_DASHBOARD_URL__'

DASH_URL="${DASHBOARD_URL:-/}"

# Strict whitelist: the value must be a safe URL (scheme + host) or "/".
# Guards against a bad env value corrupting the static HTML.
case "$DASH_URL" in
    http://*|https://*|/|'') ;;
    *)
        echo "[docs] ignoring unsafe DASHBOARD_URL (use http(s)://... or /)" >&2
        DASH_URL="/"
        ;;
esac
[ -z "$DASH_URL" ] && DASH_URL="/"

# Escape sed replacement metacharacters (\, &, /) so any legal URL value is
# substituted literally.
SAFE_URL=$(printf '%s' "$DASH_URL" | sed -e 's/[\\&/]/\\&/g')
# Escape the search pattern too (it only contains alnum + '_', but be safe).
SAFE_TOKEN=$(printf '%s' "$TOKEN" | sed -e 's/[\\&/]/\\&/g')

find /usr/share/nginx/html -type f \( -name '*.html' -o -name '*.js' -o -name '*.json' \) -print0 2>/dev/null \
  | xargs -0 -r sed -i "s/$SAFE_TOKEN/$SAFE_URL/g" 2>/dev/null || true

echo "[docs] runtime DASHBOARD_URL -> $DASH_URL"

exec "$@"
