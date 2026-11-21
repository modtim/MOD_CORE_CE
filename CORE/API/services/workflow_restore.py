"""expr 9.10 — restore a soft-deleted workflow inside the recovery window.

``crud/workflow_cud.delete_workflow`` (via
``routers/workflows_domains/assets_cogs_files.py:404``) tombstones the
``Workflow`` row AND, in the same transaction, the workflow's three memory
tables (``services/workflow_memory_lifecycle.soft_delete_workflow_memory``).
Until now the tombstone was one-way: nothing in the API could bring a deleted
workflow back, and the memory rows were only recoverable through the
lifecycle service, which no endpoint calls.

This module is the recovery path. It mirrors the cog precedent
(``services/cogs_assets_cogs_files_service.restore_cog`` — fetch-any-delete-state,
owner/manager authz, 404-not-found, 409-not-deleted, audit row) with one
addition the design asks for: a bounded window. A workflow tombstoned longer
than ``expr.workflow_recovery_window_days`` (default 30, 1..365, tenant-
overridable) is past recovery and gets a 410, not a silent resurrection.

Window authority: the workflow-level window registered HERE is the one that
decides both the 410 and the memory restore — it is passed to
``restore_workflow_memory`` as ``recovery_days`` so the two layers can never
disagree about whether a tombstone is still reachable.

ISO 27001: MOD never hard-deletes. There is no ``DELETE`` statement anywhere
in this module — restore only clears ``deleted_at`` back to NULL on the
workflow row and on the memory rows stamped with that exact timestamp.

Transaction discipline: the service NEVER commits. It mutates the fetched ORM
row, flushes so the change is written into the caller's transaction, and the
ROUTER commits (the delete endpoint, by contrast, commits inside crud — see
the ordering comment there).
"""
from __future__ import annotations

from product_names import themed_logger
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from services import platform_settings as psr
from services import workflow_memory_lifecycle as _wfl_mem
from services._base import ConflictError, NotFoundError, ServiceError

import models

logger = themed_logger(__name__, 'workflows')

RECOVERY_WINDOW_KEY = "expr.workflow_recovery_window_days"
DEFAULT_RECOVERY_WINDOW_DAYS = 30


def _validate_recovery_window_days(value) -> None:
    """Honest-range rule for the workflow recovery window: 1..365 days.

    Zero would mean "restore is impossible", which is a flag's job, not a
    window's; a year is the longest window design.md contemplates.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 365:
        raise ValueError(f"must be 1..365, got {value!r}")


psr.register(
    RECOVERY_WINDOW_KEY,
    int,
    DEFAULT_RECOVERY_WINDOW_DAYS,
    validator=_validate_recovery_window_days,
    description="expr 9.10 (tasks.md 9.10) — how long a soft-deleted WORKFLOW "
                "stays restorable after its deleted_at timestamp. POST "
                "/road/{id}/restore refuses with 410 once now - deleted_at "
                "exceeds this window; the same window is handed to "
                "restore_workflow_memory so the workflow row and its memory "
                "rows agree. Default 30 (design.md 30-day recovery window), "
                "range 1..365, tenant-scope row can raise/lower it. No env var.",
    group="expr",
)


def _recovery_window_days(db: Optional[Session], tenant_id: Optional[int]) -> int:
    """Resolve the recovery window, fail-closed to the registered default."""
    try:
        value = psr.get_setting(RECOVERY_WINDOW_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed: a broken settings layer
        logger.exception("workflow recovery window unreadable; using default")
        return DEFAULT_RECOVERY_WINDOW_DAYS
    try:
        _validate_recovery_window_days(value)
    except ValueError:
        logger.warning("workflow recovery window out of range: %r", value)
        return DEFAULT_RECOVERY_WINDOW_DAYS
    return int(value)


class ForbiddenError(ServiceError):
    """Caller is not entitled to restore (router maps -> 403).

    Declared locally, same shape as the cogs domain service: the typed errors
    a module raises are its own subclasses of :class:`ServiceError` so the
    router can map each to an exact status code.
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class GoneError(ServiceError):
    """Tombstone is older than the recovery window (router maps -> 410).

    A plain :class:`ServiceError` with its own ``code`` — deliberately NOT a
    :class:`ConflictError` subclass: ``ConflictError.__init__`` fixes the code
    and takes no ``code=`` kwarg, so subclassing it would force us to reuse
    ``conflict``. The restore router checks this type before the base classes.
    """

    def __init__(self, message: str = "Recovery window expired", *, details: Any = None):
        super().__init__(message, code="recovery_window_expired", details=details)


