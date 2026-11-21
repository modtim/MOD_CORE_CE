# GX.D1 (launch-completion-megaplan Group X) — workflow/node/run worker
# placement: pin a workflow's tasks to the worker (or pool of tagged
# workers) where its LLM is reachable.
#
# Shapes (all optional; either key may be absent):
#   {"worker_id": <worker_licenses.id>}   — one specific worker
#   {"worker_tag": "<hardware_tag>"}      — any worker advertising the tag
# Both keys together = AND (that worker, and it must carry the tag).
#
# Precedence (highest wins): run request > node config > workflow default.
# Resolved at DISPATCH (apply_task_placement) and enforced at CLAIM time
# (the claim SQL in routers/worker_tasks.py only lets a matching worker
# claim a stamped task — a pinned task is never silently run elsewhere).
#
# The pure functions here are testable without the app; the DB-facing
# helpers (apply_task_placement / validate_worker_placement) and the
# claim-guard SQL builder (placement_claim_guard_sql) are imported by
# routers/worker_tasks.py and services/workflow_orchestrator.py.
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

PLACEMENT_WORKER_ID = "worker_id"
PLACEMENT_WORKER_TAG = "worker_tag"

# Task-payload keys stamped by apply_task_placement. `worker_pin` is the
# worker NAME (the claim path compares worker_name strings); the integer
# id is kept in `placement` for diagnostics. Absent = unpinned.
PAYLOAD_WORKER_PIN = "worker_pin"
PAYLOAD_WORKER_PIN_TAG = "worker_pin_tag"
PAYLOAD_PLACEMENT = "placement"
PAYLOAD_WORKER_MISSING = "placement_worker_missing"


# ---------------------------------------------------------------------------
# Pure resolution
# ---------------------------------------------------------------------------

