"""GX.S7 (launch-completion-megaplan Group X) — scheduled HTTP triggers.

Routes (mounted at /v1/monkeys in main.py — OZ-F3 rename at the MOUNT, the
router module and its identifiers stay; router prefix /triggers; so the public
path is /v1/monkeys/triggers):
    GET    /v1/monkeys/triggers               list the tenant's triggers
    POST   /v1/monkeys/triggers               create (returns the signing secret ONCE)
    GET    /v1/monkeys/triggers/{id}          fetch one
    PATCH  /v1/monkeys/triggers/{id}/pause    pause firing
    PATCH  /v1/monkeys/triggers/{id}/resume   resume firing
    POST   /v1/monkeys/triggers/{id}/run      fire now (one-off, off the schedule)
    GET    /v1/monkeys/triggers/{id}/runs     append-only run history
    DELETE /v1/monkeys/triggers/{id}          soft delete

Auth: `require_bearer_or_api_key` (Bearer token OR scoped app key); every
route is tenant-scoped by the authenticated user's tenant. Business logic
lives in services/http_triggers.py.
"""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
import services.http_triggers as ht
from dependencies import get_db, require_bearer_or_api_key

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

router = APIRouter(prefix="/triggers", tags=["HTTP Triggers"])


class TriggerCreateIn(BaseModel):
    name: str = Field(..., max_length=255)
    # 'cron' (5-field cron_expression) | 'interval' (interval_seconds)
    schedule_kind: str = Field(..., pattern="^(cron|interval)$")
    cron_expression: Optional[str] = Field(None, max_length=128)
    interval_seconds: Optional[int] = Field(None, ge=1)
    method: str = Field("POST", max_length=8)
    path: str = Field("", max_length=512)
    # 'stack-service' | 'app'
    target_kind: str = Field(..., pattern="^(stack-service|app)$")
    app_id: Optional[int] = None
    stack_id: Optional[int] = None
    stack_service: Optional[str] = Field(None, max_length=127)
    stack_port: Optional[int] = Field(None, ge=1, le=65535)
    # Optional outbound-webhook target for the failure alert fan-out.
    callback_url: Optional[str] = Field(None, max_length=1024)


def _tenant_id(user: models.User) -> int:
    if not user.tenant_id:
        from fastapi import HTTPException, status as s
        raise HTTPException(status_code=s.HTTP_400_BAD_REQUEST,
                            detail="user has no tenant")
    return user.tenant_id


@router.get("", response_model=dict)
def list_triggers(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    return {"triggers": ht.list_triggers(db, _tenant_id(user))}


@router.post("", status_code=201, response_model=dict)
def create_trigger(
    body: TriggerCreateIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    return ht.create_trigger(
        db, tenant_id=_tenant_id(user), user_id=user.id,
        app_id=body.app_id, name=body.name,
        schedule_kind=body.schedule_kind,
        cron_expression=body.cron_expression,
        interval_seconds=body.interval_seconds,
        method=body.method, path=body.path,
        target_kind=body.target_kind, stack_id=body.stack_id,
        stack_service=body.stack_service, stack_port=body.stack_port,
        callback_url=body.callback_url,
    )


def _get_or_404(db: Session, trigger_id: int, tenant_id: int):
    t = ht.get_trigger(db, trigger_id, tenant_id)
    if t is None:
        from fastapi import HTTPException, status as s
        raise HTTPException(status_code=s.HTTP_404_NOT_FOUND,
                            detail="trigger not found")
    return t


@router.get("/{trigger_id}", response_model=dict)
def get_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    t = _get_or_404(db, trigger_id, _tenant_id(user))
    return ht._public(t)


@router.patch("/{trigger_id}/pause", response_model=dict)
def pause_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    t = _get_or_404(db, trigger_id, _tenant_id(user))
    t = ht.pause_trigger(db, trigger_id, _tenant_id(user), user_id=user.id)
    return ht._public(t)


@router.patch("/{trigger_id}/resume", response_model=dict)
def resume_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    t = _get_or_404(db, trigger_id, _tenant_id(user))
    t = ht.resume_trigger(db, trigger_id, _tenant_id(user), user_id=user.id)
    return ht._public(t)


@router.post("/{trigger_id}/run", response_model=dict)
def run_trigger_now(
    trigger_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    """Fire the trigger once, immediately, off the schedule.

    The fire runs on a daemon thread with its own DB sessions (the HTTP
    call must not happen inside this request's open transaction) and the
    response reports that the fire was dispatched — the run-history row is
    written by the fire path."""
    t = _get_or_404(db, trigger_id, _tenant_id(user))
    from database import SessionLocal as _SL

    def _go():
        try:
            ht.run_trigger_fire(_SL, trigger_id=trigger_id)
        except Exception:  # noqa: BLE001
            logger.exception("http_triggers: run-now failed (trigger=%s)",
                             trigger_id)

    threading.Thread(target=_go, name=f"http-trigger-run-now-{trigger_id}",
                     daemon=True).start()
    return {"status": "dispatched", "trigger_id": trigger_id}


@router.get("/{trigger_id}/runs", response_model=dict)
def list_runs(
    trigger_id: int,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    _get_or_404(db, trigger_id, _tenant_id(user))
    limit = max(1, min(limit, 200))
    return {"runs": ht.list_runs(db, trigger_id, _tenant_id(user),
                                 skip=skip, limit=limit)}


@router.delete("/{trigger_id}", response_model=dict)
def delete_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_bearer_or_api_key),
):
    t = _get_or_404(db, trigger_id, _tenant_id(user))
    ok = ht.soft_delete_trigger(db, trigger_id, _tenant_id(user),
                                user_id=user.id)
    return {"status": "deleted" if ok else "not_found",
            "trigger_id": trigger_id}
