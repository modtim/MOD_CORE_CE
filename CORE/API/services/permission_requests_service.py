"""Permission Requests — service layer (Gate 2b query-layer migration).

Business logic / policy for the domain, built entirely on top of
`crud.permission_requests` (no `db.query` here or in the router). Owns the
transaction boundary for writes; the router stays HTTP-only (parse input, call
here, map a `ServiceError` to `HTTPException`, shape the response).

404-vs-403 trap: `crud.get_request_by_id` is tenant-UNSCOPED on purpose. This
module applies the tenant check itself (`get_reviewable_request`) and only
*then* evaluates resource ownership (`authorize_reviewer`). That ordering
matters: a query that filtered by tenant_id AND ownership in one WHERE clause
would turn a legitimate "wrong owner, same tenant" case into an indistinguishable
404, when it should be a 403. Cross-tenant is still folded into `NotFoundError`
(no cross-tenant existence leak — matches the pre-migration behavior, which
also scoped that lookup by tenant).

`services._base` has no `ForbiddenError`, so it is declared here per the
migration rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

import crud.permission_requests as crud
import models
from models import PermissionLevel, PermissionRequestStatus
from permissions import create_permission
from services._base import NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    """Domain validation failure (router -> 400) — mirrors the pre-migration
    inline `HTTPException(400, ...)` call sites verbatim."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# --------------------------------------------------------------------------- #
# Display helpers (used to build the response envelope)
# --------------------------------------------------------------------------- #

def resource_display_name(db: Session, req: "models.PermissionRequest") -> str:
    """Resolve the display name of a request's target (cog, workflow, secret)."""
    if req.resource_type == "workflow":
        wf = crud.get_active_workflow(db, req.resource_id)
        return wf.name if wf else "Unknown workflow"
    if req.resource_type == "secret":
        sec = crud.get_active_secret(db, req.resource_id)
        return sec.name if sec else "Unknown secret"
    cog = crud.get_active_cog(db, req.cog_id)
    return cog.name if cog else "Unknown"


def get_user(db: Session, user_id: Optional[int]) -> Optional["models.User"]:
    """Passthrough to the CRUD layer — routers use this instead of reaching
    into `crud.permission_requests` directly."""
    return crud.get_user(db, user_id)


def requester_and_reviewer(
    db: Session, req: "models.PermissionRequest"
) -> Tuple[Optional["models.User"], Optional["models.User"]]:
    """The requesting user and (if reviewed) the reviewing user."""
    requester = crud.get_user(db, req.requester_id)
    reviewer = crud.get_user(db, req.reviewed_by_user_id) if req.reviewed_by_user_id else None
    return requester, reviewer


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #

def validate_permission_level(raw: str) -> PermissionLevel:
    try:
        return PermissionLevel(raw.lower())
    except ValueError:
        raise BadRequestError(f"Invalid permission level: {raw}")


def create_request(
    db: Session,
    *,
    tenant_id: int,
    requester: "models.User",
    cog_id: int,
    requested_level: str,
    requested_duration_days: Optional[int],
    justification: Optional[str],
) -> Tuple["models.PermissionRequest", "models.Cog"]:
    """Validate + create a permission request. Returns `(request, cog)` — the
    router needs `cog` (name/owner) to build the response and owner notification
    without an extra query."""
    perm_level = validate_permission_level(requested_level)

    cog = crud.get_cog_for_tenant(db, cog_id, tenant_id)
    if not cog:
        raise NotFoundError("Folder not found")

    # Gate 30: read sharing grants from the ResourcePermission read-model
    # (duck-typed to legacy Permission) instead of the legacy Permission table.
    from cog_grants import list_cog_grants
    existing_permission = list_cog_grants(db, user_id=requester.id, cog_ids=[cog_id])
    if existing_permission:
        raise BadRequestError("You already have permission for this folder")

    if cog.owner_id == requester.id:
        raise BadRequestError("You already own this folder")

    existing_request = crud.get_pending_request_for_cog(db, requester.id, cog_id)
    if existing_request:
        raise BadRequestError("You already have a pending request for this folder")

    permission_request = models.PermissionRequest(
        tenant_id=tenant_id,
        requester_id=requester.id,
        cog_id=cog_id,
        requested_level=perm_level,
        requested_duration_days=requested_duration_days,
        justification=justification,
        status=PermissionRequestStatus.PENDING,
    )
    db.add(permission_request)
    db.commit()
    db.refresh(permission_request)
    return permission_request, cog


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #

def list_my_requests(
    db: Session, *, requester_id: int, status_filter: Optional[str]
) -> List["models.PermissionRequest"]:
    status_enum = None
    if status_filter:
        try:
            status_enum = PermissionRequestStatus(status_filter.lower())
        except ValueError:
            raise BadRequestError(f"Invalid status: {status_filter}")
    return crud.list_requests_for_requester(db, requester_id, status_enum)


def list_pending_for_reviewer(db: Session, *, current_user: "models.User") -> List["models.PermissionRequest"]:
    tenant_id = current_user.tenant_id
    owned_cog_ids = crud.list_owned_cog_ids(db, current_user.id, tenant_id)
    owned_wf_ids = crud.list_owned_workflow_ids(db, current_user.id, tenant_id)
    owned_secret_ids = crud.list_owned_secret_ids(db, current_user.id, tenant_id)
    is_admin = bool(getattr(current_user, "is_platform_admin", False))
    return crud.list_pending_requests(
        db,
        tenant_id,
        is_platform_admin=is_admin,
        owned_cog_ids=owned_cog_ids,
        owned_wf_ids=owned_wf_ids,
        owned_secret_ids=owned_secret_ids,
    )


# --------------------------------------------------------------------------- #
# Review (approve/deny)
# --------------------------------------------------------------------------- #

def get_reviewable_request(db: Session, *, request_id: int, tenant_id: int) -> "models.PermissionRequest":
    """Tenant-scoped existence check. The CRUD fetch is unscoped; the tenant
    check is applied here (cross-tenant -> 404, same as the pre-migration
    single tenant-filtered query)."""
    req = crud.get_request_by_id(db, request_id)
    if req is None or req.tenant_id != tenant_id:
        raise NotFoundError("Permission request not found")
    return req


def authorize_reviewer(db: Session, req: "models.PermissionRequest", current_user: "models.User") -> None:
    """Only the target resource's owner may review it. No platform-admin bypass
    (ISO/tenant isolation — an admin cannot approve/deny access to another
    tenant's resource, and this domain has no cross-tenant requests anyway)."""
    if req.resource_type == "workflow":
        wf = crud.get_active_workflow(db, req.resource_id)
        if not wf:
            raise NotFoundError("Workflow not found")
        if wf.created_by_user_id != current_user.id:
            raise ForbiddenError("Only the workflow owner can review this request")
    elif req.resource_type == "secret":
        sec = crud.get_active_secret(db, req.resource_id)
        if not sec:
            raise NotFoundError("Secret not found")
        if getattr(sec, "created_by_user_id", None) != current_user.id:
            raise ForbiddenError("Only the secret owner can review this request")
    else:
        cog = crud.get_active_cog(db, req.cog_id)
        if not cog:
            raise NotFoundError("Folder not found")
        if cog.owner_id != current_user.id:
            raise ForbiddenError("Only the folder owner can review permission requests")


_CAPS_BY_LEVEL = {
    "workflow": {
        "viewer": ["view", "view_details", "view_runs", "view_versions"],
        "editor": ["view", "view_details", "view_runs", "view_versions", "edit", "execute", "create"],
        "owner": ["view", "view_details", "view_runs", "view_versions", "edit", "execute",
                  "create", "share", "manage_permissions", "delete"],
    },
    "secret": {
        "viewer": ["view", "view_value", "use_in_workflow"],
        "editor": ["view", "view_value", "use_in_workflow", "edit"],
        "owner": ["view", "view_value", "use_in_workflow", "edit", "delete", "share", "manage_permissions"],
    },
}


