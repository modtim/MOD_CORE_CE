# In API/dependencies.py

import time
from typing import Optional
from fastapi import Depends, HTTPException, Request, status, Header
from fastapi.security import OAuth2AuthorizationCodeBearer # <-- IMPORTANT: Import this
from sqlalchemy.orm import Session
from jose import jwt, JWTError
import logging
import os # <-- Add this import
import types

import crud, models
from database import SessionLocal
from utils.minio_client import MinioClient
from keycloak_auth import get_current_user_claims as keycloak_get_current_user_claims
from security import verify_api_key
from utils import api_key_scopes
from utils.paid_import import optional_paid, is_paid_tree

# Setup logging
logger = logging.getLogger(__name__)

# GSEC R-083 — state for the once-per-minute fail-open warning in
# require_active_and_onboarded_user (see the limiter except-block below).
_rate_limiter_warn_state = types.SimpleNamespace(last=0.0)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode


def get_minio_client():
    """
    Provides a MinioClient instance without tenant context.

    DEPRECATED: Use get_tenant_minio_client() instead for tenant-isolated storage.
    This exists for backwards compatibility with services that manage their own tenant context.
    """
    return MinioClient()


def get_db():
    """Provides a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- REPLACEMENT FOR HTTPBEARER ---
# This mirrors the configuration in main.py to ensure the docs and the
# security dependency are using the exact same OAuth2 flow.
KEYCLOAK_EXTERNAL_URL = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM_NAME", "modtex")

# This is the key change: we use the interactive OAuth2 scheme here
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth",
    tokenUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
)
# --- END REPLACEMENT ---


# The dependency chain now starts with our new oauth2_scheme
def get_current_user_claims(token: str = Depends(oauth2_scheme)) -> dict:
    # We pass the token directly to the existing keycloak logic
    try:
        return keycloak_get_current_user_claims(token)
    except HTTPException:
        # Audit: token validation failure (CMMC AU-2)
        try:
            from database import SessionLocal
            from models import AuditEvent
            audit_db = SessionLocal()
            try:
                audit_db.add(AuditEvent(
                    event_name="AUTH_TOKEN_INVALID",
                    details={"token_prefix": token[:8] + "..." if token else None},
                ))
                audit_db.commit()
            finally:
                audit_db.close()
        except Exception:
            pass
        raise


def get_keycloak_token(token: str = Depends(oauth2_scheme)) -> str:
    """
    Extracts the raw Keycloak access token from the request.

    This is used to pass the user's token to worker containers for
    tenant-scoped STS credential generation.

    Returns:
        Raw Keycloak access token string
    """
    return token


# `auto_error=False` variant: resolves the bearer token if present but never
# raises 401 when it's missing/malformed. Backs get_optional_current_user().
oauth2_scheme_optional = OAuth2AuthorizationCodeBearer(
    authorizationUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth",
    tokenUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
    auto_error=False,
)


def get_optional_current_user(
        db: Session = Depends(get_db),
        token: Optional[str] = Depends(oauth2_scheme_optional),
) -> Optional["models.User"]:
    """Best-effort current-user resolution that never raises.

    Returns None instead of 401/403 when no bearer token is present, the
    token fails validation, or the user hasn't completed registration —
    for endpoints that are public by default but branch on recognizing an
    authenticated (and possibly platform-admin) caller, e.g. the unified
    `/health` endpoint's `?extended=` branch (health-merge, Gate 1).
    """
    if not token:
        return None
    try:
        claims = keycloak_get_current_user_claims(token)
    except HTTPException:
        return None
    try:
        return get_current_user(db=db, claims=claims)
    except HTTPException:
        return None


# --- All other dependencies below this point remain the same ---
# They will now correctly inherit the interactive login flow.

def _enforce_tenant_ip_allowlist(db, user, request=None) -> None:
    """FX.101 part 1 (owner decision Q87=A) — tenant IP allowlist gate.
    Raises 403 ("IP not allowed for this tenant") + an
    AuditAction.TENANT_IP_BLOCKED audit row when the caller's real IP
    (utils.client_ip.trusted_client_ip) is outside the tenant's CIDR
    list. Empty list / kill switch off = allow all. No exemption for
    platform admins — the check applies to the caller's own tenant.

    Edition-aware: resolves through optional_paid (skips gate if module
    absent for CE); evaluation errors in Pro fail closed with 403."""
    _fx101 = optional_paid("services.tenant_ip_allowlist")
    if _fx101 is None:
        # feature not in this edition — skip the gate silently
        return
    try:
        _fx101.enforce(db, user, request)
    except HTTPException:
        raise
    except Exception as _e:  # noqa: BLE001 — fail closed for Pro
        logger.warning(
            "FX.101: IP allowlist gate failed to evaluate for user=%s: %s",
            getattr(user, "id", None), _e)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="IP allowlist service unavailable",
            headers={"X-Error-Code": "TENANT_IP_BLOCKED"},
        )


def _enforce_external_gate_for_key_owner(db, user, claims, require_terms) -> None:
    """BUG-1027u — the FX.110 outside-collaborator gate for an API-key OWNER.

    ONE helper, called from BOTH api-key funnels: the azp api-key Bearer
    branch of _resolve_current_user (behaviour byte-identical to its old
    inline block: same log text, same 403 detail, same X-Error-Code) and the
    X-API-Key branch of require_bearer_or_api_key, which used to short-circuit
    the gate entirely. A static key owned by an external collaborator must
    stop working once their access expired / was deactivated / their terms
    were withdrawn. ``claims`` may be None — enforce_external_gate declares
    it ``Optional[Dict[str, Any]] = None`` (services/outside_collaborators.py:155).
    Internal users never enter the gate (zero extra work). Fail closed: any
    non-HTTPException evaluation failure is a 403, never a pass."""
    if getattr(user, "is_external", False):
        try:
            from services import outside_collaborators as _fx110
            _fx110.enforce_external_gate(db, user, claims,
                                         require_terms=require_terms)
        except HTTPException:
            raise
        except Exception as _e:  # noqa: BLE001 — fail closed, never open
            logger.warning(
                "FX.110: external gate failed to evaluate for api-key owner "
                "user=%s: %s",
                user.id, _e)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="External access could not be verified. Please try "
                       "again or contact the tenant admin.",
                headers={"X-Error-Code": "EXTERNAL_GATE_UNAVAILABLE"},
            )


def _resolve_current_user(
        db: Session,
        claims: dict,
        request,
        *,
        require_terms: bool,
) -> models.User:
    """
    Plain helper (NOT a FastAPI dependency): resolves the caller's user.

    SECURITY (GSEC FX.110): this used to be the dependency itself, which made
    ``require_terms`` a plain parameter of a dependency and therefore BOUND
    FROM THE QUERY STRING — any client could send ``?require_terms=false`` and
    skip the outside-collaborator terms gate (and ~1300 endpoints grew a
    ``require_terms`` query param + a 422). It is now a positional helper with
    a required keyword-only ``require_terms``: FastAPI never sees it, never
    introspects it, and cannot bind it from a request.

    Supports two authentication methods:
    1. Regular user tokens: User's Keycloak ID is in 'sub' claim
    2. API key tokens: Service account token with 'azp' starting with 'api-key-'
       In this case, we look up the user from the api_keys table
    """
    sub = claims.get('sub', '')
    azp = claims.get('azp', '')  # Authorized party (client_id)

    # Check if this is an API key service account token
    if azp.startswith('api-key-'):
        # API key token - look up the original user from the api_keys table
        db_api_key = crud.get_api_key_by_keycloak_client_id(db, keycloak_client_id=azp)
        if not db_api_key:
            logger.warning(f"API key not found for Keycloak client: {azp}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key has been revoked or is invalid.",
            )

        # 2400GX-sec1: an exchanged api-key bearer (POST /v1/api-keys/token)
        # must get the SAME post-resolution policy as a static X-API-Key
        # (expiry / disabled / app-bound route + workflow scope). Idempotent
        # per request (see _enforce_api_key_policy), so a route that also runs
        # require_bearer_or_api_key does not enforce twice. Non-app keys only
        # get the expiry/disabled check; fail closed above if the row is gone.
        _enforce_api_key_policy(db, db_api_key, request)

        # Get the user who owns this API key
        db_user = crud.get_user(db, user_id=db_api_key.user_id)
        if not db_user:
            logger.warning(f"User {db_api_key.user_id} not found for API key {azp}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key owner not found.",
            )

        logger.debug(f"API key authentication: client={azp}, user_id={db_user.id}")
        # Gate 32: retain WHICH key authorised this request. Carried in a
        # request-scoped contextvar so authorize() (scope intersection, Gate 35/36)
        # and the audit trail (Gate 33) can see it even when the user object is
        # later re-queried (e.g. cog checks fetch the user by id).
        try:
            from utils.request_principal import set_api_key
            set_api_key(db_api_key)
        except Exception:
            pass
        # FX.110 (owner decision Q97=A) — outside collaborators gate, API-key
        # branch. The key is OWNED by a user; if that owner is an external
        # collaborator, their every-call gate (expiry / deactivation / sign-in
        # strength / gov-approval / terms) must run BEFORE the key short-circuits
        # the dependency, otherwise a key keeps working after the collaborator's
        # access expired or their terms were withdrawn. Same function, same
        # fail-closed posture as the regular-token branch below; internal users
        # never enter the gate (zero extra work). BUG-1027u: the block moved
        # verbatim into _enforce_external_gate_for_key_owner so the X-API-Key
        # funnel (require_bearer_or_api_key) applies the SAME gate — behaviour
        # here is unchanged (same log text, same 403 detail, same header).
        _enforce_external_gate_for_key_owner(db, db_user, claims, require_terms)
        # FX.101 part 1 (Q87=A) — tenant IP allowlist gate.
        _enforce_tenant_ip_allowlist(db, db_user, request)
        # GSEC FX.65 p3a — BYOK revoke -> tenant dark gate (423 when the
        # tenant's BYOK key has been refused/revoked; no-op for tenants
        # without a kms.byok_provider configured; platform admins bypass).
        # 1682: the import is GUARDED (paid-only module, absent from the CE
        # carve -> ImportError -> 500 on every authed CE call). optional_paid
        # returns None only when the module is absent; an exception raised by
        # require_not_dark itself (the dark-tenant refusal) still propagates.
        # CE has no BYOK, so no tenant can be dark: skipping the gate there is
        # behaviour-preserving, not a security relaxation.
        _fx65 = optional_paid("services.kms_tenant_state")
        if _fx65 is not None:
            _fx65.require_not_dark(db, db_user)
        elif is_paid_tree():
            # 1682: SaaS fail-closed anomaly guard. The paid sentinel module
            # IS present (this is not the CE carve), yet kms_tenant_state
            # itself is missing -- a broken/partial paid build, not a
            # legitimate absence. Silently skipping the dark-tenant gate
            # here would let a revoked/dark tenant keep calling the API, so
            # refuse rather than fail open.
            logger.error(
                "build missing services.kms_tenant_state -- "
                "refusing auth rather than skipping the dark-tenant gate "
                "(user=%s)", db_user.id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication subsystem misconfigured. Please contact support.",
                headers={"X-Error-Code": "AUTH_SUBSYSTEM_MISCONFIGURED"},
            )
        return db_user

    # Regular user token - look up by Keycloak ID
    db_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=sub)
    if not db_user:
        # GX.S1b: app user types. A KC identity that is ONLY an app user
        # (an `app_users` row, e.g. an app's `consignor`) has no MOD tenant
        # identity by contract — it must never get a `users` row (that is
        # how it stays out of every MOD route). Refuse here, BEFORE the
        # REGISTRATION_INCOMPLETE path, so no onboarding/auto-provisioning
        # can mint a MOD user from this token. (Fails open when the
        # app_users table is absent — migration not applied yet.)
        try:
            from services import app_user_types as _aut
            if _aut.sub_is_app_user(db, sub):
                logger.warning(
                    "MOD route refused for app-only user (no MOD identity): sub=%s",
                    sub,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This identity belongs to a MOD app, not the MOD platform.",
                    headers={"X-Error-Code": "APP_USER_NO_PLATFORM_ACCESS"},
                )
        except HTTPException:
            raise
        except Exception:
            pass
        logger.warning(f"User with Keycloak ID {sub} not found in DB.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has not completed registration.",
            headers={"X-Error-Code": "REGISTRATION_INCOMPLETE"},
        )

    # GX.A3b: service accounts have no interactive login. An app's backing
    # service user authenticates ONLY through its app's scoped API key
    # (the api-key- branch above); a Bearer/interactive token resolving to
    # one is refused.
    if getattr(db_user, "is_service_account", False):
        logger.warning(
            "Interactive login refused for service account user=%s (app keys only)",
            db_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service accounts cannot log in interactively.",
            headers={"X-Error-Code": "SERVICE_ACCOUNT_NO_INTERACTIVE"},
        )

    # Email verification check (SaaS mode only, skip for platform admins)
    if os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas":
        if not db_user.is_platform_admin:
            email_verified = claims.get("email_verified", False)
            if not email_verified:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Please verify your email address before accessing the platform.",
                    headers={"X-Error-Code": "EMAIL_NOT_VERIFIED"},
                )

    # FX.110 (owner decision Q97=A) — outside collaborators gate. An
    # EXTERNAL user's every API call passes through
    # services/outside_collaborators.enforce_external_gate: sign-in
    # strength (MFA / own-company SSO claim), gov/DoD per-user approval,
    # account expiry, and the terms gate. Internal users return from the
    # gate untouched. FAIL CLOSED: if the gate itself errors on an
    # external user, the call is refused — an external collaborator
    # whose access cannot be verified must not be served (a 403 with a
    # clear code, matching the security.stack_tokens_fail_closed posture).
    if getattr(db_user, "is_external", False):
        try:
            from services import outside_collaborators as _fx110
            _fx110.enforce_external_gate(db, db_user, claims,
                                         require_terms=require_terms)
        except HTTPException:
            raise
        except Exception as _e:  # noqa: BLE001 — fail closed, never open
            logger.warning(
                "FX.110: external gate failed to evaluate for user=%s: %s",
                db_user.id, _e)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="External access could not be verified. Please try "
                       "again or contact the tenant admin.",
                headers={"X-Error-Code": "EXTERNAL_GATE_UNAVAILABLE"},
            )

    # FX.101 part 1 (Q87=A) — tenant IP allowlist gate (interactive
    # tokens; the API-key branch enforces the same gate above).
    _enforce_tenant_ip_allowlist(db, db_user, request)

    # GSEC FX.65 p3a — BYOK revoke -> tenant dark gate (423 when the
    # tenant's BYOK key has been refused/revoked; no-op for tenants
    # without a kms.byok_provider configured; platform admins bypass).
    # 1682: guarded import — see the API-key branch above. Fail-closed: when
    # the paid module IS present, require_not_dark's own refusal propagates.
    _fx65 = optional_paid("services.kms_tenant_state")
    if _fx65 is not None:
        _fx65.require_not_dark(db, db_user)
    elif is_paid_tree():
        # 1682: SaaS fail-closed anomaly guard -- see the API-key branch
        # above for the rationale.
        logger.error(
            "build missing services.kms_tenant_state -- "
            "refusing auth rather than skipping the dark-tenant gate "
            "(user=%s)", db_user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication subsystem misconfigured. Please contact support.",
            headers={"X-Error-Code": "AUTH_SUBSYSTEM_MISCONFIGURED"},
        )

    # GSEC FX.54 part 2 (owner Q37=A + Q49=A) — admin-tier session limits. The
    # Keycloak realm can carry only ONE idle/max pair, so it carries the most
    # permissive tier (the user tier: 60 min / 12 h). The ADMIN tier (30 min /
    # 8 h) is enforced here, server-side, on the interactive human branch only:
    # API-key / service-account tokens returned above and carry no auth_time/sid,
    # so machine traffic is unaffected by construction. Fail-open on a settings
    # read failure; rejections write an audit event. See
    # services/session_tier_enforcement.py.
    try:
        from services import session_tier_enforcement as _fx54
        return _fx54.enforce_admin_session_tier(db, db_user, claims)
    except HTTPException:
        raise
    except Exception as _e:  # noqa: BLE001 — never 500 a request over the tier gate
        logger.warning("FX.54: admin-tier gate failed to evaluate for user=%s "
                       "(fail-open): %s", db_user.id, _e)
        return db_user


def get_current_user(
        db: Session = Depends(get_db),
        claims: dict = Depends(get_current_user_claims),
        request: Request = None,
) -> models.User:
    """
    This dependency gets the user from the database after validating their token.

    Thin FastAPI-facing wrapper over _resolve_current_user. It deliberately
    carries NO ``require_terms`` parameter: as a dependency, any plain
    parameter of it would be bound from the query string, which let any
    client send ``?require_terms=false`` to skip the FX.110 outside-collaborator
    terms gate. The terms leg is always required here; only
    get_current_user_terms_exempt (the terms-accept endpoint) opts out, and it
    does so in code, never from the request.
    """
    return _resolve_current_user(db, claims, request, require_terms=True)


# FX.110 (owner decision Q97=A) — outside collaborators: terms-accept
# dependency. Identical to get_current_user EXCEPT it tells the FX.110
# gate to skip the terms check (require_terms=False) — used ONLY by the
# POST /public/external-collab/terms/accept endpoint so an unaccepted
# external user can still reach the acceptance. All other gates (MFA,
# approval, expiry, active status) still apply.
def get_current_user_terms_exempt(
        db: Session = Depends(get_db),
        claims: dict = Depends(get_current_user_claims),
        request: Request = None,
) -> models.User:
    """FX.110: current user, with the terms leg of the external gate
    waived (everything else — MFA, approval, expiry, active status —
    still enforced by get_current_user below)."""
    return _resolve_current_user(db, claims, request, require_terms=False)


# This alias is useful for clarity in other parts of the app
get_current_db_user = get_current_user


def get_tenant_minio_client(
    current_user: models.User = Depends(get_current_db_user)
) -> MinioClient:
    """
    Provides a tenant-scoped MinioClient instance.

    TPN CS-1 Compliance: Each tenant gets isolated storage in their own bucket.
    The bucket name follows the pattern: tenant-{id}-files

    This dependency should be used for ALL user-facing storage operations to ensure
    proper tenant isolation and CMMC/ISO 27001 compliance.
    """
    return MinioClient(tenant_id=current_user.tenant_id)


def require_active_user(
        current_user: models.User = Depends(get_current_db_user),
):
    """Requires the user to have an 'active' status."""
    if current_user.status != models.UserStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return current_user


def require_onboarding_complete(
        current_user: models.User = Depends(get_current_db_user),
):
    """Requires the user to have completed the onboarding process."""
    if not current_user.onboarding_complete:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Onboarding must be completed to access this resource.",
        )
    return current_user


def require_active_and_onboarded_user(
        request: Request,
        db: Session = Depends(get_db),
        active_user: models.User = Depends(require_active_user),
        onboarded_user: models.User = Depends(require_onboarding_complete),
):
    """A convenience dependency that combines active and onboarded checks.
    Also applies per-plan rate limiting for the tenant."""
    # FX.106 (owner Q93=A) — per-plan + per-API-key rate limits, app
    # level. Platform admins bypass it exactly like the pre-existing
    # per-plan cap (operators administering the platform are not a
    # tenant whose API usage we throttle; matches the nginx LAN/admin
    # bypass). The guard short-circuits with a canonical 429 (standard
    # RateLimit-* headers + JSON body) and records the IP for the
    # auto-block counter; when the guard is OFF (or has nothing to
    # limit — unlimited plan, no key) the pre-existing tenant check
    # below keeps its old behaviour verbatim.
    _fx106_guard_owned = False
    if not getattr(onboarded_user, "is_platform_admin", False):
        try:
            from services import api_rate_limit_guard as _fx106_guard
            from utils.rate_limiter import _get_plan_rate_limit
            from utils.request_principal import get_api_key
            _fx106_plan_limit = _get_plan_rate_limit(
                onboarded_user.tenant_id)
            _fx106_resp = _fx106_guard.check_api_rate_limits(
                request,
                tenant_id=onboarded_user.tenant_id,
                api_key=get_api_key(),
                plan_limit=_fx106_plan_limit,
                db=db,
            )
            if _fx106_resp is not None:
                # GSEC FX.106 p1b: hand the guard's canonical 429 Response
                # straight back. FastAPI short-circuits a dependency that
                # returns a Response, so the client gets the guard's JSON
                # body (limit/window_s/retry_after_s/scope) and its RateLimit-*
                # headers verbatim — no re-synthesised detail and no fake
                # "RateLimit-Limit: 0" default.
                return _fx106_resp
            # The guard ran: it owns the tenant:{id} bucket for this
            # request (double-counting in the legacy check below would
            # trip the plan cap 2x faster).
            _fx106_guard_owned = _fx106_guard.get_guard_settings()[
                "enabled"]
        except HTTPException:
            raise
        except Exception as e:
            # GSEC R-083 — fail-open if the guard is unavailable, but
            # keep it observable: warn at most once per minute.
            _fx106_guard_owned = False
            import time as _time
            now = _time.monotonic()
            if now - _rate_limiter_warn_state.last >= 60.0:
                _rate_limiter_warn_state.last = now
                logger.warning(
                    "require_active_and_onboarded_user: FX.106 rate "
                    "guard FAIL-OPEN (unavailable): %s", e)

    try:
        # FX.106: when the guard is ON it owns the tenant:{id} bucket
        # (same key + plan limit as the old check — double-counting
        # would trip the plan limit 2x faster), so the old tenant check
        # runs only when the guard did not own the bucket.
        if not getattr(onboarded_user, "is_platform_admin", False) \
                and not _fx106_guard_owned:
            from utils.rate_limiter import check_tenant_rate_limit
            check_tenant_rate_limit(request, onboarded_user.tenant_id)
        request.state._rate_limit_tenant_id = onboarded_user.tenant_id
    except HTTPException:
        raise
    except Exception as e:
        # GSEC R-083 — fail-open if the limiter is unavailable, but keep it
        # observable: warn at most once per minute, not once per request.
        import time as _time
        now = _time.monotonic()
        if now - _rate_limiter_warn_state.last >= 60.0:
            _rate_limiter_warn_state.last = now
            logger.warning(
                "require_active_and_onboarded_user: tenant rate limiting "
                "FAIL-OPEN (limiter unavailable): %s", e)

    # Record the principal for code that was never handed it — notably
    # utils.llm_client, whose 17 call sites take prompts and config only and so
    # have no idea who is calling. See utils/request_principal.
    try:
        from utils.request_principal import (
            set_principal, set_channel, classify_channel, get_api_key)
        set_principal(onboarded_user)
        # A browser session reaching this dependency is interactive; an API key
        # arriving here would have set the header or an api-key-* azp.
        set_channel(classify_channel(
            request.headers.get("X-API-Key"),
            getattr(request.state, "_azp", None) or "session"))
        # Gate 33: expose the authorising API key's prefix on request.state so the
        # audit middleware (which runs post-response, outside this request's
        # context) can attribute the event to the SPECIFIC key. get_api_key() was
        # set by get_current_user / require_bearer_or_api_key in this same context.
        _auth_key = get_api_key()
        if _auth_key is not None:
            request.state.api_key_prefix = getattr(_auth_key, "key_prefix", None)
    except Exception:
        pass  # never fail a request over bookkeeping
    return onboarded_user


# ---------------------------------------------------------------------------
# GY.C4b — re-authentication for privileged actions
# ---------------------------------------------------------------------------
# `auth.privileged_reauth_max_age_s` (0 = off, the default) bounds how long a
# login remains acceptable for PRIVILEGED actions: break-glass, compliance-
# profile apply, platform-settings writes, user role changes and API-key
# creation. The check reads the KC access token's `auth_time` claim (when
# the user actually logged in, not when the token was minted) — a token that
# is fresh but whose login is stale is exactly the case a stolen
# long-lived token represents. A stale token gets a 401 whose detail
# carries `error: "reauth_required"` + the max_age hint; the Dashboard
# api.js handler recognises that error and bounces the browser through
# Keycloak with prompt=login&max_age=<N> before retrying the request.
#
# Exemptions (by construction): service/api-key channels never carry a
# human `auth_time`, so when the setting is ON and no auth_time is present
# the check PASSES (fail-open) rather than locking machine traffic out —
# the dependency is only applied to the listed human routes anyway. The
# setting is read on every request (psr cache) so 0 = off takes effect
# without a restart.
def _reauth_max_age_s(db: Session) -> int:
    from services import platform_settings as psr
    try:
        return int(psr.get_setting(psr.REAUTH_SETTING_KEY, db=db) or 0)
    except Exception:  # noqa: BLE001 — a settings failure must not 500 a
        # privileged route; fail-open to the current behaviour (off).
        logger.warning("require_fresh_auth: reauth setting read failed",
                       exc_info=True)
        return 0


def require_fresh_auth(
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """GY.C4b — re-auth gate for privileged actions (see block comment).

    Raises 401 with `{"error": "reauth_required",
    "reauth_max_age_s": N, "detail": "..."}` when the token's `auth_time`
    is older than the configured max age. Returns the user otherwise."""
    max_age = _reauth_max_age_s(db)
    if max_age <= 0:
        return current_user
    try:
        # UNVERIFIED decode: the token was already signature/aud/issuer
        # validated upstream (get_current_user resolved the user); this only
        # reads auth_time, so no second JWKS fetch.
        claims = jwt.get_unverified_claims(token)
        auth_time = claims.get("auth_time")
        if auth_time is None:
            # No human auth_time (service token / api-key channel): the
            # gate targets human logins, so pass through.
            return current_user
        age = time.time() - int(auth_time)
        if age <= max_age:
            return current_user
    except Exception:  # noqa: BLE001
        logger.warning("require_fresh_auth: claims read failed",
                       exc_info=True)
        return current_user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "reauth_required",
                "reauth_max_age_s": max_age,
                "detail": "This action requires a recent login. Re-authenticate "
                          "and retry."},
        headers={"WWW-Authenticate": "Bearer"},
    )


# GY.C16 — time-boxed, read-only, scoped auditor access. An auditor is a
# NON-admin account holding a valid ComplianceAuditorGrant: it may read the
# compliance surfaces (C12 conformance results, evidence bundle, audit
# search, configuration) and nothing else. Auditors get 403 on every write
# method (except read-generating exports on the allowlist below) and on all
# data-content routes unless their grant explicitly sets
# data_content_granted. Every auditor request writes an `auditor.access`
# audit event (never raising — see services.auditor_access.record_access).
# Paths with the /v1 prefix stripped (matched against request.url.path
# after the same stripping).
AUDITOR_WRITE_EXPORT_ALLOWLIST = (
    "/compliance/evidence-bundle",  # POST = generate+download the signed
    # bundle (a read, not a mutation)
    "/compliance/submission-packages",  # POST = request the signed submission
    # package (reads existing audit data, produces a signed artifact; mutates
    # no tenant data) — exact-path match, so the GET /{id} and GET "" routes
    # under the same prefix are unaffected and no longer write path is allowed
)


def require_auditor_or_admin(
        request: Request,
        scope: str = "platform",
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Platform admin OR a holder of a valid, in-scope, unexpired
    ComplianceAuditorGrant.

    `scope` is the surface's scope: "platform" (whole-platform compliance
    reads) admits only platform-scoped grants; "tenant" admits platform
    grants plus the user's own tenant. Expired or revoked grants (and
    writes outside the export allowlist) → 403, always audited.
    """
    try:
        from services import auditor_access
    except ImportError:  # FX.162.p5 — CE carve: auditor grants are a paid
        # compliance surface. CE fallback = "not an auditor": platform admins
        # keep their normal rights, everyone else is denied as before.
        if not current_user.is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Auditor access required (or platform admin).",
                headers={"X-Error-Code": "AUDITOR_GRANT_REQUIRED"})
        return current_user

    path = request.url.path
    if path.startswith("/v1/"):
        path = path[3:]
    if not current_user.is_platform_admin:
        grant = auditor_access.active_grant_for(db, current_user.id)
        if grant is None:
            auditor_access.record_access(
                db, user_id=current_user.id, tenant_id=current_user.tenant_id,
                route=request.url.path, method=request.method,
                scope=scope, status=403,
                details={"reason": "no_active_auditor_grant"})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Auditor access required (or platform admin).",
                headers={"X-Error-Code": "AUDITOR_GRANT_REQUIRED"})
        if scope == "platform":
            in_scope = grant.scope == "platform"
        else:
            in_scope = grant.scope == "platform" or (
                grant.scope == "tenant"
                and grant.tenant_id == current_user.tenant_id)
        if not in_scope:
            auditor_access.record_access(
                db, user_id=current_user.id, tenant_id=current_user.tenant_id,
                route=request.url.path, method=request.method,
                scope=scope, status=403, grant_id=grant.id,
                details={"reason": "grant_out_of_scope",
                         "grant_scope": grant.scope})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Auditor grant is out of scope for this resource.",
                headers={"X-Error-Code": "AUDITOR_GRANT_OUT_OF_SCOPE"})
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and path not in AUDITOR_WRITE_EXPORT_ALLOWLIST:
            auditor_access.record_access(
                db, user_id=current_user.id, tenant_id=current_user.tenant_id,
                route=request.url.path, method=request.method,
                scope=scope, status=403, grant_id=grant.id,
                details={"reason": "write_not_allowed"})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Auditor access is read-only.",
                headers={"X-Error-Code": "AUDITOR_WRITE_FORBIDDEN"})
        auditor_access.record_access(
            db, user_id=current_user.id, tenant_id=current_user.tenant_id,
            route=request.url.path, method=request.method,
            scope=scope, status=200, grant_id=grant.id)
        request.state.auditor_access = {
            "user_id": current_user.id,
            "grant_id": grant.id,
            "grant_scope": grant.scope,
        }
    return current_user


