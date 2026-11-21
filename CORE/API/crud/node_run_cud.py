"""Node Run CRUD operations - Node run management within workflows."""
from typing import Optional, List, Dict, Any, Sequence

from sqlalchemy import desc, asc
from sqlalchemy.orm import Session, joinedload, selectinload
from datetime import datetime

import models, schemas


def get_node_runs_for_workflow_run(db: Session, workflow_run_id: int) -> List[models.NodeRun]:
    """Retrieve all node runs for a given workflow run."""
    return db.query(models.NodeRun).filter(models.NodeRun.workflow_run_id == workflow_run_id).all()


def get_node_run_by_id(
    db: Session,
    node_run_id: int,
    tenant_id: int
) -> Optional[models.NodeRun]:
    """Retrieve a single node run by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.NodeRun)
        .join(models.WorkflowRun, models.NodeRun.workflow_run_id == models.WorkflowRun.id)
        .join(models.Workflow, models.WorkflowRun.workflow_id == models.Workflow.id)
        .filter(
            models.NodeRun.id == node_run_id,
            models.Workflow.tenant_id == tenant_id
        )
        .first()
    )


def update_node_run_status(
    db: Session,
    db_node_run: models.NodeRun,
    status: models.TaskStatus,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> models.NodeRun:
    """Update the status, inputs, and outputs of a node run.

    FX.117 p3a — the worker's text is masked for the run's exact secret VALUES
    (services.secret_masking) before it is stored, so a node that echoes an
    injected tenant secret into its stdout/output/error never persists it here
    (and the API therefore never serves it). Gated by the
    `secrets.mask_values_in_logs` platform setting (default True)."""
    db_node_run.status = status
    _masked_inputs, _masked_outputs, _masked_error = _fx117_mask_node_run_content(
        db, db_node_run, inputs, outputs, error_message)
    if inputs is not None:
        db_node_run.inputs = clamp_node_run_text(_masked_inputs)
    if outputs is not None:
        db_node_run.outputs = clamp_node_run_text(_masked_outputs)
    if error_message is not None:
        db_node_run.error_message = clamp_node_run_text(_masked_error)
    if status == models.TaskStatus.RUNNING:
        db_node_run.started_at = datetime.utcnow()
    elif status in [models.TaskStatus.SUCCESS, models.TaskStatus.FAILURE, models.TaskStatus.COMPLETED, models.TaskStatus.FAILED]:
        db_node_run.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(db_node_run)
    return db_node_run


def get_workflow_runs(
    db: Session,
    workflow_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "started_at",
    order: str = "desc",
    include_details: bool = False,
    purposes_exclude: Optional[Sequence[str]] = None,
):
    """List historical runs for a specific workflow.

    include_details=False (default) is the LIGHTWEIGHT list used by the hot poll
    paths (RunRail's 4s dropdown, the runs-grid): it returns only the per-run
    summary and does NOT load node_runs/run_files. This matters a lot — the old
    query joinedload'd TWO one-to-many collections (run_files AND node_runs) in
    one statement, which is a cartesian product (run_files × node_runs rows per
    run) wrapped in a LIMIT subquery. That was the single most expensive query
    on the box under a heavy run, and it was polled every 4s per client — the
    cause of "runs take 30s to show up" + the /runs 500s (connection held long
    enough to hit the idle-in-transaction timeout) + a big share of Postgres CPU.

    include_details=True loads them via selectinload (separate IN() queries — no
    cartesian, no N+1) for the few callers that actually need per-node data.
    """
    sort_column = getattr(models.WorkflowRun, sort_by, "started_at")
    order_by = desc(sort_column) if order == "desc" else asc(sort_column)

    opts = [
        joinedload(models.WorkflowRun.workflow),          # to-one: safe to join
        joinedload(models.WorkflowRun.workflow_version),  # to-one: safe to join
    ]
    if include_details:
        # selectinload, NOT joinedload: separate IN() queries avoid the
        # collection×collection cartesian blow-up the old code triggered.
        opts.append(selectinload(models.WorkflowRun.run_files))
        opts.append(
            selectinload(models.WorkflowRun.node_runs).joinedload(models.NodeRun.workflow_node)
        )

    query = (
        db.query(models.WorkflowRun).join(
            models.WorkflowVersion,
            models.WorkflowRun.workflow_version_id == models.WorkflowVersion.id
        ).options(*opts).filter(
            models.WorkflowVersion.workflow_id == workflow_id
        )
    )
    if purposes_exclude:
        query = query.filter(
            models.WorkflowRun.purpose.notin_(purposes_exclude)
        )
    runs = query.order_by(order_by).offset(offset).limit(limit).all()

    # GY.C7 — batched effective labels for these runs (one cog query total;
    # parameters are already loaded on each run row). Fail-OPEN: a lookup
    # hiccup must not 500 the hot RunRail poll — it just hides the chip.
    try:
        from services import data_classification as _gy_c7_dc
        _gy_c7_labels = _gy_c7_dc.run_labels_for_runs(db, runs)
    except Exception:
        _gy_c7_labels = {}

    # Build response dicts with relationship data
    return [
        {
            "id": run.id,
            "run_name": run.run_name,
            "status": run.status.value if run.status else "queued",
            "paused": bool(getattr(run, "paused", False)),
            "paused_nodes": list(getattr(run, "paused_nodes", None) or []),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "workflow_id": run.workflow_id,
            "workflow_version_id": run.workflow_version_id,
            "workflow_name": run.workflow.name if run.workflow else None,
            "version_number": run.workflow_version.version_number if run.workflow_version else None,
            "orchestrator_run_id": run.orchestrator_run_id,
            # GY.C7 — run's effective data-classification label (highest label
            # among its input cogs). Resolved in ONE batched query below.
            "data_classification": _gy_c7_labels.get(run.id),
            # Guarded by include_details: when lightweight, do NOT touch
            # run.node_runs / run.run_files — accessing an un-eager-loaded
            # collection would lazy-load it (an N+1 query per run).
            "node_runs": [
                {
                    "id": nr.id,
                    "workflow_node_id": nr.workflow_node_id,
                    "status": nr.status.value if nr.status else "queued",
                    "started_at": nr.started_at,
                    "completed_at": nr.completed_at,
                    "node_id": nr.workflow_node.graph_node_id if nr.workflow_node else None
                }
                for nr in run.node_runs
            ] if include_details else [],
            "run_files": [
                {
                    "id": rf.id,
                    "file_id": rf.file_id,
                    "title": rf.title,
                    "status": rf.status.value if rf.status else None
                }
                for rf in run.run_files
            ] if include_details else []
        }
        for run in runs
    ]


def get_workflow_run_by_id(
    db: Session,
    run_id: int,
    tenant_id: Optional[int] = None
) -> Optional[models.WorkflowRun]:
    """
    Retrieve a single workflow run by its ID.
    If tenant_id is provided, ensures the run belongs to that tenant.
    If tenant_id is None (for platform admins), returns the run without tenant filtering.
    """
    query = (
        db.query(models.WorkflowRun)
        .options(
            joinedload(models.WorkflowRun.workflow),          # to-one: safe to join
            joinedload(models.WorkflowRun.workflow_version),  # to-one: safe to join
            # selectinload (separate IN() queries), NOT joinedload: joining BOTH
            # run_files AND node_runs in one statement is a cartesian product
            # (run_files × node_runs rows for the run) — the same blow-up that made
            # the runs LIST slow. This endpoint (/workflow-runs/{run_id}) is polled,
            # so keep it off the cartesian. pg_stat_statements flagged it as the top
            # query by total time.
            selectinload(models.WorkflowRun.run_files),
            selectinload(models.WorkflowRun.node_runs).joinedload(models.NodeRun.workflow_node),
        )
        .filter(models.WorkflowRun.id == run_id)
    )

    # Filter by tenant if tenant_id is provided (non-platform admins)
    if tenant_id is not None:
        query = query.filter(models.WorkflowRun.tenant_id == tenant_id)

    return query.first()


# ---------------------------------------------------------------------------
# GSEC R-086 — NodeRun content bound
# ---------------------------------------------------------------------------
# `NodeRun.inputs` / `NodeRun.outputs` / `NodeRun.error_message` used to take
# whatever the worker reported back, verbatim. A node that prints a runaway
# log tail (or embeds an inline blob in its result payload) pinned the row at
# tens of MB, and every runs-list load then paid for it. The cap is a
# platform setting, not an env var: `node_run.content_max_chars`
# (0 = clamp disabled, i.e. the pre-fix unbounded behaviour).
#
# The clamp is per STRING, not per payload: a chatty node must still complete,
# it just stops contributing unbounded text. Platform-identity fields
# (content_hash, file paths, cog ids, urls) are short and never near the cap,
# so this cannot break the NodeOutputCache key or fan-out resolution.
def clamp_node_run_text(value: Any, max_chars: Optional[int] = None) -> Any:
    """Recursively clamp every string in `value` to the content cap.

    Dict KEYS are left alone (they are field names the platform itself wrote);
    only values are clamped. Non-strings pass through untouched.
    """
    if max_chars is None:
        from services import platform_settings
        max_chars = platform_settings.get_setting("node_run.content_max_chars")
    if not isinstance(max_chars, int) or max_chars <= 0:
        return value
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        dropped = len(value) - max_chars
        return value[:max_chars] + f"…[truncated {dropped} chars]"
    if isinstance(value, dict):
        return {k: clamp_node_run_text(v, max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out = [clamp_node_run_text(v, max_chars) for v in value]
        return type(value)(out) if isinstance(value, tuple) else out
    return value


# ---------------------------------------------------------------------------
# FX.117 p3a — exact-value masking of tenant secret VALUES at node-run ingest.
#
# The customer secret store (FX.117, owner Q104=A) injects a tenant's secret
# VALUE into the consuming node. That node printing the credential into its
# stdout/output/error used to persist it verbatim in NodeRun.inputs /
# .outputs / .error_message — pattern redaction (utils.secret_redaction.redact)
# only catches values that LOOK like a secret, so a bare opaque credential
# slipped straight through. Part 1 masked the worker TASK report; this closes
# the same gap at the NODE-RUN ingest point, which is where the API persists
# node logs/outputs/errors and therefore also serves them to the operator.
#
# The values come from the run's own graph (`graph_json[].data.secret_refs`),
# resolved STRICTLY against the run's tenant — a node can never pull another
# tenant's secret. Gated by the `secrets.mask_values_in_logs` platform setting
# (default True). Never raises: a lookup/settings failure degrades masking,
# it never breaks the ingest the worker already completed.
# ---------------------------------------------------------------------------
def _fx117_mask_node_run_content(db, db_node_run, inputs, outputs, error_message):
    """Mask the run's exact secret values in the node-run content about to be
    stored. Returns (inputs, outputs, error_message) — unchanged when the
    setting is off, the run has no secrets, or the lookup fails."""
    if inputs is None and outputs is None and error_message is None:
        return inputs, outputs, error_message
    try:
        from services import secret_masking
        if not secret_masking.masking_enabled(db):
            return inputs, outputs, error_message
        values = secret_masking.run_secret_values(
            db, getattr(db_node_run, "workflow_run_id", None))
        if not values:
            return inputs, outputs, error_message
        return (
            secret_masking.mask_in_obj(inputs, values) if inputs is not None else None,
            secret_masking.mask_in_obj(outputs, values) if outputs is not None else None,
            secret_masking.mask_secret_values(error_message, values)
            if error_message is not None else None,
        )
    except Exception:  # noqa: BLE001 — masking must never break the ingest
        return inputs, outputs, error_message
