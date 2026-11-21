"""Workflow CRUD operations - Workflow management within tenants."""
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from sqlalchemy.orm import Session
from datetime import datetime

import models, schemas
from sqlalchemy.orm import joinedload
from fastapi import HTTPException  # FORTRESS.2.6 — save-time validation refusals
from sqlalchemy import func
import logging
from utils.paid_import import optional_paid

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# manager-work-loans (GC.MWL.7b/7c) — top-level `conf` keys stripped before
# a snapshot is written to durable storage. `keycloak_token` is the one
# confirmed live secret in today's run-creation `conf`; the rest are
# defensive against a future call site adding one.
#
# GC.MWL.7c: this same stripping is now ALSO applied to `WorkflowRun.
# parameters` (not just `execution_conf`) — the token is kept ONLY in the
# transient in-process `conf` dict that `services.workflow_orchestrator.
# start_workflow_execution` is handed directly by the caller (never reloaded
# from the DB), so in-process dispatch is unaffected while neither durable
# column ever stores the secret.
_EXECUTION_CONF_SECRET_KEYS = (
    "keycloak_token", "refresh_token", "access_token", "id_token",
    "password", "api_key", "secret",
)


def _execution_conf_snapshot(conf: Dict[str, Any]) -> Dict[str, Any]:
    """A durable, secret-free copy of `conf` for `WorkflowRun.execution_conf`
    (GC.MWL.7b) — see that column's comment in models.py and the migration
    docstring for why this is a separate column from `parameters`."""
    return {k: v for k, v in (conf or {}).items() if k not in _EXECUTION_CONF_SECRET_KEYS}


