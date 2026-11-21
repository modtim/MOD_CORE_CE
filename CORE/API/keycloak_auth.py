import os
import base64
import binascii
import httpx
import time
import logging
import threading
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
import json
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

# --- Setup logging ---
from utils.log_config import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

# --- Configuration ---
KEYCLOAK_INTERNAL_URL = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
KEYCLOAK_EXTERNAL_URL = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_FASTAPI_CLIENT_ID", "fastapi-app")
# BUG B86h: on Keycloak >= 25 the `sub` claim comes from this DEFAULT client
# scope. An app client without it produces tokens with no `sub`, which breaks
# the app session cookie (see services/app_forward_auth.parse_session_token).
BASIC_CLIENT_SCOPE = "basic"
# Per-process record of app clients already verified to carry `basic`, so the
# self-heal path hits Keycloak at most once per client per process.
_BASIC_SCOPE_CHECKED: set = set()
WELL_KNOWN_URL = f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"


# --- Secret Reading Function ---
def get_fastapi_client_secret():
    secret_path = "/run/secrets/keycloak_fastapi_client_secret.env"
    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            content = f.read().strip()
            if '=' in content:
                return content.split("=")[1]
            return content
    return os.getenv("KEYCLOAK_FASTAPI_CLIENT_SECRET")


def get_fastapi_admin_secret():
    """Get admin service client secret for KeycloakAdmin operations."""
    secret_path = "/run/secrets/keycloak_fastapi_admin_secret.env"
    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            content = f.read().strip()
            if '=' in content:
                return content.split("=")[1]
            return content
    return os.getenv("KEYCLOAK_FASTAPI_ADMIN_SECRET")


def get_dashboard_client_secret():
    """Confidential dashboard-client secret (Option C: PKCE + client_secret)."""
    secret_path = "/run/secrets/keycloak_dashboard_client_secret.env"
    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            content = f.read().strip()
            if '=' in content:
                return content.split("=")[1]
            return content
    return os.getenv("KEYCLOAK_DASHBOARD_CLIENT_SECRET")


bearer_scheme = HTTPBearer()


# --- Robust OIDC and JWKS Fetching with TTL refresh ---
# The old forever-lru_cache here was the second half of the 2026-10-04
# login outage: after a Keycloak restart, keycloak-init.sh re-asserted the
# realm's `frontendUrl`, but this API kept validating tokens against the
# cached (stale) issuer until the container was restarted. Config/JWKS are
# therefore cached for OIDC_CONFIG_TTL_SECONDS and re-fetched after expiry;
# get_current_user_claims() additionally force-refreshes once when token
# validation fails (issuer/signature mismatch against stale cache) — but
# expired/audience-mismatched tokens 401 without any fetch, and the forced
# refresh is rate-limited by FORCED_REFRESH_COOLDOWN_SECONDS.
OIDC_CONFIG_TTL_SECONDS = 300

_oidc_config_lock = threading.Lock()
_oidc_config_cache = {"value": None, "expires_at": 0.0}
_jwks_lock = threading.Lock()
_jwks_cache = {"value": None, "expires_at": 0.0}
# When the JWKS cache was last REFRESHED (monotonic; 0.0 = never fetched).
# Only the serve-stale fallback arms below are bounded by this; the normal
# TTL path (OIDC_CONFIG_TTL_SECONDS) is unchanged.
_jwks_fetched_at = 0.0

# Serve-stale bound: while the certs endpoint is down we keep handing the
# previously-cached keys to requests, but only for this long. Past it the
# fallback arms 401 instead (fail-closed) — a rotated-out/revoked key must
# not validate forever. The TTL path is untouched.
JWKS_MAX_STALE_SECONDS = 3600

# A failed token validation must not force a synchronous OIDC+JWKS
# re-fetch on every request: expired tokens are routine (WB polls just
# before its refresh) and garbage tokens are free for an attacker to
# send. At most one forced refresh per process per this cooldown.
FORCED_REFRESH_COOLDOWN_SECONDS = 30
_forced_refresh_lock = threading.Lock()
_forced_refresh_state = {"last": 0.0}


def _claim_forced_refresh() -> bool:
    """Allow a forced OIDC/JWKS refresh, rate-limited by
    FORCED_REFRESH_COOLDOWN_SECONDS (monotonic, lock-guarded)."""
    with _forced_refresh_lock:
        now = time.monotonic()
        if now - _forced_refresh_state["last"] < FORCED_REFRESH_COOLDOWN_SECONDS:
            return False
        _forced_refresh_state["last"] = now
        return True


def _oidc_candidate_urls() -> list:
    """Discovery-doc candidates: the internal alias first, the externally
    configured origin second (deduped). Same realm, two ways to dial it.

    A dind/netns room whose Keycloak alias died (stack torn down + re-pinned
    while an API surface kept serving) resolves NEITHER at boot, but a room
    where only the alias is dead (published-port KC, orphaned upstream netns)
    resolves the external origin — which is exactly the case where the old
    single-candidate fetch left the API serving pre-rotation keys forever.
    """
    urls = [WELL_KNOWN_URL]
    external = KEYCLOAK_EXTERNAL_URL.rstrip("/") + f"/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"
    if external != WELL_KNOWN_URL:
        urls.append(external)
    return urls


def _fetch_oidc_config():
    retries = 10
    last_err = None
    for i in range(retries):
        for url in _oidc_candidate_urls():
            try:
                if is_debug_mode():
                    logger.info(f"Attempting to fetch OIDC config from: {url}")
                response = httpx.get(url, timeout=5)
                response.raise_for_status()
                if is_debug_mode():
                    logger.info("--> Successfully fetched OIDC config from Keycloak.")
                return response.json()
            except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e:
                last_err = e
                if is_debug_mode():
                    logger.warning(f"--> Could not fetch OIDC config on attempt {i + 1}/{retries}. Error: {e}")
                else:
                    logger.warning(f"--> Could not fetch OIDC config on attempt {i + 1}/{retries}")
        if i < retries - 1:
            time.sleep(5)
        else:
            logger.critical(f"FATAL: Could not fetch OIDC config after {retries} attempts.")
            raise RuntimeError("Could not initialize authentication service: OIDC config fetch failed.") from last_err
    raise RuntimeError("Failed to get OIDC config after all retries.")


def get_oidc_config(force: bool = False) -> dict:
    """OIDC discovery document, cached for OIDC_CONFIG_TTL_SECONDS.

    force=True bypasses the cache (used for the one-shot refresh when
    token validation fails against a stale issuer/JWKS).
    """
    with _oidc_config_lock:
        cached = _oidc_config_cache["value"]
        fresh = cached is not None and time.monotonic() < _oidc_config_cache["expires_at"]
    if not force and fresh:
        return cached
    value = _fetch_oidc_config()
    with _oidc_config_lock:
        _oidc_config_cache["value"] = value
        _oidc_config_cache["expires_at"] = time.monotonic() + OIDC_CONFIG_TTL_SECONDS
    return value


# --- Issuer consistency for internal Keycloak calls (J159b) ---------------
# Keycloak derives the token ISSUER from the request's host (KC_PROXY_HEADERS
# = xforwarded). The browser talks to the public host (KEYCLOAK_EXTERNAL_URL,
# e.g. http://<lan-ip>:8081) so minted tokens carry that iss; the BFF posts to
# KEYCLOAK_INTERNAL_URL (keycloak:8080) with no forwarded headers, so Keycloak
# derives a DIFFERENT issuer there. The authorization_code grant mints fine
# (issuer comes from KC_HOSTNAME_URL) but the refresh grant VALIDATES the
# refresh token's iss against the request-derived issuer, so every refresh
# fails with HTTP 400 invalid_grant "Invalid token issuer. Expected
# 'http://<lan-ip>:8080/realms/modtex'" — which the BFF maps to 401 and the
# Dashboard treats as a dead session (Tim's Dell: logged out ~10 min after
# login, console "Token refresh failed: 401"). Fix: forward the public host
# on the internal token calls so both sides derive the SAME issuer.
def _forwarded_host_headers() -> dict:
    ext = urlsplit(KEYCLOAK_EXTERNAL_URL)
    if not ext.netloc:
        return {}
    return {"X-Forwarded-Host": ext.netloc, "X-Forwarded-Proto": ext.scheme or "http"}


def to_internal_url(advertised_url: str) -> str:
    """Rewrite an endpoint Keycloak advertises so it is dialled internally.

    Keeps the advertised path and query, replaces scheme+host with
    KEYCLOAK_INTERNAL_URL. Substring-replacing KEYCLOAK_EXTERNAL_URL silently
    no-ops whenever the realm advertises a THIRD hostname — `KC_HOSTNAME` or a
    realm's `frontendUrl` attribute both do exactly that — which left the API
    posting to itself and reading its own 404 as a Keycloak rejection.
    """
    advertised = urlsplit(advertised_url)
    internal = urlsplit(KEYCLOAK_INTERNAL_URL)
    return urlunsplit((
        internal.scheme,
        internal.netloc,
        advertised.path,
        advertised.query,
        "",
    ))


