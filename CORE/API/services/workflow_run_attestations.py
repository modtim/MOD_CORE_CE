"""Fortress 4.5 slice B1 — per-run compliance evidence writer/reader.

Append-only runtime evidence for ``models.WorkflowRunAttestation``: at claim
time ``record_dispatch`` writes one row PER compliance tag the run's
workflow carries (worker/region/storage actually used + whether each tag's
constraint was satisfied, reusing the existing per-tag dispatch decision in
``services/compliance_tag_dispatch_gate.py::check_dispatch`` — attestation
rules are never re-implemented here). At completion time
``record_completion`` writes one linked completion row per still-open
dispatch row for that task's node run.

This module is service-only: nothing here is wired into the claim/complete
routers yet (that is a later slice). Every public function is written to
NEVER raise into the caller — the claim/complete hot path must not break
because an audit side-table had a bad day. Only ``db.flush()`` is ever
called; the caller owns the commit. Each write is wrapped in its own
``db.begin_nested()`` SAVEPOINT so a failure here can never poison the
caller's outer transaction.
"""
from __future__ import annotations

from product_names import themed_logger
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError

import models
from utils import residency
from utils.paid_import import optional_paid

compliance_tag_dispatch_gate = optional_paid("services.compliance_tag_dispatch_gate")
compliance_tag_inheritance = optional_paid("services.compliance_tag_inheritance")

logger = themed_logger(__name__, 'workflows')

_COMPLETION_STATUSES = ("completed", "failed")


def _worker_isolation(worker: Any) -> Optional[str]:
    owner_type = getattr(worker, "owner_type", None)
    if owner_type is None:
        return None
    return getattr(owner_type, "value", str(owner_type))


def _worker_encrypted(worker: Any) -> Optional[bool]:
    specs = getattr(worker, "hardware_specs", None) or {}
    try:
        de = specs.get("disk_encryption") or {}
        status = de.get("status")
    except AttributeError:
        return None
    if status is None:
        return None
    return status == "encrypted"


def _active_storage_connection(db, tenant_id: int):
    try:
        return (
            db.query(models.StorageConnection)
            .filter(
                models.StorageConnection.tenant_id == tenant_id,
                models.StorageConnection.is_active.is_(True),
            )
            .first()
        )
    except SQLAlchemyError as e:  # noqa: BLE001
        logger.debug("workflow_run_attestations: storage lookup failed: %s",
                     type(e).__name__)
        return None


def _resolve_run_and_tags(db, task):
    """(run, workflow, tenant_id, tags) or None when there is nothing to
    attest — no resolvable workflow, or the workflow carries no compliance
    tags. Mirrors compliance_tag_dispatch_gate.gate_task_start's resolution
    so both see the same tags for the same task."""
    run_id = getattr(task, "workflow_run_id", None)
    if not run_id:
        return None
    run = db.query(models.WorkflowRun).filter(
        models.WorkflowRun.id == run_id).first()
    if not run or not getattr(run, "workflow_id", None):
        return None
    wf = db.query(models.Workflow).filter(
        models.Workflow.id == run.workflow_id).first()
    if not wf:
        return None
    if compliance_tag_dispatch_gate is None or compliance_tag_inheritance is None:
        # CE: compliance-tag modules absent from the carve — fail safe, no
        # attestation to write (never fabricate tags/dispatch evidence).
        return None
    try:
        tags = [t for t in compliance_tag_inheritance.normalize_tags(
            wf.compliance_tags) if t != "none"]
    except ValueError:
        return None
    if not tags:
        return None
    tenant_id = (getattr(wf, "tenant_id", None) or getattr(run, "tenant_id",
                 None) or getattr(task, "tenant_id", None))
    if tenant_id is None:
        return None
    return run, wf, tenant_id, tags


