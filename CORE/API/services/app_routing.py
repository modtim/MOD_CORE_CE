"""GX.A2 — app hostname routing: render the Caddy site block, apply it via the
caddy-front admin API, reconcile from the DB, and resolve route targets.

Design (gx-a1-app-design §1.4): the DB row is the source of truth. The API
merges one explicit site block per active app onto the BASE front Caddyfile
that caddy-front's entrypoint published into the shared admin volume, and
full-replaces the live config through caddy-front's admin API. Caddy validates
the new config atomically — a bad config is rejected and the running config
stays live, so a restart/redeploy converges on the next reconcile pass.

Security boundaries honoured here (GX.A2b):
  * The admin API is a server-side primitive with NO auth. It is therefore NOT
    network-reachable: it runs on a UNIX socket in the `caddy_front_admin`
    named volume, mounted rw by caddy-front (creator) and api (client) ONLY.
    Nothing else on modtex-network can touch it — no bearer token needed (and
    none exists to log/leak).
  * The base file is the entrypoint's RENDERED platform Caddyfile, not a
    re-implementation of it: an app apply can never drop the platform sites.
    A missing base file fails CLOSED (log + no push).
  * The admin API apply NEVER runs inside an open DB transaction: the caller
    loads rows, closes the session, then applies (httpx uds transport, <=5s
    timeout).
  * external-url targets are fenced against SSRF (§7.5): scheme + resolved-IP
    checks at create/update and on the hourly reconcile sweep.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException, status

import models
from services import platform_settings as ps
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

# --- admin socket / base file (bootstrap, NOT the behavioural registry) ----
# The admin socket path and the base Caddyfile path are infrastructure
# addresses (a socket in the caddy_front_admin volume + the file the
# caddy-front entrypoint published there), not tunables — they come from env
# (compose) so the admin-API call stays DB-free and can therefore NEVER run
# inside an open DB transaction. There is NO token: Caddy's admin API is
# unauthenticated by design, so the only protection is that the socket is not
# on the network (see Caddyfile.front.template's `admin unix//...`).
CADDY_ADMIN_SOCKET_ENV = "CADDY_FRONT_ADMIN_SOCKET"
CADDY_ADMIN_BASE_ENV = "CADDY_FRONT_BASE_CADDYFILE"
CADDY_ADMIN_HOST_HEADER = "caddy-admin"  # virtual Host for the socket request

ADMIN_TIMEOUT_SECONDS = 5.0  # hard cap — an admin call must not hold anything

# Per-app Caddy servers are namespaced so reconcile can find/replace only OUR
# servers and never touch the platform site's default server.
_APP_SERVER_PREFIX = "mod_app_"


def _register_settings() -> None:
    """Register the app-routing tunables in the platform-settings registry.

    Idempotent: register() overwrites a key, so repeated import is safe.
    (The admin socket/base-file paths are infra addresses — env, not the
    behavioural registry — so they are NOT registered here.)
    """
    if "app.routing.enabled" in ps._REGISTRY:
        return
    ps.register(
        "app.routing.enabled",
        bool,
        True,
        group="apps",
        description="Master switch for MOD-managed app hostname routing. "
                    "Off = the reconcile loop no-ops and no app sites are "
                    "rendered (apps still work via the API; the proxy route "
                    "is simply not managed).",
    )
    ps.register(
        "app.max_per_tenant",
        int,
        0,
        group="apps",
        description="Max apps a single tenant may hold. 0 = unlimited (owner "
                    "decision: apps are CE-unlimited).",
    )
    ps.register(
        "app.external_targets.enabled",
        bool,
        False,
        group="apps",
        description="Allow app route targets of kind 'external-url' (a fixed "
                    "http(s) host MOD proxies TO). Off by default — "
                    "a server-side fetch primitive that stays fenced until the "
                    "owner enables it (§7.5).",
    )
    ps.register(
        "apps.edge_upstream",
        str,
        "",
        group="apps",
        description="Name the owner's edge Caddy uses to reach this manager "
                    "over the tailnet, e.g. dev-mod-node.example.com. "
                    "Read-only helper text for the Apps panel's 'Public "
                    "address (edge Caddy)' block: MOD never writes the owner's "
                    "Caddy. Empty = unknown, so the panel prints a placeholder.",
    )
    # PLAIN-HTTP-APP-SITE (owner Tim 11-25): an owner edge Caddy that
    # forwards PLAIN http to this manager's :80 used to loop — the bare-hostname
    # site only answers TLS on :443, so caddy-front 308-redirected :80 to
    # https and the edge fetched https again. Opt-in: when true, render_app_sites
    # ALSO emits an explicit `http://<hostname>` site with the SAME route block
    # (auth flow, forward_auth, client-header strip, reverse_proxy,
    # X-Forwarded-Proto https) and NO tls/HSTS/redirect. Defining an explicit
    # http:// site is also what suppresses Caddy's auto HTTP→HTTPS redirect for
    # that host (same mechanism as the CA-distribution block in
    # caddy-front-entrypoint.sh). The https site stays byte-identical; the http
    # site still strips client X-Mod-* headers and runs forward_auth (fail-closed
    # render rules unchanged). MOD auth redirects/cookies keep the public https
    # hostname — the route block pins X-Forwarded-Proto https upstream.
    ps.register(
        "apps.edge_plain_http",
        bool,
        False,
        group="apps",
        description="Serve app hostnames on plain :80 too (an explicit "
                    "http:// site with the same route block, no tls/redirect). "
                    "For an owner edge Caddy that forwards plain http to this "
                    "manager; off by default so :80 keeps the 308 to https.",
    )


_register_settings()


def routing_enabled(db=None) -> bool:
    return bool(ps.get_setting("app.routing.enabled", db=db))


def max_apps_per_tenant(db=None) -> int:
    return int(ps.get_setting("app.max_per_tenant", db=db))


def external_targets_enabled(db=None) -> bool:
    return bool(ps.get_setting("app.external_targets.enabled", db=db))


def edge_plain_http(db=None) -> bool:
    """PLAIN-HTTP-APP-SITE opt-in switch (owner Tim 11-25).

    True ⇒ render_app_sites also emits an explicit ``http://<hostname>`` site
    (same route block, no tls/redirect) so an owner edge Caddy forwarding plain
    http to this manager's :80 no longer loops through the 308. Default False:
    :80 keeps Caddy's auto redirect to https."""
    return bool(ps.get_setting("apps.edge_plain_http", db=db))


