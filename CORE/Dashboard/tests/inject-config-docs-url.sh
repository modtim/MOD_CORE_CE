#!/bin/sh
# FIX-CE-DASHBOARD-DOCS-URL - DOCS_URL branch check for CORE/Dashboard/inject-config.sh
# (from results-1110/S-compose-mod-dashboard.md R18).
#
# Run through the harness ONLY (never on a live stack, never docker compose up):
#   MCW_BASE_DIR=_base_cloud-h57 MCW_RUN_ID=cloud-h57-docsurl MCW_HOST=gpu5 \
#     <test harness> hosttest CORE/Dashboard/tests/inject-config-docs-url.sh
#
# hosttest cds to the synced repo root and runs `bash <this file>` with no env
# passthrough, so every case sets its own env explicitly. The script under test
# writes /usr/share/nginx/html/js/config.js and ends with `exec nginx -g ...`;
# neither is available here, so each case runs a copy with ONLY those two lines
# adapted (config path -> a temp file, exec nginx -> true). The DOCS_URL logic
# itself is exercised verbatim, never re-implemented.
#
# Cases (brief WORK item 3):
#   a CE LAN     PROTOCOL=http  BASE_DOMAIN=203.0.113.150 DOCS_SUBDOMAIN='' (SET)   -> http://203.0.113.150:3002
#   b CE local   PROTOCOL=http  BASE_DOMAIN=localhost      DOCS_SUBDOMAIN='' (SET)   -> http://localhost:3002
#   c paid       PROTOCOL=https BASE_DOMAIN=example.com    DOCS_SUBDOMAIN UNSET      -> https://docs.example.com
#     c2: the FULL generated config for the optional env is byte-identical to the pre-fix
#         expression (paid behaviour unchanged; the harness-level MCW_OVERRIDE run
#         proves the same for the whole old file).
#   d explicit   PROTOCOL=https BASE_DOMAIN=example.com    DOCS_SUBDOMAIN=docs       -> https://docs.example.com
#   e sub==base  PROTOCOL=https BASE_DOMAIN=example.com    DOCS_SUBDOMAIN=example.com -> https://example.com:3002
#   f mutation   the OLD one-liner (pre-fix :38) under env (a) must NOT match (a)
#   g generated config.js stays valid JS (11 keys, one object, quoted values)
#
# Exit 0 = all passed. Prints "docs-url-check: N passed, M failed".

SCRIPT=CORE/Dashboard/inject-config.sh
if [ ! -f "$SCRIPT" ]; then
    echo "docs-url-check: inject-config.sh not in the run tree" >&2
    exit 2
fi

TMPD="${TMPDIR:-/tmp}/inject-docs-url.$$"
mkdir -p "$TMPD"
CFG="$TMPD/config.js"
LOG="$TMPD/gen.log"
RUNNER="$TMPD/runner.sh"

prep() {
    # $1 = script under test -> a copy whose config path and nginx exec are local
    sed 's#/usr/share/nginx/html/js/config.js#'"$CFG"'#' "$1" \
        | sed 's#^exec nginx.*#true#' > "$RUNNER"
}

gen() {
    rm -f "$CFG"
    sh "$RUNNER" > "$LOG" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "gen rc=$rc (last 5 lines:)"; tail -5 "$LOG"
    fi
    return $rc
}

docs_url() {
    sed -n 's/.*DOCS_URL: "\(.*\)".*/\1/p' "$CFG" | head -1
}

pass=0
fail=0

# --- (i) the script under test still parses under /bin/sh (no bashisms) ---
if sh -n "$SCRIPT" 2>/dev/null; then
    pass=$((pass + 1)); echo "PASS i sh-n rc=0"
else
    fail=$((fail + 1)); echo "FAIL i sh-n rc!=0"; sh -n "$SCRIPT"
fi

check() {
    got=$(docs_url)
    if [ "$got" = "$2" ]; then
        pass=$((pass + 1)); echo "PASS $1 -> $got"
    else
        fail=$((fail + 1)); echo "FAIL $1 want=$2 got=$got"
    fi
}