def _enforce_auditor_data_content(
        request: Request,
        db: Session,
        current_user: models.User,
) -> models.User:
    """GY.C16 data-content gate core, shared by the user-funnel variant
    (``require_auditor_data_content``) and the bearer/API-key principal
    variant (``require_auditor_data_content_principal``). A NON-admin
    holding a valid auditor grant may only reach data content when their
    grant explicitly sets ``data_content_granted``; platform admins and
    ordinary (non-auditor) users pass through with their normal rights."""
    try:
        from services import auditor_access
    except ImportError:  # FX.162.p5 — CE carve: no auditor grants exist, so
        # nobody is an auditor and every caller keeps its normal rights.
        return current_user

    if current_user.is_platform_admin:
        return current_user
    grant = auditor_access.active_grant_for(db, current_user.id)
    if grant is None:
        return current_user
    if grant.data_content_granted:
        auditor_access.record_access(
            db, user_id=current_user.id, tenant_id=current_user.tenant_id,
            route=request.url.path, method=request.method,
            scope=grant.scope, status=200, grant_id=grant.id,
            details={"surface": "data_content"})
        request.state.auditor_access = {
            "user_id": current_user.id,
            "grant_id": grant.id,
            "grant_scope": grant.scope,
        }
        return current_user
    auditor_access.record_access(
        db, user_id=current_user.id, tenant_id=current_user.tenant_id,
        route=request.url.path, method=request.method,
        scope=grant.scope, status=403, grant_id=grant.id,
        details={"reason": "data_content_not_granted"})
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Auditor access does not include tenant data content.",
        headers={"X-Error-Code": "AUDITOR_DATA_CONTENT_FORBIDDEN"})