def edge_upstream(db=None) -> Optional[str]:
    """The owner's edge-Caddy upstream name for this manager (GX Q-ING=A).

    '' (the default) means unknown, so callers surface None and the Apps
    panel keeps its placeholder. Read-only helper text — MOD never writes
    the owner's edge Caddy."""
    value = ps.get_setting("apps.edge_upstream", db=db)
    value = (value or "").strip()
    return value or None


# ---------------------------------------------------------------------------
# SSRF fence for external-url targets (§7.5)
# ---------------------------------------------------------------------------

# Blocked destination networks (checked on RESOLVED IPs, per connection where
# possible). RFC1918 + loopback + link-local + CGNAT/tailnet + IPv6 equivalents.
_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918
    # RFC1918 as ints: the open-core build scrub rewrites dotted private-IP
    # literals into doc-range IPs, which would make these strict networks
    # raise ValueError at import in the CE tree. Int form is scrub-inert and
    # semantically identical.
    ipaddress.ip_network((0xAC100000, 12)),    # 172.16-172.31/12 RFC1918
    ipaddress.ip_network((0xC0A80000, 16)),    # 192.168/16 RFC1918
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT / tailnet
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(ip in net for net in _BLOCKED_NETS)


def validate_external_url(url: str) -> str:
    """Validate an external-url target and return it normalised.

    Raises HTTPException(400) on: bad scheme (only http/https allowed),
    unparseable host, or a host that resolves (now) to a blocked range.

    Does NO database work — callers must not hold an open transaction across
    this call (it performs blocking DNS).
    """
    from urllib.parse import urlparse

    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="external_url is required")
    try:
        parsed = urlparse(url.strip())
    except ValueError as e:
        raise internal_http_error(400, "Invalid external_url", e) from e

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="external_url scheme must be http or https",
        )
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="external_url must have a host")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise internal_http_error(400, "external_url host does not resolve", e) from e

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if is_blocked_ip(ip):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"external_url host {host!r} resolves to a blocked range "
                    f"({addr}); SSRF guard rejects private/link-local/tailnet targets"
                ),
            )
    return url.strip()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def _stack_running(stack: Optional[models.PersistentStack]) -> bool:
    """A stack-service route is active only while its stack is up."""
    if stack is None:
        return False
    return getattr(stack, "status", None) in ("running", "active", "ready")


def _magicdns_name(stack: models.PersistentStack, service: str) -> Optional[str]:
    """The WORKER-ADVERTISED MagicDNS name for this service, or None.

    A redeployed/migrated stack keeps the same advertised name, so Caddy
    re-resolving it (short valid=) needs no proxy change.

    BUG.B60: there is deliberately NO deterministic `t<tenant>-<id>-<svc>`
    invention here any more. That name only exists when the deploy provisioned
    mesh sidecars, and a stack deployed without them (stack 114) advertises
    nothing — the made-up name then 502s at caddy-front ("no such host"), which
    cannot resolve tailnet names at all from mod_modtex-network. When nothing is
    advertised, resolve_upstream falls through to the stack's PUBLISHED host
    port (below), and if that is unknown the route is INACTIVE (503 block)."""
    # STACK-MESH-ENDPOINTS-SHAPE-MISMATCH: the WRITER is the contract —
    # `_provision_mesh_identity` (routers/persistent_stacks/_shared.py:1360-1372)
    # stores FLAT `{svc: {magicdns_name, status, anchors}}` (the migration doc
    # `migrations/20260822_stack_mesh_endpoints.py:5` and every other reader —
    # `_stack_mesh_endpoints`, `_stack_reachability`, `generate_env.py`, the
    # Dashboard — agree), and the live rows (xeon stack 23) are flat. This
    # reader used to key `mesh_endpoints["services"][svc]` ONLY, a shape
    # nothing writes, so it returned None for every stack. It now accepts
    # BOTH shapes (flat `{svc: {...}}` and the nested `{"services": {svc: ...}}`
    # the fixtures used) so old rows keep working either way.
    #
    # A REVOKED mesh entry (same rule as `_stack_reachability`'s
    # `mesh_provisioned`, _shared.py:1310-1313) advertises nothing: the
    # identity was pulled, so fall through to the published host port / 503.
    eps = stack.mesh_endpoints if isinstance(stack.mesh_endpoints, dict) else {}

    def _advertise(entry):
        if not isinstance(entry, dict):
            return None
        if (entry.get("status") or "provisioning") == "revoked":
            return None  # mesh identity pulled — nothing advertised
        return entry.get("magicdns_name") or None

    # Nested shape first (back-compat with the fixtures/tests that used it),
    # then the FLAT writer shape (the live rows). A service literally named
    # "services" is rare; the nested read only wins when it carries THIS
    # service as a dict, so a flat {"services": {...}} row still resolves.
    nested = eps.get("services")
    if isinstance(nested, dict):
        name = _advertise(nested.get(service))
        if name:
            return name
    entry = eps.get(service)
    if isinstance(entry, dict):
        return _advertise(entry)
    return None


