#!/bin/sh
# Caddy entrypoint — tenant STACK ingress (caddy-stack-ingress).
# Renders Caddyfile.template from env, mirroring scripts/nginx-entrypoint.sh
# (a two-placeholder substitution; awk not envsubst — stock caddy:2-alpine ships
# no gettext, see the render step below). The swappable issuer (design D3) is
# resolved HERE, at container start, into two computed blocks the template
# substitutes: CADDY_GLOBAL_TLS_BLOCK (global options) and CADDY_STACK_SITE
# (the stack ingress site). Unset/invalid issuer ⇒ both empty ⇒ edge INERT.
#
# ADDITIVE + PROFILE-GATED: this whole service only runs under the
# `stack-ingress` compose profile AND a set STACK_TLS_ISSUER. With neither,
# platform traffic stays entirely on nginx — this file never runs.

set -e

echo "🔧 Rendering Caddyfile from template (STACK_TLS_ISSUER=${STACK_TLS_ISSUER:-<unset>})..."

# --------------------------------------------------------------------------
# Hostname ownership split (design D6). Caddy answers ONLY the stack wildcards
# + tenant custom domains; platform names stay on nginx. Defaults derive from
# BASE_DOMAIN so a normal install needs no extra config; override per-install.
# --------------------------------------------------------------------------
: "${STACK_INGRESS_APPS_WILDCARD:=*.apps.${BASE_DOMAIN}}"
: "${STACK_INGRESS_SERVICES_WILDCARD:=*.services.${BASE_DOMAIN}}"
export STACK_INGRESS_APPS_WILDCARD STACK_INGRESS_SERVICES_WILDCARD

# The site host matcher = both wildcards. Tenant custom domains (api.acme.com)
# are outside our zone and are matched by on_demand_tls (no static list), so
# they need no entry here — the ask endpoint (Group 2) authorizes them.
STACK_HOSTS="https://${STACK_INGRESS_APPS_WILDCARD}, https://${STACK_INGRESS_SERVICES_WILDCARD}"

# HSTS at the stack edge. The stack site renders ONLY when an issuer is
# active (inert edge ⇒ empty site ⇒ nothing emitted), and it binds Caddy's own
# HTTPS port — so the header lands on TLS-terminating responses only and is
# never seen on a plain-HTTP path.
STACK_HSTS_HEADER=$(printf '\theader Strict-Transport-Security "max-age=31536000; includeSubDomains"\n')

# --------------------------------------------------------------------------
# On-demand TLS ask endpoint (Group 2). The internal API seam is implemented at
# GET /stacks/ingress/tls-authorize (mounted under the persistent_stacks
# router). Caddy appends ?domain=<host>; the endpoint authorises only a declared
# good-standing stack fqdn and fails closed on anything else. Caddy's ask cannot
# add a header, so an optional shared token rides the query string — Caddy keeps
# the configured query and merely appends &domain=.
# --------------------------------------------------------------------------
# The api container listens on :80 INSIDE the compose network (EXPOSE 80; 8000 is
# only the host-published mapping) — reach it by service name on 80, not 8000.
: "${STACK_TLS_ASK_URL:=http://api:80/v1/stacks/ingress/tls-authorize}"
# Derive the API ingress BASE (…/ingress/) from the URL BEFORE the
# token is appended, then append the token to EACH seam URL on its own. The old
# code cut the token-carrying ask URL at "tls-authorize*", which dropped the
# query and sent the hosts/snippet/tls-min-version reads with NO token (403).
: "${STACK_API_BASE:=${STACK_TLS_ASK_URL%tls-authorize*}}"
: "${MOD_INGRESS_ASK_TOKEN:=}"

# _tok_url URL -> URL with ?token=/&token= appended (unchanged if no token is
# set or the URL already carries one). _redact URL -> URL with token=*** — use
# it in EVERY line that prints a URL, so the token never reaches the logs.
_tok_url() {
    if [ -z "$MOD_INGRESS_ASK_TOKEN" ]; then printf '%s' "$1"; return 0; fi
    case "$1" in
        *token=*) printf '%s' "$1" ;;
        *\?*)     printf '%s&token=%s' "$1" "$MOD_INGRESS_ASK_TOKEN" ;;
        *)        printf '%s?token=%s' "$1" "$MOD_INGRESS_ASK_TOKEN" ;;
    esac
}
_redact() { printf '%s' "$1" | sed 's/token=[^&]*/token=***/g'; }

