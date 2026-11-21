"""Universal resource permission checker.

Provides require_permission() FastAPI dependency and helper functions
for checking ResourcePermission grants.

Resolution order:
1. Platform admins always have access
2. Resource owner always has access (if resource has owner_id)
3. Direct user permission on specific resource_id
4. Wildcard user permission (resource_id=NULL) on resource_type
5. Team permissions (via user's team memberships)
6. Role permissions (via user's assigned roles)
"""

import logging
from typing import Optional, Callable
from functools import wraps
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from dependencies import get_db, require_active_and_onboarded_user
from models import (
    User, ResourcePermission, Capability, ResourceType,
    CustomRole, team_members_association,
)

logger = logging.getLogger(__name__)


def _is_not_expired(expires_at, now) -> bool:
    if not expires_at:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


# --- Gate 35/36: API-key scope (effective capabilities = owner ∩ key_scope) ----

def _read_only_capability(resource_type, capability_name) -> bool:
    """True if a capability is READ-ONLY (non-mutating) for its resource type. The
    canonical read set is the 'viewer' role preset — the same capabilities a
    viewer holds. Anything outside it (upload, create, edit, delete, execute,
    share, ...) is a mutation a read-only key must be refused (Gate 36)."""
    read_caps = ROLE_PRESETS["viewer"]["capabilities"].get(resource_type, [])
    return capability_name in read_caps


def _api_key_scope_allows(api_key, resource_type, capability_name) -> bool:
    """Whether the key's scope permits this capability (Gate 35). A read-only key
    allows only non-mutating capabilities; a key carrying an explicit
    capability_scope allowlist permits only its listed "RESOURCE_TYPE:capability"
    entries. An UNSCOPED key (is_read_only False + capability_scope NULL) permits
    everything its owner holds — behaves exactly as before the scope existed."""
    if getattr(api_key, "is_read_only", False) and not _read_only_capability(
            resource_type, capability_name):
        return False
    scope = getattr(api_key, "capability_scope", None)
    if scope:
        # Accept either the enum value ("cog:view") or name ("COG:view") form so a
        # caller-supplied allowlist matches regardless of which they wrote.
        if (f"{resource_type.value}:{capability_name}" not in scope
                and f"{resource_type.name}:{capability_name}" not in scope):
            return False
    return True


def _derived_owner_allows(
    db: Session,
    user: User,
    resource_type: ResourceType,
    resource_id: Optional[int],
) -> bool:
    """Gate DO-1 — ownership derived at check time, no materialized rows.

    The owner of a resource holds EVERY capability of that resource type — the
    same set `grant_owner_permissions` materializes (it grants all caps), now
    computed from the owner relation instead of stored. Tenant-scoped: an owner
    in another tenant is not an owner here. Covers the resource types that carry
    an owner relation today — COG (`cog.owner_id`, folders AND file-cogs) and
    WORKFLOW (`workflow.created_by_user_id`); every other type has no owner and
    returns False.
    """
    if resource_id is None:
        return False
    # DO-4: an explicit ownership row is AUTHORITATIVE and may name a USER or a
    # TEAM (shared-drive model). A team-owned resource is owned by every member
    # of the owning team. Tenant-scoped. When no row exists, fall through to the
    # legacy owner pointer, so existing resources are unaffected.
    from models import ResourceOwnership, PrincipalType
    row = db.query(ResourceOwnership).filter(
        ResourceOwnership.resource_type == resource_type,
        ResourceOwnership.resource_id == resource_id,
    ).first()
    if row is not None:
        if row.tenant_id != user.tenant_id:
            return False
        if row.principal_type == PrincipalType.USER:
            return row.principal_id == user.id
        if row.principal_type == PrincipalType.TEAM:
            return _user_in_team(db, user.id, row.principal_id)
        return False

    if resource_type == ResourceType.COG:
        from models import Cog
        cog = db.query(Cog).filter(
            Cog.id == resource_id, Cog.deleted_at.is_(None)
        ).first()
        return bool(cog and cog.owner_id == user.id
                    and cog.tenant_id == user.tenant_id)
    if resource_type == ResourceType.WORKFLOW:
        from models import Workflow
        wf = db.query(Workflow).filter(
            Workflow.id == resource_id, Workflow.deleted_at.is_(None)
        ).first()
        return bool(wf and wf.created_by_user_id == user.id
                    and wf.tenant_id == user.tenant_id)
    return False


def _user_in_team(db: Session, user_id: int, team_id: int) -> bool:
    from sqlalchemy import select
    return db.execute(
        select(team_members_association.c.user_id).where(
            team_members_association.c.team_id == team_id,
            team_members_association.c.user_id == user_id,
        )
    ).first() is not None


# ResourceType -> the model whose row carries the resource's owning tenant. Only
# types backed by a tenant-scoped table appear; every other type resolves to None
# (see resource_tenant_id). Built lazily to avoid a models import cycle at load.
def _tenant_scoped_models():
    from models import (
        Cog, Workflow, WorkflowRun, Secret, NodeDefinition,
        StorageConnection, Team, CustomRole,
    )
    return {
        ResourceType.COG: Cog,
        ResourceType.WORKFLOW: Workflow,
        ResourceType.WORKFLOW_RUN: WorkflowRun,
        ResourceType.SECRET: Secret,
        ResourceType.NODE_DEFINITION: NodeDefinition,
        ResourceType.STORAGE_LOCATION: StorageConnection,
        ResourceType.TEAM: Team,
        ResourceType.ROLE: CustomRole,
    }