def apply_review(
    db: Session,
    *,
    req: "models.PermissionRequest",
    reviewer: "models.User",
    action: str,
    review_notes: Optional[str],
    override_level: Optional[str],
    override_duration_days: Optional[int],
) -> Tuple["models.PermissionRequest", Optional[Any], Optional[str]]:
    """Validate + apply an approve/deny decision, in the same order the
    pre-migration handler did (already-reviewed -> authorize -> action value).

    Returns `(request, permission, resolved_perm_level)`:
      * `permission` is the created cog `Permission` row, or `None` for a
        capability-based workflow/secret grant (nothing to return there).
      * `resolved_perm_level` is only set on approval (used by the router for
        the audit log + notification email).
    """
    if req.status != PermissionRequestStatus.PENDING:
        raise BadRequestError(f"Request already {req.status.value}")

    authorize_reviewer(db, req, reviewer)

    if action not in ("approve", "deny"):
        raise BadRequestError("Action must be 'approve' or 'deny'")

    req.reviewed_by_user_id = reviewer.id
    req.reviewed_at = datetime.now(timezone.utc)
    req.review_notes = review_notes

    permission = None
    perm_level_str: Optional[str] = None

    if action == "approve":
        req.status = PermissionRequestStatus.APPROVED

        perm_level_str = override_level if override_level else req.requested_level.value
        duration_days = (
            override_duration_days if override_duration_days is not None else req.requested_duration_days
        )

        try:
            perm_level_enum = PermissionLevel(perm_level_str.lower())
        except ValueError:
            raise BadRequestError(f"Invalid permission level: {perm_level_str}")

        expires_at = None
        if duration_days is not None and duration_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

        if req.resource_type in ("workflow", "secret"):
            # Workflow/secret access is capability-based — grant the capability
            # set for the approved level via the resource-permission system.
            from resource_permissions import grant_resource_permission
            rtype = (
                models.ResourceType.WORKFLOW
                if req.resource_type == "workflow"
                else models.ResourceType.SECRET
            )
            caps_by_level = _CAPS_BY_LEVEL[req.resource_type]
            for cap_name in caps_by_level.get(perm_level_enum.value, ["view"]):
                grant_resource_permission(
                    db, reviewer.tenant_id, req.requester_id,
                    rtype, cap_name, req.resource_id,
                    reviewer.id,
                )
            req.granted_permission_id = None
        else:
            # Create the cog permission (level-based)
            permission = create_permission(
                db,
                cog_id=req.cog_id,
                level=perm_level_enum,
                granted_by_user_id=reviewer.id,
                user_id=req.requester_id,
                expires_at=expires_at,
                invitation_context={
                    "request_id": req.id,
                    "justification": req.justification,
                },
            )
            req.granted_permission_id = permission.id

    else:  # deny
        req.status = PermissionRequestStatus.DENIED

    db.commit()
    db.refresh(req)
    return req, permission, perm_level_str


# --------------------------------------------------------------------------- #
# Cancel
# --------------------------------------------------------------------------- #

def cancel_request(db: Session, *, request_id: int, current_user: "models.User") -> "models.PermissionRequest":
    """Only the requester may cancel, and only while still pending. A request
    that exists but belongs to someone else is reported as `NotFoundError`
    (matches the pre-migration query, which scoped by `requester_id` and so
    never distinguished "not yours" from "doesn't exist")."""
    req = crud.get_request_by_id(db, request_id)
    if req is None or req.requester_id != current_user.id:
        raise NotFoundError("Permission request not found")

    if req.status != PermissionRequestStatus.PENDING:
        raise BadRequestError(f"Cannot cancel {req.status.value} request")

    req.status = PermissionRequestStatus.CANCELLED
    db.commit()
    return req


def raise_http(exc: ServiceError) -> None:
    """Map a `ServiceError` to the exact `HTTPException` shape the pre-migration
    router raised inline (plain-string `detail`, not the `ErrorEnvelope` — that
    is still scaffold-only, see api_conventions.py)."""
    from fastapi import HTTPException

    status_map = {"not_found": 404, "forbidden": 403, "bad_request": 400, "conflict": 409}
    raise HTTPException(status_code=status_map.get(exc.code, 400), detail=exc.message)