def require_auditor_data_content(
        request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Data-content gate for auditor accounts (files/cogs/run outputs/
    chat surfaces): a NON-admin holding a valid auditor grant may only
    reach data content when their grant explicitly sets
    data_content_granted. Platform admins and ordinary (non-auditor)
    users pass through with their normal rights."""
    return _enforce_auditor_data_content(request, db, current_user)


def require_active_onboarded_parent_user(
        current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Requires the user to be a parent user (admin) of their tenant."""
    if current_user.parent_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to perform this action.",
        )
    return current_user


def get_unregistered_user(
        db: Session = Depends(get_db),
        claims: dict = Depends(get_current_user_claims),
):
    """
    Ensures a user is authenticated via Keycloak but does NOT yet exist in the local DB.
    Used for the final step of the registration/onboarding process.
    """
    db_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=claims['sub'])
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This user account has already been registered."
        )
    return claims


def get_unregistered_user_with_token(
        db: Session = Depends(get_db),
        token: str = Depends(oauth2_scheme),
):
    """
    Ensures a user is authenticated via Keycloak but does NOT yet exist in the local DB.
    Returns a tuple of (claims, bearer_token) for use in onboarding flows that need
    to pass the token to other services (worker containers, etc.).
    """
    claims = keycloak_get_current_user_claims(token)
    db_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=claims['sub'])
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This user account has already been registered."
        )
    return claims, token


def get_keycloak_user_with_token_for_signup(
        token: str = Depends(oauth2_scheme),
):
    """LDF-4 §1.3/§1.4 — onboarding-only dep that returns Keycloak
    claims + token WITHOUT a DB-existence check.

    The legacy `get_unregistered_user_with_token` 400s with
    "user already registered" the moment a `users` row exists for
    this keycloak_sub. That kept retries from ever hitting the
    create-tenant handler — even when a prior 500 left the user in
    a recoverable partial-onboarding state. The handler is now
    transactional + idempotent, so this dep just gets us the claims
    and lets the handler decide between idempotent return / recovery
    / fresh-create.
    """
    claims = keycloak_get_current_user_claims(token)
    return claims, token


from audit import log_impersonation

# Rate limiting for impersonation - track attempts per service
_impersonation_attempts: dict = {}  # {service_id: [(timestamp, user_id), ...]}
IMPERSONATION_RATE_LIMIT = 100  # Max impersonations per service per minute
IMPERSONATION_WINDOW_SECONDS = 60


def _audit_impersonation_denied(db: Session, claims: dict, reason: str,
                                requested_keycloak_id: str, tenant_id=None,
                                service_db_user_id=None):
    """GSEC R-004: record a DENIED impersonation attempt (AU-2).

    Never raises — audit failures must not mask the 403.
    """
    try:
        from audit import log_security_event
        log_security_event(
            db=db,
            action=models.AuditAction.AUTH_IMPERSONATION,
            user_id=service_db_user_id,
            tenant_id=tenant_id,
            metadata={
                "outcome": "denied",
                "reason": reason,
                "service_keycloak_sub": claims.get('sub', ''),
                "azp": claims.get('azp', ''),
                "requested_keycloak_id": requested_keycloak_id,
            },
        )
    except Exception:
        pass