def _configured_certs_urls(advertised_jwks_uri: str) -> list:
    """The ONLY URLs the realm JWKS is ever dialled: the configured Keycloak
    origins (internal alias first, external second, deduped) with the
    configured realm's certs path and NO query string. Built from settings
    alone — the discovery document contributes nothing, so a hostile/tenant-
    customised doc cannot name a host, path or query string we dial (dialling
    it verbatim was an SSRF primitive and a key-confusion vector: its key set
    got cached as the realm's keys).

    `advertised_jwks_uri` is never dialled and never used to build a URL; it
    only says WHETHER a second (configured-external) origin is warranted: when
    the doc already names the internal alias, the single internal candidate is
    all we dial, which keeps the one-fetch bound of the TTL re-fetch exactly.
    """
    path = f"/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
    urls = []
    for configured in (KEYCLOAK_INTERNAL_URL, KEYCLOAK_EXTERNAL_URL):
        origin = urlsplit(configured)
        url = urlunsplit((origin.scheme, origin.netloc, path, "", ""))
        if url not in urls:
            urls.append(url)
    internal = urlsplit(KEYCLOAK_INTERNAL_URL)
    advertised = urlsplit(advertised_jwks_uri) if advertised_jwks_uri else None
    if advertised and (advertised.scheme, advertised.netloc) == (internal.scheme, internal.netloc):
        return urls[:1]
    return urls


def _jwks_fetch(jwks_uri: str) -> Optional[dict]:
    """Fetch the realm JWKS over the CONFIGURED origins only (at most 2 GETs
    per refetch, each with timeout=5; a refetch itself happens at most once
    per OIDC_CONFIG_TTL_SECONDS or once per FORCED_REFRESH_COOLDOWN_SECONDS —
    so a forged-kid flood still cannot produce a fetch storm).

    `jwks_uri` (what the discovery doc advertises) is NEVER dialled and never
    used to build a URL: it is compared only to log a PATH mismatch (path
    component, truncated, no host, no query) so an unexpected realm config is
    visible in logs without ever becoming a request. The internal alias alone
    is not enough — a room whose alias does not resolve (dind/netns boot,
    orphaned upstream netns) dies on ConnectError and keeps serving
    pre-rotation keys forever; the configured external origin is the second
    candidate. Nothing here weakens validation: the token is still signature-
    verified against whichever key set we actually fetched, and an unknown kid
    is never accepted.
    """
    candidates = _configured_certs_urls(jwks_uri)
    advertised_path = urlsplit(jwks_uri).path if jwks_uri else ""
    if advertised_path and advertised_path != urlsplit(candidates[0]).path:
        logger.warning(
            "discovery doc advertises a JWKS path (%.64s) different from the configured realm path; "
            "only the configured URLs are dialled", advertised_path)
    last_err = None
    for url in candidates:
        try:
            response = httpx.get(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning("JWKS fetch failed for candidate (%s); trying next candidate if any", type(e).__name__)
    logger.warning("JWKS unreachable on every candidate; keeping previously cached keys")
    return None


def get_jwks(force: bool = False) -> dict:
    """Realm JWKS, cached for OIDC_CONFIG_TTL_SECONDS.

    force=True bypasses the cache (see get_oidc_config).
    """
    global _jwks_fetched_at
    with _jwks_lock:
        cached = _jwks_cache["value"]
        fresh = cached is not None and time.monotonic() < _jwks_cache["expires_at"]
    if not force and fresh:
        return cached
    oidc_config = get_oidc_config()
    jwks_uri = oidc_config.get("jwks_uri")
    if not jwks_uri:
        raise RuntimeError("jwks_uri not found in OIDC configuration.")

    value = _jwks_fetch(jwks_uri)
    if value is None:
        raise RuntimeError("Could not fetch the realm JWKS.")
    with _jwks_lock:
        _jwks_cache["value"] = value
        _jwks_cache["expires_at"] = time.monotonic() + OIDC_CONFIG_TTL_SECONDS
        _jwks_fetched_at = time.monotonic()
    return value


# --- User Authentication Dependency ---
def _expected_issuers() -> list:
    """BUG 1816b — the realm's issuer is NOT request-stable on this stack.

    The browser reaches Keycloak through the nginx auth vhost, which forwards
    X-Forwarded-Proto/Host, so KC (KC_PROXY_HEADERS=xforwarded) mints tokens
    with the PUBLIC issuer (https://<kc host>/realms/<realm>). The API's OIDC
    discovery runs over KEYCLOAK_INTERNAL_URL, where KC advertises its own
    http/:8080 view of the same KC_HOSTNAME — a DIFFERENT issuer string.
    python-jose then rejects every freshly-minted token on the issuer check
    (``/v1/me`` 401 immediately after a successful ``/v1/auth/exchange-code``
    = the "logged out right after logging in" loop), and the forced OIDC/JWKS
    refresh cannot help because the realm genuinely advertises both.

    Accept EITHER spelling: the internally-advertised issuer plus the
    canonical public one derived from KEYCLOAK_EXTERNAL_URL. Both are the
    same realm; the audience + JWKS signature + azp pins still do the real
    validation.
    """
    advertised = get_oidc_config().get("issuer")
    public = (KEYCLOAK_EXTERNAL_URL.rstrip("/") + f"/realms/{KEYCLOAK_REALM}")
    return [advertised, public] if advertised and advertised != public else [public]


class UnknownKidError(JWTError):
    """The token's kid is absent from our cached JWKS — the cache may predate
    a keystore rotation. Subclasses JWTError so every existing handler still
    catches it; the detail text stays exactly "Public key not found.".
    """


def get_current_user_claims(token: str) -> dict:
    """
    Validates a JWT token and returns the claims.
    Now accepts a plain string token (from OAuth2AuthorizationCodeBearer scheme).
    """
    def _validate(jwks: dict) -> dict:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise JWTError("Missing 'kid' in token header.")
        rsa_key = {}
        for key in jwks["keys"]:
            if key["kid"] == kid:
                rsa_key = {"kty": key["kty"], "kid": key["kid"], "use": key["use"], "n": key["n"], "e": key["e"]}
                break

        if not rsa_key:
            # Unknown kid = the cached JWKS predates a keystore change. Log the
            # kid PREFIX only (<=8 chars): never the token, never key material.
            logger.warning("token kid %.8s not in cached JWKS; one rate-limited refetch may follow", kid[:8])
            raise UnknownKidError("Public key not found.")
        return jwt.decode(
            token, rsa_key, algorithms=["RS256"],
            audience=KEYCLOAK_AUDIENCE, issuer=_expected_issuers()
        )

    def _unauthorized(err: JWTError) -> HTTPException:
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                              detail=f"Could not validate credentials: {err}",
                              headers={"WWW-Authenticate": "Bearer"})

    def _jwks_snapshot() -> Optional[dict]:
        """Cached JWKS without any outbound fetch (None when never fetched,
        and None once the cache is older than JWKS_MAX_STALE_SECONDS — the
        serve-stale window is over, so every fallback arm here fails closed.
        The normal TTL path (get_jwks) is untouched by this bound.)"""
        with _jwks_lock:
            if _jwks_cache["value"] is None:
                return None
            if time.monotonic() - _jwks_fetched_at > JWKS_MAX_STALE_SECONDS:
                return None
            return _jwks_cache["value"]

    # The JWKS object this request already validated against. Identity
    # compare only (no deep compare, no fetch): a different object means the
    # cache was refreshed by someone else while this request was in flight.
    tried = None

    try:
        tried = get_jwks()
        return _validate(tried)
    except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
        # The cold-start / TTL re-fetch itself failed. Serve the previously
        # cached keys (never cleared to empty on an error) so a kid that IS
        # still published keeps validating — but only inside
        # JWKS_MAX_STALE_SECONDS; past that _jwks_snapshot() is None and we
        # 401 with the same detail the kid-miss path gives (fail-closed).
        logger.warning("JWKS fetch failed (%s); serving cached keys if any", type(exc).__name__)
        cached = _jwks_snapshot()
        if cached is None:
            raise _unauthorized(UnknownKidError("Public key not found."))
        try:
            return _validate(cached)
        except JWTError as e:
            raise _unauthorized(e)
    except JWTError as e:

        # Expired tokens (routine: clients poll just before their refresh)
        # and audience-mismatched tokens can never be fixed by re-fetching
        # OIDC config / JWKS — 401 straight away, no outbound fetches.
        if isinstance(e, ExpiredSignatureError) or (
            isinstance(e, JWTClaimsError) and "audience" in str(e).lower()
        ):
            raise _unauthorized(e)
        # Issuer/signature/unknown-key validation failed — that can mean
        # our cached OIDC config / JWKS are stale (e.g. the realm's keystore
        # rotated, so the token's kid is not in the cached JWKS). Force-refresh
        # both caches ONCE and retry exactly one time; the old forever-lru_cache
        # made recovery require an API restart. The refresh itself is
        # rate-limited by _claim_forced_refresh(): at most one refetch per
        # FORCED_REFRESH_COOLDOWN_SECONDS GLOBALLY, so a forged-kid flood can't
        # turn into a fetch storm — inside the cooldown an unknown kid 401s
        # immediately, with no outbound call.
        if not _claim_forced_refresh():
            # Burst recovery: Keycloak rotated its key, the FIRST kid-miss
            # spent the one cooldown slot and refetched, so the cache NOW
            # holds the new keys while the rest of the burst still 401s.
            # Re-validate ONCE against the current cached JWKS — but only if
            # it is a different object than the one this request already
            # tried (identity compare: no deep compare, no outbound fetch, so
            # the 1-fetch-per-cooldown bound and the kid-gated lookup are
            # unchanged; _jwks_snapshot() is None past JWKS_MAX_STALE_SECONDS,
            # so a stale cache never rescues a request).
            refreshed = _jwks_snapshot()
            if refreshed is not None and refreshed is not tried:
                try:
                    return _validate(refreshed)
                except (JWTError, KeyError):
                    # KeyError = malformed cached payload (pre-existing quirk);
                    # still fail closed — fall through to the unchanged 401.
                    pass
            raise _unauthorized(e)
        try:
            get_oidc_config(force=True)
            return _validate(get_jwks(force=True))
        except (RuntimeError, httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            # The refetch itself failed: get_jwks caches nothing on failure, so
            # the OLD keys are still what we serve, and this token 401s with the
            # unchanged detail. Never clear the cache to empty here. Later
            # requests may still serve those keys, but only while
            # time.monotonic() - _jwks_fetched_at <= JWKS_MAX_STALE_SECONDS —
            # past that window _jwks_snapshot() is None and every fallback arm
            # 401s (fail-closed); the TTL path just keeps re-fetching.
            logger.warning("forced JWKS refetch failed (%s); keeping previously cached keys", type(exc).__name__)
            raise _unauthorized(e)
        except JWTError as e2:
            raise _unauthorized(e2)


def _client_secret_for(client_id: str) -> Optional[str]:
    """
    Map a Keycloak client_id to the server-held secret. Used by the BFF so
    the browser never carries a confidential-client secret itself.
    Returns None for unknown client_ids (caller decides whether that's fatal).
    """
    if client_id == "dashboard-client":
        return get_dashboard_client_secret()
    if client_id == KEYCLOAK_AUDIENCE:  # fastapi-app
        return get_fastapi_client_secret()
    return None


# --- Refresh-token client binding (J159) ------------------------------------
# Keycloak binds every refresh token to the client that ISSUED it. Refreshing
# a workflow-builder-client token under dashboard-client is rejected with
# HTTP 400 invalid_grant "Token client and authorized client don't match",
# which is what the rc20e CE room logs every ~15 s: the WorkflowBuilder hook
# mints via the public workflow-builder-client (PKCE) but its inline refresh
# posts client_id="dashboard-client" to the BFF.
#
# The azp claim below is read WITHOUT signature verification, and ONLY to
# choose a client_id from a strict first-party allowlist. Keycloak still
# authenticates the refresh token itself (client_secret for confidential
# clients, the token value for public ones), so a forged azp can at worst
# redirect the refresh to another first-party client of THIS realm — which
# Keycloak then rejects outright when it does not own the token. An azp
# outside the allowlist never reaches the wire: we fall back to the default.
# Nothing about the token is ever logged.
_FIRST_PARTY_CLIENTS = frozenset(filter(None, (
    KEYCLOAK_AUDIENCE,
    os.getenv("KEYCLOAK_DASHBOARD_CLIENT_ID", "dashboard-client"),
    os.getenv("KEYCLOAK_WORKFLOW_CLIENT_ID", "workflow-builder-client"),
    os.getenv("MOBILE_OAUTH_CLIENT_ID", "mod-mobile"),
)))
# First-party clients Keycloak provisions as PUBLIC (no server-side secret),
# so a refresh for them must not carry a client_secret.
_PUBLIC_FIRST_PARTY_CLIENTS = frozenset(filter(None, (
    os.getenv("KEYCLOAK_WORKFLOW_CLIENT_ID", "workflow-builder-client"),
    os.getenv("MOBILE_OAUTH_CLIENT_ID", "mod-mobile"),
)))


# Upper bound on the base64url payload segment we are willing to decode when
# sniffing azp. Our own refresh-token payloads are a few hundred bytes (sub,
# azp, scope, exp, iat, jti), so 8 KiB is generous headroom while keeping json
# recursion and allocation bounded on attacker-supplied strings.
MAX_REFRESH_PAYLOAD_SEGMENT_BYTES = 8192


def _token_audience_client(refresh_token: str) -> Optional[str]:
    """Unverified azp of a refresh token, accepted only if first-party.

    Returns None for opaque (non-JWT) refresh tokens, malformed tokens, and
    any azp outside _FIRST_PARTY_CLIENTS — callers then keep the default.
    Never logs or returns token contents.
    """
    if not isinstance(refresh_token, str):
        return None
    parts = refresh_token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    # Real refresh-token payloads are a few hundred bytes; anything larger is
    # not one of our tokens. Cap BEFORE decoding so a hostile segment cannot
    # blow up json.loads (deep nesting → RecursionError, huge input → MemoryError).
    if len(segment) > MAX_REFRESH_PAYLOAD_SEGMENT_BYTES:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, binascii.Error, TypeError, RecursionError, MemoryError):
        return None
    if not isinstance(claims, dict):
        return None
    azp = claims.get("azp")
    if isinstance(azp, str) and azp in _FIRST_PARTY_CLIENTS:
        return azp
    return None


