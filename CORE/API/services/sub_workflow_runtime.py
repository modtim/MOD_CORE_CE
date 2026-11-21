"""workflow-expressiveness-primitives 2.1b — sub_workflow RUNTIME.

A ``sub_workflow`` node executes as a UNIT: the referenced workflow VERSION is
dispatched as an ordinary run whose inputs come from the parent node's input,
the parent node parks until that child reaches a terminal status, and the child's
terminal output is propagated as the parent node's output
(spec scenario "Sub-workflow executes as a unit at dispatch time").

Shape is the proven human-approval pair, mirrored exactly:

* ``services/workflow_orchestrator.park_for_human_approval`` — True means the
  caller must NOT enqueue a task; the wait is a database row (PENDING_ACTION),
  never a container holding a worker slot; the run event is emitted BEFORE the
  commit; re-dispatch of an already-parked node is idempotent.
* ``services/interventions_service.approve`` — completes the parked NodeRun,
  persists a BORN-COMPLETED ``ApiTaskQueue`` gate row (so
  ``routers/worker_tasks._check_and_dispatch_downstream``'s parent-readiness
  query sees a COMPLETED parent), then calls that dispatcher. The row is created
  on completion, never at park time: a QUEUED row would be claimed by the first
  worker to poll, which is exactly the blocking-on-a-child we park to avoid.

Child creation mirrors ``dispatch_trigger_run`` (which cannot be reused as-is:
it always picks the highest ``version_number``, and this node needs a PINNED
``workflow_version_id``), including its provisional ``started_at`` stamp and its
background-thread hand-off to ``start_workflow_execution``.

Same-tenant only: cross-tenant refs are refused at compose time
(``sub_workflow_validation`` / ``sub_workflow_resolve``), and the runtime
re-checks the tenant of the pinned version so a hand-edited stamp can never
dispatch another tenant's graph.

No module-level import of the orchestrator / worker_tasks: the orchestrator
grows a call-site hook into this module, so a top-level import here would be a
circular import (the FX.106 p3a client_ip <-> platform_settings <-> audit shape).
Both are imported lazily at the call site.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

import models
from services import sub_workflow_resolve as swr

_expressiveness_metrics: Any = None
try:
    from services.expressiveness_metrics import (
        record_sub_workflow_dispatch,
    )
except (ImportError, ModuleNotFoundError):
    pass

logger = logging.getLogger(__name__)

SUB_WORKFLOW_NODE_KIND = "sub_workflow"
CHILD_MARKER = "sub_workflow_child_run_id"

TERMINAL_NODE_STATUSES = (
    models.TaskStatus.SUCCESS,
    models.TaskStatus.COMPLETED,
    models.TaskStatus.FAILURE,
    models.TaskStatus.FAILED,
)


def _graph_nodes(graph: Any) -> list:
    nodes = (graph or {}).get("nodes") if isinstance(graph, dict) else None
    return nodes if isinstance(nodes, list) else []


def _graph_edges(graph: Any) -> list:
    edges = (graph or {}).get("edges") if isinstance(graph, dict) else None
    return edges if isinstance(edges, list) else []


def child_terminal_outputs(db: Session, child_run) -> Dict[str, Any]:
    """Merged outputs of the child's TERMINAL graph nodes, keyed by graph node id.

    Terminal = a node of the child's version graph with no outgoing edge. Its
    NodeRun(s) in this child run carry the outputs; a node with several attempts
    merges oldest-first so the newest write wins.
    """
    version = db.get(models.WorkflowVersion, child_run.workflow_version_id)
    graph = version.graph_json if version else {}
    edges = _graph_edges(graph)
    sources = {e.get("source") for e in edges if isinstance(e, dict)}
    terminal_ids = [
        n.get("id") for n in _graph_nodes(graph)
        if isinstance(n, dict) and n.get("id") is not None and n.get("id") not in sources
    ]
    if not terminal_ids:
        # A child graph with no edges at all: every node is terminal, including
        # the roots. Fall back to every node so the output is never silently {}.
        terminal_ids = [n.get("id") for n in _graph_nodes(graph)
                        if isinstance(n, dict) and n.get("id") is not None]

    merged: Dict[str, Any] = {}
    for graph_node_id in terminal_ids:
        rows = (
            db.query(models.NodeRun)
            .join(models.WorkflowNode)
            .filter(
                models.NodeRun.workflow_run_id == child_run.id,
                models.NodeRun.status == models.TaskStatus.SUCCESS,
                models.WorkflowNode.graph_node_id == graph_node_id,
            )
            .order_by(models.NodeRun.id.asc())
            .all()
        )
        for row in rows:
            if row.outputs is not None:
                merged[graph_node_id] = row.outputs
    return merged


def park_and_dispatch_sub_workflow(
    db: Session,
    *,
    workflow_run_id: int,
    node_run_id: Optional[int],
    node_id: str,
    node_kind: str,
    node_data: dict,
) -> bool:
    """Gate 2.1b. If this node is a sub_workflow, park it and dispatch the child.

    True means the caller must NOT enqueue a task for this node — the node's
    wait is the child run, not a container. Idempotent: a NodeRun already
    carrying ``CHILD_MARKER`` returns True without creating a second child.

    A resolve failure creates NO child and fails the parent node + parent run
    with the resolver's error code (spec: "refuse the parent workflow if the
    reference does not resolve").
    """
    if node_kind != SUB_WORKFLOW_NODE_KIND:
        return False

    from services.workflow_orchestrator import _emit_run_event
    from utils.minio_client import get_tenant_bucket_name

    run = db.get(models.WorkflowRun, workflow_run_id)
    tenant_id = run.tenant_id if run is not None else None

    node_run = db.get(models.NodeRun, node_run_id) if node_run_id is not None else None

    # Idempotent re-dispatch: the marker is the durable proof a child exists.
    if node_run is not None and isinstance(node_run.outputs, dict) \
            and node_run.outputs.get(CHILD_MARKER) is not None:
        return True

    # --- version pin -------------------------------------------------------
    version_id = None
    try:
        pinned = (node_data or {}).get("resolved_workflow_version_id")
        if isinstance(pinned, int) and not isinstance(pinned, bool):
            version_id = pinned
        else:
            stamped = swr.resolve_sub_workflow_refs(
                db, tenant_id,
                {"nodes": [{"id": node_id, "data": node_data or {}}], "edges": []},
            )
            nodes = stamped.get("nodes") or []
            if nodes and isinstance(nodes[0], dict):
                got = (nodes[0].get("data") or {}).get("resolved_workflow_version_id")
                if isinstance(got, int) and not isinstance(got, bool):
                    version_id = got
    except swr.SubWorkflowResolveError as e:
        return _fail_parent_for_resolve_error(
            db, _emit_run_event, workflow_run_id, node_run, node_id, e
        )

    version = db.get(models.WorkflowVersion, version_id) if version_id is not None else None
    if version is None or not version.graph_json or tenant_id is None \
            or version.workflow_id is None:
        return _fail_parent_for_resolve_error(
            db, _emit_run_event, workflow_run_id, node_run, node_id,
            swr.SubWorkflowResolveError(swr.CODE_UNRESOLVED, node_id),
        )

    # Same tenant ONLY: the compose-time resolver is tenant-scoped, but a
    # hand-stamped / stale resolved_workflow_version_id must never dispatch
    # another tenant's graph, so re-check the owner here.
    owner = db.get(models.Workflow, version.workflow_id)
    if owner is None or owner.tenant_id != tenant_id or owner.deleted_at is not None \
            or version.deleted_at is not None:
        return _fail_parent_for_resolve_error(
            db, _emit_run_event, workflow_run_id, node_run, node_id,
            swr.SubWorkflowResolveError(swr.CODE_CROSS_TENANT, node_id),
        )

    # --- chain depth -------------------------------------------------------
    from services.workflow_orchestrator import MAX_CHAIN_DEPTH

    parent_depth = getattr(run, "chain_depth", None)
    parent_depth = parent_depth if isinstance(parent_depth, int) else 0
    child_depth = parent_depth + 1
    if child_depth > MAX_CHAIN_DEPTH:
        # The existing chain-cap refusal shape: a synthetic FAILED child is
        # persisted (auditable), the parent node + run fail, nothing dispatches.
        synthetic = models.WorkflowRun(
            tenant_id=tenant_id,
            workflow_id=owner.id,
            workflow_version_id=version_id,
            triggered_by_user_id=run.triggered_by_user_id,
            status=models.TaskStatus.FAILED,
            error_message=f"sub_workflow chain depth exceeded (depth {child_depth})",
            completed_at=datetime.now(timezone.utc),
            run_name=f"{owner.name} (sub_workflow chain-depth cap)",
            purpose=run.purpose if run.purpose else "production",
        )
        db.add(synthetic)
        db.commit()
        db.refresh(synthetic)
        return _fail_parent_for_resolve_error(
            db, _emit_run_event, workflow_run_id, node_run, node_id,
            swr.SubWorkflowResolveError("chain_depth_exceeded", node_id),
        )

    # --- child run ---------------------------------------------------------
    inputs = node_run.inputs if node_run is not None else None
    extra_inputs = dict(inputs) if isinstance(inputs, dict) else {}
    graph = version.graph_json or {}
    tenant_bucket = get_tenant_bucket_name(tenant_id, db)
    full_conf = {
        "tenant_id": tenant_id,
        "tenant_bucket": tenant_bucket,
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "project_info": {
            "tenant_id": tenant_id,
            "tenant_bucket": tenant_bucket,
            "user_id": run.triggered_by_user_id,
            "cog_ids": [],
            "metadata_cog_ids": [],
            "parent_folder_id": None,
            "workflow_name": owner.name,
            "extra_variables": dict(extra_inputs),
        },
    }

    child = models.WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=owner.id,
        workflow_version_id=version_id,
        triggered_by_user_id=run.triggered_by_user_id,
        status=models.TaskStatus.QUEUED,
        parameters=full_conf,
        run_name=f"{owner.name} (sub_workflow of run {workflow_run_id})",
        # GO.6c, same as dispatch_trigger_run: a provisional started_at so a
        # run whose dispatch thread dies is still visible to the zombie sweep.
        started_at=datetime.now(timezone.utc),
        chain_depth=child_depth,
        purpose=run.purpose if run.purpose else "production",
    )
    db.add(child)
    db.commit()
    db.refresh(child)

    full_conf["workflow_run_id"] = child.id

    # --- parent node: park + marker ----------------------------------------
    if node_run is not None:
        marker = {CHILD_MARKER: child.id}
        existing = node_run.outputs
        if isinstance(existing, dict):
            marker = {**existing, **marker}
        node_run.outputs = marker
        node_run.status = models.TaskStatus.PENDING_ACTION
        flag_modified(node_run, "outputs")
        db.commit()

    # BEFORE the commit: _emit_run_event does not commit, and an event written
    # after one is discarded (the a6dd71f4 mistake).
    _emit_run_event(
        db, workflow_run_id, "orchestrator", "info",
        f"Sub-workflow '{node_id}' dispatched as run #{child.id}",
        {"node_id": node_id, "child_run_id": child.id,
         "workflow_version_id": version_id, "chain_depth": child_depth},
    )
    db.commit()

    # Hand orchestration to a background thread exactly like dispatch_trigger_run
    # does, so the caller's request never blocks on plan-build + enqueue.
    # Same background hand-off dispatch_trigger_run uses, so the caller's
    # request never blocks on plan-build + enqueue. Wrapped in _start_child so
    # the hand-off is one monkeypatchable call (and so this module never needs
    # the orchestrator at import time).
    import threading
    from database import SessionLocal
    threading.Thread(
        target=_start_child,
        args=(SessionLocal, full_conf, child.id),
        daemon=True,
    ).start()

    # EXPR 14.1: counts sub-workflow dispatches (refusal/resolve paths return
    # earlier and never reach here). sub_workflow_dispatch_count is monotonic
    # (the helper only inc/sets, no decrement) — it tallies dispatches, not
    # the current in-flight count.
    try:
        record_sub_workflow_dispatch()
    except Exception:  # noqa: BLE001 — metrics are best-effort
        pass
    logger.info(
        "Run %d: sub_workflow node %s -> child run %d (tenant %d, version %d, depth %d)",
        workflow_run_id, node_id, child.id, tenant_id, version_id, child_depth,
    )
    return True


def _start_child(db_factory, conf: dict, workflow_run_id: int):
    """The same background hand-off ``dispatch_trigger_run`` uses.

    A thin module-level wrapper so the hand-off is monkeypatchable in tests
    (no real worker/thread work) and so this module never imports the
    orchestrator at module scope.
    """
    from services.workflow_orchestrator import start_workflow_execution
    return start_workflow_execution(db_factory, conf, workflow_run_id)


def complete_parent_for_child(db: Session, child_run) -> bool:
    """Terminal fan-out: a child run reached COMPLETED/FAILED -> complete the parent.

    MUST NOT raise: this runs inside ``fire_completion_triggers``, where a
    failure would roll back the child's committed terminal status.

    The parent is found through the marker, tenant-scoped via the parent's own
    WorkflowRun (a foreign tenant's NodeRun carrying a forged marker is never
    touched). Idempotent: an already-terminal parent NodeRun returns False.
    """
    try:
        return _complete_parent_for_child_inner(db, child_run)
    except Exception:  # noqa: BLE001 — never propagate out of the fan-out
        logger.exception(
            "sub_workflow completion for child run %s failed",
            child_run.id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _complete_parent_for_child_inner(db: Session, child_run) -> bool:
    from services.workflow_orchestrator import _emit_run_event
    from routers.worker_tasks import _check_and_dispatch_downstream
    from crud.workflows_domain import conditional_cancel_run

    if child_run.status not in (models.TaskStatus.COMPLETED, models.TaskStatus.FAILED):
        return False

    # The marker row: JSONB containment on NodeRun.outputs, then the tenant
    # filter through the parent's OWN WorkflowRun (NodeRun has no tenant column).
    candidates = (
        db.query(models.NodeRun)
        .filter(
            models.NodeRun.outputs[CHILD_MARKER].as_integer() == child_run.id,
        )
        .all()
    )
    parent_node_run = None
    parent_run = None
    for row in candidates:
        run = db.get(models.WorkflowRun, row.workflow_run_id)
        if run is None or run.id == child_run.id:
            continue
        if run.tenant_id != child_run.tenant_id:
            # Foreign tenant with a forged marker — never completed.
            continue
        if row.status in TERMINAL_NODE_STATUSES:
            # Idempotent: the parent already completed/failed on an earlier
            # fan-out (or a manual resume), so a second call is a no-op.
            return False
        parent_node_run = row
        parent_run = run
        break
    if parent_node_run is None or parent_run is None:
        return False

    # Atomic claim: a conditional UPDATE guards the PENDING_ACTION -> terminal
    # transition against a second, concurrent fan-out for the same child run
    # (e.g. fire_completion_triggers invoked twice). Postgres row-locks the
    # matched row for the duration of the UPDATE, so a racing transaction
    # blocks here and then re-evaluates the WHERE clause against the
    # already-committed new status, matching zero rows and losing the race
    # cleanly instead of double-writing the gate row / downstream dispatch.
    new_status = (
        models.TaskStatus.SUCCESS
        if child_run.status == models.TaskStatus.COMPLETED
        else models.TaskStatus.FAILURE
    )
    claimed = (
        db.query(models.NodeRun)
        .filter(
            models.NodeRun.id == parent_node_run.id,
            models.NodeRun.status == models.TaskStatus.PENDING_ACTION,
        )
        .update({"status": new_status}, synchronize_session="fetch")
    )
    if claimed == 0:
        # Lost the race: another completion already claimed this parent.
        db.rollback()
        return False

    if child_run.status == models.TaskStatus.COMPLETED:
        outputs = child_terminal_outputs(db, child_run)
        marker = {CHILD_MARKER: child_run.id, "outputs": outputs}
        parent_node_run.outputs = marker
        flag_modified(parent_node_run, "outputs")
        parent_node_run.completed_at = datetime.now(timezone.utc)

        wnode = db.get(models.WorkflowNode, parent_node_run.workflow_node_id)
        gate_task = models.ApiTaskQueue(
            workflow_run_id=parent_run.id,
            node_run_id=parent_node_run.id,
            node_id=wnode.graph_node_id if wnode else None,
            tenant_id=parent_run.tenant_id,
            queue_name="CPU",
            status=models.ApiTaskStatus.COMPLETED,
            task_payload={"node_kind": SUB_WORKFLOW_NODE_KIND,
                          CHILD_MARKER: child_run.id},
            result_payload={"outputs": outputs},
        )
        db.add(gate_task)
        db.commit()
        db.refresh(gate_task)
        try:
            _check_and_dispatch_downstream(db, gate_task)
        except Exception:  # noqa: BLE001
            logger.exception(
                "sub_workflow child %s: parent %s downstream dispatch failed",
                child_run.id, parent_run.id,
            )
        return True

    # Child FAILED -> the parent node fails and the parent run fails through the
    # shared atomic flip helper (nothing downstream dispatches: the run is
    # terminal, so _check_and_dispatch_downstream is deliberately NOT called).
    message = f"sub_workflow child run {child_run.id} failed"
    parent_node_run.error_message = message
    _emit_run_event(db, parent_run.id, "orchestrator", "error", message,
                    {"child_run_id": child_run.id})
    conditional_cancel_run(
        db, parent_run.id,
        completed_at=datetime.now(timezone.utc),
        error_message=message,
    )
    db.commit()
    return True


def _fail_parent_for_resolve_error(
    db: Session,
    emit_run_event,
    workflow_run_id: int,
    node_run,
    node_id,
    error,
) -> bool:
    """No child was created: fail the parent NodeRun + parent run with the code.

    The run flip goes through crud/workflows_domain.conditional_cancel_run —
    the shared atomic QUEUED/RUNNING -> FAILED helper the orchestrator's own
    fail-fast guards use — never a hand-rolled status write.
    """
    from crud.workflows_domain import conditional_cancel_run

    message = f"{error.code}: sub_workflow node {node_id}"
    if node_run is not None:
        node_run.status = models.TaskStatus.FAILURE
        node_run.error_message = message
    emit_run_event(db, workflow_run_id, "orchestrator", "error", message,
                   {"node_id": node_id, "code": error.code})
    conditional_cancel_run(
        db, workflow_run_id,
        completed_at=datetime.now(timezone.utc),
        error_message=message,
    )
    db.commit()
    logger.error("Run %d: sub_workflow node %s refused (%s)",
                 workflow_run_id, node_id, error.code)
    return True
