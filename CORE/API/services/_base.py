"""Service / CRUD data-access base pattern (openspec: stack-training-and-prewarm, D9).

THE STRUCTURAL ROOT the refactor targets: the deep audit
(docs/api-audit/deep/layer-summary.md) found ~100/141 router files run
`db.query(models.X)` INLINE, bypassing the service/CRUD layer. That is *why* the
same bug class recurs everywhere — each router hand-writes its own data access,
so the 3 cartesian queries, 54 N+1s, 61 unbounded lists and 40
external-call-in-a-DB-session offenders were all written per-router.

This module defines the convention a migrated domain follows so the CORRECT
pattern (tenant scoping, eager-load discipline, bounded pagination, no external
call inside a txn) lives in ONE place per domain.

LAYERING (top calls down; never the reverse):

    routers/<domain>.py        HTTP only: parse/validate input, auth via
                               api_conventions, shape the response. NO db.query.
        -> services/<domain>.py    business logic / orchestration / cross-entity
                                   rules. Owns the transaction boundary. Calls
                                   external systems OUTSIDE the DB session.
            -> crud/<domain>_cud.py   the ONLY place `db.query(models.X)` lives
                                      for this domain. Eager-load + pagination
                                      discipline enforced here, once.
                -> models.py          SQLAlchemy ORM.

RULES (checked, later enforced, by tools/api_lint.py):
    * A router MUST NOT contain `db.query(`. All data access goes through a
      crud/service function.
    * List reads MUST be bounded — take an `api_conventions.Pagination` and pass
      it down; return the slice + a total so the router can build a `Page`.
    * Collection eager-loads use `selectinload` (separate SELECT), never multiple
      `joinedload` collections in one query (the cartesian bug).
    * No blocking external call (requests/httpx/S3/keycloak/vault) while a DB
      Session is open — do object/network I/O in the service, before/after the
      session, not interleaved with it.
    * Every tenant-scoped query filters on `tenant_id`; `CRUDBase` does this for
      you when the model has a `tenant_id` column.

`CRUDBase` is a small, optional convenience for the common single-model,
tenant-scoped case. A domain with richer needs writes plain functions in its
`crud/<domain>_cud.py` (matching the existing style, e.g. crud/tag_cud.py) — the
RULES above are the contract, `CRUDBase` is just one way to satisfy them.

SCAFFOLD ONLY: no existing domain is rewired onto this. See
`services/_reference_domain_example.py` for a full worked router->service->crud
example.
"""
from __future__ import annotations

from typing import Any, Generic, List, Optional, Sequence, Tuple, Type, TypeVar

from sqlalchemy.orm import Session

ModelT = TypeVar("ModelT")