STACK_TLS_ASK_URL="$(_tok_url "$STACK_TLS_ASK_URL")"
export STACK_TLS_ASK_URL

# --------------------------------------------------------------------------
# TLS minimum version at the REAL public edge.
# The ACTIVE compliance profile reaches the edge through the platform setting
# tls.min_version (default "1.2"; locked to "1.3" by the cmmc + fedramp_high_il5
# profiles in services/compliance_profiles.py). We read it through the existing
# settings/profile API — GET /stacks/ingress/tls-min-version (guarded by the
# SAME optional ingress token as the `ask` endpoint above) — rather than a new
# env var, so a profile change is picked up on the next deploy with no edge
# code change. The URL is derived from STACK_TLS_ASK_URL (the same proven,
# token-carrying base), swapping the final path segment. Unreachable at render
# time (API down / no token) => default "1.2" (the baseline, never weaker).
# --------------------------------------------------------------------------
: "${MOD_TLS_MIN_VERSION_URL:=${STACK_API_BASE}tls-min-version}"
MOD_TLS_MIN_VERSION_URL="$(_tok_url "$MOD_TLS_MIN_VERSION_URL")"
TLS_MIN_VERSION="1.2"
if command -v wget >/dev/null 2>&1; then
    _mv_json="$(wget -qO- --tries=1 --timeout=3 "${MOD_TLS_MIN_VERSION_URL}" 2>/dev/null || true)"
    case "${_mv_json:-}" in
        *'"min_version"'*"1.3"*) TLS_MIN_VERSION="1.3" ;;
    esac
    unset _mv_json
fi
export TLS_MIN_VERSION
# The Caddy protocols line for the stack site: gov (1.3) => TLS 1.3 ONLY.
TLS_PROTOCOLS="tls1.2 tls1.3"
[ "$TLS_MIN_VERSION" = "1.3" ] && TLS_PROTOCOLS="tls1.3"
export TLS_PROTOCOLS

echo "   TLS min version (Q111=A): ${TLS_MIN_VERSION} -> protocols ${TLS_PROTOCOLS}"

# --------------------------------------------------------------------------
# Browser login in front of stack ingress (EDGE side).
# The API owns the per-host forward-auth Caddy body
# (services/stack_forward_auth.py renders auth_flow_directives + forward_auth
# from the stack's stored compose `x-mod.ingress.auth` block); this edge learns
# WHICH hosts need it from GET /stacks/ingress/hosts and fetches each host's
# body from GET /stacks/ingress/auth-snippet?host=<h> (both seams carry the
# SAME optional ingress token as the `ask` seam above, derived the same way —
# swap the final path segment of STACK_TLS_ASK_URL).
#
# Each require_login host gets an EXPLICIT site block rendered BEFORE the
# wildcard site: tls{} + HSTS + the fetched snippet (handle_path /_mod/auth*
# first, then forward_auth) + the shared bridge body. Caddy picks the more
# specific host, so the wildcard site's plain proxy never answers those hosts.
#
# FAIL CLOSED: a require_login host whose snippet fetch fails or returns an
# empty snippet renders `respond 503` for that host — it is NEVER proxied
# unauthenticated. Hosts the discovery read lists as require_login false need
# no block at all (the wildcard site already proxies them, no login). If the
# discovery read itself fails (API down / 403 / malformed body) the entrypoint
# retries ~60 s then EXITS 1 (fail closed): it never falls back to
# the wildcard-only render, which would serve login hosts with no login. A
# SUCCESSFUL read with zero require_login hosts still renders wildcard-only.
# --------------------------------------------------------------------------
: "${MOD_INGRESS_HOSTS_URL:=${STACK_API_BASE}hosts}"
: "${MOD_INGRESS_SNIPPET_URL:=${STACK_API_BASE}auth-snippet}"
MOD_INGRESS_HOSTS_URL="$(_tok_url "$MOD_INGRESS_HOSTS_URL")"
MOD_INGRESS_SNIPPET_URL="$(_tok_url "$MOD_INGRESS_SNIPPET_URL")"
# Query separator for appending ?host=: the snippet URL may already carry ?token=.
case "$MOD_INGRESS_SNIPPET_URL" in *\?*) _SNIP_SEP="&" ;; *) _SNIP_SEP="?" ;; esac