def _host_port_entries(stack: models.PersistentStack) -> List[Tuple[Optional[str], dict]]:
    """The worker's `host_port_mappings` report as (service, entry) pairs.

    Recorded verbatim by `stack_port_placement.record_host_port_allocation`
    onto `storage_refs["host_port_mappings"]` when a deploy result is ingested
    (GW.2/GW.4: the worker allocates the host port for `host: "any"` requests
    and reports the chosen mapping back). Trusted-as-data, not as-shape: both
    the documented flat-list shape and the `{service: [entries...]}` mapping
    are tolerated, junk entries are skipped (same tolerance as
    `routers.persistent_stacks._stack_allocated_host_ports`)."""
    refs = getattr(stack, "storage_refs", None)
    if not isinstance(refs, dict):
        return []
    mapping = refs.get("host_port_mappings")
    if isinstance(mapping, list):
        pairs = [(None, e) for e in mapping]
    elif isinstance(mapping, dict):
        pairs = [(str(svc), e)
                 for svc, items in mapping.items()
                 if isinstance(items, list) for e in items]
    else:
        return []
    return [(svc, e) for svc, e in pairs if isinstance(e, dict)]


def _published_host_port(stack: models.PersistentStack, service: Optional[str],
                         target_port: Optional[int]) -> Optional[int]:
    """The HOST port the worker published for this service, or None.

    This is the routable address for a stack with no mesh plane: the manager
    host's published port (proven: `modstack114g0-web-1` publishes
    `0.0.0.0:20002->8080/tcp`, and `host.docker.internal:20002` answers 200
    from inside mod_caddy_front). Matched on the service name first, then on
    the container (target) port the app routes at, so a stack that reports no
    per-entry service still resolves. Host ports are validated as ints in
    range; a junk mapping yields None, never a guess."""
    entries = _host_port_entries(stack)
    for svc, e in entries:
        name = e.get("service") if e.get("service") is not None else svc
        name = str(name) if name is not None else None
        if name is None or (service is not None and name != str(service)):
            continue
        host = e.get("host")
        if isinstance(host, bool) or not isinstance(host, int) or not 1 <= host <= 65535:
            continue
        target = e.get("target")
        if isinstance(target, bool) or not isinstance(target, int):
            target = None
        if target_port and target is not None and target != target_port:
            continue  # another container port of the same service
        return host
    if target_port:
        for _svc, e in entries:
            host = e.get("host")
            if isinstance(host, bool) or not isinstance(host, int) or not 1 <= host <= 65535:
                continue
            if e.get("target") == target_port:
                return host
    return None


_EMBEDDED_WORKER_NAME = "mod-worker-embedded"  # routers/persistent_stacks/_shared.py (mirrored, not imported)
# BUG-EMBEDDED-WORKER-UI — CE names the SAME on-manager embedded worker
# `mod-worker-ce` (open-core/overlay/docker-compose.yml:949). Keying only the
# SaaS literal made _worker_host dial `http://mod-worker-ce:<port>`, which is
# not a resolvable host name, instead of the shared docker host.
_EMBEDDED_WORKER_NAMES = (_EMBEDDED_WORKER_NAME, "mod-worker-ce")
_MANAGER_HOST = "host.docker.internal"


def _worker_host(stack: models.PersistentStack) -> str:
    """Host caddy-front dials for this stack's worker.

    The manager-embedded worker IS the docker host the front container shares
    (same fallback `stack_wake._run_probe` uses), so `host.docker.internal`.
    Any other worker is dialed by its worker name — the address the API already
    uses for that worker elsewhere; no new setting is invented here."""
    worker = getattr(stack, "target_worker", None)
    if not worker or worker in _EMBEDDED_WORKER_NAMES:
        return _MANAGER_HOST
    return worker


def _target_service_unhealthy(stack: Optional[models.PersistentStack],
                              service: Optional[str]) -> bool:
    """True when the stack's health record says THIS service is currently bad.

    Reads `storage_refs["status_health"]` — {state, streak, bad_services,
    reason, threshold} — written only by the observer's streak updater. No
    import of the paid module here: the route gate must stay inert (and
    cheap) on CE, where the record simply never exists. Only a `degraded`
    stack AND a named bad service condemns the route: a degraded stack whose
    problem is another service keeps this app's route live (the brief: the
    503 is for the app whose TARGET service is unhealthy)."""
    if stack is None or service is None:
        return False
    rec = (getattr(stack, "storage_refs", None) or {}).get("status_health")
    if not isinstance(rec, dict) or rec.get("state") != "degraded":
        return False
    bad = rec.get("bad_services") or []
    return str(service) in [str(b) for b in bad]