def create_workflow_run(
    db: Session,
    version: models.WorkflowVersion,
    user_id: int,
    conf: Dict[str, Any],
    encrypted_refresh_token: Optional[bytes] = None,
    run_name: Optional[str] = None,
    purpose: str = "production",
    trial_node_definition_id: Optional[int] = None,
    trial_data_source: Optional[str] = None,
    idempotency: Optional[tuple] = None,
) -> models.WorkflowRun:
    """
    Creates a new workflow run record in the database, ready to be queued.
    Also creates WorkflowNode and NodeRun records for tracking.

    Args:
        version: The workflow version to run (contains the workflow reference)
        user_id: ID of the user triggering the run
        conf: Configuration dict containing tenant_id for multi-tenant isolation
        encrypted_refresh_token: Encrypted Keycloak refresh token for secret access during execution
        run_name: Human-readable name for the run (e.g., "My Workflow v3 - photo1.jpg, photo2.jpg")
        purpose: workflow-agentic-execution-loop §workflow-run-execution —
            'production' (default) is today's behavior. 'wizard_test_run'
            routes through the sandbox path (isolated MinIO prefix, no
            credit charge, etc.). Persisted on the WorkflowRun row so the
            orchestrator can read it at dispatch time.
    """
    tenant_id = conf.get('tenant_id')
    if not tenant_id:
        raise ValueError("tenant_id must be provided in conf for workflow run creation")

    workflow = version.workflow

    # AI scheduler — priority class inherited from the WorkflowVersion at run
    # creation, mutable via conf override for one-off priority bumps (admin
    # re-prioritising a stuck batch run, scheduled jobs that bypass the
    # default). Drives the packer's algorithm selection downstream.
    priority_class = conf.get("priority_class") or getattr(version, "priority_class", None) or "normal"

    # Build run-row kwargs. `purpose` is a new column from
    # workflow-agentic-execution-loop §workflow-run-execution; set via
    # setattr to keep the call safe on schemas without the column yet
    # (deployment race window).
    run_kwargs = dict(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        triggered_by_user_id=user_id,
        status=models.TaskStatus.QUEUED,
        # GC.MWL.7c: `parameters` is durable (workflow_runs.parameters) —
        # never write a live keycloak/refresh/access token into it. The
        # UNSTRIPPED `conf` is still what the caller hands directly to
        # `start_workflow_execution` for this request's in-process dispatch,
        # so no in-process consumer loses the token.
        parameters=_execution_conf_snapshot(conf),
        encrypted_refresh_token=encrypted_refresh_token,
        run_name=run_name,
        priority_class=priority_class,
    )
    db_run = models.WorkflowRun(**run_kwargs)
    if hasattr(models.WorkflowRun, "purpose"):
        try:
            db_run.purpose = purpose or "production"
        except Exception:
            pass
    # manager-work-loans (GC.MWL.7b) — best-effort/hasattr-guarded exactly
    # like `purpose` above: safe on a deployment where the additive
    # migration hasn't landed yet.
    if hasattr(models.WorkflowRun, "execution_conf"):
        try:
            db_run.execution_conf = _execution_conf_snapshot(conf)
        except Exception:
            pass
    # node-trial-runs — stamp the trial markers when this is a try-before-buy run.
    if purpose == "node_trial":
        for _attr, _val in (("trial_node_definition_id", trial_node_definition_id),
                            ("trial_data_source", trial_data_source)):
            if hasattr(models.WorkflowRun, _attr):
                try:
                    setattr(db_run, _attr, _val)
                except Exception:
                    pass
    db.add(db_run)
    db.flush()  # Get the run ID without committing
    if idempotency is not None:
        # 2400GX-n8a: (tenant_id, version_id, key). Fill the claimed key's run
        # id in THIS transaction so it never commits with workflow_run_id NULL.
        from services import run_idempotency as _run_idem
        _run_idem.attach_run_nocommit(db, idempotency[0], idempotency[1],
                                      idempotency[2], db_run.id)

    # FORTRESS.2.8 slice B — allocate this run's per-graph-node deterministic
    # seeds at creation, once, so every later read (reruns included) replays
    # the same values. Best-effort: a seed failure must never break run
    # creation, so swallow any error and log the exception class only.
    # Lazy import: services.deterministic_seeds -> platform_settings -> audit
    # pulls more of the package; importing it here keeps crud's import graph
    # unchanged at module load. The call is wrapped in its own SAVEPOINT so a
    # DB-level failure inside ensure_run_seeds (it runs its own query/flush)
    # rolls back only that savepoint, never poisoning the outer transaction
    # that the run insert and the NodeRun loop below still need.
    try:
        from services import deterministic_seeds as _deterministic_seeds
        with db.begin_nested():
            _deterministic_seeds.ensure_run_seeds(db, tenant_id, db_run)
    except Exception as exc:
        logger.warning("FORTRESS.2.8 seed allocation failed: %s", type(exc).__name__)

    # Create WorkflowNode and NodeRun records for each node in the graph
    node_run_by_graph_id: Dict[str, "models.NodeRun"] = {}
    node_def_dag_by_graph_id: Dict[str, str] = {}
    if version.graph_json:
        graph_json = version.graph_json
        if isinstance(graph_json, dict) and "nodes" in graph_json:
            nodes = graph_json["nodes"]

            for node in nodes:
                node_id = node.get("id")  # e.g., "input-1", "process-2"
                node_def_id = node.get("data", {}).get("nodeDefinitionId")

                if not node_id:
                    continue

                # Find or create WorkflowNode
                workflow_node = db.query(models.WorkflowNode).filter(
                    models.WorkflowNode.workflow_id == workflow.id,
                    models.WorkflowNode.graph_node_id == node_id
                ).first()

                if not workflow_node:
                    workflow_node = models.WorkflowNode(
                        workflow_id=workflow.id,
                        node_definition_id=node_def_id,
                        graph_node_id=node_id,
                        configuration=node.get("data", {})
                    )
                    # node-version-pinning — pin this node to a specific build so
                    # a later rebuild can't change what this workflow runs. Honor
                    # an explicit pin the builder saved into the graph; otherwise
                    # pin to the current build. Find-or-create above means this
                    # happens once per node and then stays put (re-runs reuse it),
                    # which is exactly the reproducibility guarantee.
                    try:
                        from crud import node_definition_cud as _ndc
                        _data = node.get("data", {}) or {}
                        workflow_node.pinned_node_version_id = (
                            _ndc.resolve_pin_for_new_workflow_node(
                                db, node_def_id, _data.get("pinnedNodeVersionId")
                            )
                        )
                    except Exception:  # noqa: BLE001 — pin is best-effort
                        pass
                    db.add(workflow_node)
                    db.flush()  # Get the workflow_node ID
                else:
                    # node-version-pinning — the node already has a WorkflowNode
                    # row (workflow has run before). "Update to vN" re-pins by
                    # saving a changed pinnedNodeVersionId into the graph; honor
                    # it here so an existing workflow can adopt a newer build.
                    # Only a VALID explicit change re-pins; absent/unchanged
                    # leaves the existing pin untouched (reproducibility).
                    try:
                        _data = node.get("data", {}) or {}
                        _explicit = _data.get("pinnedNodeVersionId")
                        if _explicit and _explicit != workflow_node.pinned_node_version_id:
                            from crud import node_definition_cud as _ndc
                            _resolved = _ndc.resolve_pin_for_new_workflow_node(
                                db, workflow_node.node_definition_id, _explicit
                            )
                            if _resolved == _explicit:
                                workflow_node.pinned_node_version_id = _resolved
                                db.flush()
                        elif not workflow_node.pinned_node_version_id:
                            # FX.103 (owner Q89=A) — backfill pin on re-run:
                            # legacy nodes that never got a pin (workflow ran
                            # before pinning existed) resolve "latest" on every
                            # dispatch. Pin them to the current build now so
                            # THIS run and every later re-run use exactly the
                            # same build; upgrading stays explicit
                            # (POST /workflows/{id}/upgrade-nodes).
                            from crud import node_definition_cud as _ndc
                            _backfill = _ndc.current_node_version(
                                db, workflow_node.node_definition_id
                            )
                            if _backfill is not None:
                                workflow_node.pinned_node_version_id = _backfill.id
                                db.flush()
                    except Exception:  # noqa: BLE001 — re-pin is best-effort
                        pass

                # AI scheduler — predict per-node duration at run creation.
                # Cheap pattern_db lookup keyed on the node's node_kind;
                # falls back to a 120s default for unseen nodes. Bucket H-1
                # will widen this to ONNX cold-start when no pattern exists.
                est_duration = _estimate_node_duration_seconds(db, node_def_id)

                # Create NodeRun for this execution
                node_run = models.NodeRun(
                    workflow_run_id=db_run.id,
                    workflow_node_id=workflow_node.id,
                    status=models.TaskStatus.QUEUED,
                    inputs=None,
                    outputs=None,
                    estimated_duration_seconds=est_duration,
                )
                db.add(node_run)
                node_run_by_graph_id[node_id] = node_run
                if node_def_id is not None:
                    nd = db.query(models.NodeDefinition.node_kind).filter(
                        models.NodeDefinition.id == node_def_id
                    ).first()
                    if nd and nd[0]:
                        node_def_dag_by_graph_id[node_id] = nd[0]

    # Critical-path workflow ETA: longest path through the metadata with each
    # node weighted by its estimated_duration_seconds. Uses graph_json edges;
    # if no edges (single-node or unstructured graph), falls back to max
    # of node estimates. This is the number the WorkflowProgress UI shows
    # in the run header.
    if node_run_by_graph_id:
        edges = version.graph_json.get("edges", []) if isinstance(version.graph_json, dict) else []
        critical_path_s = _compute_critical_path_seconds(node_run_by_graph_id, edges)
        db_run.estimated_duration_seconds = critical_path_s

    db.commit()
    db.refresh(db_run)
    return db_run