# JSON-string unescape (the snippet rides the wire as one JSON string value):
# \" -> ", then \n / \t / \\ -> real characters via %b.
_json_unescape() {
    printf '%b\n' "$(printf '%s' "$1" | sed 's/\\\\"/"/g')"
}

# Pull one flat JSON object's `"snippet"` value out of the auth-snippet
# response (greedy up to the quote that precedes the `require_login` key).
# Pull the `"snippet"` value out of the auth-snippet response. The endpoint's
# key order is fixed by the API (host, snippet, require_login) and the value's
# own quotes ride escaped as \", so anchor on the `,` + `"require_login"` that
# closes it — greedy `.*` then stops at the snippet's real closing quote.
_json_field() {  # $1 = json, $2 = key
    printf '%s' "$1" | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\(.*\)"[[:space:]]*,[[:space:]]*"require_login".*/\1/p'
}

STACK_LOGIN_HOSTS=""
# FAIL CLOSED: the hosts read is retried with backoff for up to
# ~60 s (HOSTS_RETRY_BUDGET_S, 1+2+4+8+8... s sleeps). A read that never yields a
# JSON array (wget error / non-2xx incl. 403 / body not an array) => exit 1, so
# the container restarts under its restart policy and NEVER serves a
# login-protected stack through the unauthenticated wildcard site. Only a
# SUCCESSFUL read with zero require_login hosts renders wildcard-only.
#
# Factored so the reload watcher reuses the SAME read + hostname
# validation. $1 = start (the behaviour above, byte-identical) | poll (ONE read
# attempt, quiet, never exits: returns 1 on a failed/malformed read so the
# caller keeps the current config).
: "${HOSTS_RETRY_BUDGET_S:=60}"
read_login_hosts() {  # $1 = start|poll
    _mode="${1:-start}"
    STACK_LOGIN_HOSTS=""
    _hosts_json=""; _hosts_ok=""; _waited=0; _sleep=1
    while :; do
        if _hosts_json="$(wget -qO- --tries=1 --timeout=3 "${MOD_INGRESS_HOSTS_URL}" 2>/dev/null)"; then
            _trim="$(printf '%s' "${_hosts_json}" | tr -d ' \t\r\n')"
            case "${_trim}" in
                "[]"|'[{'*'}]') _hosts_ok=1; break ;;
            esac
        fi
        if [ "${_mode}" = "poll" ]; then return 1; fi
        if [ "${_waited}" -ge "${HOSTS_RETRY_BUDGET_S}" ]; then break; fi
        echo "   GX.S1c ingress-hosts read failed ($(_redact "${MOD_INGRESS_HOSTS_URL}")) — retrying in ${_sleep}s (${_waited}/${HOSTS_RETRY_BUDGET_S}s)"
        sleep "${_sleep}"; _waited=$((_waited + _sleep))
        [ "${_sleep}" -lt 8 ] && _sleep=$((_sleep * 2))
    done
    if [ -z "${_hosts_ok}" ]; then
        echo "❌ GX.S1c FAIL-CLOSED: the ingress host-discovery read never returned a valid host list after ${_waited}s (API down, bad/missing MOD_INGRESS_ASK_TOKEN => 403, or a malformed body). Refusing to start: rendering without it would serve login-protected stacks unauthenticated. Exiting so the restart policy retries." >&2
        exit 1
    fi
    case "${_hosts_json:-}" in
        *'"host"'*)
            # Objects are flat, so `}` ends one row. Keep only rows whose
            # require_login is true; validate the hostname before it is pasted
            # into the Caddyfile (letters/digits/-/. only, and a real name).
            for _row in $(printf '%s' "${_hosts_json}" | tr '}' '\n' | grep '"require_login"[[:space:]]*:[[:space:]]*true' | sed 's/.*"host"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/'); do
                case "${_row}" in
                    *[!A-Za-z0-9.-]*) echo "   ⚠️  ingress host '${_row}' rejected (not a hostname) — skipped"; continue ;;
                esac
                case "${_row}" in
                    *.*) : ;;
                    *) echo "   ⚠️  ingress host '${_row}' rejected (no TLD) — skipped"; continue ;;
                esac
                STACK_LOGIN_HOSTS="${STACK_LOGIN_HOSTS} ${_row}"
            done
            ;;
        *)  echo "   GX.S1c ingress-hosts read OK: no login-protected stack hosts." ;;
    esac
    unset _hosts_json _hosts_ok _trim _waited _sleep _row
    export STACK_LOGIN_HOSTS
    if [ "${_mode}" = "start" ] && [ -n "${STACK_LOGIN_HOSTS# }" ]; then echo "   GX.S1c login-protected stack hosts:${STACK_LOGIN_HOSTS}"; fi
    return 0
}
read_login_hosts start

