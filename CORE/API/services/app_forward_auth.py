"""GX.S1 — app forward-auth: per-app require_login / allowed_groups /
public_paths config, the Caddy ``forward_auth`` rendering, and the helpers the
verify endpoint (routers/apps.py GET /v1/apps/auth/verify) uses.

How it works (design, decomp gx-mod-apps S1/S2):
  * The app's caddy-front site block renders a ``forward_auth`` directive
    pointing at the API's verify endpoint (``http://api:80``
    + ``/v1/apps/auth/verify``), with ``copy_headers`` for the X-Mod-*
    identity headers. Caddy sends the client's Authorization header to the
    verify endpoint; on 2xx it copies the named response headers onto the
    original request and reverse_proxies to the app.
  * The verify endpoint validates the Bearer token with the platform's
    standard JWKS check (audience = fastapi-app, which the app client's
    audience mapper adds), requires ``azp == <this app's KC client>`` (the
    token must have been minted by THIS app's client), enforces
    allowed_groups, and returns 200 + X-Mod-* headers or 401 + the app's KC
    login URL.
  * Per-app config lives in ``apps.forward_auth_config`` (JSONB, one column —
    migration 20261002_app_forward_auth.py; code defaults when NULL):
        {"require_login": bool, "allowed_groups": [str], "public_paths": [str]}
    require_login=false ⇒ no forward_auth rendered at all (app fully public).
    public_paths are prefix matchers rendered as a ``@public`` matcher that
    EXCLUDES those paths from forward_auth (rendered as ``@mod_protected`` =
    ``not path ...``; 2400GX-n3f fixed an inverted matcher). A public path
    ``/p`` renders as ``/p /p/*`` (exact + subtree); an explicit trailing ``*``
    is a prefix opt-in. public_paths match case-insensitively (Caddy path
    matcher).
  * Reconciled from the DB by app_routing (same hourly sweep + per-change
    trigger as A2) — survives restarts.

GX.S1c — browser session for forward-auth. A plain browser page load carries
no Bearer token, so MOD itself runs the OIDC code flow ON THE APP'S OWN HOST
(Caddy routes ``/_mod/auth/*`` to the API, public, BEFORE forward_auth):
  * ``GET /v1/apps/auth/login`` mints a PKCE S256 pair + state and stores them
    in a short-lived SIGNED COOKIE (never the DB), then 302s to the realm's
    authorize endpoint for the app's KC client.
  * ``GET /v1/apps/auth/callback`` verifies state (CSRF), exchanges the code
    (PKCE verifier — the app client is a PUBLIC code-flow client, so PKCE is
    the credential), validates the token with the standard JWKS check +
    ``azp == <app client>``, sets the session cookie and 302s to the original
    (relative-only) path.
  * The session cookie ``__Host-mod_app_session`` is a compact HS256 JWT
    (app_id, kc sub, email, groups, user_type, exp), scoped to the app host
    (no Domain attribute). ``verify`` accepts the cookie OR a Bearer token;
    with no valid credential it 302s to ``/_mod/auth/login`` for HTML
    requests (Caddy's forward_auth returns ANY non-2xx — including a 3xx —
    to the client unchanged) and 401-JSON otherwise.
  * The HS256 key is fetched from Vault KV (``apps/mod_app_session_hmac``)
    or derived from AUDIT_HMAC_KEY via HKDF (default, controlled by the
    platform setting ``apps.session_hmac_source``).  In vault mode the KV
    carries ``current`` + optional ``previous`` keys; sign uses ``current``
    only, verify accepts either (rotation grace).  Default is ``"derived"``
    so the HKDF path remains active until the lead seeds Vault and flips the
    setting.  Per-tenant rotation via
    ``POST /v1/settings/apps.session_hmac_key.rotate`` is a follow-up; one
    platform key is correct while the API is a single signing authority).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod
import threading
import uuid
import logging
import os
import re
import secrets
import time
from typing import Dict, List, Optional

import models

# Lazy platform-settings reader — imported at module level so it can be
# referenced by _session_hmac_key() at call time (not at import time).
import services.platform_settings as _psr  # noqa: F401  # module import

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session HMAC key — dedicated Vault key (vault mode) or derived from
# AUDIT_HMAC_KEY (derived mode).  The setting `apps.session_hmac_source`
# controls the mode; default "derived" keeps existing behaviour.
# ---------------------------------------------------------------------------

class SessionHmacError(Exception):
    """Raised when the session HMAC key cannot be loaded (vault mode, no
    prior load, Vault unreachable)."""


def _session_source_validator(value: str) -> None:
    """Allowed values: 'derived', 'vault'."""
    if value not in ("derived", "vault"):
        raise ValueError(
            f"apps.session_hmac_source must be 'derived' or 'vault', "
            f"got {value!r}"
        )


def register_settings() -> None:
    """Idempotently register the GX.S1c tunables (tunables = DB platform
    settings, never env)."""
    if "apps.session_ttl_seconds" in _psr._REGISTRY:
        return
    _psr.register(
        "apps.session_ttl_seconds",
        int,
        28800,
        group="apps",
        description="App browser-session cookie TTL in seconds "
                    "(__Host-mod_app_session). Default 28800 (8h).",
    )
    _psr.register(
        "apps.session_hmac_source",
        str,
        "derived",
        group="apps",
        validator=_session_source_validator,
        description="Where the app-session HMAC key comes from. "
                    "'derived' = HKDF from AUDIT_HMAC_KEY (same as before); "
                    "'vault' = read from Vault KV "
                    "apps/mod_app_session_hmac (current/previous rotation). "
                    "Default 'derived' — flip only after seeding Vault.",
    )


# The verify endpoint the rendered forward_auth calls. The api service is
# stable on the compose network as `api:80` (infra address, not a tunable).
VERIFY_UPSTREAM = "http://api:80"
VERIFY_URI = "/v1/apps/auth/verify"

# Identity headers the verify endpoint sets and forward_auth copies onto the
# proxied request (S1). copy_headers is a WHITELIST — nothing else from the
# verify response leaks into the app request.
IDENTITY_HEADERS = ("X-Mod-User", "X-Mod-Email", "X-Mod-Groups", "X-Mod-User-Type")

# public_paths entries become Caddy `path` matcher tokens (`p` + `p/*`).
# Only path-safe characters are allowed (no spaces/braces/quotes — the Caddyfile
# is text, so a hostile entry must not be able to close the block).
_PATH_TOKEN_RE = re.compile(r"^/[A-Za-z0-9._/*-]*$")


def default_config() -> dict:
    return {"require_login": False, "allowed_groups": [], "public_paths": [],
            "fail_closed": False}


def _fail_closed_for(require_login: bool, stored) -> bool:
    """2400GX-c2-J1: per-App fail-closed posture.

    Omitted/None ⇒ derived: True iff the App is login-protected (a public App
    never silently becomes fail-closed). An explicit bool is kept as given.
    """
    if isinstance(stored, bool):
        return stored
    return bool(require_login)


def config_for(app: models.App) -> dict:
    """The app's forward-auth config with defaults applied.

    ``forward_auth_config`` is a JSONB column that SQLAlchemy hands back as a
    dict (never call .astext on it — custom JSONB throws). NULL / non-dict
    rows mean "use the defaults" (public app, no forward_auth rendered).
    """
    raw = getattr(app, "forward_auth_config", None)
    if not isinstance(raw, dict):
        raw = {}
    require_login = bool(raw.get("require_login", False))
    return {
        "require_login": require_login,
        "allowed_groups": [str(g) for g in (raw.get("allowed_groups") or [])],
        "public_paths": [str(p) for p in (raw.get("public_paths") or [])],
        "fail_closed": _fail_closed_for(require_login, raw.get("fail_closed")),
    }


def validate_config(require_login: bool, allowed_groups: List[str],
                    public_paths: List[str],
                    fail_closed: Optional[bool] = None) -> dict:
    """Validate a PUT payload and return the storable config dict.

    Raises ValueError with a human message (the router maps it to 400).
    public_paths must be Caddy-``path``-matcher-safe (see _PATH_TOKEN_RE).
    ``fail_closed`` (2400GX-c2-J1): None/omitted ⇒ derived from
    ``require_login``; an explicit bool is kept; anything else (``"true"``,
    ``1``) is rejected — S7 strict bool.
    """
    if fail_closed is not None and not isinstance(fail_closed, bool):
        raise ValueError("fail_closed must be a boolean")
    groups = [str(g).strip() for g in (allowed_groups or [])]
    if any(not g for g in groups):
        raise ValueError("allowed_groups entries must be non-empty strings")
    paths: List[str] = []
    for p in (public_paths or []):
        p = str(p)  # stored as given; a trailing "*" is an explicit prefix opt-in
        if not _PATH_TOKEN_RE.match(p):
            raise ValueError(
                f"public_paths entry {p!r} is not a path prefix "
                "(must start with '/' and contain no spaces/braces)"
            )
        paths.append(p)
    rl = bool(require_login)
    return {"require_login": rl,
            "allowed_groups": groups,
            "public_paths": paths,
            "fail_closed": _fail_closed_for(rl, fail_closed)}


def edge_ready() -> bool:
    """True when the ingress ask token is present and non-empty.

    2400GX-c2-J1: the router uses this to answer `edge_ready` on the App
    responses and to 409 a `fail_closed=true` PUT when the edge cannot verify.
    The token itself is NEVER returned or logged — only the bool. The import is
    lazy (inside the function) so this service stays importable without the
    router package, and the read is the existing live Vault read with a 60 s
    cache (`routers/persistent_stacks/_shared._ingress_ask_token`), which must
    never block a GET: any exception ⇒ False + a debug line (no value).
    """
    try:
        from routers.persistent_stacks._shared import _ingress_ask_token
        return bool(_ingress_ask_token())
    except Exception:  # noqa: BLE001 — edge_ready must never raise (J2 vet)
        logger.debug("edge_ready: ingress ask token unreadable — treating edge "
                     "as not ready")
        return False


# TODO(GX.S1c-stacks): forward-auth on STACK ingress routes. The stack
# Caddy sites are rendered by the EXTERNAL caddy-stack-ingress container
# (this API only serves its on-demand-TLS `ask` seam —
# routers/persistent_stacks/_shared.py `_ingress_authorized_domain`), so this
# wiring cannot land in this tree. Where the stack site is generated, reuse
# `forward_auth_directives(cfg)` + `auth_flow_directives()` for any stack
# carrying an `x-mod.ingress.auth` block {require_login, allowed_groups,
# public_paths}, and extend `get_app_by_hostname` (or add a
# `get_stack_by_hostname` sibling) so the verify endpoint in routers/apps.py
# (`app_auth_verify` → `_resolve_identity`) can resolve a stack host.


def auth_flow_directives() -> str:
    """Render the Caddy ``handle_path /_mod/auth*`` block that routes the
    MOD-handled OIDC flow (login/callback/logout) to the API.

    This is ALWAYS public (it must be reachable by a logged-out browser).
    MUST render BEFORE ``forward_auth`` inside the ``route { }`` block
    that wraps auth directives — Caddy sorts directives by its standard
    order at the site-block level, so literal ordering is not guaranteed
    without the ``route`` container. ``handle_path`` strips the
    ``/_mod/auth`` prefix, then ``rewrite`` re-anchors the remainder under
    ``/v1/apps/auth`` (the query string travels with ``{uri}``), so a browser
    hitting ``/_mod/auth/login?rd=/x`` arrives at the API as
    ``/v1/apps/auth/login?rd=/x``. The ``reverse_proxy`` carries NO
    ``header_up Host`` line on purpose: caddy already forwards the inbound
    Host to the upstream, which is what the API needs to resolve the app/stack,
    and the absence of that line is how the site renderers tell this handle
    apart from an app's own upstream ``reverse_proxy``.
    """
    return (
        "\thandle_path /_mod/auth* {\n"
        f"\t\trewrite * {API_AUTH_PREFIX}{{uri}}\n"
        f"\t\treverse_proxy {VERIFY_UPSTREAM}\n"
        "\t}\n"
    )


def forward_auth_directives(config: Optional[dict]) -> str:
    """Render the Caddy lines for one app site.

    2400GX-c2-J1: the X-Mod-* client-header strip is emitted for EVERY site,
    login-protected or public; a public App renders strip-only (no
    ``forward_auth``). A login-protected App renders strip + forward_auth
    exactly as before.

    Output (internal example):
        \trequest_header -X-Mod-User   (one per identity header, then -X-Mod-*)
        \t@mod_protected {
        \t\tnot path /healthz*
        \t}
        \tforward_auth @mod_protected http://api:80 {
        \t\turi /v1/apps/auth/verify
        \t\tcopy_headers X-Mod-User X-Mod-Email X-Mod-Groups X-Mod-User-Type
        \t}

    ``uri`` sets the path the verify request hits (forward_auth's upstream
    argument is host:port only); ``copy_headers`` whitelists exactly the
    identity headers from the verify response onto the proxied request.
    """
    cfg = config if isinstance(config, dict) else default_config()
    lines: List[str] = []
    # 2400GX-c2-J1: the client-header strip is emitted for EVERY app site —
    # it used to sit below the `require_login` early-return, so a public App
    # (require_login=false) stripped NOTHING and a client-supplied X-Mod-*
    # reached the upstream as if MOD had set it (S8/c4 gap). Caddy 2 (`caddy:2-
    # alpine`, CORE/startup/docker-compose.yml:2720) supports the `-Name*`
    # prefix-delete form — CORE/startup/scripts/caddy-entrypoint.sh:379 already
    # renders the bare `request_header -X-Mod-*` on the stack-ingress route and
    # CORE/startup/tests/test_stack_ingress_auth_snippet.sh:261 asserts it — so
    # the wildcard stays, plus the exact identity names (one field per
    # directive, literal-first inside the caller's route{}).
    for _h in (*IDENTITY_HEADERS, ASSERTION_HEADER, "X-Mod-*"):
        lines.append(f"\trequest_header -{_h}")
    if not cfg.get("require_login"):
        # A public App renders strip-only: no @mod_protected, no forward_auth
        # (S9: never emit a forward_auth we cannot verify).
        return "".join(line + "\n" for line in lines)
    # 2400GX-n3fR3: exact + subtree (`p` and `p/*`), so /healthz does not make
    # /healthzz public; an explicit trailing "*" in config stays a prefix match.
    paths: List[str] = []
    for p in (cfg.get("public_paths") or []):
        p = str(p)
        paths += [p] if p.endswith("*") else [p.rstrip("/") or "/", p.rstrip("/") + "/*"]
    lines: List[str] = []
    # 2400GX-n3f: strip client-supplied identity headers FIRST, unconditionally
    # (public_paths skip forward_auth, so without this a client X-Mod-* header
    # would reach the upstream as if MOD set it; and copy_headers only
    # overwrites a header when verify returned one). Exact names + the X-Mod-
    # prefix. Inside the caller's route{} this is literal-first.
    # (request_header takes ONE field per directive.)
    for _h in (*IDENTITY_HEADERS, ASSERTION_HEADER, "X-Mod-*"):
        lines.append(f"\trequest_header -{_h}")
    # public_paths: forward_auth takes a matcher selecting the requests it
    # APPLIES to, so the matcher must be the NEGATION (everything except the
    # public prefixes). The old `forward_auth @public` applied it to the
    # public paths ONLY and left every other path unauthenticated.
    if paths:
        lines.append("\t@mod_protected {\n\t\tnot path " + " ".join(paths) + "\n\t}")
    matcher = " @mod_protected" if paths else ""
    lines.append(
        f"\tforward_auth{matcher} {VERIFY_UPSTREAM} {{\n"
        f"\t\turi {VERIFY_URI}\n"
        f"\t\tcopy_headers {' '.join(IDENTITY_HEADERS + (ASSERTION_HEADER,))}\n"
        "\t}"
    )
    # 30.6.4 c4b — OPTIONAL identity on public_paths, rendered ONLY when
    # require_login is true AND public_paths is non-empty. MUST-1: the matcher
    # requires the MOD session cookie, so a request with NO cookie (probe, curl,
    # logged-out browser) never matches and never reaches the API — a public
    # path keeps working even when the API is down. A logged-in browser DOES
    # match and gets identity + X-Mod-Assertion on the public path too; the
    # verify endpoint's optional=1 variant answers 200 for every outcome, so
    # this can never redirect or 401 (MUST-2).
    if paths:
        cookie_re = "^|;\\s*" + re.escape(SESSION_COOKIE) + "="
        lines.append(
            "\t@mod_public_session {\n"
            "\t\tpath " + " ".join(paths) + "\n"
            f"\t\theader_regexp Cookie {cookie_re}\n"
            "\t}"
        )
        lines.append(
            f"\tforward_auth @mod_public_session {VERIFY_UPSTREAM} {{\n"
            f"\t\turi {VERIFY_URI}?optional=1\n"
            f"\t\tcopy_headers {' '.join(IDENTITY_HEADERS + (ASSERTION_HEADER,))}\n"
            "\t}"
        )
    return "".join(line + "\n" for line in lines)


# ---------------------------------------------------------------------------
# data access for the verify endpoint (service layer — no raw db.query in
# routers/apps.py, per the R5 lint convention)
# ---------------------------------------------------------------------------

def get_app_by_hostname(db, host: str) -> Optional[models.App]:
    """The live (non-soft-deleted) app owning this hostname, or None.

    ``host`` is the inbound Host header (caddy's forward_auth passes the
    original Host to the verify request); port and case are ignored.
    """
    host = (host or "").split(":")[0].strip().lower()
    if not host:
        return None
    return (
        db.query(models.App)
        .filter(models.App.hostname == host,
                models.App.deleted_at.is_(None))
        .first()
    )


def get_user_by_kc_id(db, keycloak_sub: str) -> Optional[models.User]:
    """MOD user linked to this KC principal, or None (a KC user who has never
    signed into MOD — identity headers are still emitted from the claims)."""
    if not keycloak_sub:
        return None
    return (
        db.query(models.User)
        .filter(models.User.keycloak_user_id == keycloak_sub)
        .first()
    )


def user_type_for(user, db=None, app=None, claims=None) -> str:
    """X-Mod-User-Type: the app decides what this kind of user sees (S1).

    A MOD user is ``member``/``external``. A KC identity with no MOD user is
    the app's own user type (GX.S1b ``app_users`` row, or created on first
    login by self-signup) when ``db``/``app``/``claims`` are given, else
    ``unlinked``.
    """
    if user is not None:
        return "external" if getattr(user, "is_external", False) else "member"
    if db is None or app is None or not claims:
        return "unlinked"
    try:
        from services import app_user_types as _aut
        sub = claims.get("sub", "")
        t = _aut.app_user_type_for(db, app.id, sub)
        if t is None:
            t = _aut.self_sign_up_for(db, app, sub, claims.get("email", ""),
                                      claims.get("groups") or [])
        return t or "unlinked"
    except Exception as exc:
        logger.warning("app user type lookup failed (app %s): %s",
                       getattr(app, "id", None), exc)
        return "unlinked"


def app_user_disabled(db, app, sub: str) -> bool:
    """True when this KC identity's app_users row for this app is disabled
    or soft-deleted — the app admin revoked them (GX.S1b)."""
    if not sub:
        return False
    try:
        row = (db.query(models.AppUser)
               .filter(models.AppUser.app_id == app.id,
                       models.AppUser.kc_sub == sub)
               .first())
    except Exception as exc:
        logger.warning("app user disabled check failed (app %s): %s",
                       getattr(app, "id", None), exc)
        return False
    return row is not None and (row.status != "active"
                                or row.deleted_at is not None)


def kc_client_id_for(app: models.App) -> str:
    """The app's KC client id (row value, else the deterministic form from
    keycloak_auth.app_client_id)."""
    if app.kc_client_id:
        return app.kc_client_id
    from keycloak_auth import keycloak_admin_client
    return keycloak_admin_client.app_client_id(app.tenant_id, app.slug)


def login_url_for(app: models.App) -> str:
    """The app's KC authorize base URL (client_id pre-filled).

    The app appends its own response_type/scope/redirect_uri/PKCE params per
    the OIDC code flow — the redirect URI must match the client's registered
    ``{origin}/*`` pattern.
    """
    base = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    return (f"{base}/realms/{realm}/protocol/openid-connect/auth"
            f"?client_id={kc_client_id_for(app)}")


# ---------------------------------------------------------------------------
# GX.S1c — browser session (signed cookie) for forward-auth
# ---------------------------------------------------------------------------

# The session cookie. The ``__Host-`` prefix is a BROWSER-enforced guarantee
# (Secure + no Domain attribute = scoped to the requesting app host only), so
# a cookie set on one app's host can never be replayed against another.
SESSION_COOKIE = "__Host-mod_app_session"

# BUG.B86e: cap on how many same-named session-cookie values a single request
# is allowed to make us HMAC-verify (see session_cookie_values() below).
_MAX_SESSION_COOKIE_CANDIDATES = 8

# Short-lived auth-CODE state cookie (PKCE verifier + state + rd). It is
# consumed at callback; it must outlive the time a human takes to log in.
STATE_COOKIE = "__Host-mod_app_auth_state"

# The Caddy ``handle_path`` prefix (rendered in render_app_site, BEFORE
# forward_auth, so it is always public) that routes the flow to the API.
MOD_AUTH_PATH = "/_mod/auth"
API_AUTH_PREFIX = "/v1/apps/auth"

# The HS256 key is fetched from this Vault KV v2 path (self-provisioned on
# first use, mirroring utils/build_task_token.py's managers/{id} pattern).
# When absent, the key is DERIVED from AUDIT_HMAC_KEY (an env secret every
# process already has at import time) so all workers share the same key.
_VAULT_PATH = "apps/mod_app_session_hmac"

# HKDF info label — never print or log this value.
_HKDF_INFO = b"mod-app-session-hmac-v1"

# ``rd`` (return destination) is a RELATIVE path only — anything else is an
# open redirect. Leading "/" + path-safe charset; no scheme, no "//host".
_RD_RE = re.compile(r"^/(?!/)[A-Za-z0-9._/?&=~-]*$")

# Auth-code state cookie TTL (5 min — a human logging in).
_STATE_TTL_SECONDS = 300


# Vault KV re-read TTL — seconds before we refresh the cached key pair.
_HMAC_CACHE_TTL = 300

# In-process cache: {"current": str|None, "previous": str|None, "ts": float}.
# "current" is always set when loaded (derived or vault). "previous" is set
# only in vault mode after rotation.  ts = monotonic clock of the last load.
_HMAC_KEY_CACHE: Dict[str, Optional[str]] = {"current": None, "previous": None}
_HMAC_CACHE_TS: float = 0.0


def _session_hmac_key() -> str:
    """The HS256 key for the app-session tokens.

    When ``apps.session_hmac_source`` == ``"derived"`` (default): derive
    deterministically from AUDIT_HMAC_KEY via HKDF-SHA256 so all workers
    share the same key, cached in-process.  Never logged.

    When ``"vault"``: read KV ``apps/mod_app_session_hmac`` which carries
    ``current`` (required, ≥ 32 bytes) and optional ``previous``.  Sign only
    with ``current``; accept either for verification (rotation grace window).
    Cache with a re-read TTL of ~300 s.  If a re-read fails and no key has
    ever been loaded, raise :exc:`SessionHmacError` — never fall back to
    derivation in vault mode.
    """
    source = _psr.get_setting("apps.session_hmac_source")

    if source == "vault":
        return _session_hmac_key_vault()
    # "derived" (or unknown — degrade to derived for safety)
    return _session_hmac_key_derived()


def _session_hmac_verify_keys() -> List[str]:
    """Keys to TRY, in order, when verifying a token's signature.

    Derived mode: just the one derived key (unchanged behaviour).
    Vault mode: ``current`` first, then ``previous`` if Vault has one — this
    is the rotation-grace window so a session minted just before a rotation
    is not invalidated the instant the new key is loaded. Signing (mint)
    always uses ``current`` only via :func:`_session_hmac_key`.
    """
    source = _psr.get_setting("apps.session_hmac_source")
    if source != "vault":
        return [_session_hmac_key_derived()]
    current = _session_hmac_key_vault()  # loads/refreshes the cache
    keys = [current]
    previous = _HMAC_KEY_CACHE.get("previous")
    if previous and previous != current:
        keys.append(previous)
    return keys


def _session_hmac_key_vault() -> str:
    """Vault-mode key: read from KV with current/previous rotation.

    Returns the ``current`` key.  If a re-read fails but we have a cached
    key, use it (logged at WARNING).  If no key has ever been loaded and
    Vault is unreachable, raise :exc:`SessionHmacError`.
    """
    global _HMAC_KEY_CACHE, _HMAC_CACHE_TS

    now = time.monotonic()
    if _HMAC_CACHE_TS > 0 and (now - _HMAC_CACHE_TS) < _HMAC_CACHE_TTL:
        return _HMAC_KEY_CACHE["current"]

    # --- Try Vault re-read ---
    loaded = False
    try:
        from utils.vault_client import approle_login, read_kv_v2
        role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
        secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
        if role_id and secret_id:
            token = approle_login(role_id=role_id, secret_id=secret_id)
            existing = None
            try:
                existing = read_kv_v2(_VAULT_PATH, token=token)
            except Exception:
                existing = None
            if existing:
                data = existing.get("data", existing)
                if not isinstance(data, dict):
                    data = {}
                current_b64 = data.get("current")
                previous_b64 = data.get("previous")
                if current_b64:
                    _HMAC_KEY_CACHE["current"] = base64.b64decode(
                        current_b64).decode("latin-1")
                    _HMAC_KEY_CACHE["previous"] = (
                        base64.b64decode(previous_b64).decode("latin-1")
                        if previous_b64 else None
                    )
                    _HMAC_CACHE_TS = time.monotonic()
                    loaded = True
    except Exception:
        pass

    if not loaded:
        # Re-read failed: keep last-good keys if available.
        if _HMAC_KEY_CACHE["current"] is not None:
            logger.warning(
                "app-session HMAC key: Vault re-read failed, using cached "
                "key (Vault may be sealed or unreachable)"
            )
            return _HMAC_KEY_CACHE["current"]
        # No prior load → fail closed.
        raise SessionHmacError(
            "app-session HMAC key: Vault is unreachable and no key has "
            "been loaded; cannot sign session tokens"
        )
    return _HMAC_KEY_CACHE["current"]


def _session_hmac_key_derived() -> str:
    """Derived-mode key: HKDF from AUDIT_HMAC_KEY.

    Same as the original behaviour — cached in-process, no Vault.
    """
    global _HMAC_KEY_CACHE

    if _HMAC_KEY_CACHE["current"] is not None:
        return _HMAC_KEY_CACHE["current"]

    _HMAC_KEY_CACHE["current"] = _derive_key()
    return _HMAC_KEY_CACHE["current"]


def _derive_key() -> str:
    """HKDF-SHA256 derivation of the session HMAC key from AUDIT_HMAC_KEY.

    Uses AUDIT_HMAC_KEY (an env secret already present in every process at
    import time, read by audit.py for CMMC AU-9 integrity).  The salt is a
    fixed domain-specific constant; the info label ties this derivation to
    this specific purpose.  Never logs key material.
    """
    # Lazy import so the module can still be parsed without cryptography
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    root = os.getenv("AUDIT_HMAC_KEY", "").encode()
    if not root or len(root) < 32:
        raise RuntimeError(
            "AUDIT_HMAC_KEY is not configured or is too short (<32 bytes); "
            "app forward-auth session cookies cannot function without it."
        )
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"mod-app-session-hmac-salt-v1",
        info=_HKDF_INFO,
    )
    derived = hkdf.derive(root)
    return derived.decode("latin-1")


def _jwt_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt_b64url_pad(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def mint_session_token(claims: Dict, ttl_seconds: int) -> str:
    """Sign the session-cookie JWT (HS256, typ=mod-app-session).

    ``claims`` carries app_id / sub / email / groups / user_type; iat+exp are
    added here. The value is deliberately compact — it rides in a cookie.

    BUG B86h: a token with no ``sub`` is unreadable — parse_session_token
    requires sub, so minting one produces a cookie the browser carries back and
    verify rejects, looping forever. Fail loud instead of minting a cookie that
    can never be read: the cause is the app's KC client missing the `basic`
    default client scope (Keycloak >= 25 delivers `sub` from it).
    """
    if not claims.get("sub"):
        raise ValueError(
            "KC token has no 'sub' — app client missing 'basic' scope")
    from jose import jwt
    body = dict(claims)
    now = int(time.time())
    body.setdefault("iat", now)
    body["typ"] = "mod-app-session"
    body["exp"] = now + int(ttl_seconds)
    return jwt.encode(body, _session_hmac_key(), algorithm="HS256")


def parse_session_token(token: str) -> Optional[Dict]:
    """Verify the session-cookie JWT. Returns the claims, or None when the
    token is missing/malformed/expired/unsigned-by-us.

    Raises :exc:`SessionHmacError` when vault mode cannot load a key at all
    (never caught here — a loud, named "key unavailable" state is distinct
    from "this token is invalid", and the caller turns it into a 503 rather
    than treating the request as a bad login)."""
    if not token:
        return None
    from jose import jwt
    try:
        keys = _session_hmac_verify_keys()
    except SessionHmacError:
        raise
    except Exception:
        return None
    claims = None
    for key in keys:
        try:
            # verify_aud off: this is a MOD-minted session token whose
            # security boundary is `typ` + `app_id` + the HMAC signature,
            # NOT a KC audience (a token that happens to carry an `aud`
            # claim must not be rejected for it — python-jose otherwise
            # refuses any aud it can't match).
            claims = jwt.decode(
                token, key, algorithms=["HS256"],
                options={"verify_aud": False})
            break
        except Exception:
            continue
    if claims is None:
        return None
    if claims.get("typ") != "mod-app-session":
        return None
    if not isinstance(claims.get("app_id"), int) or not claims.get("sub"):
        return None
    return claims


def mint_state_cookie(app_id: int, rd: str) -> str:
    """A signed, short-lived (5 min) HS256 JWT holding the PKCE verifier +
    state + the app_id this flow was started for.

    ``rd`` (the post-login destination) is verified RELATIVE-only before it
    is embedded — an external URL must never survive the round-trip. Signing
    it (not just random) means a forged/other-app cookie cannot be replayed
    into another app's callback (app_id pin, same as the session cookie).
    """
    from jose import jwt
    if not _RD_RE.match(rd or "/"):
        rd = "/"
    now = int(time.time())
    body = {
        "typ": "mod-app-auth-state",
        "app_id": int(app_id),
        "rd": rd,
        "verifier": secrets.token_urlsafe(48),
        "state": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + _STATE_TTL_SECONDS,
    }
    return jwt.encode(body, _session_hmac_key(), algorithm="HS256")


def parse_state_cookie(value: str, app_id: int) -> Optional[Dict]:
    """Verify the state cookie → {verifier, state, rd, app_id}, or None when
    it is missing/malformed/expired/unsigned-by-us/minted-for-another-app.

    Raises :exc:`SessionHmacError` when vault mode cannot load a key at all
    (same "key unavailable" distinction as :func:`parse_session_token`).
    """
    if not value:
        return None
    from jose import jwt
    try:
        keys = _session_hmac_verify_keys()
    except SessionHmacError:
        raise
    except Exception:
        return None
    claims = None
    for key in keys:
        try:
            claims = jwt.decode(value, key, algorithms=["HS256"])
            break
        except Exception:
            continue
    if claims is None:
        return None
    if claims.get("typ") != "mod-app-auth-state":
        return None
    if int(claims.get("app_id") or 0) != int(app_id):
        return None
    if not claims.get("verifier") or not claims.get("state"):
        return None
    return claims


def session_cookie_attrs(ttl_seconds: int) -> dict:
    """starlette ``response.set_cookie`` kwargs for the session cookie.

    Secure + no Domain + Path=/ + HttpOnly + SameSite=Lax: scoped to the
    requesting app host by construction (the __Host- name double-guarantees
    it in the browser).
    """
    return {
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "path": "/",
        "max_age": int(ttl_seconds),
    }


def state_cookie_attrs() -> dict:
    """Auth-code state cookie: short-lived, consumed at callback."""
    return {
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "path": "/",
        "max_age": 300,
    }


def host_cookie_clear_attrs() -> dict:
    """``response.delete_cookie`` kwargs for clearing a ``__Host-`` cookie.

    P0 example.com redirect-loop fix: a cookie deletion IS a
    Set-Cookie (``name=""; Max-Age=0``), and the ``__Host-`` prefix's Secure
    requirement is enforced on *every* Set-Cookie for that name — deletions
    included. Starlette's ``delete_cookie()`` defaults to ``secure=False,
    httponly=False`` when no kwargs are passed, so a bare
    ``resp.delete_cookie(SESSION_COOKIE)`` / ``resp.delete_cookie(STATE_COOKIE)``
    emits a Set-Cookie the browser is required to silently reject outright —
    the stale cookie is never actually cleared, it just sits there forever.
    Must match ``session_cookie_attrs``/``state_cookie_attrs`` (host-only: no
    ``domain``) so this clears the SAME cookie slot those mint.
    """
    return {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}


def _cookie_values(request, cookie_name: str) -> List[str]:
    """EVERY value the browser sent for ``cookie_name``.

    BUG.B86e: Starlette's ``request.cookies`` is a ``SimpleCookie`` and keeps
    only ONE value per name, so when the browser holds two same-named cookies
    — the current host-only ``__Host-`` cookie plus a stale variant stored
    under a parent ``Domain=`` or a different ``Path=`` (browsers key a cookie
    by name+domain+path, so both survive and BOTH are sent in one Cookie
    header) — only one of them reaches the API, and it is not necessarily the
    one just minted. The stale one then fails signature verification, which
    the verify endpoint reports as ``has_cookie=True ... claim_app_id=None``:
    the login loop. Both our ``__Host-`` cookies (session AND state) are the
    same name for every app — this generic-by-name helper is shared by both
    so neither one regresses to the single-value Starlette behaviour, and it
    applies identically no matter which app/tenant minted the cookie.

    Parsing the raw Cookie header keeps every value, so the caller can try
    each. Values are never logged or returned to the caller in any response.

    Capped at ``_MAX_SESSION_COOKIE_CANDIDATES``: a legitimate browser never
    sends more than 2-3 same-named variants, and an attacker-controlled
    Cookie header must not be able to force an unbounded number of HMAC
    verify attempts per request.
    """
    raw = request.headers.get("cookie", "") or ""
    values: List[str] = []
    for chunk in raw.split(";"):
        if len(values) >= _MAX_SESSION_COOKIE_CANDIDATES:
            break
        name, _, value = chunk.partition("=")
        if name.strip() == cookie_name:
            value = value.strip()
            if value:
                values.append(value)
    return values


def session_cookie_values(request) -> List[str]:
    """EVERY value the browser sent for the session-cookie name — see
    :func:`_cookie_values`."""
    return _cookie_values(request, SESSION_COOKIE)


def state_cookie_values(request) -> List[str]:
    """EVERY value the browser sent for the auth-state-cookie name — the
    same B86e shadowing class can hit the state cookie too (it is also an
    ``__Host-`` cookie with a long-lived name across deploys), so the
    callback leg must try each candidate rather than trust
    ``request.cookies`` for exactly the same reason the verify leg does.
    See :func:`_cookie_values`."""
    return _cookie_values(request, STATE_COOKIE)


def stale_session_cookie_clears(request) -> List[dict]:
    """set_cookie kwargs that expire the stale same-named cookie variants.

    The browser will keep a stale cookie until it is explicitly expired, and
    ``response.delete_cookie()`` only expires the host-only/``Path=/`` variant.
    Emit one clear per plausible storage shape: the host-only cookie and the
    parent-Domain cookie (e.g. ``.example.com``) at ``Path=/``.
    """
    host = (request.headers.get("x-forwarded-host")
           or request.headers.get("host") or "").split(":")[0].strip()
    # kwargs for starlette ``delete_cookie`` (which supplies Max-Age=0 itself).
    # Only the PARENT-Domain variant is emitted: the host-only Path=/ variant
    # IS the cookie the callback mints, so expiring it would undo the mint.
    attrs = {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}
    parts = host.split(".")
    if len(parts) < 2:
        return []
    return [{**attrs, "domain": "." + ".".join(parts[-2:])}]


def kc_authorize_url(client_id: str, redirect_uri: str, state: str,
                     code_challenge: str) -> str:
    """The realm's authorize endpoint for the app's PUBLIC client (PKCE S256 —
    no client_secret: the app client is public, so PKCE is the credential and
    the callback's code exchange needs no secret)."""
    from urllib.parse import urlencode
    base = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email groups",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{base}/realms/{realm}/protocol/openid-connect/auth?{urlencode(params)}"


def kc_token_url() -> str:
    """The realm's token endpoint (EXTERNAL url — the code exchange is done
    server-side; to_internal_url is applied where it is applied elsewhere)."""
    base = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    return f"{base}/realms/{realm}/protocol/openid-connect/token"


def kc_end_session_url() -> str:
    """The realm's end-session endpoint for the logout 302."""
    base = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    return f"{base}/realms/{realm}/protocol/openid-connect/logout"


def pkce_challenge(verifier: str) -> str:
    """S256 challenge for a PKCE verifier (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _jwt_b64url(digest)


def exchange_auth_code(code: str, redirect_uri: str, code_verifier: str,
                       client_id: str) -> Dict:
    """Exchange the authorization code for tokens (server-side PKCE exchange
    for the app's PUBLIC client — no client_secret needed).

    ``client_id`` is REQUIRED by the token endpoint for a public client: the
    browser leg authenticates as the client, so the POST must name it (BUG.B86b
    — omitting it made Keycloak log ``CODE_TO_TOKEN_ERROR clientId="null"`` and
    answer 400, which surfaced as a 401 on ``/_mod/auth/callback``). The code
    itself was minted for that client at /authorize, so a missing/foreign
    client_id is a rejected exchange, never a fallback.

    Returns the KC token response dict (access_token / id_token / ...).
    Raises ValueError on a non-2xx token response (bad/expired code).
    """
    import httpx
    from keycloak_auth import to_internal_url
    if not client_id:
        raise ValueError("code exchange missing client_id")
    # Every client this flow can name is a public PKCE client (per-app clients
    # and the platform fastapi-app client are both registered publicClient=true
    # with standardFlowEnabled=true; the confidential clients are
    # standardFlowEnabled=false service accounts, never a browser hop), so the
    # exchange names the client and carries NO secret.
    resp = httpx.post(
        to_internal_url(kc_token_url()),
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        raise ValueError(f"code exchange failed: HTTP {resp.status_code}")
    return resp.json()


def session_ttl_seconds() -> int:
    """The session-cookie TTL — platform setting (admin UI), not env.
    Default 28800 (8h); a mis-set value is clamped to [300, 7d]."""
    try:
        ttl = int(_psr.get_setting("apps.session_ttl_seconds"))
    except Exception:
        ttl = 28800
    return max(300, min(ttl, 7 * 24 * 3600))


register_settings()


# ============================================================================
# 30.6.4 c4a — signed X-Mod-Assertion for app hosts.
#
# The forward-auth verify endpoint mints an RS256 assertion (same manager
# keypair + kid machinery as utils/build_task_token.py — c5a 2627bf87b landed
# _kid()/_verify_key_for()/the JWKS current+previous list there; REUSE ONLY,
# never edited) that app hosts can verify independently. Lead vet (10-27) WINS
# over decomp/gx-c/c4-design.md wherever they differ: sub = the Keycloak
# subject, MOD's own user id goes in a separate mod_user_id claim; iss =
# "mod-forward-auth"; preferred_username is added; stable error codes.
#
# S1: tenant_id/app_id come ONLY from the DB app row (never claims/headers).
# S2: aud pinned per app.  S5: never logged, never in a JSON body, never
# stored.  S6: typ + iss pinned.  S7: <=8192 chars, <=64x128 groups, TTL
# clamped 30-300 on mint AND verify.
# ============================================================================

ASSERTION_HEADER = "X-Mod-Assertion"
ASSERTION_TYP = "mod-app-assertion-v1"
ASSERTION_ISS = "mod-forward-auth"
ASSERTION_TTL_SECONDS = 300
ASSERTION_MAX_CHARS = 8192
ASSERTION_MAX_GROUPS = 64
ASSERTION_MAX_GROUP_CHARS = 128
ASSERTION_REPLAY_WINDOW_SECONDS = 300


class AppAssertionError(Exception):
    """Stable-coded rejection for mint/verify of X-Mod-Assertion (30.6.4 c4a).

    Codes: assertion_invalid / assertion_aud_mismatch / assertion_typ_mismatch
    / assertion_expired. A tenant mismatch maps to assertion_invalid (S1) —
    no dedicated code is needed, so `assertion_tenant_mismatch` does not exist.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _mint_assertion(*, aud: str, subject_claims: dict, tenant_id: int,
                    user, claims, groups, user_type,
                    ttl_seconds: int = ASSERTION_TTL_SECONDS) -> str:
    """Shared RS256 mint for the X-Mod-Assertion families (30.6.4 c4a/c4d).

    `aud` is the caller's PIN (`app:<id>` / `stack:<id>`) and `subject_claims`
    the kind-specific id claim(s) (`app_id` / `stack_id`); `tenant_id` MUST come
    from the DB row the caller resolved, never from claims (S1). Key order here
    is the wire order python-jose serialises — the app wrapper's payload is
    byte-identical to c4a's. Raises AppAssertionError on an unusable sub or an
    oversized token; RuntimeError from _private_key() when the manager keypair
    is unset (the caller falls back, no header). Never logged (S5).
    """
    from jose import jwt
    from utils.build_task_token import _private_key, _public_key, _kid

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AppAssertionError("assertion_invalid")

    now = int(time.time())
    ttl = max(30, min(int(ttl_seconds), ASSERTION_TTL_SECONDS))
    truncated = [str(g)[:ASSERTION_MAX_GROUP_CHARS]
                 for g in list(groups or [])[:ASSERTION_MAX_GROUPS]]
    token = jwt.encode(
        {
            "iss": ASSERTION_ISS,
            "typ": ASSERTION_TYP,
            "aud": aud,
            "sub": sub,
            "mod_user_id": int(user.id) if user is not None else None,
            "preferred_username": ((user.username if user is not None
                                    else claims.get("preferred_username"))
                                   or claims.get("sub", "")),
            "email": ((user.email if user is not None
                       else claims.get("email")) or ""),
            "groups": truncated,
            "user_type": user_type,
            # kind pin: app_id for apps, stack_id for stacks.
            **{k: int(v) for k, v in subject_claims.items()},
            # S1: from the DB row, never from claims or headers.
            "tenant_id": int(tenant_id),
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        },
        _private_key(),
        algorithm="RS256",
        headers={"kid": _kid(_public_key())},
    )
    if len(token) > ASSERTION_MAX_CHARS:
        raise AppAssertionError("assertion_invalid")
    return token


def mint_app_assertion(*, app, user, claims, groups, user_type,
                      ttl_seconds: int = ASSERTION_TTL_SECONDS) -> str:
    """Mint the signed X-Mod-Assertion for an APP host (aud pinned app:<id>).

    Thin wrapper over _mint_assertion — claims, typ/iss/kid and the ttl clamp
    are unchanged from c4a (byte-identical output for apps).
    """
    return _mint_assertion(
        aud=f"app:{int(app.id)}",
        subject_claims={"app_id": int(app.id)},
        tenant_id=app.tenant_id,
        user=user, claims=claims, groups=groups, user_type=user_type,
        ttl_seconds=ttl_seconds)


_ASSERTION_REPLAY: Dict[str, int] = {}
_ASSERTION_REPLAY_LOCK = threading.Lock()


def verify_app_assertion(token, *, app_id, tenant_id, db=None,
                         check_replay: bool = True) -> Dict:
    """Verify an X-Mod-Assertion. Returns the claims dict or raises
    AppAssertionError with a stable code; never returns None, never raises
    anything else.

    aud is compared MANUALLY (decode with verify_aud=False) so the code
    mapping is never string-matched on a python-jose message.
    """
    try:
        return _verify_app_assertion_inner(token, app_id=app_id,
                                           tenant_id=tenant_id, db=db,
                                           check_replay=check_replay)
    except AppAssertionError:
        raise
    except Exception:
        # Any unexpected failure (bad types, keypair unset, ...) -> invalid.
        raise AppAssertionError("assertion_invalid")


def verify_stack_assertion(token, *, stack_id, tenant_id, db=None,
                           check_replay: bool = True) -> Dict:
    """Verify a stack-host X-Mod-Assertion (30.6.4 c4d).

    Mirrors verify_app_assertion but pins aud to `stack:<stack_id>` and
    requires the `stack_id` claim to match; an app assertion (aud=app:*) can
    never verify here and vice versa (S2). tenant_id comes from the stack's
    DB row, never from claims (S1). Same stable codes, same replay cache.
    """
    try:
        return _verify_assertion(token, aud=f"stack:{int(stack_id)}",
                                 subject_claims={"stack_id": int(stack_id)},
                                 tenant_id=tenant_id, db=db,
                                 check_replay=check_replay)
    except AppAssertionError:
        raise
    except Exception:
        # Any unexpected failure (bad types, keypair unset, ...) -> invalid.
        raise AppAssertionError("assertion_invalid")


def _verify_app_assertion_inner(token, *, app_id, tenant_id, db=None,
                                check_replay: bool = True) -> Dict:
    return _verify_assertion(token, aud=f"app:{int(app_id)}",
                             subject_claims={"app_id": int(app_id)},
                             tenant_id=tenant_id, db=db,
                             check_replay=check_replay)


def _verify_assertion(token, *, aud, subject_claims: dict, tenant_id,
                      db=None, check_replay: bool = True) -> Dict:
    """Shared verify core for the assertion families (30.6.4 c4a/c4d).

    `aud` is the caller's kind pin (`app:<id>` / `stack:<id>`); `subject_claims`
    are the kind id claims that must match (app_id / stack_id). There is no
    generic entry point that accepts either aud — mint/verify pairs are per
    kind. Returns the claims dict or raises AppAssertionError with a stable
    code; never returns None, never raises anything else.
    """
    from jose import jwt
    from jose.exceptions import (ExpiredSignatureError, JWTError,
                                 JWTClaimsError)
    from utils.build_task_token import _verify_key_for

    if not token or not isinstance(token, str) or len(token) > ASSERTION_MAX_CHARS:
        raise AppAssertionError("assertion_invalid")

    key = _verify_key_for(token, db=db)
    if key is None:
        # unknown kid -> reject, never fall back to the current key.
        raise AppAssertionError("assertion_invalid")

    try:
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            issuer=ASSERTION_ISS,
            options={"verify_aud": False, "verify_exp": True},
        )
    except ExpiredSignatureError:
        raise AppAssertionError("assertion_expired")
    except JWTClaimsError:
        # issuer mismatch (aud is verified manually below).
        raise AppAssertionError("assertion_invalid")
    except JWTError:
        raise AppAssertionError("assertion_invalid")

    # S6: typ pinned.
    if claims.get("typ") != ASSERTION_TYP:
        raise AppAssertionError("assertion_typ_mismatch")
    # S2: aud pinned per kind+id (manual compare -> stable code, no string
    # match on a python-jose message).
    if claims.get("aud") != aud:
        raise AppAssertionError("assertion_aud_mismatch")
    # S1: tenant pin from the DB row the caller resolved (A3 maps here to
    # assertion_invalid — no dedicated tenant code), plus the kind id claim.
    if int(claims.get("tenant_id") or -1) != int(tenant_id):
        raise AppAssertionError("assertion_invalid")
    for _k, _v in subject_claims.items():
        if int(claims.get(_k) or -1) != int(_v):
            raise AppAssertionError("assertion_invalid")
    # S7: TTL cap enforced on verify too.
    if int(claims.get("exp") or 0) - int(claims.get("iat") or 0) > ASSERTION_TTL_SECONDS:
        raise AppAssertionError("assertion_invalid")

    if check_replay:
        # Per-process replay cache; multi-replica replay protection is out of
        # scope (TTL <=300 s bounds the window). Bounded + fail-closed: never
        # evict live entries — when full after purging, reject.
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise AppAssertionError("assertion_invalid")
        now = int(time.time())
        with _ASSERTION_REPLAY_LOCK:
            for seen in [k for k, exp in _ASSERTION_REPLAY.items()
                         if exp <= now or exp - now > ASSERTION_REPLAY_WINDOW_SECONDS]:
                _ASSERTION_REPLAY.pop(seen, None)
            if jti in _ASSERTION_REPLAY:
                raise AppAssertionError("assertion_invalid")
            if len(_ASSERTION_REPLAY) >= 10000:
                raise AppAssertionError("assertion_invalid")
            _ASSERTION_REPLAY[jti] = int(claims["exp"])

    return claims