def resource_tenant_id(
    db: Session,
    resource_type: ResourceType,
    resource_id: Optional[int],
) -> Optional[int]:
    """The tenant that owns a resource, or None when it cannot be determined.

    None means "no tenant boundary to enforce here" — the resource does not
    exist, it is a globally-scoped row (tenant_id NULL, e.g. a system node
    definition), or the resource type has no tenant-scoped table. Callers MUST
    NOT treat None as a tenant match; it means "unknown, do not block".
    """
    if resource_id is None:
        return None
    model = _tenant_scoped_models().get(resource_type)
    if model is None:
        return None
    row = db.query(model.tenant_id).filter(model.id == resource_id).first()
    return row[0] if row else None


def check_resource_permission(
    db: Session,
    user: User,
    resource_type: ResourceType,
    capability_name: str,
    resource_id: Optional[int] = None,
) -> bool:
    """Check if user has a specific capability on a resource.

    Returns True if granted, False otherwise.
    """
    # NOTE: no platform-admin bypass. ISO/DoD tenant isolation — access is
    # per-user grants only; a platform admin holds no grants in other tenants and
    # is therefore denied there, exactly like any user. Platform-scoped operations
    # are gated separately by require_platform_admin, not here.
    #
    # Gate 35/36: cap the request's effective capabilities by the API key's scope
    # (effective = owner ∩ key_scope). If an API key authorised this request and
    # its scope forbids this capability, deny WITHOUT consulting owner grants — a
    # scoped key can never exceed its scope, and a read-only key can never mutate,
    # regardless of how much its owner (even a platform admin) holds. Unscoped keys
    # and non-key (session/system) requests reach this with no restriction.
    from utils.request_principal import get_api_key as _get_api_key
    _req_key = _get_api_key()
    if _req_key is not None and not _api_key_scope_allows(
            _req_key, resource_type, capability_name):
        return False

    # CRITICAL — tenant enforcement chokepoint. When a specific resource is named,
    # resolve its owning tenant and DENY if it is not the caller's, BEFORE honoring
    # any grant, wildcard, team/role permission or role-own-capability below. Those
    # paths match on (user/team/role, capability, resource_type) and never on the
    # resource's tenant, so without this a grant/role-cap in one tenant would
    # resolve True against an identically-typed resource in another. None means the
    # tenant is unknown (resource absent, globally-scoped, or a non-tenant-scoped
    # type) and imposes no boundary here.
    if resource_id is not None:
        r_tenant = resource_tenant_id(db, resource_type, resource_id)
        if r_tenant is not None and r_tenant != user.tenant_id:
            return False

    cap = db.query(Capability).filter(
        Capability.resource_type == resource_type,
        Capability.name == capability_name,
    ).first()
    if not cap:
        return False

    # Gate DO-1 — derived ownership. The owner holds every capability of the
    # resource type, computed from the owner relation at check time (no stored
    # rows). Placed AFTER the api-key scope gate above (so a scoped/read-only key
    # still caps an owner: effective = owner ∩ key_scope) and AFTER the
    # capability-existence check (so a non-existent capability is denied even for
    # an owner). This is the single rule the scattered owner short-circuits
    # collapse into.
    if _derived_owner_allows(db, user, resource_type, resource_id):
        return True

    now = datetime.now(timezone.utc)

    # Check direct user permissions (specific resource first, then wildcard)
    query = db.query(ResourcePermission).filter(
        ResourcePermission.user_id == user.id,
        ResourcePermission.capability_id == cap.id,
        ResourcePermission.resource_type == resource_type,
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
    )

    if resource_id is not None:
        specific = query.filter(ResourcePermission.resource_id == resource_id).first()
        if specific and _is_not_expired(specific.expires_at, now):
            return True

    wildcard = query.filter(ResourcePermission.resource_id.is_(None)).first()
    if wildcard and _is_not_expired(wildcard.expires_at, now):
        return True

    # Check team permissions
    from sqlalchemy import select
    team_ids = [
        row[0] for row in
        db.execute(select(team_members_association.c.team_id).where(
            team_members_association.c.user_id == user.id
        )).all()
    ]
    if team_ids:
        team_query = db.query(ResourcePermission).filter(
            ResourcePermission.team_id.in_(team_ids),
            ResourcePermission.capability_id == cap.id,
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.is_active == True,
            ResourcePermission.revoked_at.is_(None),
        )
        if resource_id is not None:
            team_specific = team_query.filter(ResourcePermission.resource_id == resource_id).first()
            if team_specific and _is_not_expired(team_specific.expires_at, now):
                return True
        team_wildcard = team_query.filter(ResourcePermission.resource_id.is_(None)).first()
        if team_wildcard and _is_not_expired(team_wildcard.expires_at, now):
            return True

    # Check role permissions
    role_ids = [user.custom_role_id] if user.custom_role_id else []

    if role_ids:
        role_query = db.query(ResourcePermission).filter(
            ResourcePermission.role_id.in_(role_ids),
            ResourcePermission.capability_id == cap.id,
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.is_active == True,
            ResourcePermission.revoked_at.is_(None),
        )
        if resource_id is not None:
            role_specific = role_query.filter(ResourcePermission.resource_id == resource_id).first()
            if role_specific and _is_not_expired(role_specific.expires_at, now):
                return True
        role_wildcard = role_query.filter(ResourcePermission.resource_id.is_(None)).first()
        if role_wildcard and _is_not_expired(role_wildcard.expires_at, now):
            return True

    # The role's OWN capabilities (the require_capability / role-only mechanism),
    # unified in here so ONE resolver covers BOTH role-grant styles: role_id
    # ResourcePermission (above) and CustomRole.capabilities (here). This makes
    # authorize() a superset of the previously-separate resolvers.
    if user.custom_role_id:
        from sqlalchemy.orm import joinedload
        role = db.query(CustomRole).options(
            joinedload(CustomRole.capabilities)
        ).filter(
            CustomRole.id == user.custom_role_id,
            CustomRole.deleted_at.is_(None),
        ).first()
        if role and any(c.id == cap.id for c in role.capabilities):
            return True

    return False


