# CORE/API/routers/workflow_resume_event.py
"""EXPR 9.4 (slice B) — `POST /v1/road/runs/{run_id}/resume-event`.

Who calls this: the EXTERNAL system that owns the parked node — the thing
that was handed `resume_token` when its node returned
`{status: "awaiting_external_event", resume_token: ...}` (a human-in-the-loop
console, an inbound webhook handler, a lab instrument's POST-back). It is NOT
called by the UI or by the orchestrator: nothing inside MOD knows when the
outside event lands, which is the whole reason the token exists.

Idempotency: the FIRST accepted call flips the parked node + the run back to
RUNNING, so a SECOND call with the same (or any) token answers 409
`not_awaiting` — the run is no longer awaiting. A replay can neither
re-deliver a payload nor advance the DAG twice.

Status -> HTTP (the service vocabulary of
`services.awaiting_external_event.resume_with_event`, mapped verbatim):
    resumed          -> 200 {"status": "resumed", "node_run_id", "advance"}
    not_found        -> 404 (missing run AND another tenant's run: existence
                          never leaks, routers/workflow_mcp_exposure_router.py
                          posture; tenant = current_user.tenant_id)
    not_awaiting     -> 409 (already resumed / never parked)
    token_mismatch   -> 403 (the run IS awaiting, this token is not its token)
    payload_too_large-> 413
    error            -> 503 (service-level failure only; no exception text
                          ever crosses the wire)

NOT mounted in main.py — the lead wires it (`app.include_router(...)` under
/v1). This file is deliberately self-contained so it imports on its own.

The advance re-entry: `resume_with_event` leaves the node_run SUCCESS but
dispatches nothing. The narrowest existing helper that advances downstream
for an already-terminal node is
`routers/worker_tasks.py:11301 _check_and_dispatch_downstream(db, completed_task)`
— the ONE re-entry every other API-side completion uses
(`services/workflow_orchestrator.py:480` for agent nodes,
`services/sub_workflow_runtime.py:427` for a finished child run), fed a
synthetic COMPLETED ApiTaskQueue row for the node we just resumed. We build
that row here (best-effort: an advance failure never fails the resume — the
state change is already committed) and answer "advance": "deferred" instead
of lying. Nothing here logs the token or the payload.

WSIP run-observability READ surface (this file, not a new module)
----------------------------------------------------------------
`workflow_run_events` rows are written by the orchestrator, the autonomous
operator and the adversary store, but the only READ side was per-run
(`crud/workflows_domain.py get_run_events`). The spec requires each event
TYPE to be queryable per workflow / per tenant / per date range, paginated
like the existing observability contract (routers/drift_events.py). These
two GET routes live HERE because `main.py` is claimed by task 883: a new
router module would need a mount line, and this module is already mounted
(`main.py` import + include under "") with prefix `/road/runs`, so the
new routes land at `/v1/road/runs/events` with zero main.py edits.

Read-only: no writes, no commits anywhere below the resume route.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as satext
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/road/runs", tags=["The Road", "workflow-runs"])

RESUME_EVENT_ERRORS = (
    "not_found", "not_awaiting", "token_mismatch", "payload_too_large", "error",
)


class ResumeEventIn(BaseModel):
    """The event the external system delivers.

    `resume_token` is the opaque handle the node handed out when it parked
    (1..512 chars; over-cap is a 422 from pydantic, never a DB touch).
    `payload` is whatever the node should see as its output — any JSON value.
    """
    resume_token: str = Field(..., min_length=1, max_length=512)
    payload: Any = None


class ResumeEventOut(BaseModel):
    status: str
    node_run_id: Optional[int] = None
    advance: Optional[str] = None


def _payload_size(value: Any) -> Optional[int]:
    """Byte length of the delivered payload (size ONLY — the payload itself is
    secret material and never enters the audit trail)."""
    if value is None:
        return None
    try:
        return len(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001 — size is best-effort context
        return None


def _audit_resume_attempt(db: Session, current_user: models.User,
                          run_id: int, outcome: str,
                          payload_bytes: Optional[int],
                          *, commit: bool) -> None:
    """EXPR 9.4 / ISO 27001 AU-2 — one audit-trail row per resume attempt.

    Every outcome (resumed / not_found / not_awaiting / token_mismatch /
    payload_too_large / error) gets exactly one `AuditEvent` row:
    event_name "workflow_run.resume_event", resource = the run id, outcome =
    the service status word, actor = the authenticated user/tenant. The
    details carry ids + the payload's byte length ONLY — never the resume
    token, its hash, or any payload content.

    ``commit``: refusals land on their own; the "resumed" row is added BEFORE
    the resume's existing commit() so it lands in the SAME transaction.

    Best-effort (the admin_adversary_findings.py `import audit` + try/except
    posture): an audit failure never changes the HTTP response. The insert is
    wrapped in its OWN savepoint (`db.begin_nested()`): on the "resumed" path
    (commit=False) a flush error here must only discard the audit row, never
    abort the outer transaction that still holds the resume's state change —
    without the savepoint, the caller's subsequent `db.commit()` would roll
    the whole resume back along with the failed audit insert.
    """
    try:
        with db.begin_nested():
            db.add(models.AuditEvent(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                event_name="workflow_run.resume_event",
                details={
                    "outcome": outcome,
                    "resource_type": "workflow_run",
                    "resource_id": run_id,
                    "payload_bytes": payload_bytes,
                },
            ))
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning(
            "resume-event audit emit failed (run %s, outcome %s): %s",
            run_id, outcome, type(exc).__name__,
        )
        return

    if commit:
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            logger.warning(
                "resume-event audit commit failed (run %s, outcome %s): %s",
                run_id, outcome, type(exc).__name__,
            )
            try:
                db.rollback()  # nothing else pending on the refusal path
            except Exception:  # noqa: BLE001
                pass


def _error(status_code: int, code: str):
    """Bare {"error": code} body (routers/goal_planner.py posture): returned,
    not raised, so it bypasses `response_model` and the dashboard can switch
    on the code without parsing a detail envelope."""
    return JSONResponse(status_code=status_code, content={"error": code})


@router.post("/{run_id}/resume-event", response_model=ResumeEventOut)
def resume_event(
    payload: ResumeEventIn,
    run_id: int,
    current_user: models.User = Depends(require_bearer_or_api_key),
    db: Session = Depends(get_db),
):
    """Deliver an external event to a run parked on `awaiting_external_event`."""
    # 1788i CE carve: awaiting_external_event is paid, so CE never parks a run on
    # `awaiting_external_event` at all — the resume endpoint answers the same
    # shape as "no such awaiting run" (404 not_found), never a 500.
    try:
        from services.awaiting_external_event import resume_with_event
    except ImportError:
        outcome = {"status": "not_found"}
    else:
        outcome = resume_with_event(
            db,
            tenant_id=current_user.tenant_id,
            run_id=run_id,
            resume_token=payload.resume_token,
            payload=payload.payload,
        )
    status = (outcome or {}).get("status")
    payload_bytes = _payload_size(payload.payload)

    if status != "resumed":
        # token_mismatch is deliberately 403 (the run exists and IS awaiting —
        # only the token is wrong), while not_found stays 404 (existence never
        # leaks). Neither body mentions the token.
        code = status if status in RESUME_EVENT_ERRORS else "error"
        # ISO 27001 AU-2: the refusal is a state-changing ATTEMPT, so it gets
        # its own audit row (committed on its own — nothing else changed).
        _audit_resume_attempt(db, current_user, run_id, code, payload_bytes,
                              commit=True)
        return _error(_STATUS_HTTP.get(status, 503), code)

    node_run_id = outcome.get("node_run_id")
    # The "resumed" row is added BEFORE the commit below, so it lands in the
    # SAME transaction as the resume itself (no trail gap if the commit fails).
    _audit_resume_attempt(db, current_user, run_id, "resumed", payload_bytes,
                          commit=False)
    db.commit()  # the resume is the contract: committed before we touch the DAG

    advance = "dispatched"
    try:
        _advance_downstream(db, run_id, node_run_id, current_user.tenant_id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — the advance is best-effort
        logger.warning(
            "resume-event run %s node_run %s: downstream advance failed: %s",
            run_id, node_run_id, type(exc).__name__,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        advance = "deferred"

    return ResumeEventOut(status="resumed", node_run_id=node_run_id,
                          advance=advance)


_STATUS_HTTP = {
    "not_found": 404,
    "not_awaiting": 409,
    "token_mismatch": 403,
    "payload_too_large": 413,
    "error": 503,
}


def _advance_downstream(db: Session, run_id: int,
                        node_run_id: Optional[int], tenant_id: int) -> None:
    """Re-enter the ONE DAG-advance path for the node we just resumed.

    `_check_and_dispatch_downstream(db, completed_task)`
    (routers/worker_tasks.py:11301) is the re-entry every other API-side
    completion uses — routers/worker_tasks.py's own report_task_complete,
    services/workflow_orchestrator.py:480 for agent nodes,
    services/sub_workflow_runtime.py:427 for a finished child run — all fed a
    synthetic COMPLETED ApiTaskQueue row. Same here: the resumed node_run is
    already SUCCESS (slice A stamped it), so we hand the helper a synthetic
    row carrying its node_id and let the standard parent-readiness check
    enqueue whatever is ready. The helper also owns the per-run dispatch lock,
    the terminal-run guard and the whole-workflow-completion detection, which
    is exactly why we call it instead of walking the plan ourselves.

    Raises only to our caller: the resume is already committed, so an advance
    failure answers 200 + "advance": "deferred", never a 5xx.
    """
    from models import ApiTaskQueue, ApiTaskStatus, WorkflowNode
    from routers.worker_tasks import _check_and_dispatch_downstream

    if node_run_id is None:
        return

    node_run = db.get(models.NodeRun, node_run_id)
    graph_node = (db.get(models.WorkflowNode, node_run.workflow_node_id)
                  if node_run is not None else None)
    synth = ApiTaskQueue(
        workflow_run_id=run_id,
        node_run_id=node_run_id,
        node_id=(graph_node.graph_node_id if graph_node is not None
                 else f"resume-event-{node_run_id}"),
        tenant_id=tenant_id,
        queue_name="CPU",
        status=ApiTaskStatus.COMPLETED,
        task_payload={"node_kind": "awaiting_external_event",
                      "node_run_id": node_run_id},
    )
    # expr 9.4 row-order guard: `_check_and_dispatch_downstream` reads the parent's
    # outputs with an UNORDERED `.first()` over every COMPLETED row for (run, node),
    # so this synthetic row is a second such row. Give it the SAME
    # result_payload["outputs"] the real row carries (services/awaiting_external_event
    # ._rewrite_task_outputs already rewrote it) — otherwise Postgres row order can
    # hand the child `parent_outputs == {}` and the event payload is lost.
    # 1788i CE carve: awaiting_external_event is paid (CE never parks a run, so
    # this advance is unreachable there); if it is reached without the module,
    # leave the synthetic row without the rewritten outputs — the child then
    # sees the envelope the worker already reported, never a 500.
    try:
        from services.awaiting_external_event import _task_output_element
    except ImportError:
        _task_output_element = None
    if _task_output_element is not None and node_run is not None \
            and node_run.outputs is not None:
        synth.result_payload = {"outputs": _task_output_element(node_run.outputs)}
    db.add(synth)
    db.flush()
    _check_and_dispatch_downstream(db, synth)


# ---------------------------------------------------------------------------
# WSIP run-observability: query workflow_run_events per workflow / tenant /
# date range / event type (routers/drift_events.py posture).
# ---------------------------------------------------------------------------

EVENTS_TABLE = "workflow_run_events"
RUNS_TABLE = "workflow_runs"

DEFAULT_LIMIT = 50
MIN_LIMIT = 1
MAX_LIMIT = 200
DETAIL_STRING_CAP = 2000


class _RunEventItem(BaseModel):
    id: int
    workflow_run_id: Optional[int] = None
    workflow_id: Optional[int] = None
    ts: Optional[str] = None
    source: Optional[str] = None
    level: Optional[str] = None
    message: Optional[str] = None
    event_type: Optional[str] = None
    event_metadata: Optional[Any] = None


class _RunEventsOut(BaseModel):
    items: List[_RunEventItem]
    next_cursor: Optional[str] = None


def _cap_jsonb(value: Any, cap: int = DETAIL_STRING_CAP) -> Any:
    """Pass stored JSONB through, capping any string longer than `cap` chars."""
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _cap_jsonb(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_jsonb(v, cap) for v in value]
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _cursor_key(ts: Any, event_id: int) -> str:
    """Keyset key for (ts, id); `|` never appears in isoformat()."""
    return f"{_iso(ts)}|{event_id}"


def _parse_cursor(raw: str) -> Optional[tuple]:
    """(ts, id) from a cursor key, or None when malformed."""
    if not raw or "|" not in raw:
        return None
    ts_raw, _, id_raw = raw.rpartition("|")
    try:
        event_id = int(id_raw)
    except ValueError:
        return None
    try:
        ts_val = datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if ts_val.tzinfo is None:
        ts_val = ts_val.replace(tzinfo=timezone.utc)
    return ts_val, event_id


def _utc(value: Any) -> Optional[Any]:
    """Naive datetimes are UTC here — never let Postgres guess its own TZ."""
    if value is None or not hasattr(value, "tzinfo") or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _shape_row(row: tuple) -> Dict[str, Any]:
    (eid, run_id, workflow_id, ts, source, level, message, metadata) = row
    meta = _cap_jsonb(metadata)
    event_type = None
    if isinstance(metadata, dict):
        raw_type = metadata.get("event_type")
        event_type = raw_type if isinstance(raw_type, str) else (
            None if raw_type is None else str(raw_type))
    return {
        "id": eid,
        "workflow_run_id": run_id,
        "workflow_id": workflow_id,
        "ts": _iso(ts),
        "source": source,
        "level": level,
        "message": message,
        "event_type": event_type,
        "event_metadata": meta,
    }


def _fetch_run_events(
    db: Session,
    tenant_id: int,
    workflow_id: Optional[int],
    run_id: Optional[int],
    event_type: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """One page of the CALLER's tenant's run events, newest first.

    `workflow_run_events` has no tenant column, so tenant isolation is the
    JOIN: every row is reachable only through a `workflow_runs` row whose
    tenant_id is the caller's. The join is in EVERY query, never optional.
    """
    params: Dict[str, Any] = {"t": tenant_id, "n": limit + 1}
    filters = ["r.tenant_id = :t", "r.deleted_at IS NULL"]
    if workflow_id is not None:
        filters.append("r.workflow_id = :wid")
        params["wid"] = workflow_id
    if run_id is not None:
        filters.append("ev.workflow_run_id = :rid")
        params["rid"] = run_id
    if event_type is not None:
        # `->>` yields text, so the JSONB comparison never needs `.astext`.
        filters.append("ev.event_metadata->>'event_type' = :et")
        params["et"] = event_type
    since, until = _utc(since), _utc(until)
    if since is not None:
        filters.append("ev.ts >= :since")
        params["since"] = since
    if until is not None:
        filters.append("ev.ts <= :until")
        params["until"] = until
    if cursor is not None:
        cur = _parse_cursor(cursor)
        if cur is None:
            return {"items": [], "next_cursor": None, "bad_cursor": True}
        filters.append("(ev.ts < :c_ts OR (ev.ts = :c_ts AND ev.id < :c_id))")
        params["c_ts"], params["c_id"] = cur

    rows = db.execute(satext(f"""
        SELECT ev.id, ev.workflow_run_id, r.workflow_id, ev.ts, ev.source,
               ev.level, ev.message, ev.event_metadata
        FROM {EVENTS_TABLE} AS ev
        JOIN {RUNS_TABLE} AS r ON r.id = ev.workflow_run_id
        WHERE {' AND '.join(filters)}
        ORDER BY ev.ts DESC, ev.id DESC
        LIMIT :n
    """), params).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_shape_row(row) for row in rows]
    next_cursor = (_cursor_key(rows[-1][3], rows[-1][0]) if has_more and rows
                   else None)
    return {"items": items, "next_cursor": next_cursor}


@router.get("/events", response_model=_RunEventsOut)
def list_run_events(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
    workflow_id: Optional[int] = Query(None, ge=1),
    run_id: Optional[int] = Query(None, ge=1),
    event_type: Optional[str] = Query(None, min_length=1, max_length=64),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    cursor: Optional[str] = Query(None, max_length=128),
) -> Any:
    """Query the caller's tenant's run events: per workflow / per run / per
    date range / per event type, newest first, keyset-paginated.

    Tenant isolation is the JOIN (workflow_run_events has no tenant column):
    a row is visible only through a workflow_runs row of the caller's tenant.
    """
    limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))
    page = _fetch_run_events(db, current_user.tenant_id, workflow_id, run_id,
                             event_type, since, until, limit, cursor)
    if page.get("bad_cursor"):
        return _error(422, "invalid_cursor")
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/events/{event_id}", response_model=_RunEventItem)
def get_run_event_detail(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> Any:
    """One run event by id. Other tenant / missing -> 404 {"error": "not_found"}."""
    rows = db.execute(satext(f"""
        SELECT ev.id, ev.workflow_run_id, r.workflow_id, ev.ts, ev.source,
               ev.level, ev.message, ev.event_metadata
        FROM {EVENTS_TABLE} AS ev
        JOIN {RUNS_TABLE} AS r ON r.id = ev.workflow_run_id
        WHERE ev.id = :eid AND r.tenant_id = :t AND r.deleted_at IS NULL
    """), {"eid": event_id, "t": current_user.tenant_id}).fetchall()

    if not rows:
        return _error(404, "not_found")
    return _shape_row(rows[0])