# --------------------------------------------------------------------------
# Public → mesh bridge (Group 4). Caddy derives the upstream from the inbound
# hostname and reverse_proxies to the service's MagicDNS NAME, re-resolved every
# request — NEVER a pinned address — so a stack that migrates workers needs ZERO
# edge change (design D5, the mesh D6 payoff for public traffic).
#
# TAILNET JOIN (4.1): Caddy reaches the overlay the SAME way nginx does — over
# the manager's HOST-MODE tailscaled route that mod-connect-service-mesh Group 1
# landed (MOD_TAILNET_MANAGER_MODE=host). Task 4.1 says "a ts-caddy sidecar OR the
# manager's host-mode tailscaled route (whichever mesh Group 1 lands)"; Group 1
# landed host-mode, and nginx already resolves MagicDNS from a bridge container
# via ${MOD_MAGICDNS_RESOLVER} (203.0.113.100) on that route — so Caddy piggybacks
# on the proven mechanism rather than a second (userspace, less-proven) join. A
# per-edge ts-caddy sidecar recipe is documented in docker-compose.yml for a
# deployment that instead wants Caddy to hold its own overlay identity.
#
# NAME DERIVATION: the public FQDN is t<tid>-<stackid>.apps.<domain> (stack-level,
# keyed on IDs). The per-service MagicDNS name is t<tid>-<stackid>-<service>. Caddy
# captures the t<tid>-<stackid> label from the host and dials it +
# STACK_INGRESS_UPSTREAM_SUFFIX on STACK_INGRESS_UPSTREAM_PORT. Suffix defaults to
# empty — see the compose note + integration issue on single- vs multi-service and
# custom-domain resolution.
# --------------------------------------------------------------------------
: "${STACK_INGRESS_UPSTREAM_PORT:=80}"
: "${STACK_INGRESS_UPSTREAM_SUFFIX:=}"
# Same resolver nginx uses for mesh stack/LLM locations (mod-connect-service-mesh
# §4.3). 203.0.113.100 is Tailscale MagicDNS, reachable from this bridge container
# only when the manager has a host-mode overlay route. With the mesh off (or no
# route) the name simply does not resolve → the bounded 502 below fires (never a
# hang). Override per-install if the resolver lives elsewhere.
: "${MOD_MAGICDNS_RESOLVER:=203.0.113.100}"
# Re-resolve interval — how fast a migrated stack's new address is picked up.
: "${STACK_INGRESS_UPSTREAM_REFRESH:=10s}"
# Bounded failure (4.4): fail fast, never hang, when the fabric/service is down.
: "${STACK_INGRESS_DIAL_TIMEOUT:=5s}"
: "${STACK_INGRESS_TRY_DURATION:=15s}"

# --------------------------------------------------------------------------
# vault-acme knobs (design D3). ACME directory shape is Vault PKI's own ACME
# endpoint: <VAULT_ADDR>/v1/<pki-mount>/acme/directory. PKI mount is a ${VAR}
# with a sane default. Airgapped: internal DNS → on-prem box, CA already in the
# trust store, ZERO external calls.
# --------------------------------------------------------------------------
: "${STACK_TLS_VAULT_ADDR:=${VAULT_ADDR:-http://openbao:8200}}"
: "${STACK_TLS_VAULT_PKI_MOUNT:=pki}"
: "${STACK_TLS_VAULT_ACME_DIRECTORY:=${STACK_TLS_VAULT_ADDR}/v1/${STACK_TLS_VAULT_PKI_MOUNT}/acme/directory}"
# Optional ACME account email (letsencrypt/vault-acme). Blank ⇒ omitted.
: "${STACK_TLS_ACME_EMAIL:=}"

