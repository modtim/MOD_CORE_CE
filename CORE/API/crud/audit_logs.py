# API/crud/audit_logs.py
"""Data access for the audit-logs domain (routers/audit_logs.py).

The ONLY place `db.query(models.PermissionAuditLog)` /
`db.query(models.AuditEvent)` / `db.query(models.Cog)` /
`db.query(models.Tenant)` run for this domain (Gate 2b migration). Read-only:
this domain never writes rows here (see audit.py for the hash-chain writers,
which are frozen/off-limits for this task).

Tenant scoping note: `get_permission_audit_logs` / `get_permission_audit_log`
are called with an explicit `tenant_id` and filter on it here (list/get). The
export queries are intentionally tenant-UNSCOPED (platform-admin-only "all
tenants" export) -- that gate is enforced by the router/service, not here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import desc, or_, cast
from sqlalchemy import String
from sqlalchemy.orm import Session, aliased

import models


def get_tenant(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    """Fetch a tenant by id."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def _search_permission_audit_logs(query, term: str):
    """BUG.B30a free-text search bar ("Audit: add a search bar").

    Adds the actor/target email joins (OUTER, so a row whose target_user_id is
    NULL is still searchable by its actor email) and one case-insensitive ILIKE
    OR across the action, the resource_type and both emails. `%` and `_` in the
    caller's term are LIKE wildcards to the database, not to the user, so they
    are escaped (escape character first) before the surrounding `%...%`.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    actor = aliased(models.User)
    target = aliased(models.User)
    return (
        query.outerjoin(
            actor, models.PermissionAuditLog.actor_user_id == actor.id
        )
        .outerjoin(
            target, models.PermissionAuditLog.target_user_id == target.id
        )
        .filter(
            or_(
                cast(models.PermissionAuditLog.action, String).ilike(
                    pattern, escape="\\"
                ),
                models.PermissionAuditLog.resource_type.ilike(pattern, escape="\\"),
                actor.email.ilike(pattern, escape="\\"),
                target.email.ilike(pattern, escape="\\"),
            )
        )
    )


def list_permission_audit_logs(
    db: Session,
    *,
    tenant_id: int,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    action: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    q: Optional[str] = None,
    limit: int,
    offset: int,
) -> List["models.PermissionAuditLog"]:
    """Bounded, tenant-scoped, filtered list, most recent first.

    `q` is the BUG.B30a free-text search bar: when set, rows must match it
    (case-insensitive ILIKE) on the action, the resource type, or the actor /
    target email. The email joins are OUTER so a row with no target user is
    still searchable by its actor.
    """
    query = db.query(models.PermissionAuditLog).filter(
        models.PermissionAuditLog.tenant_id == tenant_id
    )

    term = (q or "").strip()
    if term:
        query = _search_permission_audit_logs(query, term)

    if resource_type:
        query = query.filter(models.PermissionAuditLog.resource_type == resource_type)

    if resource_id is not None:
        query = query.filter(models.PermissionAuditLog.resource_id == resource_id)

    if action:
        query = query.filter(models.PermissionAuditLog.action == action)

    if actor_user_id is not None:
        query = query.filter(models.PermissionAuditLog.actor_user_id == actor_user_id)

    if target_user_id is not None:
        query = query.filter(models.PermissionAuditLog.target_user_id == target_user_id)

    if start_date:
        query = query.filter(models.PermissionAuditLog.timestamp >= start_date)

    if end_date:
        query = query.filter(models.PermissionAuditLog.timestamp <= end_date)

    return (
        query.order_by(desc(models.PermissionAuditLog.timestamp))
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_permission_audit_log(
    db: Session, log_id: int, *, tenant_id: int
) -> Optional["models.PermissionAuditLog"]:
    """Fetch one tenant-scoped permission audit log row by id."""
    return (
        db.query(models.PermissionAuditLog)
        .filter(
            models.PermissionAuditLog.id == log_id,
            models.PermissionAuditLog.tenant_id == tenant_id,
        )
        .first()
    )


def get_cog_name_map(db: Session, cog_ids: Iterable[int]) -> Dict[int, Dict[str, object]]:
    """Batch-resolve `{cog_id: {"name", "parent_id", "parent_name"}}` for
    resource-name enrichment on the list endpoint (2 queries: cogs, then their
    parents)."""
    cog_ids = set(cog_ids)
    cog_map: Dict[int, Dict[str, object]] = {}
    if not cog_ids:
        return cog_map

    cogs = (
        db.query(models.Cog.id, models.Cog.name, models.Cog.parent_id)
        .filter(models.Cog.id.in_(cog_ids))
        .all()
    )
    cog_map = {c.id: {"name": c.name, "parent_id": c.parent_id} for c in cogs}

    parent_ids = {c.parent_id for c in cogs if c.parent_id}
    if parent_ids:
        parents = (
            db.query(models.Cog.id, models.Cog.name)
            .filter(models.Cog.id.in_(parent_ids))
            .all()
        )
        parent_map = {p.id: p.name for p in parents}
        for info in cog_map.values():
            info["parent_name"] = parent_map.get(info["parent_id"])

    return cog_map


def list_permission_audit_logs_for_export(
    db: Session,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 10000,
) -> List["models.PermissionAuditLog"]:
    """Cross-tenant permission-audit-log export slice (platform-admin gated
    by the router/service, NOT here)."""
    query = db.query(models.PermissionAuditLog)
    if start_date:
        query = query.filter(models.PermissionAuditLog.timestamp >= start_date)
    if end_date:
        query = query.filter(models.PermissionAuditLog.timestamp <= end_date)
    return query.order_by(models.PermissionAuditLog.id).limit(limit).all()


def list_audit_events_for_export(
    db: Session,
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 10000,
) -> List["models.AuditEvent"]:
    """Cross-tenant general audit-event export slice (platform-admin gated
    by the router/service, NOT here)."""
    query = db.query(models.AuditEvent)
    if start_date:
        query = query.filter(models.AuditEvent.timestamp >= start_date)
    if end_date:
        query = query.filter(models.AuditEvent.timestamp <= end_date)
    return query.order_by(models.AuditEvent.id).limit(limit).all()


def find_delivery_row_by_watermark_id(
    db: Session, wm_id: str,
) -> Optional["models.PermissionAuditLog"]:
    """GY.V1d — the most recent delivery-audit row whose metadata carries
    `wm_id` (routers/compliance_watermark_decode.py). The two key names in
    play: `watermark_id` (services.delivery_audit.record_delivery's own
    kwarg — share-link / zip paths) and `payload_id` (the V1b single-file
    path's extra metadata key). JSONB containment (`@>`) is used rather than
    `.astext` — this column's TypeDecorator (models.JSONB) breaks `.astext`
    (see models.py), and `.op("@>")` is the pattern already used on this
    exact column in services/conformance/checks.py. Cross-tenant on purpose
    (platform-admin-only forensic lookup) — the gate is enforced by the
    router, not here."""
    from sqlalchemy import func, or_

    return (
        db.query(models.PermissionAuditLog)
        .filter(
            or_(
                models.PermissionAuditLog.audit_metadata.op("@>")(
                    func.jsonb_build_object("watermark_id", wm_id)),
                models.PermissionAuditLog.audit_metadata.op("@>")(
                    func.jsonb_build_object("payload_id", wm_id)),
            )
        )
        .order_by(models.PermissionAuditLog.timestamp.desc())
        .first()
    )