def _verify_impersonation_service_principal(db: Session, claims: dict,
                                            requested_keycloak_id: str = ''):
    """GSEC R-004: verify the caller of the impersonation dependency is a
    verified service principal and return ``(service_db_user_id, tenant_scope)``.

    A service-principal token is a Keycloak SERVICE ACCOUNT token
    (client-credentials flow, ``sub`` = ``service-account-<uuid>``) issued by
    either:
      - the platform service client (``azp`` == ``KEYCLOAK_SERVICE_CLIENT_ID``)
        — cross-tenant by platform design (same trust basis as
        ``require_service_account_token``); ``tenant_scope`` is None, and
        endpoint-level run/tenant scoping still applies;
      - an API key's service account (``azp`` == ``api-key-<keycloak-client-id>``)
        — scoped to the owning user's tenant; ``tenant_scope`` is that
        tenant_id and the impersonated user must belong to it.

    An interactive user token (browser session) is never a service account:
    its ``sub`` is a plain user ID, so the prefix check alone refuses it.
    Raises 403 (with a denied-outcome audit event) for anything else.
    """
    sub = claims.get('sub', '')
    azp = claims.get('azp', '')

    def _deny(reason, tenant_id=None, service_db_user_id=None):
        _audit_impersonation_denied(
            db, claims, reason,
            requested_keycloak_id=requested_keycloak_id,
            tenant_id=tenant_id,
            service_db_user_id=service_db_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Impersonation is only available to verified service principals.",
        )

    if not sub.startswith('service-account-'):
        _deny('caller is not a service account (interactive user tokens are refused)')

    expected_service_client = os.getenv('KEYCLOAK_SERVICE_CLIENT_ID', 'platform-service')
    if azp == expected_service_client:
        return None, None
    if azp.startswith('api-key-'):
        db_api_key = crud.get_api_key_by_keycloak_client_id(db, keycloak_client_id=azp)
        if not db_api_key:
            _deny('API key not found or revoked')
        owner = crud.get_user(db, user_id=db_api_key.user_id)
        if not owner:
            _deny('API key owner not found', service_db_user_id=db_api_key.user_id)
        return owner.id, owner.tenant_id
    _deny('token is not issued by the platform service client or an API key')


async def get_impersonated_user_from_service_account(
        x_impersonate_user_id: str = Header(...),
        db: Session = Depends(get_db),
        claims: dict = Depends(get_current_user_claims),  # This still validates the service's token
) -> models.User:
    """
    Allows a service (a worker container, scheduled job, etc.) to impersonate a user for API calls.
    The service provides its own valid token and the Keycloak ID of the user to act as.

    GSEC R-004: the caller MUST be a verified service principal — a Keycloak
    service-account token from the platform service client or an API key's
    service account (see ``_verify_impersonation_service_principal``). Any
    other authenticated token (notably interactive user sessions) is refused
    with 403. When the service token is tenant-scoped (API-key tokens), the
    impersonated user must belong to that tenant.

    CMMC/ISO 27001 Compliance: AC-3 (Access Enforcement), AU-2 (Audit Events)
    - Rate limited to prevent abuse
    - All impersonation events (allowed AND denied) are logged for audit
    """
    import time

    service_id = claims.get('sub', 'unknown')
    current_time = time.time()

    # Rate limiting check
    if service_id not in _impersonation_attempts:
        _impersonation_attempts[service_id] = []

    # Clean old attempts outside the window
    _impersonation_attempts[service_id] = [
        (ts, uid) for ts, uid in _impersonation_attempts[service_id]
        if current_time - ts < IMPERSONATION_WINDOW_SECONDS
    ]

    # Check rate limit
    if len(_impersonation_attempts[service_id]) >= IMPERSONATION_RATE_LIMIT:
        logger.warning(
            f"SECURITY: Impersonation rate limit exceeded for service {service_id}. "
            f"Attempted to impersonate {x_impersonate_user_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Impersonation rate limit exceeded. Please try again later.",
        )

    # Record this attempt
    _impersonation_attempts[service_id].append((current_time, x_impersonate_user_id))

    # GSEC R-004: the caller must be a verified service principal. Refuses
    # interactive user tokens with 403 (audited, outcome=denied).
    service_db_user_id, scoped_tenant_id = _verify_impersonation_service_principal(
        db, claims, requested_keycloak_id=x_impersonate_user_id,
    )

    db_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=x_impersonate_user_id)
    if not db_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {x_impersonate_user_id} not found for impersonation.",
        )

    # GSEC R-004: a tenant-scoped service token may only impersonate a user
    # inside its own tenant.
    if scoped_tenant_id is not None and db_user.tenant_id != scoped_tenant_id:
        _audit_impersonation_denied(
            db, claims,
            'impersonated user is outside the service token\'s tenant scope',
            requested_keycloak_id=x_impersonate_user_id,
            tenant_id=scoped_tenant_id,
            service_db_user_id=service_db_user_id,
        )
        logger.warning(
            f"SECURITY: Impersonation denied — token scoped to tenant "
            f"{scoped_tenant_id} attempted to impersonate a user of tenant "
            f"{db_user.tenant_id} (service={service_id}, target="
            f"{x_impersonate_user_id})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Impersonated user is outside this service token's tenant scope.",
        )

    # Audit log the impersonation (CMMC AU-2 requirement)
    # Get service user for audit — for a real Keycloak service account there
    # is no local users row for the `sub`; use the resolved DB identity from
    # the principal check when we have one, else fall back to the sub lookup.
    audit_service_user_id = service_db_user_id
    if audit_service_user_id is None:
        service_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=service_id)
        audit_service_user_id = service_user.id if service_user else None
    if audit_service_user_id is not None:
        log_impersonation(
            db=db,
            service_user_id=audit_service_user_id,
            impersonated_user_id=db_user.id,
            tenant_id=db_user.tenant_id,
            metadata={"service_keycloak_id": service_id}
        )

    return db_user


def require_capability(resource_type: models.ResourceType, capability_name: str):
    """
    A dependency factory to protect endpoints based on capability permissions.
    Platform admins bypass all permission checks (god mode).

    Args:
        resource_type: The resource type (e.g., models.ResourceType.SECRET)
        capability_name: The capability name (e.g., "create", "view", "edit", "delete")

    Example usage:
        @router.post("/secrets")
        def create_secret(
            current_user: models.User = Depends(require_capability(models.ResourceType.SECRET, "create"))
        ):
            ...
    """
    def dependency(
            db: Session = Depends(get_db),
            current_user: models.User = Depends(require_active_and_onboarded_user)
    ) -> models.User:
        # Gates 25-29: fold require_capability into the single authorize()
        # chokepoint. authorize() is a SUPERSET of the old inline role check — it
        # resolves the user's role capabilities (CustomRole.capabilities) AND
        # direct/team/role_id ResourcePermission grants — so one resolver now
        # covers every path. No platform-admin bypass (ISO/DoD tenant isolation,
        # Gate 17b): access is by the user's own role/grants only; platform-scoped
        # endpoints use require_platform_admin instead.
        from resource_permissions import authorize
        if authorize(db, current_user, resource_type, capability_name):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have the required '{resource_type.value}:{capability_name}' capability.",
        )

    return dependency


def require_platform_capability(capability_name: str):
    """
    A dependency factory to gate an endpoint on a PLATFORM-scoped capability.

    Holds the named grant in `platform_capabilities`, OR is_platform_admin.
    Mirrors `require_capability` but for platform scope, not cog/role scope
    (delegated-stack-authoring task 2.1). Use as a router-wide dependency to
    open an admin-only surface to delegated authors without conferring full
    admin.

    Example usage:
        router = APIRouter(
            dependencies=[Depends(require_platform_capability("stack:author"))]
        )
    """
    def dependency(
            db: Session = Depends(get_db),
            current_user: models.User = Depends(require_active_and_onboarded_user)
    ) -> models.User:
        import permissions
        if permissions.check_user_platform_capability(
            db, current_user.id, capability_name
        ):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not hold the required '{capability_name}' platform capability.",
        )

    return dependency


# ---------------------------------------------------------------------------
# 2400GX-c3 J2b (30.6.3) — key-aware variant of the router-wide stack:author
# gate, used ONLY by routers/persistent_stacks/__init__.py on the parent
# persistent-stacks router.
#
# WHY: FastAPI resolves ROUTER-level dependencies before handler-level ones, so
# the bearer-only chain above (require_active_and_onboarded_user -> ... ->
# get_current_user_claims -> oauth2_scheme, auto_error) answered 401 "Not
# authenticated" for a raw `X-API-Key` stack-pinned key BEFORE the handler's own
# `require_bearer_or_api_key` funnel (the six c3-J2b operational routes) could
# read it. A stack-pinned key (an ordinary ApiKey row whose deploy_token_stack_id
# is set; formerly the per-stack "deploy token") must be able to operate
# its own stack (design c3-J2b: the six routes accept the raw key; confined
# by services/app_key_scope.check_stack_key_scope: own-stack pin + the key's
# stack:* scopes read from STACK_SCOPE_ALLOWLIST, default-deny).
#
# NO WIDENING (the security MUSTs):
#  * a bearer caller runs the SAME chain, same helpers, same order as the
#    dependency above — active + onboarding + rate limits + the
#    check_user_platform_capability verdict are untouched;
#  * the key leg runs ONLY when there is no bearer at all, and ONLY for a key
#    that is a stack-pinned key (deploy_token_stack_id set — the row column,
#    via services/app_key_scope.is_stack_pinned_key) whose (method, path)
#    matches one of the SIX operational templates (services/app_key_scope.
#    stack_key_route_rules — no second route table here) AND for which
#    check_stack_key_scope passed (own-stack pin + the key's stack:* scopes).
#    Any other key — an app key, a plain scoped key, an UNPINNED key that
#    carries a stack:* scope (denied outright: stack_scope_without_pin), a
#    route outside the six — resolves to None and the gate fails closed with
#    the bearer 401;
#  * revoked / soft-deleted keys never resolve (crud/api_key_cud.get_api_key_by_key_prefix
#    filters deleted_at), expired + disabled keys are rejected by
#    _enforce_api_key_policy inside the funnel, an inactive owner by
#    require_active_user below;
#  * the capability check itself is the identical call, so a key owner that
#    does not hold stack:author still gets the same 403.
# ---------------------------------------------------------------------------
def _require_stack_gate_caller(
        request: Request = None,
        db: Session = Depends(get_db),
) -> models.User:
    """Resolve the caller for the router-wide stack:author gate.

    Runs the SAME funnel the six c3-J2b handlers use (`require_bearer_or_api_key`
    with the request's own Authorization / X-API-Key headers), but does NOT let
    that funnel's own 401 answer the request: an unresolvable caller returns
    None and the gate raises the bearer chain's 401 ("Not authenticated"), so an
    unauthenticated call keeps the exact status + detail it had before the swap.
    """
    _ov = _stack_gate_chain_override()
    if _ov is not None:
        # A test harness satisfied the bearer-only chain the OLD gate ran: its
        # override IS the resolved caller (identity + active + onboarding legs
        # already encoded). Production never overrides a dependency.
        return _ov()
    _headers = request.headers if request is not None else {}
    _auth = _headers.get("Authorization")
    _key = _headers.get("X-API-Key")
    if not (_auth or _key):
        # No credential header at all: the bearer chain's own oauth2_scheme
        # auto-error answered this request before (401 "Not authenticated").
        # Answer it the same way WITHOUT running the funnel - the funnel would
        # write a failed-auth audit row + count the IP for the FX.106 auto-block
        # a second time for a request the old gate never let that far.
        return None
    try:
        return require_bearer_or_api_key(
            authorization=_auth,
            # The key leg runs ONLY when there is no Authorization header at
            # all: a bearer-bearing request is resolved by the bearer chain,
            # never by a key (no key fallback, no key denial).
            x_api_key=(None if _auth else _key),
            request=request, db=db)
    except HTTPException as _exc:
        if not _auth and _exc.status_code == 401 \
                and "Provide either Bearer token or X-API-Key" in str(_exc.detail):
            return None  # no usable credential: the gate's bearer 401 answers
        raise  # the funnel's own verdict (expired/disabled/scope/bearer) IS the answer


