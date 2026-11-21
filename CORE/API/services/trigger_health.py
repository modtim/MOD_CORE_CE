"""services/trigger_health.py — per-trigger health summary (sz-4.8).

Read-only display logic for a "trigger health" column: for one trigger, or for
every live trigger of a tenant, report what the trigger has actually been doing
over a window — event arrivals, runs it dispatched, run success rate, dead
letters it parked, and rate-limit drops it swallowed. No router yet (service
layer only). No writes, no commits, no new tables.

Sources (all existing tables, seeded elsewhere):
  * `WorkflowTrigger` — identity/type/`is_active`; soft-delete tombstones
    (`deleted_at`, from `SoftDeleteMixin`) are excluded.
  * `TriggerEventLog` — one row per observed event arrival (backs dedup /
    rate-limit / debounce state); `fired_at` is the trigger's event history.
  * `WorkflowRun.triggered_by_trigger_id` — the run a trigger dispatched; the
    run's window anchor is `started_at` (the run's own creation stamp; `WorkflowRun`
    has no `created_at` column — `services/incident_export.py` windows the same way).
  * `TriggerDeadLetter` — retry-exhausted fires (append-only sink).
  * `AuditEvent` — rate-limit drops. `services/triggers/pipeline.py` records a
    fire blocked by the rate limit as `WORKFLOW_TRIGGER_BLOCKED` with
    `details.reason == "rate_limit"` for BOTH overflow policies ("drop" and
    "defer"), so `rate_limited_in_window` counts those audit rows instead of
    inventing a table. Two honest caveats: `_audit()` is wrapped in a
    best-effort try/except ("audit must never block the fire"), so the count is
    a LOWER BOUND; and a row whose `tenant_id` was never stamped is not
    attributable to a tenant, so it is not counted here.

Query shape: a CONSTANT number of queries regardless of trigger count — one
per aggregate (triggers, event counts, event maxima, runs, dead letters, audit
drops), each grouped by `trigger_id`. Never N+1.

`connection_status` rule (documented; thresholds are module constants because
this is display logic, not a tunable behaviour knob — the platform-settings
registry is for behaviour, not display):

  1. trigger disabled (`is_active` false)          -> "idle"
  2. no events ever (all-time, not just window)    -> "idle"
  3. dead letters > 0 and success_rate < 0.5       -> "failing"
  4. dead letters > 0                              -> "degraded"
  5. no terminal run outcome in the window         -> "unknown"
  6. success_rate < 0.9                            -> "degraded"
  7. otherwise                                     -> "ok"

`run_success_rate` scores TERMINAL runs in the window only: SUCCESS/COMPLETED
count as success, FAILURE/FAILED/BLOCKED as failure; QUEUED/RUNNING/PENDING_ACTION
runs are still in flight and are excluded from the denominator. No terminal run
in the window -> `None` ("nothing to score" is not "all failed").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models

DEFAULT_WINDOW_HOURS = 24
DEGRADED_SUCCESS_RATE = 0.9
FAILING_SUCCESS_RATE = 0.5
SUCCESS_STATUSES = frozenset({"success", "completed"})
FAILURE_STATUSES = frozenset({"failure", "failed", "blocked"})
RATE_LIMIT_AUDIT_EVENT = "WORKFLOW_TRIGGER_BLOCKED"
RATE_LIMIT_AUDIT_REASON = "rate_limit"


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a timestamp to UTC; naive DB timestamps read as UTC."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _nowish(now: Optional[datetime]) -> datetime:
    base = now if isinstance(now, datetime) else datetime.now(tz=timezone.utc)
    return _utc(base) or base





def _enum_value(value: Any) -> Optional[str]:
    """Enum-valued columns project back as the enum member when the column
    declares `values_callable` (it does not here) and as the raw DB value
    otherwise — normalise both flavours."""
    if hasattr(value, "value"):
        return value.value
    return value


def _aggregate(db: Session, tenant_id: int, window_hours: int,
               now: Optional[datetime]) -> Dict[int, Dict[str, Any]]:
    """One pass over every live trigger of `tenant_id`. Constant query count:
    triggers, event counts, event maxima, runs, dead letters, audit drops."""
    TriggerEventLog = models.TriggerEventLog
    TriggerDeadLetter = models.TriggerDeadLetter
    WorkflowRun = models.WorkflowRun
    WorkflowTrigger = models.WorkflowTrigger
    AuditEvent = models.AuditEvent

    now_dt = _nowish(now)
    window_start = now_dt - timedelta(hours=int(window_hours))

    live = db.query(
        WorkflowTrigger.id, WorkflowTrigger.trigger_type, WorkflowTrigger.is_active,
    ).filter(
        WorkflowTrigger.tenant_id == tenant_id,
        WorkflowTrigger.deleted_at.is_(None),
    ).all()
    rows: Dict[int, Dict[str, Any]] = {
        int(tid): {
            "trigger_id": int(tid),
            "trigger_type": _enum_value(ttype),
            "enabled": bool(active) if active is not None else True,
            "connection_status": "unknown",
            "last_event_at": None,
            "events_in_window": 0,
            "runs_in_window": 0,
            "run_success_rate": None,
            "dead_letters_in_window": 0,
            "rate_limited_in_window": 0,
        }
        for tid, ttype, active in live
    }
    if not rows:
        return rows
    ids = list(rows)

    for tid, count in db.query(
        TriggerEventLog.trigger_id,
        func.count(TriggerEventLog.id),
    ).filter(
        TriggerEventLog.trigger_id.in_(ids),
        TriggerEventLog.fired_at >= window_start,
    ).group_by(TriggerEventLog.trigger_id).all():
        if tid is not None and int(tid) in rows:
            rows[int(tid)]["events_in_window"] = int(count or 0)

    for tid, latest in db.query(
        TriggerEventLog.trigger_id,
        func.max(TriggerEventLog.fired_at),
    ).filter(
        TriggerEventLog.trigger_id.in_(ids),
    ).group_by(TriggerEventLog.trigger_id).all():
        if tid is not None and int(tid) in rows and latest is not None:
            rows[int(tid)]["last_event_at"] = _utc(latest).isoformat()

    for tid, status in db.query(
        WorkflowRun.triggered_by_trigger_id, WorkflowRun.status,
    ).filter(
        WorkflowRun.triggered_by_trigger_id.in_(ids),
        WorkflowRun.tenant_id == tenant_id,
        WorkflowRun.started_at >= window_start,
    ).all():
        row = rows.get(int(tid)) if tid is not None else None
        if row is None:
            continue
        row["runs_in_window"] += 1
        value = _enum_value(status)
        if value in SUCCESS_STATUSES:
            row["_ok"] = row.get("_ok", 0) + 1
        elif value in FAILURE_STATUSES:
            row["_bad"] = row.get("_bad", 0) + 1

    for tid, count in db.query(
        TriggerDeadLetter.trigger_id,
        func.count(TriggerDeadLetter.id),
    ).filter(
        TriggerDeadLetter.trigger_id.in_(ids),
        TriggerDeadLetter.first_failed_at >= window_start,
    ).group_by(TriggerDeadLetter.trigger_id).all():
        if tid is not None and int(tid) in rows:
            rows[int(tid)]["dead_letters_in_window"] = int(count or 0)

    for ev in db.query(
        AuditEvent.timestamp, AuditEvent.details,
    ).filter(
        AuditEvent.tenant_id == tenant_id,
        AuditEvent.event_name == RATE_LIMIT_AUDIT_EVENT,
        AuditEvent.timestamp >= window_start,
    ).all():
        details = ev.details or {}
        if details.get("reason") != RATE_LIMIT_AUDIT_REASON:
            continue
        tid = details.get("trigger_id")
        if isinstance(tid, int) and tid in rows:
            rows[tid]["rate_limited_in_window"] += 1

    for row in rows.values():
        ok, bad = row.pop("_ok", 0), row.pop("_bad", 0)
        terminal = ok + bad
        row["run_success_rate"] = (ok / terminal) if terminal else None
        row["connection_status"] = _connection_status(row)
    return rows


def _connection_status(row: Dict[str, Any]) -> str:
    """The documented rule — see the module docstring."""
    if not row["enabled"]:
        return "idle"
    if row["last_event_at"] is None:
        return "idle"
    rate = row["run_success_rate"]
    dl = row["dead_letters_in_window"]
    if dl > 0 and rate is not None and rate < FAILING_SUCCESS_RATE:
        return "failing"
    if dl > 0:
        return "degraded"
    if rate is None:
        return "unknown"
    return "degraded" if rate < DEGRADED_SUCCESS_RATE else "ok"


def trigger_health(db: Session, tenant_id: int, trigger_id: int, *,
                   window_hours: int = DEFAULT_WINDOW_HOURS,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Health summary for one tenant-scoped trigger.

    Raises `LookupError` when the trigger is not a live trigger of this tenant
    (foreign tenant, unknown id, or soft-deleted tombstone).
    """
    rows = _aggregate(db, tenant_id, window_hours, now)
    key = int(trigger_id)
    if key not in rows:
        raise LookupError(
            f"trigger {trigger_id} is not a live trigger of tenant {tenant_id}"
        )
    return rows[key]


def tenant_trigger_health(db: Session, tenant_id: int, *,
                          window_hours: int = DEFAULT_WINDOW_HOURS,
                          now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Health summary for every live trigger of one tenant, sorted by id.

    Soft-deleted triggers are excluded; another tenant's triggers never appear.
    """
    rows = _aggregate(db, tenant_id, window_hours, now)
    return [rows[tid] for tid in sorted(rows)]