# Canonical name for the single authorization chokepoint (unified-authorization
# Gate 05). Everything resolves resource authorization through this.
authorize = check_resource_permission


def workflow_view_scope(db: Session, user: User):
    """Which workflows `user` may 'view' — the list-endpoint counterpart to
    authorize(WORKFLOW, "view", wf_id).

    Returns (see_all, ids):
      * see_all=True  -> the user may view EVERY workflow in their tenant. This
        happens when a wildcard "view" ResourcePermission (user/team/role,
        resource_id NULL) or a role-owned "view" capability applies — exactly
        the cases authorize() resolves True for any workflow_id. ids is None.
      * see_all=False -> ids is the EXACT set of workflow ids the user may view:
        the workflows they own (derived ownership: an authoritative
        ResourceOwnership row, else the legacy created_by pointer) plus every
        workflow carrying an active, unexpired resource-specific "view" grant to
        the user, one of their teams, or their role.

    Mirrors check_resource_permission's WORKFLOW/"view" decision so the Load
    picker shows precisely what GET /road/{id} would allow — no more (the
    old list returned every tenant workflow), no less. Honors the request's
    API-key scope: a scoped/read-only key that forbids WORKFLOW:view sees none.
    """
    from models import (
        Workflow, ResourceOwnership, PrincipalType, ResourcePermission,
        Capability, ResourceType,
    )
    from sqlalchemy import select

    # API-key scope gate (Gate 35/36): a key whose scope forbids WORKFLOW:view
    # can never view a workflow, so its list is empty regardless of owner grants.
    from utils.request_principal import get_api_key as _get_api_key
    _req_key = _get_api_key()
    if _req_key is not None and not _api_key_scope_allows(
            _req_key, ResourceType.WORKFLOW, "view"):
        return False, set()

    # FX.110 (owner decision Q97=A) — outside collaborators: an external
    # user NEVER gets see_all. Even a wildcard (resource_id NULL) "view"
    # grant or a role-owned view capability is ignored for them; their
    # listing is restricted to the EXACT set of workflows explicitly
    # granted below (no tenant browsing beyond their grants).
    _fx110_is_external = bool(getattr(user, "is_external", False))

    cap = db.query(Capability).filter(
        Capability.resource_type == ResourceType.WORKFLOW,
        Capability.name == "view",
    ).first()
    if not cap:
        return False, set()

    now = datetime.now(timezone.utc)
    team_ids = [
        row[0] for row in
        db.execute(select(team_members_association.c.team_id).where(
            team_members_association.c.user_id == user.id
        )).all()
    ]
    role_id = getattr(user, "custom_role_id", None)

    # --- see_all: any wildcard "view" grant, or a role that owns the view cap.
    principal_conds = [ResourcePermission.user_id == user.id]
    if team_ids:
        principal_conds.append(ResourcePermission.team_id.in_(team_ids))
    if role_id:
        principal_conds.append(ResourcePermission.role_id == role_id)
    from sqlalchemy import or_
    wildcard_rows = db.query(ResourcePermission).filter(
        ResourcePermission.capability_id == cap.id,
        ResourcePermission.resource_type == ResourceType.WORKFLOW,
        ResourcePermission.resource_id.is_(None),
        ResourcePermission.is_active == True,  # noqa: E712
        ResourcePermission.revoked_at.is_(None),
        or_(*principal_conds),
    ).all()
    if not _fx110_is_external and \
            any(_is_not_expired(r.expires_at, now) for r in wildcard_rows):
        return True, None
    if role_id and not _fx110_is_external:
        # FX.110: role-owned view capability never widens an external user.
        from sqlalchemy.orm import joinedload
        role = db.query(CustomRole).options(
            joinedload(CustomRole.capabilities)
        ).filter(
            CustomRole.id == role_id,
            CustomRole.deleted_at.is_(None),
        ).first()
        if role and any(c.id == cap.id for c in role.capabilities):
            return True, None

    # --- ownership (derived): ResourceOwnership row is authoritative when
    # present; otherwise the legacy created_by pointer.
    ids: set = set()
    own_rows = db.query(
        ResourceOwnership.resource_id,
        ResourceOwnership.principal_type,
        ResourceOwnership.principal_id,
    ).filter(
        ResourceOwnership.resource_type == ResourceType.WORKFLOW,
        ResourceOwnership.tenant_id == user.tenant_id,
    ).all()
    rows_wf_ids = set()
    for rid, ptype, pid in own_rows:
        rows_wf_ids.add(rid)
        if ptype == PrincipalType.USER and pid == user.id:
            ids.add(rid)
        elif ptype == PrincipalType.TEAM and pid in team_ids:
            ids.add(rid)
    legacy = db.query(Workflow.id).filter(
        Workflow.tenant_id == user.tenant_id,
        Workflow.created_by_user_id == user.id,
        Workflow.deleted_at.is_(None),
    ).all()
    for (wid,) in legacy:
        if wid not in rows_wf_ids:
            ids.add(wid)

    # --- resource-specific "view" grants (user / team / role).
    grant_rows = db.query(
        ResourcePermission.resource_id, ResourcePermission.expires_at
    ).filter(
        ResourcePermission.capability_id == cap.id,
        ResourcePermission.resource_type == ResourceType.WORKFLOW,
        ResourcePermission.resource_id.isnot(None),
        ResourcePermission.is_active == True,  # noqa: E712
        ResourcePermission.revoked_at.is_(None),
        or_(*principal_conds),
    ).all()
    for rid, expires_at in grant_rows:
        if _is_not_expired(expires_at, now):
            ids.add(rid)

    return False, ids


