#!/bin/bash
# =============================================================================
# 1665n2 — CE one-shot: turn the Keycloak 26.x TEMPORARY bootstrap admin of the
# `master` realm into a PERMANENT admin whose password is the one
# first-run-secrets.sh wrote into .env (KEYCLOAK_ADMIN_PASSWORD; compose maps it
# to KC_BOOTSTRAP_ADMIN_PASSWORD).
#
# Why: KC 26.3 creates the bootstrap admin as a TEMPORARY admin
# (log `KC-SERVICES0077: Created temporary admin user`), forces a
# requiredAction reset, and the stranger then cannot use the .env
# credential in the admin console.
#
# This runs as a separate one-shot compose service (`keycloak-admin-init`,
# restart: "no", depends_on keycloak: service_healthy) — NOT as a PID-1
# wrapper around Keycloak, so `docker stop` never hangs (1665n bug 4).
#
# Password handling (1665n bugs 1/2/3):
#   * the password NEVER reaches argv: kcadm.sh reads it from the
#     KC_CLI_PASSWORD environment variable (verified against the image's
#     own `kcadm.sh config credentials --help`), and the new password is
#     POSTed to the reset-password endpoint as stdin JSON built with the
#     `printf` BUILTIN;
#   * auth, user-lookup, password-set, requiredActions-clear and final
#     re-proof failures exit non-zero with a password-free message. Three
#     steps are deliberately non-fatal: realm policy/lockout converge (a
#     failed update prints WARN and exits non-zero, never a success line),
#     origins converge (sourced, best-effort, WARN on failure), and the
#     temporary-marker drop (note only);
#   * the env vars this service gets are KC_BOOTSTRAP_ADMIN_USERNAME,
#     KC_BOOTSTRAP_ADMIN_PASSWORD and CE_PUBLIC_HOST (the keycloak-admin-init
#     environment block in docker-compose.yml);
#     KC_REALM_NAME is optional and defaults to modtex.
#
# Idempotent: a second `docker compose up` re-runs this and logs
# "already permanent" then exits 0.
# =============================================================================
set -eu

KC=/opt/keycloak/bin/kcadm.sh
# In-network service name of the keycloak service (same name the KC_DB_URL_HOST
# alias uses; the container's own healthcheck probes 127.0.0.1:9000).
SERVER=http://keycloak:8080

ADMIN_USER=${KC_BOOTSTRAP_ADMIN_USERNAME:?keycloak-admin-init needs KC_BOOTSTRAP_ADMIN_USERNAME}
ADMIN_PW=${KC_BOOTSTRAP_ADMIN_PASSWORD:?keycloak-admin-init needs KC_BOOTSTRAP_ADMIN_PASSWORD}

fail() {
  echo "keycloak-admin-init FAILED: $1 (no secret is ever printed)" >&2
  exit 1
}

# ADMIN_USER is interpolated into the kcadm user query below, so it is validated
# before any kcadm call (a quote, '&' or space would break the query).
case "$ADMIN_USER" in
  ''|-*) fail "KC_BOOTSTRAP_ADMIN_USERNAME is empty or starts with a dash" ;;
  *[!A-Za-z0-9._@-]*) fail "KC_BOOTSTRAP_ADMIN_USERNAME has characters outside [A-Za-z0-9._@-]" ;;
esac
[ "${#ADMIN_USER}" -le 128 ] || fail "KC_BOOTSTRAP_ADMIN_USERNAME is longer than 128 characters"

# --- 1. Authenticate the bootstrap admin (password from env, never argv) -----
# `config credentials` performs the master-realm password grant for the
# `keycloak` admin-API client; if it fails, nothing below can work.
KC_CLI_PASSWORD="$ADMIN_PW" "$KC" config credentials \
  --server "$SERVER" --realm master --user "$ADMIN_USER" \
  || fail "bootstrap-admin credentials grant to the master realm was rejected"

# --- 1b. 2400LR-ceKCpol — password policy + lockout on the modtex realm ------
# `start --import-realm` only applies core-realm.json to a FRESH realm, so an
# existing install (DB volume already populated, realm re-used) keeps whatever
# policy it booted with. Converge the same defaults the fresh import now ships,
# here, using the master-realm admin we just authenticated above (password stays
# in KC_CLI_PASSWORD, never argv, never printed).
# Values mirror the fresh-install lockout defaults (protected true, permanent
# false, failureFactor 5, waitIncrement 60 s, maxFailureWait 900 s,
# maxDeltaTime 43200 s, quickLoginCheck 1000 ms, minQuickLoginWait 60 s) and
# `length(N) and passwordHistory(5)` with N=12. NO passwordBlacklist clause:
# CE ships no blacklist file.
# Idempotent AND non-clobbering: if the realm already declares a >=12-char
# length policy, passwordHistory and bruteForceProtected=true, log and skip, so
# a stricter operator setting is never walked over.
POLICY='length(12) and passwordHistory(5) and notUsername and notEmail'
KC_REALM=${KC_REALM_NAME:-modtex}   # render-realm.sh pins the realm to modtex
realm_rec=$("$KC" get "realms/${KC_REALM}") \
  || { echo "keycloak-admin-init: WARN could not read ${KC_REALM} realm record" >&2; realm_rec="{}"; }
