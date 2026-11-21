"""Wizard Queue API (wizard-queue-redesign).

The single observational activity/console surface over the unified
``WizardRun`` table. Endpoints:

  GET    /wizard-queue                  list (role-scoped, paginated)
  GET    /wizard-queue/{id}             run detail
  GET    /wizard-queue/{id}/logs        persisted logs (poll fallback)
  GET    /wizard-queue/{id}/logs/stream live tail (SSE)
  GET    /wizard-queue/{id}/logs/export CSV/JSON export
  POST   /wizard-queue/{id}/cancel      HARD cancel (+ cascade to children)
  POST   /wizard-queue/{id}/retry       retry as a NEW linked run
  POST   /wizard-queue/{id}/dismiss     soft-delete one terminal run
  POST   /wizard-queue/clear-completed  soft-delete all terminal in-scope runs

Visibility + action authz are enforced server-side (D5/D15): a user
sees/acts on only their own runs; a tenant admin on their tenant's
runs; a platform admin on platform-level runs only. All log output is
secret-redacted at write time by the service (D19).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

import models
import schemas
from dependencies import get_db, require_active_and_onboarded_user
from models import (
    WizardRun,
    WizardRunLog,
    WizardRunStatus,
    WizardRunType,
    WIZARD_TERMINAL_STATUSES,
    WIZARD_RETRYABLE_STATUSES,
)
from services import wizard_queue as svc
from services import wizard_queue_router_service as router_svc
from services._base import ServiceError
# Side-effect import: registers the per-wizard HARD-cancel / retry hooks
# with the service registry at app startup (main.py loads this router).
from services import wizard_queue_emitters as _wqe  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Wizard Queue"],
    responses={404: {"description": "Not found"}},
)


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _to_response(run: WizardRun, user: models.User) -> schemas.WizardRunResponse:
    resp = schemas.WizardRunResponse.model_validate(run)
    permitted = svc.can_access(user, run)
    resp.can_cancel = permitted and run.status not in WIZARD_TERMINAL_STATUSES
    resp.can_retry = permitted and run.status in WIZARD_RETRYABLE_STATUSES
    return resp


def _get_run_or_404(db: Session, run_id: str) -> WizardRun:
    run = router_svc.get_run_or_none(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Wizard run not found")
    return run


def _require_access(user: models.User, run: WizardRun) -> None:
    # 404 (not 403) when the caller can't even see the run, to avoid
    # leaking existence across tenants — same convention as node_runs.
    if not svc.can_access(user, run):
        raise HTTPException(status_code=404, detail="Wizard run not found")


# ---------------------------------------------------------------------
# list + detail
# ---------------------------------------------------------------------
@router.get("", response_model=schemas.WizardRunListResponse)
@router.get("/", response_model=schemas.WizardRunListResponse)
def list_runs(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 20,
    offset: int = 0,
    status_filter: Optional[str] = None,
    wizard_type: Optional[str] = None,
):
    """Role-scoped, paginated list of live + recent wizard runs. Purely
    observational — it never orders or gates execution (D12)."""
    try:
        rows, total = router_svc.list_runs_for_user(
            db, current_user,
            limit=limit, offset=offset,
            status_filter=status_filter, wizard_type=wizard_type,
        )
    except ServiceError as e:
        raise HTTPException(status_code=422, detail=e.message)
    return schemas.WizardRunListResponse(
        runs=[_to_response(r, current_user) for r in rows],
        total_count=total,
    )


@router.get("/{run_id}", response_model=schemas.WizardRunResponse)
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    run = _get_run_or_404(db, run_id)
    _require_access(current_user, run)
    return _to_response(run, current_user)


# ---------------------------------------------------------------------
# logs — poll fallback + persisted retrieval
# ---------------------------------------------------------------------
@router.get("/{run_id}/logs", response_model=schemas.WizardRunLogsResponse)
def get_logs(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    after_seq: int = -1,
    limit: int = 500,
):
    """Persisted (already-redacted) logs for live OR completed/failed
    runs — the polling fallback for the SSE tail. Pass the last seq seen
    as ``after_seq`` to fetch only newer lines."""
    run = _get_run_or_404(db, run_id)
    _require_access(current_user, run)
    rows, last_seq = router_svc.get_run_logs(
        db, run_id, after_seq=after_seq, limit=limit)
    return schemas.WizardRunLogsResponse(
        run_id=run_id,
        lines=[schemas.WizardRunLogLine.model_validate(r) for r in rows],
        last_seq=last_seq,
    )


@router.get("/{run_id}/logs/stream")
async def stream_logs(
    run_id: str,
    request: Request,
    access_token: str = "",
    ticket: str = "",
):
    """SSE live-tail: replays persisted lines then follows new ones over
    Redis pub/sub.

    Auth (WS5.34b): preferred path is a short-lived, single-use ``?ticket=``
    minted via ``POST /stream-tickets`` (scope ``wizard-run``, bound to this
    run_id at mint time) with normal Bearer auth. The legacy
    ``?access_token=<JWT>`` fallback (behind
    ``SSE_LEGACY_TOKEN_QUERY_ENABLED``, default on) still works for one
    release. The poll endpoint above is the mandatory fallback either way.
    """
    from database import SessionLocal
    user = None
    if ticket:
        from services.stream_tickets import redeem_ticket
        payload = redeem_ticket(ticket)
        if (
            not payload
            or payload.get("scope") != "wizard-run"
            or payload.get("run_id") != run_id
        ):
            raise HTTPException(status_code=401, detail="invalid ticket")
        with SessionLocal() as s:
            user = s.query(models.User).filter(
                models.User.id == payload.get("user_id"),
            ).first()
            if not user or user.status != models.UserStatus.ACTIVE:
                raise HTTPException(status_code=403, detail="invalid ticket")
            run = _get_run_or_404(s, run_id)
            # Re-check access at redeem time, not just at mint time — the
            # ticket only proves it was valid a moment ago.
            _require_access(user, run)
            run_is_terminal = run.status in WIZARD_TERMINAL_STATUSES
            backlog = [
                {"seq": row.seq, "level": row.level, "message": row.message}
                for row in router_svc.get_all_run_logs(s, run_id)
            ]
    else:
        from services.stream_tickets import legacy_token_query_enabled
        if not access_token or not legacy_token_query_enabled():
            raise HTTPException(status_code=401,
                                detail="ticket query param required")
        try:
            from keycloak_auth import get_current_user_claims as kc_claims
            claims = kc_claims(access_token)
        except Exception:
            raise HTTPException(status_code=401, detail="invalid token")
        sub = claims.get("sub") if isinstance(claims, dict) else None
        if not sub:
            raise HTTPException(status_code=401, detail="token missing sub")
        # Auth + access check + backlog snapshot happen in a SHORT-LIVED
        # session that closes BEFORE the (indefinite) Redis tail loop below.
        # Under pgbouncer transaction pooling, keeping the request-scoped
        # Session (Depends(get_db)) open across the stream would pin a
        # server backend and starve the pool. The follow loop is pure Redis
        # pub/sub, so we materialize the persisted backlog + terminal flag
        # here and hold no DB connection.
        with SessionLocal() as s:
            user = router_svc.get_user_by_keycloak_sub(s, sub)
            if not user or user.status != models.UserStatus.ACTIVE:
                raise HTTPException(status_code=403, detail="user not active")
            run = _get_run_or_404(s, run_id)
            _require_access(user, run)
            run_is_terminal = run.status in WIZARD_TERMINAL_STATUSES
            backlog = [
                {"seq": row.seq, "level": row.level, "message": row.message}
                for row in router_svc.get_all_run_logs(s, run_id)
            ]

    import redis as redis_lib
    channel = svc.log_channel(run_id)

    async def event_generator() -> AsyncIterator[bytes]:
        # Replay everything persisted so far (already redacted).
        for row in backlog:
            yield (f"data: " + json.dumps(row) + "\n\n").encode("utf-8")

        # If the run is already terminal there is nothing to tail.
        if run_is_terminal:
            yield b"event: end\ndata: {}\n\n"
            return

        r = redis_lib.Redis.from_url(svc._redis_url())
        pubsub = r.pubsub(ignore_subscribe_messages=True)
        try:
            pubsub.subscribe(channel)
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                # OFFLOAD the blocking read: redis-py get_message is
                # synchronous and would block the uvicorn event loop up to 15s
                # per open stream, starving other requests on this worker.
                msg = await asyncio.to_thread(pubsub.get_message, timeout=15.0)
                if msg is None:
                    yield b": keepalive\n\n"
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                yield (f"data: {data}\n\n").encode("utf-8")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("wizard log SSE error for run %s: %s", run_id, e)
        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
                r.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/logs/export")
def export_logs(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    format: str = "json",
):
    """Download a run's (redacted) logs as CSV or JSON, like the audit
    export. Available while the logs are within the retention window."""
    run = _get_run_or_404(db, run_id)
    _require_access(current_user, run)
    rows = router_svc.get_all_run_logs(db, run_id)

    fmt = (format or "json").lower()
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["seq", "created_at", "level", "message"])
        for r in rows:
            writer.writerow([
                r.seq,
                r.created_at.isoformat() if r.created_at else "",
                r.level or "",
                r.message,
            ])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="wizard_run_{run_id}_logs.csv"',
            },
        )

    if fmt != "json":
        raise HTTPException(status_code=422, detail="format must be csv or json")

    payload = {
        "run_id": run_id,
        "wizard_type": run.wizard_type.value,
        "target_name": run.target_name,
        "status": run.status.value,
        "lines": [
            {
                "seq": r.seq,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "level": r.level,
                "message": r.message,
            }
            for r in rows
        ],
    }
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition":
                f'attachment; filename="wizard_run_{run_id}_logs.json"',
        },
    )


# ---------------------------------------------------------------------
# cancel / retry / dismiss / clear
# ---------------------------------------------------------------------
@router.post("/{run_id}/cancel", response_model=schemas.WizardRunResponse)
def cancel(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    run = _get_run_or_404(db, run_id)
    if not svc.can_access(current_user, run):
        raise HTTPException(status_code=403,
                            detail="Not permitted to cancel this run")
    if run.status in WIZARD_TERMINAL_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"run already {run.status.value}")
    svc.cancel_run(db, run, actor=current_user)
    return _to_response(run, current_user)


@router.post("/{run_id}/retry", response_model=schemas.WizardRunResponse)
def retry(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    run = _get_run_or_404(db, run_id)
    if not svc.can_access(current_user, run):
        raise HTTPException(status_code=403,
                            detail="Not permitted to retry this run")
    if run.status not in WIZARD_RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"only failed/canceled runs can be retried "
                   f"(run is {run.status.value})")
    new_run = svc.retry_run(db, run, actor=current_user)
    return _to_response(new_run, current_user)


@router.post("/{run_id}/dismiss", response_model=schemas.WizardRunResponse)
def dismiss(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete (dismiss) one terminal run. NEVER a hard delete."""
    run = _get_run_or_404(db, run_id)
    if not svc.can_access(current_user, run):
        raise HTTPException(status_code=403,
                            detail="Not permitted to dismiss this run")
    if run.status not in WIZARD_TERMINAL_STATUSES:
        raise HTTPException(status_code=409,
                            detail="only terminal runs can be dismissed")
    svc.soft_delete(db, run, actor=current_user)
    return _to_response(run, current_user)


@router.post("/clear-completed")
def clear_completed(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete every terminal, in-scope run (ISO: soft-delete only)."""
    n = svc.clear_completed(db, current_user)
    return {"cleared": n}