def upgrade_workflow_node_pins(db: Session, workflow_id: int) -> List[Dict[str, Any]]:
    """FX.103 (owner Q89=A) — the explicit "upgrade nodes" re-pin.

    Re-pins EVERY node of the workflow to its node's CURRENT build
    (the newest NodeVersion per node_definition). Returns the per-node
    changes as dicts:

        {"graph_node_id": ..., "node_definition_id": ...,
         "node_kind": ..., "previous_pin_id": ..., "previous_version": ...,
         "new_pin_id": ..., "new_version": ..., "changed": bool}

    Semantics:
      * pinned -> current: the pin moves to the newer build (upgrade);
      * pinned == current (or node has no recorded version): no change,
        `changed: False` (a deprecated/retired node without a newer build
        keeps its pin — retirement of the underlying build is part 2);
      * NULL pin (legacy/never-run node): pinned to the current build now
        (the "pin on next run" backfill, made explicit here), `changed`
        True only when a pin was actually written.

    Does NOT commit — the caller (endpoint) commits in one transaction so
    the audit row and the pin updates are atomic. No-op (returns []) for a
    workflow with no WorkflowNode rows yet (never run, never pinned).
    """
    from crud import node_definition_cud as _ndc

    changes: List[Dict[str, Any]] = []
    wns = (
        db.query(models.WorkflowNode)
        .filter(models.WorkflowNode.workflow_id == workflow_id)
        .all()
    )
    for wn in wns:
        entry: Dict[str, Any] = {
            "graph_node_id": wn.graph_node_id,
            "node_definition_id": wn.node_definition_id,
            "node_kind": getattr(wn.node_definition, "node_kind", None)
            if wn.node_definition else None,
            "previous_pin_id": wn.pinned_node_version_id,
            "previous_version": None,
            "new_pin_id": wn.pinned_node_version_id,
            "new_version": None,
            "changed": False,
        }
        if wn.pinned_node_version_id:
            pv = (
                db.query(models.NodeVersion)
                .filter(models.NodeVersion.id == wn.pinned_node_version_id)
                .first()
            )
            if pv:
                entry["previous_version"] = pv.version
        if wn.node_definition_id:
            nv = _ndc.current_node_version(db, wn.node_definition_id)
            if nv is not None:
                entry["new_pin_id"] = nv.id
                entry["new_version"] = nv.version
                if nv.id != wn.pinned_node_version_id:
                    wn.pinned_node_version_id = nv.id
                    entry["changed"] = True
                    db.flush()
        changes.append(entry)
    return changes


def _estimate_node_duration_seconds(db: Session, node_def_id: Optional[int]) -> int:
    """Lookup the average duration from ResourcePattern for this node's node_kind.

    Returns the recorded average rounded to int, or 120 (the conservative
    default the orchestrator's own fallback path uses) when no historical
    data is available. Cheap — one indexed query per node at run creation.
    """
    if not node_def_id:
        return 120
    nd = db.query(models.NodeDefinition.node_kind).filter(
        models.NodeDefinition.id == node_def_id
    ).first()
    if not nd or not nd[0]:
        return 120
    node_kind = nd[0]
    # Pick the highest-confidence pattern for this node_kind, regardless of
    # queue/size_tier — Bucket H-1 will tighten this to a worker-specific
    # selection once the predictor is wired. For ETA estimation right now,
    # any decent average is better than the 120s fallback.
    best = db.query(models.ResourcePattern).filter(
        models.ResourcePattern.node_kind == node_kind,
        models.ResourcePattern.avg_duration_seconds.isnot(None),
    ).order_by(models.ResourcePattern.confidence.desc().nullslast()).first()
    if best and best.avg_duration_seconds:
        return max(1, int(best.avg_duration_seconds))
    return 120


def _compute_critical_path_seconds(node_run_by_graph_id: Dict[str, Any], edges: List[Any]) -> int:
    """DAG longest-path computation using per-node estimates as edge weights.

    Each edge: {"source": "<graph_id>", "target": "<graph_id>"} (React Flow
    convention used by the WorkflowBuilder canvas; older shapes with
    "from"/"to" also tolerated).

    Returns the total expected duration in integer seconds. Used for the
    workflow-level ETA badge. If the metadata has cycles (shouldn't happen but
    guard against malformed input), falls back to sum of all estimates so
    the UI still gets a number.
    """
    durations = {gid: int(nr.estimated_duration_seconds or 120)
                 for gid, nr in node_run_by_graph_id.items()}
    if not durations:
        return 0

    children: Dict[str, List[str]] = {gid: [] for gid in durations}
    indeg: Dict[str, int] = {gid: 0 for gid in durations}
    for e in edges or []:
        src = e.get("source") or e.get("from")
        tgt = e.get("target") or e.get("to")
        if src in children and tgt in indeg:
            children[src].append(tgt)
            indeg[tgt] += 1

    if not edges or all(v == 0 for v in indeg.values()):
        return max(durations.values())

    # Topological sort.
    from collections import deque
    queue = deque([gid for gid, d in indeg.items() if d == 0])
    finish_time: Dict[str, int] = {gid: 0 for gid in durations}
    visited = 0
    while queue:
        gid = queue.popleft()
        visited += 1
        ft = finish_time[gid] + durations[gid]
        for child in children[gid]:
            if ft > finish_time[child]:
                finish_time[child] = ft
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)

    if visited != len(durations):
        # Cycle detected — fall back to total.
        return sum(durations.values())

    return max((finish_time[gid] + durations[gid]) for gid in durations)


def sync_workflow_nodes(db: Session, workflow: "models.Workflow",
                        graph_json: Any = None) -> int:
    """BUG.B85c1 — the graph -> `workflow_nodes` sync (find-or-create).

    Every other creation path already performs this sync: `create_workflow_run`
    find-or-creates a row per graph node at run creation, and
    `install_workflow` clones them. The orchestrator then only creates a
    NodeRun for a graph node that HAS a WorkflowNode row
    (services/workflow_orchestrator.py `_start_workflow`), so a workflow whose
    rows were never materialized runs as a NO-OP: zero NodeRuns, zero tasks,
    and — under a compliance profile — the egress gate can't resolve the node
    at all. A seeded system workflow that skipped the sync therefore does
    nothing on every single trigger, forever.

    Semantics (insert-only; it never deletes and never re-pins an existing row):
      * one row per graph node that carries an `id`;
      * an existing (workflow_id, graph_node_id) row is left exactly as it is;
      * a new row takes node_definition_id from `data.nodeDefinitionId` and
        configuration from the node's `data`, and is pinned to the node's
        current build best-effort — identical to create_workflow_run.

    `graph_json` defaults to the workflow's latest version graph. Does NOT
    commit — the caller owns the transaction. Returns rows created.
    """
    graph = graph_json
    if graph is None:
        ver = (db.query(models.WorkflowVersion)
               .filter(models.WorkflowVersion.id == workflow.latest_version_id)
               .first()) if workflow.latest_version_id else None
        graph = (ver.graph_json or {}) if ver else {}
    if not isinstance(graph, dict):
        return 0

    created = 0
    for node in graph.get("nodes") or []:
        node_id = node.get("id")
        if not node_id:
            continue
        node_data = node.get("data") or {}
        node_def_id = node_data.get("nodeDefinitionId")

        existing = db.query(models.WorkflowNode).filter(
            models.WorkflowNode.workflow_id == workflow.id,
            models.WorkflowNode.graph_node_id == node_id,
        ).first()
        if existing:
            continue

        workflow_node = models.WorkflowNode(
            workflow_id=workflow.id,
            node_definition_id=node_def_id,
            graph_node_id=node_id,
            configuration=node_data,
        )
        try:
            from crud import node_definition_cud as _ndc
            workflow_node.pinned_node_version_id = _ndc.resolve_pin_for_new_workflow_node(
                db, node_def_id, node_data.get("pinnedNodeVersionId")
            )
        except Exception:  # noqa: BLE001 — pin is best-effort
            pass
        db.add(workflow_node)
        db.flush()  # Get the workflow_node ID
        created += 1
    return created


