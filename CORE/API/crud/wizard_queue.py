"""Wizard Queue CRUD (wizard-queue-redesign / Gate 2b migration).

The ONLY place `db.query(WizardRun)` / `db.query(WizardRunLog)` /
`db.query(models.User)` lives for the ``routers/wizard_queue.py`` /
``services/wizard_queue_router_service.py`` pair.

Fetches here are deliberately tenant-UNscoped — role/tenant visibility
is applied by the CALLER (``services.wizard_queue.scope_query`` for
lists, ``services.wizard_queue.can_access`` for single-row access) so a
legitimate 403 (visible-but-forbidden) never collapses into a 404
(see MEMORY: 404-vs-403 trap / SHARED RULES item 5).
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from models import WizardRun, WizardRunLog, WizardRunStatus, WizardRunType


def get_run_by_id(db: Session, run_id: str) -> Optional[WizardRun]:
    """Fetch one non-deleted run by id. Tenant-UNscoped: the caller
    applies visibility/action policy (``services.wizard_queue.can_access``)."""
    return (
        db.query(WizardRun)
        .filter(WizardRun.id == run_id, WizardRun.deleted_at.is_(None))
        .first()
    )


def list_scoped_runs(
    db: Session,
    *,
    scope: Callable,
    status_filter: Optional[WizardRunStatus] = None,
    wizard_type_filter: Optional[WizardRunType] = None,
    limit: int,
    offset: int,
) -> Tuple[List[WizardRun], int]:
    """Bounded, role-scoped list of runs + total count.

    ``scope`` is a ``query -> query`` callable (typically
    ``services.wizard_queue.scope_query`` bound to the caller) applied
    BEFORE filtering/counting/paging so total reflects what the caller
    can see.
    """
    q = db.query(WizardRun).filter(WizardRun.deleted_at.is_(None))
    q = scope(q)
    if status_filter is not None:
        q = q.filter(WizardRun.status == status_filter)
    if wizard_type_filter is not None:
        q = q.filter(WizardRun.wizard_type == wizard_type_filter)

    total = q.count()
    rows = (
        q.order_by(WizardRun.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return rows, total


def list_logs_after(
    db: Session, run_id: str, *, after_seq: int, limit: int
) -> List[WizardRunLog]:
    """Bounded page of a run's persisted logs, ascending seq, strictly
    after ``after_seq`` (poll-fallback tail)."""
    return (
        db.query(WizardRunLog)
        .filter(WizardRunLog.run_id == run_id, WizardRunLog.seq > after_seq)
        .order_by(WizardRunLog.seq.asc())
        .limit(limit)
        .all()
    )


def list_logs_all(db: Session, run_id: str) -> List[WizardRunLog]:
    """ALL persisted logs for a run, ascending seq (export + SSE
    backlog). Intentionally unbounded here — log rows age in place and
    are archived (soft-marked ``archived_at``, never deleted) by the
    FX.57 system-log archive job, not paginated, for these two
    callers."""
    return (
        db.query(WizardRunLog)
        .filter(WizardRunLog.run_id == run_id)
        .order_by(WizardRunLog.seq.asc())
        .all()
    )


def get_user_by_keycloak_sub(db: Session, sub: str) -> Optional[models.User]:
    """Fetch a user by their Keycloak subject id (SSE log-stream auth
    path — the caller separately checks `status == ACTIVE`)."""
    return db.query(models.User).filter(models.User.keycloak_user_id == sub).first()