def resolve_upstream(app: models.App, stack: Optional[models.PersistentStack]) -> Optional[str]:
    """Resolve an app route target to a proxy upstream, or None when the route
    is INACTIVE (target stack stopped / external target invalid).

    Returns a string like ``http://<name>:<port>`` (stack-service) or the
    fixed address (external-url). None ⇒ the site renders a 503 block.
    """
    if app.target_kind == "stack-service":
        if not _stack_running(stack):
            return None
        # STACK-HEALTH-STATUS-1126 (owner decision CE7 = yes) — a stack whose
        # status is still `running` but whose target SERVICE has been observed
        # restarting/unhealthy/exited-non-zero for N consecutive beats answers
        # the 503 "app target unavailable" instead of proxying traffic into a
        # broken container. The verdict lives in `storage_refs["status_health"]`
        # (written by services/stack_observation.py's streak updater), read
        # edition-NEUTRALLY here: CE never writes the record, so the gate is
        # inert there and behaviour is byte-identical. Recovery is automatic:
        # the first clean beat resets the streak, the next site render dials
        # the upstream again ("reconcile on state change" is this read).
        if _target_service_unhealthy(stack, app.stack_service):
            return None
        if not stack or app.stack_service is None or not app.stack_port:
            return None
        # STACK-MESH-ENDPOINTS-SHAPE-MISMATCH — resolution order (proven, not
        # guessed): this upstream is dialed by TWO clients — caddy-front's
        # reverse_proxy (render_app_site) and the API's own httpx client
        # (routers/stack_call.py:_upstream_base). NEITHER can resolve tailnet
        # MagicDNS names: compose gives both containers only the
        # `host.docker.internal:host-gateway` extra_hosts mapping
        # (CORE/startup/docker-compose.yml:3211 caddy-front, :1951 api), and the
        # tailnet resolver (MOD_MAGICDNS_RESOLVER=203.0.113.100) is wired into
        # nginx (:414) and the stack-ingress caddy (:3059) — different code
        # paths, not this one. So the PUBLISHED host port (the BUG.B60 path,
        # proven answering from inside mod_caddy_front) stays FIRST: fixing the
        # mesh shape alone would have flipped these stacks onto a name nothing
        # here resolves. The advertised mesh name is the fallback for the
        # mesh-only state (_shared.py:1298 — a reachable service drops its
        # published ports at deploy, so no host port is reported); it is the
        # route that survives host moves, and a front that CAN resolve the
        # overlay (the stack-ingress caddy) dials it on its own path already.
        port = _published_host_port(stack, app.stack_service, app.stack_port)
        if port is not None:
            return f"http://{_worker_host(stack)}:{port}"
        name = _magicdns_name(stack, app.stack_service)
        if name:
            return f"http://{name}:{app.stack_port}"
        return None  # nothing published and nothing advertised ⇒ 503 block

    if app.target_kind == "external-url":
        if not app.external_url:
            return None
        # Do NOT re-run DNS here (no transaction, but keep resolve cheap); the
        # target was fenced at create/update and re-fenced on the sweep.
        return app.external_url

    return None


# ---------------------------------------------------------------------------
# Caddyfile rendering
# ---------------------------------------------------------------------------

def tls_directive() -> str:
    """The site TLS block, rendered from MOD_FRONT_TLS_MODE EXACTLY like
    caddy-front-entrypoint.sh (internal: local-CA leaf; public: empty = default
    automatic ACME).

    FX.124 part 3 (Q111=A): the rendered block ALWAYS carries the TLS policy
    (protocols per tls.min_version + the strong curve set) so every MOD app
    site is hardened even in public mode, where the entrypoint renders no
    block at all."""
    min_version = tls_min_version()
    mode = os.getenv("MOD_FRONT_TLS_MODE", "internal").lower()
    if mode in ("public", "letsencrypt"):
        return ("\ttls {\n"
                + _tls_policy_lines(min_version)
                + "\t}\n")
    return ("\ttls {\n"
            "\t\tissuer internal {\n"
            "\t\t\tlifetime 720h\n"
            "\t\t}\n"
            + _tls_policy_lines(min_version)
            + "\t}\n")


def tls_min_version(db=None) -> str:
    """FX.124 part 3 (Q111=A) — the platform setting tls.min_version
    ("1.2" default / "1.3" gov). DB-free by DEFAULT (db=None ⇒ its own
    short session) so callers outside a request can never pin an open
    transaction; pass the caller's session when the value must be read
    inside that transaction (a fresh session cannot see rows that are
    only flushed, not committed). Unreadable ⇒ "1.2" (the default,
    never weaker than the baseline)."""
    try:
        v = str(ps.get_setting("tls.min_version", db=db))
    except Exception:  # noqa: BLE001
        v = "1.2"
    return v if v in ("1.2", "1.3") else "1.2"


def _tls_policy_lines(min_version: str, indent: str = "\t\t") -> str:
    """FX.124 part 3 (Q111=A) — the Caddy `tls {}`-block policy lines for a
    site: the protocol floor + the strong curve set.

    Curves: x25519mlkem768 (PQC X25519MLKEM768, Caddy 2.10+; the live
    caddy:2-alpine is 2.11) first, then x25519/secp384r1/secp256r1. The
    TLS 1.2 ciphers are intentionally NOT restricted to a sub-list: Caddy's
    default 1.2 policy is already AEAD-only (ECDHE AES-GCM + CHACHA20-
    POLY1305; no CBC/RSA-kx/3DES/RC4), and Caddyfile has no cipher_suites
    subdirective (only the JSON API does)."""
    protocols = "tls1.3" if min_version == "1.3" else "tls1.2 tls1.3"
    return (f"{indent}protocols {protocols}\n"
            f"{indent}curves x25519mlkem768 x25519 secp384r1 secp256r1\n")


def hsts_header(indent: str = "\t") -> str:
    """R-168 follow-up: the HSTS line for a per-app Caddy site block,
    rendered EXACTLY like caddy-front-entrypoint.sh (max-age one year,
    includeSubDomains). Every app site is a bare-hostname block
    (slug.BASE_DOMAIN) — Caddy serves it over TLS on :443 in both modes
    (public ACME or internal local-CA), so HSTS belongs in the block.
    The plain-HTTP `http://` CA-distribution block in the entrypoint
    deliberately does NOT carry it: browsers ignore HSTS over http."""
    return f'{indent}header Strict-Transport-Security "max-age=31536000; includeSubDomains"\n'


