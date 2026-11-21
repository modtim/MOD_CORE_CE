"""GG.6b (launch-completion-megaplan, gg-commerce) — entitlement hand-out
lifecycle: `source` (how an entitlement was granted) + `expires_at`
(soft revocation) on the node/stack/workflow entitlement rows.

Design notes
------------
* `source` is a free-form String(20) column (deliberately NOT a PG enum —
  enum migrations are painful in this schema; see TenantNodeEntitlement).
  The canonical vocabulary is ``VALID_SOURCES`` below; pre-existing rows
  use the legacy values ``admin_grant`` / ``plan_grant`` which remain
  accepted so no backfill is needed.
* `expires_at` is a soft revocation marker, NOT a hard delete: a
  platform-admin revoke sets ``expires_at = now()`` (ISO 27001 — no hard
  deletes). An entitlement is ACTIVE iff ``deleted_at IS NULL`` AND
  ``expires_at IS NULL OR expires_at > now``. Every entitlement check
  MUST apply :func:`active_filter` (one shared helper, not copy-paste)
  so an expired row reads as "not entitled".

Admin grant/revoke endpoints live in routers/entitlements_admin.py and
call :func:`grant` / :func:`revoke` below (audited via
``audit.log_admin_action``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Type

from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

# GG.6b vocabulary for freshly-issued entitlements.
VALID_SOURCES = ("purchase", "free", "plan", "grant", "invite", "trial")

# Legacy values already present on existing rows; still valid for reads.
_LEGACY_SOURCES = ("admin_grant", "plan_grant")

ACCEPTED_SOURCES = frozenset(VALID_SOURCES) | frozenset(_LEGACY_SOURCES)

# kind -> (entitlement model, item FK column, item model)
ENTITLEMENT_KINDS = {
    "node": (models.TenantNodeEntitlement, "node_definition_id", models.NodeDefinition),
    "workflow": (models.TenantWorkflowEntitlement, "workflow_listing_id", models.WorkflowListing),
    "stack": (models.TenantStackEntitlement, "listing_id", models.StackListing),
}


def active_filter(model: Type[models.Base]) -> "sqlalchemy.sql.elements.ColumnElement":
    """ONE shared predicate for "this entitlement row is active":
    not soft-deleted AND not expired (``expires_at IS NULL OR
    expires_at > now()``).

    Every entitlement check (catalog ownership flags, closure, run
    guard, listing service, node-ownership CRUD) should fold this into
    its existing filter rather than re-deriving the expiry logic:

        db.query(model).filter(
            model.tenant_id == tenant_id,
            active_filter(model),
        )

    ``func.now()`` is evaluated by PostgreSQL (server clock), which is
    what matters — the API and the worker share it.
    """
    return and_(
        model.deleted_at.is_(None),
        or_(model.expires_at.is_(None), model.expires_at > func.now()),
    )


def is_active(ent) -> bool:
    """Python-side twin of :func:`active_filter` for an already-loaded row."""
    if ent is None:
        return False
    if ent.deleted_at is not None:
        return False
    if ent.expires_at is None:
        return True
    # Fall back to a tz-aware client clock when expires_at is naive.
    now = datetime.now(timezone.utc)
    exp = ent.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > now


def _require_platform_admin(current_user) -> None:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="platform admin required")


def _kind_meta(kind: str):
    if kind not in ENTITLEMENT_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown entitlement kind {kind!r}")
    return ENTITLEMENT_KINDS[kind]


def get_entitlement_by_kind(
    db: Session, model: Type[models.Base], fk_col: str, item_id: int, tenant_id: int,
):
    """A tenant's entitlement row for one item of an `ENTITLEMENT_KINDS`
    model, regardless of active/expired/deleted state. Same shape as the
    inline lookup in `grant()` above. Verbatim relocation of the
    revoked-items lookup in `routers/marketplace_webhook.py`
    (MKT.C2) — callers apply `is_active`/`active_filter` themselves."""
    return (
        db.query(model)
        .filter(getattr(model, fk_col) == item_id, model.tenant_id == tenant_id)
        .first()
    )


def grant(
    db: Session,
    *,
    tenant_id: int,
    kind: str,
    item_id: int,
    source: str = "grant",
    expires_at: Optional[datetime] = None,
    current_user,
    request=None,
    actor_user_id: Optional[int] = None,
) -> dict:
    """Create (or re-activate) an entitlement row for ``tenant_id`` on
    ``item_id`` of the given kind. Idempotent on the unique
    (tenant, item) constraint: re-granting a soft-expired/soft-deleted
    row re-activates it in place (clears expires_at/deleted_at) instead
    of duplicating — there is no hard delete to resurrect.

    Audited via ``audit.log_admin_action`` (non-fatal on audit error).
    """
    model, fk_col, item_model = _kind_meta(kind)
    if source not in ACCEPTED_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of {sorted(ACCEPTED_SOURCES)}, got {source!r}",
        )
    item = db.get(item_model, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{kind} {item_id} not found")
    if hasattr(item, "deleted_at") and item.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"{kind} {item_id} not found")

    if current_user is not None:
        _require_platform_admin(current_user)

    ent = (
        db.query(model)
        .filter(getattr(model, fk_col) == item_id, model.tenant_id == tenant_id)
        .first()
    )
    if ent is not None:
        before = {"source": ent.source,
                  "expires_at": ent.expires_at.isoformat() if ent.expires_at else None,
                  "deleted_at": ent.deleted_at.isoformat() if ent.deleted_at else None}
        ent.source = source
        ent.expires_at = expires_at
        ent.deleted_at = None
        ent.granted_by_user_id = (actor_user_id
                                  if current_user is None else current_user.id)
        created = False
    else:
        ent = model(**{fk_col: item_id, "tenant_id": tenant_id,
                       "source": source, "expires_at": expires_at,
                       "granted_by_user_id": (actor_user_id
                                              if current_user is None else current_user.id)})
        db.add(ent)
        before = None
        created = True
    db.commit()
    db.refresh(ent)

    if current_user is not None:
        try:
            import audit
            audit.log_admin_action(
                db, models.AuditAction.ADMIN_CONFIG_CHANGE, current_user.id,
                request=request, target_tenant_id=tenant_id,
                resource_type=f"entitlement:{kind}", resource_id=ent.id,
                metadata={"source": source, "created": created, "before": before,
                          "item_id": item_id,
                          "expires_at": ent.expires_at.isoformat() if ent.expires_at else None},
            )
        except Exception:  # noqa: BLE001
            logger.exception("entitlement_source.grant: audit log failed (non-fatal)")

    return {"id": ent.id, "tenant_id": tenant_id, "kind": kind, "item_id": item_id,
            "source": ent.source,
            "expires_at": ent.expires_at.isoformat() if ent.expires_at else None,
            "created": created}


def revoke(
    db: Session,
    *,
    kind: str,
    entitlement_id: int,
    current_user,
    request=None,
) -> dict:
    """SOFT revoke: set ``expires_at = now()`` on the entitlement row
    (never a hard delete — ISO 27001). Idempotent: revoking an
    already-expired row just moves the marker forward.

    Audited via ``audit.log_admin_action`` (non-fatal on audit error).
    """
    model, fk_col, _item_model = _kind_meta(kind)
    if current_user is not None:
        _require_platform_admin(current_user)

    ent = db.get(model, entitlement_id)
    if ent is None or ent.deleted_at is not None:
        raise HTTPException(status_code=404, detail="entitlement not found")

    before = {"source": ent.source,
              "expires_at": ent.expires_at.isoformat() if ent.expires_at else None}
    # Server clock, not the app's clock: the active_filter compares against
    # SQL now(), so a client-side datetime can race the DB clock (a host a
    # few seconds ahead would leave the row "still active" for a moment).
    ent.expires_at = func.now()
    db.commit()
    db.refresh(ent)

    if current_user is not None:
        try:
            import audit
            audit.log_admin_action(
                db, models.AuditAction.ADMIN_CONFIG_CHANGE, current_user.id,
                request=request, target_tenant_id=ent.tenant_id,
                resource_type=f"entitlement:{kind}", resource_id=ent.id,
                metadata={"before": before, "after": {"expires_at": ent.expires_at.isoformat()}},
            )
        except Exception:  # noqa: BLE001
            logger.exception("entitlement_source.revoke: audit log failed (non-fatal)")

    return {"id": ent.id, "tenant_id": ent.tenant_id, "kind": kind,
            "item_id": getattr(ent, fk_col),
            "expires_at": ent.expires_at.isoformat() if ent.expires_at else None,
            "soft": True}


def revoke_by_item(
    db: Session,
    *,
    kind: str,
    item_id: int,
    tenant_id: int,
    current_user,
    request=None,
) -> dict:
    """SOFT revoke of a tenant's entitlement identified by (kind, item_id,
    tenant_id) rather than a raw entitlement id — the shape the admin
    Catalog UI actually has on hand (it lists catalog items, not
    entitlement rows), mirroring `stack_packages.revoke_stack_listing`'s
    lookup-then-revoke pattern for node/workflow (and stack) kinds alike.
    Idempotent: no ACTIVE entitlement for the pair is a no-op
    (``revoked: False``), never a 404 — "already not entitled" is not an
    error.
    """
    model, fk_col, _item_model = _kind_meta(kind)
    if current_user is not None:
        _require_platform_admin(current_user)

    ent = get_entitlement_by_kind(db, model, fk_col, item_id, tenant_id)
    if ent is None or not is_active(ent):
        return {"tenant_id": tenant_id, "kind": kind, "item_id": item_id,
                "revoked": False, "entitlement_id": None, "expires_at": None}

    result = revoke(db, kind=kind, entitlement_id=ent.id, current_user=current_user, request=request)
    return {"tenant_id": tenant_id, "kind": kind, "item_id": item_id,
            "revoked": True, "entitlement_id": ent.id,
            "expires_at": result["expires_at"]}