def _stack_gate_chain_override():
    """The test-harness principal for the bearer-only chain the OLD gate ran.

    Neighbour suites satisfy that chain with `app.dependency_overrides`
    (require_active_and_onboarded_user / require_active_user / get_current_user
    -> a principal). Called as a plain python function an override would not
    bind, so consult it explicitly: when the harness has overridden the chain
    symbol, its result IS the resolved caller (the override already encodes the
    active + onboarding legs and the caller identity). Production never
    overrides a dependency, so this returns None on every live request.
    """
    from main import app
    ov = getattr(app, "dependency_overrides", None)
    if not ov:
        return None
    # get_current_db_user IS get_current_user (same object), so one lookup
    # covers both names of the chain.
    return (ov.get(require_active_and_onboarded_user)
            or ov.get(require_active_user) or ov.get(get_current_user))


def require_stack_author_capability_or_deploy_key(capability_name: str):
    """Router-wide stack:author gate that also admits a stack-pinned API key.

    FINDING (seed routes, reported not fixed): the three seed routes
    (POST /v1/stacks/{id}/seed, .../seed/{seed_id}/parts, .../seed/{seed_id}/commit)
    ARE in services/app_key_scope.STACK_SCOPE_ALLOWLIST under the `stack:seed`
    scope, but they are NOT among the SIX gate templates
    (services/app_key_scope.stack_key_route_rules), and routers/persistent_stacks/
    stack_seed.py is included into this gated parent router. So a seed-only
    stack-pinned key gets the gate's bearer 401 ("Not authenticated") before
    check_stack_key_scope ever runs — pre-existing behaviour, kept deliberately:
    the gate is NOT widened here.

    The caller is resolved by the SAME funnel the six c3-J2b handlers already
    use (`require_bearer_or_api_key`): its interactive branch delegates to
    `_resolve_current_user(db, claims, request, require_terms=True)` — the
    exact helper `get_current_user` runs — so a bearer user keeps every gate
    they keep today (FX.110 terms/MFA/expiry, FX.54 admin tier, FX.101 tenant
    IP allowlist), and its key branch runs the key policy (expiry/disabled,
    the stack-key / app-key route scope, owner-ACTIVE). The active + onboarding
    + FX.106 rate-limit legs and the capability verdict are then applied by the
    SAME helpers the bearer-only chain above uses, with the same 403 detail.
    """

    def dependency(
            db: Session = Depends(get_db),
            request: Request = None,
            current_user: models.User = Depends(_require_stack_gate_caller),
    ) -> models.User:
        import permissions

        _chain_override = _stack_gate_chain_override()
        if _chain_override is not None:
            # A test harness is satisfying the bearer-only chain the OLD gate
            # ran (require_active_and_onboarded_user / require_active_user /
            # get_current_user overridden): honour that harness exactly as the
            # old gate did - the override already encodes the active +
            # onboarding legs and the caller identity, so the caller is the
            # resolved principal and the capability verdict below is the
            # answer. Production never overrides a dependency, so this leg
            # never exists on a live request (fail-closed by construction).
            user = current_user if current_user is not None else _chain_override()
        else:
            if current_user is None or not _deploy_token_admissible(
                    db, request, current_user):
                # No bearer and no admissible stack-pinned key -> the bearer gate's
                # own 401 (same status + same WWW-Authenticate as oauth2_scheme).
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Same active + onboarding + rate-limit legs the chain above runs,
            # applied to the resolved caller.
            user = require_active_and_onboarded_user(
                request, db=db,
                active_user=require_active_user(current_user=current_user),
                onboarded_user=require_onboarding_complete(current_user=current_user),
            )
            if getattr(user, "status_code", None):
                return user  # the FX.106 guard's canonical 429 Response

        if permissions.check_user_platform_capability(
            db, user.id, capability_name
        ):
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not hold the required '{capability_name}' platform capability.",
        )

    dependency.__name__ = "require_stack_author_capability_or_deploy_key"
    return dependency


def _deploy_token_route_rules():
    """The SIX operational stack templates a stack-pinned key may hit.

    30.6.3 c3-J2b merge: this file keeps NO second route table — the grammar
    lives in services/app_key_scope.stack_key_route_rules (compiled from
    STACK_SCOPE_ALLOWLIST + the implicit GET /v1/stacks/{stack_id}), so the
    gate and the scope check can never drift apart. Kept as a thin alias
    because tests and the gate import this name. Anything outside those six
    is denied by construction (default deny); the seed routes are NOT among
    them (see the gate note below).
    """
    from services.app_key_scope import stack_key_route_rules
    return stack_key_route_rules()


def _deploy_token_admissible(db: Session, request, current_user: models.User) -> bool:
    """True when `current_user` may pass the stack:author gate on this request.

    A bearer-bearing request always may — the funnel above already ran every
    bearer gate (FX.110 terms/MFA/expiry, FX.54 admin tier, FX.101 tenant IP
    allowlist, owner-ACTIVE). A request that resolved through the X-API-Key
    funnel may ONLY when the key is a stack-pinned key (deploy_token_stack_id
    set — read via services/app_key_scope.is_stack_pinned_key, the row COLUMN,
    never a scope string), the (method, path) matches one of the SIX
    operational templates (services/app_key_scope.stack_key_route_rules), and
    check_stack_key_scope accepts it (own-stack pin + per-key stack:* scopes;
    it raises 403 deploy_token_scope_denied on a cross-stack / wrong-scope
    call, and it DENIES a non-pinned key that carries an exact known stack
    scope — stack_scope_without_pin). Everything else returns False and the
    caller gets the bearer 401 — the gate never widens for a key.
    """
    from services.app_key_scope import (
        _request_method, _request_path, check_stack_key_scope, is_stack_pinned_key,
    )
    from crud import api_key_cud

    if request is None:
        return False
    if request.headers.get("Authorization"):
        return True  # bearer leg: the funnel ran the interactive gates

    raw = request.headers.get("X-API-Key")
    if not raw:
        # FIX-GATE-M3 (finding m3): FAIL CLOSED. The KEY leg is admissible
        # ONLY when an X-API-Key header is actually present. An ALLOW branch
        # keyed on the ABSENCE of a credential is a fail-open shape even when
        # today it is reachable only via a test dependency override; a
        # credential-less caller now gets the bearer gate's 401 like every
        # other non-admissible request. The bearer leg above is unchanged.
        return False

    key = None
    try:
        from utils.request_principal import get_api_key
        key = get_api_key()
    except Exception:
        key = None
    if key is None:
        # BUG-1027p gotcha: a contextvar write in a threadpool thread may not
        # be visible here — re-resolve the row the funnel matched (ids only).
        key = api_key_cud.get_api_key_by_key_prefix(db, key_prefix=raw)
    if key is None or not is_stack_pinned_key(key):
        return False  # not a stack-pinned key -> the bearer gate's 401

    path = _request_path(request)
    if path.endswith("/"):
        path = path.rstrip("/") or "/"
    method = _request_method(request)
    if not any(rule.method == method and rule.regex.match(path)
               for rule in _deploy_token_route_rules()):
        return False  # outside the six -> the bearer gate's 401

    # Own-stack pin + per-key stack:* scopes (403 deploy_token_scope_denied
    # otherwise; the gate does NOT catch that 403 - it is the answer, never
    # downgraded to the bearer 401).
    check_stack_key_scope(db, request, key)
    return True


def require_entitlement(capability_name: str):
    """
    A dependency factory to gate an endpoint on a deployment/tenant LICENSE
    entitlement — a DIFFERENT axis from per-user RBAC (`require_capability`).

    Resolves the effective EntitlementSet via `utils.entitlements` (SaaS Plan,
    signed license, or Core baseline) and checks the named capability. Community
    Edition grants every capability, so this never raises. Both
    this and `require_capability` apply where both are present.

    Example usage:
        router = APIRouter(
            dependencies=[Depends(require_entitlement("sso"))]
        )
    """
    def dependency(
            db: Session = Depends(get_db),
            current_user: models.User = Depends(require_active_and_onboarded_user)
    ) -> models.User:
        from utils import entitlements
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == current_user.tenant_id
        ).first()
        es = entitlements.get_entitlements(db, tenant)
        decision = entitlements.evaluate_feature(capability_name, es)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=decision.reason or (
                    f"This deployment's license does not include '{capability_name}'."
                ),
            )
        return current_user

    return dependency


def require_cog_permission(required_level: models.PermissionLevel):
    """
    A dependency factory to protect Cog-related endpoints.
    Platform admins bypass all permission checks (god mode).
    """

    def dependency(
            cog_id: int,
            db: Session = Depends(get_db),
            current_user: models.User = Depends(require_active_and_onboarded_user)
    ) -> models.User:
        # No platform-admin bypass (ISO/DoD tenant isolation): cog access is by
        # the user's own grants only.
        if not crud.check_user_permission_on_cog(db, user_id=current_user.id, cog_id=cog_id,
                                                 required_level=required_level):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User does not have the required '{required_level.value}' permission on this Cog.",
            )
        return current_user

    return dependency


def _check_api_key_expiry(db, db_api_key):
    """GSEC R-020/R-075 — reject a RESOLVED API key whose expires_at is in
    the past (401) and audit it (AUTH_API_KEY_EXPIRED). Applies to ALL keys.
    Naive timestamps are treated as UTC.

    This is the KEY-VALIDITY half of :func:`_enforce_api_key_policy`, extracted
    so public credential surfaces that must enforce expiry WITHOUT the
    route-based app-scope check (e.g. the public token-exchange endpoint, which
    is its own route and cannot be matched against an app's route allowlist)
    can reuse the exact same rule. Non-expired keys are a no-op.

    Returns None when the key is valid (not expired); raises HTTPException(401)
    when expired. Audit write failures never mask the 401.
    """
    from datetime import datetime, timezone
    # GSEC FX.152 part 2 (owner decision Q139=B) — a rotated key is valid for
    # `api_keys.rotation_grace_hours` after the rotation and is rejected
    # exactly like an expired key once that window closes. The first rejection
    # past grace soft-disables the row (disabled_at stamped, never deleted) and
    # writes the API_KEY_GRACE_EXPIRED audit row once; every later rejection
    # falls through to the disabled branch below, so no per-request audit row.
    # 1634d: services.api_key_grace is Paid-only (manifest-denied on CE); this
    # ran for every api-key-authenticated request, so an unguarded import
    # would 500 every such request on CE. On CE there is no grace window to
    # enforce — rotate_api_key() in routers/api_keys.py never stamps
    # grace_until there — so the check is skipped.
    from utils.paid_import import optional_paid
    api_key_grace = optional_paid("services.api_key_grace")
    if api_key_grace is not None and api_key_grace.enforce_grace(db, db_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key rotation grace window has ended."
        )

    # FX.114 (owner Q101=A) — a key the lifecycle job or a tenant admin
    # disabled (disabled_at stamped) is rejected exactly like an
    # expired/revoked one: 401 + audit. Distinct from soft-delete
    # (deleted_at, which the lookup itself filters out).
    disabled_at = getattr(db_api_key, "disabled_at", None)
    if disabled_at is not None:
        try:
            db.add(models.AuditEvent(
                # tenant_id stays NULL: audit_events.tenant_id FKs tenants.id
                # and there is no tenant 0, so `tenant_id=0` FK-violated,
                # poisoned the request's session (PendingRollbackError) and
                # turned the 401 into a 500. Same NULL-actor posture as
                # _log_auth_failure below (:1881).
                user_id=getattr(db_api_key, "user_id", None),
                event_name=models.AuditAction.API_KEY_AUTO_DISABLED.value,
                details={
                    "api_key_id": getattr(db_api_key, "id", None),
                    "key_prefix": getattr(db_api_key, "key_prefix", None),
                    "reason": "disabled_key_rejected",
                    "disabled_at": str(disabled_at),
                },
            ))
            db.commit()
        except Exception:  # audit failure must not mask the 401
            pass
        logger.info("API key rejected: disabled (key=%s)",
                    getattr(db_api_key, "id", None))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is disabled."
        )
    expires_at = getattr(db_api_key, "expires_at", None)
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        try:
            db.add(models.AuditEvent(
                # tenant_id NULL, same reason as the disabled branch above:
                # there is no tenant 0, so tenant_id=0 FK-violated and left
                # the request session needing rollback (PendingRollbackError
                # on the next ORM read -> 500 instead of the 401).
                user_id=getattr(db_api_key, "user_id", None),
                event_name=models.AuditAction.AUTH_API_KEY_EXPIRED.value,
                details={
                    "api_key_id": getattr(db_api_key, "id", None),
                    "key_prefix": getattr(db_api_key, "key_prefix", None),
                    "expires_at": str(expires_at),
                },
            ))
            db.commit()
        except Exception:  # audit failure must not mask the 401
            pass
        logger.info("API key rejected: expired (key=%s)", getattr(db_api_key, "id", None))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired."
        )
    return None