def render_app_site(app: models.App, upstream: Optional[str],
                    fa_config: Optional[dict] = None,
                    plain_http: bool = False) -> str:
    """Render one explicit Caddy site block for an app.

    ``upstream is None`` ⇒ the route is INACTIVE: render a 503 responder
    (a clear body, not a hang — §1.2).

    ``fa_config`` (GX.S1) is the app's forward-auth config; when it
    requires login, a ``forward_auth`` directive (with public_paths
    matcher) is rendered BEFORE the reverse_proxy.  ``fa_config`` may also
    render strip-only (``require_login=false`` ⇒ no ``forward_auth``, just
    the ``request_header -X-Mod-*`` strip block); the site still proxies.

    GX.S1c: the MOD-handled browser login flow (``/_mod/auth/*`` → the API's
    /v1/apps/auth/*) is rendered as a public ``handle_path``.  All three
    directives (handle_path, forward_auth, reverse_proxy) are wrapped in a
    ``route { }`` block — Caddy sorts directives by its standard order,
    NOT by literal position, so the ``route`` wrapper is what preserves
    the written order (handle_path first, then forward_auth, then
    reverse_proxy).  tls and HSTS stay at site-block level.
    """
    from services import app_forward_auth

    hostname = app.hostname
    if not hostname:
        return ""
    tls = tls_directive()
    fa = app_forward_auth.forward_auth_directives(fa_config)

    # 30.6.2 c2-J4 fail-closed (S8): an active site MUST carry the client-header
    # strip block.  An empty render means the strip regressed, so a client
    # `X-Mod-*` would reach the upstream as if MOD itself set it.  `reconcile`
    # maps this ValueError to applied=False + the loud "app-route reconcile
    # REFUSED to push (fail closed)" log (app_routing.py:784-788), so the
    # previously-running Caddyfile stays live and nothing partial is pushed.
    # `upstream is None` (the 503 responder below) stays legal: no proxy,
    # nothing to spoof.
    if fa == "" and upstream is not None:
        raise ValueError(
            "app site renders no client-header strip — refusing to push an unstripped site"
        )

    if upstream is None:
        body = (
            (f"\t\t{tls.strip()}\n" if tls else "")
            + hsts_header("\t\t")
            + '\t\trespond "app target unavailable" 503\n'
        )
        return f"\n{hostname} {{\n{body}}}\n"

    auth_flow = app_forward_auth.auth_flow_directives()

    # Inside a route block we need \t\t indentation.  auth_flow and fa
    # already carry lines starting with \t, so strip one leading tab and
    # prepend two (shift each line +1 tab).
    def _reindent(s: str) -> str:
        out: List[str] = []
        for line in s.split("\n"):
            if not line:
                continue
            if line.startswith("\t"):
                out.append("\t\t" + line[1:])
            else:
                out.append("\t\t" + line)
        return "\n".join(out) + "\n"

    route_block = (
        _reindent(auth_flow)
        + _reindent(fa)
        + f"\t\treverse_proxy {upstream} {{\n"
        f"\t\t\theader_up Host {{host}}\n"
        f"\t\t\theader_up X-Forwarded-Proto https\n"
        f"\t\t}}\n"
    )

    body = (
        f"\t{tls.strip()}\n" if tls else ""
    ) + hsts_header() + f"route {{\n{route_block}}}\n"
    site = f"\n{hostname} {{\n{body}}}\n"
    if not plain_http:
        return site
    # PLAIN-HTTP-APP-SITE: the explicit http:// site owns :80 for this host
    # (defining it suppresses Caddy's auto HTTP→HTTPS redirect — same
    # mechanism as the CA-distribution block in caddy-front-entrypoint.sh).
    # SAME route block as the https site: auth flow, forward_auth, the
    # client-header strip (the fail-closed ValueError above covers it), and
    # reverse_proxy pinning X-Forwarded-Proto https so the upstream (and the
    # MOD auth redirects/cookies it derives) keep the public https scheme.
    # NO tls directive and NO HSTS here: browsers ignore HSTS over http, and
    # advertising it on a plain site would force https on the next visit.
    return site + f"\nhttp://{hostname} {{\nroute {{\n{route_block}}}\n}}\n"


def render_app_sites(apps: List[models.App], upstreams: Dict[int, Optional[str]],
                     fa_configs: Optional[Dict[int, Optional[dict]]] = None,
                     plain_http: bool = False) -> str:
    """Render all app site blocks (concatenated Caddyfile text).

    ``plain_http`` (platform setting apps.edge_plain_http) additionally emits
    an explicit ``http://<hostname>`` site per app — see render_app_site."""
    fa_configs = fa_configs or {}
    return "".join(
        render_app_site(a, upstreams.get(a.id), fa_configs.get(a.id),
                        plain_http=plain_http)
        for a in apps
    )


def read_base_caddyfile(path: Optional[str] = None) -> Optional[str]:
    """Read the base (platform-only) Caddyfile the caddy-front entrypoint
    published into the shared admin volume.

    This is the file Caddy actually runs on start — merging onto it (instead
    of re-rendering the template here) guarantees an app apply can NEVER drop
    the platform sites. Returns None when the file is absent/unreadable;
    callers MUST fail closed on that.
    """
    p = path if path is not None else os.getenv(CADDY_ADMIN_BASE_ENV,
                                                "/run/caddy-admin/base.Caddyfile")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        logger.error("base Caddyfile %s unreadable — refusing to push config "
                     "without the platform sites: %s", p, e)
        return None


