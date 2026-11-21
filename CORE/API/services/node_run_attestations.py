"""node_run_attestations — ingest & persist worker per-task attestations.

Called from ``routers.worker_tasks.report_task_complete`` on the success
path.  Best-effort: a failure here must NEVER roll back or alter the task
completion that already happened.  The router commits the transaction; we
only flush.
"""
from __future__ import annotations

import importlib
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import models

logger = logging.getLogger(__name__)


def _module_available(module_name: str) -> bool:
    """CE build check — the CE build strips whole service modules out of the
    tree (manifest.yaml `deny`).  Guard lazy `from services.x import y`
    inside a broad `except` so we never log a traceback at boot."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False

# fortress-4.5 S: attested_region / attested_storage columns are live
# (20261013f_fortress_45_region_storage.py applied before this module's
# restart) — a single INSERT shape, no pre-migration fallback needed.
_ATTESTED_CLAIM_MAX_LEN = 128

_INSERT_SQL = """
    INSERT INTO node_run_attestations
        (tenant_id, workflow_run_id, node_run_id, task_id,
         worker_name, alg, key_id, public_key_b64,
         payload_sha256, signature_b64, contribution, signed_at,
         attested_region, attested_storage)
    VALUES
        (:tenant_id, :workflow_run_id, :node_run_id, :task_id,
         :worker_name, :alg, :key_id, :public_key_b64,
         :payload_sha256, :signature_b64, :contribution,
         :signed_at, :attested_region, :attested_storage)
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_worker_attestation(
    db,
    task,
    worker_name: str,
    worker_signature: Optional[Dict[str, Any]],
    worker_contribution: Optional[Dict[str, Any]],
) -> bool:
    """Ingest a worker attestation into ``node_run_attestations``.

    Returns ``True`` when a row was recorded (or already existed via
    idempotent replay).  Returns ``False`` and inserts nothing when:

    * either ``worker_signature`` or ``worker_contribution`` is missing / not a
      dict;
    * ``run_proof_bundle.verify_worker_signature({**worker_signature,
      "contribution": worker_contribution})`` is ``False``;
    * contribution ``task_id`` != ``task.id``;
    * contribution ``node_run_id`` / ``workflow_run_id`` don't match the
      task row;
    * contribution ``worker_name`` != the authenticated ``worker_name``.

    Uses a SAVEPOINT so a duplicate ``(node_run_id, task_id)`` is treated
    as already-recorded → returns ``True``.  Flush only (the router
    commits the outer transaction).  Never raises.
    """
    try:
        # --- shape checks (fast-fail, no I/O) ---
        if not isinstance(worker_signature, dict):
            return False
        if not isinstance(worker_contribution, dict):
            return False

        # --- cross-validate contribution fields against the task row ---
        if not _check_task_id(task, worker_contribution):
            return False
        if not _check_run_ids(task, worker_contribution):
            return False
        if worker_contribution.get("worker_name") != worker_name:
            return False

        # --- merge contribution into signature for verify_worker_signature ---
        sig_with_contribution = {**worker_signature, "contribution": worker_contribution}

        # --- verify the signature ---
        if not _module_available("services.run_proof_bundle"):
            return False
        from services import run_proof_bundle as rpb
        if not rpb.verify_worker_signature(sig_with_contribution):
            return False

        # --- extract fields ---
        fields = _extract_sig_fields(worker_signature)
        if fields is None:
            return False

        alg, key_id, public_key_b64, payload_sha256, signature_b64, signed_at_str = fields

        # --- idempotent insert with SAVEPOINT ---
        _insert_attestation(
            db,
            tenant_id=task.tenant_id,
            workflow_run_id=task.workflow_run_id,
            node_run_id=task.node_run_id,
            task_id=task.id,
            worker_name=worker_name,
            alg=alg,
            key_id=key_id,
            public_key_b64=public_key_b64,
            payload_sha256=payload_sha256,
            signature_b64=signature_b64,
            contribution=worker_contribution,
            signed_at=signed_at_str,
        )
        return True

    except Exception as e:  # noqa: BLE001 — attestation never fails completion
        logger.exception(
            "node_run_attestation ingest failed for task %d: %s",
            getattr(task, "id", "?"),
            type(e).__name__,
        )
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sig_fields(sig: Dict[str, Any]) -> tuple:
    """Pull the WorkerProofSignature fields, or None if shape is wrong."""
    alg = sig.get("alg")
    key_id = sig.get("key_id")
    pub = sig.get("public_key_b64")
    payload_sha = sig.get("payload_sha256")
    signature = sig.get("signature_b64")
    signed_at = sig.get("signed_at")
    if not all(isinstance(v, str) for v in (alg, key_id, pub,
                                              payload_sha, signature,
                                              signed_at)):
        return None
    return alg, key_id, pub, payload_sha, signature, signed_at