def create_workflow(
    db: Session,
    workflow: schemas.WorkflowCreate,
    user_id: int,
    tenant_id: int
) -> models.Workflow:
    """Create a new workflow container for a specific tenant."""
    db_workflow = models.Workflow()

    db_workflow.name = workflow.name
    db_workflow.created_by_user_id = user_id
    db_workflow.tenant_id = tenant_id

    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def get_workflows_for_tenant(
    db: Session,
    tenant_id: int,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc",
    include_system: bool = False,
    include_archived: bool = False,
    viewer_user: Optional[models.User] = None,
) -> List[models.Workflow]:
    """Retrieve a paginated and sorted list of workflows for a specific tenant.

    By default (`include_system=False`) platform-internal workflows
    (Node Wizard build pipelines, system maintenance like
    HarborScanFollowup — anything with `is_system=True`) are hidden so
    the user-facing load-workflow picker isn't cluttered with
    bookkeeping rows. Platform admins can pass `include_system=True`
    when they need the full set (debugging, cleanup, etc.).

    Archived workflows (`archived_at` set) are also hidden by default —
    pass `include_archived=True` to include them (e.g. an "Archived"
    view in the Load picker).

    PRIVATE-BY-DEFAULT VISIBILITY: when `viewer_user` is provided the result is
    restricted to workflows that user may 'view' — the ones they own or hold an
    explicit grant/share to — via resource_permissions.workflow_view_scope,
    which mirrors authorize(WORKFLOW, "view", id) (the same gate GET
    /workflows/{id} uses). Tenant-scoping alone is NOT enough: without this a
    standard user saw and could load every workflow in the tenant, including the
    owner's private ones. Callers that intentionally want the full tenant set
    (system/admin maintenance paths) omit `viewer_user`.
    """
    query = db.query(models.Workflow).filter(
        models.Workflow.tenant_id == tenant_id,
        models.Workflow.deleted_at.is_(None)
    )

    if viewer_user is not None:
        from resource_permissions import workflow_view_scope
        see_all, visible_ids = workflow_view_scope(db, viewer_user)
        if not see_all:
            if not visible_ids:
                return []
            query = query.filter(models.Workflow.id.in_(visible_ids))

    if not include_system:
        query = query.filter(models.Workflow.is_system.is_(False))

    if not include_archived:
        query = query.filter(models.Workflow.archived_at.is_(None))

    if search:
        query = query.filter(models.Workflow.name.ilike(f"%{search}%"))

    if hasattr(models.Workflow, sort_by):
        column = getattr(models.Workflow, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_workflow_by_id(
    db: Session,
    workflow_id: int,
    tenant_id: int
) -> Optional[models.Workflow]:
    """Retrieve a single workflow by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.Workflow)
        .filter(
            models.Workflow.id == workflow_id,
            models.Workflow.tenant_id == tenant_id,
            models.Workflow.deleted_at.is_(None)
        )
        .first()
    )


def update_workflow(
    db: Session,
    db_workflow: models.Workflow,
    workflow_update: schemas.WorkflowUpdate
) -> models.Workflow:
    """Update a workflow's name and/or compliance tags (FORTRESS 4.1b)."""
    if workflow_update.name is not None:
        db_workflow.name = workflow_update.name
    # expr-10.1 — Workflow.differentiable opt-in flag (20261013k); reaches the
    # ORM here so PATCH /workflows/{id} can turn the save-time backward-fn
    # check (workflows_domains/workflows.py create_workflow_version) on/off.
    if workflow_update.differentiable is not None:
        db_workflow.differentiable = workflow_update.differentiable
    if workflow_update.compliance_tags is not None:
        old = set(db_workflow.compliance_tags or [])
        new = sorted(set(workflow_update.compliance_tags))
        db_workflow.compliance_tags = new
        logger.info(
            "compliance_tags changed workflow=%s added=%s removed=%s",
            db_workflow.id,
            ",".join(sorted(set(new) - old)),
            ",".join(sorted(old - set(new))),
        )
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def delete_workflow(db: Session, db_workflow: models.Workflow) -> models.Workflow:
    """Soft deletes a workflow by setting its 'deleted_at' timestamp."""
    db_workflow.deleted_at = datetime.utcnow()
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def set_workflow_archived(
    db: Session,
    db_workflow: models.Workflow,
    archived: bool,
) -> models.Workflow:
    """Archive (soft-hide) or unarchive a workflow.

    Archiving sets `archived_at` so the workflow drops out of the default
    Load picker without being deleted; unarchiving clears it. Distinct from
    the soft-delete flow (`deleted_at`), which is the "delete entire
    workflow" action and is not reversible from the UI.
    """
    db_workflow.archived_at = datetime.utcnow() if archived else None
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def duplicate_workflow(
    db: Session,
    source: models.Workflow,
    user_id: int,
    tenant_id: int,
    new_name: Optional[str] = None,
) -> models.Workflow:
    """Create a copy of a workflow (new container + a first version copied
    from the source's latest version's graph).

    The copy is owned by `user_id`, lives in `tenant_id`, and starts at
    version 1. The graph is run through `sanitize_graph_json` so no stale
    per-node runtime/preview state rides into the copy. If the source has
    no version yet, the copy is created empty (no version) — same shape a
    freshly-created workflow has.
    """
    base_name = new_name if new_name else f"{source.name} (copy)"

    # Copy the latest version's graph, if the source has one. Resolved BEFORE
    # the copy container is created so the FORTRESS 6.2 DP gate (928a) can
    # refuse a copy of a malformed-DP version before any row exists.
    source_version = None
    if source.latest_version_id is not None:
        source_version = (
            db.query(models.WorkflowVersion)
            .filter(models.WorkflowVersion.id == source.latest_version_id)
            .first()
        )
    if source_version is None:
        # Fall back to the highest-numbered version (defensive: latest_version_id
        # could be stale/null while versions exist).
        source_version = (
            db.query(models.WorkflowVersion)
            .filter(models.WorkflowVersion.workflow_id == source.id)
            .order_by(models.WorkflowVersion.version_number.desc())
            .first()
        )

    # FORTRESS 6.2 (928a) — a version saved before 911's save gate can carry a
    # malformed ``dp`` spec; duplicating it unchecked would launder it into a
    # fresh, otherwise-unreachable copy. Same gate, same 422 as the save path.
    if source_version is not None:
        _enforce_dp_gate(source_version.graph_json)

    new_workflow = models.Workflow()
    new_workflow.name = base_name
    new_workflow.created_by_user_id = user_id
    new_workflow.tenant_id = tenant_id
    # A copy is never a system/bookkeeping workflow, and is never born archived.
    new_workflow.is_system = False
    new_workflow.archived_at = None
    db.add(new_workflow)
    db.commit()
    db.refresh(new_workflow)

    if source_version is not None:
        new_version = models.WorkflowVersion(
            workflow_id=new_workflow.id,
            created_by_user_id=user_id,
            version_number=1,
            graph_json=sanitize_graph_json(source_version.graph_json),
        )
        db.add(new_version)
        db.commit()
        db.refresh(new_version)
        new_workflow.latest_version_id = new_version.id
        db.add(new_workflow)
        db.commit()
        db.refresh(new_workflow)

    return new_workflow


def count_workflow_versions(db: Session, workflow_id: int) -> int:
    """Number of versions a workflow has (used to guard last-version delete)."""
    return (
        db.query(func.count(models.WorkflowVersion.id))
        .filter(models.WorkflowVersion.workflow_id == workflow_id)
        .scalar()
        or 0
    )


def version_run_count(db: Session, version_id: int) -> int:
    """Number of (non-soft-deleted) runs referencing a version. Deleting a
    version with run history would orphan/lose that history, so callers block
    the delete when this is > 0."""
    q = db.query(func.count(models.WorkflowRun.id)).filter(
        models.WorkflowRun.workflow_version_id == version_id
    )
    # WorkflowRun carries a soft-delete column; count only live rows.
    if hasattr(models.WorkflowRun, "deleted_at"):
        q = q.filter(models.WorkflowRun.deleted_at.is_(None))
    return q.scalar() or 0


def delete_workflow_version(
    db: Session,
    db_version: models.WorkflowVersion,
) -> None:
    """Soft-delete a single workflow version.

    Callers MUST have already verified (see the router):
      - the version is not the workflow's only/last version, and
      - no runs reference it.
    If the version being deleted is the workflow's `latest_version`, the
    parent is repointed to the next-highest remaining version first so the
    latest_version_id FK never dangles. Trigger rules bound to the version
    are soft-deleted alongside it (meaningless once the version is gone, but
    kept as tombstones for the ISO audit trail); run-surface links cascade at
    the DB level.
    """
    workflow = (
        db.query(models.Workflow)
        .filter(models.Workflow.id == db_version.workflow_id)
        .first()
    )

    # Repoint latest_version_id BEFORE deleting so the FK never dangles.
    if workflow is not None and workflow.latest_version_id == db_version.id:
        next_latest = (
            db.query(models.WorkflowVersion)
            .filter(
                models.WorkflowVersion.workflow_id == db_version.workflow_id,
                models.WorkflowVersion.id != db_version.id,
            )
            .order_by(models.WorkflowVersion.version_number.desc())
            .first()
        )
        workflow.latest_version_id = next_latest.id if next_latest else None
        db.add(workflow)
        db.flush()

    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc)

    # iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    # iso-no-hard-delete). Since `WorkflowVersion` is now SOFT-deleted (the row
    # remains, so the RESTRICT FK from workflow_trigger_rules is NOT violated),
    # hard-deleting the trigger rules would needlessly destroy config. Soft-
    # delete them instead: `WorkflowTriggerRule` now carries a `deleted_at`
    # column (SoftDeleteMixin) and is in database.py's soft-delete listener, so
    # trigger-firing reads (via the `trigger_rules` relationship on a live
    # version) never see a tombstoned rule.
    db.query(models.WorkflowTriggerRule).filter(
        models.WorkflowTriggerRule.workflow_version_id == db_version.id,
        models.WorkflowTriggerRule.deleted_at.is_(None),
    ).update(
        {models.WorkflowTriggerRule.deleted_at: _now},
        synchronize_session=False,
    )

    # `WorkflowVersion` now carries a `deleted_at` column (SoftDeleteMixin) and
    # is in database.py's soft-delete listener, so the tombstoned version leaves
    # every live read. version_number is never reused (create_workflow_version
    # allocates over tombstones too).
    db_version.deleted_at = _now
    db.commit()