def can_view_workflow_run(db: Session, user: User, workflow_run) -> bool:
    """True if `user` may view a workflow RUN's content — its node runs, output
    previews, run files, per-node resource usage and progress/logs.

    This is the run-scoped counterpart to GET /road/{id}'s WORKFLOW:view
    decision: a run is viewable exactly when its parent workflow is in the
    caller's `workflow_view_scope` (owned via derived ownership, or carrying an
    active view grant/share to the user, a team, or the role) — OR the caller is
    a platform admin. Tenant-scoping alone is NOT sufficient (that was the leak:
    the run-scoped endpoints — /node-runs/{run_id}, /node-runs/{run_id}/previews,
    the /service/workflow/* siblings, run files, and build-run-progress — only
    filtered by tenant, so any tenant user could read the node graph, output
    download URLs and logs of a co-tenant's PRIVATE workflow run by run id).

    Mirrors routers/workflows.py::_user_can_view_run so every run-content
    endpoint enforces one identical gate. Callers should still resolve the run
    tenant-scoped first (defence in depth); this adds the per-workflow gate.
    """
    if workflow_run is None:
        return False
    if getattr(user, "is_platform_admin", False):
        return True
    wf_id = getattr(workflow_run, "workflow_id", None)
    if wf_id is None:
        return False
    see_all, ids = workflow_view_scope(db, user)
    return bool(see_all or wf_id in (ids or set()))


def can_manage_resource_grant(
    db: Session,
    user: User,
    resource_type: ResourceType,
    resource_id: Optional[int],
) -> bool:
    """True if `user` is authorized to CREATE or REVOKE a permission grant on a
    resource — i.e. they hold "manage_permissions" or "share" on it. The owner
    holds both via derived ownership (Gate DO-1); a co-owner shared at that level
    holds them via explicit grants.

    For a wildcard grant (resource_id None — e.g. a role preset that grants a
    capability tenant-wide) this requires the wildcard-level capability, which
    only an admin/owner-level principal holds, so a standard user is denied.

    This is the SOLE gate for creating, editing and revoking grants. There is no
    self-removal or grant-creator exception: a user cannot drop their own access
    when someone else granted it, and a former granter who no longer manages the
    resource cannot undo a grant they once made — only a current resource manager
    (owner / manage_permissions / share holder) can.
    """
    for cap_name in ("manage_permissions", "share"):
        try:
            if authorize(db, user, resource_type, cap_name, resource_id):
                return True
        except Exception:
            continue
    return False


def grant_resource_permission(
    db: Session,
    tenant_id: int,
    user_id: int,
    resource_type: ResourceType,
    capability_name: str,
    resource_id: Optional[int] = None,
    granted_by_user_id: Optional[int] = None,
) -> ResourcePermission:
    """Grant a capability on a resource to a user."""
    cap = db.query(Capability).filter(
        Capability.resource_type == resource_type,
        Capability.name == capability_name,
    ).first()
    if not cap:
        return None

    existing = db.query(ResourcePermission).filter(
        ResourcePermission.user_id == user_id,
        ResourcePermission.capability_id == cap.id,
        ResourcePermission.resource_type == resource_type,
        ResourcePermission.resource_id == resource_id,
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
    ).first()

    if existing:
        return existing

    perm = ResourcePermission(
        tenant_id=tenant_id,
        user_id=user_id,
        capability_id=cap.id,
        resource_type=resource_type,
        resource_id=resource_id,
        granted_by_user_id=granted_by_user_id,
        is_active=True,
    )
    db.add(perm)
    return perm


def set_resource_owner(
    db: Session,
    resource_type: ResourceType,
    resource_id: int,
    principal_type,
    principal_id: int,
    *,
    actor_user_id: Optional[int] = None,
) -> tuple:
    """Gates DO-3/DO-4 — set the authoritative owner (a USER or a TEAM).

    Upserts the single `ResourceOwnership` row (authoritative when present, per
    the derived-owner rule), updates the legacy owner pointer for a USER owner
    (back-compat for readers that still look at `cog.owner_id` /
    `workflow.created_by_user_id`), and revokes the PREVIOUS user-owner's
    materialized owner-grant rows (granted_by NULL — the grant_owner_permissions
    signature) so they retain no stale access, while any EXPLICIT share to them
    survives. Ownership stays a single sparse fact, so this is O(1); the new
    owner needs no capability rows (derivation covers them). Does NOT commit —
    the caller commits alongside its audit record.

    Returns (old_principal, new_principal) as ("user"|"team", id) tuples;
    old_principal is None when the resource had no prior owner.
    """
    from models import Cog, PrincipalType, ResourceOwnership, Workflow
    now = datetime.now(timezone.utc)

    prev_user_owner = None
    if resource_type == ResourceType.COG:
        res = db.query(Cog).filter(
            Cog.id == resource_id, Cog.deleted_at.is_(None)).first()
        if res is None:
            raise ValueError("cog not found")
        r_tenant = res.tenant_id
        prev_user_owner = res.owner_id
        if principal_type == PrincipalType.USER:
            res.owner_id = principal_id
    elif resource_type == ResourceType.WORKFLOW:
        res = db.query(Workflow).filter(
            Workflow.id == resource_id, Workflow.deleted_at.is_(None)).first()
        if res is None:
            raise ValueError("workflow not found")
        r_tenant = res.tenant_id
        prev_user_owner = res.created_by_user_id
        if principal_type == PrincipalType.USER:
            res.created_by_user_id = principal_id
    else:
        raise ValueError(f"{resource_type} is not an ownable resource")

    existing = db.query(ResourceOwnership).filter(
        ResourceOwnership.resource_type == resource_type,
        ResourceOwnership.resource_id == resource_id,
    ).first()
    if existing is not None:
        old_principal = (existing.principal_type.value, existing.principal_id)
        existing.principal_type = principal_type
        existing.principal_id = principal_id
        existing.created_by_user_id = actor_user_id
    else:
        old_principal = (("user", prev_user_owner)
                         if prev_user_owner is not None else None)
        db.add(ResourceOwnership(
            tenant_id=r_tenant, resource_type=resource_type, resource_id=resource_id,
            principal_type=principal_type, principal_id=principal_id,
            created_by_user_id=actor_user_id))

    # Revoke the previous USER owner's materialized owner-grant rows unless they
    # remain the owner.
    still_same = (principal_type == PrincipalType.USER
                  and principal_id == prev_user_owner)
    if prev_user_owner is not None and not still_same:
        stale = db.query(ResourcePermission).filter(
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.resource_id == resource_id,
            ResourcePermission.user_id == prev_user_owner,
            ResourcePermission.granted_by_user_id.is_(None),
            ResourcePermission.is_active == True,  # noqa: E712
            ResourcePermission.revoked_at.is_(None),
        ).all()
        for p in stale:
            p.is_active = False
            p.revoked_at = now

    return old_principal, (principal_type.value, principal_id)


def transfer_ownership(
    db: Session,
    resource_type: ResourceType,
    resource_id: int,
    new_owner_user_id: int,
    *,
    actor_user_id: Optional[int] = None,
) -> tuple:
    """Gate DO-3 — reassign a resource to a USER owner. Thin wrapper over
    `set_resource_owner`; returns (old_owner_user_id_or_None, new_owner_user_id)."""
    from models import PrincipalType
    old_principal, _ = set_resource_owner(
        db, resource_type, resource_id, PrincipalType.USER, new_owner_user_id,
        actor_user_id=actor_user_id)
    old_user = old_principal[1] if (old_principal and old_principal[0] == "user") else None
    return old_user, new_owner_user_id


def grant_owner_permissions(
    db: Session,
    tenant_id: int,
    user_id: int,
    resource_type: ResourceType,
    resource_id: int,
) -> None:
    """Grant all capabilities on a specific resource to its owner."""
    caps = db.query(Capability).filter(
        Capability.resource_type == resource_type,
    ).all()

    for cap in caps:
        existing = db.query(ResourcePermission).filter(
            ResourcePermission.user_id == user_id,
            ResourcePermission.capability_id == cap.id,
            ResourcePermission.resource_type == resource_type,
            ResourcePermission.resource_id == resource_id,
            ResourcePermission.is_active == True,
            ResourcePermission.revoked_at.is_(None),
        ).first()
        if not existing:
            perm = ResourcePermission(
                tenant_id=tenant_id,
                user_id=user_id,
                capability_id=cap.id,
                resource_type=resource_type,
                resource_id=resource_id,
                is_active=True,
            )
            db.add(perm)


# =========================================================================
# Role Presets
# =========================================================================