def _check_task_id(task, contribution: Dict[str, Any]) -> bool:
    """Return False (silently) when contribution.task_id != task.id."""
    contrib_task_id = contribution.get("task_id")
    if contrib_task_id is not None and contrib_task_id != task.id:
        return False
    return True


def _check_run_ids(task, contribution: Dict[str, Any]) -> bool:
    """Return False when node_run_id / workflow_run_id don't match the task."""
    if not hasattr(task, "node_run_id"):
        return False
    if not hasattr(task, "workflow_run_id"):
        return False

    contrib_node = contribution.get("node_run_id")
    if contrib_node is not None and contrib_node != task.node_run_id:
        return False

    contrib_workflow = contribution.get("workflow_run_id")
    if contrib_workflow is not None and contrib_workflow != task.workflow_run_id:
        return False

    return True


def _insert_attestation(
    db,
    tenant_id: int,
    workflow_run_id: Optional[int],
    node_run_id: Optional[int],
    task_id: int,
    worker_name: str,
    alg: str,
    key_id: str,
    public_key_b64: str,
    payload_sha256: str,
    signature_b64: str,
    contribution: Dict[str, Any],
    signed_at: str,
) -> None:
    """INSERT row inside a SAVEPOINT; swallow IntegrityError as idempotent.

    fortress-4.5 S: the worker's region / storage claims are lifted out of the
    free-form ``contribution`` JSONB into their own columns (``attested_region``
    / ``attested_storage``, added by 20261013f_fortress_45_region_storage.py) so
    the 4.4 compliance-tag enforcement can compare them without ever parsing
    JSONB.  Absent claim → NULL, which enforcement reads as "not attested"
    (never "allowed").
    """
    region = contribution.get("region")
    storage = contribution.get("storage")
    if not isinstance(region, str):
        region = None
    else:
        region = region[:_ATTESTED_CLAIM_MAX_LEN]
    if not isinstance(storage, str):
        storage = None
    else:
        storage = storage[:_ATTESTED_CLAIM_MAX_LEN]
    params = {
        "tenant_id": tenant_id,
        "workflow_run_id": workflow_run_id,
        "node_run_id": node_run_id,
        "task_id": task_id,
        "worker_name": worker_name,
        "alg": alg,
        "key_id": key_id,
        "public_key_b64": public_key_b64,
        "payload_sha256": payload_sha256,
        "signature_b64": signature_b64,
        "contribution": json.dumps(contribution, sort_keys=True,
                                   ensure_ascii=False),
        "signed_at": signed_at,
        "attested_region": region,
        "attested_storage": storage,
    }
    try:
        with db.begin_nested():
            db.execute(
                text(_INSERT_SQL),
                params,
            )
            db.flush()
    except IntegrityError:
        # UNIQUE violation on (node_run_id, task_id) → already recorded.
        pass


# ---------------------------------------------------------------------------
# Read side (fortress-4.5 read slice) — per-compliance-tag attestations
# ---------------------------------------------------------------------------


def attestations_for_compliance_tag(
    db, tenant_id: int, tag: str, limit: int = 200,
) -> List[Dict[str, Any]]:
    """Tenant-scoped attestations whose workflow carries ``tag``.

    ``Workflow.compliance_tags`` is a plain Python list once loaded; per
    the established convention (``compliance_tag_dispatch_gate.py``) tag
    membership is checked in Python, never via SQL ``ANY(array)``. We
    therefore over-fetch (``limit * 5`` rows, newest first) before the
    Python-side filter, then truncate to ``limit``.
    """
    rows = (
        db.query(models.NodeRunAttestation, models.Workflow.compliance_tags)
        .join(models.WorkflowRun,
              models.NodeRunAttestation.workflow_run_id == models.WorkflowRun.id)
        .join(models.Workflow,
              models.WorkflowRun.workflow_id == models.Workflow.id)
        .filter(models.NodeRunAttestation.tenant_id == tenant_id)
        .order_by(models.NodeRunAttestation.signed_at.desc())
        .limit(limit * 5)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for attestation, compliance_tags in rows:
        if tag not in (compliance_tags or []):
            continue
        out.append({
            "node_run_id": attestation.node_run_id,
            "workflow_run_id": attestation.workflow_run_id,
            "worker_name": attestation.worker_name,
            "attested_region": attestation.attested_region,
            "attested_storage": attestation.attested_storage,
            "signed_at": attestation.signed_at.isoformat()
                        if attestation.signed_at else None,
        })
        if len(out) >= limit:
            break
    return out