def exchange_code_for_token(code: str, redirect_uri: str, code_verifier: str = None, client_id: str = None) -> dict:
    """
    Exchange authorization code for access token via the BFF.

    Option C flow (dashboard-client): always sends BOTH client_secret and
    code_verifier when both are available — the secret is required because
    the client is confidential, the verifier is PKCE defence-in-depth.
    """
    token_endpoint = get_oidc_config()["token_endpoint"]
    internal_token_url = to_internal_url(token_endpoint)

    effective_client_id = client_id if client_id else KEYCLOAK_AUDIENCE

    token_data = {
        "grant_type": "authorization_code",
        "client_id": effective_client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    if code_verifier:
        token_data["code_verifier"] = code_verifier

    client_secret = _client_secret_for(effective_client_id)
    if client_secret:
        token_data["client_secret"] = client_secret
    elif not code_verifier:
        # Client is confidential per KC but we have neither secret nor verifier.
        logger.error("No client_secret or code_verifier for client_id=%s", effective_client_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OAuth misconfiguration: missing credentials for token exchange.",
        )

    response = httpx.post(internal_token_url, data=token_data, headers=_forwarded_host_headers())
    if response.status_code >= 400:
        if response.status_code < 500:
            # Bad/expired/used code, redirect mismatch, etc. — client error.
            logger.warning("Code exchange rejected by Keycloak: HTTP %d %s",
                           response.status_code, response.text[:200])
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization code is invalid, expired, or already used.",
            )
        response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str, client_id: str = "dashboard-client") -> dict:
    """
    Refresh an access token via the BFF. The client is the one that MINTED
    the refresh token (see _token_audience_client), falling back to the
    caller's client_id. Confidential first-party clients (dashboard-client,
    fastapi-app — Option C) always carry the server-held client_secret, which
    is why the browser cannot call Keycloak directly; public first-party
    clients (workflow-builder-client, mod-mobile) carry NO secret, since Keycloak
    rejects one for them and authenticates the refresh token itself.
    """
    token_endpoint = get_oidc_config()["token_endpoint"]
    internal_token_url = to_internal_url(token_endpoint)

    effective_client_id = _token_audience_client(refresh_token) or client_id

    token_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": effective_client_id,
    }
    if effective_client_id in _PUBLIC_FIRST_PARTY_CLIENTS:
        # Public client: Keycloak authenticates the refresh token itself and
        # rejects any client_secret. Secret handling for confidential clients
        # is unchanged below.
        pass
    else:
        client_secret = _client_secret_for(effective_client_id)
        if not client_secret:
            logger.error("No client_secret available for refresh with client_id=%s", effective_client_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OAuth misconfiguration: refresh token secret unavailable.",
            )
        token_data["client_secret"] = client_secret

    response = httpx.post(
        internal_token_url,
        data=token_data,
        headers=_forwarded_host_headers(),
        timeout=10,
    )
    if response.status_code >= 400:
        if response.status_code < 500:
            logger.warning("Token refresh rejected by Keycloak: HTTP %d %s",
                           response.status_code, response.text[:200])
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token is invalid or expired.",
            )
        response.raise_for_status()
    try:
        token_data = response.json()
    except Exception:
        logger.warning("Token refresh returned a non-JSON body from Keycloak")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token refresh returned an invalid (non-JSON) response.",
        )
    if not token_data.get("access_token"):
        logger.warning("Token refresh returned no access_token from Keycloak")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token refresh returned an incomplete token response (no access_token).",
        )
    # Keycloak honors the *original* grant's scope on refresh: a refresh
    # token minted without the `openid` scope comes back as HTTP 200 with
    # NO `id_token`. The BFF contract (routers/auth.py `Token`
    # response_model) requires an `id_token`, so a raw return here would
    # crash in FastAPI response validation as a 500. Surface it as a clean
    # 400 the client can act on (re-authenticate to mint a fresh pair).
    if not token_data.get("id_token"):
        logger.warning(
            "Token refresh returned no id_token (refresh token lacks the 'openid' scope)"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Refresh token was issued without the 'openid' scope, so "
                "Keycloak returned no id_token. Sign in again to obtain a "
                "fresh token pair."
            ),
        )
    return token_data


