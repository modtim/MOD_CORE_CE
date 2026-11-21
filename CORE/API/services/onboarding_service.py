"""Business logic for the onboarding domain (Gate 2b migration).

Moved out of `routers/onboarding.py` so the router stays HTTP-only (parse
input, call the service, shape the response). Data access goes through
`crud.onboarding` (this domain's query-only module) and the existing
top-level `crud` module (invitation / user / tenant helpers already used
elsewhere).

NOT moved here (deliberately — see routers/onboarding.py's module
docstring): `_has_human_platform_admin`, `_onboarding_advisory_lock`, and
`_create_tenant_or_platform_admin` stay physically in the router because a
large existing test surface (test_onboarding_router.py,
test_onboarding_resilience.py, plus the shared test_phase3_comprehensive.py
/ test_recent_fixes.py) monkeypatches / inspects them BY NAME on the
`routers.onboarding` module object, and `_create_tenant_or_platform_admin`
calls `_has_human_platform_admin` as a bare global — moving either would
silently break that monkeypatching (a moved function resolves bare names
against its *new* module's globals, not the router's). Their internal
`db.query(...)` calls were still extracted into `crud.onboarding` in place.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
import httpx
from typing import Any, Dict

from fastapi import HTTPException, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import onboarding as crud_onboarding


# =============================================================================
# Shared validation helper (moved from routers/onboarding.py — re-imported
# there so `_create_tenant_or_platform_admin`'s existing bare-name call site
# keeps working unchanged).
# =============================================================================


def _safe_user_create(**kwargs):
    """LDF-4 — `schemas.UserCreate` runs the LDF-3 §10 username/email
    validators inside its field_validators. When the onboarding routes
    construct the schema from request_body fields, a validation
    failure (e.g. username=`root`/`admin` per the denylist) raises
    pydantic.ValidationError INSIDE the handler — which FastAPI
    surfaces as a generic 500. Catch it and convert to a 422 with
    the field-level message so the FE can render it as a normal
    form error instead of "Internal Server Error".
    """
    try:
        return schemas.UserCreate(**kwargs)
    except PydanticValidationError as e:
        msg = e.errors()[0].get("msg", "Invalid input") if e.errors() else "Invalid input"
        # Pydantic prefixes value-error messages with "Value error, ".
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=msg,
        ) from None


# =============================================================================
# POST /finalize-invited-user
# =============================================================================


def finalize_invited_user(db: Session, *, request_body: Any, keycloak_user: dict) -> models.User:
    """Body of `finalize_invited_user_registration` (routers/onboarding.py).
    Moved verbatim (db.query -> crud.onboarding) — see that handler's
    docstring for the endpoint contract."""
    invitation = crud.get_pending_invitation_by_token(db, token=request_body.invitation_token)
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation code is invalid or has expired."
        )

    if invitation.email.lower() != keycloak_user['email'].lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invitation is not intended for the currently logged-in user."
        )

    user_details_to_create = _safe_user_create(
        keycloak_user_id=keycloak_user['sub'],
        email=keycloak_user['email'],
        username=request_body.username,
        first_name=request_body.first_name,
        last_name=request_body.last_name,
    )

    # GSEC FX.101 part 3 (B) — invitation acceptance is a self-service
    # account creation too: an email whose domain is a VERIFIED domain of
    # a require-SSO tenant must never end up as a password-created account
    # (in THIS invitation's tenant or anywhere else). The refusal is the
    # same 409 `sso_signup_required` + IdP hint the public sign-up gets, so
    # the invitee is sent to the owning organisation's SSO instead. When
    # require_sso is OFF the invitation stands as written — the invited
    # user joins the tenant the invitation names (an explicit admin action,
    # not a stray personal tenant), so no domain re-homing here.
    # 1788b CE carve: signup_domain_routing is paid; with it absent CE applies
    # no require-SSO invitation re-homing policy.
    from utils.paid_import import optional_paid
    _domain_routing = optional_paid("services.signup_domain_routing")
    if _domain_routing is not None:
        _domain_routing.enforce_signup(
            db, keycloak_user['email'], allow_tenant_id=invitation.tenant_id)

    # GSEC FX.108 p1 (owner decision Q95=A) — hosted sign-up AGE ATTESTATION.
    # Invitation acceptance is a self-serve account creation too, so the
    # hosted cloud demands the same 16+ tick it demands on the public sign-up.
    # Taken BEFORE anything is created: a missing tick is a 422 form error and
    # an underage attempt is a neutral 403, and in BOTH cases no users row, no
    # attestation row and no personal data is stored for the refused attempt
    # (data minimisation). On-prem the gate is inert (the owner decision scopes
    # the age rule to the hosted cloud; K-12 under-13 use is on-prem only).
    # Logic + audit rows live in services/signup_age_attestation.py.
    # 1788b CE carve: signup_age_attestation is paid; the hosted-cloud 16+
    # demand is a paid compliance gate, so CE has no age attestation at all —
    # age_decision stays None and no SignupAgeAttestation row is written.
    from utils.paid_import import optional_paid
    _age_attestation = optional_paid("services.signup_age_attestation")
    age_decision = (
        _age_attestation.attest_hosted_signup(
            db, getattr(request_body, "age_attestation", None),
            birth_year=getattr(request_body, "birth_year", None),
        ) if _age_attestation is not None else None
    )

    # Determine parent_id based on whether this is a cross-tenant invitation
    inviter = crud.get_user(db, invitation.invited_by_user_id)

    if inviter and inviter.tenant_id == invitation.tenant_id:
        # Same-tenant invitation: normal cascading hierarchy
        parent_id = invitation.invited_by_user_id
    else:
        # Cross-tenant invitation (platform admin inviting to another tenant)
        # Chain under existing root parent in target tenant
        root_parent = crud_onboarding.get_root_parent_user(db, invitation.tenant_id)

        if root_parent:
            parent_id = root_parent.id
            logging.info(f"Cross-tenant invite: Chaining user under root parent {root_parent.id} in tenant {invitation.tenant_id}")
        else:
            # No root parent exists in target tenant - make this user root
            parent_id = None
            logging.info(f"Cross-tenant invite: No root parent found in tenant {invitation.tenant_id}, creating as root")

    # Log the role assignment
    if invitation.custom_role_id:
        logging.info(f"Assigning role {invitation.custom_role_id} to invited user {keycloak_user['email']}")
    else:
        logging.warning(f"No role specified in invitation for user {keycloak_user['email']}")

    try:
        new_user = crud.create_user(
            db=db,
            user_details=user_details_to_create,
            tenant_id=invitation.tenant_id,
            parent_id=parent_id,
            custom_role_id=invitation.custom_role_id,  # Assign the role from the invitation
        )
    except IntegrityError as e:
        db.rollback()
        # Check if it's a duplicate username or email
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

    # GSEC FX.108 p1 — the account exists now, so the attestation the gate
    # just accepted becomes a RECORD: one models.SignupAgeAttestation row
    # (user + tenant + band + timestamp) and one AGE_ATTESTATION_RECORDED
    # audit row. Best effort inside the service: a record hiccup never
    # unwinds a sign-up that already succeeded.
    # 1788b CE carve: no age attestation in CE -> nothing to record.
    from utils.paid_import import optional_paid
    _age_attestation = optional_paid("services.signup_age_attestation")
    if _age_attestation is not None and age_decision:
        _age_attestation.record_attestation(
            db, user_id=new_user.id,
            tenant_id=invitation.tenant_id,
            decision={**age_decision,
                      "route": "/onboarding/finalize-invited-user"})

    crud.accept_invitation(db, db_invitation=invitation, accepted_by_user_id=new_user.id)

    # Audit: user created via invitation (CMMC AU-2)
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=invitation.tenant_id, user_id=new_user.id,
            event_name="USER_CREATED",
            # Q3b: ID-only audit — the email is PII; the actor's id is
            # already on the row (user_id=new_user.id).
            details={"method": "invitation", "created_user_id": new_user.id, "invited_by": invitation.invited_by_user_id},
        ))
        db.commit()
    except Exception:
        pass

    # FX.140 part 3 (owner Q128=A): the sign-up marketing opt-in checkbox
    # is an explicit consent decision — append a source="signup" event
    # (granted = the checkbox, default False = unticked at sign-up) + one
    # audit row (category/granted/source, never the email address).
    try:
        from services.email_unsubscribe import record_signup_consent
        record_signup_consent(
            db, new_user,
            marketing_opt_in=getattr(request_body, "marketing_opt_in", False))
        db.commit()
        from audit import log_security_event
        log_security_event(
            db=db,
            action=models.AuditAction.EMAIL_CONSENT_CHANGED,
            user_id=new_user.id,
            tenant_id=invitation.tenant_id,
            metadata={"category": "marketing",
                      "granted": bool(getattr(
                          request_body, "marketing_opt_in", False)),
                      "source": "signup"},
        )
    except Exception:
        pass

    return new_user


# =============================================================================
# POST /invitations/accept  (2400LR CE: registration is OFF, so the invite
# link itself is the credential — mirrors services/external_invitations_service
# ._provision_external_keycloak_user / routers/external_invitations.py:116)
# =============================================================================

_BAD_TOKEN = "Invitation is invalid, expired, or already accepted."


def _kc_policy_message(response) -> str:
    """Keycloak's password-policy refusal, message only. The raw KC body
    can carry realm/user internals, so only the policy `messages` strings
    (or a generic line) are surfaced — never the payload."""
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        msgs = data.get("messages") or data.get("error_description") or []
        if isinstance(msgs, str):
            msgs = [msgs]
        cleaned = [str(m).strip() for m in msgs if str(m).strip()]
        if cleaned:
            return " ".join(cleaned)[:500]
    return "The password did not meet the realm's password policy."


def _invitation_username(invitation_email: str, invitation_id: int) -> str:
    """DB username for an invited user. The KC username IS the email
    (this realm runs registrationEmailAsUsername=true — see
    services/external_invitations_service.py:81), but the DB `username`
    column is unique + validated (validators/identity.py:82
    `^[A-Za-z0-9_-]+$`, 3..32, denylist), which an email never satisfies.
    Derive the local part, sanitise, and fall back to an id-based name so
    the value is always validator-clean without ever echoing the email."""
    from validators.identity import validate_username, UsernameValidationError
    local = (invitation_email.split("@", 1)[0] if "@" in invitation_email
             else invitation_email)
    candidate = re.sub(r"[^A-Za-z0-9_-]", "", local)[:32]
    for candidate in (candidate, f"inv-{invitation_id}"[:32]):
        try:
            return validate_username(candidate)
        except UsernameValidationError:
            continue
    return f"inv-{invitation_id}"[:32]


def accept_invitation(
    db: Session,
    *,
    token: str,
    first_name: str,
    last_name: str,
    password: str,
) -> Dict[str, Any]:
    """Body of `accept_invitation_public` (routers/onboarding.py).

    2400LR CE signup: with realm `registrationAllowed:false` the KC
    registration flow is gone, so the invitation token in the accept
    link IS the credential. This mirrors the shipped external-invite
    pattern (services/external_invitations_service.py:73-102 +
    routers/external_invitations.py:116): server-side KC create, then
    the existing finalize path for the DB side.
    """
    # 1. Look up via the existing crud helper (pending + unexpired only),
    # then take a row lock so two concurrent accepts cannot both pass.
    invitation = crud.get_pending_invitation_by_token(db, token=token)
    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_BAD_TOKEN,
        )
    invitation_id = invitation.id
    locked = (
        db.query(models.Invitation)
        .filter(models.Invitation.id == invitation_id)
        .with_for_update()
        .first()
    )
    if (not locked
            or locked.status != models.InvitationStatus.PENDING
            or not (locked.expires_at > datetime.now(tz=timezone.utc))):
        # Second concurrent accept lands here: the row is already
        # ACCEPTED (or expired) once the first one commits.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_BAD_TOKEN,
        )
    invitation = locked

    # 2. Identity comes from the INVITATION, never from the body.
    email = invitation.email

    # 3. Server-side KC create — payload shape copied from
    # services/external_invitations_service.py:85-97.
    from keycloak_auth import keycloak_admin_client
    payload = {
        "username": email,
        "email": email,
        "enabled": True,
        "emailVerified": True,
        "firstName": first_name,
        "lastName": last_name,
        "credentials": [{"type": "password", "value": password,
                         "temporary": False}],
    }
    try:
        keycloak_sub = keycloak_admin_client.create_user(payload)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 409:
            # A KC user already owns that email — tell them to log in.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "account_exists"},
            ) from None
        if code == 400:
            # Password policy refusal: KC's message only, never the raw
            # body (which can carry realm/user internals).
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=_kc_policy_message(exc.response),
            ) from None
        logging.error("Invitation accept: Keycloak create failed for invitation %s: %s",
                     invitation_id, exc)
        raise

    # 4. Reuse finalize_invited_user verbatim for the child-user / role /
    # tenant logic; it also marks the invitation ACCEPTED
    # (crud/invitation_cud.py:96-105 sets status=ACCEPTED) and emits the
    # USER_CREATED audit row (services/onboarding_service.py:208-214) —
    # no extra audit event here (MUST 6).
    from routers.onboarding import FinalizeInvitedUserRequest
    request_body = FinalizeInvitedUserRequest(
        invitation_token=token,
        username=_invitation_username(email, invitation_id),
        first_name=first_name,
        last_name=last_name,
    )
    try:
        finalize_invited_user(
            db, request_body=request_body,
            keycloak_user={"sub": keycloak_sub, "email": email},
        )
    except HTTPException:
        # Structured 4xx from finalize (SSO re-home, age gate, IntegrityError
        # mapping) — pass through as-is, same as create-new-tenant's wrapper
        # (routers/onboarding.py:438-440).
        raise
    except Exception as exc:  # noqa: BLE001 — the §1.6/§1.7 catch-all
        # MUST 5 (lead decision, 2400LR-a1R): the KC account exists but the
        # DB side blew up. Roll the DB back, then DISABLE the orphan KC
        # account (keycloak_auth.py:disable_user, right after delete_user
        # :560 — httpx PUT /admin/realms/.../users/{id} {"enabled": false}).
        # ISO no-hard-delete: NEVER delete_user. If the disable itself fails,
        # log invitation id + kc user id + the exception CLASS name only
        # (never the body) and still let the router answer the §1.6 envelope
        # (routers/onboarding.py:65 _envelope_500).
        try:
            db.rollback()
        except Exception:
            pass
        try:
            keycloak_admin_client.disable_user(keycloak_sub)
        except Exception as disable_exc:
            logging.error(
                "Invitation accept: finalize failed for invitation %s (kc user %s) "
                "and the disable also failed (%s): %s",
                invitation_id, keycloak_sub, type(disable_exc).__name__, exc,
            )
        else:
            logging.error(
                "Invitation accept: finalize failed for invitation %s; kc user %s disabled",
                invitation_id, keycloak_sub,
            )
        raise

    return {"status": "accepted"}

# Moved verbatim from routers/onboarding.py — nothing monkeypatches these by
# name; the router re-exports `CHECKLIST_STEPS` / `REWARD_CREDITS` from here
# for back-compat (`from routers.onboarding import CHECKLIST_STEPS`).

# launch-day-followup-5 §7 — Expanded Getting Started.
# Steps are detection-based: each step auto-completes when the user
# does the real product action. No "Mark complete" buttons. New
# steps in §7 are: invite_team_member, complete_workflow_run,
# create_api_key, setup_payment_method (SaaS-only). Microcopy
# updated to a warmer, conversational tone per brand-guide §7.
#
# `setup_payment_method` is suppressed in on-prem mode (no Stripe).
_BASE_CHECKLIST_STEPS = [
    {"key": "complete_profile", "title": "Set up your profile", "description": "Tell us a bit about yourself", "action": None},
    {"key": "upload_file", "title": "Upload your first file", "description": "Drop in a file you'd like to work with", "action": "#files"},
    {"key": "create_workflow", "title": "Build your first workflow", "description": "Connect a few nodes to make something happen", "action": "#workflows"},
    {"key": "run_workflow", "title": "Run it", "description": "Watch your workflow do its thing", "action": "#workflows"},
    {"key": "complete_workflow_run", "title": "Land a clean run", "description": "Get a workflow all the way to success", "action": "#workflows"},
    {"key": "invite_team_member", "title": "Invite a teammate", "description": "Bring someone along — collaboration is more fun", "action": "#users"},
    {"key": "create_api_key", "title": "Create an API key", "description": "Hook MOD into something else", "action": "#account"},
]

_SAAS_ONLY_STEPS = [
    {"key": "setup_payment_method", "title": "Add a payment method", "description": "Keep things running smoothly when your trial ends", "action": "#billing"},
]


# CE-only deep links: API keys live in Settings -> API Keys and the invite
# form is on the Invitations page. Paid keeps `#account` / `#users`.
_CE_ACTION_OVERRIDES = {
    "create_api_key": "#settings",
    "invite_team_member": "#invitations",
}


def _checklist_steps_for_mode() -> list:
    """Return the checklist appropriate to the deployment mode.
    SaaS appends payment-method step; on-prem omits it. CE (billing is
    blocked there) never gets the payment step, whatever DEPLOYMENT_MODE
    says, and links to the CE pages for the API-key and invite steps."""
    ce = _is_ce_for_reward()
    deployment_mode = os.getenv("DEPLOYMENT_MODE", "saas").lower()
    if ce:
        return [
            {**s, "action": _CE_ACTION_OVERRIDES.get(s["key"], s["action"])}
            for s in _BASE_CHECKLIST_STEPS
        ]
    if deployment_mode == "saas":
        return _BASE_CHECKLIST_STEPS + _SAAS_ONLY_STEPS
    return list(_BASE_CHECKLIST_STEPS)


REWARD_CREDITS = 500


def get_checklist(db: Session, current_user: models.User) -> Dict[str, Any]:
    """Body of `get_onboarding_checklist` (routers/onboarding.py)."""
    tid = current_user.tenant_id

    profile_done = current_user.onboarding_complete or False

    file_count = crud_onboarding.count_files(db, tid)

    # Exclude is_system workflows: wizard node-build DAGs run as system
    # workflows under the building tenant, but they are NOT the user
    # "building their first workflow" — counting them falsely completes the
    # step (and, with the old FE, spammed the celebration popup).
    workflow_count = crud_onboarding.count_workflows(db, tid)

    run_count = crud_onboarding.count_workflow_runs(db, tid)

    # launch-day-followup-5 §7 — new detection queries

    # complete_workflow_run: at least one WorkflowRun reached terminal
    # success. Terminal success for a WorkflowRun is TaskStatus.COMPLETED
    # (verified in the DB — there is no "SUCCESS" WorkflowRun row; the prior
    # string filter silently matched nothing, so this checklist step never
    # tripped). Use the enum so it matches what the orchestrator actually
    # writes.
    success_run_count = 0
    try:
        success_run_count = crud_onboarding.count_successful_workflow_runs(db, tid)
    except Exception:
        # Defensive: enum-name mismatch / missing column shouldn't break
        # checklist rendering.
        success_run_count = 0

    # invite_team_member: at least one non-deleted invitation for the
    # tenant. Counts pending + accepted; both are meaningful "the
    # tenant has invited people" signals.
    invitation_count = crud_onboarding.count_invitations(db, tid)

    # create_api_key: at least one ApiKey owned by any user in this
    # tenant. ApiKey has user_id, not tenant_id; join via User.
    api_key_count = 0
    try:
        api_key_count = crud_onboarding.count_api_keys(db, tid)
    except Exception:
        api_key_count = 0

    # setup_payment_method (SaaS only): tenant has a Stripe payment
    # method on file.
    deployment_mode = os.getenv("DEPLOYMENT_MODE", "saas").lower()
    payment_method_set = False
    if deployment_mode == "saas" and not _is_ce_for_reward():
        tenant = crud_onboarding.get_tenant_by_id(db, tid)
        if tenant is not None:
            payment_method_set = bool(getattr(tenant, "stripe_payment_method_id", None))

    completed = {
        "complete_profile": profile_done,
        "upload_file": file_count > 0,
        "create_workflow": workflow_count > 0,
        "run_workflow": run_count > 0,
        "complete_workflow_run": success_run_count > 0,
        "invite_team_member": invitation_count > 0,
        "create_api_key": api_key_count > 0,
        "setup_payment_method": payment_method_set,
    }

    # Resolve steps for this mode (SaaS gets the payment step, on-prem
    # doesn't). Keeps `total` / `all_done` accurate per deployment.
    active_steps = _checklist_steps_for_mode()
    done_count = sum(1 for s in active_steps if completed.get(s["key"], False))
    total = len(active_steps)

    reward_claimed = crud_onboarding.get_checklist_reward(db, tid) is not None

    steps = []
    for step in active_steps:
        steps.append({**step, "completed": completed.get(step["key"], False)})

    return {
        "steps": steps,
        "completed": done_count,
        "total": total,
        "all_done": done_count == total,
        # CE has no bonus credits: do not advertise a reward there.
        "reward_claimed": False if _is_ce_for_reward() else reward_claimed,
        "reward_credits": 0 if _is_ce_for_reward() else REWARD_CREDITS,
    }


def _is_ce_for_reward() -> bool:
    """True when bonus credits must not exist (MOD Community Edition).

    Fail-closed: a server-side GRANT must never happen because the edition
    probe errored, so an erroring probe counts as CE (no grant).
    """
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:
        return True


# Back-compat — existing imports of CHECKLIST_STEPS keep working. Defined
# after _is_ce_for_reward because the step list depends on the edition.
CHECKLIST_STEPS = _checklist_steps_for_mode()


def ensure_checklist_reward_available() -> None:
    """402 on CE, same shape as the other CE-by-edition routes (listing_visibility)."""
    if _is_ce_for_reward():
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Onboarding bonus credits are not available in MOD Community Edition.",
        )


def claim_checklist_reward(db: Session, current_user: models.User) -> Dict[str, Any]:
    """Body of `claim_checklist_reward` (routers/onboarding.py)."""
    # CE defence in depth: no credit call and no reward row, before any DB work.
    ensure_checklist_reward_available()
    tid = current_user.tenant_id

    checklist = get_checklist(db, current_user)
    if not checklist["all_done"]:
        raise HTTPException(status_code=400, detail="Not all steps completed yet")

    existing = crud_onboarding.get_checklist_reward(db, tid)
    if existing:
        raise HTTPException(status_code=400, detail="Reward already claimed")

    try:
        from utils.credit_manager import CreditManager
        CreditManager.add_credits(
            db=db, tenant_id=tid, amount=REWARD_CREDITS,
            user_id=current_user.id, notes="Onboarding checklist completion bonus",
        )
    except Exception as e:
        logging.warning(f"Failed to grant checklist reward credits: {e}")

    reward = models.OnboardingChecklistReward(tenant_id=tid, credits_granted=REWARD_CREDITS)
    db.add(reward)
    db.commit()

    return {"message": f"{REWARD_CREDITS} bonus credits granted!", "credits": REWARD_CREDITS}