def record_dispatch(
    db, *, task, worker, node_run=None,
) -> Optional["models.WorkflowRunAttestation"]:
    """One row per compliance tag the run's workflow carries. ``None`` fast
    (no extra queries past the tag lookup) when the workflow is untagged.
    Never raises — an internal failure is logged and swallowed; the caller's
    transaction stays usable. Returns the LAST row written (callers that
    care about a specific tag should use ``get_run_attestations``)."""
    try:
        resolved = _resolve_run_and_tags(db, task)
        if resolved is None:
            return None
        run, _wf, tenant_id, tags = resolved

        node_run_id = (node_run.id if node_run is not None
                       else getattr(task, "node_run_id", None))
        worker_name = getattr(worker, "worker_name", None)
        worker_zone = residency.worker_residency_zone(worker)
        worker_isolation = _worker_isolation(worker)
        worker_encrypted = _worker_encrypted(worker)
        storage_conn = _active_storage_connection(db, tenant_id)
        storage_conn_id = str(storage_conn.id) if storage_conn else None
        storage_region = getattr(storage_conn, "region", None)
        storage_encrypted = (getattr(storage_conn, "secure", None)
                             if storage_conn else None)

        # 4.5's whole point is per-tag worker + region + storage evaluation:
        # worker_zone is already the same "eu"/"us"/"unknown" vocabulary
        # fortress 4.2 region attestations use (utils/residency.zone_from_tags
        # docstring: 'region:eu' -> 'eu'), and storage_conn_id is already the
        # StorageConnection.id fortress 4.2 storage_connection attestations
        # key on — both are valid gate subject ids, not just captured
        # evidence fields, so pass them through to be gated too.
        gate = compliance_tag_dispatch_gate.check_dispatch(
            db, tenant_id=tenant_id, tags=tags, worker_id=worker_name,
            region=worker_zone, storage_connection_id=storage_conn_id,
        )
        mode = gate.get("mode") or compliance_tag_dispatch_gate.MODE_DEFAULT
        violations_by_tag: Dict[str, List[str]] = {}
        for v in gate.get("violations") or []:
            tag = v.get("tag")
            if tag is None:
                continue
            violations_by_tag.setdefault(tag, []).append(
                f"{v.get('subject_type')}:{v.get('reason')}")

        rows: List[models.WorkflowRunAttestation] = []
        with db.begin_nested():
            for tag in tags:
                details = violations_by_tag.get(tag)
                row = models.WorkflowRunAttestation(
                    tenant_id=tenant_id,
                    workflow_run_id=run.id,
                    node_run_id=node_run_id,
                    compliance_tag=tag,
                    dispatch_mode=mode,
                    claimed_worker_name=worker_name,
                    claimed_worker_region=worker_zone,
                    claimed_worker_isolation=worker_isolation,
                    claimed_worker_encrypted=worker_encrypted,
                    storage_connection_id=storage_conn_id,
                    storage_region=storage_region,
                    storage_encrypted=storage_encrypted,
                    tag_satisfied=details is None,
                    tag_violation_detail=(", ".join(details)
                                          if details else None),
                    status="dispatched",
                )
                db.add(row)
                rows.append(row)
            db.flush()
        return rows[-1] if rows else None
    except Exception as e:  # noqa: BLE001 — must never raise into the
        # claim path; a bad evidence write is not a reason to fail a claim.
        logger.warning("workflow_run_attestations.record_dispatch failed: %s",
                       type(e).__name__)
        return None


