"""Permissions-router service — business logic for `routers/permissions.py`
(Gate 2b migration).

Named `permissions_router_service` (not `permissions_service`) to avoid
clashing with the top-level `permissions.py` policy module, which this file
composes rather than replaces: capability checks (`can_user_share_cog`,
`validate_permission_grant`, ...) still live there and are called from here
(or, where a test patches the name at `routers.permissions.<name>`, from the
router itself — see the note on each endpoint below).

404-vs-403 trap (shared rule #5): every CRUD fetch this module calls
(`crud.get_cog_in_tenant`, `crud.get_workflow_in_tenant`, `crud.get_user_in_tenant`,
`crud.get_team_in_tenant`) is tenant-scoped at the SQL level (cross-tenant is
folded into "not found" -> 404, matching the pre-migration inline queries
exactly). The separate `can_user_share_cog` / `authorize` capability checks
are what produce a 403 for an in-tenant caller who lacks the capability. The
one exception is `crud.get_cog_by_id` / `crud.get_user_by_id`, which are
deliberately UNSCOPED — see their docstrings.

Router-owned auth gates (NOT moved here): `can_user_share_cog`,
`get_effective_permissions_for_cog`, `get_inherited_permissions`, and
`get_user_permission_on_cog` are called directly by `routers/permissions.py`,
not from this module, because existing tests patch them at
`routers.permissions.<name>` (e.g. `test_small_modules_batch32.py`,
`test_routers_smoke_batch9.py`). Moving those calls in here would silently
stop those patches from taking effect (shared rule #7 — preserve patch
sites). Everything else — the actual query/mutation/audit work behind those
gates — lives here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request
from sqlalchemy.orm import Session

import audit
import models
from cog_grants import CogShareSpec, list_cog_grants
from crud import permissions_router as crud
from models import PermissionLevel
from permissions import (
    create_permission as create_permission_helper,
    get_permission_summary,
    validate_permission_grant,
)
from resource_permissions import (
    sync_cog_permission_grants,
    revoke_stale_cog_share_caps,
)
from utils.keycloak_client import validate_group_belongs_to_tenant
from services._base import NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403).

    `services._base` has no `ForbiddenError`, so it is declared here per the
    Gate 2b migration rules (shared rule #5).
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    """Domain validation failure (router -> 400) — mirrors the pre-migration
    inline `HTTPException(400, ...)` call sites verbatim."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


class UnprocessableEntityError(ServiceError):
    """Request shape is valid JSON but semantically invalid (router -> 422),
    matching the pre-migration `HTTP_422_UNPROCESSABLE_ENTITY` call site."""

    def __init__(self, message: str = "Unprocessable entity", *, details: Any = None):
        super().__init__(message, code="unprocessable_entity", details=details)


# Capability set granted per workflow share level. Mirrors the workflow branch
# of routers/permission_requests.py::review_permission_request so both paths
# grant identical access. "editor" is enough to see + open + run a shared
# workflow.
WORKFLOW_CAPS_BY_LEVEL = {
    "viewer": ["view", "view_details", "view_runs", "view_versions"],
    "editor": ["view", "view_details", "view_runs", "view_versions",
               "edit", "execute", "create"],
    "owner": ["view", "view_details", "view_runs", "view_versions",
              "edit", "execute", "create", "share", "manage_permissions",
              "delete"],
}


# --------------------------------------------------------------------------- #
# Response-shaping helper (shared by list / grant / update / get-my-permission)
# --------------------------------------------------------------------------- #

def permission_row_to_dict(perm, *, is_inherited: Optional[bool] = None) -> Dict[str, Any]:
    """Build the `schemas.Permission` field dict for a grant row.

    `get_permission_summary` already supplies `level` + the effective
    capability/tracking fields; this just adds the identity fields every call
    site repeated inline.
    """
    d: Dict[str, Any] = {
        "id": perm.id,
        "target_type": perm.target_type.value,
        "target_id": perm.target_id,
        "user_id": perm.user_id,
        "team_id": perm.team_id,
        "keycloak_group_id": perm.keycloak_group_id,
    }
    if is_inherited is not None:
        d["is_inherited"] = is_inherited
    d.update(get_permission_summary(perm))
    return d


