"""
Permission System Helper Functions

This module provides utilities for checking and managing folder/cog permissions
with a flexible capability-based model.
"""
from __future__ import annotations  # Gate 30: annotations lazy; `Permission` (deleted) survives only in type hints, which are duck-typed CogGrant now

from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from models import (
    PermissionLevel, User, Cog, PermissionTargetType,
    PlatformCapability, PlatformRole, CustomRole, AuditAction,
)


# =============================================================================
# --- PLATFORM-SCOPED CAPABILITIES ---
# =============================================================================
# Platform capabilities are granted directly to a user (not via cog/role) and
# checked platform-wide. See models.PlatformCapability. This is the least-
# privilege path that hands a specific ability (e.g. authoring persistent
# stacks) to a user without conferring full is_platform_admin.
# (merged from feature/stack-wizard — delegated-stack-authoring §1.2/§1.6)

# Capability name constants — the single source of truth (design task 1.2).
STACK_AUTHOR = "stack:author"

PLATFORM_CAPABILITIES = frozenset({STACK_AUTHOR})

# PlatformRole resolves to a bundle of capability names rather than being
# checked directly, so the dormant role enum can light up later without
# touching call sites (design task 1.2).
PLATFORM_ROLE_CAPABILITIES = {
    PlatformRole.USER: frozenset(),
    PlatformRole.OPERATOR: frozenset({STACK_AUTHOR}),
    PlatformRole.ADMIN: PLATFORM_CAPABILITIES,
    PlatformRole.SUPER_ADMIN: PLATFORM_CAPABILITIES,
}

# CE-only role defaults, keyed by TENANT SYSTEM-ROLE NAME (crud/tenant_cud.SYSTEM_ROLE_SPECS).
# WHY: the platform-capability grant CRUD (crud/platform_capabilities.py + the POST
# /v1/platform-capabilities route) is carved out of the CE tree
# (open-core/manifest.yaml:1398-1399), so on a fresh CE install NOTHING can mint
# `stack:author` and the router-wide gate on /v1/stacks
# (routers/persistent_stacks/__init__.py:57-58) would 403 every legitimate tenant
# admin. On CE the tenant's system "Admin" role IS the authoring authority, so its
# role-default bundle applies when the user has NO grant row at all. Paid/SaaS never
# consults this table (guarded by utils.paid_import.is_ce_edition) — there the grant
# CRUD ships and least-privilege delegation stays the only path.
CE_SYSTEM_ROLE_CAPABILITIES = {
    "Admin": PLATFORM_CAPABILITIES,
}


def _ce_edition() -> bool:
    """True on the CE (open-source) carve. Lazy import: `permissions` is on the
    hot import path of models/crud, so the edition predicate is pulled in at
    call time (same pattern as the routers' `import permissions`)."""
    from utils.paid_import import is_ce_edition
    return is_ce_edition()


def _has_any_grant_row(db: Session, user_id: int, capability: str) -> bool:
    """True if the user has ANY grant row for ``capability`` — active, expired
    OR revoked. Used only to decide whether the CE role-default fallback may
    apply: an explicit row (even a revoked one) means the grant CRUD was in
    play, so the fallback must never shadow it."""
    return db.query(PlatformCapability).filter(
        PlatformCapability.user_id == user_id,
        PlatformCapability.capability == capability,
    ).count() > 0