pol=$(printf '%s' "$realm_rec" | grep -m1 -o '"passwordPolicy"[[:space:]]*:[[:space:]]*"[^"]*"' || true)
pol_len=$(printf '%s' "$pol" | grep -m1 -o 'length([0-9]\{1,\})' | grep -m1 -o '[0-9]\{1,\}' || true)
has_history=0
if printf '%s' "$pol" | grep -q 'passwordHistory('; then has_history=1; fi
bf_on=0
if printf '%s' "$realm_rec" | grep -Eq '"bruteForceProtected"[[:space:]]*:[[:space:]]*true'; then bf_on=1; fi
if [ "${pol_len:-0}" -ge 12 ] && [ "$has_history" = 1 ] && [ "$bf_on" = 1 ]; then
  echo "keycloak-admin-init: realm ${KC_REALM} already has a password policy (length >= ${pol_len}) + brute-force protection; leaving it alone"
else
  if "$KC" update "realms/${KC_REALM}" \
    -s "passwordPolicy=${POLICY}" \
    -s bruteForceProtected=true \
    -s permanentLockout=false \
    -s failureFactor=5 \
    -s waitIncrementSeconds=60 \
    -s maxFailureWaitSeconds=900 \
    -s maxDeltaTimeSeconds=43200 \
    -s quickLoginCheckMilliSeconds=1000 \
    -s minimumQuickLoginWaitSeconds=60; then
    echo "keycloak-admin-init: realm ${KC_REALM} now has passwordPolicy=${POLICY} + brute-force lockout (failureFactor 5)"
  else
    # FAIL LOUDLY: no success line, non-zero exit (the compose one-shot fails visibly).
    echo "keycloak-admin-init: WARN realm ${KC_REALM} NOT converged (policy/lockout unchanged)" >&2
    exit 1
  fi
fi

# --- 1c. AUTH-7 — brute-force protection on the MASTER realm ------------------
# The master realm (admin console on :8080) ships with bruteForceProtected=false
# (Keycloak default), so the admin password could be guessed without limit.
# Same TEMPORARY lockout values as step 1b (permanentLockout=false: the bootstrap
# admin is only ever delayed, never locked out for good). Runs after the
# credentials grant above. Best-effort by design: a failure logs a WARN and the
# script continues (the admin login this script finishes must not depend on it).
# Idempotent: skipped when the master realm already has bruteForceProtected=true.
master_rec=$("$KC" get "realms/master") \
  || { echo "keycloak-admin-init: WARN could not read master realm record" >&2; master_rec="{}"; }
if printf '%s' "$master_rec" | grep -Eq '"bruteForceProtected"[[:space:]]*:[[:space:]]*true'; then
  echo "keycloak-admin-init: master realm already has brute-force protection; leaving it alone"
elif "$KC" update "realms/master" \
    -s bruteForceProtected=true \
    -s permanentLockout=false \
    -s failureFactor=5 \
    -s waitIncrementSeconds=60 \
    -s maxFailureWaitSeconds=900 \
    -s maxDeltaTimeSeconds=43200 \
    -s quickLoginCheckMilliSeconds=1000 \
    -s minimumQuickLoginWaitSeconds=60; then
  echo "keycloak-admin-init: master realm now has brute-force lockout (failureFactor 5, temporary)"
else
  echo "keycloak-admin-init: WARN master realm brute-force protection NOT enabled (best-effort; continuing)" >&2
fi

# --- 1b. Converge the realm clients' redirect/web origins to PUBLIC_HOST ---
# Best-effort by design (converge-client-origins.sh always returns 0); a
# failure to source it is reported, not fatal: login does not depend on it.
. /opt/keycloak/init/converge-client-origins.sh \
  || echo "keycloak-admin-init: WARN origins converge did not complete (best-effort; login unaffected)" >&2

