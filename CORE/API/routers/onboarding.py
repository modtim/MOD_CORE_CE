# In API/routers/onboarding.py

from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
import hashlib
import os
import secrets
import traceback
import uuid

import crud
import models
import schemas
from crud import onboarding as crud_onboarding
from dependencies import (
    get_db,
    get_unregistered_user,
    get_unregistered_user_with_token,
    get_keycloak_user_with_token_for_signup,
)
from keycloak_auth import keycloak_admin_client
from services import onboarding_service
from services.onboarding_service import _safe_user_create
from utils.rate_limiter import rate_limit_auth
import logging

router = APIRouter(
    tags=["Onboarding"]
)


def _onboarding_advisory_lock(db: Session, keycloak_sub: str) -> None:
    """LDF-4 §1.3 — serialize concurrent retries for the same Keycloak
    `sub`. Postgres advisory locks held for the transaction duration:
    a second concurrent request with the same sub blocks until the
    first commits/rolls back, eliminating the partial-state race
    where two simultaneous retries both saw "no users row" and both
    tried to create one.
    """
    # SHA-256 → bottom-60-bits gives us a stable signed-positive bigint
    # that fits in pg's int8 advisory-lock keyspace.
    digest = hashlib.sha256(keycloak_sub.encode("utf-8")).hexdigest()
    lock_key = int(digest[:15], 16) & 0x7FFFFFFFFFFFFFFF
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})


def _log_onboarding_500(error_id: str, sub: str, route: str, exc: BaseException, body: dict) -> None:
    """LDF-4 §1.7 — structured log on every 500 from the onboarding
    surface. Redacts free-form text fields that might carry PII while
    keeping the keycloak `sub` + error_id + stack trace so ops can
    correlate a user complaint to a single failure.
    """
    redacted = {k: ("<redacted>" if k in {"first_name", "last_name", "company_name"} else v)
                for k, v in (body or {}).items()}
    logging.error(
        "[onboarding error_id=%s sub=%s route=%s] %s — body=%s\n%s",
        error_id, sub, route, type(exc).__name__, redacted,
        "".join(traceback.format_exception(exc)),
    )