# --------------------------------------------------------------------------- #
# GET /cogs/{cog_id}
# --------------------------------------------------------------------------- #

def build_cog_permission_list(
    db: Session, permissions: List[Any], cog_id: int
) -> List[Dict[str, Any]]:
    """Enrich a list of grant rows (direct or effective/inherited) with
    grantee email/name/tenant display fields.

    `permissions` is whatever the router's `can_user_share_cog`-gated branch
    already fetched (`get_effective_permissions_for_cog` or `list_cog_grants`
    — both router-owned calls, see module docstring).
    """
    user_cache: Dict[int, Any] = {}
    tenant_cache: Dict[int, str] = {}
    result: List[Dict[str, Any]] = []

    for perm in permissions:
        user_email = None
        user_name = None
        user_tenant_id = None
        user_tenant_name = None
        if perm.user_id:
            if perm.user_id not in user_cache:
                user_cache[perm.user_id] = crud.get_user_by_id(db, perm.user_id)
            u = user_cache.get(perm.user_id)
            if u:
                user_email = u.email
                user_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username
                user_tenant_id = u.tenant_id
                if u.tenant_id not in tenant_cache:
                    t = crud.get_tenant_by_id(db, u.tenant_id)
                    tenant_cache[u.tenant_id] = t.name if t else f"Tenant {u.tenant_id}"
                user_tenant_name = tenant_cache.get(u.tenant_id)

        row = permission_row_to_dict(perm, is_inherited=(perm.target_id != cog_id))
        row["user_email"] = user_email
        row["user_name"] = user_name
        row["tenant_id"] = user_tenant_id
        row["tenant_name"] = user_tenant_name
        result.append(row)

    return result


# --------------------------------------------------------------------------- #
# POST /cogs/{cog_id}
# --------------------------------------------------------------------------- #

def maybe_log_cross_tenant_cog_share_blocked(
    db: Session,
    current_user: "models.User",
    cog_id: int,
    http_request: Optional[Request],
) -> None:
    """data-files-launch-bulletproof §0.5 — emit a cross-tenant audit event if
    a platform admin was denied `can_user_share_cog` on a cog in another
    tenant. `crud.get_cog_by_id` is deliberately unscoped so this can see the
    real tenant to compare against. Best-effort: any failure here must not
    mask the caller's original 403."""
    try:
        if current_user.is_platform_admin:
            cog = crud.get_cog_by_id(db, cog_id)
            if cog and cog.tenant_id != current_user.tenant_id:
                audit.log_cross_tenant_data_access_blocked(
                    db=db,
                    admin_user_id=current_user.id,
                    target_tenant_id=cog.tenant_id,
                    request=http_request,
                    resource_type="cog",
                    resource_id=cog_id,
                    endpoint_path=http_request.url.path if http_request else None,
                    method="POST",
                )
    except Exception:
        pass