def inject_tls_policy(base_text: str, min_version: str) -> str:
    """FX.124 part 3 (Q111=A) — normalize the TLS policy (protocols +
    curves) on every TLS-terminating Caddyfile block in ``base_text``.

    Pure text transform, no DB: the front Caddy's base file is rendered by a
    shell entrypoint that can only bake the 1.2 baseline, so the ACTIVE
    floor (the tls.min_version setting — "1.3" on a gov install) lands on
    the platform sites HERE, at each admin-API /load. This NORMALIZES: a
    block that already carries protocols/curves lines (the entrypoint's
    baseline, or a previous pass) has them replaced in place, so a
    1.2 → 1.3 upgrade re-renders without duplicating directives.

    Handles BOTH TLS terminator forms present in the rendered base:
      * a ``tls { ... }`` block (internal mode / app sites) — the policy
        lines are inserted just before the block's closing ``}`` (or
        replace the existing pair in place);
      * an explicit-pair ``tls /cert /key`` line (the portless mesh-login
        vhost) — a ``tls { protocols ...; curves ... }`` block is appended
        right after the pair line (Caddy merges an explicit pair and a tls
        block within the same site; verified with ``caddy validate``).

    Sites with NO TLS terminator (the :2021 health block, the http:// CA
    block) are never touched.
    """
    if min_version not in ("1.2", "1.3"):
        min_version = "1.2"
    protocols = "tls1.3" if min_version == "1.3" else "tls1.2 tls1.3"
    prot_line = f"\t\tprotocols {protocols}"
    curves_line = "\t\tcurves x25519mlkem768 x25519 secp384r1 secp256r1"
    lines = base_text.split("\n")
    # line index -> lines to insert BEFORE it / lines to REPLACE it with.
    inserts: Dict[int, List[str]] = {}
    replace: Dict[int, List[str]] = {}

    def _normalize_block(open_i: int) -> None:
        """Normalize the policy of the ``tls {`` block opened at ``open_i``
        (a site-level, one-tab opener)."""
        j = open_i + 1
        depth = 1
        prot = cur = None
        close = -1
        while j < len(lines):
            s = lines[j]
            if not s.strip().startswith("#"):
                depth += s.count("{") - s.count("}")
                if re.match(r"^\s{2}protocols\s", s):
                    prot = j
                elif re.match(r"^\s{2}curves\s", s):
                    cur = j
            if depth <= 0:
                close = j
                break
            j += 1
        if close < 0:
            return
        if prot is not None:
            # Replace in place (this is what makes a 1.2→1.3 upgrade
            # re-render instead of duplicate).
            replace[prot] = [prot_line] + ([] if cur is not None
                                            else [curves_line])
            if cur is not None:
                replace[cur] = [curves_line]
        else:
            inserts[close] = [prot_line, curves_line]

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        # A site-level TLS directive (one tab of indent, so nested blocks
        # and comments are never touched).
        if line.startswith("\ttls ") and not line.startswith("\t\t"):
            if s == "tls {":
                _normalize_block(i)
            elif re.match(r"^\s*tls\s+\S+\s+\S+\s*$", line):
                # Explicit pair (tls /cert /key). The next real directive
                # (skipping comments/blanks) is a ``tls {`` policy block
                # only if the renderer already emitted one — normalize it;
                # otherwise insert one right after the pair line.
                k = i + 1
                found = -1
                while k < len(lines):
                    kk = lines[k].strip()
                    if kk.startswith("#") or kk == "":
                        k += 1
                        continue
                    if kk == "tls {":
                        found = k
                    break  # first real directive decides; nothing else does
                if found > 0:
                    _normalize_block(found)
                elif (i + 1) not in inserts:
                    inserts[i + 1] = [
                        "\ttls {", prot_line, curves_line, "\t}",
                    ]
        i += 1
    if not inserts and not replace:
        return base_text
    out: List[str] = []
    for n, line in enumerate(lines):
        if n in inserts:
            out.extend(inserts[n])
        if n in replace:
            out.extend(replace[n])
        else:
            out.append(line)
    return "\n".join(out)


def build_full_caddyfile(app_sites: str, base: Optional[str] = None,
                         base_path: Optional[str] = None) -> str:
    """Assemble the FULL front Caddyfile: the entrypoint's rendered base
    (platform global block, :2021 health site, http:// CA site, platform
    vhosts, any static blocks like the mesh login server) + the app sites
    appended.

    App sites are EXPLICIT per-host blocks (never wildcards, §7.1) so Caddy
    issues a cert only for hostnames a tenant admin registered.

    FX.124 part 3 (Q111=A): before appending the app sites, the TLS policy
    (protocols per the tls.min_version setting + the strong curve set) is
    injected into every ``tls {`` block of the base — this is how the
    platform sites (rendered by the shell entrypoint, which has no settings
    access) pick up the gov TLS-1.3-only floor without a container restart.

    FAILS CLOSED: without a readable base file the platform sites would be
    lost on the full-replace, so this raises 503-equivalent (ValueError —
    the caller maps it to a logged failure) instead of pushing.
    """
    if base is None:
        base = read_base_caddyfile(base_path)
    if base is None:
        raise ValueError(
            "base Caddyfile missing — refusing to push an app config without "
            "the platform sites (caddy_front_admin volume not published?)"
        )
    base = inject_tls_policy(base, tls_min_version())
    text = base
    if not text.endswith("\n"):
        text += "\n"
    return text + app_sites


# ---------------------------------------------------------------------------
# Admin API apply
# ---------------------------------------------------------------------------

def admin_socket() -> str:
    """caddy-front admin UNIX-socket path (an infra address, bootstrap-class —
    read from env, NOT the behavioural settings registry, so this call is
    DB-free and can never run inside an open transaction)."""
    return os.getenv(CADDY_ADMIN_SOCKET_ENV,
                     "/run/caddy-admin/admin.sock") or \
        "/run/caddy-admin/admin.sock"