def restore_workflow(db: Session, *, tenant_id: int, workflow_id: int,
                     current_user) -> models.Workflow:
    """Undo ONE workflow soft-deletion: the workflow row AND its memory rows.

    Fetch is tenant-scoped and INCLUDES soft-deleted rows
    (``execution_options(include_deleted=True)``, same opt-out the autonomous
    deploy path uses at services/autonomous_deploy_on_approval.py:214) — a
    plain lookup would 404 the very row this endpoint exists to recover.

    Errors (typed, router maps them):
      - :class:`NotFoundError` — no such workflow in THIS tenant (a same-id row
        in another tenant is indistinguishable: ISO/DoD isolation, no leak).
      - :class:`ConflictError` — the row is not currently soft-deleted.
      - :class:`ForbiddenError` — caller is neither the workflow's owner nor a
        manager (tenant manager = ``parent_user_id IS NULL``) nor a platform
        admin; same rule as ``restore_cog``.
      - :class:`GoneError` — ``deleted_at`` is older than the recovery window.

    On success: clears ``deleted_at`` on the workflow row, then calls
    ``restore_workflow_memory`` with the EXACT tombstone timestamp and the
    window resolved here (so both layers agree), writes a
    ``WORKFLOW_RESTORED`` audit row the same non-fatal way ``restore_cog``
    does, and flushes. Never commits — the router does.
    """
    db_workflow = (
        db.query(models.Workflow)
        .execution_options(include_deleted=True)
        .filter(models.Workflow.id == workflow_id,
                models.Workflow.tenant_id == tenant_id)
        .first()
    )
    if db_workflow is None:
        raise NotFoundError("Workflow not found")

    is_manager = bool(
        getattr(current_user, "is_platform_admin", False)
        or getattr(current_user, "parent_user_id", None) is None
    )
    if not (is_manager or db_workflow.created_by_user_id == current_user.id):
        raise ForbiddenError("You don't have permission to restore this workflow")

    if db_workflow.deleted_at is None:
        raise ConflictError("This workflow is not deleted.")

    deleted_at = db_workflow.deleted_at
    window = _recovery_window_days(db, tenant_id)
    now = datetime.now(timezone.utc)
    if (now - deleted_at) > timedelta(days=window):
        raise GoneError("Recovery window expired")

    db_workflow.deleted_at = None
    db.add(db_workflow)

    # Memory rows: exact-timestamp scoped, so rows tombstoned by the 9.11
    # retention sweep or individually deleted earlier stay deleted — same
    # posture as the deletion, which stamped every live row with THIS
    # timestamp.  The service never raises; its status is informational only.
    memory = _wfl_mem.restore_workflow_memory(
        db, tenant_id=tenant_id, workflow_id=workflow_id,
        deleted_at=deleted_at, recovery_days=window)

    # Audit: content restored. Non-fatal, same try/except shape as restore_cog.
    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=tenant_id,
            user_id=current_user.id,
            event_name="WORKFLOW_RESTORED",
            details={
                "workflow_id": workflow_id,
                "workflow_name": db_workflow.name,
                "deleted_at": str(deleted_at),
                "recovery_window_days": window,
                "memory": memory.get("status"),
                "memory_tables": memory.get("tables"),
            },
        ))
    except Exception:
        logger.exception("WORKFLOW_RESTORED audit emit failed for workflow=%s",
                         workflow_id)

    db.flush()
    return db_workflow
