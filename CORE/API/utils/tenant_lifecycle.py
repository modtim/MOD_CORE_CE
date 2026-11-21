"""GSEC FX.59 part 2 (owner decision Q42=A) — tenant contract-end
lifecycle enforcement.

State machine on `tenants.lifecycle_state`:

    active     normal operation
    read_only  days 0-30 after contract end — writes blocked (423),
               reads and self-serve export stay open
    suspended  day 30-60 — everything blocked (reads 403, writes 423);
               reactivation within the grace period restores everything

This module owns:

* the FastAPI enforcement dependencies — `require_tenant_writable`
  (write routes: 423 Locked when the caller's tenant is read_only or
  suspended) and `require_tenant_active` (read routes: 403 when
  suspended). Platform admins bypass both.
* the single state-mutation entry point `set_lifecycle_state(db,
  tenant, state, reason, actor)` — validates the value, stamps
  lifecycle_changed_at / lifecycle_reason, and writes the
  TENANT_LIFECYCLE_STATE_CHANGED audit row. Part 4's admin
  suspension/reactivation endpoints call this.

Timings (when to flip the state) come from the platform settings
`tenant_lifecycle.read_only_days` / `tenant_lifecycle.export_window_days`
and are driven by part 4's sweep; this module only enforces whatever
state the row carries, so a manual admin flip is honoured immediately.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from dependencies import (get_db, get_optional_current_user,
                          require_active_and_onboarded_user)
import models

logger = logging.getLogger(__name__)

# Canonical state-machine values (mirrors the ck_tenants_lifecycle_state
# CHECK constraint in models.py / migration 20261007i).
ACTIVE = "active"
READ_ONLY = "read_only"
SUSPENDED = "suspended"
VALID_LIFECYCLE_STATES = (ACTIVE, READ_ONLY, SUSPENDED)

# FX.59 p4a/p4b — the ONE transition-legality rule, shared by the HTTP
# surface (routers/platform_tenant_lifecycle.py) and the admin CLI
# (scripts/tenant_lifecycle_admin.py). A transition is legal only from the
# states listed here; a tenant already in the target state is rejected
# rather than silently no-op'd, so a double-click or a stale admin UI
# surfaces instead of hiding a mistake.
LEGAL_TRANSITIONS = {
    SUSPENDED: (ACTIVE, READ_ONLY),
    ACTIVE: (SUSPENDED, READ_ONLY),
}

# Wording used by the router's 409 detail; kept byte-identical to the
# responses shipped by part 4a.
TRANSITION_VERB = {SUSPENDED: "suspend", ACTIVE: "reactivate"}


def check_transition(current: Optional[str], target: str) -> None:
    """Raise ValueError on an illegal lifecycle transition, else return None.

    `current` is the tenant's stored `lifecycle_state` (None is read as
    `active`, matching the column default). `target` is the state being
    moved to. The message is the same one the router returns as its 409
    detail, so the CLI and the API refuse the same transitions with the
    same words.
    """
    if target not in LEGAL_TRANSITIONS:
        raise ValueError(
            f"invalid lifecycle state {target!r}; expected one of "
            f"{tuple(LEGAL_TRANSITIONS)}"
        )
    current_state = current or ACTIVE
    if current_state not in LEGAL_TRANSITIONS[target]:
        raise ValueError(
            f"Tenant is already {current_state}; cannot {TRANSITION_VERB[target]}"
        )


def _tenant_state(db: Session, tenant_id: Optional[int]) -> Optional[str]:
    """Current lifecycle_state for a tenant, or None if unknown.

    A lookup failure must not break the request path: this gate is an
    ADDITION on top of existing auth, and a gate error must not take
    down tenants that are not in a lifecycle window (same
    fail-open-then-warn stance as the FX.92 export-window gate)."""
    if not tenant_id:
        return None
    try:
        state = db.query(models.Tenant.lifecycle_state).filter(
            models.Tenant.id == tenant_id
        ).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.59 lifecycle gate lookup failed (proceeding): %s", e)
        return None
    return state


def _require_writable_for(db: Session, current_user: models.User) -> models.User:
    """Core WRITE-path lifecycle check (no FastAPI dependency wiring) —
    shared by ``require_tenant_writable`` (route-level) and
    ``tenant_lifecycle_gate`` (router-level, FX.59 p2b). Raises 423 Locked
    when the tenant is read_only or suspended; platform admins bypass."""
    if getattr(current_user, "is_platform_admin", False):
        return current_user
    state = _tenant_state(db, getattr(current_user, "tenant_id", None))
    if state == READ_ONLY:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "error": "Tenant is read-only",
                "code": "tenant_read_only",
                "message": ("Your contract has ended. Your workspace is "
                            "read-only while your export window runs — "
                            "you can still download files, export workflow "
                            "JSON, run results and the audit log. "
                            "Resubscribe to restore normal access, or "
                            "export everything before the window ends."),
            },
        )
    if state == SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "error": "Tenant is suspended",
                "code": "tenant_suspended",
                "message": ("Your workspace is suspended. Reactivate the "
                            "contract to restore access; your data is "
                            "intact."),
            },
        )
    return current_user


def require_tenant_writable(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Dependency for WRITE routes: 423 Locked when the caller's tenant
    is read_only or suspended. Platform admins bypass (they manage the
    lifecycle, not live in it). Reads never take this dependency.

    Rides on `require_active_and_onboarded_user` (not the raw
    get_current_user) so it is resolved once per request when the route
    already takes it, and stays overridable in tests the same way the
    routes' own auth dependency is overridden. The check itself lives in
    `_require_writable_for` (shared with the router-level gate, FX.59
    p2b)."""
    return _require_writable_for(db, current_user)