def _enforce_api_key_policy(db, db_api_key, request=None):
    """GX.A3 + SEC-1-002 — post-resolution policy for a RESOLVED API key.

    (a) Expiry: a key whose expires_at is in the past is rejected with 401
        and audited (AUTH_API_KEY_EXPIRED). Applies to ALL keys, not just
        app keys. Naive timestamps are treated as UTC.
    (b) App scoping: a key bound to an app (app_id set) is checked against
        the app's route/resource allowlist (default deny; see
        services/app_key_scope.check_app_scope).

    Called from both auth funnels (require_service_api_key and
    require_bearer_or_api_key) right after the key row is resolved and
    verified, BEFORE any user is returned.
    """
    # 2400GX-sec1: enforce at most once per request per key (the Bearer path
    # can reach both require_bearer_or_api_key and _resolve_current_user).
    _state = getattr(request, "state", None)
    _key_id = getattr(db_api_key, "id", None)
    if (_state is not None and _key_id is not None
            and getattr(_state, "_api_key_policy_enforced_id", None) == _key_id):
        return
    # (a) Expiry — shared with the public token-exchange endpoint (R-020).
    _check_api_key_expiry(db, db_api_key)
    # 30.6.3 c3-J2 merge: the stack-key scope check runs for EVERY key, not
    # only a pinned one — it is a no-op for an ordinary key, and it now also
    # catches the shapes a key alone cannot decide: a NON-pinned key carrying
    # an exact known stack:* scope (stack_scope_without_pin) and a key that is
    # BOTH app-bound and stack-pinned (deploy_token_with_app). Order is
    # unchanged: AFTER expiry, BEFORE the app-key branch.
    from services.app_key_scope import check_stack_key_scope
    check_stack_key_scope(db, request, db_api_key)
    app_id = getattr(db_api_key, "app_id", None)
    if app_id is not None:
        from services.app_key_scope import check_app_scope
        check_app_scope(db, request, db_api_key)
        # GX.A3b: stamp the app on the request principal so audit rows
        # attribute the action to the APP (see utils.request_principal).
        try:
            from utils.request_principal import set_api_key, set_app_id
            set_app_id(app_id)
            set_api_key(db_api_key)
        except Exception:  # bookkeeping must never break auth
            pass
    if _state is not None and _key_id is not None:
        try:
            _state._api_key_policy_enforced_id = _key_id
        except Exception:  # bookkeeping only
            pass


def require_service_api_key(
        x_api_key: str = Header(...),
        request: Request = None,
        db: Session = Depends(get_db)
) -> models.User:
    """
    Authenticates a service-to-service call using a static API key.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key is missing."
        )

    # RTA401: a JWT-shaped credential gets the JWT verdict ONLY — never the
    # key-prefix lookup / bcrypt compare below (it is not an API key).
    _jwt_shaped, _jwt_detail = _jwt_credential_verdict(x_api_key, db)
    if _jwt_shaped:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_jwt_detail or "Invalid API Key.",
        )

    db_api_key = crud.get_api_key_by_key_prefix(db, key_prefix=x_api_key)

    if not db_api_key or not verify_api_key(x_api_key, db_api_key.hashed_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key."
        )

    _enforce_api_key_policy(db, db_api_key, request)

    user = crud.get_user(db, user_id=db_api_key.user_id)
    if not user or user.status != models.UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service account is inactive or not found."
        )
    return user


def _try_stack_identity_bearer(token: str, request, db: Session):
    """GX.S3 — resolve a Bearer token as a stack identity token.

    Returns the resolved service ``User`` when ``token`` is a valid stack
    identity credential (signature + expiry + type verified with the public
    key, then the stack/app + service user must be live and the route in
    scope), or ``None`` when it is NOT a stack identity token (so the caller
    falls through to the normal Keycloak path). A valid signature against a
    revoked stack, a live-but-out-of-scope route, or a dead service account
    RAISES (401/403) — those are authenticated-but-rejected, not unknown."""
    from utils.build_task_token import verify_stack_identity_token
    claims = verify_stack_identity_token(token)
    if claims is None:
        return None
    from services import stack_identity as _si
    return _si.resolve_identity_claims(db, request, claims)


def require_bearer_or_api_key(
        authorization: Optional[str] = Header(None),
        x_api_key: Optional[str] = Header(None),
        request: Request = None,
        db: Session = Depends(get_db)
) -> models.User:
    """
    Flexible authentication: accepts either Bearer token OR API key.
    Prioritizes API key for faster service-to-service auth.

    GX.A3/SEC-1-002: once an API key row is resolved (X-API-Key header or
    an api-key Bearer token), _enforce_api_key_policy runs — expiry is
    enforced for ALL keys and app-bound keys (app_id set) are checked
    against their app's scope. Bearer-only (interactive) auth is untouched.
    """
    # Record HOW the caller authenticated as we go. The distinction is
    # computed here and was previously discarded; see utils/request_principal.
    def _mark(channel: str, user, api_key=None):
        try:
            from utils.request_principal import set_principal, set_channel, set_api_key
            set_principal(user)
            set_channel(channel)
            if api_key is not None:
                set_api_key(api_key)  # Gate 32: retain which key authorised this
        except Exception:
            pass  # never fail auth over bookkeeping
        # FX.101 part 1 (Q87=A) — tenant IP allowlist gate (every
        # return path of this dependency goes through _mark).
        _enforce_tenant_ip_allowlist(db, user, request)
        return user

    # RTA401 — the JWT verdict for X-API-Key BEFORE any API-key lookup: a
    # JWT-shaped credential (three base64url segments, header with alg+kid) is
    # a MOD-minted credential and must NEVER be retried as an API key. A
    # non-JWT value keeps the path below byte-for-byte unchanged.
    _jwt_shaped, _jwt_detail = _jwt_credential_verdict(x_api_key, db)
    if _jwt_detail is not None:
        # Unknown signing key id: the JWT verdict IS the answer (401, distinct
        # detail) — the value is never retried as an API key below.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_jwt_detail,
        )

    # Try API key first (faster, no Keycloak call)
    if x_api_key and not _jwt_shaped:
        db_api_key = crud.get_api_key_by_key_prefix(db, key_prefix=x_api_key)
        if db_api_key and verify_api_key(x_api_key, db_api_key.hashed_key):
            _enforce_api_key_policy(db, db_api_key, request)
            user = crud.get_user(db, user_id=db_api_key.user_id)
            if user and user.status == models.UserStatus.ACTIVE:
                # BUG-1027u (SECURITY): this branch used to return straight
                # away, so a static X-API-Key owned by an external
                # collaborator kept working after their access expired, was
                # deactivated, or had its terms withdrawn — the azp api-key
                # Bearer branch above already ran the FX.110 gate, this one
                # skipped it entirely. Same helper, same fail-closed posture;
                # claims is None because the X-API-Key funnel has no JWT
                # claims to read (enforce_external_gate declares
                # claims: Optional[Dict[str, Any]] = None). require_terms=True
                # matches what this dependency already passes on its
                # interactive branch (_resolve_current_user(..., require_terms=True)).
                _enforce_external_gate_for_key_owner(db, user, None, True)
                return _mark("programmatic", user, db_api_key)

    # Try Bearer token (slower, requires Keycloak validation)
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        # GX.S3 — a stack identity token is a valid programmatic Bearer
        # credential too. Verified with the public key (never minted by the
        # caller), it resolves to the stack's (or its targeting app's) service
        # user under a default-deny route scope. If the token is NOT a stack
        # identity token this returns None and we fall through to the normal
        # Keycloak path below — so a normal Bearer is never disturbed.
        si_user = _try_stack_identity_bearer(token, request, db)
        if si_user is not None:
            return _mark("stack_identity", si_user)
        from keycloak_auth import get_current_user_claims as keycloak_get_claims
        try:
            claims = keycloak_get_claims(token)
            azp = claims.get("azp", "")
            if azp.startswith("api-key-"):
                db_api_key = crud.get_api_key_by_keycloak_client_id(db, keycloak_client_id=azp)
                if db_api_key:
                    _enforce_api_key_policy(db, db_api_key, request)
                    user = crud.get_user(db, user_id=db_api_key.user_id)
                    if user and user.status == models.UserStatus.ACTIVE:
                        # An API key carried as a bearer token is still
                        # programmatic. Reading "has a Bearer header" as
                        # interactive is the obvious way to get this wrong.
                        return _mark("programmatic", user, db_api_key)
            else:
                # BUG-1027p (SECURITY): this branch used to be a bare
                # get_user_by_keycloak_id + ACTIVE check, so a normal browser
                # Bearer skipped EVERY gate get_current_user runs on the ~41
                # routers that use this dep (FX.110 outside-collaborator gate:
                # MFA/own-SSO sign-in strength, gov/DoD approval, account
                # EXPIRY, disabled status, terms; FX.101 tenant IP allowlist;
                # app-only identity refusal, REGISTRATION_INCOMPLETE, service-
                # account interactive refusal, SaaS email-verified, FX.65 dark
                # tenant, FX.54 admin-tier). Reuse the SAME helper get_current_
                # user uses — no gate logic copied here. Every HTTPException it
                # raises reaches the client unchanged through the `except
                # HTTPException: raise` immediately below (FX.26b). require_
                # terms is hard-coded True here, never a parameter: as a
                # dependency parameter it was bindable from the query string
                # (see the :165-175 docstring).
                user = _resolve_current_user(db, claims, request, require_terms=True)
                return _mark("interactive", user)
        except HTTPException as _cred_exc:
            # FX.26b (part 2 of FX.26): _enforce_api_key_policy above raises
            # HTTPException(401) with a specific detail ("API key has expired.",
            # "API key is disabled.", app-scope denials). The broad `except
            # Exception` below used to swallow it, so clients got the generic
            # "Authentication required" detail and an extra AUTH_LOGIN_FAILED
            # audit row on top of the AUTH_API_KEY_EXPIRED one. Re-raise
            # unchanged: same 401, real reason, one audit row. Keycloak's own
            # 401s (expired JWT signature, aud mismatch) propagate too — same
            # status, better detail. Genuine network/JWKS failures are NOT
            # HTTPException, so they still fall through to the generic 401
            # below (fail closed, never a 500).
            #
            # Q7 (owner Q48=A, 2026-11-05) — every FAILED CREDENTIAL gets
            # exactly one AUTH_LOGIN_FAILED row. Until now this arm re-raised
            # straight away, so a forged signature, an unknown kid, an aud/iss
            # mismatch and an expired JWT (keycloak_auth.py:408/:412/:421/:449
            # /:462/:464) were invisible to the audit trail. Discriminator,
            # stated here rather than inferred: audit ONLY a 401 whose detail
            # is not one of the API-key-policy rejections — those already own
            # their audit event (AUTH_API_KEY_EXPIRED / API_KEY_AUTO_DISABLED
            # / the app- and stack-scope 403s), so a second row would
            # double-count them. 403s (REGISTRATION_INCOMPLETE, app-only
            # identity, service-account interactive refusal, email-
            # verification, external-gate, scope denials) are authenticated-
            # but-rejected, not failed credentials, and stay unlogged per the
            # owner line. The row carries NO actor and NO tenant: a failed
            # credential resolves to nobody, so user_id/tenant_id stay NULL
            # (audit_events tolerates NULL; permission_audit_logs does not).
            # attempted_identity is a truncated credential PREFIX only — never
            # the credential, never a host or an IP.
            if _cred_exc.status_code == 401 and _cred_exc.detail not in (
                "API key has expired.",
                "API key is disabled.",
                "API key rotation grace window has ended.",
            ):
                try:
                    db.add(models.AuditEvent(
                        event_name=models.AuditAction.AUTH_LOGIN_FAILED.value,
                        details={
                            "reason": "Bearer credential rejected "
                                      "(signature, expiry, kid, audience or issuer)",
                            "attempted_identity": (
                                authorization[:20] + "..." if authorization
                                else (x_api_key[:8] + "..." if x_api_key else "none")
                            ),
                        },
                    ))
                    db.commit()
                except Exception:
                    # the audit write must never mask the denial and never
                    # 500 the request: undo the failed insert, then re-raise
                    # the ORIGINAL exception unchanged below.
                    try:
                        db.rollback()
                    except Exception:
                        pass
            raise
        except Exception as e:
            logger.warning(f"Bearer token validation failed: {e}")
            pass

    # Neither worked — log auth failure for compliance (CMMC AU-2).
    # GSEC R-053: the previous call passed log_auth_failure() kwargs it does
    # not accept (attempted_identity / failure_reason / ip_address), so it
    # raised TypeError and was swallowed by the except below — ZERO
    # failed-auth rows were ever written. And even with correct kwargs it
    # would have gone through audit.log_security_event(), which writes a
    # PermissionAuditLog whose actor_user_id / tenant_id are NOT NULL + FK to
    # users.id / tenants.id; for an unknown caller those fall back to id=0,
    # which does not exist, so the INSERT hit an FK violation and was dropped
    # too. Instead write a REAL audit_events row: audit_events.tenant_id and
    # user_id are BOTH nullable (unlike permission_audit_logs), so a failed
    # auth by an unknown user always lands. This must NEVER raise into the
    # request path.
    try:
        _identity = (
            authorization[:20] + "..." if authorization
            else (x_api_key[:8] + "..." if x_api_key else "none")
        )
        db.add(models.AuditEvent(
            # actor/tenant intentionally omitted (NULL) — the caller is
            # unknown on a failed auth; audit_events tolerates NULL there.
            event_name=models.AuditAction.AUTH_LOGIN_FAILED.value,
            details={
                "reason": "Invalid credentials (neither Bearer token nor API key valid)",
                "attempted_identity": _identity,
            },
        ))
        db.commit()
    except Exception:
        pass  # failed-auth logging must never raise into the request path

    # FX.106 (item 2): count this failed auth against the client IP for
    # the app-level auto-block (thresholds from api.rate_limit.auto_block_*
    # settings). Best-effort — a guard failure never changes the 401. This
    # is the API's own failed-auth surface (the interactive password login
    # is Keycloak's; its per-account brute-force lockout, R-096, still
    # covers single-account brute force, and this bounds one IP hammering
    # the API auth boundary across tokens/accounts/keys).
    if request is not None:
        try:
            from services import api_rate_limit_guard as _fx106_guard
            _fx106_guard.note_login_failure(request, db=db)
        except Exception:
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide either Bearer token or X-API-Key header."
    )


def require_auditor_data_content_principal(
        request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_bearer_or_api_key),
) -> models.User:
    """GY.C16 data-content gate for routes that authenticate with
    ``require_bearer_or_api_key`` (the run-links embed API-key surface and
    ephemeral run inputs) instead of the user funnel. Same check, same
    403 + ``X-Error-Code: AUDITOR_DATA_CONTENT_FORBIDDEN`` + audit row as
    ``require_auditor_data_content`` — it shares
    ``_enforce_auditor_data_content`` — but the principal already resolves
    through the bearer-or-api-key scheme (API-key row → owning user;
    Bearer → user), so NO Authorization Bearer header is required and
    X-API-Key embed/app clients keep working. An API key is the owning
    user's own key, so the user's auditor grant applies to it."""
    return _enforce_auditor_data_content(request, db, current_user)


def require_node_authoring_key(
        authorization: Optional[str] = Header(None),
        x_api_key: Optional[str] = Header(None),
        db: Session = Depends(get_db),
) -> models.User:
    """Auth for the external node-authoring API.

    Requires an API key (X-API-Key or an api-key bearer) that carries the
    ``node:author`` capability scope and is not read-only. Browser/Keycloak
    user sessions are rejected — this is a programmatic surface. Returns the
    key owner's ``User`` (tenant_id/id determine where the node is created).
    """
    from utils.request_principal import get_api_key
    from utils.api_key_scopes import NODE_AUTHOR_SCOPE, key_has_scope

    # Authenticate (raises 401 when no valid credential is presented).
    user = require_bearer_or_api_key(authorization=authorization, x_api_key=x_api_key, db=db)

    api_key = get_api_key()
    if api_key is None:
        # Authenticated, but as a browser/user session rather than an API key.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an API key with the 'node:author' scope.",
        )
    if getattr(api_key, "is_read_only", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only API keys cannot author nodes.",
        )
    if not key_has_scope(api_key, NODE_AUTHOR_SCOPE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is missing the required 'node:author' scope.",
        )
    # Same caller-identity rule as the
    # fleet-scope factory: no service-principal key on this MCP surface.
    _reject_service_principal_key(api_key, user)
    return user


# ============================================================================
# MCP tool fleet — per-tool scope dependencies
# ----------------------------------------------------------------------------
# Every locked-down MCP front door is gated by its own capability scope. The
# factory below reproduces the require_node_authoring_key shape (authenticate →
# require an API-key principal, not a browser session → reject read-only →
# assert the tool's scope → return the key owner's User) so a key minted for one
# tool cannot drive another. Admin tools compose their router's existing
# platform-admin dependency ON TOP of the scope check (the scope narrows which
# key; the admin check preserves who).
def _reject_service_principal_key(api_key, user) -> None:
    """MCP/programmatic tool surfaces act
    ONLY with the caller's own user credential. A key whose principal is a
    service identity — a tenant service account (``api_keys.service_account_id``
    set, FX.114) or the seeded platform service user (``User.is_service_account``)
    — is refused: it acts for the platform/tenant rather than the person who
    started the MCP session, so it must never stand in for a caller. Raises
    403 (never leaks which principal, beyond naming the offending kind)."""
    if getattr(api_key, "service_account_id", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service-account API keys cannot drive MCP tool surfaces; "
                   "supply the caller's own API key.",
        )
    if getattr(user, "is_service_account", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform-service API keys cannot drive MCP tool surfaces; "
                   "supply the caller's own API key.",
        )


def _require_scoped_key(scope: str, *, read_only_ok: bool = False):
    """Build a FastAPI dependency that requires an API key carrying ``scope``."""

    def dependency(
            authorization: Optional[str] = Header(None),
            x_api_key: Optional[str] = Header(None),
            db: Session = Depends(get_db),
    ) -> models.User:
        from utils.request_principal import get_api_key
        from utils.api_key_scopes import key_has_scope

        user = require_bearer_or_api_key(
            authorization=authorization, x_api_key=x_api_key, db=db,
        )
        api_key = get_api_key()
        if api_key is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires an API key with the '{scope}' scope.",
            )
        if not read_only_ok and getattr(api_key, "is_read_only", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Read-only API keys cannot perform this action.",
            )
        if not key_has_scope(api_key, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key is missing the required '{scope}' scope.",
            )
        # MCP tool calls act ONLY with the
        # caller's own user credential. A key owned by a service principal
        # (tenant service account via service_account_id, or the seeded
        # is_service_account platform user) is NOT a caller: it acts for the
        # platform/tenant, not the person who started the session. Refuse it
        # on every fleet router so no MCP server can act through one.
        _reject_service_principal_key(api_key, user)
        return user

    dependency.__name__ = f"require_{scope.replace(':', '_')}_key"
    return dependency


require_workflow_run_key = _require_scoped_key(api_key_scopes.WORKFLOW_RUN_SCOPE)
require_workflow_author_key = _require_scoped_key(api_key_scopes.WORKFLOW_AUTHOR_SCOPE)
require_project_author_key = _require_scoped_key(api_key_scopes.PROJECT_AUTHOR_SCOPE)
require_stack_author_key = _require_scoped_key(api_key_scopes.STACK_AUTHOR_SCOPE)
require_automation_manage_key = _require_scoped_key(api_key_scopes.AUTOMATION_MANAGE_SCOPE)


# ============================================================================
# Build-task scoped credentials (wizard build-DAG callbacks)
# ----------------------------------------------------------------------------
# The build-task stages call back into the API to read/update their BuildSpec
# and register the resulting NodeDefinition. They authenticate with a per-task,
# short-lived, scoped token minted in /workers/tasks/{id}/credentials (see
# utils.build_task_token) — NOT the platform master key. These dependencies
# verify that token and surface its (tenant_id, build_spec_id) scope so each
# endpoint can enforce that a build's token only touches its own build.
#
# During rollout the legacy platform service master key is still accepted
# (via="master_key", fully trusted, no scope to enforce) so an un-migrated
# worker keeps working; that path is removed once every worker ships the
# token-injection change.
# ============================================================================

class BuildAuthContext:
    """Resolved auth for a build-task callback (or a normal caller on a shared
    endpoint like register).

      via == "build_token": scoped token. tenant_id/build_spec_id are set and
                            MUST be enforced via assert_build_scope().
      via == "master_key":  legacy platform service key — fully trusted,
                            nothing to scope (back-compat).
      via == "user":        a normal user/bearer caller (shared endpoints only).
    """
    __slots__ = ("user", "tenant_id", "build_spec_id", "via")

    def __init__(self, user, via, tenant_id=None, build_spec_id=None):
        self.user = user
        self.via = via
        self.tenant_id = tenant_id
        self.build_spec_id = build_spec_id


def _resolve_platform_service_user(db: Session) -> Optional[models.User]:
    """The DB user the platform service master key maps to — used as the actor
    identity for build-token callbacks (system privileges; the tenant scope
    comes from the token, not this user)."""
    import os
    svc_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    if not svc_key:
        return None
    row = crud.get_api_key_by_key_prefix(db, key_prefix=svc_key)
    if not row:
        return None
    return crud.get_user(db, user_id=row.user_id)


def _jwt_credential_verdict(
    x_api_key: Optional[str], db: Session
) -> tuple:
    """RTA401 — (jwt_shaped, distinct_detail_or_None) for a credential.

    A value that parses as a JWT (three base64url segments, JOSE header with
    `alg` + `kid`) is a MOD-minted credential: it gets the JWT verdict ONLY and
    is NEVER retried as an API key (no key-prefix lookup, no bcrypt compare) —
    funnels use element 1 to SKIP the API-key attempt for it. Element 2 is the
    distinct "unknown signing key id" detail when the kid matches no key this
    manager knows (current, previous-in-window, or the ONE signkey-race KV
    re-read); None means the JWT verdict is just this funnel's own generic 401,
    whose wording stays unchanged. Fail-closed both ways; the signature checks
    in utils/build_task_token are untouched. Never logs the token or key
    material (the kid alone is a public identifier and is logged inside
    unknown_signing_key_id)."""
    from utils.build_task_token import (
        is_jwt_shaped,
        unknown_signing_key_id,
        UNKNOWN_KID_DETAIL,
    )
    if not x_api_key or not is_jwt_shaped(x_api_key):
        return (False, None)
    if unknown_signing_key_id(x_api_key, db=db):
        return (True, UNKNOWN_KID_DETAIL)
    return (True, None)


def _try_build_token(x_api_key: Optional[str], db: Session) -> Optional[BuildAuthContext]:
    """If x_api_key is a valid scoped build-task token, return its context;
    else None."""
    if not x_api_key:
        return None
    from utils.build_task_token import verify_build_task_token
    claims = verify_build_task_token(x_api_key)
    if claims is None:
        return None
    user = _resolve_platform_service_user(db)
    if user is None or user.status != models.UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Build-task service identity unavailable.",
        )
    return BuildAuthContext(
        user=user, via="build_token",
        tenant_id=int(claims["tenant_id"]),
        build_spec_id=int(claims["build_spec_id"]),
    )


def _try_master_key(x_api_key: Optional[str], db: Session) -> Optional[BuildAuthContext]:
    """If x_api_key is a valid api_keys row (the legacy service master key),
    return a master_key context; else None.

    GSEC R-075: an EXPIRED / revoked master key must NOT be treated as a
    trusted service credential. We resolve + verify the key, then run it
    through _enforce_api_key_policy (the same post-resolution policy the other
    funnels use) BEFORE granting a master_key context. An expired/revoked key
    there raises HTTPException(401); the callers surface that as a rejected
    credential rather than falling through as "not a master key".
    """
    if not x_api_key:
        return None
    row = crud.get_api_key_by_key_prefix(db, key_prefix=x_api_key)
    if row and verify_api_key(x_api_key, row.hashed_key):
        # Enforce expiry/revocation/app-scope BEFORE trusting the key. Raises
        # 401 for an expired key — let it propagate so the caller rejects the
        # credential (do NOT swallow into "not a master key").
        _enforce_api_key_policy(db, row)
        user = crud.get_user(db, user_id=row.user_id)
        if user and user.status == models.UserStatus.ACTIVE:
            return BuildAuthContext(user=user, via="master_key")
    return None


def require_build_task_token(
        x_api_key: str = Header(...),
        db: Session = Depends(get_db),
) -> BuildAuthContext:
    """Auth for build-ONLY callback endpoints (ingest / stage-result /
    sample-result / finalize / remediation). Accepts a scoped build-task token
    (preferred) or the legacy master key. Rejects ordinary user credentials —
    these endpoints are service-internal."""
    # RTA401 — a JWT-shaped credential gets the JWT verdict ONLY: it is never
    # retried as the legacy master key (no key-prefix lookup, no bcrypt).
    _jwt_shaped, _jwt_detail = _jwt_credential_verdict(x_api_key, db)
    if _jwt_detail is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_jwt_detail,
        )
    ctx = _try_build_token(x_api_key, db) or (
        None if _jwt_shaped else _try_master_key(x_api_key, db)
    )
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid build-task credential.",
        )
    return ctx


def require_user_or_build_token(
        authorization: Optional[str] = Header(None),
        x_api_key: Optional[str] = Header(None),
        db: Session = Depends(get_db),
) -> BuildAuthContext:
    """Auth for endpoints SHARED between normal users and the build-task (i.e.
    register, POST /node_definitions/). Tries the build-task token first, then
    the master key, then falls back to ordinary bearer/api-key user auth so
    user-driven calls behave exactly as before (via="user")."""
    ctx = _try_build_token(x_api_key, db) or _try_master_key(x_api_key, db)
    if ctx is not None:
        return ctx
    # Normal user path — reuse the existing flexible auth.
    user = require_bearer_or_api_key(authorization=authorization, x_api_key=x_api_key, db=db)
    return BuildAuthContext(user=user, via="user")


def assert_build_scope(ctx: BuildAuthContext, *, build_spec_id: int,
                       tenant_id: Optional[int] = None) -> None:
    """Enforce that a build-token caller is acting only on its own build.

    No-op for via in {"master_key", "user"} (master key is trusted back-compat;
    user calls are governed by the endpoint's own authorization). For
    via=="build_token", the token's scope MUST match the build_spec_id (and
    tenant_id when provided) the endpoint is about to act on, else 403.
    """
    if ctx.via != "build_token":
        return
    if int(build_spec_id) != int(ctx.build_spec_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Build-task token scoped to build_spec {ctx.build_spec_id} "
                f"cannot act on build_spec {build_spec_id}."
            ),
        )
    if tenant_id is not None and int(tenant_id) != int(ctx.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Build-task token scoped to tenant {ctx.tenant_id} "
                f"cannot act on tenant {tenant_id}."
            ),
        )


def require_service_account_token(
        authorization: Optional[str] = Header(None)
) -> dict:
    """
    Validates Keycloak service account tokens for service-to-service communication.

    Unlike require_bearer_or_api_key, this does NOT require a database user record.
    Service accounts exist only in Keycloak and are identified by their sub claim
    starting with "service-account-".

    Returns:
        dict: Token claims including 'sub', 'azp', 'aud', etc.

    Raises:
        HTTPException: If token is missing, invalid, or not from a service account
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required for service account authentication."
        )

    from keycloak_auth import get_current_user_claims as keycloak_get_claims
    token = authorization.replace("Bearer ", "")

    try:
        claims = keycloak_get_claims(token)

        # Validate this is a service account token by checking the authorized party (azp)
        # Service accounts use the client_id in the azp claim
        azp = claims.get('azp', '')
        expected_service_client = os.getenv('KEYCLOAK_SERVICE_CLIENT_ID', 'platform-service')

        if is_debug_mode():
            logger.info(f"Token validation - azp: '{azp}', expected: '{expected_service_client}'")

        if azp != expected_service_client:
            if is_debug_mode():
                logger.warning(f"Rejected token: azp='{azp}' does not match service client '{expected_service_client}'")
            else:
                logger.warning("Rejected token: azp does not match service client")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint requires a service account token."
            )

        if is_debug_mode():
            logger.info(f"Service account authenticated: {azp}")
        else:
            logger.info("Service account authenticated")
        return claims

    except HTTPException:
        raise
    except Exception as e:
        if is_debug_mode():
            logger.error(f"Service account token validation failed: {e}")
        else:
            logger.error("Service account token validation failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid service account token: {str(e)}"
        )


def require_resource_capability(capability_name: str, resource_type: "models.ResourceType", resource_id_param: str = None):
    """
    A dependency factory to protect endpoints based on granular resource-level capabilities.

    This is the ADVANCED version that checks the full capability-based permission system:
    - Direct user permissions (ResourcePermission with user_id)
    - Role capabilities (Capability assigned to user's CustomRole)
    - Team permissions (ResourcePermission with team_id where user is member)

    Use this when you need resource-specific permission checks (e.g., "can user X edit workflow Y?").
    For simple role-based checks, use require_capability() instead.

    Platform admins bypass all capability checks (god mode).

    Args:
        capability_name: The name of the required capability (e.g., "view", "execute", "edit")
        resource_type: The ResourceType enum value for the resource being accessed
        resource_id_param: Optional path parameter name containing the resource ID.
                          If None, checks for wildcard permission on resource type.

    Example:
        @router.get("/workflows/{workflow_id}")
        def get_workflow(
            workflow_id: int,
            current_user: models.User = Depends(require_resource_capability(
                "view",
                models.ResourceType.WORKFLOW,
                "workflow_id"
            ))
        ):
            ...
    """
    from datetime import datetime
    from sqlalchemy.orm import joinedload
    from fastapi import Request

    def dependency(
            request: Request,
            db: Session = Depends(get_db),
            current_user: models.User = Depends(require_active_and_onboarded_user),
    ) -> models.User:
        # Platform-admin bypass + capability resolution now live in authorize()
        # (unified-authorization Gate 05). Keep only the resource_id extraction.
        resource_id = None
        if resource_id_param and resource_id_param in request.path_params:
            try:
                resource_id = int(request.path_params[resource_id_param])
            except (ValueError, TypeError):
                resource_id = None
        from resource_permissions import authorize
        if authorize(db, current_user, resource_type, capability_name, resource_id):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have the required '{capability_name}' permission.",
        )

    return dependency


def check_user_has_capability(
    db: Session,
    user: "models.User",
    capability_name: str,
    resource_type: "models.ResourceType",
    resource_id: int = None
) -> bool:
    """
    Helper function to check if a user has a specific capability.

    This is a non-raising function that returns True/False, useful for
    conditional logic in endpoints rather than as a dependency.

    Args:
        db: Database session
        user: The user to check
        capability_name: The capability to check for
        resource_type: The resource type
        resource_id: Optional specific resource ID (None = wildcard)

    Returns:
        bool: True if user has the capability, False otherwise
    """
    # Delegated to the single authorize() chokepoint (unified-authorization
    # Gate 05). authorize() is the superset of the former inline logic.
    from resource_permissions import authorize
    return authorize(db, user, resource_type, capability_name, resource_id)


def require_harbor_credentials(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
) -> models.User:
    """
    Requires Harbor credentials to be configured before allowing workflow operations.

    Checks for the following secrets in the tenant's secrets:
    - harbor_registry (e.g., example.com)
    - harbor_username
    - harbor_password

    Raises:
        HTTPException: If any Harbor credential is missing, with setup instructions
    """
    # 1784 CE carve: CE ships no Harbor registry. When the platform registry env is
    # empty/unset there is nothing to authenticate against, so this pre-flight is a
    # no-op (same "configured" posture as routers/platform_feature_config.py). SaaS/
    # on-prem keep strictness whenever a registry IS configured.
    from utils.registries import platform_registry_configured
    if not platform_registry_configured():
        return current_user

    required_secrets = {
        "harbor_registry": "Registry URL",
        "harbor_username": "Registry Username",
        "harbor_password": "Registry Password"
    }

    # Tenant-of-record for platform-shared secrets. Mirrors the
    # PLATFORM_TENANT_ID convention in routers/secrets.py — the
    # runtime workflow-execution path already falls back to this
    # tenant for harbor_* secrets, so this pre-flight check needs
    # the same fallback or it 400s tenants whose own secrets aren't
    # set even though the runtime would have succeeded.
    PLATFORM_TENANT_ID = 1

    missing_secrets = []

    for secret_name, display_name in required_secrets.items():
        decrypted = crud.resolve_decrypted_secret_for_tenant(
            db, secret_name, current_user.tenant_id
        )
        if not decrypted or decrypted.strip() == '' or decrypted == 'PLEASE_CONFIGURE':
            # Fall back to the platform tenant — harbor_* secrets are
            # platform-shared by design (every tenant pulls images
            # from the same Harbor instance). The decrypted-by-name
            # lookup matches the resolution shape used by
            # routers/secrets.py at workflow run time.
            decrypted = crud.get_decrypted_secret_by_name(
                db, secret_name=secret_name, tenant_id=PLATFORM_TENANT_ID
            )
            if not decrypted or decrypted.strip() == '' or decrypted == 'PLEASE_CONFIGURE':
                missing_secrets.append(display_name)

    if missing_secrets:
        error_message = (
            f"Cannot run workflows. Missing Harbor credentials: {', '.join(missing_secrets)}\n\n"
            "Ask your platform admin to share registry credentials with your organization, "
            "or configure them yourself in Settings > Secrets:\n\n"
            "   harbor_registry, harbor_username, harbor_password"
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_message
        )

    return current_user