# === WORKFLOW VERSIONS ===

# Per-node runtime keys that a workflow version's graph_json must NEVER carry.
# graph_json is the workflow DEFINITION (nodes + edges + config). Runtime
# state — a node's outputs, previews, per-run status and timing — lives in
# the WorkflowRun / NodeRun tables, NOT the graph. But the WorkflowBuilder
# canvas writes these fields onto node.data WHILE a run is in flight/complete
# (status, started_at, previewAssets, outputCount, inputPreviewUrl, …) so it
# can render live progress and result previews. If the user then saves — even
# via "Save a clean copy" — those fields ride into graph_json and are stored
# verbatim, so reopening the saved/template workflow shows stale processing
# data (node outputs, previews, prior run state). We strip them here, on the
# single version-create chokepoint every save path funnels through, so every
# persisted version and template is clean regardless of what the client sends.
#
# NOTE: these are RUNTIME keys only. Definition/config keys are preserved —
# in particular schema keys (input_schema/output_schema/configuration_schema,
# input_type/output_type), input-node config (selectedFiles/fileId/fileName/
# file), runtime-override bags (extraVariables), secret bindings (secret_refs)
# and node pinning (pinnedNodeVersionId) are NOT in this set and ride through.
_RUNTIME_NODE_DATA_KEYS = frozenset({
    # WorkflowBuilder canvas run-state / preview fields (lib/types.ts NodeData).
    "previewAssets", "preview_assets",
    "outputCount", "output_count",
    "status",
    "error_message", "errorMessage",
    "started_at", "startedAt",
    "finished_at", "finishedAt", "completed_at", "completedAt",
    "inputPreviewUrl", "input_preview_url",
    "inputPreviewCategory", "input_preview_category",
    "workflow_run_id", "workflowRunId",
    "node_run_id", "nodeRunId",
    # Defensive generic runtime/result keys (never part of a node definition).
    "outputs", "output",
    "results", "result",
    "preview", "previews",
    "lastRun", "last_run",
    "runState", "run_state",
    "nodeRun", "node_run",
    "execution", "executionResult", "execution_result",
    "duration_seconds", "durationSeconds",
    "logs", "log",
})