# --- (a) CE LAN shape: what open-core/overlay/docker-compose.yml:1166 now passes ---
PROTOCOL=http
BASE_DOMAIN=203.0.113.150
DOCS_SUBDOMAIN=
export PROTOCOL BASE_DOMAIN DOCS_SUBDOMAIN
prep "$SCRIPT"
gen
check a 'http://203.0.113.150:3002'
# --- (b) CE localhost shape (the compose default) ---
BASE_DOMAIN=localhost
gen
check b 'http://localhost:3002'

# --- (c) paid shape: DOCS_SUBDOMAIN UNSET (CORE/startup/docker-compose.yml:376/2131/3118
#     pass ${DOCS_SUBDOMAIN:-docs}, so the variable is never empty on paid) ---
unset DOCS_SUBDOMAIN
PROTOCOL=https
BASE_DOMAIN=example.com
export PROTOCOL BASE_DOMAIN
gen
check c 'https://docs.example.com'
CFG_C="$TMPD/config-c.js"; cp "$CFG" "$CFG_C"

# --- (c2) the pre-fix expression, verbatim from inject-config.sh:38, same env ---
old_url=$(printf '%s' "${PROTOCOL}://${DOCS_SUBDOMAIN:-docs}.${BASE_DOMAIN}")
got=$(docs_url)
if [ "$got" = "$old_url" ]; then
    pass=$((pass + 1)); echo "PASS c2 baseline-unchanged: new=$got old=$old_url"
else
    fail=$((fail + 1)); echo "FAIL c2 baseline-unchanged new=$got old=$old_url"
fi

# --- (d) explicit subdomain ---
DOCS_SUBDOMAIN=docs
export DOCS_SUBDOMAIN
gen
check d 'https://docs.example.com'

# --- (e) subdomain equals BASE_DOMAIN (the pre-1900BR2b unreachable shape) ---
DOCS_SUBDOMAIN=example.com
gen
check e 'https://example.com:3002'

# --- (f) mutation: the OLD unconditional line under env (a) must NOT match (a) ---
old_a=$(PROTOCOL=http BASE_DOMAIN=203.0.113.150 DOCS_SUBDOMAIN= sh -c \
    'printf "%s" "${PROTOCOL}://${DOCS_SUBDOMAIN:-docs}.${BASE_DOMAIN}"')
if [ "$old_a" != 'http://203.0.113.150:3002' ]; then
    pass=$((pass + 1)); echo "PASS f mutation-as-expected: old gives $old_a (dead host), new gives http://203.0.113.150:3002"
else
    fail=$((fail + 1)); echo "FAIL f mutation not detected"
fi

# --- (g) the generated config.js is still valid JS for the other keys (CE env) ---
PROTOCOL=http
BASE_DOMAIN=203.0.113.150
DOCS_SUBDOMAIN=
export PROTOCOL BASE_DOMAIN DOCS_SUBDOMAIN
gen
keys=$(grep -c '^  [A-Z][A-Z_]*: "' "$CFG")
objs=$(grep -c '^window.APP_CONFIG = {' "$CFG")
closers=$(grep -c '^};' "$CFG")
badq=$(grep -c '^  [A-Z][A-Z_]*: "[^"]*"[,]*$' "$CFG")
if [ "$keys" -eq 10 ] && [ "$objs" -eq 1 ] && [ "$closers" -eq 1 ] && [ "$badq" -eq 10 ]; then
    pass=$((pass + 1)); echo "PASS g config.js shape: keys=$keys objects=$objs closers=$closers quoted=$badq"
else
    fail=$((fail + 1)); echo "FAIL g config.js shape keys=$keys objects=$objs closers=$closers quoted=$badq"
fi

# --- (h) no-subdomain config.js byte-identical to the pre-fix expression's output ---
# diff the DOCS_URL line of the paid config against the old expression's value.
if grep -q "DOCS_URL: \"https://docs.example.com\"" "$CFG_C"; then
    pass=$((pass + 1)); echo "PASS h no-subdomain config.js carries the unchanged docs vhost"
else
    fail=$((fail + 1)); echo "FAIL h no-subdomain config.js docs line changed"
fi

rm -rf "$TMPD"
echo "docs-url-check: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
exit 0
