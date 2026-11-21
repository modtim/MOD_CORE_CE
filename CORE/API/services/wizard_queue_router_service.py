"""Wizard Queue ROUTER service (Gate 2b: per-file service/CRUD migration
for ``routers/wizard_queue.py``).

Named ``wizard_queue_router_service`` (NOT ``wizard_queue_service``)
because ``services/wizard_queue.py`` already owns this domain's
write path, scope/authorization logic and native cancel/retry hooks —
this module must not collide with or duplicate it. It composes that
existing module instead of reimplementing anything in it.

This is the thin POLICY layer between the router and
``crud/wizard_queue.py``: input clamping, status/wizard_type string ->
enum validation (mapped by the router to the same 422s it always
returned), and last-seq bookkeeping. No HTTP here (raises
``services._base.ServiceError``, not ``HTTPException``); no
``db.query`` here (all data access goes through ``crud.wizard_queue``).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from models import WizardRun, WizardRunLog, WizardRunStatus, WizardRunType

import crud.wizard_queue as crud_wq
from services import wizard_queue as svc
from services._base import ServiceError

# Policy caps — independent of any caller-supplied value, matching the
# clamps the router used to hand-roll inline.
MAX_LIST_LIMIT = 100
MAX_LOG_LIMIT = 2000


def clamp_list_limit(limit: int) -> int:
    if limit > MAX_LIST_LIMIT:
        return MAX_LIST_LIMIT
    if limit < 1:
        return 1
    return limit


def clamp_log_limit(limit: int) -> int:
    return MAX_LOG_LIMIT if limit > MAX_LOG_LIMIT else limit


def parse_status_filter(status_filter: Optional[str]) -> Optional[WizardRunStatus]:
    if not status_filter:
        return None
    try:
        return WizardRunStatus(status_filter)
    except ValueError:
        raise ServiceError(f"invalid status '{status_filter}'", code="unprocessable_entity")


def parse_wizard_type_filter(wizard_type: Optional[str]) -> Optional[WizardRunType]:
    if not wizard_type:
        return None
    try:
        return WizardRunType(wizard_type)
    except ValueError:
        raise ServiceError(f"invalid wizard_type '{wizard_type}'", code="unprocessable_entity")


def list_runs_for_user(
    db: Session,
    user: models.User,
    *,
    limit: int,
    offset: int,
    status_filter: Optional[str],
    wizard_type: Optional[str],
) -> Tuple[List[WizardRun], int]:
    """Role-scoped, bounded list + total for the wizard queue list
    endpoint. Raises ``ServiceError`` (code ``unprocessable_entity``) on
    a bad status/wizard_type filter string, same as the pre-migration
    inline 422s."""
    limit = clamp_list_limit(limit)
    status_enum = parse_status_filter(status_filter)
    wizard_type_enum = parse_wizard_type_filter(wizard_type)
    return crud_wq.list_scoped_runs(
        db,
        scope=lambda q: svc.scope_query(q, user),
        status_filter=status_enum,
        wizard_type_filter=wizard_type_enum,
        limit=limit,
        offset=offset,
    )


def get_run_or_none(db: Session, run_id: str) -> Optional[WizardRun]:
    return crud_wq.get_run_by_id(db, run_id)


def get_run_logs(
    db: Session, run_id: str, *, after_seq: int, limit: int
) -> Tuple[List[WizardRunLog], int]:
    """Bounded page of persisted logs + the new last_seq cursor."""
    limit = clamp_log_limit(limit)
    rows = crud_wq.list_logs_after(db, run_id, after_seq=after_seq, limit=limit)
    last_seq = rows[-1].seq if rows else after_seq
    return rows, last_seq


def get_all_run_logs(db: Session, run_id: str) -> List[WizardRunLog]:
    """ALL persisted logs for a run (export download + SSE backlog)."""
    return crud_wq.list_logs_all(db, run_id)


def get_user_by_keycloak_sub(db: Session, sub: str) -> Optional[models.User]:
    return crud_wq.get_user_by_keycloak_sub(db, sub)