def sanitize_graph_json(graph_json: Any) -> Any:
    """Return a copy of graph_json with per-node RUNTIME output/preview/run-state
    fields removed, so a persisted workflow version is a clean definition.

    Strips only the keys in `_RUNTIME_NODE_DATA_KEYS` from each node's `data`
    object. Node/edge definitions and all config keys are preserved. Non-dict
    input, or graphs without a well-formed `nodes` list, are returned unchanged
    (the caller stores whatever it was given). The input is not mutated.
    """
    if not isinstance(graph_json, dict):
        return graph_json
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return graph_json

    sanitized_nodes: List[Any] = []
    changed = False
    for node in nodes:
        if not isinstance(node, dict):
            sanitized_nodes.append(node)
            continue
        data = node.get("data")
        if not isinstance(data, dict):
            sanitized_nodes.append(node)
            continue
        cleaned_data = {k: v for k, v in data.items() if k not in _RUNTIME_NODE_DATA_KEYS}
        if len(cleaned_data) != len(data):
            changed = True
            new_node = dict(node)
            new_node["data"] = cleaned_data
            sanitized_nodes.append(new_node)
        else:
            sanitized_nodes.append(node)

    if not changed:
        return graph_json
    new_graph = dict(graph_json)
    new_graph["nodes"] = sanitized_nodes
    return new_graph


def _child_compliance_tags_for(db: Session, tenant_id: int, ref: object):
    """Resolve one sub-workflow ref key to that workflow's compliance tags.

    FORTRESS 4.3 — resolver half of the compose-time tag-inheritance check
    (``services/compliance_tag_inheritance.check_inheritance`` owns the pure
    half). Ref keys mirror ``utils/subworkflow_refs`` /
    ``services/sub_workflow_validation._resolve_ref_key``: an int is a
    ``Workflow.id``; a ``(tenant_slug, workflow_slug)`` pair matches the tenant
    on ``Tenant.name`` and the workflow on ``Workflow.name`` (this codebase has
    no dedicated slug column). Resolution is tenant-scoped and live-only: a
    foreign-tenant, unknown or soft-deleted ref resolves to ``None``, which
    makes the service emit a fail-closed ``compliance_tag_unresolved``.
    """
    if isinstance(ref, int) and not isinstance(ref, bool):
        wf = get_workflow_by_id(db, workflow_id=ref, tenant_id=tenant_id)
        return list(wf.compliance_tags or []) if wf else None
    if isinstance(ref, tuple) and len(ref) == 2:
        tenant_slug, workflow_slug = ref
        tenant = (
            db.query(models.Tenant)
            .filter(models.Tenant.name == tenant_slug)
            .first()
        )
        if tenant is None or tenant.id != tenant_id:
            return None
        wf = (
            db.query(models.Workflow)
            .filter(
                models.Workflow.tenant_id == tenant_id,
                models.Workflow.name == workflow_slug,
                models.Workflow.deleted_at.is_(None),
            )
            .first()
        )
        return list(wf.compliance_tags or []) if wf else None
    return None


def _dp_gate_violations(graph_json: Any) -> List[Dict]:
    """FORTRESS 6.2 — pure half of the compose-time DP spec gate (911).

    Walks the graph's nodes and returns one violation dict per node whose
    ``dp`` spec services/dp_budget.validate_dp rejects. Spec lookup mirrors
    the dispatch path exactly (routers/workflows_domains/workflows.py):
    node["data"]["dp"] wins over legacy node["config"]["dp"], and only when
    the container value is a dict. Inert (returns []) when no node declares
    dp: zero cost, no import.
    """
    _fortress62_dp_specs = []
    for _fortress62_node in (
        (graph_json.get("nodes") or [])
        if isinstance(graph_json, dict) else []
    ):
        if not isinstance(_fortress62_node, dict):
            continue
        _fortress62_spec = None
        for _fortress62_src in ("data", "config"):
            _fortress62_container = _fortress62_node.get(_fortress62_src)
            if isinstance(_fortress62_container, dict) and isinstance(
                _fortress62_container.get("dp"), dict
            ):
                _fortress62_spec = _fortress62_container["dp"]
                break
        if _fortress62_spec is not None:
            _fortress62_dp_specs.append(
                (_fortress62_node.get("id"), _fortress62_spec))
    if not _fortress62_dp_specs:
        return []
    # 1788j CE carve: services.dp_budget is paid/denied in CE — the
    # differential-privacy budget feature is OFF, so there is no budget check
    # and the gate is inert (no violations, same posture as no dp specs).
    _dp_mod = optional_paid("services.dp_budget")
    if _dp_mod is None:
        return []
    _dp_validate = _dp_mod.validate_dp
    _dp_violations = []
    for _fortress62_node_id, _fortress62_spec in _fortress62_dp_specs:
        try:
            _dp_validate(_fortress62_spec)
        except ValueError as _dp_exc:
            _dp_violations.append({
                "node_id": _fortress62_node_id,
                "error": str(_dp_exc),
                "keys": sorted(_fortress62_spec.keys()),
            })
    return _dp_violations


def _enforce_dp_gate(graph_json: Any) -> None:
    """FORTRESS 6.2 — refuse (422) a graph whose DP specs are malformed.

    Same walk, same precedence, same validate_dp as 911's save gate; the
    422 shape (detail.error == 'dp_validation_failed', violations capped at
    50) is byte-identical so 911's tests keep passing.
    """
    _dp_violations = _dp_gate_violations(graph_json)
    if _dp_violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "dp_validation_failed",
                "violations": _dp_violations[:50],
            },
        )