class CRUDBase(Generic[ModelT]):
    """A minimal, tenant-aware base for a single-model CRUD module.

    Subclass or instantiate with a model class. Every read is optionally
    tenant-scoped (when the model has a `tenant_id` column) and every list is
    bounded (`limit`/`offset`). This centralises the discipline the audit found
    missing per-router.

    Example (in crud/<domain>_cud.py):

        from services._base import CRUDBase
        import models

        widgets = CRUDBase(models.Widget)

        def list_widgets(db, tenant_id, *, limit, offset):
            return widgets.list(db, tenant_id=tenant_id, limit=limit, offset=offset)
    """

    #: Relationship names to `selectinload` on reads (NEVER joinedload two
    #: collections together — that is the cartesian-row-multiplication bug).
    selectin_relationships: Sequence[str] = ()

    #: Default column to order list results by (falls back to primary key).
    default_order_by: Optional[str] = None

    def __init__(self, model: Type[ModelT]):
        self.model = model
        self._has_tenant = hasattr(model, "tenant_id")
        self._has_soft_delete = hasattr(model, "deleted_at")

    # -- internal query builders ------------------------------------------

    def _base_query(self, db: Session, tenant_id: Optional[int]):
        """A query scoped to the tenant + excluding soft-deleted rows + eager
        loading declared relationships. The one place scoping is applied."""
        from sqlalchemy.orm import selectinload

        query = db.query(self.model)
        if self._has_tenant and tenant_id is not None:
            query = query.filter(self.model.tenant_id == tenant_id)
        if self._has_soft_delete:
            query = query.filter(self.model.deleted_at.is_(None))
        for rel in self.selectin_relationships:
            attr = getattr(self.model, rel, None)
            if attr is not None:
                query = query.options(selectinload(attr))
        return query

    def _order(self, query):
        col_name = self.default_order_by
        if col_name and hasattr(self.model, col_name):
            return query.order_by(getattr(self.model, col_name))
        return query

    # -- read -------------------------------------------------------------

    def get(self, db: Session, obj_id: Any, *, tenant_id: Optional[int] = None) -> Optional[ModelT]:
        """Fetch one row by primary key, tenant-scoped. Returns None if absent."""
        return self._base_query(db, tenant_id).filter(self.model.id == obj_id).first()

    def list(
        self,
        db: Session,
        *,
        tenant_id: Optional[int] = None,
        limit: int,
        offset: int,
    ) -> Tuple[List[ModelT], int]:
        """Return a bounded slice AND the total count for pagination.

        Returns `(rows, total)`. The router builds `api_conventions.Page` from
        this. The count is computed on the same scoped/filtered query (before
        the limit/offset window) so it reflects what the caller can see.
        """
        scoped = self._base_query(db, tenant_id)
        total = scoped.order_by(None).count()
        rows = self._order(scoped).offset(offset).limit(limit).all()
        return rows, total

    # -- write ------------------------------------------------------------

    def create(self, db: Session, values: dict, *, commit: bool = True) -> ModelT:
        """Insert a row. Caller passes already-validated values (from a Pydantic
        schema). Commits by default; pass `commit=False` to batch within a
        service-owned transaction."""
        obj = self.model(**values)
        db.add(obj)
        if commit:
            db.commit()
            db.refresh(obj)
        else:
            db.flush()
        return obj

    def update(self, db: Session, obj: ModelT, values: dict, *, commit: bool = True) -> ModelT:
        """Apply a partial update (only provided keys) to an existing row."""
        for key, value in values.items():
            setattr(obj, key, value)
        if commit:
            db.commit()
            db.refresh(obj)
        else:
            db.flush()
        return obj

    def soft_delete(self, db: Session, obj: ModelT, *, commit: bool = True) -> ModelT:
        """Soft-delete when the model supports it (ISO 27001: no hard deletes —
        MEMORY iso-no-hard-delete). Raises if the model has no `deleted_at`."""
        if not self._has_soft_delete:
            raise AttributeError(
                f"{self.model.__name__} has no `deleted_at`; a hard delete would "
                "violate the append-only / no-hard-delete rule. Use a real "
                "soft-delete column or an explicit, audited CRUD function."
            )
        from datetime import datetime, timezone

        obj.deleted_at = datetime.now(timezone.utc)
        if commit:
            db.commit()
            db.refresh(obj)
        else:
            db.flush()
        return obj


class ServiceError(Exception):
    """Base for domain/service-layer errors.

    Services raise domain errors (this or subclasses); the ROUTER maps them to
    `HTTPException` (or the canonical error envelope). Keeping HTTP concerns out
    of the service layer is what lets a service be reused by a background job or
    another service, not just an HTTP handler.
    """

    def __init__(self, message: str, *, code: str = "service_error", details: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


class NotFoundError(ServiceError):
    """Requested entity does not exist (router maps -> 404)."""

    def __init__(self, message: str = "Not found", *, details: Any = None):
        super().__init__(message, code="not_found", details=details)


class ConflictError(ServiceError):
    """Write conflicts with existing state, e.g. a uniqueness violation
    (router maps -> 409)."""

    def __init__(self, message: str = "Conflict", *, details: Any = None):
        super().__init__(message, code="conflict", details=details)
