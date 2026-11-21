"""Business logic for the `task_queue_scheduler` node_runs sub-module
(Gate 2b migration of `routers/node_runs_domains/task_queue_scheduler.py`
— openspec: stack-training-and-prewarm, D9).

Companion to `crud/node_runs_task_queue.py` (the ONLY place this slice's
`db.query` lives). The router stays HTTP-only: parse input, call a
function here, map a `ServiceError` to `HTTPException`, shape the
response.

Extracted verbatim from the router during the Gate-2b migration —
behaviour-preserving MOVE. Every function below is the same logic that
used to live inline in the router handler of the same name; only the
data access now goes through `crud.node_runs_task_queue` and the
"not found" branches raise `NotFoundError` instead of a bare
`HTTPException`.

404-vs-403 (SHARED RULES #5): CRUD fetches stay tenant-UNSCOPED;
`_enforce_user_run_view` (from `routers.node_runs_domains._shared`,
pre-existing and NOT owned by this migration) applies the tenant/view
gate here, before any data is returned, so a legitimate 403 doesn't
collapse into a 404. That helper raises `HTTPException(404, ...)`
directly (it predates this migration and is shared with sibling
sub-modules) — it is allowed to propagate through this service
unchanged; FastAPI handles an `HTTPException` raised anywhere in the
call stack the same way.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

import models
from crud import node_runs_task_queue as crud
from services._base import NotFoundError


# =============================================================================
# get_node_run_resources_for_workflow_run_service
# =============================================================================


def get_run_resource_summary(db: Session, run_id: int, principal: Any) -> List[Dict[str, Any]]:
    """Per-node-run resource usage for a workflow run (integrals from
    `task_resource_aggregate` + peaks from `task_resource_samples`).
    Materializes any missing aggregate rows on-demand before reading."""
    from routers.node_runs_domains._shared import _enforce_user_run_view
    from utils.run_task_auth import pin_run_task_tenant

    db_workflow_run = crud.get_workflow_run(db, run_id)
    if not db_workflow_run:
        raise NotFoundError("Workflow run not found")

    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=db_workflow_run.tenant_id, run_id=run_id)
    _enforce_user_run_view(db, principal, db_workflow_run)

    # The periodic aggregator runs hourly at :15. Export containers fire
    # right after the workflow completes, so the aggregate rows may not
    # exist yet — materialize on-demand from samples so the JSON has
    # real integrals instead of null. resource_monitor is a Paid module:
    # on CE skip the on-demand materialization (null fields are fine).
    from utils.paid_import import optional_paid
    _resource_monitor = optional_paid("services.resource_monitor")
    if _resource_monitor is not None:
        pending_task_ids = crud.get_pending_resource_aggregate_task_ids(db, run_id)
        for tid in pending_task_ids:
            try:
                _resource_monitor.aggregate_task(db, tid)
            except Exception:
                # Best-effort; null fields are fine if aggregation fails.
                db.rollback()
        if pending_task_ids:
            db.commit()

    rows = crud.get_node_run_resource_rows(db, run_id)
    if not rows:
        return []

    task_ids = [r.task_id for r in rows if r.task_id is not None]
    peaks_by_task = {}
    if task_ids:
        peak_rows = crud.get_resource_peaks_for_tasks(db, task_ids)
        peaks_by_task = {p.task_id: p for p in peak_rows}

    wn_ids = list({r.workflow_node_id for r in rows})
    kind_by_wn_id: Dict[int, Any] = {}
    if wn_ids:
        kind_by_wn_id = crud.get_node_kinds_for_workflow_nodes(db, wn_ids)

    out = []
    for r in rows:
        peaks = peaks_by_task.get(r.task_id)
        out.append({
            "node_run_id": r.node_run_id,
            "task_id": r.task_id,
            "graph_node_id": r.graph_node_id,
            "node_kind": kind_by_wn_id.get(r.workflow_node_id),
            "status": r.status.value.lower() if r.status else None,
            "started_at": r.started_at.isoformat() + "Z" if r.started_at else None,
            "completed_at": r.completed_at.isoformat() + "Z" if r.completed_at else None,
            "worker": r.assigned_to_worker,
            "wall_clock_seconds": r.wall_clock_seconds,
            "cpu_core_seconds": r.cpu_core_seconds,
            "ram_gb_seconds": r.ram_gb_seconds,
            "gpu_seconds": r.gpu_seconds,
            "vram_gb_seconds": r.vram_gb_seconds,
            "disk_read_mb": r.disk_read_mb,
            "disk_write_mb": r.disk_write_mb,
            "peak_cpu_percent": peaks.peak_cpu_percent if peaks else None,
            "peak_ram_mb": peaks.peak_ram_mb if peaks else None,
            "peak_vram_mb": peaks.peak_vram_mb if peaks else None,
            "peak_gpu_util_pct": peaks.peak_gpu_util_pct if peaks else None,
            "peak_gpu_temp_c": peaks.peak_gpu_temp_c if peaks else None,
            "sample_count": peaks.sample_count if peaks else 0,
        })
    return out


# =============================================================================
# get_node_run_by_node_id / update_node_run_by_node_id
# =============================================================================


def _resolve_node_run(
    db: Session,
    workflow_run_id: int,
    node_id: str,
    principal: Any,
    *,
    with_workflow_node: bool,
) -> models.NodeRun:
    """Shared resolution for the two graph-node-id-scoped endpoints: fetch
    the WorkflowRun, enforce the view gate, resolve the WorkflowNode by
    `graph_node_id`, then the NodeRun for that (run, node) pair."""
    from routers.node_runs_domains._shared import _enforce_user_run_view
    from utils.run_task_auth import pin_run_task_tenant

    workflow_run = crud.get_workflow_run(db, workflow_run_id)
    if not workflow_run:
        raise NotFoundError("Workflow run not found")

    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=workflow_run.tenant_id, run_id=workflow_run_id)
    _enforce_user_run_view(db, principal, workflow_run)

    workflow_node = crud.get_workflow_node_by_graph_id(db, workflow_run.workflow_id, node_id)
    if not workflow_node:
        raise NotFoundError(f"Workflow node '{node_id}' not found")

    node_run = crud.get_node_run_for_workflow_node(
        db, workflow_run_id, workflow_node.id, with_workflow_node=with_workflow_node
    )
    if not node_run:
        raise NotFoundError(f"Node run for node '{node_id}' not found")

    return node_run


def get_node_run_by_node_id(
    db: Session, workflow_run_id: int, node_id: str, principal: Any
) -> models.NodeRun:
    """Get a node run by workflow_run_id and graph_node_id. Service account
    endpoint for use by Traffic Cop DAG to get node_run_id."""
    # GY.V4b — redact the text fields on READ only (inputs / outputs /
    # error_message). The stored row is never mutated and the off profile hands
    # back the ORM row as queried, so this is byte-identical to today. The
    # sibling PATCH path is a write, not a display, and is left untouched.
    from utils.pii_redaction_surfaces import redact_node_run_view
    return redact_node_run_view(_resolve_node_run(
        db, workflow_run_id, node_id, principal, with_workflow_node=True))


def update_node_run_by_node_id(
    db: Session,
    workflow_run_id: int,
    node_id: str,
    principal: Any,
    *,
    status: models.TaskStatus,
    inputs: Any,
    outputs: Any,
    error_message: Any,
) -> models.NodeRun:
    """Update a node run by workflow_run_id and graph_node_id. Service
    account endpoint for use by Traffic Cop DAG."""
    from crud.node_run_cud import update_node_run_status

    node_run = _resolve_node_run(db, workflow_run_id, node_id, principal, with_workflow_node=False)
    return update_node_run_status(
        db,
        db_node_run=node_run,
        status=status,
        inputs=inputs,
        outputs=outputs,
        error_message=error_message,
    )