def create_workflow_version(
    db: Session,
    workflow: models.Workflow,
    version_create: schemas.WorkflowVersionCreate,
    user_id: int
) -> models.WorkflowVersion:
    """Create a new, immutable version of a workflow and update the parent's 'latest_version_id'."""
    # include_deleted: version_number is UNIQUE per workflow (plain constraint),
    # so a soft-deleted version still owns its number. Allocate the next number
    # over ALL versions incl. tombstones (iso-soft-delete-sweep) or a re-created
    # version could reuse a soft-deleted number and collide on INSERT.
    # FORTRESS.2.6 — compose-time reproducibility gate. A version that
    # declares requires_reproducibility (top level or under settings) must pin
    # every node container image to an @sha256 digest; a mutable handle
    # (:latest, untagged, any other tag) makes the run's proof bundle a lie, so
    # the SAVE itself is refused (422) — never a version that cannot be
    # reproduced. Inert (returns []) when the flag is off or when the
    # compliance.reproducibility_check_enabled platform setting is off.
    # 1784 CE carve: reproducibility_check is paid; absent -> no violations, i.e. the
    # gate is inert (same posture as flag-off / setting-off). SaaS unchanged.
    _repro = optional_paid("services.reproducibility_check")
    _violations = (
        _repro.check_workflow(db, workflow.tenant_id, version_create.graph_json)
        if _repro is not None else []
    )
    if _violations:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _repro.VIOLATION_CODE,
                "message": (
                    "This workflow declares requires_reproducibility, but "
                    f"{len(_violations)} node image(s) are not digest-pinned "
                    "(@sha256:...). Rebuild/pin those nodes or clear the "
                    "declaration before saving."
                ),
                "violations": _violations,
            },
        )

    # EXPR.10.1 — compose-time differentiability gate (single source of
    # truth for this check; see services/differentiable_nodes.py). Triggers
    # when EITHER the version's own graph_json declares
    # ``"differentiable": true`` inline (the pre-existing, documented
    # convention also enforced at listing/publish/import time) OR the parent
    # Workflow opted in via the ``differentiable`` column (PATCH
    # /workflows/{id}, migration 20261013k) -- either way every node must
    # give a differentiable path (analytical ``backward`` ref or
    # ``numerical_gradient: true``); a bare node makes the declared gradient
    # semantics a lie, so the SAVE itself is refused (422) — never a version
    # that cannot be trained. A second, conflicting save-time gate keyed only
    # off the column used to live in routers/workflows_domains/workflows.py;
    # it has been folded into this one so there is exactly one validation
    # path and one error code. Inert (returns []) when neither signal opts
    # in, so non-differentiable saves behave exactly as before.
    # 1784 CE carve: differentiable_nodes is paid; absent -> no violations, i.e. the
    # gate is inert (same posture as neither-signal-opting-in). SaaS unchanged.
    _diff = optional_paid("services.differentiable_nodes")
    _diff_graph = version_create.graph_json
    if (_diff is not None and getattr(workflow, "differentiable", False)
            and isinstance(_diff_graph, dict)
            and not _diff_graph.get("differentiable")):
        _diff_graph = {**_diff_graph, "differentiable": True}
    _diff_violations = (
        _diff.validate_differentiable(_diff_graph) if _diff is not None else []
    )
    if _diff_violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "differentiable_validation_failed",
                "violations": _diff_violations[:50],
            },
        )

    # FORTRESS 6.2 — compose-time differential-privacy spec gate. A node that
    # declares a ``dp`` spec must be calibratable: services/dp_budget.validate_dp
    # is the single source of truth (dispatch already calls it), but a malformed
    # spec (epsilon <= 0, non-numeric epsilon, missing sensitivity, ...) only
    # raises at run/dispatch time — and preflight_run calls validate_dp outside
    # its DpBudgetExhausted handler, so such a graph surfaces as an unhandled 500
    # there. Refuse the SAVE itself (422): a bad DP graph never becomes a
    # version. Spec lookup mirrors the dispatch path exactly
    # (routers/workflows_domains/workflows.py): node["data"]["dp"] wins over
    # legacy node["config"]["dp"], and only when the container value is a dict.
    # Inert when no node declares dp: zero cost, no import, no behaviour change.
    _enforce_dp_gate(version_create.graph_json)

    # FORTRESS 4.3 — compose-time compliance-tag inheritance gate. A tagged
    # workflow SHALL only call sub-workflows that carry a SUPERSET of its tags,
    # so a version whose child ref drops a tag is refused at the SAVE (422) —
    # never a version that dispatches a HIPAA parent into a non-HIPAA child.
    # Inert (zero extra queries) when the parent is untagged, and the resolver
    # is never even built in that case. Posture is the tenant's
    # ``fortress.tag_dispatch_gate_mode`` (shared with 4.4): 'off' skips
    # entirely, 'shadow' logs codes + node ids only, 'enforce' refuses.
    _parent_tags = list(workflow.compliance_tags or [])
    if _parent_tags:
        _taginh = optional_paid("services.compliance_tag_inheritance")
        _taggate = optional_paid("services.compliance_tag_dispatch_gate")
        if _taggate is not None and _taginh is not None:
            _tag_mode = _taggate._mode(db, workflow.tenant_id)
            if _tag_mode != "off":
                try:
                    _tag_violations = _taginh.check_inheritance(
                        _parent_tags,
                        version_create.graph_json,
                        child_tags_for=lambda ref: _child_compliance_tags_for(
                            db, workflow.tenant_id, ref),
                    )
                except Exception as e:  # noqa: BLE001 — resolver/DB outage must not 500 the save in shadow mode
                    logger.debug(
                        "compliance_tag_inheritance: resolver failed: %s",
                        type(e).__name__,
                    )
                    if _tag_mode == "enforce":
                        # fail closed: an unresolvable check is an unresolved child
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "compliance_tag_inheritance_failed",
                                "violations": [{
                                    "code": _taginh.UNRESOLVED_CODE,
                                    "node_id": "",
                                    "ref": "",
                                    "missing_tags": _parent_tags,
                                }][:50],
                            },
                        )
                    _tag_violations = []
                if _tag_violations:
                    if _tag_mode == "enforce":
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "compliance_tag_inheritance_failed",
                                "violations": _tag_violations[:50],
                            },
                        )
                    logger.warning(
                        "compliance_tag_inheritance: shadow violations on save "
                        "codes=%s nodes=%s",
                        sorted({str(v.get("code")) for v in _tag_violations}),
                        sorted({str(v.get("node_id")) for v in _tag_violations}),
                    )

    # FORTRESS 5.2 — compose-time policy evaluation gate. After the 4.3
    # tag-inheritance check we evaluate every active Rego policy against the
    # workflow version being saved. This is independent of whether the
    # workflow itself carries compliance_tags — tenant-subscribed policies
    # (compliance_policy_store.active_policies) apply to every save, not
    # just tagged ones. Inert (zero extra queries) when mode is 'off' or
    # the tenant has zero active-policy subscriptions.
    _fpe = optional_paid("services.compliance_policy_evaluator")
    _fps = optional_paid("services.compliance_policy_store")
    if _fpe is not None and _fps is not None:
        _fp_mode = _fpe.mode_for(db, workflow.tenant_id)
        if _fp_mode != "off":
            try:
                with db.begin_nested():
                    _fp_policies = _fps.active_policies(db, tenant_id=workflow.tenant_id)
            except Exception as e:  # noqa: BLE001 — policy-table lookup outage must not 500 the save
                logger.warning(
                    "compliance_policy_store: active_policies lookup failed: %s",
                    type(e).__name__,
                )
                if _fp_mode == "enforce":
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "compliance_policy_eval_failed",
                            "violations": [
                                _fpe._gate_error(None, None, None,
                                                 f"{type(e).__name__}: internal error"),
                            ][:50],
                        },
                    )
                _fp_policies = []
            if _fp_policies:
                _fp_input_doc = {
                    "phase": "compose",
                    "tenant_id": workflow.tenant_id,
                    "workflow": {
                        "id": workflow.id,
                        "compliance_tags": list(workflow.compliance_tags or []),
                    },
                    "graph": {
                        "nodes": list(
                            version_create.graph_json.get("nodes", [])
                        ),
                        "edges": list(
                            version_create.graph_json.get("edges", [])
                        ),
                    },
                    "declared_egress": [],
                    "storage": [],
                }
                try:
                    _fp_violations = _fpe.evaluate_policies(
                        _fp_policies, _fp_input_doc, phase="compose",
                    )
                except Exception as e:  # noqa: BLE001 — eval outage must not 500 the save
                    logger.debug(
                        "compliance_policy_evaluator: eval failed: %s",
                        type(e).__name__,
                    )
                    _fp_violations = []
                if _fp_violations:
                    if _fp_mode == "enforce":
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "error": "compliance_policy_eval_failed",
                                "violations": _fp_violations[:50],
                            },
                        )
                    logger.info(
                        "compliance_policy_evaluator: shadow violations on save "
                        "count=%d rule_ids=%s",
                        len(_fp_violations),
                        sorted({v.get("rule") for v in _fp_violations}),
                    )

    latest_version_number = db.query(func.max(models.WorkflowVersion.version_number)).execution_options(
        include_deleted=True).filter(
        models.WorkflowVersion.workflow_id == workflow.id).scalar() or 0
    new_version_number = latest_version_number + 1

    db_version = models.WorkflowVersion(
        workflow_id=workflow.id,
        created_by_user_id=user_id,
        version_number=new_version_number,
        graph_json=sanitize_graph_json(version_create.graph_json),
    )
    db.add(db_version)
    db.commit()
    db.refresh(db_version)

    workflow.latest_version_id = db_version.id
    db.add(workflow)
    db.commit()
    db.refresh(db_version)
    return db_version