# --- OAuth 2.0 Device Authorization Grant BFF (mobile-runtime-enrollment P3) ---
# The confidential client_secret (if any) stays server-side, same as the
# authorization-code exchange. A public mobile client has no secret; the device
# grant then relies only on the client_id (device flow needs no PKCE).

def device_authorization_start(client_id: Optional[str] = None) -> dict:
    """Begin the device grant: returns device_code/user_code/verification_uri(_complete)."""
    cfg = get_oidc_config()
    endpoint = cfg.get("device_authorization_endpoint")
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Identity provider does not advertise a device authorization endpoint.",
        )
    internal = to_internal_url(endpoint)
    effective_client_id = client_id or KEYCLOAK_AUDIENCE
    data = {"client_id": effective_client_id, "scope": "openid profile email"}
    secret = _client_secret_for(effective_client_id)
    if secret:
        data["client_secret"] = secret
    resp = httpx.post(internal, data=data, timeout=10)
    if resp.status_code >= 400:
        logger.warning("Device authorization start rejected: HTTP %d %s",
                       resp.status_code, resp.text[:200])
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Could not start device authorization.")
    return resp.json()


def device_token_poll(device_code: str, client_id: Optional[str] = None) -> dict:
    """Poll the device grant. Returns {"status": "complete", **tokens} on success,
    {"status": "pending"|"slow_down"} while waiting, or raises on a terminal error."""
    cfg = get_oidc_config()
    internal = to_internal_url(cfg["token_endpoint"])
    effective_client_id = client_id or KEYCLOAK_AUDIENCE
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": effective_client_id,
    }
    secret = _client_secret_for(effective_client_id)
    if secret:
        data["client_secret"] = secret
    resp = httpx.post(internal, data=data, timeout=10, headers=_forwarded_host_headers())
    if resp.status_code == 200:
        return {"status": "complete", **resp.json()}
    body = {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        pass
    err = body.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"status": "authorization_pending" if err == "authorization_pending" else "slow_down"}
    # expired_token / access_denied / invalid_grant → terminal
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=body.get("error_description") or err or "Device authorization failed.",
    )