def check_user_platform_capability(db: Session, user_id: int, capability: str) -> bool:
    """True if the user holds ``capability`` platform-wide.

    Cheapest check first:
      1. ``is_platform_admin`` — full bypass (god mode), matching every other
         capability gate in the codebase.
      2. an ACTIVE direct grant in ``platform_capabilities`` — not revoked and
         not past ``expires_at``. Expiry is evaluated HERE, at check time, so an
         expired grant reads false without any sweeper deleting it.
      3. CE ONLY (grant CRUD absent from the carve): the tenant system-role
         default bundle (``CE_SYSTEM_ROLE_CAPABILITIES``), and only when the
         user has NO grant row at all for the capability.

    Reads ONLY local state — no cross-manager/remote lookup — so a grant issued
    in one deployment is never consulted by another (design task 1.6).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return False
    if getattr(user, "is_platform_admin", False):
        return True

    grant = db.query(PlatformCapability).filter(
        PlatformCapability.user_id == user_id,
        PlatformCapability.capability == capability,
        PlatformCapability.revoked_at.is_(None),
    ).order_by(PlatformCapability.granted_at.desc()).first()

    # CE-only role default (see CE_SYSTEM_ROLE_CAPABILITIES above). WHY it is
    # scoped this way: only when the install is the CE carve (utils.paid_import
    # is_ce_edition — the ONE edition predicate) AND the user has NO grant row
    # at all for this capability (no active, revoked or expired row) does the
    # tenant system-role bundle apply. Any explicit row wins, so a revocation
    # can never be masked by the role default, and on the paid tree this block
    # is dead code (byte-identical paid behaviour).
    if grant is None and _ce_edition() and not _has_any_grant_row(db, user_id, capability):
        role_id = getattr(user, "custom_role_id", None)
        # CE follow-up (10-29 review): the role lookup is TENANT-SCOPED, matching
        # crud/role_cud.get_custom_role (role_cud.py:51-60). CustomRole.id is only
        # unique WITHIN a tenant (uq_tenant_custom_role_name, models.py:1479), so
        # an unscoped lookup let a user whose custom_role_id happens to collide
        # with ANOTHER tenant's system "Admin" role inherit this tenant's bundle.
        role = db.query(CustomRole).filter(
            CustomRole.id == role_id,
            CustomRole.tenant_id == user.tenant_id,
            CustomRole.is_system_role.is_(True),
        ).first() if role_id is not None else None
        if role is not None and capability in CE_SYSTEM_ROLE_CAPABILITIES.get(role.name, frozenset()):
            # CE follow-up (10-29 review): the fallback grant is invisible in the
            # trail otherwise, so log it (CMMC AU-2/AU-3). Emitted ONLY on the
            # grant decision — never on a denial, and never on the paid tree or
            # when an explicit grant row exists — so the row count is bounded by
            # stack-authoring requests resolved by the role default, not by
            # every capability check. Secret VALUES are never written: the
            # metadata carries ids, the role NAME and the capability name only.
            # Same error/commit shape as the other call sites (crud/user_cud.py:232
            # -240): lazy import, swallow failures so an audit-write hiccup can
            # never raise into (or flip) the permission decision, and no extra
            # commit here — log_permission_audit commits its own row.
            try:
                from audit import log_permission_audit
                from models import AuditAction
                log_permission_audit(
                    db=db, action=AuditAction.PERMISSION_GRANTED,
                    actor_user_id=user_id, tenant_id=user.tenant_id,
                    resource_type="platform_capability", resource_id=user_id,
                    target_user_id=user_id,
                    permission_level=capability, permission_id=role_id,
                    metadata={
                        "source": "ce_system_role_default",
                        "capability": capability,
                        "role_name": role.name,
                        "role_is_system_role": True,
                        "user_id": user_id,
                        "tenant_id": user.tenant_id,
                    },
                )
            except Exception:
                pass
            return True

    if grant is None:
        return False
    if grant.expires_at is not None and grant.expires_at <= datetime.now(timezone.utc):
        return False
    return True


# =============================================================================
# --- PERMISSION DEFAULTS BY LEVEL ---
# =============================================================================

PERMISSION_DEFAULTS = {
    PermissionLevel.VIEWER: {
        "can_upload": False,
        "can_create_subfolders": False,
        "can_move_files": False,
        "can_process": False,
        "can_share": False,
        "can_delete": False,
    },
    PermissionLevel.EDITOR: {
        "can_upload": True,
        "can_create_subfolders": True,
        "can_move_files": True,
        "can_process": True,
        "can_share": False,  # Editors can't invite others by default
        "can_delete": False, # Editors can't delete by default
    },
    PermissionLevel.OWNER: {
        "can_upload": True,
        "can_create_subfolders": True,
        "can_move_files": True,
        "can_process": True,
        "can_share": True,
        "can_delete": True,
    },
}

# Permission level hierarchy (higher number = more privileged)
PERMISSION_LEVEL_HIERARCHY = {
    PermissionLevel.VIEWER: 1,
    PermissionLevel.EDITOR: 2,
    PermissionLevel.OWNER: 3,
}


# =============================================================================
# --- PERMISSION ESCALATION PREVENTION ---
# =============================================================================

def get_permission_level_rank(level: PermissionLevel) -> int:
    """
    Get the numeric rank of a permission level for comparison.

    Args:
        level: The permission level to get the rank for

    Returns:
        int: The numeric rank (1-3) of the permission level

    Raises:
        ValueError: If the level is not a valid PermissionLevel
    """
    if level not in PERMISSION_LEVEL_HIERARCHY:
        raise ValueError(f"Invalid permission level: {level}")
    return PERMISSION_LEVEL_HIERARCHY[level]


def is_valid_permission_level(level: PermissionLevel) -> bool:
    """Check if a permission level is valid."""
    return level in PERMISSION_LEVEL_HIERARCHY


def compare_permission_levels(level1: PermissionLevel, level2: PermissionLevel) -> int:
    """
    Safely compare two permission levels.

    Args:
        level1: First permission level
        level2: Second permission level

    Returns:
        int: Negative if level1 < level2, zero if equal, positive if level1 > level2

    Raises:
        ValueError: If either level is not a valid PermissionLevel
    """
    rank1 = get_permission_level_rank(level1)
    rank2 = get_permission_level_rank(level2)
    return rank1 - rank2


def can_grant_permission_level(
    granter_level: PermissionLevel,
    target_level: PermissionLevel
) -> bool:
    """
    Check if a user with granter_level can grant target_level to others.

    Users cannot grant permissions higher than their own level.

    Args:
        granter_level: The permission level of the user granting the permission
        target_level: The permission level being granted

    Returns:
        bool: True if the grant is allowed, False otherwise
    """
    return get_permission_level_rank(granter_level) >= get_permission_level_rank(target_level)


def get_user_permission_level_on_cog(
    db: Session,
    user_id: int,
    cog_id: int
) -> Optional[PermissionLevel]:
    """
    Get a user's permission level on a specific cog.

    Owners of the cog always have OWNER level.

    Args:
        db: Database session
        user_id: ID of the user
        cog_id: ID of the cog

    Returns:
        PermissionLevel or None if no permission
    """
    # Check if user is the owner of the cog
    cog = db.query(Cog).filter(
        Cog.id == cog_id,
        Cog.deleted_at.is_(None)
    ).first()

    if cog and cog.owner_id == user_id:
        return PermissionLevel.OWNER

    # Get user's permission
    permission = get_user_permission_on_cog(db, user_id, cog_id)
    if permission:
        return permission.level

    return None


def validate_permission_grant(
    db: Session,
    granter_user_id: int,
    cog_id: int,
    target_level: PermissionLevel
) -> tuple[bool, Optional[str]]:
    """
    Validate that a user can grant a specific permission level on a cog.

    This prevents privilege escalation by ensuring users cannot grant
    permissions higher than their own level.

    Args:
        db: Database session
        granter_user_id: ID of the user attempting to grant the permission
        cog_id: ID of the cog being shared
        target_level: The permission level to be granted

    Returns:
        Tuple of (is_valid, error_message)
    """
    granter_level = get_user_permission_level_on_cog(db, granter_user_id, cog_id)

    if granter_level is None:
        return False, "You don't have permission on this item"

    if not can_grant_permission_level(granter_level, target_level):
        return False, f"Cannot grant {target_level.value} permission - you only have {granter_level.value} access"

    return True, None


# =============================================================================
# --- PERMISSION RESOLUTION FUNCTIONS ---
# =============================================================================

def get_effective_capability(permission: Permission, capability: str) -> bool:
    """
    Get the effective value of a capability for a permission.

    If the capability is explicitly set (not None), return that value.
    Otherwise, return the default for the permission level.

    Args:
        permission: The Permission object
        capability: Name of the capability (e.g., "can_upload")

    Returns:
        bool: The effective value of the capability
    """
    # Get the explicit value from the permission
    explicit_value = getattr(permission, capability, None)

    # If explicitly set, use that value
    if explicit_value is not None:
        return explicit_value

    # Otherwise, use the default for this level
    return PERMISSION_DEFAULTS[permission.level][capability]


def is_permission_expired(permission: Permission) -> bool:
    """Check if a permission has expired."""
    if permission.expires_at is None:
        return False
    return datetime.now(timezone.utc) >= permission.expires_at


def get_user_permission_on_cog(
    db: Session,
    user_id: int,
    cog_id: int,
    include_inherited: bool = True,
    _visited: Optional[set] = None
) -> Optional[Permission]:
    """
    Get a user's permission on a specific cog/folder.

    Args:
        db: Database session
        user_id: ID of the user
        cog_id: ID of the cog/folder
        include_inherited: If True, check parent folders for inherited permissions
        _visited: Internal parameter for cycle detection - do not pass externally

    Returns:
        Permission object if found and not expired, None otherwise
    """
    # Cycle detection: prevent infinite recursion from circular parent references
    if _visited is None:
        _visited = set()
    if cog_id in _visited:
        return None  # Cycle detected, stop traversal
    _visited.add(cog_id)

    # Gate 30: the sharing grant is reconstructed from ResourcePermission (the
    # legacy Permission table is retired). Returns a CogGrant duck-typed to the
    # legacy Permission shape (.level, .can_*, ...). Expired grants are excluded.
    from cog_grants import list_cog_grants
    direct = list_cog_grants(db, cog_ids=[cog_id], user_id=user_id,
                             include_expired=False)
    if direct:
        return direct[0]

    # If no direct permission and inheritance is enabled, check parent folders
    if include_inherited:
        cog = db.query(Cog).filter(
            Cog.id == cog_id,
            Cog.deleted_at.is_(None)  # Exclude soft-deleted cogs
        ).first()
        if cog and cog.parent_id:
            # Recursively check parent permissions with cycle detection
            return get_user_permission_on_cog(db, user_id, cog.parent_id, include_inherited=True, _visited=_visited)

    return None


# Legacy cog capability names (level defaults + can_* flags) -> the seeded COG
# ResourcePermission capability names. `view` is the implicit read capability any
# grant carries. Gate 16 migrated the data using this same mapping.
# Legacy cog capability names -> seeded COG ResourcePermission capability names.
_LEGACY_COG_CAP = {
    "view": "view",
    "can_upload": "upload",
    "can_create_subfolders": "create_subfolder",
    "can_move_files": "move",
    "can_process": "process",
    "can_share": "share",
    "can_delete": "delete",
}


def check_user_capability_on_cog(
    db: Session,
    user_id: int,
    cog_id: int,
    capability: str
) -> bool:
    """
    Check if a user has a specific capability on a cog/folder.

    Resolves through the single authorize() chokepoint against ResourcePermission
    data. Now safe: cog Permission writes dual-write to ResourcePermission
    (create + revoke), so a share created/revoked at runtime reflects immediately.
    Behavior preserved (frozen by the Gate 02 oracle):
      - cog owner shortcut short-circuits (Gate 24 replaces with an owner grant);
      - folder inheritance keeps direct-beats-inherited: walk self -> parents and
        evaluate the capability at the NEAREST node carrying any grant for the user;
      - expiry honoured inside authorize().
    No platform-admin bypass (ISO/DoD tenant isolation).
    """
    from models import User, ResourceType
    from resource_permissions import (authorize, user_has_any_cog_grant_on,
                                       _derived_owner_allows)

    cog = db.query(Cog).filter(
        Cog.id == cog_id,
        Cog.deleted_at.is_(None)
    ).first()
    if cog is None:
        return False
    # Gate 24: NO cog owner short-circuit. Ownership is an OWNER grant now
    # (grant_cog_owner_if_folder on create + backfill_cog_owner_grants). FILE cogs
    # keep ownership via files.user_id, honoured by can_user_access_file before it
    # ever reaches this cog primitive.

    cap_name = _LEGACY_COG_CAP.get(capability)
    if cap_name is None:
        return False

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return False

    node = cog
    visited: set = set()
    while node is not None and node.id not in visited:
        visited.add(node.id)
        # A node the user OWNS carries access too. Ownership is a DERIVED grant
        # now (authz-derived-ownership DO-1), computed inside authorize() rather
        # than stored as a row — so it must be part of this "nearest node with
        # access" gate, or a bare file-cog owner (no grant row, no owner row)
        # would be walked straight past and denied at their own file.
        if (user_has_any_cog_grant_on(db, user, node.id)
                or _derived_owner_allows(db, user, ResourceType.COG, node.id)):
            return authorize(db, user, ResourceType.COG, cap_name, node.id)
        if not node.parent_id:
            break
        node = db.query(Cog).filter(
            Cog.id == node.parent_id,
            Cog.deleted_at.is_(None)
        ).first()
    return _tenant_root_write_allowed(db, user, cog, capability, cap_name)


# Write capabilities a tenant manager holds on THEIR tenant's root folder.
# Deliberately excludes `view`/`delete`/`share`/`move`: the root is a shared
# container, so write-at-root must not expose other users' top-level items
# (list_cogs returns all children only when the caller can VIEW the parent).
_TENANT_ROOT_WRITE_CAPS = frozenset({"can_upload", "can_create_subfolders"})


def _tenant_root_write_allowed(db: Session, user, cog, capability: str, cap_name: str) -> bool:
    """Upload / create-subfolder at the tenant root for any tenant manager.

    The root ("My Files") is auto-created on first use and OWNED by whoever
    touched it first. In a single-tenant edition (CE) every signup is a tenant
    manager (parent_user_id NULL, Admin role) of the SAME tenant, so every
    later admin — including the platform admin — held no grant on the root and
    got 403 on create-folder / upload at Home. Honour the role's own COG
    capability (authorize() with no resource id = wildcard/team/role grants and
    the role's capability list, with the API-key scope gate) for the tenant's
    root folder ONLY — never for descendants, which stay owner/grant-gated.
    """
    from models import CogType, ResourceType, UserStatus
    from resource_permissions import authorize

    if capability not in _TENANT_ROOT_WRITE_CAPS:
        return False
    if cog.parent_id is not None or cog.cog_type != CogType.MASTER_FOLDER:
        return False
    if cog.tenant_id != user.tenant_id:
        return False
    if getattr(user, "parent_user_id", None) is not None or getattr(user, "is_external", False):
        return False
    if user.status != UserStatus.ACTIVE:
        return False
    from crud.cogs_shared import get_tenant_master_root
    root = get_tenant_master_root(db, cog.tenant_id)
    if root is None or root.id != cog.id:
        return False
    return authorize(db, user, ResourceType.COG, cap_name, None)


# =============================================================================
# --- PERMISSION INHERITANCE FUNCTIONS ---
# =============================================================================

def get_inherited_permissions(
    db: Session,
    cog_id: int,
    _visited: Optional[set] = None
) -> list[Permission]:
    """
    Get all permissions inherited from parent folders.

    Args:
        db: Database session
        cog_id: ID of the cog/folder
        _visited: Internal parameter for cycle detection - do not pass externally

    Returns:
        List of Permission objects inherited from parent folders (not expired)
    """
    # Cycle detection: prevent infinite recursion from circular parent references
    if _visited is None:
        _visited = set()
    if cog_id in _visited:
        return []  # Cycle detected, stop traversal
    _visited.add(cog_id)

    inherited = []
    cog = db.query(Cog).filter(
        Cog.id == cog_id,
        Cog.deleted_at.is_(None)  # Exclude soft-deleted cogs
    ).first()

    if not cog or not cog.parent_id:
        return inherited

    # Gate 30: sharing grants reconstructed from ResourcePermission (not-expired).
    from cog_grants import list_cog_grants
    inherited.extend(list_cog_grants(db, cog_ids=[cog.parent_id], include_expired=False))

    # Recursively get permissions from grandparents with cycle detection
    inherited.extend(get_inherited_permissions(db, cog.parent_id, _visited=_visited))

    return inherited


def get_effective_permissions_for_cog(
    db: Session,
    cog_id: int,
    user_id: Optional[int] = None
) -> list[Permission]:
    """
    Get all effective permissions for a folder, including inherited ones.

    Args:
        db: Database session
        cog_id: ID of the cog/folder
        user_id: Optional user ID to filter permissions for specific user

    Returns:
        List of Permission objects (direct + inherited), with direct permissions
        taking precedence over inherited ones
    """
    # Gate 30: direct sharing grants reconstructed from ResourcePermission
    # (not-expired), duck-typed to the legacy Permission shape.
    from cog_grants import list_cog_grants
    direct_permissions = list_cog_grants(
        db, cog_ids=[cog_id], user_id=user_id, include_expired=False)

    # Get inherited permissions
    inherited = get_inherited_permissions(db, cog_id)

    # Filter inherited: only include if user doesn't have direct permission
    if user_id:
        inherited = [p for p in inherited if p.user_id == user_id]

    # Create a set of user IDs that have direct permissions
    direct_user_ids = {p.user_id for p in direct_permissions if p.user_id}
    direct_team_ids = {p.team_id for p in direct_permissions if p.team_id}
    direct_group_ids = {p.keycloak_group_id for p in direct_permissions if p.keycloak_group_id}

    # Only add inherited permissions for users/teams/groups without direct permissions
    effective_inherited = []
    for perm in inherited:
        if perm.user_id and perm.user_id not in direct_user_ids:
            effective_inherited.append(perm)
        elif perm.team_id and perm.team_id not in direct_team_ids:
            effective_inherited.append(perm)
        elif perm.keycloak_group_id and perm.keycloak_group_id not in direct_group_ids:
            effective_inherited.append(perm)

    return direct_permissions + effective_inherited


# =============================================================================
# --- CONVENIENCE FUNCTIONS FOR SPECIFIC CAPABILITIES ---
# =============================================================================

def can_user_view_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can view a cog/folder — routed through authorize() (any grant
    carries `view`), preserving the owner shortcut + inheritance."""
    return check_user_capability_on_cog(db, user_id, cog_id, "view")


def can_user_upload_to_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can upload files to a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_upload")


def can_user_create_subfolder(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can create subfolders in a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_create_subfolders")


def can_user_move_files_in_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can move/rename files in a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_move_files")


def can_user_process_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can trigger workflows on a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_process")


def can_user_share_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can invite others to a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_share")


def can_user_delete_cog(db: Session, user_id: int, cog_id: int) -> bool:
    """Check if user can delete a cog/folder."""
    return check_user_capability_on_cog(db, user_id, cog_id, "can_delete")


# =============================================================================
# --- FILE PERMISSION FUNCTIONS ---
# =============================================================================

def can_user_access_file(db: Session, user_id: int, file_id: str, capability: str = "view") -> bool:
    """
    Check if a user can access a file with a specific capability.

    Resolution order (additive — any match grants access):
    1. File owner always has full access
    2. Direct permission on the file's own cog record
    3. Permission on the parent folder (inherited via cog.parent_id)

    Args:
        db: Database session
        user_id: ID of the user
        file_id: ID of the file (UUID string from files table)
        capability: Capability to check (default: "view" for basic access)

    Returns:
        bool: True if user can access the file with the specified capability
    """
    from models import File, Cog, CogType, ResourceType

    file = db.query(File).filter(
        File.id == file_id,
        File.deleted_at.is_(None)
    ).first()
    if not file:
        return False

    if file.user_id == user_id:
        # Route the owner short-circuit through the API-key scope gate so a
        # read-only / capability-scoped key cannot exceed its scope on the
        # owner's OWN files (effective = owner ∩ key_scope), matching the
        # standard cog path which resolves through authorize(). A session or
        # unscoped-key request (no key, or an unrestricted one) is unaffected.
        from utils.request_principal import get_api_key
        from resource_permissions import _api_key_scope_allows
        _key = get_api_key()
        if _key is not None:
            cog_cap = _LEGACY_COG_CAP.get(capability, capability)
            if not _api_key_scope_allows(_key, ResourceType.COG, cog_cap):
                return False
        return True

    # Find the file's own cog record (cog_type=FILE with matching name in the parent folder)
    if file.cog_id is not None:
        file_cog = db.query(Cog).filter(
            Cog.parent_id == file.cog_id,
            Cog.name == file.name,
            Cog.cog_type == CogType.FILE,
            Cog.deleted_at.is_(None),
        ).first()

        # Check file cog permissions first (includes inheritance from parent via check_user_capability_on_cog)
        if file_cog:
            if capability == "view":
                return can_user_view_cog(db, user_id, file_cog.id)
            return check_user_capability_on_cog(db, user_id, file_cog.id, capability)

        # Fallback: check parent folder directly
        if capability == "view":
            return can_user_view_cog(db, user_id, file.cog_id)
        return check_user_capability_on_cog(db, user_id, file.cog_id, capability)

    return False


# =============================================================================
# --- PERMISSION CREATION HELPERS ---
# =============================================================================

def create_permission(
    db: Session,
    cog_id: int,
    level: PermissionLevel,
    granted_by_user_id: int,
    user_id: Optional[int] = None,
    team_id: Optional[int] = None,
    keycloak_group_id: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    is_external_user: bool = False,
    invitation_context: Optional[Dict[str, Any]] = None,
    **capability_overrides
) -> Permission:
    """
    Create a new permission with optional capability overrides.

    Args:
        db: Database session
        cog_id: Cog/folder being shared
        level: Permission level (VIEWER, EDITOR, OWNER)
        granted_by_user_id: User granting the permission
        user_id: User receiving the permission (optional, one of user/team/group required)
        team_id: Team receiving the permission (optional)
        keycloak_group_id: Keycloak group receiving the permission (optional)
        expires_at: Optional expiration date
        is_external_user: Whether this is for an external user
        invitation_context: Optional metadata about the invitation
        **capability_overrides: Optional overrides for specific capabilities
            (e.g., can_share=True, can_process=False)

    Returns:
        Permission: The created permission object

    Raises:
        ValueError: If cog not found or no grantee specified
    """
    # Validate that at least one grantee is specified
    if not user_id and not team_id and not keycloak_group_id:
        raise ValueError("At least one of user_id, team_id, or keycloak_group_id must be specified")

    # Get the cog to determine tenant_id
    cog = db.query(Cog).filter(
        Cog.id == cog_id,
        Cog.deleted_at.is_(None)
    ).first()
    if not cog:
        raise ValueError(f"Cog with id {cog_id} not found or has been deleted")

    # Gate 30: the sharing grant lives ONLY in ResourcePermission now. Build a
    # TRANSIENT Permission (never added to the session) purely to drive the
    # existing capability expansion — get_effective_capability reads its attrs —
    # then write the ResourcePermission rows carrying the grant metadata, and
    # revoke any capability this grant no longer includes (e.g. a level downgrade).
    # Idempotent: _upsert_cog_grant refreshes an existing grant's rows in place.
    from cog_grants import CogShareSpec
    transient = CogShareSpec(
        tenant_id=cog.tenant_id,
        target_id=cog_id,
        user_id=user_id,
        team_id=team_id,
        keycloak_group_id=keycloak_group_id,
        level=level,
        granted_by_user_id=granted_by_user_id,
        expires_at=expires_at,
        is_external_user=is_external_user,
        invitation_context=invitation_context,
        **capability_overrides
    )
    from resource_permissions import (
        sync_cog_permission_grants, revoke_stale_cog_share_caps)
    sync_cog_permission_grants(db, transient)
    revoke_stale_cog_share_caps(db, transient)
    db.commit()

    from cog_grants import list_cog_grants
    grants = list_cog_grants(db, cog_ids=[cog_id], user_id=user_id, team_id=team_id,
                             keycloak_group_id=keycloak_group_id)
    return grants[0] if grants else None


def get_permission_summary(permission: Permission) -> Dict[str, Any]:
    """
    Get a summary of a permission with all effective capabilities.

    Args:
        permission: The Permission object

    Returns:
        Dict with level and all resolved capabilities
    """
    return {
        "level": permission.level.value,
        "can_upload": get_effective_capability(permission, "can_upload"),
        "can_create_subfolders": get_effective_capability(permission, "can_create_subfolders"),
        "can_move_files": get_effective_capability(permission, "can_move_files"),
        "can_process": get_effective_capability(permission, "can_process"),
        "can_share": get_effective_capability(permission, "can_share"),
        "can_delete": get_effective_capability(permission, "can_delete"),
        "granted_by_user_id": permission.granted_by_user_id,
        "granted_at": permission.granted_at,
        "expires_at": permission.expires_at,
        "is_external_user": permission.is_external_user,
        "is_expired": is_permission_expired(permission),
    }