# --------------------------------------------------------------------------
# Render the two blocks from the issuer. Each branch must produce a VALID
# Caddyfile fragment. Unset/invalid ⇒ empty ⇒ inert (NO silent public-ACME
# default — that would mint certs unexpectedly, design D3/3.5).
# --------------------------------------------------------------------------
# Optional ACME account email line (indented for the global options block).
EMAIL_LINE=""
[ -n "$STACK_TLS_ACME_EMAIL" ] && EMAIL_LINE="	email ${STACK_TLS_ACME_EMAIL}
"

# --------------------------------------------------------------------------
# The public → mesh routing body (Group 4). SHARED by every active issuer — the
# issuer differs only in how TLS is obtained (on_demand vs loaded byo cert); the
# routing to the overlay is identical. ${...} are expanded HERE (render time);
# Caddy placeholders use {..} (no $) so they survive verbatim.
#   - @stack captures t<tid>-<stackid> from the wildcard host.
#   - reverse_proxy → dynamic `a` upstream: the MagicDNS name, re-resolved via the
#     mesh resolver every ${STACK_INGRESS_UPSTREAM_REFRESH}, so a migrated stack
#     needs NO edit (4.2/4.3). Overlay hop is plain HTTP inside WireGuard; public
#     TLS already terminated at Caddy (design D5).
#   - handle {} : a wildcard host we cannot derive a name for → bounded 502 (4.4).
#   - handle_errors : fabric/service down → fast, diagnosable 5xx + short
#     Retry-After, never a hang (4.4).
# --------------------------------------------------------------------------
STACK_BRIDGE_HEAD=$(cat <<EOF
	# Public → mesh bridge (Group 4). Derive the stack's overlay identity from the
	# hostname and reverse_proxy to its MagicDNS name, re-resolved — never pinned.
	# The FQDN's first label IS the per-service mesh node name (t<tid>-<stackid>-<svc>,
	# set by the API's _public_hostname), so capture the WHOLE label and dial it —
	# a bare t<tid>-<stackid> is not a registered node. UPSTREAM_SUFFIX stays empty.
	@stack header_regexp stackid Host ^([a-z0-9-]+)\\.(apps|services)\\.
EOF
)
STACK_BRIDGE_ROUTE=$(cat <<EOF
	handle @stack {
		reverse_proxy {
			dynamic a {
				name {re.stackid.1}${STACK_INGRESS_UPSTREAM_SUFFIX}
				port ${STACK_INGRESS_UPSTREAM_PORT}
				resolvers ${MOD_MAGICDNS_RESOLVER}
				refresh ${STACK_INGRESS_UPSTREAM_REFRESH}
				dial_timeout ${STACK_INGRESS_DIAL_TIMEOUT}
			}
			lb_try_duration ${STACK_INGRESS_TRY_DURATION}
		}
	}
	# A wildcard host with no derivable overlay name (e.g. a malformed label, or a
	# BYO custom domain that needs the resolve seam) → bounded, diagnosable (4.4).
	handle {
		respond "MOD stack ingress: no derivable overlay route for {host}" 502
	}
EOF
)
STACK_BRIDGE_ERRORS=$(cat <<EOF
	# Fabric/service down → fail fast with a distinguishable reason + short retry,
	# never a hang (4.4). dial_timeout above bounds the wait upstream-side.
	handle_errors {
		header Retry-After 5
		respond "MOD stack ingress: upstream unavailable ({err.status_code}) for {host} — fabric or service down, retry shortly" {err.status_code}
	}
EOF
)
# Wildcard-site body: identical text/order to the pre-n3f single heredoc.
STACK_BRIDGE_BODY="${STACK_BRIDGE_HEAD}
${STACK_BRIDGE_ROUTE}
${STACK_BRIDGE_ERRORS}"

