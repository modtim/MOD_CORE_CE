# API/crud/harbor_cud.py
"""Data access for the Harbor domain (routers/harbor.py).

The ONLY place `db.query(models.X)` runs for this domain: the small
Tenant lookup used by the entitlement checks, and the
TenantImageDeletion / NodeDefinition bookkeeping behind the tenant
Harbor-image soft-delete / restore / reap flow.

Gate 2b per-file migration (see services/harbor_service.py for the
business logic that calls these, and routers/harbor.py for the thin
HTTP handlers). Secret-backed platform/custom-registry reads and
writes already went through crud.secret_cud before this migration and
are left as-is -- they were never inline `db.query`.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Set, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import models


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Fetch a tenant by id, unscoped -- the caller (service) applies any
    entitlement/permission policy on top of this."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


# ---------------------------------------------------------------------------
# NodeDefinition (gpu_variants enrichment join + post-reap purge)
# ---------------------------------------------------------------------------

def list_nodes_for_tenant(db: Session, tenant_id: int) -> List[models.NodeDefinition]:
    """All NodeDefinition rows owned by a tenant. Deliberately unfiltered
    by soft-delete -- matches the pre-migration query exactly, since both
    the variant-lookup join (tenant-images listing) and the post-reap
    purge need to see every row regardless of the node's own soft-delete
    state."""
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id == tenant_id,
    ).all()


def set_node_gpu_variants(db: Session, node: models.NodeDefinition, variants: dict) -> None:
    """Replace a node's gpu_variants mapping in place (JSON column --
    needs flag_modified so SQLAlchemy notices the in-place mutation)."""
    node.gpu_variants = variants
    flag_modified(node, "gpu_variants")


def delete_node(db: Session, node: models.NodeDefinition) -> None:
    """Soft-delete a NodeDefinition whose last image variant was reaped.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). NodeDefinition already carries SoftDeleteMixin and is
    in database.py's soft-delete listener, so the tombstoned node drops out of
    live reads. Best-effort de-index from Qdrant (node RAG) so it also leaves
    the wizard's describe-search, mirroring
    ``node_definition_cud.soft_delete_node_definition``. The caller commits.
    """
    from datetime import datetime, timezone
    nid = int(node.id)
    node.deleted_at = datetime.now(timezone.utc)
    db.add(node)
    try:
        from crud import node_definition_cud as _ndc
        _ndc._deindex_node(nid)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TenantImageDeletion (ISO 27001 soft-delete bookkeeping)
# ---------------------------------------------------------------------------

def active_soft_delete_keys(db: Session, tenant_id: int) -> Set[Tuple[str, str]]:
    """(repository, tag) pairs still inside the retention window."""
    rows = db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.reaped_at.is_(None),
    ).all()
    return {(r.repository, r.tag) for r in rows}


def get_active_soft_delete(
    db: Session, tenant_id: int, repository: str, tag: str,
) -> Optional[models.TenantImageDeletion]:
    """Unreaped soft-delete row for (tenant, repo, tag), if any -- used
    for the soft-delete endpoint's idempotency check."""
    return db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.repository == repository,
        models.TenantImageDeletion.tag == tag,
        models.TenantImageDeletion.reaped_at.is_(None),
    ).first()


def list_active_soft_deletes(db: Session, tenant_id: int) -> List[models.TenantImageDeletion]:
    """A tenant's restorable ("Recently deleted") rows, newest first."""
    return db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.reaped_at.is_(None),
    ).order_by(models.TenantImageDeletion.deleted_at.desc()).all()


def get_latest_deletion(
    db: Session, tenant_id: int, repository: str, tag: str,
) -> Optional[models.TenantImageDeletion]:
    """Most recent deletion row for (tenant, repo, tag) regardless of
    reaped state -- the restore endpoint needs to see a reaped row too,
    to return 410 instead of a false "nothing to restore"."""
    return db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.repository == repository,
        models.TenantImageDeletion.tag == tag,
    ).order_by(models.TenantImageDeletion.id.desc()).first()


def list_expired_deletions(db: Session, *, now: datetime) -> List[models.TenantImageDeletion]:
    """Unreaped rows whose retention window has passed -- the reaper's
    work queue."""
    return db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.reaped_at.is_(None),
        models.TenantImageDeletion.retention_until <= now,
    ).all()


def add_soft_delete(
    db: Session,
    *,
    tenant_id: int,
    repository: str,
    tag: str,
    digest: Optional[str],
    size_bytes: Optional[int],
    deleted_by_user_id: int,
    retention_until: datetime,
) -> models.TenantImageDeletion:
    row = models.TenantImageDeletion(
        tenant_id=tenant_id,
        repository=repository,
        tag=tag,
        digest=digest,
        size_bytes=size_bytes,
        deleted_by_user_id=deleted_by_user_id,
        retention_until=retention_until,
    )
    db.add(row)
    return row


def delete_deletion_row(db: Session, row: models.TenantImageDeletion) -> None:
    # ISO-exception: this removes a soft-delete TOMBSTONE row, it is not a
    # business-record deletion. `TenantImageDeletion` IS the ISO tombstone
    # ledger for a Harbor image (its `deleted_at` is the deletion EVENT time,
    # not a soft-delete marker); the only caller is the image-RESTORE path
    # (services/harbor_service.restore), which un-deletes the image by dropping
    # its tombstone. Retaining it would leave the image permanently hidden.
    db.delete(row)


def mark_reaped(row: models.TenantImageDeletion, *, reaped_at: datetime) -> None:
    row.reaped_at = reaped_at


# ---------------------------------------------------------------------------
# Audit (ISO 27001 append-only trail)
# ---------------------------------------------------------------------------

def add_audit_event(db: Session, **kwargs) -> "models.AuditEvent":
    from models import AuditEvent

    event = AuditEvent(**kwargs)
    db.add(event)
    return event
