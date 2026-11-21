"""Service layer for `routers/permission_analytics.py` (tenant-wide permission
metrics: overview, per-folder stats, expiration trend, template usage,
capability distribution, per-user / external-user summaries).

Owns the aggregation logic that used to live inline in the router. All data
access goes through `crud/permission_analytics.py` (plain Cog/User/
PermissionTemplate lookups) and `cog_grants.list_cog_grants` — the existing
Gate 30 sharing-grant read model over `ResourcePermission`, already called
directly from services elsewhere (e.g. `services/permission_templates_service.py`,
`services/permissions_router_service.py`) rather than re-wrapped in a crud
module.

Functions return plain dicts shaped exactly like the router's response
schemas (`PermissionAnalyticsOverview`, `FolderPermissionStats`, ...); FastAPI's
`response_model` on each endpoint validates/coerces them, so this module has
no dependency on the router's Pydantic classes (no circular import, and the
response bytes are unchanged from the pre-migration inline version).

Security-sensitive note: `get_user_permission_summary`'s tenant scoping is
applied INSIDE the CRUD lookup itself (`crud.permission_analytics.
get_tenant_user_by_id`), matching the pre-migration behavior exactly — a
cross-tenant `user_id` has always yielded a plain 404 here (there is no
row-level 403 case for this endpoint to collapse), so `NotFoundError` is the
correct and only outcome to preserve.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from sqlalchemy.orm import Session

import models
from models import PermissionLevel
from cog_grants import list_cog_grants
from permissions import is_permission_expired
from crud import permission_analytics as permission_analytics_crud
from services._base import NotFoundError


# =============================================================================
# Shared aggregation helpers (pure; no behavior change from the pre-migration
# per-endpoint duplicated inline versions)
# =============================================================================


def _level_counts(permissions) -> Dict[str, int]:
    return {
        "viewer": sum(1 for p in permissions if p.level == PermissionLevel.VIEWER),
        "editor": sum(1 for p in permissions if p.level == PermissionLevel.EDITOR),
        "owner": sum(1 for p in permissions if p.level == PermissionLevel.OWNER),
    }


def _expiring_soon_count(permissions, now: datetime, *, days: int = 7) -> int:
    horizon = now + timedelta(days=days)
    return sum(
        1 for p in permissions
        if p.expires_at and not is_permission_expired(p) and p.expires_at <= horizon
    )


def _folder_access_list(db: Session, permissions) -> List[Dict[str, Any]]:
    """Per-permission folder-access rows, excluding soft-deleted cogs."""
    folders: List[Dict[str, Any]] = []
    for perm in permissions:
        cog = permission_analytics_crud.get_active_cog(db, perm.target_id)
        if not cog:
            continue

        folders.append({
            "cog_id": cog.id,
            "cog_name": cog.name,
            "permission_level": perm.level.value,
            "expires_at": perm.expires_at.isoformat() if perm.expires_at else None,
            "granted_at": perm.granted_at.isoformat() if perm.granted_at else None,
            "is_expired": is_permission_expired(perm) if perm.expires_at else False,
        })
    return folders


def _user_permission_summary(db: Session, user: "models.User", permissions, now: datetime) -> Dict[str, Any]:
    counts = _level_counts(permissions)
    return {
        "user_id": user.id,
        "user_email": user.email,
        "user_name": f"{user.first_name} {user.last_name}",
        "total_permissions": len(permissions),
        "viewer_permissions": counts["viewer"],
        "editor_permissions": counts["editor"],
        "owner_permissions": counts["owner"],
        "expiring_soon_count": _expiring_soon_count(permissions, now),
        "folders_accessed": _folder_access_list(db, permissions),
    }


def _folder_permission_stats(db: Session, cog_id: int, perms) -> Dict[str, Any]:
    cog = permission_analytics_crud.get_active_cog(db, cog_id)
    if not cog:
        return None

    owner = permission_analytics_crud.get_user_by_id(db, cog.owner_id)
    counts = _level_counts(perms)
    now = datetime.now(timezone.utc)

    return {
        "cog_id": cog.id,
        "cog_name": cog.name,
        "owner_name": f"{owner.first_name} {owner.last_name}" if owner else "Unknown",
        "total_permissions": len(perms),
        "viewer_count": counts["viewer"],
        "editor_count": counts["editor"],
        "owner_count": counts["owner"],
        "external_user_count": sum(1 for p in perms if p.is_external_user),
        "expiring_soon_count": _expiring_soon_count(perms, now),
    }


# =============================================================================
# Endpoint-facing functions
# =============================================================================


def get_permission_analytics_overview(db: Session, *, tenant_id: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)
    seven_days_future = now + timedelta(days=7)
    thirty_days_future = now + timedelta(days=30)

    # Gate 30: sharing grants reconstructed from ResourcePermission (duck-typed to
    # legacy Permission). Scoped to the tenant's cogs; aggregation below is
    # unchanged (reads .level, .is_external_user, .target_id, .expires_at).
    all_permissions = list_cog_grants(db, tenant_id=tenant_id)

    total_permissions = len(all_permissions)
    counts = _level_counts(all_permissions)
    distribution_by_level = {**counts, "total": total_permissions}

    unique_cog_ids = set(p.target_id for p in all_permissions)
    total_folders_with_permissions = len(unique_cog_ids)

    # External user stats
    external_permissions = [p for p in all_permissions if p.is_external_user]
    external_user_ids = set(p.user_id for p in external_permissions)

    external_expiring_7d = sum(
        1 for p in external_permissions
        if p.expires_at and not is_permission_expired(p) and p.expires_at <= seven_days_future
    )
    external_expiring_30d = sum(
        1 for p in external_permissions
        if p.expires_at and not is_permission_expired(p) and p.expires_at <= thirty_days_future
    )

    durations = []
    for p in external_permissions:
        if p.expires_at and p.granted_at:
            durations.append((p.expires_at - p.granted_at).days)
    avg_duration = sum(durations) / len(durations) if durations else None

    external_user_stats = {
        "total_external_users": len(external_user_ids),
        "total_external_permissions": len(external_permissions),
        "expiring_within_7d": external_expiring_7d,
        "expiring_within_30d": external_expiring_30d,
        "average_duration_days": avg_duration,
    }

    # Activity stats - count permissions granted/revoked in time periods.
    # We track grants via granted_at, but revocations aren't tracked in the DB
    # so we track grants and extensions.
    granted_7d = sum(1 for p in all_permissions if p.granted_at and p.granted_at >= seven_days_ago)
    granted_30d = sum(1 for p in all_permissions if p.granted_at and p.granted_at >= thirty_days_ago)

    extended_7d = 0
    extended_30d = 0
    for p in all_permissions:
        if p.invitation_context and "extensions" in p.invitation_context:
            for ext in p.invitation_context["extensions"]:
                ext_time = datetime.fromisoformat(ext["extended_at"].replace('Z', '+00:00'))
                if ext_time >= seven_days_ago:
                    extended_7d += 1
                if ext_time >= thirty_days_ago:
                    extended_30d += 1

    activity_stats = {
        "granted_last_7d": granted_7d,
        "granted_last_30d": granted_30d,
        "revoked_last_7d": 0,  # Not tracked yet
        "revoked_last_30d": 0,  # Not tracked yet
        "extended_last_7d": extended_7d,
        "extended_last_30d": extended_30d,
    }

    # Top shared folders (by permission count)
    folder_permission_counts: Dict[int, list] = {}
    for p in all_permissions:
        folder_permission_counts.setdefault(p.target_id, []).append(p)

    top_folders = []
    for cog_id, perms in sorted(folder_permission_counts.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
        stats = _folder_permission_stats(db, cog_id, perms)
        if stats is not None:
            top_folders.append(stats)

    return {
        "total_permissions": total_permissions,
        "total_folders_with_permissions": total_folders_with_permissions,
        "distribution_by_level": distribution_by_level,
        "external_user_stats": external_user_stats,
        "activity_stats": activity_stats,
        "top_shared_folders": top_folders,
    }


def get_folder_permission_stats(db: Session, *, tenant_id: int, limit: int) -> List[Dict[str, Any]]:
    # Gate 30: sharing grants from ResourcePermission (tenant-scoped).
    all_permissions = list_cog_grants(db, tenant_id=tenant_id)

    folder_permission_map: Dict[int, list] = {}
    for p in all_permissions:
        folder_permission_map.setdefault(p.target_id, []).append(p)

    folder_stats = []
    for cog_id, perms in folder_permission_map.items():
        stats = _folder_permission_stats(db, cog_id, perms)
        if stats is not None:
            folder_stats.append(stats)

    folder_stats.sort(key=lambda x: x["total_permissions"], reverse=True)
    return folder_stats[:limit]


def get_expiration_trend(db: Session, *, tenant_id: int, days_ahead: int) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)

    # Gate 30: sharing grants with an expiry (tenant-scoped).
    permissions = [g for g in list_cog_grants(db, tenant_id=tenant_id) if g.expires_at is not None]

    expiration_counts = {}
    for i in range(days_ahead):
        date = (now + timedelta(days=i)).date()
        expiration_counts[date] = 0

    for perm in permissions:
        if not perm.expires_at or is_permission_expired(perm):
            continue
        exp_date = perm.expires_at.date()
        if exp_date in expiration_counts:
            expiration_counts[exp_date] += 1

    return [
        {"date": date.isoformat(), "expiring_count": count}
        for date, count in sorted(expiration_counts.items())
    ]


def get_template_usage_stats(db: Session, *, tenant_id: int) -> List[Dict[str, Any]]:
    templates = permission_analytics_crud.list_permission_templates_for_tenant(db, tenant_id)

    all_grants = list_cog_grants(db, tenant_id=tenant_id)

    stats = []
    for template in templates:
        active_count = sum(
            1 for g in all_grants
            if g.invitation_context and g.invitation_context.get("template_id") == template.id
        )

        stats.append({
            "template_id": template.id,
            "template_name": template.name,
            "times_used": template.times_used,
            "last_used_at": template.last_used_at,
            "active_permissions_count": active_count,
        })

    stats.sort(key=lambda x: x["times_used"], reverse=True)
    return stats


def get_capability_distribution(db: Session, *, tenant_id: int) -> List[Dict[str, Any]]:
    # Gate 30: sharing grants from ResourcePermission (tenant-scoped).
    permissions = list_cog_grants(db, tenant_id=tenant_id)

    capabilities = [
        "can_upload",
        "can_create_subfolders",
        "can_move_files",
        "can_process",
        "can_share",
        "can_delete",
    ]

    distribution = []
    for capability in capabilities:
        enabled = 0
        disabled = 0
        default = 0

        for perm in permissions:
            value = getattr(perm, capability, None)
            if value is None:
                default += 1
            elif value is True:
                enabled += 1
            else:
                disabled += 1

        distribution.append({
            "capability": capability,
            "enabled_count": enabled,
            "disabled_count": disabled,
            "default_count": default,
        })

    return distribution


def get_user_permission_summary(db: Session, *, tenant_id: int, user_id: int) -> Dict[str, Any]:
    """Raises `NotFoundError` when `user_id` doesn't exist in `tenant_id`
    (tenant scoping is applied inside the CRUD lookup itself, exactly as the
    pre-migration inline query did — see module docstring)."""
    user = permission_analytics_crud.get_tenant_user_by_id(db, user_id, tenant_id)
    if not user:
        raise NotFoundError("User not found")

    # Gate 30: this user's sharing grants (tenant-scoped).
    permissions = list_cog_grants(db, tenant_id=tenant_id, user_id=user_id)
    now = datetime.now(timezone.utc)

    return _user_permission_summary(db, user, permissions, now)


def get_external_users_summary(db: Session, *, tenant_id: int) -> List[Dict[str, Any]]:
    external_users = permission_analytics_crud.list_external_users_for_tenant(db, tenant_id)

    summaries = []
    now = datetime.now(timezone.utc)

    for user in external_users:
        # Gate 30: this external user's sharing grants (tenant-scoped).
        permissions = list_cog_grants(db, tenant_id=tenant_id, user_id=user.id)
        if not permissions:
            continue

        summaries.append(_user_permission_summary(db, user, permissions, now))

    return summaries
