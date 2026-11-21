"""Record a worker's per-task egress report (GB.EGR.11a).

The Go worker attaches an ``egress`` object to its complete/failed report:

    {"mode": "proxy" | "cilium" | "unenforced" | "advisory" | ...,
     "allowed_hosts": ["api.example.com:443", ...],
     "denied": [{"host": str, "port": int, "reason": str, "count": int}, ...],
     "denied_truncated": bool}

It is stored on ``ApiTaskQueue.result_payload["egress"]`` (the run detail log
reads it from there), and two audit facts are written when relevant:

- ``EGRESS_UNENFORCED`` — the node declared an allowlist but this worker or
  cluster could not enforce it (``mode`` unenforced/advisory).
- ``EGRESS_DENIED`` — the proxy blocked at least one connection.

``NodeRun`` has no free-form payload column, so the node run is used only for
context (its workflow run id); the task row is the single source of truth.

Old workers send nothing, so ``None`` / non-dict input is a no-op. Malformed
input never raises: this runs inside the worker-report transaction and must
not turn a completed task into a 500. The caller owns the transaction — this
only ``db.add``s.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

MAX_DENIED = 50
UNENFORCED_MODES = ("unenforced", "advisory")


def _clean_denied(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict) or not d.get("host"):
            continue
        try:
            port = int(d.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            count = max(int(d.get("count") or 1), 1)
        except (TypeError, ValueError):
            count = 1
        out.append({
            "host": str(d["host"])[:255],
            "port": port,
            "reason": str(d.get("reason") or "")[:255],
            "count": count,
        })
    return out


def _clean(egress: Dict[str, Any]) -> Dict[str, Any]:
    hosts = egress.get("allowed_hosts")
    allowed = [str(h)[:255] for h in hosts if h] if isinstance(hosts, list) else []
    denied = _clean_denied(egress.get("denied"))
    truncated = bool(egress.get("denied_truncated"))
    if len(denied) > MAX_DENIED:
        denied = denied[:MAX_DENIED]
        truncated = True
    return {
        "mode": str(egress.get("mode") or "")[:32],
        "allowed_hosts": allowed,
        "denied": denied,
        "denied_truncated": truncated,
    }


def record_egress_report(db: Session, task: "models.ApiTaskQueue",
                         node_run: Optional["models.NodeRun"],
                         egress: Optional[Dict[str, Any]]) -> None:
    """Stamp ``egress`` onto the task's result payload and write the audit
    rows it implies. See the module docstring for the shape."""
    if not isinstance(egress, dict):
        return
    try:
        clean = _clean(egress)
    except Exception:
        logger.warning("egress report for task %s is malformed; ignored",
                       getattr(task, "id", None), exc_info=True)
        return

    # A NEW dict so SQLAlchemy's JSONB change tracking sees the assignment.
    task.result_payload = {**(task.result_payload or {}), "egress": clean}

    payload = task.task_payload if isinstance(task.task_payload, dict) else {}
    run_id = getattr(task, "workflow_run_id", None)
    if run_id is None and node_run is not None:
        run_id = getattr(node_run, "workflow_run_id", None)
    context = {
        "task_id": getattr(task, "id", None),
        "workflow_run_id": run_id,
        "node_run_id": getattr(task, "node_run_id", None)
                       or getattr(node_run, "id", None),
        "node_id": getattr(task, "node_id", None),
        "node_kind": payload.get("node_kind"),
        "worker": getattr(task, "claimed_by_worker", None)
                  or getattr(task, "assigned_to_worker", None),
        "mode": clean["mode"],
    }

    if clean["mode"] in UNENFORCED_MODES:
        db.add(models.AuditEvent(
            tenant_id=task.tenant_id,
            user_id=None,
            event_name="EGRESS_UNENFORCED",
            details={**context, "allowed_hosts": clean["allowed_hosts"]},
        ))
    if clean["denied"]:
        db.add(models.AuditEvent(
            tenant_id=task.tenant_id,
            user_id=None,
            event_name="EGRESS_DENIED",
            details={**context, "denied": clean["denied"],
                     "denied_truncated": clean["denied_truncated"]},
        ))