# --------------------------------------------------------------------------
# One explicit site block per login-protected stack host, rendered
# BEFORE the wildcard site so Caddy's more-specific host wins and the wildcard
# site's plain proxy never answers it. $1 = the issuer's tls {} block (issuer-
# specific: on_demand vs byo), reused verbatim per host.
#
# FAIL CLOSED: snippet fetch fails / returns an empty snippet => `respond 503`
# for that host (never proxied unauthenticated). The 503 responder sits AFTER
# the bridge body's handlers, so it must be the whole body instead of them.
# --------------------------------------------------------------------------
# The FAIL-CLOSED body: what a login-protected host renders when its snippet
# fetch failed. A whole-site replacement (NOT appended to the bridge handlers —
# a request would hit the bridge's `handle` first), so the host is refused
# outright rather than proxied with no forward_auth.
STACK_LOGIN_FAIL_BODY=$(cat <<EOF
	# FAIL CLOSED: the API's auth-snippet seam was unreadable for this host at
	# render time, so its login gate could not be rendered. Refuse the host
	# outright rather than proxying it with no forward_auth.
	respond "MOD stack ingress: forward-auth snippet unavailable for {host}" 503
EOF
)

render_login_sites() {  # $1 = tls block
    STACK_LOGIN_SITES=""
    for _h in ${STACK_LOGIN_HOSTS}; do
        _json="$(wget -qO- --tries=1 --timeout=3 "${MOD_INGRESS_SNIPPET_URL}${_SNIP_SEP}host=${_h}" 2>/dev/null || true)"
        _snip=""
        case "${_json:-}" in
            *'"snippet"'*) _snip="$(_json_unescape "$(_json_field "${_json}" snippet)")" ;;
        esac
        unset _json
        # Indent the snippet into the login route{} (its first/last lines arrive
        # at column 0; keep the rendered site block's own closing brace unique).
        [ -n "${_snip:-}" ] && _snip="$(printf '%s\n' "${_snip}" | sed "s/^/$(printf '\t')/")"
        case "${_snip:-}" in
            "")
                echo "   ⚠️  GX.S1c FAIL-CLOSED: no forward-auth snippet for ${_h} — rendering a 503 responder (host never proxied unauthenticated)"
                STACK_LOGIN_SITES="${STACK_LOGIN_SITES}${_h} {
${1}
${STACK_HSTS_HEADER}
${STACK_LOGIN_FAIL_BODY}
}
"
                ;;
            *)
                STACK_LOGIN_SITES="${STACK_LOGIN_SITES}${_h} {
${1}
${STACK_HSTS_HEADER}
${STACK_BRIDGE_HEAD}
	# ONE literal-order route{} — Caddy sorts site-level directives
	# (handle runs BEFORE route), so the bridge handles must live INSIDE the same
	# route as the auth snippet, AFTER it, or forward_auth is never reached.
	route {
		# Drop any client-supplied identity header before anything else (also on
		# public_paths, which skip forward_auth).
		request_header -X-Mod-*
${_snip}
${STACK_BRIDGE_ROUTE}
	}
${STACK_BRIDGE_ERRORS}
}
"
                echo "   GX.S1c: login site rendered for ${_h} (forward-auth + /_mod/auth*)"
                ;;
        esac
        unset _snip
    done
    export STACK_LOGIN_SITES
}

case "${STACK_TLS_ISSUER:-}" in
    letsencrypt)
        # Public ACME directory (certmagic default CA = Let's Encrypt prod).
        # Needs outbound :443 to the CA + inbound reachability for the challenge.
        echo "   Issuer: letsencrypt (public ACME)"
        CADDY_GLOBAL_TLS_BLOCK=$(cat <<EOF
${EMAIL_LINE}	on_demand_tls {
		ask ${STACK_TLS_ASK_URL}
	}
EOF
)
        CADDY_STACK_SITE=$(cat <<EOF
${STACK_HOSTS} {
	# Per-host cert issued live at first request, gated by the ask endpoint.
	tls {
		on_demand
		protocols ${TLS_PROTOCOLS}
		curves x25519mlkem768 x25519 secp384r1 secp256r1
	}
${STACK_HSTS_HEADER}
${STACK_BRIDGE_BODY}
}
EOF
)
        ;;
    vault-acme)
        # ACME directory points at Vault PKI's ACME endpoint. Airgapped posture:
        # internal DNS → on-prem box, CA already trusted, ZERO external calls.
        echo "   Issuer: vault-acme (Vault PKI ACME: ${STACK_TLS_VAULT_ACME_DIRECTORY})"
        CADDY_GLOBAL_TLS_BLOCK=$(cat <<EOF
${EMAIL_LINE}	acme_ca ${STACK_TLS_VAULT_ACME_DIRECTORY}
	on_demand_tls {
		ask ${STACK_TLS_ASK_URL}
	}
EOF
)
        CADDY_STACK_SITE=$(cat <<EOF
${STACK_HOSTS} {
	# Per-host cert issued live from the Vault CA, gated by the ask endpoint.
	tls {
		on_demand
		protocols ${TLS_PROTOCOLS}
		curves x25519mlkem768 x25519 secp384r1 secp256r1
	}
${STACK_HSTS_HEADER}
${STACK_BRIDGE_BODY}
}
EOF
)
        ;;
    byo)
        # Bring-your-own: NO automatic issuance. Certs are loaded later via the
        # admin API by the watch-and-reload step (Group 5, out of scope here).
        # disable_certs turns off cert management but keeps the HTTPS server.
        echo "   Issuer: byo (automatic issuance DISABLED; certs loaded via admin API in Group 5)"
        CADDY_GLOBAL_TLS_BLOCK=$(printf '\tauto_https disable_certs\n')
        CADDY_STACK_SITE=$(cat <<EOF
${STACK_HOSTS} {
	# No on_demand / no ACME — issuance is off (auto_https disable_certs above).
	# The cert is pre-loaded from the service's Vault path via the admin API
	# (Group 5). The routing bridge below is issuer-independent; until a cert is
	# loaded the TLS handshake for a hostname simply has no cert to present.
	# The tls {} block carries the protocol floor +
	# curve policy (the byo cert is loaded into the tls app via the admin API,
	# so there is no issuer line here — protocols/curves are still honoured).
	tls {
		protocols ${TLS_PROTOCOLS}
		curves x25519mlkem768 x25519 secp384r1 secp256r1
	}
${STACK_HSTS_HEADER}
${STACK_BRIDGE_BODY}
}
EOF
)
        ;;
    "")
        # No issuer configured → edge INERT. Do NOT default to public ACME.
        echo "⚠️  STACK_TLS_ISSUER is unset — Caddy stack ingress stays INERT (no 443 listener, no issuance). Platform traffic is unaffected (nginx)."
        CADDY_GLOBAL_TLS_BLOCK=""
        CADDY_STACK_SITE=""
        ;;
    *)
        # Unknown value → fail closed to inert, and log loudly. Never guess.
        echo "⚠️  STACK_TLS_ISSUER='${STACK_TLS_ISSUER}' is not one of {letsencrypt,vault-acme,byo} — leaving the edge INERT (no issuance). Fix the value to enable stack ingress."
        CADDY_GLOBAL_TLS_BLOCK=""
        CADDY_STACK_SITE=""
        ;;
esac
export CADDY_GLOBAL_TLS_BLOCK CADDY_STACK_SITE

# --------------------------------------------------------------------------
# Prepend the per-host login sites (rendered above) to the stack
# site. The issuer's tls {} block is lifted out of the rendered wildcard site
# (it is the FIRST `tls {` … `}` pair) so a login site gets the SAME cert
# policy (on_demand for ACME issuers, plain protocols for byo) + HSTS.
# Inert edge (no issuer) ⇒ no site ⇒ no login sites.
# --------------------------------------------------------------------------
CADDY_STACK_SITE_BASE="${CADDY_STACK_SITE}"
build_login_site() {
    CADDY_STACK_SITE="${CADDY_STACK_SITE_BASE}"
    if [ -n "${CADDY_STACK_SITE:-}" ] && [ -n "${STACK_LOGIN_HOSTS# }" ]; then
        LOGIN_TLS_BLOCK=$(printf '%s\n' "${CADDY_STACK_SITE}" \
            | awk '/^\ttls \{/{f=1} f{print} f&&/^\t\}/{exit}')
        render_login_sites "${LOGIN_TLS_BLOCK}"
        CADDY_STACK_SITE="${STACK_LOGIN_SITES}${CADDY_STACK_SITE}"
    fi
    export CADDY_STACK_SITE
}
build_login_site

# --------------------------------------------------------------------------
# Substitute ONLY our two computed blocks. Everything else in the template
# (Caddy's own {...} placeholders, the admin/health blocks) is left verbatim.
#
# Done with busybox `awk` (via ENVIRON), NOT envsubst: the stock caddy:2-alpine
# image ships no gettext/envsubst, so an envsubst render would fail at container
# start. awk is built in. This also keeps the edge airgap-shippable on stock
# Caddy (design D4) — no extra package. Each placeholder sits alone on its line
# in the template; the whole line is replaced by the block's (possibly empty,
# possibly multi-line) value.
# --------------------------------------------------------------------------
_awk_render() {
awk '
  $0 == "${CADDY_GLOBAL_TLS_BLOCK}" { print ENVIRON["CADDY_GLOBAL_TLS_BLOCK"]; next }
  $0 == "${CADDY_STACK_SITE}"       { print ENVIRON["CADDY_STACK_SITE"];       next }
  { print }
' /etc/caddy/Caddyfile.template
}
_awk_render > /etc/caddy/Caddyfile

echo "✅ Caddyfile rendered"
echo "   Stack hostnames (Caddy-owned): ${STACK_INGRESS_APPS_WILDCARD}, ${STACK_INGRESS_SERVICES_WILDCARD}"

# --------------------------------------------------------------------------
# Bounded re-render + `caddy reload` loop. A stack toggling
# require_login (PUT /persistent-stacks/{id}/ingress-auth) changes the hosts
# list; without this the edge only picked it up at container restart.
# FAIL CLOSED: a failed/malformed hosts read keeps the CURRENT config (never a
# wildcard-only fallback, which would ungate every login host) and never exits.
# --------------------------------------------------------------------------
_ingress_reload_once() {
    if ! read_login_hosts poll >/dev/null; then
        echo "   GX.n3h: ingress-hosts read failed ($(_redact "${MOD_INGRESS_HOSTS_URL}")) — keeping the current config"
        return 0
    fi
    build_login_site >/dev/null
    if ! _awk_render > /etc/caddy/Caddyfile.next; then
        echo "   GX.n3h: render failed — keeping the current config"; return 0
    fi
    if cmp -s /etc/caddy/Caddyfile.next /etc/caddy/Caddyfile; then return 0; fi
    if ! caddy validate --config /etc/caddy/Caddyfile.next --adapter caddyfile >/dev/null 2>&1; then
        echo "   GX.n3h: new render failed validation — keeping the current config"; return 0
    fi
    if ! caddy reload --config /etc/caddy/Caddyfile.next --adapter caddyfile >/dev/null 2>&1; then
        echo "   GX.n3h: caddy reload failed — keeping the current config"; return 0
    fi
    mv /etc/caddy/Caddyfile.next /etc/caddy/Caddyfile
    set -- ${STACK_LOGIN_HOSTS}
    echo "   GX.n3h: reloaded (login hosts $#)"
    return 0
}

_start_ingress_reload_watcher() {
    _iv="${MOD_INGRESS_RELOAD_INTERVAL_S:-30}"
    case "${_iv}" in ''|*[!0-9]*) _iv=30 ;; esac
    if [ "${_iv}" -eq 0 ]; then
        echo "   GX.n3h: ingress reload watcher disabled (MOD_INGRESS_RELOAD_INTERVAL_S=0)"
        return 0
    fi
    if [ -z "${CADDY_STACK_SITE_BASE:-}" ]; then return 0; fi   # inert edge: nothing to gate
    ( while :; do sleep "${_iv}"; _ingress_reload_once || true; done ) &
    echo "   GX.n3h: ingress reload watcher every ${_iv}s"
}

# Test seam: define everything above, then stop before validate/exec.
if [ -n "${MOD_INGRESS_ENTRYPOINT_LIB:-}" ]; then return 0; fi

# Validate before serving so a bad render fails fast with a clear message
# rather than a half-started edge. (Watcher starts only once validate passed;
# caddy stays PID 1 via exec below.)
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && _start_ingress_reload_watcher || exit 1

echo "🚀 Starting Caddy..."
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