def _envelope_500(error_id: str, message: str) -> HTTPException:
    """LDF-4 §1.6 — every onboarding 500 returns parseable JSON with
    `detail` + `error_id` so the FE error handler can render a real
    message instead of choking on `JSON.parse('Internal Server Error')`.
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"detail": message, "error_id": error_id},
    )


def _has_human_platform_admin(db: Session) -> bool:
    """LDF-4 follow-up — does any human (non-internal-service) user
    already hold platform admin? When False, the next signup is the
    bootstrap signup and gets auto-elevated. Factored into a helper
    so tests can monkeypatch it."""
    return crud_onboarding.has_human_platform_admin(db)


# `_safe_user_create` moved to services/onboarding_service.py (imported
# above) — re-imported here under the same bare name so
# `_create_tenant_or_platform_admin`'s existing call site below keeps
# working unchanged.


# =============================================================================
# --- Pydantic Models for Onboarding Flows ---
# =============================================================================

class FinalizeInvitedUserRequest(BaseModel):
    """Data model for an invited user completing their registration."""
    invitation_token: str
    username: str
    first_name: str
    last_name: str
    # FX.140 part 3 (owner Q128=A): the sign-up marketing opt-in checkbox.
    # Optional and DEFAULT FALSE — marketing/product email is OPT-IN, so a
    # signup payload that does not tick it records granted=false (the user
    # gets no marketing mail until they explicitly opt in).
    marketing_opt_in: bool = False
    # GSEC FX.108 p1 (owner Q95=A): the hosted age tick. Optional at the
    # SCHEMA level (on-prem sign-ups never send it and must keep working
    # unchanged) — the HOSTED path enforces it in the handler, which turns
    # a missing tick into a 422 form error and an underage attempt into a
    # neutral 403 with no personal data stored.
    age_attestation: Optional[str] = None
    # FE convenience only: a form that prefers a year. NEVER stored, never
    # echoed — reduced to a band the moment it is read.
    birth_year: Optional[int] = None

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        from validators.identity import validate_username, UsernameValidationError
        try:
            return validate_username(v)
        except UsernameValidationError as e:
            raise ValueError(e.message) from None


class CreateTenantAndParentUserRequest(BaseModel):
    """Data model for a new customer creating a tenant and becoming a parent user."""
    username: str
    first_name: str
    last_name: str
    company_name: str  # You can collect extra info like a company name
    # FX.140 part 3 (owner Q128=A): sign-up marketing opt-in checkbox —
    # optional, default FALSE (marketing/product email is OPT-IN).
    marketing_opt_in: bool = False
    # GSEC FX.108 p1 (owner Q95=A): the hosted age tick ("I confirm I am 16
    # or older"). Optional at the SCHEMA level so an on-prem sign-up that
    # never sends it is unchanged; the HOSTED handler demands it and answers
    # a missing tick with 422 and an underage attempt with a neutral 403.
    age_attestation: Optional[str] = None
    # FE convenience only — never stored, never echoed (reduced to a band).
    birth_year: Optional[int] = None

    # LDF-4 — wire LDF-3 §10 validators at the request boundary so
    # bad inputs (e.g. company_name='root', username='admin') 422
    # cleanly with the validator's own message instead of slipping
    # through to the inner schemas.UserCreate / db.commit() paths
    # where they cause inconsistent or destructive behaviour.
    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        from validators.identity import validate_username, UsernameValidationError
        try:
            return validate_username(v)
        except UsernameValidationError as e:
            raise ValueError(e.message) from None

    @field_validator("company_name")
    @classmethod
    def _validate_company_name(cls, v: str) -> str:
        from validators.identity import validate_company_name, CompanyNameValidationError
        try:
            return validate_company_name(v)
        except CompanyNameValidationError as e:
            raise ValueError(e.message) from None


# =============================================================================
# --- Onboarding Endpoints ---
# =============================================================================


class InvitationPrefill(BaseModel):
    """Minimal data to pre-fill the Keycloak signup form when arriving
    via an invitation link. Only the email is returned — that's the
    identity-critical field where typos break the whole flow because
    the post-signup `/finalize-invited-user` check requires the
    Keycloak account's email to match the invitation's email exactly."""
    email: EmailStr


@router.get(
    "/invitation-prefill/{token}",
    response_model=InvitationPrefill,
    summary="Look up the email on a pending invitation (for signup pre-fill)",
)
def get_invitation_prefill(
        request: Request,
        token: str,
        db: Session = Depends(get_db),
):
    """Public endpoint — no auth required. The token IS the secret
    (32-byte urlsafe), and the invitation email was already delivered
    to whoever received that token. Returning it here lets the
    Dashboard pre-fill (and lock) the email field in the Keycloak
    registration form so the invited user can't accidentally sign
    up with a typo'd address that would later fail the
    /finalize-invited-user equality check.

    Returns 404 for unknown / expired / already-accepted tokens so
    the Dashboard falls back to letting the user type their email
    manually rather than 500-ing the signup flow.
    """
    rate_limit_auth(request)
    invitation = crud.get_pending_invitation_by_token(db, token=token)
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation is invalid, expired, or already accepted.",
        )
    return {"email": invitation.email}


class AcceptInvitationRequest(BaseModel):
    """2400LR CE signup: the invite link's token is the credential (the
    realm's registration flow is OFF, so there is no KC account to sign
    into yet). `password` provisions the new KC account's credential —
    Keycloak's policy is the authority (a rejected password answers 422
    with KC's policy message). The email is NEVER taken from the body:
    it comes from the invitation row (services/onboarding_service.py
    .accept_invitation)."""
    token: str
    first_name: str
    last_name: str
    password: str


class AcceptInvitationResult(BaseModel):
    """201 body — deliberately minimal: the invitee's identity is already
    on the invitation row, nothing here needs echoing back."""
    status: str


# api-lint: public-by-design: 2400LR CE signup — the invitation token in the BODY is the credential (same trust model as GET /invitation-prefill/{token} above); there is no session to require while realm registration is OFF.
@router.post("/invitations/accept", status_code=status.HTTP_201_CREATED,
             response_model=AcceptInvitationResult,
             summary="Accept an invitation (CE: registration off)")
def accept_invitation_public(
        request: Request,
        request_body: AcceptInvitationRequest,
        db: Session = Depends(get_db),
):
    """PUBLIC endpoint — no session; the invitation token in the BODY is
    the credential (same trust model as GET /invitation-prefill/{token}
    above; the token never travels in a URL here).

    Creates the invitee's Keycloak account server-side (the shipped
    external-invite pattern) then reuses `finalize_invited_user` for the
    DB child-user/role/tenant logic, which also marks the invitation
    ACCEPTED. Responses per the 2400LR contract: 201 {status: accepted};
    404 one neutral text for every bad-token case (no oracle); 409
    {code: account_exists}; 422 KC password-policy message; 500 §1.6
    envelope. Both public invite routes are limited by
    utils/rate_limiter.py:rate_limit_auth (10 req/min per client IP).
    """
    rate_limit_auth(request)
    try:
        return onboarding_service.accept_invitation(
            db,
            token=request_body.token,
            first_name=request_body.first_name,
            last_name=request_body.last_name,
            password=request_body.password,
        )
    except HTTPException:
        # Already structured (404/409/422/4xx from finalize).
        raise
    except Exception as exc:  # noqa: BLE001 — the §1.6/§1.7 catch-all
        # MUST 5: the KC account may exist while the DB side blew up
        # (the service rolled back). Log invitation-route context only —
        # never the token/password/email — then answer the envelope.
        _log_onboarding_500(str(uuid.uuid4()), "-", "/onboarding/invitations/accept",
                            exc, {"first_name": "<redacted>",
                                  "last_name": "<redacted>",
                                  "token": "<redacted>",
                                  "password": "<redacted>"})
        raise _envelope_500(
            str(uuid.uuid4()),
            "Invitation acceptance hit an unexpected server error. "
            "Please try again in a moment, or contact support with this error_id.",
        ) from exc


@router.post("/finalize-invited-user", status_code=status.HTTP_201_CREATED, response_model=schemas.User,
             summary="Finalize Registration for Invited User")
def finalize_invited_user_registration(
        request_body: FinalizeInvitedUserRequest,
        db: Session = Depends(get_db),
        keycloak_user: dict = Depends(get_unregistered_user),
):
    """
    Final step for an **invited user** after they have signed up via Keycloak.

    This endpoint validates their invitation token and creates a **child user** record,
    linking them to the correct existing tenant.
    """
    return onboarding_service.finalize_invited_user(
        db, request_body=request_body, keycloak_user=keycloak_user,
    )


@router.post("/create-new-tenant", status_code=status.HTTP_201_CREATED, response_model=schemas.User,
             summary="Create New Tenant and Parent User")
def create_new_tenant_with_parent_user(
        request_body: CreateTenantAndParentUserRequest,
        db: Session = Depends(get_db),
        user_and_token: tuple = Depends(get_keycloak_user_with_token_for_signup),
        http_request: Request = None,
):
    """
    Creates a new Tenant and a parent user for that tenant.

    Special case: the FIRST human signup on a deployment (when no human
    platform admin yet exists) is assigned to the platform tenant (ID=1)
    as a platform admin — see _create_tenant_or_platform_admin /
    _has_human_platform_admin. No env var / pre-configured email is
    consulted; whoever signs in first owns the platform.

    After creation, this endpoint seeds default node definitions for the
    new tenant inline (no external orchestrator).

    LDF-4 §1.3/§1.4 — the handler is idempotent and transactional:
      * Per-`sub` Postgres advisory lock serializes concurrent retries.
      * A users row whose tenant_id is already set returns 200 with
        the existing tenant payload (the prior call succeeded; this is
        a retry).
      * Any unhandled exception raises the §1.6 envelope so the FE
        gets a parseable JSON `{detail, error_id}` instead of
        "Internal Server Error".
    """
    # GSEC FX.74 p2 (Q57=A) — sanctioned-country block at hosted signup
    # (SaaS only; on-prem skips inside the gate). IP country from the
    # site's front-proxy geo header; no billing country exists pre-signup.
    # 1788b CE carve: sanctions_gate is paid; with it absent CE has no
    # sanctioned-country signup policy (the setting + SDN list are paid), so
    # signup proceeds exactly as before FX.74 p2.
    from utils.paid_import import optional_paid
    _sanctions_gate = optional_paid("services.sanctions_gate")
    if _sanctions_gate is not None:
        _sanctions_gate.check_blocked(
            db, _sanctions_gate.country_from_request(http_request), None,
            request=http_request,
            route="/onboarding/create-new-tenant")

    # GSEC FX.108 p1 (owner decision Q95=A) — hosted sign-up AGE ATTESTATION,
    # taken FIRST, before the SDN screening (which writes an email-bearing
    # audit row) and before anything is created. Q95=A: "hosted-cloud sign-up
    # requires age 16+ (tick-box confirmation), under-13s never on the hosted
    # cloud". A missing tick is a 422 form error (nothing stored); an
    # underage attempt is a neutral 403 whose audit row carries only the BAND
    # — no name, no email, no birth year, no IP — so a refused attempt stores
    # NO personal data at all (data minimisation). On-prem the gate is inert:
    # the owner decision scopes the age rule to the hosted cloud, and K-12
    # under-13 use is on-prem only under the school's own authority.
    # 1788b CE carve: signup_age_attestation is paid; the hosted-cloud 16+
    # demand is a paid compliance gate, so CE has no age attestation at all —
    # age_decision stays None and no SignupAgeAttestation row is written.
    from utils.paid_import import optional_paid
    _age_attestation = optional_paid("services.signup_age_attestation")
    age_decision = (
        _age_attestation.attest_hosted_signup(
            db, request_body.age_attestation,
            birth_year=request_body.birth_year,
        ) if _age_attestation is not None else None
    )

    error_id = str(uuid.uuid4())
    keycloak_user, bearer_token = user_and_token
    keycloak_sub = keycloak_user['sub']

    # GSEC FX.135 s1 — DISPOSABLE-EMAIL BLOCK at self-service sign-up, taken
    # BEFORE the SDN screening (which writes an email-bearing audit row) and
    # before anything is created: a throwaway-domain sign-up (mailinator,
    # yopmail, 10minutemail, ... plus the configured list) has no permanent
    # identity to attach a tenant to, so it is refused 422 "Please use a
    # permanent email address" with no tenant, no user, no Keycloak link and
    # no Stripe side effect. Hosted only: `fraud.block_disposable_email` is
    # registered False on-prem, so an on-prem sign-up is UNCHANGED.
    # Logic + audit rows live in services/disposable_email_block.py.
    # 1788b CE carve: disposable_email_block is paid; CE has no disposable-
    # email signup block (fraud.block_disposable_email is a paid knob).
    from utils.paid_import import optional_paid
    _disposable_email_block = optional_paid("services.disposable_email_block")
    if _disposable_email_block is not None and _has_human_platform_admin(db):
        _disposable_email_block.enforce_signup(
            db, keycloak_user.get('email'), request=http_request)

    # GSEC FX.74 p3a — offline OFAC SDN NAME screening at hosted signup
    # (the NAME side of the same FX.74 vertical as the country gate above).
    # The account name (first + last) and the company name are screened
    # against the local SDN CSV — no network, no external service. A match
    # is NOT a block (owner decision): the account is still created and
    # HELD for review (audit row + user status PENDING). Disabled or no
    # local list -> clear, no-op.
    # 1788b CE carve: sanctions_screening is paid; with it absent there is no
    # SDN name screening at signup, so nothing is ever held for review.
    from utils.paid_import import optional_paid
    _sanctions_screening = optional_paid("services.sanctions_screening")
    screening = (
        _sanctions_screening.screen_signup(
            db,
            name=f"{request_body.first_name} {request_body.last_name}",
            company=request_body.company_name,
            email=keycloak_user.get("email"),
        ) if _sanctions_screening is not None else {"status": "clear"}
    )
    request_body_dump = request_body.model_dump() if hasattr(request_body, "model_dump") else request_body.dict()

    try:
        # §1.3 — serialize concurrent retries for the same Keycloak sub.
        _onboarding_advisory_lock(db, keycloak_sub)

        # §1.4 — idempotent return. If a users row already exists for
        # this Keycloak sub AND has a tenant_id, the prior call landed;
        # we just hand back the same payload so the FE proceeds instead
        # of looping.
        existing_user = crud.get_user_by_keycloak_id(db, keycloak_user_id=keycloak_sub)
        if existing_user is not None:
            if existing_user.tenant_id is not None:
                logging.info(
                    "[onboarding] idempotent re-submit for sub=%s — returning existing user_id=%s tenant_id=%s",
                    keycloak_sub, existing_user.id, existing_user.tenant_id,
                )
                return existing_user
            # tenant_id is NOT NULL in the schema, so an existing row
            # without a tenant_id should be impossible. If we ever see
            # one, log it and treat it as a recoverable partial.
            logging.warning(
                "[onboarding] existing users row sub=%s has NULL tenant_id — schema invariant violated; reusing row",
                keycloak_sub,
            )
        # GSEC FX.101 part 3 (B) — the domain decision is taken BEFORE the
        # tenant-creation quota guard because a verified-domain sign-up never
        # mints a tenant: the refusal (require_sso ON) and the placement into
        # the OWNING tenant (require_sso OFF) must both work in a single-tenant
        # edition. Business logic + audit rows live in
        # services/signup_domain_routing.py; the router only shapes the call.
        domain_decision = None
        # 1788b CE carve: signup_domain_routing is paid; with it absent CE
        # never re-homes a verified-domain signup into the owning tenant, so
        # domain_decision stays None and the normal tenant-creation path runs.
        if _has_human_platform_admin(db):
            from utils.paid_import import optional_paid
            _domain_routing = optional_paid("services.signup_domain_routing")
            if _domain_routing is not None:
                domain_decision = _domain_routing.enforce_signup(
                    db, keycloak_user.get('email'),
                    request=http_request,
                )
        # GT.D7: single-tenant / multi-tenant guard, shared with every other
        # tenant-creation path via services.tenant_limits. CE (and any tier
        # whose license lacks Cap.MULTI_TENANT / an unlimited tenant quota)
        # supports EXACTLY ONE tenant. The FIRST signup (platform bootstrap,
        # no human admin yet) is always allowed — that's the free single
        # tenant; SaaS is MOD's own multi-tenant infra and is never gated.
        # Fail CLOSED: if the guard errors out we refuse rather than wedge
        # signup open.
        if domain_decision is None and _has_human_platform_admin(db):
            from services.tenant_limits import TenantLimitError, assert_can_create_tenant
            try:
                assert_can_create_tenant(db)
            except TenantLimitError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "single_tenant_only",
                            "detail": exc.message},
                )
            except Exception:  # noqa: BLE001
                logging.warning("[onboarding] multi-tenant gate errored; refusing (fail-closed)", exc_info=True)
                raise HTTPException(
                    status_code=403,
                    detail={"code": "single_tenant_only",
                            "detail": "This edition supports a single tenant; multi-tenancy is not available."},
                )
        new_user = _create_tenant_or_platform_admin(
            db=db,
            keycloak_user=keycloak_user,
            bearer_token=bearer_token,
            request_body=request_body,
            error_id=error_id,
            domain_decision=domain_decision,
        )
        # GSEC FX.74 p3a — a screening match HELDS the account for review
        # (it is never blocked outright). The existing pending/held
        # mechanism signup already has is models.UserStatus.PENDING: the
        # users.status enum defaults to PENDING, both creation paths
        # (crud.create_user / crud.create_tenant_and_parent_user) flip it
        # to ACTIVE, and dependencies.require_active_user refuses any
        # non-ACTIVE user with 403. So the account exists (the audit row
        # + the response's status field carry the hold) but stays PENDING
        # until an admin reviews it. Best effort: a failed hold must not
        # fail signup outright — the audit row already recorded the match.
        if screening.get("status") == "held":
            try:
                new_user.status = models.UserStatus.PENDING
                db.commit()
            except Exception:  # noqa: BLE001 — hold is best effort
                logging.warning(
                    "[onboarding] FX.74 p3a: could not hold user %s for "
                    "review (SDN match)", new_user.id, exc_info=True)
        # GSEC FX.108 p1 — the account exists now, so the attestation the
        # gate accepted above becomes a RECORD: one models.SignupAgeAttestation
        # row (user + tenant + band + timestamp) and one AGE_ATTESTATION_RECORDED
        # audit row. Best effort: a record hiccup never unwinds a sign-up that
        # already succeeded (the service logs + swallows).
        # 1788b CE carve: no age attestation in CE -> nothing to record.
        from utils.paid_import import optional_paid
        _age_attestation = optional_paid("services.signup_age_attestation")
        if _age_attestation is not None and age_decision:
            _age_attestation.record_attestation(
                db, user_id=new_user.id, tenant_id=new_user.tenant_id,
                decision={**age_decision,
                          "route": "/onboarding/create-new-tenant"})
        return new_user
    except HTTPException:
        # Already structured (4xx, validators, IntegrityError-mapped).
        raise
    except Exception as exc:  # noqa: BLE001 — this is the catch-all on purpose
        # §1.7 + §1.6: log structured + return JSON envelope.
        _log_onboarding_500(error_id, keycloak_sub, "/onboarding/create-new-tenant", exc, request_body_dump)
        try:
            db.rollback()
        except Exception:
            pass
        raise _envelope_500(
            error_id,
            "Account creation hit an unexpected server error. "
            "Please try again in a moment, or contact support with this error_id.",
        ) from exc


def _create_tenant_or_platform_admin(
    *,
    db: Session,
    keycloak_user: dict,
    bearer_token: str,
    request_body: CreateTenantAndParentUserRequest,
    error_id: str,
    domain_decision=None,
) -> models.User:
    """Body of the create-new-tenant handler, factored out so the
    advisory-lock + idempotent-return + 500-envelope wrapper above
    stays readable. Returns a User on success; raises HTTPException
    on validation / integrity / 4xx paths."""

    # Platform admin elevation: the FIRST human signup on a deployment
    # becomes the platform admin. No env var, no pre-configured email —
    # whoever signs in first owns the platform. (LDF-4 follow-up:
    # INITIAL_USER_EMAIL was removed because it created a chicken-and-
    # egg where the operator had to know + set their email + SMTP
    # before the first signup, but they ARE the one configuring SMTP
    # after signup.)
    #
    # Check: does any human user already hold platform admin? The
    # api_service / api@service.local internal row is excluded via
    # the `email NOT LIKE '%@internal.local'` filter so the seed
    # service user doesn't satisfy the gate.
    is_platform_admin = not _has_human_platform_admin(db)
    if is_platform_admin:
        logging.info(
            "🎯 First-signup bootstrap: no human platform admin exists yet, "
            "auto-elevating %s to platform admin",
            keycloak_user.get('email'),
        )

    if not is_platform_admin and os.getenv("ALLOW_PUBLIC_TENANT_CREATION", "true").lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public tenant creation is disabled.",
        )

    user_details_to_create = _safe_user_create(
        keycloak_user_id=keycloak_user['sub'],
        email=keycloak_user['email'],
        username=request_body.username,
        first_name=request_body.first_name,
        last_name=request_body.last_name,
    )

    # GSEC FX.101 part 3 (B) — `domain_decision` comes from the handler
    # (taken before the tenant-creation quota guard, because a verified-domain
    # sign-up mints no tenant). When it is set the user joins the OWNING
    # tenant with its default role instead of a personal tenant; the tail
    # below (USER_CREATED audit, node scan, welcome email, consent) still
    # runs because a user really was created.
    if domain_decision is not None:
        # 1788b CE carve: signup_domain_routing is paid, so domain_decision is
        # always None in CE and this branch is unreachable; the guard keeps the
        # call site import-safe if a caller ever passes a decision through.
        from utils.paid_import import optional_paid
        _domain_routing = optional_paid("services.signup_domain_routing")
        try:
            new_parent_user = _domain_routing.create_user_in_owning_tenant(
                db, user_details_to_create, domain_decision)
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Username '{request_body.username}' or email "
                        f"'{keycloak_user['email']}' is already in use in "
                        "the organisation that owns this domain. Sign in "
                        "instead of creating a new account."),
            )
        db.commit()
        db.refresh(new_parent_user)
    elif is_platform_admin:
        # Platform admin: assign to platform tenant (ID=1)
        logging.info(f"🎯 Platform admin detected: {keycloak_user['email']}")

        # Get platform tenant
        platform_tenant = crud_onboarding.get_tenant_by_id(db, 1)
        if not platform_tenant:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Platform tenant not found. System initialization error."
            )

        # Create user in platform tenant with god mode
        try:
            new_parent_user = crud.create_user(
                db=db,
                user_details=user_details_to_create,
                tenant_id=platform_tenant.id,
                parent_id=None
            )
        except IntegrityError as e:
            db.rollback()
            if 'username' in str(e.orig).lower():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Username '{request_body.username}' is already taken. Please choose a different username."
                )
            elif 'email' in str(e.orig).lower():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Email '{keycloak_user['email']}' is already registered."
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Unable to create user. This username or email may already be in use."
                )

        # Set platform admin flags
        new_parent_user.is_platform_admin = True
        new_parent_user.onboarding_complete = True
        new_parent_user.status = models.UserStatus.ACTIVE

        # Assign the platform tenant's Admin role. There is no platform-admin
        # permission bypass (Gate 17b — access is by role/grants only), so
        # without a role the platform admin holds zero capabilities and is
        # denied every require_capability() surface in its own tenant (secrets,
        # etc.). The old seed migration skipped platform admins on the
        # now-false assumption that the bypass existed; assign it here so a
        # freshly bootstrapped admin can actually administer tenant 1.
        admin_role = crud_onboarding.get_admin_role(db, platform_tenant.id)
        if admin_role is not None:
            new_parent_user.custom_role_id = admin_role.id
        else:
            logging.warning("Platform admin bootstrap: no 'Admin' role found in "
                            "tenant %s — user %s will hold no capabilities until "
                            "a role is assigned.", platform_tenant.id, new_parent_user.id)

        db.commit()
        db.refresh(new_parent_user)

        logging.info(f"✅ Platform admin created: User ID {new_parent_user.id}, Tenant ID {platform_tenant.id}")

        # managed-AI-self-deploy: first admin sign-in is one trigger for the
        # SAME conservative reconcile the stack-reconciler tick and worker
        # enrollment use — deploy-if-absent, best-fit tier, never over CPU that
        # was hand-picked. Calling it here just makes the deploy start the moment
        # setup finishes instead of waiting for the next tick. Best-effort: a
        # fresh deployment must never fail to create its first admin because the
        # optional AI tier could not be placed yet, and it is idempotent.
        try:
            from routers.persistent_stacks import reconcile_managed_ai
            reconcile_managed_ai(db)
        except Exception:  # noqa: BLE001 — never block first-admin creation
            logging.warning("managed-AI reconcile on first-admin bootstrap failed "
                            "(non-fatal; retries on the reconciler tick)", exc_info=True)

        # Assign platform admin to Keycloak groups for MinIO super-admin access
        platform_admin_groups = ["platform_admin", "minio_super_admin"]
        for group_name in platform_admin_groups:
            try:
                success = keycloak_admin_client.assign_user_to_group(
                    keycloak_user_id=new_parent_user.keycloak_user_id,
                    group_name=group_name
                )
                if success:
                    logging.info(f"✅ Platform admin assigned to {group_name} Keycloak group")
                else:
                    logging.warning(f"⚠️  Failed to assign platform admin to {group_name} group (non-critical)")
            except Exception as e:
                logging.warning(f"⚠️  Could not assign platform admin to {group_name} group: {e}")

        # Platform admin doesn't need company name - use "Platform"
        return new_parent_user
    else:
        # Normal user: create new tenant
        try:
            new_parent_user = crud.create_tenant_and_parent_user(
                db=db,
                user_details=user_details_to_create
            )

            new_parent_user.tenant.name = request_body.company_name
            db.commit()
            db.refresh(new_parent_user)
        except IntegrityError as e:
            db.rollback()
            # Log the raw Postgres error so we never lose the real constraint name
            # behind a misleading "username or email may already be in use" message.
            raw_err = str(e.orig).lower() if e.orig else str(e).lower()
            logging.warning("Onboarding IntegrityError for email=%s username=%s company=%s: %s",
                            keycloak_user.get('email'), request_body.username,
                            request_body.company_name, raw_err)

            if 'users_username_key' in raw_err or 'users_username' in raw_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Username '{request_body.username}' is already taken. Please choose a different username."
                )
            elif 'users_email_key' in raw_err or ('email' in raw_err and 'users' in raw_err):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Email '{keycloak_user['email']}' is already registered. Sign in instead, or use a different email."
                )
            elif 'tenants_name_key' in raw_err or 'tenants_name' in raw_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Company name '{request_body.company_name}' is already taken. Please choose a different name."
                )
            else:
                # Fall-back: the constraint is NOT on username/email/tenant
                # (we already matched those cases above), so don't tell the
                # user to change those. Surface that it's a server-side
                # data issue and point at support. The raw error is already
                # logged above for debugging.
                snippet = raw_err[:160]
                logging.error("Onboarding failed with unhandled IntegrityError: %s", raw_err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Account creation hit an unexpected server error "
                           f"(db constraint: {snippet}). This isn't a problem "
                           f"with your username or company name — it's a "
                           f"server-side issue. Please try again in a moment "
                           f"or contact support if it persists.",
                )

    # Audit: tenant/user created (CMMC AU-2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=new_parent_user.tenant_id, user_id=new_parent_user.id,
            event_name="USER_CREATED",
            # Q3b: ID-only audit — the email is PII; the actor's id is
            # already on the row (user_id=new_parent_user.id).
            details={"method": "tenant_creation", "created_user_id": new_parent_user.id, "is_platform_admin": getattr(new_parent_user, 'is_platform_admin', False)},
        ))
        db.commit()
    except Exception:
        pass

    # Try to trigger DAG but don't fail onboarding if it fails
    node_kind_to_trigger = "Node_Dag_Gen_Dag"
    logging.info(f"Triggering DAG '{node_kind_to_trigger}' for tenant_id: {new_parent_user.tenant.id}")

    try:
        from services.node_scanner import scan_and_register_nodes
        from database import SessionLocal
        import threading
        threading.Thread(target=scan_and_register_nodes, args=(SessionLocal,), daemon=True).start()
    except Exception as e:
        logging.warning(f"Failed to trigger node scan (non-critical): {e}")

    # Send welcome email (SaaS mode only, new tenants only, not platform admin).
    # send_welcome_email stamps users.welcome_email_sent_at on success and
    # users.welcome_email_error on failure so /platform/signup-orphans can
    # surface accounts whose welcome silently failed. Pre-fix the only
    # signal was a single WARNING log line and an account that could
    # authenticate but never received its next-step email.
    if os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas" and not is_platform_admin:
        from utils.welcome_email import send_welcome_email
        send_welcome_email(db, new_parent_user)

    # FX.140 part 3 (owner Q128=A): the sign-up marketing opt-in checkbox
    # is an explicit consent decision — append a source="signup" event
    # (granted = the checkbox, default False = unticked) + one audit row
    # (category/granted/source, never the email address).
    _record_signup_consent(db, new_parent_user, request_body)

    return new_parent_user


def _record_signup_consent(db, new_parent_user, request_body) -> None:
    """CARVE-U7B — sign-up marketing-consent bookkeeping, edition-safe.

    services/email_unsubscribe.py is DENIED by open-core/manifest.yaml with
    no CE overlay, so on CE the module is simply absent: optional_paid
    answers None and we skip the consent event + audit row (the CE
    behaviour — the feature does not exist there). An ImportError whose
    missing name is NOT the carved module is a broken deploy and
    re-raises; there is no `except Exception` — the old site swallowed
    every failure (including a real consent/audit bug on the paid tree)
    behind a WARNING log, a fail-open.
    """
    from utils.paid_import import optional_paid
    _eu = optional_paid("services.email_unsubscribe")
    if _eu is None:
        return
    _eu.record_signup_consent(
        db, new_parent_user,
        getattr(request_body, "marketing_opt_in", False))
    db.commit()
    from audit import log_security_event
    log_security_event(
        db=db,
        action=models.AuditAction.EMAIL_CONSENT_CHANGED,
        user_id=new_parent_user.id,
        tenant_id=new_parent_user.tenant_id,
        metadata={"category": "marketing",
                  "granted": bool(getattr(
                      request_body, "marketing_opt_in", False)),
                  "source": "signup"},
    )


# =========================================================================
# Onboarding Checklist — dynamic step checking + credit reward
# =========================================================================

from dependencies import get_current_user

# Step tables + REWARD_CREDITS now live in services/onboarding_service.py
# (moved with the rest of the checklist policy — nothing monkeypatches
# these by name). Re-exported here so existing imports keep working:
#   from routers.onboarding import CHECKLIST_STEPS, REWARD_CREDITS
CHECKLIST_STEPS = onboarding_service.CHECKLIST_STEPS
REWARD_CREDITS = onboarding_service.REWARD_CREDITS


# ── Response models for onboarding endpoints ────────────────────────────────

class ChecklistStep(BaseModel):
    key: str
    title: str
    description: str
    action: Optional[str] = None
    completed: bool


class OnboardingChecklistResponse(BaseModel):
    steps: List[ChecklistStep]
    completed: int
    total: int
    all_done: bool
    reward_claimed: bool
    reward_credits: int


class ClaimRewardResponse(BaseModel):
    message: str
    credits: int


class MobileEnrollmentCode(BaseModel):
    """Body returned by POST /onboarding/mobile-enrollment/new-code (BUG.B41).
    Same shape as the GET above: config-only, nothing stored."""
    enroll_code: str
    enroll_url: str
    qr_svg: Optional[str] = None
    payload: dict


@router.get("/checklist", response_model=OnboardingChecklistResponse)
def get_onboarding_checklist(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Get the onboarding checklist with dynamic completion status."""
    return onboarding_service.get_checklist(db, current_user)


