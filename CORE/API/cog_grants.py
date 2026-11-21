"""Gate 30 — the sharing-grant read model over ResourcePermission.

The legacy `Permission` table was the sharing-grant record: one row per grant,
carrying level + external flag + invitation metadata + grantee. Its DECISIONS
moved to ResourcePermission (Gate 17); Gate 30 moves the GRANT record there too.
A grant's metadata now rides on its ResourcePermission rows (permission_level,
is_external_user, invitation_context, keycloak_group_id, granted_by_user_id), so
a grant is reconstructable from ResourcePermission alone.

A grant expands to several capability rows; `CogGrant` regroups them into the one
logical grant the sharing UI / analytics read, DUCK-TYPED to the legacy
`Permission` shape (.level, .target_id, .user_id, .can_upload, ...) so a read site
changes only its QUERY source, not its field access.

A "share" is distinguished from owner/role/decision grants by permission_level
being set: create_permission sets it; grant_owner_permissions and role grants do
not. So list_cog_grants returns exactly what the legacy Permission table held.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from models import (
    ResourcePermission, Capability, ResourceType, PermissionLevel,
    PermissionTargetType,
)

# legacy Permission capability flag -> COG capability name (mirrors _COG_FLAG_TO_CAP)
_FLAG_TO_CAP = {
    "can_upload": "upload",
    "can_create_subfolders": "create_subfolder",
    "can_move_files": "move",
    "can_process": "process",
    "can_share": "share",
    "can_delete": "delete",
}


class CogShareSpec:
    """A lightweight, NON-persisted grant spec that drives the ResourcePermission
    write path (sync_cog_permission_grants / revoke_stale_cog_share_caps) without a
    legacy Permission row — the transient that replaced `models.Permission(...)` in
    every write site once the legacy table was retired (Gate 30). Duck-typed to
    what those writers + get_effective_capability read: `.level`, `.can_*` (None =
    use the level default), the grantee fields, and the grant metadata."""

    def __init__(self, *, tenant_id, target_id, user_id=None, team_id=None,
                 keycloak_group_id=None, level=None, granted_by_user_id=None,
                 expires_at=None, is_external_user=False, invitation_context=None,
                 can_upload=None, can_create_subfolders=None, can_move_files=None,
                 can_process=None, can_share=None, can_delete=None):
        from models import PermissionTargetType
        self.tenant_id = tenant_id
        self.target_id = target_id
        self.target_type = PermissionTargetType.COG
        self.user_id = user_id
        self.team_id = team_id
        self.keycloak_group_id = keycloak_group_id
        self.level = level
        self.granted_by_user_id = granted_by_user_id
        self.expires_at = expires_at
        self.is_external_user = is_external_user
        self.invitation_context = invitation_context
        self.deleted_at = None
        self.can_upload = can_upload
        self.can_create_subfolders = can_create_subfolders
        self.can_move_files = can_move_files
        self.can_process = can_process
        self.can_share = can_share
        self.can_delete = can_delete


class CogGrant:
    """One reconstructed sharing grant over a cog, duck-typed to legacy Permission
    for the attributes the sharing UI / analytics read."""

    def __init__(self, *, id, tenant_id, target_id, user_id, team_id,
                 keycloak_group_id, level, expires_at, granted_at,
                 granted_by_user_id, is_external_user, invitation_context, caps):
        self.tenant_id = tenant_id
        # A stable handle for the grant: the smallest ResourcePermission.id among
        # the grant's rows. Deterministic and stable while the grant is active, so
        # the sharing UI can reference/extend/revoke a specific grant by it —
        # replacing the legacy Permission.id. (A re-created grant gets a new id,
        # exactly as a new Permission row would have.)
        self.id = id
        self.target_id = target_id
        self.target_type = PermissionTargetType.COG
        self.user_id = user_id
        self.team_id = team_id
        self.keycloak_group_id = keycloak_group_id
        self.level = level                       # PermissionLevel
        self.expires_at = expires_at
        self.granted_at = granted_at
        self.granted_by_user_id = granted_by_user_id
        self.is_external_user = bool(is_external_user)
        self.invitation_context = invitation_context
        self._caps = caps                        # set of COG capability names
        self.deleted_at = None                   # reconstructed grants are active

    def _has(self, cap_name: str) -> bool:
        return cap_name in self._caps

    # Capability flags read like legacy Permission columns, so get_effective_capability
    # and get_permission_summary work on a CogGrant unchanged.
    @property
    def can_upload(self): return self._has("upload")
    @property
    def can_create_subfolders(self): return self._has("create_subfolder")
    @property
    def can_move_files(self): return self._has("move")
    @property
    def can_process(self): return self._has("process")
    @property
    def can_share(self): return self._has("share")
    @property
    def can_delete(self): return self._has("delete")


def list_cog_grants(
    db: Session,
    *,
    cog_ids: Optional[Iterable[int]] = None,
    user_id: Optional[int] = None,
    team_id: Optional[int] = None,
    keycloak_group_id: Optional[str] = None,
    granted_by_user_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    include_expired: bool = True,
    expiring_before: Optional[datetime] = None,
    is_external_user: Optional[bool] = None,
) -> List[CogGrant]:
    """Reconstruct sharing grants over cogs from ResourcePermission, grouped by
    (grantee, cog). Replaces `db.query(Permission).filter(target_type=COG, ...)`.

    Only rows with permission_level set are shares (owner/role/decision grants are
    excluded), matching what the legacy Permission table contained.
    """
    q = (
        db.query(ResourcePermission, Capability.name)
        .join(Capability, Capability.id == ResourcePermission.capability_id)
        .filter(
            ResourcePermission.resource_type == ResourceType.COG,
            ResourcePermission.is_active == True,  # noqa: E712
            ResourcePermission.revoked_at.is_(None),
            ResourcePermission.permission_level.isnot(None),
        )
    )
    if tenant_id is not None:
        q = q.filter(ResourcePermission.tenant_id == tenant_id)
    if cog_ids is not None:
        q = q.filter(ResourcePermission.resource_id.in_(list(cog_ids)))
    if user_id is not None:
        q = q.filter(ResourcePermission.user_id == user_id)
    if team_id is not None:
        q = q.filter(ResourcePermission.team_id == team_id)
    if keycloak_group_id is not None:
        q = q.filter(ResourcePermission.keycloak_group_id == keycloak_group_id)
    if granted_by_user_id is not None:
        q = q.filter(ResourcePermission.granted_by_user_id == granted_by_user_id)
    if is_external_user is not None:
        q = q.filter(ResourcePermission.is_external_user == is_external_user)

    grants: dict = {}
    for rp, cap_name in q.all():
        key = (rp.user_id, rp.team_id, rp.keycloak_group_id, rp.resource_id)
        g = grants.get(key)
        if g is None:
            g = CogGrant(
                id=rp.id, tenant_id=rp.tenant_id,
                target_id=rp.resource_id, user_id=rp.user_id, team_id=rp.team_id,
                keycloak_group_id=rp.keycloak_group_id, level=rp.permission_level,
                expires_at=rp.expires_at, granted_at=rp.granted_at,
                granted_by_user_id=rp.granted_by_user_id,
                is_external_user=rp.is_external_user,
                invitation_context=rp.invitation_context, caps=set(),
            )
            grants[key] = g
        elif rp.id < g.id:
            g.id = rp.id  # stable representative = smallest row id
        g._caps.add(cap_name)

    result = list(grants.values())
    now = datetime.now(timezone.utc)
    if not include_expired:
        result = [g for g in result
                  if g.expires_at is None or _aware(g.expires_at) > now]
    if expiring_before is not None:
        result = [g for g in result
                  if g.expires_at is not None and _aware(g.expires_at) <= expiring_before]
    return result


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def get_cog_grant_by_id(db: Session, grant_id: int) -> Optional[CogGrant]:
    """The sharing grant a `grant_id` (a ResourcePermission.id belonging to it, i.e.
    CogGrant.id) identifies, or None. Replaces `Permission.query.get(id)`."""
    rp = (
        db.query(ResourcePermission)
        .filter(
            ResourcePermission.id == grant_id,
            ResourcePermission.resource_type == ResourceType.COG,
            ResourcePermission.permission_level.isnot(None),
        )
        .first()
    )
    if rp is None:
        return None
    grants = list_cog_grants(
        db, cog_ids=[rp.resource_id], user_id=rp.user_id, team_id=rp.team_id,
        keycloak_group_id=rp.keycloak_group_id)
    return grants[0] if grants else None


def _grant_rows(db: Session, grant_id: int):
    """Every active share ResourcePermission row of the grant identified by grant_id."""
    rp = (
        db.query(ResourcePermission)
        .filter(ResourcePermission.id == grant_id,
                ResourcePermission.resource_type == ResourceType.COG)
        .first()
    )
    if rp is None:
        return []
    q = db.query(ResourcePermission).filter(
        ResourcePermission.resource_type == ResourceType.COG,
        ResourcePermission.resource_id == rp.resource_id,
        ResourcePermission.permission_level.isnot(None),
        ResourcePermission.is_active == True,  # noqa: E712
        ResourcePermission.revoked_at.is_(None),
    )
    if rp.user_id is not None:
        q = q.filter(ResourcePermission.user_id == rp.user_id)
    elif rp.team_id is not None:
        q = q.filter(ResourcePermission.team_id == rp.team_id)
    else:
        q = q.filter(ResourcePermission.keycloak_group_id == rp.keycloak_group_id)
    return q.all()


def extend_cog_grant(db: Session, grant_id: int, new_expiry, commit: bool = True) -> bool:
    """Set a new expiry on every row of the grant. Returns True if it existed.
    Pass commit=False inside an atomic bulk operation that commits once itself."""
    rows = _grant_rows(db, grant_id)
    if not rows:
        return False
    for rp in rows:
        rp.expires_at = new_expiry
    if commit:
        db.commit()
    return True


def set_cog_grant_invitation_context(db: Session, grant_id: int,
                                     invitation_context, commit: bool = True) -> bool:
    """Persist a new invitation_context on every row of the grant (used for
    expiry-warning notification tracking). Returns True if the grant existed."""
    rows = _grant_rows(db, grant_id)
    if not rows:
        return False
    for rp in rows:
        rp.invitation_context = invitation_context
    if commit:
        db.commit()
    return True


def revoke_cog_grant_by_id(db: Session, grant_id: int,
                           revoked_by_user_id: Optional[int] = None,
                           commit: bool = True) -> bool:
    """Revoke (soft-delete) every row of the grant. Returns True if it existed.
    Pass commit=False inside an atomic bulk operation that commits once itself."""
    rows = _grant_rows(db, grant_id)
    if not rows:
        return False
    now = datetime.now(timezone.utc)
    for rp in rows:
        rp.is_active = False
        rp.revoked_at = now
        if revoked_by_user_id is not None:
            rp.revoked_by_user_id = revoked_by_user_id
    if commit:
        db.commit()
    return True