def record_completion(
    db, *, task, worker, status: str,
) -> List["models.WorkflowRunAttestation"]:
    """One linked completion row per still-open dispatch row for this
    task's node run. Idempotent: a dispatch that already has a completion
    (the partial unique ``uq_wra_one_completion_per_dispatch``) is skipped —
    no IntegrityError ever escapes. Never raises; ``[]`` on any failure."""
    if status not in _COMPLETION_STATUSES:
        logger.warning("workflow_run_attestations.record_completion: bad "
                       "status %r", status)
        return []
    try:
        node_run_id = getattr(task, "node_run_id", None)
        run_id = getattr(task, "workflow_run_id", None)
        if not run_id or node_run_id is None:
            return []
        dispatch_rows = (
            db.query(models.WorkflowRunAttestation)
            .filter(
                models.WorkflowRunAttestation.workflow_run_id == run_id,
                models.WorkflowRunAttestation.node_run_id == node_run_id,
                models.WorkflowRunAttestation.status == "dispatched",
            )
            .all()
        )
        if not dispatch_rows:
            return []

        already_done = {
            r[0]
            for r in db.query(models.WorkflowRunAttestation.referenced_dispatch_id)
            .filter(
                models.WorkflowRunAttestation.referenced_dispatch_id.in_(
                    [d.id for d in dispatch_rows]),
            )
            .all()
        }
        worker_name = getattr(worker, "worker_name", None)
        worker_zone = residency.worker_residency_zone(worker)

        written: List[models.WorkflowRunAttestation] = []
        for dispatch in dispatch_rows:
            if dispatch.id in already_done:
                continue
            try:
                with db.begin_nested():
                    row = models.WorkflowRunAttestation(
                        tenant_id=dispatch.tenant_id,
                        workflow_run_id=dispatch.workflow_run_id,
                        node_run_id=dispatch.node_run_id,
                        compliance_tag=dispatch.compliance_tag,
                        dispatch_mode=dispatch.dispatch_mode,
                        tag_satisfied=dispatch.tag_satisfied,
                        tag_violation_detail=dispatch.tag_violation_detail,
                        completed_worker_name=worker_name,
                        completed_region=worker_zone,
                        completed_storage=dispatch.storage_connection_id,
                        status=status,
                        referenced_dispatch_id=dispatch.id,
                    )
                    db.add(row)
                    db.flush()
            except SQLAlchemyError as e:  # noqa: BLE001 — a race lost to a
                # concurrent completion writer is not a failure, just a skip.
                logger.debug(
                    "workflow_run_attestations.record_completion: dispatch "
                    "%s already completed concurrently: %s", dispatch.id,
                    type(e).__name__)
                continue
            written.append(row)
        return written
    except Exception as e:  # noqa: BLE001 — must never raise into the
        # complete path.
        logger.warning(
            "workflow_run_attestations.record_completion failed: %s",
            type(e).__name__)
        return []


def get_run_attestations(
    db, *, tenant_id: int, run_id: Optional[int] = None,
    tag: Optional[str] = None, worker: Optional[str] = None,
    satisfied: Optional[bool] = None, since: Optional[datetime] = None,
    until: Optional[datetime] = None, limit: int = 500,
) -> List[Dict[str, Any]]:
    """Tenant-scoped audit read. Never raises — ``[]`` on any failure."""
    try:
        q = db.query(models.WorkflowRunAttestation).filter(
            models.WorkflowRunAttestation.tenant_id == tenant_id)
        if run_id is not None:
            q = q.filter(models.WorkflowRunAttestation.workflow_run_id == run_id)
        if tag is not None:
            q = q.filter(models.WorkflowRunAttestation.compliance_tag == tag)
        if worker is not None:
            q = q.filter(models.WorkflowRunAttestation.claimed_worker_name
                        == worker)
        if satisfied is not None:
            q = q.filter(models.WorkflowRunAttestation.tag_satisfied
                        == satisfied)
        if since is not None:
            q = q.filter(models.WorkflowRunAttestation.created_at >= since)
        if until is not None:
            q = q.filter(models.WorkflowRunAttestation.created_at <= until)
        rows = (q.order_by(models.WorkflowRunAttestation.created_at.desc())
                .limit(limit).all())
        return [_row_dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001 — an audit read must degrade,
        # not 500 the officer's console.
        logger.warning("workflow_run_attestations.get_run_attestations "
                       "failed: %s", type(e).__name__)
        return []


def _row_dict(row: "models.WorkflowRunAttestation") -> Dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "workflow_run_id": row.workflow_run_id,
        "node_run_id": row.node_run_id,
        "compliance_tag": row.compliance_tag,
        "dispatch_mode": row.dispatch_mode,
        "claimed_worker_name": row.claimed_worker_name,
        "claimed_worker_region": row.claimed_worker_region,
        "claimed_worker_isolation": row.claimed_worker_isolation,
        "claimed_worker_encrypted": row.claimed_worker_encrypted,
        "storage_connection_id": row.storage_connection_id,
        "storage_region": row.storage_region,
        "storage_encrypted": row.storage_encrypted,
        "tag_satisfied": row.tag_satisfied,
        "tag_violation_detail": row.tag_violation_detail,
        "completed_worker_name": row.completed_worker_name,
        "completed_region": row.completed_region,
        "completed_storage": row.completed_storage,
        "status": row.status,
        "referenced_dispatch_id": row.referenced_dispatch_id,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }
