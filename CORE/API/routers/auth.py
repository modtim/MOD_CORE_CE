# API/routers/auth.py

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from sqlalchemy.orm import Session
from keycloak_auth import exchange_code_for_token, refresh_access_token, keycloak_admin_client
from dependencies import get_db
from utils.rate_limiter import check_rate_limit, rate_limit_auth, _rate_limit_client_ip
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Authentication"]
)

class Token(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    id_token: str
    scope: str
    # Adding other potential fields from keycloak response to be safe
    refresh_expires_in: int
    session_state: str
    not_before_policy: int = Field(alias="not-before-policy")


class CodeExchangeRequest(BaseModel):
    code: str
    redirect_uri: str
    code_verifier: Optional[str] = None  # Add PKCE support
    client_id: Optional[str] = None  # Allow specifying client_id for different clients
    grant_type: Optional[str] = None  # GY.C3.s2 — login-gate exemption pass-through


@router.post("/exchange-code", response_model=Token, summary="Exchange Authorization Code for Tokens")
def exchange_authorization_code(
        request: Request,
        request_body: CodeExchangeRequest = Body(...),
        db: Session = Depends(get_db),
):
    # S6: Rate limit auth endpoints (10 req/min per IP)
    rate_limit_auth(request)
    """
    Exchanges an authorization code from Keycloak for an access token.
    This is part of the standard OAuth2 authorization code flow for SPAs.
    Supports both PKCE (public clients) and client_secret (confidential clients).
    """
    token_data = exchange_code_for_token(
        code=request_body.code,
        redirect_uri=request_body.redirect_uri,
        code_verifier=request_body.code_verifier,  # Pass the code_verifier
        client_id=request_body.client_id  # Pass the client_id
    )

    # Decode claims once for downstream JIT / audit logic.
    from jose import jwt as jose_jwt
    try:
        claims = jose_jwt.get_unverified_claims(token_data.get("access_token", ""))
    except Exception:
        claims = {}

    # FX.101 part 2a (Q87=A) — require-SSO login gate. A token whose
    # `identity_provider` claim is empty was NOT brokered by an IdP: if
    # the email's domain is a verified domain of a tenant with
    # auth.require_sso on, the password/direct-grant login is refused
    # (403 + the IdP hint the UI redirects to) BEFORE any JIT/audit work.
    # 1788b CE carve: sso_requirement is paid; with it absent there is no
    # require-SSO login policy in CE (the setting itself is a paid knob), so the
    # login continues exactly as before FX.101 part 2a.
    from utils.paid_import import optional_paid
    _sso_requirement = optional_paid("services.sso_requirement")
    if _sso_requirement is not None:
        _sso_requirement.enforce_login(db, claims, request=request)

    # add-self-service-sso Phase E — JIT provisioning hook.
    # Fires BEFORE the audit-emit + session-create block because a JIT
    # rejection must return a 403 with a structured body that the
    # Dashboard can surface; we can't let an Exception-pass swallow it.
    db_user = _run_jit_if_needed(db, claims)

    # GY.C3.s2 — enforced-MFA login gate; fail-closed BEFORE any login-success
    # bookkeeping (grant_type carries the service / client-credentials /
    # direct-grant exemptions; e2e_service_principals comes from the token's
    # groups claim). A forced identity never gets its token handed out.
    # FX.162.p2 CE carve (owner Q157: the compliance vertical is PAID-only):
    # services/compliance_gate_task_start.py is denied from the CE tree, so the
    # gate is reached through optional_paid — the SAME shape as the
    # sso_requirement probe above. With the module absent there is no enforced-
    # MFA login policy in CE (the settings that force it are paid knobs), so the
    # login continues exactly as before GY.C3.s2: the CE fallback is ALLOW
    # ({"forced": False}, a no-op), never fail-closed — CE must not block logins.
    _mfa_gate_mod = optional_paid("services.compliance_gate_task_start")
    _mfa_gate = {"forced": False}
    if db_user is None:
        import crud
        db_user = crud.get_user_by_keycloak_id(
            db, keycloak_user_id=claims.get("sub", ""))
    if db_user is not None and _mfa_gate_mod is not None:
        _mfa_gate = _mfa_gate_mod.login_gate(
            db, db_user,
            grant_type=request_body.grant_type,
            kc_group_names=claims.get("groups") or [],
            kc_client=keycloak_admin_client)
    if _mfa_gate["forced"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "MFA_ENROLLMENT_REQUIRED",
                    "required_action": _mfa_gate["required_action"]})

    # Audit: login success (CMMC AU-2)
    try:
        from models import AuditEvent, UserSession, User
        db.add(AuditEvent(
            event_name="AUTH_LOGIN_SUCCESS",
            # Q3b: ID-only audit — the Keycloak sub (user id) stays; the
            # email is PII and is NOT written to audit rows.
            details={"sub": claims.get("sub")},
        ))

        # Track session for multi-session management
        sub = claims.get("sub", "")
        session_state = token_data.get("session_state", "")
        if sub and db_user is None:
            import crud
            db_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=sub)
        if db_user:
            from services.client_ip import resolve_client_ip  # FX.106 p3a — real client IP
            client_ip = resolve_client_ip(request)
            user_agent = request.headers.get("user-agent", "")[:500]
            # Gate 2b: relocated to services/auth_service.py — pure
            # multi-session bookkeeping (not an auth decision); the inline
            # db.query it replaces lived at crud/auth.py.
            from services import auth_service
            auth_service.record_user_session_activity(
                db,
                user_id=db_user.id,
                session_state=session_state,
                ip_address=client_ip,
                device_info=user_agent,
            )

        db.commit()

        # §5.6 — SSO_PASSWORD_BYPASS_USED. Login completed with a token
        # whose `identity_provider` claim is empty (= no broker was
        # involved) but the email's domain has an active SSO IdP. The
        # user actively chose the "Use password instead" escape hatch
        # OR posted credentials directly to /auth/exchange-code without
        # going through the email-first step. Either way it's a bypass.
        _maybe_audit_password_bypass(db, db_user, claims)
    except Exception:
        pass

    return token_data