def grant_cog_permission(
    db: Session,
    current_user: "models.User",
    cog_id: int,
    permission: "Any",  # schemas.PermissionCreate
    http_request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Validate + create a cog permission grant (everything after the
    router's `can_user_share_cog` gate). Raises `UnprocessableEntityError`,
    `ForbiddenError`, `NotFoundError`, or `BadRequestError` for the router to
    map to the matching status code."""
    provided = sum([
        permission.user_id is not None,
        permission.team_id is not None,
        permission.keycloak_group_id is not None,
    ])
    if provided != 1:
        raise UnprocessableEntityError(
            "Exactly one of user_id, team_id, or keycloak_group_id must be provided"
        )

    if permission.keycloak_group_id:
        if not validate_group_belongs_to_tenant(permission.keycloak_group_id, current_user.tenant_id):
            raise ForbiddenError("The specified Keycloak group does not belong to your organization")

    if permission.user_id:
        target_user = crud.get_user_in_tenant(db, permission.user_id, current_user.tenant_id)
        if not target_user:
            raise ForbiddenError("Cannot grant permission to user from a different organization")

    if permission.team_id:
        target_team = crud.get_team_in_tenant(db, permission.team_id, current_user.tenant_id)
        if not target_team:
            raise ForbiddenError("Cannot grant permission to team from a different organization")

    cog = crud.get_cog_in_tenant(db, cog_id, current_user.tenant_id)
    if not cog:
        raise NotFoundError("Item not found")

    try:
        level_enum = PermissionLevel(permission.level)
    except ValueError:
        raise BadRequestError(
            f"Invalid permission level: {permission.level}. Must be one of: viewer, editor, owner"
        )

    # Prevent privilege escalation: users cannot grant permissions higher than
    # their own.
    is_valid, error_msg = validate_permission_grant(db, current_user.id, cog_id, level_enum)
    if not is_valid:
        raise ForbiddenError(error_msg)

    # Granting to a user/team/group that already has a grant on this cog is an
    # UPSERT, not an error — create_permission_helper updates the existing row
    # in place (also reactivates a revoked one). This makes (bulk) share
    # idempotent: re-sharing, or sharing a new level, just works instead of 409.
    capability_overrides: Dict[str, bool] = {}
    if permission.can_upload is not None:
        capability_overrides["can_upload"] = permission.can_upload
    if permission.can_create_subfolders is not None:
        capability_overrides["can_create_subfolders"] = permission.can_create_subfolders
    if permission.can_move_files is not None:
        capability_overrides["can_move_files"] = permission.can_move_files
    if permission.can_process is not None:
        capability_overrides["can_process"] = permission.can_process
    if permission.can_share is not None:
        capability_overrides["can_share"] = permission.can_share
    if permission.can_delete is not None:
        capability_overrides["can_delete"] = permission.can_delete

    new_perm = create_permission_helper(
        db=db,
        cog_id=cog_id,
        level=level_enum,
        granted_by_user_id=current_user.id,
        user_id=permission.user_id,
        team_id=permission.team_id,
        keycloak_group_id=permission.keycloak_group_id,
        expires_at=permission.expires_at,
        is_external_user=permission.is_external_user,
        invitation_context=permission.invitation_context,
        **capability_overrides,
    )

    audit.log_permission_granted(
        db=db,
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        target_user_id=permission.user_id,
        resource_type="cog",
        resource_id=cog_id,
        permission_id=new_perm.id,
        permission_level=permission.level,
        metadata={
            "capability_overrides": capability_overrides,
            "expires_at": permission.expires_at.isoformat() if permission.expires_at else None,
            "is_external_user": permission.is_external_user,
        },
        request=http_request,
    )

    return permission_row_to_dict(new_perm)


# --------------------------------------------------------------------------- #
# POST /workflows/{workflow_id}
# --------------------------------------------------------------------------- #

def grant_workflow_permission(
    db: Session,
    current_user: "models.User",
    workflow_id: int,
    body: "Any",  # routers.permissions.WorkflowShareRequest
    http_request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Grant a user access to a workflow (see + open + run).

    Requires the "share" capability on the workflow (the owner has it via
    derived ownership; a user shared at "owner" level also holds it).
    Granting at "owner" level additionally requires "manage_permissions".
    Tenant-isolated: both the workflow and the target user must belong to the
    sharer's tenant.
    """
    from resource_permissions import authorize, grant_resource_permission

    # 1. Workflow must exist in the sharer's tenant (cross-tenant stays 404).
    wf = crud.get_workflow_in_tenant(db, workflow_id, current_user.tenant_id)
    if not wf:
        raise NotFoundError("Workflow not found")

    # 2. Sharer must hold "share" on this workflow.
    if not authorize(db, current_user, models.ResourceType.WORKFLOW, "share", workflow_id):
        raise ForbiddenError("You don't have permission to share this workflow")

    # 3. Validate the requested level.
    level = (body.level or "").lower()
    if level not in WORKFLOW_CAPS_BY_LEVEL:
        raise BadRequestError(
            f"Invalid permission level: {body.level}. Must be one of: viewer, editor, owner"
        )

    # 4. Granting co-owner access needs manage_permissions (privilege guard).
    if level == "owner" and not authorize(
            db, current_user, models.ResourceType.WORKFLOW, "manage_permissions", workflow_id):
        raise ForbiddenError("You don't have permission to grant owner-level access")

    # 5. Target user must belong to the same tenant (cross-tenant security).
    target_user = crud.get_user_in_tenant(db, body.user_id, current_user.tenant_id)
    if not target_user:
        raise ForbiddenError("Cannot share workflow with a user from a different organization")

    caps = WORKFLOW_CAPS_BY_LEVEL[level]

    # 6. Grant the capability set (grant_resource_permission is idempotent).
    for cap_name in caps:
        grant_resource_permission(
            db, current_user.tenant_id, target_user.id,
            models.ResourceType.WORKFLOW, cap_name, workflow_id,
            current_user.id,
        )

    # 7. Revoke any previously-shared capabilities that are NOT in the new
    # set, so a downgrade (e.g. editor -> viewer) actually removes
    # edit/execute. Only this user's explicit share rows on this workflow are
    # touched; the owner's access is derived (no rows) and therefore
    # untouched.
    now = datetime.now(timezone.utc)
    stale = crud.get_active_workflow_resource_permissions(db, target_user.id, workflow_id)
    for perm, cap in stale:
        if cap.name not in caps:
            perm.is_active = False
            perm.revoked_at = now

    # 8. Optional expiry: stamp the freshly-granted rows. Re-queries (the
    # revoke above autoflushes first) so this only sees the caps that stayed.
    if body.expires_at is not None:
        for perm, cap in crud.get_active_workflow_resource_permissions(db, target_user.id, workflow_id):
            if cap.name in caps:
                perm.expires_at = body.expires_at

    audit.log_permission_granted(
        db=db,
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        target_user_id=target_user.id,
        resource_type="workflow",
        resource_id=workflow_id,
        permission_id=0,
        permission_level=level,
        metadata={
            "capabilities": caps,
            "expires_at": body.expires_at.isoformat() if body.expires_at else None,
        },
        request=http_request,
    )

    db.commit()

    return {
        "workflow_id": workflow_id,
        "user_id": target_user.id,
        "level": level,
        "capabilities": caps,
        "expires_at": body.expires_at,
    }


# --------------------------------------------------------------------------- #
# PATCH /{permission_id}
# --------------------------------------------------------------------------- #

def apply_cog_permission_update(
    db: Session,
    current_user: "models.User",
    perm,  # cog_grants.CogGrant read-model row, target_type == COG, already
           # tenant + can_user_share_cog checked by the router
    permission_update: "Any",  # schemas.PermissionUpdate
    http_request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Apply a level/capability-override update to a cog grant and re-sync it
    onto `ResourcePermission` (the grant is a read-model view; its fields
    aren't directly writable — see `cog_grants.sync_cog_permission_grants`)."""
    old_level = perm.level.value
    old_capabilities = {
        "can_upload": perm.can_upload,
        "can_create_subfolders": perm.can_create_subfolders,
        "can_move_files": perm.can_move_files,
        "can_process": perm.can_process,
        "can_share": perm.can_share,
        "can_delete": perm.can_delete,
        "expires_at": perm.expires_at.isoformat() if perm.expires_at else None,
    }

    if permission_update.level is not None:
        try:
            new_level = PermissionLevel(permission_update.level)
        except ValueError:
            raise BadRequestError(f"Invalid permission level: {permission_update.level}")

        # Prevent privilege escalation when changing level.
        is_valid, error_msg = validate_permission_grant(db, current_user.id, perm.target_id, new_level)
        if not is_valid:
            raise ForbiddenError(error_msg)

        perm.level = new_level

    # perm is a read-model view (its capability flags are read-only), so
    # compute the target caps/expiry and RE-WRITE the grant to
    # ResourcePermission. perm.level already reflects the new level if one
    # was provided above.
    def _cap(field, cur):
        v = getattr(permission_update, field)
        return v if v is not None else cur

    target_expiry = (
        permission_update.expires_at
        if permission_update.expires_at is not None else perm.expires_at
    )
    target = CogShareSpec(
        tenant_id=perm.tenant_id, target_id=perm.target_id,
        user_id=perm.user_id, team_id=perm.team_id,
        keycloak_group_id=perm.keycloak_group_id,
        level=perm.level, granted_by_user_id=perm.granted_by_user_id,
        expires_at=target_expiry, is_external_user=perm.is_external_user,
        invitation_context=perm.invitation_context,
        can_upload=_cap("can_upload", perm.can_upload),
        can_create_subfolders=_cap("can_create_subfolders", perm.can_create_subfolders),
        can_move_files=_cap("can_move_files", perm.can_move_files),
        can_process=_cap("can_process", perm.can_process),
        can_share=_cap("can_share", perm.can_share),
        can_delete=_cap("can_delete", perm.can_delete),
    )
    sync_cog_permission_grants(db, target)
    revoke_stale_cog_share_caps(db, target)
    db.commit()

    updated = list_cog_grants(
        db, cog_ids=[perm.target_id], user_id=perm.user_id,
        team_id=perm.team_id, keycloak_group_id=perm.keycloak_group_id,
    )
    if updated:
        perm = updated[0]

    audit.log_permission_updated(
        db=db,
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        target_user_id=perm.user_id,
        resource_type=perm.target_type.value,
        resource_id=perm.target_id,
        permission_id=perm.id,
        permission_level=perm.level.value,
        metadata={
            "old_level": old_level,
            "new_level": perm.level.value,
            "old_capabilities": old_capabilities,
            "new_capabilities": {
                "can_upload": perm.can_upload,
                "can_create_subfolders": perm.can_create_subfolders,
                "can_move_files": perm.can_move_files,
                "can_process": perm.can_process,
                "can_share": perm.can_share,
                "can_delete": perm.can_delete,
                "expires_at": perm.expires_at.isoformat() if perm.expires_at else None,
            },
            "team_id": perm.team_id,
            "keycloak_group_id": perm.keycloak_group_id,
        },
        request=http_request,
    )

    return permission_row_to_dict(perm)


# --------------------------------------------------------------------------- #
# DELETE /{permission_id}
# --------------------------------------------------------------------------- #

def revoke_cog_permission(
    db: Session,
    current_user: "models.User",
    perm,  # cog_grants.CogGrant read-model row, target_type == COG, already
           # tenant + can_user_share_cog checked by the router
    http_request: Optional[Request] = None,
) -> None:
    """Audit-log then revoke (soft-delete) a cog permission grant."""
    audit.log_permission_revoked(
        db=db,
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        target_user_id=perm.user_id,
        resource_type=perm.target_type.value,
        resource_id=perm.target_id,
        permission_id=perm.id,
        permission_level=perm.level.value,
        metadata={
            "team_id": perm.team_id,
            "keycloak_group_id": perm.keycloak_group_id,
        },
        request=http_request,
    )

    # Gate 30: revoke every row of the grant (soft delete via revoked_at, so
    # the ResourcePermission audit trail is preserved for ISO 27001).
    from cog_grants import revoke_cog_grant_by_id

    revoke_cog_grant_by_id(db, perm.id, revoked_by_user_id=current_user.id)


# --------------------------------------------------------------------------- #
# GET /my-permissions/cogs/{cog_id}
# --------------------------------------------------------------------------- #

def owner_permission_view(cog: "models.Cog", current_user: "models.User") -> Dict[str, Any]:
    """Synthesize the "owner" Permission view for a cog's own owner (there is
    no real grant row for derived ownership)."""
    return {
        "id": 0,  # Not a real permission record
        "level": "owner",
        "target_type": "cog",
        "target_id": cog.id,
        "user_id": current_user.id,
        "team_id": None,
        "keycloak_group_id": None,
        "can_upload": True,
        "can_create_subfolders": True,
        "can_move_files": True,
        "can_process": True,
        "can_share": True,
        "can_delete": True,
        "granted_by_user_id": None,
        "granted_at": cog.created_at,
        "expires_at": None,
        "is_external_user": False,
        "is_expired": False,
        "invitation_context": None,
    }