def apply_config(caddyfile_text: str, uds: Optional[str] = None) -> dict:
    """POST the full Caddyfile to caddy-front's admin API (atomic
    validate-then-apply) over the shared-volume UNIX socket.

    The socket path comes from the CADDY_FRONT_ADMIN_SOCKET env (compose), so
    this stays DB-free and can never run inside an open transaction. The
    request uses a virtual Host (``caddy-admin``) — Caddy checks Host/Origin
    on the admin API, and for a unix-socket listener the default enforce_origin
    is lenient, so a stable virtual host is all that is needed. No bearer
    token: the admin API has none; the socket's reach is the auth.

    Raises HTTPException(502) on transport failure, 500 on a Caddy rejection
    (bad config — the running config stays live).
    """
    sock = uds or admin_socket()
    url = f"http://{CADDY_ADMIN_HOST_HEADER}/load?adapt=caddyfile"
    transport = httpx.HTTPTransport(uds=sock)
    try:
        with httpx.Client(timeout=ADMIN_TIMEOUT_SECONDS, transport=transport) as client:
            r = client.post(url, content=caddyfile_text.encode("utf-8"),
                            headers={"Host": CADDY_ADMIN_HOST_HEADER,
                                     "Content-Type": "text/caddyfile"})
    except httpx.RequestError as e:
        logger.error("caddy-front admin socket %s unreachable: %s", sock, e)
        raise HTTPException(status_code=502,
                            detail="caddy-front admin API unreachable (unix socket)")
    if r.status_code >= 400:
        logger.error("caddy-front rejected app config (HTTP %s): %s",
                     r.status_code, r.text[:500])
        raise HTTPException(
            status_code=500,
            detail=f"caddy-front rejected the rendered config (HTTP {r.status_code})",
        )
    return {"status": "applied", "http": r.status_code, "uds": sock}


class _AppView:
    """Plain-data stand-in for a models.App after the session is committed —
    rendering must not touch the DB (and thus must not open a transaction)
    after the load phase ends."""
    __slots__ = ("id", "hostname", "target_kind")

    def __init__(self, id: int, hostname: str, target_kind: Optional[str]):
        self.id = id
        self.hostname = hostname
        self.target_kind = target_kind


def _active_apps_with_targets(db) -> List[Tuple[int, str, Optional[str], Optional[dict]]]:
    """Load all live (non-soft-deleted, active) apps + their stacks, resolve
    each upstream, and read each app's forward-auth config (GX.S1). Returns
    plain (id, hostname, upstream, fa_config) tuples so the render/apply phase
    needs no DB access at all (the fa_config dict is materialised here)."""
    apps = (
        db.query(models.App)
        .filter(models.App.deleted_at.is_(None))
        .filter(models.App.status == "active")
        .filter(models.App.hostname.isnot(None))
        .all()
    )
    out: List[Tuple[int, str, Optional[str], Optional[dict]]] = []
    for app in apps:
        stack = None
        if app.target_kind == "stack-service" and app.stack_id is not None:
            stack = db.get(models.PersistentStack, app.stack_id)
        fa = app_forward_auth_config(app)  # JSONB dict, defaults applied
        out.append((app.id, app.hostname, resolve_upstream(app, stack), fa))
    return out


def app_forward_auth_config(app: models.App) -> Optional[dict]:
    """Materialised forward-auth config for rendering (defaults applied).
    Kept as a plain dict so it can be snapshotted before the session closes."""
    from services import app_forward_auth
    return app_forward_auth.config_for(app)


def reconcile(db) -> dict:
    """Idempotent full-replace of the app sites on caddy-front from the DB.

    ``db`` is a Session owned by the caller (the scheduler tick opens + closes
    its own; the per-change trigger does the same). Loads rows, COMMITs the
    session, then renders + applies — the admin HTTP call is NEVER inside an
    open transaction.

    Returns a summary: {"apps": n, "applied": bool}.
    """
    _register_settings()
    if not routing_enabled(db=db):
        return {"apps": 0, "active": 0, "applied": False, "skipped": "disabled"}

    rows = _active_apps_with_targets(db)
    # PLAIN-HTTP-APP-SITE: snapshot the opt-in switch on the same session
    # before the transaction closes (same pattern as the fa configs).
    plain_http = edge_plain_http(db=db)
    db.commit()  # end the load transaction before the admin call

    views = [_AppView(i, h, None) for (i, h, _u, _f) in rows]
    upstreams = {i: u for (i, _h, u, _f) in rows}
    fa_configs = {i: f for (i, _h, _u, f) in rows}
    # CE (no caddy-front): skip cleanly when the admin socket is absent.
    # Paid installs must still fail-closed on transient socket absence.
    sock = admin_socket()
    if not os.path.exists(sock):
        try:
            from utils.paid_import import is_ce_edition
        except ImportError:  # noqa: BLE001
            is_ce_edition = lambda: False  # unknown = treat as paid → continue to fail-closed
        if is_ce_edition():
            logger.info("app-route reconcile skipped: no caddy front (CE edition)")
            return {"apps": len(views), "active": len(views), "applied": False, "skipped": "no caddy front"}
        # Paid: continue to fail-closed
    try:
        # 30.6.2 c2-J4: the RENDER is inside the fail-closed try so a site that
        # renders no client-header strip (the ValueError from render_app_site)
        # takes the SAME path as a missing base file below — applied=False + the
        # loud "REFUSED to push (fail closed)" log, no admin call, the
        # previously-running Caddyfile stays live.
        app_sites = render_app_sites(views, upstreams, fa_configs,
                                     plain_http=plain_http)
        full = build_full_caddyfile(app_sites)  # ValueError if base missing
        applied = True
        apply_config(full)
    except HTTPException as e:
        # A rejected config keeps the running one live — log, don't crash the
        # loop. The next pass retries.
        logger.warning("app-route reconcile apply failed: %s", e.detail)
        applied = False
    except ValueError as e:
        # FAIL CLOSED: no base file ⇒ we would drop the platform sites. Never
        # push; log loudly so the operator sees the missing volume publish.
        logger.error("app-route reconcile REFUSED to push (fail closed): %s", e)
        applied = False

    return {"apps": len(views), "active": len(views), "applied": applied}


