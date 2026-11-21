#!/bin/sh
# GT.B1 — render public/runtime-config.js from container env at start.
#
# The customer domain is a RUNTIME value, not a build arg: the image is built
# once per release and the domain is supplied per container via MOD_API_URL /
# MOD_DASHBOARD_URL (falling back to NEXT_PUBLIC_* so pre-existing containers
# and local `next start` keep working). Changing the domain therefore needs
# only a container recreate, never an image rebuild.
#
# Values are JSON-escaped with node (available in this image) so arbitrary
# domains can never break the JS we emit.
set -eu

API_URL="${MOD_API_URL:-${NEXT_PUBLIC_API_URL:-}}"
DASHBOARD_URL="${MOD_DASHBOARD_URL:-${NEXT_PUBLIC_DASHBOARD_URL:-}}"

# Next.js standalone serves public assets from <appdir>/public when present
# (the server resolves the public dir relative to its own location), so write
# to <workdir>/public — which is /app in the container (WORKDIR) — and also
# to <workdir>/.next/standalone/public when that layout exists.
APPDIR="$(pwd)"
TARGETS="$APPDIR/public"
[ -d "$APPDIR/.next/standalone" ] && TARGETS="$TARGETS $APPDIR/.next/standalone/public"

for d in $TARGETS; do
    mkdir -p "$d"
    API_URL="$API_URL" DASHBOARD_URL="$DASHBOARD_URL" node -e '
      const fs = require("fs");
      const cfg = {
        apiUrl: (process.env.API_URL || "").trim().replace(/\/+$/, ""),
        dashboardUrl: (process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, ""),
      };
      const out = "window.__MOD_RUNTIME_CONFIG__=" + JSON.stringify(cfg) + ";\n";
      fs.writeFileSync(process.argv[1], out);
      console.log("[runtime-config] wrote " + process.argv[1] + " -> " + out.trim());
    ' "$d/runtime-config.js"
done

# exec the original CMD (node server.js) so signals reach the right process.
exec "$@"