ROLE_PRESETS = {
    "viewer": {
        "display_name": "Viewer",
        "description": "Read-only access to everything",
        "capabilities": {
            ResourceType.COG: ["view", "view_thumbnail", "preview", "download_proxy", "view_comments", "view_versions", "view_activity"],
            ResourceType.WORKFLOW: ["view", "view_details", "view_runs", "view_versions"],
            ResourceType.WORKFLOW_RUN: ["view", "view_status", "view_progress", "view_logs", "view_outputs", "view_parameters"],
            ResourceType.NODE_DEFINITION: ["view", "view_cost"],
            ResourceType.USER: ["view"],
            ResourceType.TEAM: ["view", "view_members"],
            ResourceType.SECRET: ["view"],
            ResourceType.API_KEY: ["view"],
            ResourceType.WORKER: ["view", "view_status"],
        },
    },
    "editor": {
        "display_name": "Editor",
        "description": "View, create, and edit. Can run workflows.",
        "capabilities": {
            ResourceType.COG: ["view", "view_thumbnail", "preview", "download_proxy", "download_original", "download_bulk", "upload", "upload_version", "create_subfolder", "rename", "move", "copy", "comment", "view_comments", "tag", "edit_metadata", "view_versions", "view_activity", "process", "view_processing_history"],
            ResourceType.WORKFLOW: ["view", "view_details", "execute", "execute_with_params", "create", "edit", "duplicate", "view_versions", "view_runs", "export"],
            ResourceType.WORKFLOW_RUN: ["view", "view_status", "view_progress", "view_logs", "download_logs", "view_outputs", "download_outputs", "cancel", "retry", "view_parameters"],
            ResourceType.NODE_DEFINITION: ["view", "use", "create", "edit", "view_cost"],
            ResourceType.USER: ["view", "view_details"],
            ResourceType.TEAM: ["view", "view_members"],
            ResourceType.SECRET: ["view", "create", "use_in_workflow"],
            ResourceType.API_KEY: ["view", "create"],
            ResourceType.WORKER: ["view", "view_status"],
        },
    },
    "admin": {
        "display_name": "Admin",
        "description": "Full access to everything in the tenant",
        "capabilities": "all",
    },
}


def apply_role_preset(
    db: Session,
    tenant_id: int,
    user_id: int,
    preset_name: str,
    granted_by_user_id: Optional[int] = None,
) -> int:
    """Apply a role preset to a user. Returns count of permissions granted."""
    preset = ROLE_PRESETS.get(preset_name)
    if not preset:
        raise ValueError(f"Unknown preset: {preset_name}")

    count = 0

    if preset["capabilities"] == "all":
        all_caps = db.query(Capability).all()
        for cap in all_caps:
            existing = db.query(ResourcePermission).filter(
                ResourcePermission.user_id == user_id,
                ResourcePermission.capability_id == cap.id,
                ResourcePermission.resource_type == cap.resource_type,
                ResourcePermission.resource_id.is_(None),
                ResourcePermission.is_active == True,
                ResourcePermission.revoked_at.is_(None),
            ).first()
            if not existing:
                perm = ResourcePermission(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    capability_id=cap.id,
                    resource_type=cap.resource_type,
                    resource_id=None,
                    granted_by_user_id=granted_by_user_id,
                    is_active=True,
                )
                db.add(perm)
                count += 1
    else:
        for resource_type, cap_names in preset["capabilities"].items():
            for cap_name in cap_names:
                cap = db.query(Capability).filter(
                    Capability.resource_type == resource_type,
                    Capability.name == cap_name,
                ).first()
                if not cap:
                    continue

                existing = db.query(ResourcePermission).filter(
                    ResourcePermission.user_id == user_id,
                    ResourcePermission.capability_id == cap.id,
                    ResourcePermission.resource_type == resource_type,
                    ResourcePermission.resource_id.is_(None),
                    ResourcePermission.is_active == True,
                    ResourcePermission.revoked_at.is_(None),
                ).first()
                if not existing:
                    perm = ResourcePermission(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        capability_id=cap.id,
                        resource_type=resource_type,
                        resource_id=None,
                        granted_by_user_id=granted_by_user_id,
                        is_active=True,
                    )
                    db.add(perm)
                    count += 1

    db.commit()
    return count


def revoke_all_preset_permissions(
    db: Session,
    user_id: int,
) -> int:
    """Revoke all wildcard ResourcePermissions for a user (for role changes)."""
    now = datetime.now(timezone.utc)
    perms = db.query(ResourcePermission).filter(
        ResourcePermission.user_id == user_id,
        ResourcePermission.resource_id.is_(None),
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
    ).all()

    count = 0
    for p in perms:
        p.is_active = False
        p.revoked_at = now
        count += 1

    db.commit()
    return count


# =========================================================================
# FastAPI Dependency
# =========================================================================

def require_permission(
    resource_type: ResourceType,
    capability_name: str,
    resource_id_param: Optional[str] = None,
):
    """FastAPI dependency factory for permission checks.

    Usage:
        @router.get("/workflows/{workflow_id}")
        def get_workflow(
            workflow_id: int,
            user = Depends(require_permission(ResourceType.WORKFLOW, "view", "workflow_id"))
        ):
    """
    def dependency(
        db: Session = Depends(get_db),
        current_user: User = Depends(require_active_and_onboarded_user),
        **kwargs,
    ):
        rid = None
        if resource_id_param and resource_id_param in kwargs:
            rid = kwargs[resource_id_param]

        if not check_resource_permission(db, current_user, resource_type, capability_name, rid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have '{capability_name}' permission on this {resource_type.value}",
            )
        return current_user

    return dependency