def _require_active_for(db: Session, current_user: models.User) -> models.User:
    """Core READ-path lifecycle check (no FastAPI dependency wiring) —
    shared by ``require_tenant_active`` (route-level) and
    ``tenant_lifecycle_gate`` (router-level, FX.59 p2b). Raises 403 when
    the tenant is suspended; read_only still allows reads; platform
    admins bypass."""
    if getattr(current_user, "is_platform_admin", False):
        return current_user
    state = _tenant_state(db, getattr(current_user, "tenant_id", None))
    if state == SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "Tenant is suspended",
                "code": "tenant_suspended",
                "message": ("Your workspace is suspended and reads are "
                            "blocked. Reactivate the contract to restore "
                            "access; your data is intact."),
            },
        )
    return current_user


def require_tenant_active(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Dependency for READ routes: 403 when the caller's tenant is
    suspended (read_only still allows reads — the whole point of the
    export window). Platform admins bypass."""
    return _require_active_for(db, current_user)


# GSEC FX.59 p2b — central tenant-lifecycle gate for router-level wiring
# (main.py attaches it to the tenant-scoped include_router calls so every
# tenant-scoped route inherits the checks without per-route edits).
#
# Exempt path prefixes: these must keep working while a tenant is
# read_only or suspended — auth/login/session endpoints (the user needs
# to still sign in and see their account state), billing/subscription/
# checkout (a suspended tenant must be able to PAY), tenant export
# (/v1/tenant/export is the one thing a read_only/suspended tenant may
# do during the export window, Q42=A), support (support chat/tickets +
# stream tickets), health, webhooks (signature-authenticated platform
# callbacks, not tenant-user auth), platform-admin (/v1/platform/ +
# /v1/admin/), and worker endpoints (worker/service auth, not tenant
# users).
GATE_EXEMPT_PREFIXES = (
    "/v1/auth/",
    "/v1/me/",
    "/v1/onboarding/",
    "/v1/billing/",
    "/v1/subscriptions/",
    "/v1/checkout/",
    "/v1/tenant/export",
    "/v1/support",
    "/v1/stream-tickets",
    "/v1/health",
    "/v1/webhooks",
    "/v1/stripe-webhooks",
    "/v1/marketplace/purchase-webhook",
    "/v1/marketplace/tenant-link",
    "/v1/platform/",
    "/v1/admin/",
    "/v1/workers",
    "/v1/node-credentials/",
)


def _current_user_for_gate(
    db: Session,
    x_api_key: Optional[str],
    interactive_user: Optional[models.User],
) -> Optional[models.User]:
    """Resolve the caller for the gate: an X-API-Key header (programmatic
    tenant access) wins, else the interactive bearer user. Never raises —
    a resolution failure means "no authenticated tenant user" (public
    path), which the gate skips."""
    if x_api_key:
        try:
            import crud
            from security import verify_api_key
            row = crud.get_api_key_by_key_prefix(db, key_prefix=x_api_key)
            if row and verify_api_key(x_api_key, row.hashed_key):
                u = crud.get_user(db, user_id=row.user_id)
                if u is not None:
                    return u
        except Exception as e:  # noqa: BLE001 — fail open (route's own auth
            # dependency still enforces the key; the gate is an addition)
            logger.warning("FX.59 p2b gate API-key resolution failed "
                           "(skipping): %s", e)
    return interactive_user


def tenant_lifecycle_gate(
    request: Request,
    db: Session = Depends(get_db),
    interactive_user: Optional[models.User] = Depends(get_optional_current_user),
    x_api_key: Optional[str] = Header(None),
):
    """Method-aware tenant-lifecycle gate for router-level wiring
    (FX.59 p2b). Attached once to the tenant-scoped router includes in
    main.py:

    * mutating methods (POST/PUT/PATCH/DELETE/...) -> ``require_tenant_writable``
      semantics: 423 Locked when the caller's tenant is read_only or
      suspended;
    * safe methods (GET/HEAD/OPTIONS) -> ``require_tenant_active``
      semantics: 403 when suspended, reads still open in read_only.

    Skipped (no-op) when the path is exempt (see ``GATE_EXEMPT_PREFIXES``)
    or when no authenticated tenant user could be resolved (public /
    worker / signature-auth endpoints — their own auth dependencies
    still apply). Platform admins bypass (their routes are gated by
    ``require_platform_admin``; the gate must not double-enforce on
    them)."""
    path = getattr(request.url, "path", "") or ""
    if path.startswith(GATE_EXEMPT_PREFIXES):
        return None
    user = _current_user_for_gate(db, x_api_key, interactive_user)
    if user is None:
        return None
    if getattr(user, "is_platform_admin", False):
        return user
    method = (request.method or "").upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return _require_active_for(db, user)
    return _require_writable_for(db, user)


def set_lifecycle_state(
    db: Session,
    tenant: models.Tenant,
    state: str,
    reason: Optional[str] = None,
    actor: Optional[int] = None,
) -> models.Tenant:
    """Change a tenant's lifecycle state (part 4's admin endpoints call
    this — there is no self-serve path to it).

    Validates `state` against VALID_LIFECYCLE_STATES (ValueError on an
    unknown value), stamps lifecycle_changed_at / lifecycle_reason,
    writes the TENANT_LIFECYCLE_STATE_CHANGED audit row, and commits.
    """
    if state not in VALID_LIFECYCLE_STATES:
        raise ValueError(
            f"invalid lifecycle state {state!r}; expected one of "
            f"{VALID_LIFECYCLE_STATES}"
        )
    from_state = getattr(tenant, "lifecycle_state", None) or ACTIVE
    tenant.lifecycle_state = state
    tenant.lifecycle_changed_at = datetime.now(timezone.utc)
    tenant.lifecycle_reason = (reason or "")[:500] or None
    db.add(tenant)

    from audit import log_security_event
    log_security_event(
        db=db,
        action=models.AuditAction.TENANT_LIFECYCLE_STATE_CHANGED,
        user_id=actor,
        tenant_id=tenant.id,
        metadata={
            "from_state": from_state,
            "to_state": state,
            "reason": tenant.lifecycle_reason,
            "tenant_name": tenant.name,
        },
    )
    db.commit()
    db.refresh(tenant)
    return tenant