@router.post("/checklist/claim-reward", response_model=ClaimRewardResponse)
def claim_checklist_reward(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Claim the 500 credit bonus for completing all onboarding steps."""
    # CE: no bonus credits; answer 402 before any DB write or credit call.
    onboarding_service.ensure_checklist_reward_available()
    return onboarding_service.claim_checklist_reward(db, current_user)

@router.get("/mobile-enrollment", response_model=MobileEnrollmentCode,
             summary="Config-only mobile app enrollment code")
def mobile_enrollment(
    current_user: models.User = Depends(get_current_user),
):
    """Assemble this MOD Core instance's PUBLIC config into a mobile enrollment
    code (mobile-runtime-enrollment P3.1). Config-ONLY: no DB write, no secret,
    no expiry, no redemption — it just points the app at this instance; the
    human still logs in via OIDC afterward. The QR is rendered client-side by
    the Dashboard from `enroll_url`.
    """
    from utils.paid_import import require_paid
    _m = require_paid("utils.mobile_enrollment", "Mobile device enrollment")
    build_enrollment_payload, build_enrollment_code, build_qr_svg = (
        _m.build_enrollment_payload, _m.build_enrollment_code, _m.build_qr_svg
    )

    org = current_user.tenant.name if getattr(current_user, "tenant", None) else None
    payload = build_enrollment_payload(org or "MOD")
    code = build_enrollment_code(payload)
    return {"enroll_code": code, "enroll_url": code, "qr_svg": build_qr_svg(code), "payload": payload}


@router.post("/mobile-enrollment/new-code", response_model=MobileEnrollmentCode,
             summary="Issue a NEW mobile app enrollment code (config-only)")
def mobile_enrollment_new_code(
    current_user: models.User = Depends(get_current_user),
):
    """BUG.B41 — the GET above is a pure function of public config, so re-GETting
    it returns a byte-identical code and the Dashboard's "new enrollment code"
    click visibly does nothing. This POST issues a FRESH code: same config-only
    payload, plus a random per-issuance nonce (`&n=`, outside the payload) so the
    code string and its QR genuinely change every press. Nothing is stored,
    expired, or redeemed — the nonce only makes successive codes distinct, and
    the app ignores unknown query parameters.
    """
    from utils.paid_import import require_paid
    _m = require_paid("utils.mobile_enrollment", "Mobile device enrollment")
    build_enrollment_payload, build_enrollment_code, build_qr_svg = (
        _m.build_enrollment_payload, _m.build_enrollment_code, _m.build_qr_svg
    )

    org = current_user.tenant.name if getattr(current_user, "tenant", None) else None
    payload = build_enrollment_payload(org or "MOD")
    code = build_enrollment_code(payload, secrets.token_urlsafe(11))
    return {"enroll_code": code, "enroll_url": code, "qr_svg": build_qr_svg(code), "payload": payload}