def backfill_workflow_owner_grants(db) -> int:
    """Gate 09 backfill — formerly granted each existing workflow's creator an
    OWNER ResourcePermission.

    authz-derived-ownership DO-6: owner access is derived (DO-1); this no
    longer materializes owner rows. Existing rows are still READ (back-compat);
    only writing stops.
    """
    return 0


# --- Gate 24: cog ownership becomes an OWNER grant (FOLDERS only) --------------
# Removing the `cog.owner_id == user` short-circuit from the decision path means a
# folder owner must hold real OWNER grants, exactly like the workflow precedent
# (Gate 09/10). FILE-type cogs are DELIBERATELY excluded: a file already carries
# its owner in files.user_id (one immutable owner record per file, honoured by
# can_user_access_file), so materialising ~32 capability rows per file would bloat
# resource_permissions without bound on the upload path (millions of files). So
# folders get grants; files keep file.user_id ownership + folder inheritance.

def grant_cog_owner_if_folder(db, cog) -> None:
    """Formerly granted a FOLDER cog's owner an OWNER grant on create.

    authz-derived-ownership DO-6: owner access is derived (DO-1); this no
    longer materializes owner rows. Existing rows are still READ (back-compat);
    only writing stops.
    """
    return None


def backfill_cog_owner_grants(db) -> int:
    """Gate 24 backfill / safety-net — formerly granted each existing FOLDER
    cog's owner an OWNER ResourcePermission.

    authz-derived-ownership DO-6: owner access is derived (DO-1); this no
    longer materializes owner rows. Existing rows are still READ (back-compat);
    only writing stops.
    """
    return 0


# --- Gate 16: legacy cog Permission -> ResourcePermission data migration -------

# The legacy cog path (permissions.py can_user_*_cog) distinguishes exactly seven
# capabilities: an implicit `view` (any grant at VIEWER+ can view) plus the six
# `can_*` flags. Each maps 1:1 to a seeded COG capability. Fine-grained COG caps
# (view_thumbnail, download_original, ...) are NOT part of legacy behavior, so a
# behavior-preserving migration grants only these seven. Reads stay on the legacy
# path this gate; Gate 17 switches them to authorize().
_COG_FLAG_TO_CAP = [
    ("can_upload", "upload"),
    ("can_create_subfolders", "create_subfolder"),
    ("can_move_files", "move"),
    ("can_process", "process"),
    ("can_share", "share"),
    ("can_delete", "delete"),
]


def _upsert_cog_grant(db, *, tenant_id, cap, resource_id, user_id=None,
                      team_id=None, keycloak_group_id=None, expires_at=None,
                      granted_by=None, permission_level=None,
                      is_external_user=False, invitation_context=None) -> bool:
    """Idempotently add/refresh one COG ResourcePermission for a user, team OR
    keycloak-group grant, carrying the sharing-grant metadata (Gate 30:
    permission_level, is_external_user, invitation_context). Returns True if a new
    row was added, False if an equivalent one already existed — whose metadata is
    refreshed to match the grant, so a re-share (e.g. a level change) reflects."""
    q = db.query(ResourcePermission).filter(
        ResourcePermission.capability_id == cap.id,
        ResourcePermission.resource_type == ResourceType.COG,
        ResourcePermission.resource_id == resource_id,
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
    )
    if user_id is not None:
        q = q.filter(ResourcePermission.user_id == user_id,
                     ResourcePermission.team_id.is_(None),
                     ResourcePermission.keycloak_group_id.is_(None))
    elif team_id is not None:
        q = q.filter(ResourcePermission.team_id == team_id,
                     ResourcePermission.user_id.is_(None),
                     ResourcePermission.keycloak_group_id.is_(None))
    else:
        q = q.filter(ResourcePermission.keycloak_group_id == keycloak_group_id,
                     ResourcePermission.user_id.is_(None),
                     ResourcePermission.team_id.is_(None))
    existing = q.first()
    if existing is not None:
        # Refresh the grant metadata (level/external/invitation/expiry) so a
        # re-share updates the mirrored rows in place rather than diverging.
        if permission_level is not None:
            existing.permission_level = permission_level
            existing.is_external_user = bool(is_external_user)
            existing.invitation_context = invitation_context
            existing.expires_at = expires_at
            if granted_by is not None:
                existing.granted_by_user_id = granted_by
        return False
    db.add(ResourcePermission(
        tenant_id=tenant_id, user_id=user_id, team_id=team_id,
        keycloak_group_id=keycloak_group_id,
        capability_id=cap.id, resource_type=ResourceType.COG,
        resource_id=resource_id, expires_at=expires_at,
        granted_by_user_id=granted_by, is_active=True,
        permission_level=permission_level,
        is_external_user=bool(is_external_user),
        invitation_context=invitation_context,
    ))
    return True






# --- Gate 17: cog read path via authorize() -----------------------------------