# --- 2. Look the user up -----------------------------------------------------
# NB: `--fields id,requiredActions,attributes` must NOT be used here — the
# fields filter collapses the nested `attributes` object to `{ }` and hides the
# `is_temporary_admin` marker KC 26.3 stores inside it, which would make the
# marker check blind. Read the full record instead.
rec=$("$KC" get users -r master -q "username=${ADMIN_USER}") \
  || fail "could not read the master-realm user record"
[ -n "$(printf '%s' "$rec" | grep -m1 -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"')" ] \
  || fail "no such master-realm user: ${ADMIN_USER}"

uid=$(printf '%s' "$rec" | grep -m1 -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | cut -d'"' -f4)

has_actions=0
if printf '%s' "$rec" | grep -Eq '"requiredActions"[[:space:]]*:[[:space:]]*\[[[:space:]]*["a-zA-Z]'; then
  has_actions=1
fi
marker=$(printf '%s' "$rec" | grep -io '"[a-z_]*temporary[a-z_]*"' | head -1 | cut -d'"' -f2 || true)

# --- 3. Idempotency ----------------------------------------------------------
# The brief's test is "no requiredActions AND no temporary marker AND the grant
# already succeeds". KC 26.3 may keep the `is_temporary_admin` attribute, so that
# third leg may never be satisfiable on a re-run; the two legs that decide
# whether the stranger can log in are the forced action and the password grant,
# so we gate on those and treat the marker as informational.
if [ "$has_actions" = 0 ] && KC_CLI_PASSWORD="$ADMIN_PW" "$KC" config credentials \
     --server "$SERVER" --realm master --user "$ADMIN_USER" >/dev/null 2>&1; then
  echo "keycloak-admin-init: master-realm admin '${ADMIN_USER}' is already permanent (the .env password authenticates, no requiredActions); nothing to do"
  exit 0
fi

# --- 4. Set the password permanently (stdin JSON, printf builtin) ------------
# bash-only escaping of the two JSON-hostile characters; first-run-secrets.sh
# generates an alphanumeric password, so control chars are not a concern.
esc=${ADMIN_PW//\\/\\\\}
esc=${esc//\"/\\\"}
# CredentialRepresentation body (the reset-password endpoint takes the
# credential itself, per the image's own `kcadm.sh update users/ID/reset-password`
# help: -s type=password -s value=... -s temporary=true). We send it as stdin
# JSON instead of -s argv pairs so the password never reaches argv.
json=$(printf '{"type":"password","value":"%s","temporary":false}' "$esc")
printf '%s' "$json" | "$KC" update "users/${uid}/reset-password" -r master -f - \
  || fail "could not set the permanent password on user ${uid}"

# --- 5. Clear forced-actions + drop the temporary marker ---------------------
"$KC" update "users/${uid}" -r master -s 'requiredActions=[]' \
  || fail "could not clear requiredActions on user ${uid}"

if [ -n "$marker" ]; then
  # KC 26.3 stores `is_temporary_admin` in the user's attributes and may keep it
  # through the admin API (both `-s ...=[]` and `-d attributes.is_temporary_admin`
  # may leave it in place). The flag is informational only: what actually matters
  # — and what the stranger's admin console needs — is that the .env password is
  # accepted and no requiredAction is forced, both proven below. So we attempt
  # the drop best-effort and gate on the real signal (the password grant).
  echo "keycloak-admin-init: attempting to drop temporary-admin attribute '${marker}' (KC 26.3 may keep the flag; the grant check below is the real test)"
  "$KC" update "users/${uid}" -r master -d "attributes.${marker}" \
    || echo "keycloak-admin-init: note — KC 26.3 kept the '${marker}' attribute (not clearable via the admin API)"
fi

# --- 6. Re-read and confirm (full record: the fields filter hides the marker)
rec=$("$KC" get users -r master -q "username=${ADMIN_USER}") \
  || fail "re-read of the user record failed"
# The real proof of permanence: the .env password now authenticates on the
# master realm (this IS the password grant) and no action is forced.
KC_CLI_PASSWORD="$ADMIN_PW" "$KC" config credentials \
  --server "$SERVER" --realm master --user "$ADMIN_USER" >/dev/null 2>&1 \
  || fail "the .env password was still not accepted on the master realm"
if printf '%s' "$rec" | grep -Eq '"requiredActions"[[:space:]]*:[[:space:]]*\[[[:space:]]*["a-zA-Z]'; then
  fail "requiredActions still set after permanentize"
fi
echo "keycloak-admin-init: master-realm admin '${ADMIN_USER}' is now PERMANENT — the .env password authenticates on the master realm, no requiredActions are forced (the informational is_temporary_admin attribute, if KC still echoes it, may not be clearable via the 26.3 admin API and does not affect login)"
