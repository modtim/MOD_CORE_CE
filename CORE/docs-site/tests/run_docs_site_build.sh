#!/usr/bin/env bash
# Docs-site build wrapper for the r9700-run.sh hosttest verb.
# The host runs exactly: timeout 540 bash <path> from the synced tree root.
set -u
cd "$(dirname "$0")/.."

echo "node: $(node --version 2>&1)"
echo "npm: $(npm --version 2>&1)"

if [ ! -d node_modules ]; then
  npm ci --no-audit --no-fund --prefer-offline || echo "npm ci rc=$?"
fi

npm run build > /tmp/docs-site-build.log 2>&1
rc=$?

echo "DOCS_BUILD_RC=$rc"
echo "DOCS_BUILD_ERROR_LINES=$(grep -c -i -E '^\[ERROR\]|Error:' /tmp/docs-site-build.log)"
tail -15 /tmp/docs-site-build.log
exit $rc
