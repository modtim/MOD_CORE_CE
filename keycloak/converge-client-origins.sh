#!/bin/sh
# keycloak/converge-client-origins.sh - sourced by permanentize-admin.sh (the
# keycloak-admin-init one-shot) AFTER its kcadm login, so $KC is already
# authenticated. Makes the three realm clients accept the install's public host
# (CE_PUBLIC_HOST, from compose PUBLIC_HOST) in redirectUris and webOrigins.
# Why here: --import-realm only imports on a FRESH realm, so editing
# core-realm.json cannot help an existing install; this runs on every
# (re)create of keycloak-admin-init and is idempotent (skip when the host is
# already present). The localhost entries are always kept.
# Never prints secrets; the host is validated before it reaches kcadm.
# Origins convergence is BEST-EFFORT by design: every path returns 0 and a
# failure is reported as a WARN line; login does not depend on it.
KC_REALM_N="${KC_REALM_NAME:-modtex}"
H="${CE_PUBLIC_HOST:-localhost}"
case "$H" in
  localhost|127.0.0.1|'') echo "converge-client-origins: localhost-only install; nothing to add"; return 0 2>/dev/null || exit 0 ;;
esac
case "$H" in
  *[!A-Za-z0-9.-]*) echo "converge-client-origins: WARN refusing PUBLIC_HOST with unexpected characters" >&2; return 0 2>/dev/null || exit 0 ;;
esac

converge_client() { # converge_client <clientId> <port> [<port> ...]
  cid="$1"; shift
  # kcadm stderr is NOT discarded: the lookup error reaches the log before the WARN.
  rec=$("$KC" get clients -r "$KC_REALM_N" -q "clientId=$cid" --fields id,redirectUris) \
    || { echo "converge-client-origins: WARN cannot read client $cid" >&2; return 0; }
  uuid=$(printf '%s' "$rec" | grep -m1 -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/')
  [ -n "$uuid" ] || { echo "converge-client-origins: WARN client $cid not found" >&2; return 0; }
  first="$1"
  if printf '%s' "$rec" | grep -qF "\"http://$H:$first/*\""; then
    echo "converge-client-origins: $cid already allows $H"; return 0
  fi
  ru=""; wo=""
  for p in "$@"; do
    ru="$ru\"http://localhost:$p/\",\"http://localhost:$p/*\",\"http://$H:$p/\",\"http://$H:$p/*\","
    wo="$wo\"http://localhost:$p\",\"http://$H:$p\","
  done
  "$KC" update "clients/$uuid" -r "$KC_REALM_N" \
    -s "redirectUris=[${ru%,}]" -s "webOrigins=[${wo%,}]" \
    && echo "converge-client-origins: $cid now allows localhost and $H" \
    || echo "converge-client-origins: WARN could not update $cid" >&2
}

converge_client workflow-builder-client 3001 3000
converge_client dashboard-client 3000
converge_client fastapi-app 8000