def _run_jit_if_needed(db: Session, claims: dict):
    """Phase E — invoke `sso.jit_provisioning.provision_or_login` when
    the token has an `identity_provider` claim (SSO origin). Returns
    the resolved/created MOD `User` or None.

    Translates `JitRejection` into HTTP 403 with a structured body so
    the Dashboard can render the over-quota / unknown-domain page.
    Other exceptions are swallowed (non-SSO logins, claims without sub,
    etc. — the outer audit-block handles the legacy path).
    """
    if not claims:
        return None
    if not (claims.get("identity_provider") or "").strip():
        # Not an SSO login — let the legacy lookup-only path run.
        return None
    try:
        from sso.jit_provisioning import (
            provision_or_login, JitRejection,
            JitSeatLimitExceeded, JitUnknownDomain,
        )
    except Exception:
        return None
    try:
        result = provision_or_login(db, claims)
        # The user object may not be flushed-and-committed yet. Commit
        # so the JIT row is durable before the session row is added.
        db.commit()
        return result.user
    except JitSeatLimitExceeded as e:
        db.rollback()
        # Re-emit the audit on a separate session so a rollback didn't
        # eat it (advisory lock + audit both happened pre-rollback).
        # For v1 we accept the audit may be lost on rollback; the
        # rejection itself surfaces to the caller via the 403.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": e.code,
                "message": (
                    "This workspace cannot add another user right now. Contact your tenant "
                    "admin to remove an inactive user."
                ),
                "metadata": e.metadata,
            },
        )
    except JitUnknownDomain as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": e.code,
                "message": (
                    "Your team isn't on MOD yet. Ask your IT team to set "
                    "up MOD for your organisation."
                ),
                "metadata": e.metadata,
            },
        )
    except JitRejection:
        db.rollback()
        return None
    except Exception:
        logger.exception("JIT provision_or_login raised (non-fatal)")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _maybe_audit_password_bypass(db: Session, db_user, claims: dict) -> None:
    """add-self-service-sso §5.6 — emit SSO_PASSWORD_BYPASS_USED when a
    user with an SSO-matching email logs in via the password form.

    Triggers when:
      - claim `identity_provider` is missing/empty (= no broker)
      - resolve_email_domain returns a non-None redirect_alias (= the
        user's email-domain has an active IdP + verified-domain link)

    Requires the dashboard-client `identity_provider` User Session
    Note mapper to be present (provisioned by
    `_configure_sso_identity_provider_mapper`). Without that mapper
    the claim is missing on EVERY login and we'd false-positive every
    SSO login. Stage operators must enqueue the mapper task before
    flipping any tenant's SSO on; the pre-GA hardening pass (§8) will
    verify this is in place.

    Non-fatal: never raises into the caller. Reuses the auth session
    — by the time this runs the outer commit has already landed, so
    a rollback here rolls back nothing meaningful (only the audit
    row itself would be at stake).
    """
    identity_provider = (claims.get("identity_provider") or "").strip()
    email = claims.get("email") or ""
    if not email or db_user is None:
        return

    # Lazy import — domain_routing imports redis, and we don't want
    # an import-time side effect on the hot auth path if SSO isn't
    # configured for this deployment.
    #
    # CARVE-U7 — this lookup sits OUTSIDE the non-fatal handler below on
    # purpose. open-core/manifest.yaml denies CORE/API/sso/** wholesale, so on
    # CE no email domain can be IdP-bound and no password bypass exists to
    # audit: absent => return (the feature CE does not ship; nothing is granted
    # and nothing is hidden). An ImportError from a PRESENT module is a real
    # bug and must not be swallowed by the "audit emit failed" handler —
    # swallowing it would let a genuine SSO bypass through with no audit row
    # (fail-open on the auth path). optional_paid answers None only for a
    # genuinely absent module; anything else propagates.
    from utils.paid_import import optional_paid
    _domain_routing = optional_paid("sso.domain_routing")
    if _domain_routing is None:
        return
    resolve_email_domain = _domain_routing.resolve_email_domain

    try:
        decision = resolve_email_domain(db, email)
        if decision.redirect_alias is None:
            return  # domain isn't SSO-bound; nothing to audit
        if identity_provider:
            return  # genuine SSO login; not a bypass

        import models
        import audit
        try:
            audit.log_permission_audit(
                db=db,
                action=models.AuditAction.SSO_PASSWORD_BYPASS_USED,
                actor_user_id=db_user.id,
                tenant_id=db_user.tenant_id,
                resource_type="user",
                resource_id=db_user.id,
                metadata={
                    "email": email,
                    "domain": decision.domain,
                    "matched_idp_alias": decision.redirect_alias,
                    "sso_tenant_id": decision.tenant_id,
                },
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
    except Exception:
        logger.warning(
            "SSO password-bypass audit emit failed (non-fatal).",
            exc_info=True,
        )


class RefreshTokenRequest(BaseModel):
    refresh_token: str
    client_id: Optional[str] = "dashboard-client"


@router.post("/refresh-token", response_model=Token, summary="Refresh Access Token via BFF")
def refresh_token_endpoint(
        request: Request,
        request_body: RefreshTokenRequest = Body(...),
):
    rate_limit_auth(request, scope="refresh-token")
    """
    BFF refresh endpoint. The browser posts its refresh_token here; the API
    proxies to Keycloak with the confidential client_secret attached. Needed
    because dashboard-client is a confidential client (Option C) — Keycloak
    rejects browser-direct refresh calls that only carry client_id.
    """
    return refresh_access_token(
        refresh_token=request_body.refresh_token,
        client_id=request_body.client_id or "dashboard-client",
    )


# Response models. Every field is Optional (default None) and extra keys are
# allowed, so these document the existing payloads without being able to
# reject or strip one — critical here, since device_token_poll and
# password_policy_summary build their dict from external Keycloak responses.
class SendVerificationEmailResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    sent: Optional[bool] = None
    reason: Optional[str] = None


class AuthDiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    token_endpoint: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    device_authorization_endpoint: Optional[str] = None
    issuer: Optional[str] = None
    mobile_crypto_profile: str = "default"


class DeviceStartResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_code: Optional[str] = None
    user_code: Optional[str] = None
    verification_uri: Optional[str] = None
    verification_uri_complete: Optional[str] = None
    expires_in: Optional[int] = None
    interval: Optional[int] = None


class DevicePollResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    device_code: Optional[str] = None
    access_token: Optional[str] = None
    id_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_in: Optional[int] = None
    scope: Optional[str] = None
    error: Optional[str] = None


class PasswordPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    min_zxcvbn_score: Optional[int] = None
    rules: Optional[list] = None
    rotation: Optional[str] = None
    compliance: Optional[list] = None


class PasswordStrengthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    score: Optional[int] = None
    length_ok: Optional[bool] = None
    crack_time_seconds: Optional[str] = None
    suggestions: Optional[list] = None
    warning: Optional[str] = None


@router.post("/send-verification-email", response_model=SendVerificationEmailResponse, summary="Resend the Keycloak verification email")
def send_verification_email(request: Request):
    """Trigger a fresh verification email for the currently-
    authenticated user. The user is identified by their JWT
    `sub` claim — we deliberately do NOT require email_verified=
    true to call this, because the whole point is the user is
    blocked at the EMAIL_NOT_VERIFIED gate and needs a way to
    proceed. We read the bearer token directly here (bypassing
    the `require_active_and_onboarded_user` dependency that
    would reject the request) and dispatch to Keycloak's admin
    send-verify-email endpoint.

    Returns {sent: True} on success, {sent: False, reason: ...}
    on any failure. Never raises.

    Security: validates the JWT signature via Keycloak JWKS (not
    unverified claims) and applies rate limiting to prevent
    email-enumeration / spam attacks.
    """
    from utils.keycloak_client import send_verify_email
    from keycloak_auth import get_current_user_claims as _verify_claims
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.startswith("Bearer "):
        return {"sent": False, "reason": "missing bearer token"}
    raw = auth_header.split(" ", 1)[1]
    # Validate JWT signature, expiry, audience + issuer against Keycloak
    # JWKS. A forged or malformed token is rejected outright.
    try:
        claims = _verify_claims(raw)
    except Exception as e:
        return {"sent": False, "reason": f"token validation failed: {e}"}
    kc_user_id = claims.get("sub")
    if not kc_user_id:
        return {"sent": False, "reason": "no sub claim"}
    # AUTH-4: the per-IP limiter runs only AFTER the bearer token has been
    # verified, so unauthenticated/forged junk cannot burn the bucket that
    # legitimate callers behind the same IP share. It guards the real cost
    # (a Keycloak email send).
    rate_limit_auth(request)
    ok = send_verify_email(kc_user_id)
    return {"sent": bool(ok)}


@router.get("/discovery", response_model=AuthDiscoveryResponse)
def auth_discovery(db: Session = Depends(get_db)):
    """
    Public endpoint returning Keycloak OIDC endpoints.
    Used by the worker install script to discover the token endpoint
    for password-grant authentication.
    """
    import os
    keycloak_url = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
    realm = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
    base = f"{keycloak_url}/realms/{realm}/protocol/openid-connect"
    # NOTE: the first three fields are consumed by the worker install script —
    # keep them intact. device_authorization_endpoint is ADDITIVE (P3.3) for the
    # mobile device-grant login.
    #
    # CUIP.10f — mobile_crypto_profile: platform-scoped (tenant=None), read from
    # the global platform-settings store via mobile_cui_policy. The discovery
    # endpoint has no tenant context, so it serves the platform-level profile
    # which is the only value that matters for enrollment.
    # 1788b CE carve: mobile_cui_policy is paid; the mobile crypto profile is a
    # paid CUIP.10 knob, so CE advertises its documented default 'default'.
    from utils.paid_import import optional_paid
    _mcp = optional_paid("services.mobile_cui_policy")
    crypto_profile = _mcp.crypto_profile(db) if _mcp is not None else "default"
    return {
        "token_endpoint": f"{base}/token",
        "authorization_endpoint": f"{base}/auth",
        "device_authorization_endpoint": f"{base}/auth/device",
        "issuer": f"{keycloak_url}/realms/{realm}",
        "mobile_crypto_profile": crypto_profile,
    }


# --- Device Authorization Grant BFF (mobile-runtime-enrollment P3) ----------
# The mobile kiosk/second-device login. The confidential client_secret (if any)
# stays server-side, mirroring /auth/exchange-code — the device never holds it.

class DeviceStartRequest(BaseModel):
    client_id: Optional[str] = None


class DevicePollRequest(BaseModel):
    device_code: str
    client_id: Optional[str] = None


@router.post("/device/start", response_model=DeviceStartResponse, summary="Begin the OAuth device authorization grant")
def auth_device_start(
        request: Request,
        body: DeviceStartRequest,
):
    rate_limit_auth(request)
    from keycloak_auth import device_authorization_start
    return device_authorization_start(body.client_id)


@router.post("/device/poll", response_model=DevicePollResponse, summary="Poll the device grant for tokens")
def auth_device_poll(
        request: Request,
        body: DevicePollRequest,
        db: Session = Depends(get_db),
):
    check_rate_limit(
        f"auth-device-poll:{_rate_limit_client_ip(request)}",
        max_requests=60,
        window_seconds=60,
    )
    from keycloak_auth import device_token_poll
    result = device_token_poll(body.device_code, body.client_id)
    # FX.101 part 2a (Q87=A) — the kiosk/second-device login is the same
    # interactive login as the browser flow: a require-SSO tenant's
    # verified-domain user may only complete it through the tenant's IdP.
    # A pending poll has no token/claims yet, so nothing to gate.
    if result.get("status") == "complete":
        from jose import jwt as jose_jwt
        try:
            claims = jose_jwt.get_unverified_claims(result.get("access_token", ""))
        except Exception:
            claims = {}
        # 1788b CE carve: sso_requirement is paid — no require-SSO policy in CE.
        from utils.paid_import import optional_paid
        _sso_requirement = optional_paid("services.sso_requirement")
        if _sso_requirement is not None:
            _sso_requirement.enforce_login(db, claims)
    return result


# --- Phase 1 — password policy + strength probe ----------------------------

class PasswordStrengthRequest(BaseModel):
    """Body for POST /auth/password-strength. Live UI feedback only —
    does NOT hit HIBP or block on score."""
    password: str = Field(..., min_length=1, max_length=256)
    username: Optional[str] = None
    email: Optional[str] = None
    tenant_slug: Optional[str] = None


@router.get("/password-policy", response_model=PasswordPolicyResponse)
def get_password_policy():
    """Public endpoint returning the active password policy rules.

    Frontend signup / password-reset forms call this to render hint
    text + the strength meter floor. Not authenticated — these are
    user-facing rules, not secrets."""
    from security.password_policy import password_policy_summary
    return password_policy_summary()


@router.post("/password-strength", response_model=PasswordStrengthResponse)
def check_password_strength(
    request: Request,
    body: PasswordStrengthRequest = Body(...),
):
    """Non-blocking strength probe. Returns the zxcvbn score + human-
    readable suggestions for live UI feedback. Rate-limited to prevent
    using this endpoint as a poor-man's offline cracker.

    Does NOT hit HIBP and does NOT raise on weak passwords — clients
    decide how to render the score. The full enforcement gate fires on
    the actual password-set endpoint."""
    rate_limit_auth(request, scope="password-strength")
    from security.password_policy import password_strength
    s = password_strength(
        body.password,
        username=body.username,
        email=body.email,
        tenant_slug=body.tenant_slug,
    )
    return {
        "score": s.score,
        "length_ok": s.length_ok,
        "crack_time_seconds": s.crack_time_seconds,
        "suggestions": s.suggestions,
        "warning": s.warning,
    }