def user_has_any_cog_grant_on(db, user, resource_id) -> bool:
    """True if the user — directly, via a team, or via their custom role — holds
    any active, unexpired COG ResourcePermission on this specific cog. Used to
    find the nearest node in the folder tree that carries a grant, reproducing
    the legacy "direct permission takes precedence over inherited" rule before
    evaluating the specific capability at that node."""
    from sqlalchemy import or_, select
    now = datetime.now(timezone.utc)
    conds = [ResourcePermission.user_id == user.id]
    team_ids = [
        row[0] for row in
        db.execute(select(team_members_association.c.team_id).where(
            team_members_association.c.user_id == user.id
        )).all()
    ]
    if team_ids:
        conds.append(ResourcePermission.team_id.in_(team_ids))
    if getattr(user, "custom_role_id", None):
        conds.append(ResourcePermission.role_id == user.custom_role_id)
    rows = db.query(ResourcePermission).filter(
        ResourcePermission.resource_type == ResourceType.COG,
        ResourcePermission.resource_id == resource_id,
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
        or_(*conds),
    ).all()
    return any(_is_not_expired(r.expires_at, now) for r in rows)


# --- Gate 19-23: cog Permission write dual-sync to ResourcePermission ----------
# Explicit dual-write (matches the workflow precedent: grant_owner_permissions at
# the write site). Every cog Permission create calls sync_cog_permission_grants;
# every revoke calls revoke_cog_permission_grants. Keeps ResourcePermission in
# lockstep with the legacy table so cog reads can move to authorize() (re-doing
# Gate 17) without a share created/revoked after boot going missing.

def sync_cog_permission_grants(db, permission) -> int:
    """Materialize the ResourcePermission grants equivalent to ONE legacy cog
    Permission row. Idempotent; safe to call after creating/updating the row.
    Returns the number of new grants added."""
    from permissions import get_effective_capability
    if permission is None or permission.target_type != ResourceType_COG_TARGET():
        return 0
    if getattr(permission, "deleted_at", None) is not None:
        return 0
    user_id = permission.user_id
    team_id = permission.team_id if not user_id else None
    group_id = permission.keycloak_group_id if (not user_id and not team_id) else None
    if not user_id and not team_id and not group_id:
        return 0
    caps = {c.name: c for c in db.query(Capability).filter(
        Capability.resource_type == ResourceType.COG).all()}
    wanted = ["view"]
    for flag, cap_name in _COG_FLAG_TO_CAP:
        if get_effective_capability(permission, flag):
            wanted.append(cap_name)
    added = 0
    for cap_name in wanted:
        cap = caps.get(cap_name)
        if cap and _upsert_cog_grant(
            db, tenant_id=permission.tenant_id, cap=cap,
            resource_id=permission.target_id, user_id=user_id, team_id=team_id,
            keycloak_group_id=group_id,
            expires_at=permission.expires_at,
            granted_by=getattr(permission, "granted_by_user_id", None),
            permission_level=getattr(permission, "level", None),
            is_external_user=bool(getattr(permission, "is_external_user", False)),
            invitation_context=getattr(permission, "invitation_context", None),
        ):
            added += 1
    return added


def revoke_stale_cog_share_caps(db, permission) -> int:
    """Gate 30 — after (re)writing a cog share, revoke the share's
    ResourcePermission rows whose capability is no longer part of the grant (e.g.
    an EDITOR->VIEWER downgrade drops upload/create/move/process). Only SHARE rows
    (permission_level set) for this exact grantee+cog are touched; owner/role
    grants are untouched. Returns the number of rows revoked."""
    from permissions import get_effective_capability
    if permission is None or permission.target_type != ResourceType_COG_TARGET():
        return 0
    user_id = permission.user_id
    team_id = permission.team_id if not user_id else None
    group_id = permission.keycloak_group_id if (not user_id and not team_id) else None
    if not user_id and not team_id and not group_id:
        return 0
    wanted = {"view"}
    for flag, cap_name in _COG_FLAG_TO_CAP:
        if get_effective_capability(permission, flag):
            wanted.add(cap_name)
    cap_name_by_id = {c.id: c.name for c in db.query(Capability).filter(
        Capability.resource_type == ResourceType.COG).all()}
    q = db.query(ResourcePermission).filter(
        ResourcePermission.resource_type == ResourceType.COG,
        ResourcePermission.resource_id == permission.target_id,
        ResourcePermission.permission_level.isnot(None),
        ResourcePermission.is_active == True,
        ResourcePermission.revoked_at.is_(None),
    )
    if user_id is not None:
        q = q.filter(ResourcePermission.user_id == user_id)
    elif team_id is not None:
        q = q.filter(ResourcePermission.team_id == team_id)
    else:
        q = q.filter(ResourcePermission.keycloak_group_id == group_id)
    now = datetime.now(timezone.utc)
    revoked = 0
    for rp in q.all():
        if cap_name_by_id.get(rp.capability_id) not in wanted:
            rp.is_active = False
            rp.revoked_at = now
            revoked += 1
    return revoked




def ResourceType_COG_TARGET():
    """The PermissionTargetType value that denotes a cog (imported lazily to avoid
    a models import cycle at module load)."""
    from models import PermissionTargetType
    return PermissionTargetType.COG