# --- Keycloak Admin Client Class ---
class KeycloakAdmin:
    def __init__(self):
        # Use dedicated admin service client with service account enabled
        self.client_id = "fastapi-admin-service"
        self.client_secret = get_fastapi_admin_secret()
        self.token_url = f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
        self.admin_api_url = f"{KEYCLOAK_INTERNAL_URL}/admin/realms/{KEYCLOAK_REALM}"
        self._token = None
        self._token_expires = 0

    def _get_token(self):
        if self._token and time.time() < self._token_expires:
            return self._token
        response = httpx.post(
            self.token_url,
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 300) - 60
        return self._token

    def create_user(self, user_data: dict):
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.post(f"{self.admin_api_url}/users", headers=headers, json=user_data)
        response.raise_for_status()
        location_header = response.headers.get("Location")
        if not location_header:
            raise Exception("Keycloak did not return user location header.")
        return location_header.split("/")[-1]

    def get_users_by_email(self, email: str) -> list:
        """Look up Keycloak users by email. Returns list of user dicts
        (each with 'id', 'username', 'email', 'firstName', 'lastName')."""
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = httpx.get(
            f"{self.admin_api_url}/users",
            headers=headers,
            params={"email": email},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def assign_user_to_group(self, keycloak_user_id: str, group_name: str) -> bool:
        """
        Assigns a user to a Keycloak group by name.
        Returns True if successful, False otherwise.
        """
        try:
            token = self._get_token()
            headers = {"Authorization": f"Bearer {token}"}

            # Find the group ID by name
            response = httpx.get(
                f"{self.admin_api_url}/groups",
                headers=headers,
                params={"search": group_name},
                timeout=10
            )
            response.raise_for_status()
            groups = response.json()

            # Find exact match
            group = next((g for g in groups if g["name"] == group_name), None)
            if not group:
                logger.error(f"Group '{group_name}' not found in Keycloak")
                return False

            group_id = group["id"]
            logger.info(f"Found group '{group_name}' with ID: {group_id}")

            # Assign user to group
            response = httpx.put(
                f"{self.admin_api_url}/users/{keycloak_user_id}/groups/{group_id}",
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"✅ Assigned user {keycloak_user_id} to group '{group_name}'")
            return True

        except Exception as e:
            logger.error(f"Failed to assign user to group '{group_name}': {e}")
            return False

    def delete_user(self, user_id: str):
        """
        Deletes a user from Keycloak by their user ID.
        Raises an exception if the deletion fails.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.delete(
            f"{self.admin_api_url}/users/{user_id}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"✅ Deleted user {user_id} from Keycloak")

    def disable_user(self, user_id: str):
        """
        Disables (enables=false) a Keycloak user by their user ID.
        Raises an exception if the update fails.
        ISO no-hard-delete: an orphaned account is disabled, never removed.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.put(
            f"{self.admin_api_url}/users/{user_id}",
            headers=headers,
            json={"enabled": False},
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"✅ Disabled user {user_id} in Keycloak")

    def create_service_account_client(self, client_id: str, description: str, user_id: int, tenant_id: int) -> dict:
        """
        Creates a Keycloak client with service account enabled for API key authentication.
        Returns dict with client_id and client_secret.

        CMMC/ISO 27001 Compliance:
        - Service accounts follow least privilege principle
        - Tokens are short-lived (configured in Keycloak)
        - Full audit trail via Keycloak events
        """
        import secrets as secrets_module

        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Generate a secure client secret
        client_secret = secrets_module.token_urlsafe(32)

        # Create the client configuration
        client_config = {
            "clientId": client_id,
            "name": f"API Key: {description}",
            "description": f"Service account for API key. User ID: {user_id}, Tenant ID: {tenant_id}",
            "enabled": True,
            "clientAuthenticatorType": "client-secret",
            "secret": client_secret,
            "serviceAccountsEnabled": True,  # This is key - enables client credentials flow
            "standardFlowEnabled": False,  # No browser login
            "directAccessGrantsEnabled": False,  # No password grant
            "publicClient": False,  # Confidential client
            "protocol": "openid-connect",
            "attributes": {
                "api_key_user_id": str(user_id),
                "api_key_tenant_id": str(tenant_id),
                # Token lifespan - 1 hour access token
                "access.token.lifespan": "3600",
            },
            # Default scopes
            "defaultClientScopes": ["openid", "profile", "email"],
            # Protocol mappers to include fastapi-app in audience
            # This is CRITICAL - without this, API key tokens will fail audience validation
            "protocolMappers": [
                {
                    "name": "fastapi-app-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "consentRequired": False,
                    "config": {
                        "included.client.audience": KEYCLOAK_AUDIENCE,  # "fastapi-app"
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "lightweight.claim": "false",
                        "introspection.token.claim": "true",
                    }
                }
            ],
        }

        response = httpx.post(
            f"{self.admin_api_url}/clients",
            headers=headers,
            json=client_config,
            timeout=30
        )

        # Idempotent (same shape as ensure_mobile_client / ensure_app_client):
        # a clientId collision is NOT an error — rotate re-mints the SAME
        # slug-derived clientId, so reuse the existing client in place. Raising
        # here made rotate_key 500 with no CORS headers (the browser saw a
        # "blocked by CORS policy" failure instead of a real response).
        if response.status_code == 409:
            existing = httpx.get(
                f"{self.admin_api_url}/clients",
                headers=headers,
                params={"clientId": client_id},
                timeout=30
            )
            existing.raise_for_status()
            rows = existing.json()
            if not rows:
                raise HTTPException(
                    status_code=500,
                    detail=f"Keycloak client {client_id} collision unresolved",
                )
            client_uuid = rows[0]["id"]
            merged = {**rows[0], **client_config}
            update = httpx.put(
                f"{self.admin_api_url}/clients/{client_uuid}",
                headers=headers,
                json=merged,
                timeout=30
            )
            update.raise_for_status()
            logger.info(f"✅ Reused existing service account client: {client_id}")
        else:
            response.raise_for_status()
            logger.info(f"✅ Created service account client: {client_id}")

            # Get the client UUID (needed for further operations)
            client_uuid = self._get_client_uuid(client_id)

        # Get the service account user and add custom attributes
        service_account_user = self._get_service_account_user(client_uuid)
        if service_account_user:
            self._update_service_account_attributes(
                service_account_user["id"],
                user_id,
                tenant_id
            )

        return {
            "keycloak_client_id": client_id,
            "keycloak_client_secret": client_secret,
            "keycloak_client_uuid": client_uuid,
        }

    def ensure_mobile_client(self) -> dict:
        """Idempotently provision the PUBLIC `mod-mobile` client
        (mobile-runtime-enrollment). Every MOD Core instance auto-creates its
        OWN client so the app can enroll against any server — no shared OAuth,
        no manual per-operator step. Public + PKCE (no server secret); the
        enrollment QR carries this client_id at scan time. Runs on
        start-main-server + update (surgical, idempotent)."""
        client_id = os.getenv("MOBILE_OAUTH_CLIENT_ID", "mod-mobile")
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Redirect URIs: the universal custom scheme (works for any instance,
        # incl. air-gapped on-prem) + the App-Link https callback (SaaS) + dev.
        proto = os.getenv("PROTOCOL", "https")
        app_domain = os.getenv("MOBILE_APP_DOMAIN") or os.getenv("APP_DOMAIN") or "example.com"
        redirect_uris = [
            "com.example.mod://auth/callback",
            f"{proto}://{app_domain}/auth/callback",
            "http://localhost:3000/auth/callback",
        ]

        config = {
            "clientId": client_id,
            "name": "MOD Mobile",
            "description": "Public mobile app client (runtime enrollment, PKCE, device grant).",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,           # no server secret — PKCE is the credential
            "standardFlowEnabled": True,    # browser-hop authorization-code
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": redirect_uris,
            "webOrigins": ["+"],
            "attributes": {
                "pkce.code.challenge.method": "S256",                    # PKCE required
                "oauth2.device.authorization.grant.enabled": "true",    # device-grant login
                "post.logout.redirect.uris": "+".join([]) or "+",
            },
            "defaultClientScopes": ["openid", "profile", "email"],
            # Tokens must carry the fastapi-app audience or the API rejects them.
            "protocolMappers": [
                {
                    "name": "fastapi-app-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "consentRequired": False,
                    "config": {
                        "included.client.audience": KEYCLOAK_AUDIENCE,
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "introspection.token.claim": "true",
                    },
                }
            ],
        }

        # Idempotent: update in place if it already exists, else create.
        existing = httpx.get(f"{self.admin_api_url}/clients", headers=headers,
                             params={"clientId": client_id}, timeout=30)
        existing.raise_for_status()
        rows = existing.json()
        if rows:
            uuid = rows[0]["id"]
            merged = {**rows[0], **config}
            r = httpx.put(f"{self.admin_api_url}/clients/{uuid}", headers=headers, json=merged, timeout=30)
            r.raise_for_status()
            logger.info("✅ Updated mobile client %s", client_id)
            return {"client_id": client_id, "action": "updated"}
        r = httpx.post(f"{self.admin_api_url}/clients", headers=headers, json=config, timeout=30)
        if r.status_code not in (201, 409):
            r.raise_for_status()
        logger.info("✅ Created mobile client %s", client_id)
        return {"client_id": client_id, "action": "created"}

    def _get_client_uuid(self, client_id: str) -> str:
        """Get the internal UUID of a client by its clientId."""
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        response = httpx.get(
            f"{self.admin_api_url}/clients",
            headers=headers,
            params={"clientId": client_id},
            timeout=10
        )
        response.raise_for_status()
        clients = response.json()

        if not clients:
            raise Exception(f"Client {client_id} not found")

        return clients[0]["id"]

    # ------------------------------------------------------------------
    # GX.A2 — per-app OIDC client (an app built on MOD gets its own
    # code-flow-only public client: hostname is its identity).
    # ------------------------------------------------------------------

    @staticmethod
    def app_client_id(tenant_id, slug: str) -> str:
        return f"mod-app-{tenant_id}-{slug}"

    def ensure_app_client(self, app) -> dict:
        """Idempotently create-or-update the PUBLIC per-app OIDC client
        (gx-a1-app-design §2.1).

        ``app`` is a models.App (needs .tenant_id and .slug). Code-flow-only:
        PKCE S256, NO direct grants (SEC-1-005 — do not copy
        ensure_mobile_client), NO service account, and webOrigins is the app's
        ONE real origin — never "+" (open-redirect fence §7.2).
        """
        client_id = self.app_client_id(app.tenant_id, app.slug)
        proto = os.getenv("PROTOCOL", "https")
        base = os.getenv("BASE_DOMAIN", "localhost")
        hostname = app.hostname or f"{app.slug}.{base}"
        origin = f"{proto}://{hostname}"

        config = {
            "clientId": client_id,
            "name": f"MOD App {app.slug}",
            "description": f"Per-app OIDC client (code flow + PKCE S256) for app slug={app.slug}.",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,   # SEC-1-005: code flow only
            "serviceAccountsEnabled": False,
            # The app's host IS its identity — exactly its own host, no dev
            # overrides, no wildcards beyond the host path (§7.2).
            "redirectUris": [f"{origin}/*"],
            "webOrigins": [origin],
            "attributes": {
                "pkce.code.challenge.method": "S256",
            },
            # BUG B86h: on Keycloak >= 25 the `sub` claim is delivered by the
            # `basic` client scope. Without it an app access token has no `sub`,
            # the callback mints a session cookie with sub=None, and
            # parse_session_token rejects it -> login loop. `basic` is therefore
            # mandatory on every app client (also enforced post-create by
            # ensure_app_client_scopes, which covers clients created before this
            # fix and realms where the create call returned an existing row).
            "defaultClientScopes": ["openid", "profile", "email", "groups", "basic"],
            "optionalClientScopes": ["offline_access"],
            # BUG FIX (app provisioning failed at step kc_client): Keycloak 400
            # "Unrecognized field requestedClientScopes" — that key is NOT a
            # ClientRepresentation field. Optional scopes are assigned through
            # the admin REST API after the client exists (see
            # _assign_optional_client_scope below).
            # App tokens must carry the fastapi-app audience or the API
            # rejects them (audience validation in validate_token).
            "protocolMappers": [
                {
                    "name": "fastapi-app-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "consentRequired": False,
                    "config": {
                        "included.client.audience": KEYCLOAK_AUDIENCE,
                        "id.token.claim": "false",
                        "access.token.claim": "true",
                        "introspection.token.claim": "true",
                    },
                }
            ],
        }

        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Idempotent: update in place if it exists, else create (same shape as
        # ensure_mobile_client).
        existing = httpx.get(f"{self.admin_api_url}/clients", headers=headers,
                             params={"clientId": client_id}, timeout=30)
        existing.raise_for_status()
        rows = existing.json()
        if rows:
            uuid = rows[0]["id"]
            merged = {**rows[0], **config}
            r = httpx.put(f"{self.admin_api_url}/clients/{uuid}", headers=headers,
                          json=merged, timeout=30)
            r.raise_for_status()
            self._assign_optional_client_scope(uuid, "offline_access")
            self.ensure_app_client_scopes(client_id, uuid)
            logger.info("✅ Updated app client %s", client_id)
            return {"client_id": client_id, "uuid": uuid, "action": "updated"}
        r = httpx.post(f"{self.admin_api_url}/clients", headers=headers,
                       json=config, timeout=30)
        if r.status_code not in (201, 409):
            r.raise_for_status()
        uuid = None
        if r.status_code == 201:
            # KC 26.x answers 201 with an EMPTY body; the new client's UUID is
            # only in the Location header (same shape as create_user).
            loc = r.headers.get("Location")
            if loc:
                uuid = loc.rstrip("/").split("/")[-1]
            if not uuid:
                try:
                    uuid = r.json().get("id")
                except (ValueError, AttributeError):
                    uuid = None
            if not uuid:
                uuid = self._get_client_uuid(client_id)
        else:
            uuid = self._get_client_uuid(client_id)
        if uuid:
            self._assign_optional_client_scope(uuid, "offline_access")
            self.ensure_app_client_scopes(client_id, uuid)
        logger.info("✅ Created app client %s", client_id)
        return {"client_id": client_id, "uuid": uuid, "action": "created"}

    def ensure_app_client_scopes(self, client_id: str,
                                 client_uuid: Optional[str] = None) -> bool:
        """BUG B86h — self-heal the `basic` DEFAULT client scope on an app client.

        On Keycloak >= 25 the `sub` claim is delivered by the realm's `basic`
        client scope. An app client created without it mints access tokens with
        no `sub`; the app callback then signs a session cookie whose `sub` is
        None, which :func:`services.app_forward_auth.parse_session_token`
        rejects — so verify keeps bouncing the browser back to login (the
        consign login loop).

        Idempotent: if `basic` is already attached, no PUT is issued. If the
        realm has no `basic` scope (older Keycloak), skip silently and return
        False — nothing to attach, and the older realm puts `sub` on the token
        anyway. Other HTTP errors propagate as usual.

        Per-process memo (:data:`_BASIC_SCOPE_CHECKED`): a client that has been
        checked once is not re-checked, so the login path costs at most one KC
        round trip per client per process.
        """
        if client_id in _BASIC_SCOPE_CHECKED:
            return True
        uuid = client_uuid or self._get_client_uuid(client_id)
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        r = httpx.get(f"{self.admin_api_url}/client-scopes", headers=headers, timeout=30)
        r.raise_for_status()
        scope_id = None
        for scope in r.json():
            if scope.get("name") == BASIC_CLIENT_SCOPE:
                scope_id = scope.get("id")
                break
        if not scope_id:
            logger.warning(
                "Keycloak client scope %r not present in realm %s; skipped for "
                "app client %s (older Keycloak puts `sub` on the token itself)",
                BASIC_CLIENT_SCOPE, KEYCLOAK_REALM, client_id)
            _BASIC_SCOPE_CHECKED.add(client_id)
            return False

        assigned = httpx.get(
            f"{self.admin_api_url}/clients/{uuid}/default-client-scopes",
            headers=headers, timeout=30)
        assigned.raise_for_status()
        if any(s.get("id") == scope_id or s.get("name") == BASIC_CLIENT_SCOPE
               for s in assigned.json()):
            _BASIC_SCOPE_CHECKED.add(client_id)
            return True

        r = httpx.put(
            f"{self.admin_api_url}/clients/{uuid}/default-client-scopes/{scope_id}",
            headers=headers, timeout=30)
        r.raise_for_status()
        _BASIC_SCOPE_CHECKED.add(client_id)
        logger.info("✅ Attached default client scope %s to app client %s",
                    BASIC_CLIENT_SCOPE, client_id)
        return True

    def _assign_optional_client_scope(self, client_uuid: str, scope_name: str) -> bool:
        """Assign an OPTIONAL client scope to a client the supported way.

        Keycloak's ClientRepresentation has no ``requestedClientScopes`` field:
        putting it in the client body 400s with "Unrecognized field
        requestedClientScopes", which failed app provisioning at step
        kc_client. Optional scopes are assigned through the admin REST API:
        PUT /admin/realms/{realm}/clients/{clientUuid}/optional-client-scopes/{scopeId}.

        Idempotent — an already-assigned scope is fine (no second call). If the
        scope does not exist in the realm, log and return False rather than
        failing provisioning; other HTTP errors propagate as usual.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        r = httpx.get(f"{self.admin_api_url}/client-scopes", headers=headers, timeout=30)
        r.raise_for_status()
        scope_id = None
        for scope in r.json():
            if scope.get("name") == scope_name:
                scope_id = scope.get("id")
                break
        if not scope_id:
            logger.warning(
                "Keycloak client scope %r not present in realm %s; skipped for client %s",
                scope_name, KEYCLOAK_REALM, client_uuid)
            return False

        assigned = httpx.get(
            f"{self.admin_api_url}/clients/{client_uuid}/optional-client-scopes",
            headers=headers, timeout=30)
        assigned.raise_for_status()
        if any(s.get("id") == scope_id or s.get("name") == scope_name for s in assigned.json()):
            return True

        r = httpx.put(
            f"{self.admin_api_url}/clients/{client_uuid}/optional-client-scopes/{scope_id}",
            headers=headers, timeout=30)
        r.raise_for_status()
        logger.info("✅ Assigned optional client scope %s to client %s", scope_name, client_uuid)
        return True

    def disable_app_client(self, client_id: str) -> dict:
        """Disable (NOT delete — ISO 27001 keep-the-record) an app's OIDC
        client on revoke. New logins + refreshes stop immediately; the
        auditable client row stays in Keycloak (§2.2)."""
        uuid = self._get_client_uuid(client_id)
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = httpx.put(f"{self.admin_api_url}/clients/{uuid}", headers=headers,
                      json={"enabled": False}, timeout=30)
        if r.status_code == 404:
            return {"client_id": client_id, "action": "not_found"}
        r.raise_for_status()
        logger.info("✅ Disabled app client %s", client_id)
        return {"client_id": client_id, "uuid": uuid, "action": "disabled"}

    def _get_service_account_user(self, client_uuid: str) -> dict:
        """Get the service account user for a client."""
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        response = httpx.get(
            f"{self.admin_api_url}/clients/{client_uuid}/service-account-user",
            headers=headers,
            timeout=10
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        return response.json()

    def _update_service_account_attributes(self, service_user_id: str, user_id: int, tenant_id: int):
        """
        Update service account user attributes to include API key owner info.
        This allows token validation to retrieve the original user context.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Update user attributes
        update_data = {
            "attributes": {
                "api_key_user_id": [str(user_id)],
                "api_key_tenant_id": [str(tenant_id)],
            }
        }

        response = httpx.put(
            f"{self.admin_api_url}/users/{service_user_id}",
            headers=headers,
            json=update_data,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"✅ Updated service account attributes for user {service_user_id}")

    def delete_service_account_client(self, client_uuid: str):
        """
        Deletes a Keycloak client (service account) by its UUID.
        Called when revoking an API key.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        response = httpx.delete(
            f"{self.admin_api_url}/clients/{client_uuid}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"✅ Deleted service account client: {client_uuid}")

    # ─── Identity Provider Management ───────────────────────────────

    PROVIDER_CONFIGS = {
        "github": {
            "providerId": "github",
            "displayName": "GitHub",
            "config": {
                "clientId": "",
                "clientSecret": "",
                "defaultScope": "user:email",
            },
        },
        "google": {
            "providerId": "google",
            "displayName": "Google",
            "config": {
                "clientId": "",
                "clientSecret": "",
                "defaultScope": "openid email profile",
                "hostedDomain": "",
            },
        },
        "linkedin-openid-connect": {
            "providerId": "linkedin-openid-connect",
            "displayName": "LinkedIn",
            "config": {
                "clientId": "",
                "clientSecret": "",
                "defaultScope": "openid profile email",
            },
        },
        "facebook": {
            "providerId": "facebook",
            "displayName": "Facebook",
            "config": {
                "clientId": "",
                "clientSecret": "",
                "defaultScope": "email",
            },
        },
        "microsoft": {
            "providerId": "microsoft",
            "displayName": "Microsoft",
            "config": {
                "clientId": "",
                "clientSecret": "",
                "defaultScope": "openid email profile",
            },
        },
    }

    def list_identity_providers(self) -> list:
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.get(
            f"{self.admin_api_url}/identity-provider/instances",
            headers=headers, timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_identity_provider(self, alias: str) -> dict:
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.get(
            f"{self.admin_api_url}/identity-provider/instances/{alias}",
            headers=headers, timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def configure_identity_provider(self, provider_type: str, client_id: str, client_secret: str) -> dict:
        """Create or update an identity provider in Keycloak."""
        template = self.PROVIDER_CONFIGS.get(provider_type)
        if not template:
            raise ValueError(f"Unknown provider type: {provider_type}")

        alias = provider_type
        config = {**template["config"], "clientId": client_id, "clientSecret": client_secret}
        payload = {
            "alias": alias,
            "providerId": template["providerId"],
            "displayName": template["displayName"],
            "enabled": True,
            "trustEmail": True,
            "storeToken": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": config,
        }

        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        existing = self.get_identity_provider(alias)
        if existing:
            response = httpx.put(
                f"{self.admin_api_url}/identity-provider/instances/{alias}",
                headers=headers, json=payload, timeout=10,
            )
        else:
            response = httpx.post(
                f"{self.admin_api_url}/identity-provider/instances",
                headers=headers, json=payload, timeout=10,
            )

        response.raise_for_status()
        logger.info(f"Configured identity provider: {alias}")
        return {"alias": alias, "status": "configured"}

    def remove_identity_provider(self, alias: str):
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = httpx.delete(
            f"{self.admin_api_url}/identity-provider/instances/{alias}",
            headers=headers, timeout=10,
        )
        if response.status_code == 404:
            return
        response.raise_for_status()
        logger.info(f"Removed identity provider: {alias}")

    # -----------------------------------------------------------------
    # add-self-service-sso §2.7 — generic Identity Provider lifecycle.
    #
    # configure_identity_provider() above was OIDC-only with a
    # hardcoded provider lookup; these methods are protocol-agnostic
    # (SAML 2.0 + OIDC both flow through create_identity_provider).
    # Keeping the old method untouched so existing OIDC callers don't
    # break.
    # -----------------------------------------------------------------

    def create_identity_provider(
        self,
        alias: str,
        protocol: str,
        config: dict,
        *,
        display_name: Optional[str] = None,
        first_broker_login_flow_alias: str = "first broker login",
        enabled: bool = False,
        trust_email: bool = False,
        store_token: bool = False,
    ) -> dict:
        """Create a per-tenant Identity Provider in the realm.

        `protocol` is "saml" or "oidc"; maps to Keycloak's
        `providerId` ("saml" or "oidc" — Keycloak uses the same
        identifier).

        `config` is the protocol-specific config dict. For OIDC the
        common keys are clientId, clientSecret, authorizationUrl,
        tokenUrl, jwksUrl, defaultScope. For SAML they're
        singleSignOnServiceUrl, signingCertificate, nameIDPolicyFormat,
        wantAssertionsSigned, wantAssertionsEncrypted.

        `enabled=False` is the default — per the sso-idp-config spec,
        IdPs are created in `draft` state and only flipped to enabled
        after at least one associated domain is verified. The caller
        flips `enabled=true` via update_identity_provider() once the
        gate passes.

        Returns the alias on success, raises httpx.HTTPStatusError on
        Keycloak rejection.
        """
        if protocol not in ("saml", "oidc"):
            raise ValueError(f"Unsupported SSO protocol: {protocol!r}")
        token = self._get_token()
        payload = {
            "alias": alias,
            "providerId": protocol,
            "displayName": display_name or alias,
            "enabled": enabled,
            "trustEmail": trust_email,
            "storeToken": store_token,
            "firstBrokerLoginFlowAlias": first_broker_login_flow_alias,
            "config": config,
        }
        response = httpx.post(
            f"{self.admin_api_url}/identity-provider/instances",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Created identity provider alias=%s protocol=%s enabled=%s",
                    alias, protocol, enabled)
        return {"alias": alias, "status": "created"}

    def update_identity_provider(self, alias: str, patch: dict) -> dict:
        """Partial-update an Identity Provider. `patch` is merged into
        the existing record server-side via PUT-after-GET so we don't
        accidentally drop fields we didn't include. Supports flipping
        `enabled`, swapping the firstBrokerLoginFlowAlias, rotating a
        client_secret, etc.
        """
        existing = self.get_identity_provider(alias)
        if not existing:
            raise ValueError(f"Identity provider not found: {alias}")
        # Shallow merge for top-level keys; nested `config` dict needs
        # a separate merge so partial config updates don't blow away
        # untouched config keys.
        merged = {**existing, **{k: v for k, v in patch.items() if k != "config"}}
        if "config" in patch and isinstance(patch["config"], dict):
            merged["config"] = {**(existing.get("config") or {}), **patch["config"]}
        token = self._get_token()
        response = httpx.put(
            f"{self.admin_api_url}/identity-provider/instances/{alias}",
            headers={"Authorization": f"Bearer {token}"},
            json=merged,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Updated identity provider alias=%s keys=%s",
                    alias, sorted(patch.keys()))
        return {"alias": alias, "status": "updated"}

    def set_first_broker_login_flow(self, idp_alias: str, flow_alias: str) -> dict:
        """Point an existing IdP at a specific Authentication Flow for
        its First Broker Login. The custom flow_alias is provisioned
        idempotently at startup (see §2.8 — the
        first-broker-login-consent flow that surfaces the explicit
        account-link consent screen instead of silently creating
        duplicates).
        """
        return self.update_identity_provider(
            idp_alias, {"firstBrokerLoginFlowAlias": flow_alias},
        )

    def revoke_user_sessions(self, keycloak_user_id: str) -> bool:
        """Revoke every active session for a Keycloak user. Used when
        an IdP is deleted (each user authenticated via that IdP must
        be logged out immediately), when a domain ownership lapses
        and we auto-deactivate, or when an admin force-rotates a SCIM
        user (deferred to add-scim-directory-sync).
        """
        token = self._get_token()
        # Keycloak's logout-all endpoint: POST users/{id}/logout
        response = httpx.post(
            f"{self.admin_api_url}/users/{keycloak_user_id}/logout",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if response.status_code in (200, 204):
            logger.info("Revoked sessions for user_id=%s", keycloak_user_id)
            return True
        if response.status_code == 404:
            return False  # user doesn't exist; nothing to revoke
        logger.warning("Failed to revoke sessions for %s: HTTP %s %s",
                       keycloak_user_id, response.status_code, response.text[:200])
        return False

    def get_service_account_token(self, client_id: str, client_secret: str) -> dict:
        """
        Exchange client credentials for an access token.
        This is the OAuth 2.0 Client Credentials flow.

        Returns the full token response including access_token, expires_in, etc.
        """
        token_url = f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"

        response = httpx.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json()


    def set_verify_email(self, enabled: bool) -> tuple[bool, int, str]:
        """Returns (ok, status_code, detail). status_code=502 if Keycloak unreachable."""
        try:
            token = self._get_token()
            resp = httpx.put(
                self.admin_api_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"verifyEmail": enabled},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                return True, resp.status_code, ""
            detail = f"Keycloak {resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 403:
                detail += " (fastapi-admin-service likely missing realm-management/manage-realm role)"
            logger.warning("Keycloak verifyEmail update rejected: %s", detail)
            return False, resp.status_code, detail
        except Exception as e:
            logger.warning("Failed to reach Keycloak for verifyEmail update: %s", e)
            return False, 502, f"Cannot reach Keycloak: {e}"

    def get_verify_email(self) -> bool:
        try:
            token = self._get_token()
            resp = httpx.get(
                self.admin_api_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("verifyEmail", False)
        except Exception as e:
            logger.warning("Failed to get Keycloak verifyEmail: %s", e)
        return False

    def set_reset_password_allowed(self, enabled: bool) -> tuple[bool, int, str]:
        """Enable/disable the realm's self-service 'Forgot Password?' flow.
        Returns (ok, status_code, detail). status_code=502 if Keycloak is
        unreachable. Kept parallel to set_verify_email so the email-config
        save path can wire both together: a reset link is useless without
        SMTP to send the mail, so the two settings move as one."""
        try:
            token = self._get_token()
            resp = httpx.put(
                self.admin_api_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"resetPasswordAllowed": enabled},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                return True, resp.status_code, ""
            detail = f"Keycloak {resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 403:
                detail += " (fastapi-admin-service likely missing realm-management/manage-realm role)"
            logger.warning("Keycloak resetPasswordAllowed update rejected: %s", detail)
            return False, resp.status_code, detail
        except Exception as e:
            logger.warning("Failed to reach Keycloak for resetPasswordAllowed update: %s", e)
            return False, 502, f"Cannot reach Keycloak: {e}"

    def update_realm_smtp(self, smtp_host: str, smtp_port: int, smtp_user: str, smtp_password: str, from_email: str,
                          smtp_security: Optional[str] = None) -> bool:
        # auth/starttls/ssl come from utils.smtp_security so the realm uses the same
        # rule as the mailer. smtp_security=None ('auto') keeps the historical
        # output byte-for-byte (starttls only for 587/465, ssl only for 465).
        from utils.smtp_security import keycloak_smtp_flags
        flags = keycloak_smtp_flags(smtp_port, smtp_security, smtp_user)
        try:
            token = self._get_token()
            resp = httpx.put(
                self.admin_api_url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "smtpServer": {
                        "host": smtp_host,
                        "port": str(smtp_port),
                        "auth": flags["auth"],
                        "user": smtp_user or "",
                        "password": smtp_password or "",
                        "from": from_email,
                        "fromDisplayName": "MOD Platform",
                        "starttls": flags["starttls"],
                        "ssl": flags["ssl"],
                    }
                },
                timeout=10,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.warning("Failed to update Keycloak SMTP: %s", e)
            return False

    # -----------------------------------------------------------------
    # Phase 1 — password policy management
    # -----------------------------------------------------------------

    def set_user_password(
        self,
        user_id: str,
        new_password: str,
        *,
        temporary: bool = False,
        validate: bool = True,
        username: Optional[str] = None,
        email: Optional[str] = None,
        tenant_slug: Optional[str] = None,
    ) -> bool:
        """Set or reset a user's Keycloak password. When `validate=True`
        (default) the password is run through `security.password_policy.
        validate_password` BEFORE the Keycloak call so the API and the
        realm policy agree on rejections.

        `temporary=True` flags the password as needing reset on next
        login (used for admin-initiated resets).
        """
        if validate:
            from security.password_policy import validate_password
            validate_password(
                new_password,
                username=username,
                email=email,
                tenant_slug=tenant_slug,
            )
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.put(
                f"{self.admin_api_url}/users/{user_id}/reset-password",
                headers=headers,
                json={
                    "type": "password",
                    "value": new_password,
                    "temporary": bool(temporary),
                },
                timeout=10,
            )
            if response.status_code in (200, 204):
                logger.info("Set password for Keycloak user %s (temporary=%s)",
                            user_id, temporary)
                return True
            logger.warning(
                "Keycloak set-password returned %d for user %s",
                response.status_code, user_id,
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to set Keycloak password for %s (%s)",
                             user_id, type(e).__name__)
            return False

    def apply_realm_password_policy(
        self, *, policy: Optional[str] = None,
    ) -> bool:
        """Push the canonical password policy onto the realm. Returns
        True on success. Idempotent — Keycloak silently no-ops when the
        policy string is unchanged."""
        from security.password_policy import KEYCLOAK_PASSWORD_POLICY
        target_policy = policy or KEYCLOAK_PASSWORD_POLICY
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.put(
                f"{self.admin_api_url}",
                headers=headers,
                json={
                    "passwordPolicy": target_policy,
                    "bruteForceProtected": True,
                    "failureFactor": 5,
                    "permanentLockout": False,
                    "waitIncrementSeconds": 60,
                    "maxFailureWaitSeconds": 900,
                },
                timeout=15,
            )
            if resp.status_code in (200, 204):
                logger.info("Applied Keycloak realm password policy: %s",
                            target_policy)
                return True
            logger.warning(
                "Keycloak realm-policy update returned %d: %s",
                resp.status_code, resp.text[:300],
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to apply realm password policy (%s)",
                             type(e).__name__)
            return False

    # FX.54 p3d — the KC-init realm-session push. keycloak-init.sh pins these
    # five realm fields from env (it runs inside the Keycloak container and
    # cannot read the API DB); this is the API-side half of the same init path,
    # so the realm converges to the LIVE platform-settings values on every boot
    # and on every settings change (services/platform_settings.py schedules
    # services/session_tier_enforcement.converge_session_tier after commit).
    # The realm carries the NORMAL (human-user) tier only — KC has no per-role
    # session lifetimes, so the admin tiers stay server-side (FX.54 p3b gate).
    REALM_SESSION_TIER_FIELDS = ("ssoSessionIdleTimeout", "ssoSessionMaxLifespan",
                                 "rememberMe",
                                 "ssoSessionIdleTimeoutRememberMe",
                                 "ssoSessionMaxLifespanRememberMe")

    def desired_realm_session_tier(self, db) -> dict:
        """The five realm session fields, read from the settings registry.

        NORMAL tier pair via services/session_policy.tier_limits (the single
        source of truth p3a/p3b/p3c share) + the remember-me trio. Raises only
        on a settings read failure — the caller logs it, never crashes init.
        """
        import services.session_policy as session_policy
        from services.session_policy import NORMAL_TIER, REMEMBER_ME_SETTING_KEYS
        from services import platform_settings as psr

        limits = session_policy.tier_limits(NORMAL_TIER, db=db)
        if limits.get("errors"):
            raise ValueError(f"setting read failed: {limits['errors']}")
        return {
            "ssoSessionIdleTimeout": int(limits["idle"] or 0),
            "ssoSessionMaxLifespan": int(limits["absolute"] or 0),
            "rememberMe": bool(psr.get_setting(
                REMEMBER_ME_SETTING_KEYS["enabled"], db=db)),
            "ssoSessionIdleTimeoutRememberMe": int(psr.get_setting(
                REMEMBER_ME_SETTING_KEYS["idle"], db=db) or 0),
            "ssoSessionMaxLifespanRememberMe": int(psr.get_setting(
                REMEMBER_ME_SETTING_KEYS["absolute"], db=db) or 0),
        }

    def push_realm_session_tier(self, db) -> dict:
        """Idempotently converge the realm's session-tier + remember-me fields.

        PARTIAL realm PUT only, carrying the live fields Keycloak 26.x would
        otherwise reset (attributes/frontendUrl — the 2026-10-04 login-outage
        wipe) through untouched, so no unrelated realm attribute changes.
        Never raises: a KC failure is logged and reported in ``note``.
        Returns ``{"applied", "changed", "desired", "note"}``.
        """
        result = {"applied": False, "changed": False, "desired": {}, "note": ""}
        try:
            desired = self.desired_realm_session_tier(db)
            result["desired"] = desired
        except Exception as exc:  # noqa: BLE001 — init must not crash
            logger.warning("FX.54 p3d KC init: session-tier settings read "
                           "failed (non-fatal): %s", exc)
            result["note"] = f"setting read failed: {exc}"
            return result

        try:
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            resp = httpx.get(self.admin_api_url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.warning("FX.54 p3d KC init: realm GET -> HTTP %s "
                               "(keycloak-init.sh re-converges on boot)",
                               resp.status_code)
                result["note"] = f"kc error: realm GET -> HTTP {resp.status_code}"
                return result
            existing = resp.json()
            changed = [f for f in desired if existing.get(f) != desired[f]]
            if not changed:
                result["applied"] = True
                result["note"] = "already converged"
                return result

            body = {"realm": existing.get("realm")
                            or self.admin_api_url.rstrip("/").rsplit("/", 1)[-1]}
            for field in self._REALM_PUT_CARRY_THROUGH_FIELDS:
                if field in existing:
                    body[field] = existing[field]
            body.update(desired)
            put = httpx.put(self.admin_api_url, headers=headers, json=body,
                            timeout=30)
            if put.status_code in (200, 204):
                result.update(applied=True, changed=True,
                              note=f"converged fields: {sorted(changed)}")
                logger.info("FX.54 p3d KC init: realm session tier converged "
                            "(%s)", sorted(changed))
            else:
                logger.warning("FX.54 p3d KC init: realm PUT -> HTTP %s: %s",
                               put.status_code, put.text[:200])
                result["note"] = f"kc error: realm PUT -> HTTP {put.status_code}"
            return result
        except Exception as exc:  # noqa: BLE001 — never crash init
            logger.warning("FX.54 p3d KC init: realm session-tier push failed "
                           "(non-fatal): %s", exc)
            result["note"] = f"{type(exc).__name__}: {exc}"
            return result

    # Fields a partial realm PUT must carry through or Keycloak 26.x resets
    # them (the 2026-10-04 frontendUrl wipe). Whitelist, not blacklist: masked
    # secrets (smtpServer.password, keystore, credentials) are never re-sent.
    _REALM_PUT_CARRY_THROUGH_FIELDS = (
        "attributes",
        "bruteForceProtected", "permanentLockout", "failureFactor",
        "waitIncrementSeconds", "maxFailureWaitSeconds", "maxDeltaTimeSeconds",
        "quickLoginCheckMilliSeconds", "minimumQuickLoginWaitSeconds",
        "loginTheme", "accountTheme", "adminTheme",
    )

    def bulk_add_required_action(
        self,
        action: str = "UPDATE_PASSWORD",
        *,
        max_users: int = 5000,
    ) -> int:
        """Add the given Keycloak required-action to every realm user
        that does not already have it queued. Returns the number of
        users updated. Idempotent — users with the action already
        queued are skipped.

        Used at policy-rollout time to force every existing user to
        reset their password on next login (CMMC reading of policy
        change == force reset).
        """
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        updated = 0
        first = 0
        page_size = 100
        try:
            while first < max_users:
                resp = httpx.get(
                    f"{self.admin_api_url}/users",
                    headers=headers,
                    params={"first": first, "max": page_size, "briefRepresentation": False},
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json() or []
                if not batch:
                    break
                for user in batch:
                    user_id = user.get("id")
                    if not user_id:
                        continue
                    actions = user.get("requiredActions") or []
                    if action in actions:
                        continue
                    actions = list(actions) + [action]
                    put_resp = httpx.put(
                        f"{self.admin_api_url}/users/{user_id}",
                        headers=headers,
                        json={"requiredActions": actions},
                        timeout=10,
                    )
                    if put_resp.status_code in (200, 204):
                        updated += 1
                first += page_size
                if len(batch) < page_size:
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception("bulk_add_required_action failed mid-batch: %s", e)
        logger.info(
            "bulk_add_required_action(%s): queued action on %d user(s)",
            action, updated,
        )
        return updated


# --- GSEC R-043 — x509 / PIV client-cert login seam (documented no-op) ---

def x509_piv_login_enabled(db=None) -> bool:
    """GSEC R-043 (IA-2(12) / High-baseline IA-2) — x509/PIV login seam.

    CURRENT STATE: a documented no-op. This function only reports the
    platform setting ``auth.x509_enabled`` (default False). No auth path
    consults it, no network call is made, and existing token validation
    (well-known discovery, JWKS, audience/issuer checks) is untouched.
    With the setting False — as everywhere today — this is a pure no-op.

    FORWARD DESIGN (implementation wave; nothing below is wired yet):

    1. Keycloak side — add a dedicated PIV/X509 flow to the realm (a copy
       of ``browser``) containing the built-in X509 authenticator with the
       "X509/Validate Username" execution. The reverse proxy in front of
       Keycloak terminates mutual TLS and must forward the verified peer
       certificate (e.g. ``X-SSL-Cert`` + ``X-SSL-Verify=SUCCESS``
       headers); the authenticator validates that certificate against
       the DoD CA / PIV card issuer trust store configured on the realm.

    2. Mapping to MOD users — the authenticator (or a following
       user-session-attribute / required-action step) matches the
       certificate's subject DN (or serial) against a per-user attribute
       on the Keycloak user record, e.g.
       ``x509_subject_dn`` / ``x509_serial``, and authenticates that
       specific user with no password. The MOD user is then resolved
       EXACTLY as today — via the user's ``keycloak_user_id`` on the
       app-side User row — so there is no new identity channel on the API
       side; the PIV login simply produces the same tokens the password
       flow does.

    3. Operational switch — once (1) and (2) exist, ``auth.x509_enabled``
       becomes the gate this function consults, and any token-validation
       code that must accept tokens minted through the PIV flow checks it
       here. Until then it stays False and this function returns False.

    Raises nothing and mutates nothing; safe to call from any request
    path.
    """
    from services.platform_settings import get_setting

    return get_setting("auth.x509_enabled", db=db)


# --- Probe OIDC config on startup: warn loudly, but never block the import ---
# Both getters are TTL-cached and cache nothing on failure, so the first
# request that needs them retries. Raising here instead left uvicorn unable to
# load the app at all, which presents as a "running" container serving nothing.
try:
    OIDC_CONFIG = get_oidc_config()
    JWKS = get_jwks()
except Exception as _startup_auth_exc:
    OIDC_CONFIG = None
    JWKS = None
    logger.error(
        "Keycloak unreachable at startup (%s: %s). The API will start and retry "
        "on first authenticated request; logins fail until Keycloak answers.",
        type(_startup_auth_exc).__name__, _startup_auth_exc,
    )
keycloak_admin_client = KeycloakAdmin()