def normalize_placement(raw: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Coerce a raw placement (dict / None / garbage) into a clean
    {"worker_id": int, "worker_tag": str} (keys dropped when absent).

    Returns None for anything that is not a usable dict — a None/empty
    placement means UNPINNED, which must stay byte-for-byte the existing
    behaviour everywhere.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    out: Dict[str, Any] = {}
    # camelCase aliases: graph node data uses camelCase keys
    # (nodeDefinitionId, extraVariables, pinnedNodeVersionId, ...) — a node's
    # own placement rides there as workerId / workerTag.
    wid = raw.get(PLACEMENT_WORKER_ID, raw.get("workerId"))
    if wid is not None:
        try:
            out[PLACEMENT_WORKER_ID] = int(wid)
        except (TypeError, ValueError):
            pass
    tag = raw.get(PLACEMENT_WORKER_TAG, raw.get("workerTag"))
    if tag is not None:
        tag = str(tag).strip()
        if tag:
            out[PLACEMENT_WORKER_TAG] = tag
    return out or None


def merge_placement(*layers: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Resolve precedence across placement layers.

    The FIRST non-None layer wins WHOLESALE (a run-level worker_tag replaces
    a node-level worker_id, not merges with it) — the most specific setting
    is the one that applies, same shape as the per-node extraVariables
    precedence. Callers pass layers most-specific-first.
    """
    for layer in layers:
        norm = normalize_placement(layer)
        if norm is not None:
            return norm
    return None


def _is_placement_pinned(payload: Optional[Any]) -> bool:
    """True when a task payload carries any placement marker (set by
    apply_task_placement). Used to keep the predictive scheduler away from
    pinned tasks — pre-placement by score has no notion of the pin, so it
    would strand one. Pure: safe on non-dict payloads."""
    if not isinstance(payload, dict):
        return False
    return bool(payload.get(PAYLOAD_WORKER_PIN)
                or payload.get(PAYLOAD_WORKER_PIN_TAG)
                or payload.get(PAYLOAD_WORKER_MISSING))


def worker_tags_of(hardware_specs: Optional[Dict[str, Any]]) -> List[str]:
    """A worker's hardware tags, lowercased — the same normalisation the
    claim path (routers/worker_tasks.py) uses for gpu_variants, so a tag
    pinned at dispatch matches a tag reported at heartbeat."""
    specs = hardware_specs or {}
    tags = specs.get("hardware_tags") or []
    return [str(t).strip().lower() for t in tags if isinstance(t, str)]


def worker_matches_placement(
    placement: Optional[Dict[str, Any]],
    worker_id: Optional[int],
    hardware_specs: Optional[Dict[str, Any]],
) -> bool:
    """Does THIS worker satisfy the placement?

    AND semantics when both keys are set: the exact worker_id must match
    AND the tag must be among its hardware_tags. An empty/None placement
    matches every worker (unpinned = today's behaviour).
    """
    pl = normalize_placement(placement)
    if pl is None:
        return True
    if pl.get(PLACEMENT_WORKER_ID) is not None and pl[PLACEMENT_WORKER_ID] != worker_id:
        return False
    if pl.get(PLACEMENT_WORKER_TAG) is not None:
        if str(pl[PLACEMENT_WORKER_TAG]).strip().lower() not in worker_tags_of(hardware_specs):
            return False
    return True


def placement_claim_guard_sql() -> str:
    """The claim-time enforcement of a stamped placement (both claim
    branches in routers/worker_tasks._try_claim_task embed this).

    A task is PINNED when any of `worker_pin` / `worker_pin_tag` /
    `placement_worker_missing` is present in its payload (stamped by
    apply_task_placement at dispatch). Unpinned tasks — none of the three —
    pass through byte-for-byte the existing behaviour.

    A pinned task is claimable ONLY by a matching worker, never silently run
    elsewhere:
      - `worker_pin` present  -> the worker_name must equal it;
      - `worker_pin_tag` present -> the worker's hardware_tags must include it;
      - `placement_worker_missing` present -> NO worker can claim (the pinned
        worker_id has no live row); the task stays queued until it reappears,
        and the unrunnable sweep names the missing id in its reason.

    `:worker` (worker_name) and `:worker_hw_tags` are already bound by both
    claim queries.
    """
    return """
        AND (
            (task_payload->>'worker_pin' IS NULL
             AND task_payload->>'worker_pin_tag' IS NULL
             AND task_payload->>'placement_worker_missing' IS NULL)
            OR (
                (task_payload->>'worker_pin' IS NULL
                 OR task_payload->>'worker_pin' = :worker)
                AND (task_payload->>'worker_pin_tag' IS NULL
                     OR lower(task_payload->>'worker_pin_tag') = ANY(:worker_hw_tags))
                AND task_payload->>'placement_worker_missing' IS NULL
            )
        )
    """


def pinned_shortfall_reason(
    payload: Optional[Dict[str, Any]],
    active_workers: List[Any],
) -> Optional[str]:
    """Why a PINNED task is currently unrunnable, for the
    unrunnable_tasks_sweep banner; None when it is not pinned or a
    matching active worker exists.

    `active_workers` are worker rows (ORM or plain objects with .id /
    .worker_name / .hardware_specs). Pure — the sweep feeds it the rows it
    already loads, so no second session is opened.
    """
    if not isinstance(payload, dict):
        return None
    pin_name = payload.get(PAYLOAD_WORKER_PIN)
    pin_tag = payload.get(PAYLOAD_WORKER_PIN_TAG)
    missing_id = payload.get(PAYLOAD_WORKER_MISSING)
    if not (pin_name or pin_tag or missing_id):
        return None
    if missing_id:
        return (
            f"Waiting for pinned worker (id {missing_id}) to rejoin the "
            "fleet — the task will only run on that worker."
        )
    for w in active_workers:
        pl = {PLACEMENT_WORKER_TAG: pin_tag} if pin_tag else None
        if not worker_matches_placement(pl, getattr(w, "id", None),
                                        getattr(w, "hardware_specs", None)):
            continue
        if pin_name is not None and getattr(w, "worker_name", None) != pin_name:
            continue
        return None
    bits = []
    if pin_name:
        bits.append(f"worker '{pin_name}'")
    if pin_tag:
        bits.append(f"hardware tag '{pin_tag}'")
    return (
        "Waiting for a worker matching the placement pin "
        f"({' and '.join(bits)}) — no online worker matches; a pinned "
        "task is never run elsewhere."
    )


# ---------------------------------------------------------------------------
# DB-facing helpers
# ---------------------------------------------------------------------------

def effective_node_placement(
    db,
    run,
    node_data: Optional[Dict[str, Any]],
    workflow,
) -> Optional[Dict[str, Any]]:
    """Resolve the effective placement for ONE node of a run — the single
    place precedence (run request > node config > workflow default) is read.

    The run request's placement rides the persisted run conf
    (`WorkflowRun.parameters["placement"]`); the node's own config rides the
    (already enriched) node data as `placement` (or the camelCase
    `workerId` / `workerTag` spellings the WB graph uses); the workflow
    default rides `Workflow.placement`. Returns the resolved dict or None
    (unpinned).
    """
    run_placement = None
    params = getattr(run, "parameters", None) or {}
    if isinstance(params, dict):
        run_placement = params.get("placement")
    node_placement = _node_placement_from_data(node_data)
    workflow_placement = getattr(workflow, "placement", None)
    return merge_placement(run_placement, node_placement, workflow_placement)


def _node_placement_from_data(node_data: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Extract the node-level placement from a node's graph data.

    The node may declare it EITHER as a `placement` object ({"worker_id"} /
    {"worker_tag"}) OR with the camelCase `workerId` / `workerTag` keys the
    WB graph uses for other per-node settings. normalize_placement accepts
    both spellings, so feed it whichever is present.
    """
    if not isinstance(node_data, dict):
        return None
    if isinstance(node_data.get("placement"), dict):
        return normalize_placement(node_data.get("placement"))
    if "workerId" in node_data or "workerTag" in node_data:
        return normalize_placement(node_data)
    return None


def _worker_name_by_id(db, worker_id: int) -> Optional[str]:
    from models import WorkerLicense
    row = db.query(WorkerLicense.worker_name).filter(
        WorkerLicense.id == int(worker_id)).first()
    return row[0] if row else None


def _load_run_and_workflow(db, workflow_run_id: int):
    """The run + its workflow in ONE query (join, not two round-trips) —
    this runs per enqueued task, so it must be cheap. Returns (run, wf) or
    (None, None) when the run row is gone."""
    from models import WorkflowRun, Workflow
    row = db.query(WorkflowRun, Workflow.placement).outerjoin(
        Workflow, Workflow.id == WorkflowRun.workflow_id
    ).filter(WorkflowRun.id == workflow_run_id).first()
    if row is None:
        return None, None
    run, wf_placement = row
    return run, type("Wf", (), {"placement": wf_placement})()


def apply_task_placement(
    db,
    payload: Dict[str, Any],
    *,
    workflow_run_id: Optional[int] = None,
    node_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stamp a task payload with the effective placement for one node.

    `workflow_run_id` is the run the task belongs to (its persisted
    `parameters["placement"]` carries the run request's override; the run's
    workflow's `placement` column carries the workflow default);
    `node_data` is the node's (enriched) graph data. Precedence run > node
    > workflow is resolved by effective_node_placement. Returns the payload
    UNCHANGED (same object) when nothing pins it — unpinned tasks stay
    byte-identical to today. `workflow_run_id` of None (non-workflow task)
    means no stamp.
    """
    if not workflow_run_id:
        return payload
    run, workflow = _load_run_and_workflow(db, workflow_run_id)
    if run is None:
        return payload
    effective = effective_node_placement(db, run, node_data, workflow)
    if effective is None:
        return payload
    stamped = dict(payload)
    stamped[PAYLOAD_PLACEMENT] = effective
    wid = effective.get(PLACEMENT_WORKER_ID)
    if wid is not None:
        name = _worker_name_by_id(db, wid)
        if name:
            stamped[PAYLOAD_WORKER_PIN] = name
        else:
            # The worker row is gone (revoked/decommissioned). Keep the id
            # so the sweep can name it in the unrunnable reason; the claim
            # guard (no name to match) keeps the task queued.
            stamped[PAYLOAD_WORKER_MISSING] = int(wid)
    tag = effective.get(PLACEMENT_WORKER_TAG)
    if tag:
        stamped[PAYLOAD_WORKER_PIN_TAG] = str(tag).strip().lower()
    return stamped


def validate_worker_placement(
    db,
    placement: Optional[Any],
    tenant_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a placement before persisting it.

    Returns (normalized_placement, None) on success — a None placement
    clears the pin — or (normalized_placement, error_message) when a
    named worker does not exist or is not owned by / assigned to THIS
    tenant (the caller 404s on the error: other tenants' workers must not
    be referenceable by id, mirroring the tenant-isolation stance of the
    claim path).
    """
    pl = normalize_placement(placement)
    if pl is None:
        return None, None
    wid = pl.get(PLACEMENT_WORKER_ID)
    if wid is None:
        return pl, None
    from models import WorkerLicense, WorkerOwnerType
    w = db.query(WorkerLicense).filter(WorkerLicense.id == int(wid)).first()
    if w is None:
        return pl, f"worker id {wid} does not exist"
    ot = w.owner_type
    assigned = list(w.assigned_tenant_ids or [])
    if ot == WorkerOwnerType.TENANT_OWNED:
        ok = w.owner_tenant_id == tenant_id
    elif ot == WorkerOwnerType.PLATFORM_POOL:
        ok = (not assigned) or (tenant_id in assigned)
    elif ot == WorkerOwnerType.PLATFORM_ASSIGNED:
        ok = tenant_id in assigned
    else:
        ok = False  # PLATFORM_ONLY (and unknown types) serve no tenant
    if not ok:
        return pl, f"worker id {wid} is not available to this tenant"
    return pl, None