def start_reconcile_job() -> None:
    """Register the periodic reconcile in the existing APScheduler.

    Called from main.py startup alongside start_scheduler(). Each tick opens
    its own session (leader-only process; per-change triggers run on every
    worker via trigger_reconcile_async). The sweep re-fences external-url
    targets (§7.5) and auto-flags any that now resolve into a blocked range.
    """
    try:
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception:
        logger.warning("apscheduler unavailable — app-route reconcile not started")
        return

    try:
        from services.scheduler import _scheduler
    except Exception:
        logger.warning("scheduler not initialised — app-route reconcile not started")
        return

    if _scheduler is None or not getattr(_scheduler, "running", False):
        # start_scheduler() is a Redis-leader gate: only the leader worker
        # actually runs it, so a non-leader worker simply skips.
        logger.debug("scheduler not running in this worker — "
                     "app-route reconcile job not registered here")
        return

    def _tick() -> None:
        from database import SessionLocal
        db = SessionLocal()
        try:
            res = reconcile(db)
            _sweep_external_targets(db)
            logger.info("app-route reconcile: %s", res)
        except Exception as e:  # noqa: BLE001
            logger.exception("app-route reconcile tick failed: %s", e)
        finally:
            db.close()

    _scheduler.add_job(
        _tick,
        IntervalTrigger(hours=1),
        id="app_route_reconcile",
        name="App hostname route reconcile (GX.A2)",
        replace_existing=True,
    )


def trigger_reconcile_async() -> None:
    """Fire-and-forget reconcile after a create/update/revoke (own thread + own
    session so the request path never blocks on the admin API)."""
    import threading

    def _run() -> None:
        from database import SessionLocal
        db = SessionLocal()
        try:
            reconcile(db)
        except Exception as e:  # noqa: BLE001
            logger.exception("triggered app-route reconcile failed: %s", e)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name="app-route-reconcile").start()


# A stack redeploy moves its published host port, so an App routed at
# `stack-service` must be re-pointed without waiting for the hourly sweep.
# Several redeploys landing together (a rebalance, several migrates at once) share ONE
# delayed refresh: the delay also lets the deploy-result commit become visible
# to the refresh's own session.
RETARGET_COALESCE_SECONDS = 2.0
_retarget_lock = None
_retarget_pending = False


def _schedule_retarget_refresh() -> bool:
    """Schedule one coalesced route refresh. Returns False when one is already
    pending (this call was folded into it)."""
    import threading

    global _retarget_lock, _retarget_pending
    if _retarget_lock is None:
        _retarget_lock = threading.Lock()
    with _retarget_lock:
        if _retarget_pending:
            return False
        _retarget_pending = True

    def _fire() -> None:
        global _retarget_pending
        with _retarget_lock:
            _retarget_pending = False
        try:
            trigger_reconcile_async()
        except Exception as e:  # noqa: BLE001
            logger.warning("app-route retarget refresh failed: %s", e)

    t = threading.Timer(RETARGET_COALESCE_SECONDS, _fire)
    t.daemon = True
    t.name = "app-route-retarget"
    t.start()
    return True


def refresh_routes_for_stack(db, stack_id: int) -> int:
    """Re-point app routes after a stack's published host ports changed.

    Call AFTER the deploy-result transaction committed, and only when the
    recorded host-port mapping actually changed. Reuses the app-write refresh
    (`trigger_reconcile_async`, which re-resolves every upstream from the DB and
    pushes through the caddy-front admin socket). Best effort: never raises, so
    a caddy/DB problem can never change the deploy-result ingest outcome; on a
    failed query the session is rolled back.

    Returns the number of live apps routed at this stack (0 = nothing done).
    """
    try:
        n = (
            db.query(models.App)
            .filter(models.App.stack_id == stack_id)
            .filter(models.App.target_kind == "stack-service")
            .filter(models.App.deleted_at.is_(None))
            .filter(models.App.status == "active")
            .filter(models.App.hostname.isnot(None))
            .count()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("app-route retarget lookup failed for stack %s: %s", stack_id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    if not n:
        return 0
    try:
        _schedule_retarget_refresh()
    except Exception as e:  # noqa: BLE001
        logger.warning("app-route retarget refresh not scheduled for stack %s: %s",
                       stack_id, e)
        return 0
    logger.info("app-route retarget scheduled: stack %s moved ports, %d app(s)",
                stack_id, n)
    return n


def _sweep_external_targets(db) -> None:
    """Hourly SSRF re-fence (§7.5): re-resolve every active external-url
    target; auto-flag the app provisioning_failed (audit) if it NOW resolves
    into a blocked range (DNS rebinding after create).

    ``db`` is the caller's session. The DNS lookups run with the load
    transaction COMMITTED (external fetch never inside a DB transaction), so
    this is: load → commit → resolve → flag.
    """
    apps = (
        db.query(models.App)
        .filter(models.App.deleted_at.is_(None))
        .filter(models.App.status == "active")
        .filter(models.App.target_kind == "external-url")
        .filter(models.App.external_url.isnot(None))
        .all()
    )
    db.commit()
    for app in apps:
        try:
            validate_external_url(app.external_url)  # blocking DNS, no txn open
        except HTTPException as e:
            app.status = "provisioning_failed"
            app.failure_detail = {"step": "external-target-sweep",
                                  "error": str(e.detail)}
            _audit_app_event(db, app, "APP_EXTERNAL_TARGET_BLOCKED",
                             {"hostname": app.hostname, "reason": str(e.detail)})
            logger.warning("app %s external target now blocked: %s", app.id, e.detail)


def _audit_app_event(db, app: models.App, event_name: str, details: dict) -> None:
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=app.tenant_id,
            user_id=app.created_by_user_id,
            event_name=event_name,
            details=details,
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        pass