def get_workflow_versions(
    db: Session,
    workflow_id: int,
    limit: int = 50,
    offset: int = 0
) -> List[models.WorkflowVersion]:
    """Retrieve a paginated list of versions for a specific workflow."""
    return (
        db.query(models.WorkflowVersion)
        .filter(models.WorkflowVersion.workflow_id == workflow_id)
        .order_by(models.WorkflowVersion.version_number.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_workflow_version_by_id(
    db: Session,
    version_id: int,
    tenant_id: int
):
    """Retrieve a single workflow version by its ID, ensuring it belongs to the correct tenant."""
    # Explicitly define the join condition to resolve ambiguity
    return db.query(models.WorkflowVersion).join(
        models.Workflow, models.WorkflowVersion.workflow_id == models.Workflow.id
    ).filter(
        models.WorkflowVersion.id == version_id,
        models.Workflow.tenant_id == tenant_id
    ).first()


def get_or_create_project_workflows_folder(
    db: Session,
    tenant_id: int,
    user_id: int
) -> models.Cog:
    """Gets or creates the project workflows folder for a tenant."""
    folder = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.name == "Workflows",
        models.Cog.owner_id == user_id,
        models.Cog.cog_type == models.CogType.MASTER_FOLDER,
        models.Cog.deleted_at.is_(None)
    ).first()

    if not folder:
        folder = models.Cog(
            tenant_id=tenant_id,
            owner_id=user_id,
            name="Workflows",
            cog_type=models.CogType.MASTER_FOLDER,
        )
        db.add(folder)
        db.commit()
        db.refresh(folder)
        # Gate 24: folder owner gets an OWNER grant.
        from resource_permissions import grant_cog_owner_if_folder
        grant_cog_owner_if_folder(db, folder)
        db.commit()

    return folder


def create_workflow_run_folder(
    db: Session,
    workflow_run: models.WorkflowRun,
    user_id: int,
    tenant_id: int
) -> models.Cog:
    """Creates a folder to store workflow run files and results."""
    folder_name = f"workflow_run_{workflow_run.id}"

    folder = models.Cog(
        tenant_id=tenant_id,
        owner_id=user_id,
        name=folder_name,
        cog_type=models.CogType.ASSET_FOLDER,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    # Gate 24: folder owner gets an OWNER grant.
    from resource_permissions import grant_cog_owner_if_folder
    grant_cog_owner_if_folder(db, folder)
    db.commit()

    return folder


def create_workflow_step_folder(
    db: Session,
    workflow_run: models.WorkflowRun,
    step_name: str,
    user_id: int,
    tenant_id: int
) -> models.Cog:
    """Creates a folder for a specific step within a workflow run."""
    folder_name = f"step_{step_name.replace('.', '_')}"

    folder = models.Cog(
        tenant_id=tenant_id,
        owner_id=user_id,
        name=folder_name,
        cog_type=models.CogType.ASSET_FOLDER,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    # Gate 24: folder owner gets an OWNER grant.
    from resource_permissions import grant_cog_owner_if_folder
    grant_cog_owner_if_folder(db, folder)
    db.commit()

    return folder
