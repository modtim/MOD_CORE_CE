"""sz-2.9 — per-tenant paused-run quota + paused-run timeout (service only).

Two platform settings (group "workflows", tenant-overridable through the
platform-settings registry — get_setting resolves tenant row > platform row
> default):

    workflows.paused_run_quota       int  default 0 in CE (0..1000; 0 = UNLIMITED)
        Max RUNNING runs a tenant may hold with paused=True at the same
        time. can_pause answers "may this tenant pause one more run?"
    workflows.paused_run_timeout_s   int  default 3600 (60..86400)
        A paused run older than this (per-tenant resolved) is auto-cancelled
        by sweep_expired_paused_runs so abandoned pauses never pin the
        operator's capacity forever.

Age anchor: WorkflowRun has NO paused_at column (models.py: paused_at exists
only on WorkerLicense / stacks, not on runs), so the sweep cannot use
started_at — a run that ran for hours before being paused would be cancelled
on the very next sweep. Instead, the first sweep that observes a paused run
writes a `paused_seen_at` (UTC ISO) marker into the run's `parameters` JSONB
and does NOT cancel it; later sweeps measure age from that marker. This
means the enforced timeout is accurate to within one sweep interval (a run
paused just after a sweep runs is first seen ~one interval later, then
timed out `timeout_s` after that), not to the second. If `paused` flips
back to False the marker is cleared so a later re-pause starts a fresh
window.

Wiring (pause endpoint + scheduler sweep) is a LATER item; this module is
the service surface only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import case, func, or_

import models
from services import platform_settings as psr
from utils.paid_import import optional_paid

_data_classification = optional_paid("services.data_classification")

logger = logging.getLogger(__name__)

QUOTA_KEY = "workflows.paused_run_quota"
TIMEOUT_KEY = "workflows.paused_run_timeout_s"
DEFAULT_QUOTA = 10
DEFAULT_TIMEOUT_S = 3600
AUDIT_EVENT = "PAUSED_RUN_TIMEOUT_CANCELLED"
CANCEL_MARKER = "sz29_timeout_cancelled"  # parameters-JSONB idempotency marker
SEEN_AT_MARKER = "paused_seen_at"  # parameters-JSONB age anchor (no paused_at column)


def _validate_quota(value: Any) -> None:
    if not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 1000:
        return
    raise ValueError(f"workflows.paused_run_quota must be an int in [0, 1000]: {value!r}")


def _validate_timeout(value: Any) -> None:
    if not isinstance(value, bool) and isinstance(value, int) and 60 <= value <= 86400:
        return
    raise ValueError(f"workflows.paused_run_timeout_s must be an int in [60, 86400]: {value!r}")


psr.register(
    QUOTA_KEY,
    int,
    psr._ce_unlimited(DEFAULT_QUOTA),
    validator=_validate_quota,
    group="workflows",
    description="sz-2.9 — max RUNNING+paused workflow runs a tenant may hold "
                "simultaneously. can_pause refuses (allowed=False) at the quota. "
                "0 means UNLIMITED (the CE default; CE has no quantity caps). "
                "Default 0 (unlimited); range 0..1000. Tenant-overridable.",
)
psr.register(
    TIMEOUT_KEY,
    int,
    DEFAULT_TIMEOUT_S,
    validator=_validate_timeout,
    group="workflows",
    description="sz-2.9 — seconds a paused RUNNING run may stay paused before "
                "sweep_expired_paused_runs auto-cancels it. Age measured from "
                "the sweep-written paused_seen_at marker (runs have no "
                "paused_at column), accurate to one sweep interval. Default "
                "3600, range 60..86400. Tenant-overridable.",
)


def _is_paused_expr() -> Any:
    """SQL predicate: a run counts as "paused" for the sz-2.9 quota + expiry
    sweep when EITHER flag is set (B99):

      * `paused IS TRUE` — the user's run-level pause (pause_workflow_run), and
      * `paused_nodes` is a non-empty JSONB array — a node-level hold: the
        operator's pause-node, live-debugger step_over/hot_swap children, and
        breakpoint_gate holds. Those never touch `paused` (B99 fix), so without
        this half of the predicate a breakpoint-held run would silently escape
        the quota and never be swept for timeout.

    Postgres-native form: `paused_nodes` is `Column(JSONB)` (models.py ~2387)
    and the test engine is the real Postgres `modtech_test` DB (tests/conftest.py
    creates the schema with Base.metadata.create_all against that engine), so
    the `jsonb_*` functions are available — the repo already uses raw
    `jsonb_array_length` the same way in routers/worker_tasks.py.

    The guard is `jsonb_typeof(...) = 'array'` inside a CASE, NOT
    `paused_nodes.isnot(None)`: SQLAlchemy's JSON/JSONB TypeDecorator defaults
    to none_as_null=False, so an unset column stores JSON `null` (a scalar),
    which passes an `is not NULL` SQL check and then makes
    `jsonb_array_length` raise "cannot get array length of a scalar". CASE
    evaluates only the matched branch, so the length call only ever sees real
    arrays; SQL NULL / JSON null / object / string all take the else arm.
    """
    return or_(
        models.WorkflowRun.paused.is_(True),
        case(
            (func.jsonb_typeof(models.WorkflowRun.paused_nodes) == "array",
             func.jsonb_array_length(models.WorkflowRun.paused_nodes) > 0),
            (None, False),
            else_=False,
        ),
    )


def _node_held(run: models.WorkflowRun) -> bool:
    """Python mirror of _is_paused_expr for the already-materialised sweep row."""
    return bool(run.paused) or bool(run.paused_nodes)


def _paused_runs(db: Session, tenant_id: Optional[int] = None) -> List[models.WorkflowRun]:
    """RUNNING runs a tenant is holding — soft-delete excluded. tenant_id=None = all.

    "Holding" = run-level `paused IS TRUE` OR a non-empty `paused_nodes` JSONB
    array (node-level hold: pause-node / live debugger / breakpoint gate). See
    _is_paused_expr for the exact predicate and why the JSONB form is safe.
    """
    q = (
        db.query(models.WorkflowRun)
        .filter(
            _is_paused_expr(),
            models.WorkflowRun.status == models.TaskStatus.RUNNING,
            models.WorkflowRun.deleted_at.is_(None),
        )
    )
    if tenant_id is not None:
        q = q.filter(models.WorkflowRun.tenant_id == tenant_id)
    return q.all()


def _age_seconds(anchor: datetime, now: datetime) -> float:
    """(now - anchor) in seconds, tolerating naive/aware mixes (Postgres
    returns aware timestamptz; SQLite/callers may pass naive)."""
    if anchor.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elif anchor.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - anchor).total_seconds()


def _default_cancel(db: Session, run_id: int) -> bool:
    """Default timeout action: the same conditional flip the user-cancel
    endpoint uses (crud/workflows_domain.conditional_cancel_run). Returns
    True only if the row actually changed (lost races = False = idempotent)."""
    from crud import workflows_domain as workflows_domain_crud
    res = workflows_domain_crud.conditional_cancel_run(
        db,
        run_id,
        completed_at=datetime.now(),
        error_message="Cancelled automatically: paused run exceeded "
                      "workflows.paused_run_timeout_s (sz-2.9)",
    )
    db.commit()
    return res.rowcount > 0


def can_pause(db: Session, tenant_id: int) -> dict:
    """{"allowed": bool, "paused": n, "quota": q} for one tenant.

    Counts RUNNING runs the tenant is holding — run-level paused=True OR a
    non-empty paused_nodes JSONB array, i.e. node-level holds too (B99; see
    _is_paused_expr). Soft-delete excluded. allowed =
    paused < quota; the quota is tenant-resolved (tenant row > platform >
    default 10)."""
    quota = psr.get_setting(QUOTA_KEY, db=db, tenant_id=tenant_id)
    paused = len(_paused_runs(db, tenant_id))
    # 0 = UNLIMITED sentinel (CE default; operator-set on Paid too): the
    # `paused < quota` compare would refuse at 0, so guard it. Finite
    # quotas behave byte-for-byte as before.
    return {"allowed": quota == 0 or paused < quota, "paused": paused, "quota": quota}


def _write_parameters_key(run: models.WorkflowRun, key: str, value: str) -> None:
    """Set parameters[key] = value without clobbering other keys, and flag
    the JSONB column modified (SQLAlchemy does not auto-detect in-place
    mutation of a JSON blob)."""
    params = dict(run.parameters or {})
    params[key] = value
    run.parameters = params
    flag_modified(run, "parameters")


def _pop_parameters_keys(run: models.WorkflowRun, keys: List[str]) -> bool:
    """Remove `keys` from parameters if present; returns True if anything
    changed. Leaves every other key untouched."""
    params = dict(run.parameters or {})
    changed = False
    for key in keys:
        if key in params:
            del params[key]
            changed = True
    if changed:
        run.parameters = params
        flag_modified(run, "parameters")
    return changed


def clear_paused_markers(db: Session, run: models.WorkflowRun) -> bool:
    """Clear the paused_seen_at / timeout-cancelled markers from a run's
    parameters. Intended to be called by the (future) resume endpoint when
    `paused` flips back to False, so a later re-pause starts a fresh timeout
    window instead of reusing a stale paused_seen_at. Also defensively
    skipped/ignored by the sweep itself for any run observed with
    paused=False. Commits if something changed. Returns True if a marker was
    actually cleared."""
    if _pop_parameters_keys(run, [SEEN_AT_MARKER, CANCEL_MARKER]):
        db.commit()
        return True
    return False


def sweep_expired_paused_runs(db: Session, *, now: Optional[datetime] = None,
                              cancel_fn: Optional[Callable[..., bool]] = None) -> List[int]:
    """Cancel paused RUNNING runs older than their tenant's timeout.

    Age anchor is `parameters["paused_seen_at"]`, NOT started_at (a run can
    run for hours before being paused; started_at would make it look already
    expired on the very next sweep). The first sweep that observes a given
    paused run writes paused_seen_at = now and does NOT cancel it; only a
    later sweep, once (now - paused_seen_at) >= timeout_s, cancels. This
    means the effective timeout is accurate to one sweep interval, not to
    the second — documented in the module docstring.

    Every tenant is covered; each run's timeout is resolved with ITS OWN
    tenant_id. cancel_fn(db, run_id) -> truthy defaults to the conditional
    cancel flip. One audit row per cancel (ids only). Never raises per-run:
    a failure is logged and the sweep continues. Idempotent: a cancelled run
    is no longer RUNNING+paused, so a second sweep returns []. If a run's
    `paused` is False its marker is irrelevant here (it's excluded by
    _paused_runs); the (future) resume endpoint should call
    clear_paused_markers() so a later re-pause starts a fresh window.
    Returns the list of run ids cancelled this pass.
    """
    now = now or datetime.now(timezone.utc)
    cancel = cancel_fn or _default_cancel
    cancelled: List[int] = []
    for run in _paused_runs(db, None):
        if not _node_held(run):
            # belt-and-suspenders; _paused_runs already filters this. Same
            # predicate as the SQL side (B99): a run with paused False AND an
            # empty paused_nodes is not being held — skip it.
            continue
        params = run.parameters or {}
        if params.get(CANCEL_MARKER):
            continue  # already cancelled by a previous sweep (idempotent)
        seen_at_raw = params.get(SEEN_AT_MARKER)
        if not seen_at_raw:
            try:
                _write_parameters_key(run, SEEN_AT_MARKER, now.isoformat())
                db.commit()
            except Exception:
                logger.warning("sz-2.9: failed to write paused_seen_at for "
                               "run_id=%s (logged, continuing)", run.id)
            continue  # first observation — never cancel on the same sweep
        try:
            seen_at = datetime.fromisoformat(seen_at_raw)
        except (TypeError, ValueError):
            logger.warning("sz-2.9: unparsable paused_seen_at for run_id=%s "
                           "(logged, continuing)", run.id)
            continue
        timeout_s = psr.get_setting(TIMEOUT_KEY, db=db, tenant_id=run.tenant_id)
        try:
            if _age_seconds(seen_at, now) < timeout_s:
                continue
            if not cancel(db, run.id):
                continue
            _write_parameters_key(run, CANCEL_MARKER,
                                  datetime.now(timezone.utc).isoformat())
            db.commit()
            if _data_classification is not None:
                _data_classification.record_audit(
                    db,
                    tenant_id=run.tenant_id,
                    user=None,
                    event_name=AUDIT_EVENT,
                    details={"workflow_run_id": run.id,
                             "tenant_id": run.tenant_id,
                             "timeout_s": timeout_s},
                )
            cancelled.append(run.id)
        except Exception:
            logger.warning("sz-2.9: paused-run sweep failed for run_id=%s "
                           "(logged, continuing)", run.id)
    return cancelled
