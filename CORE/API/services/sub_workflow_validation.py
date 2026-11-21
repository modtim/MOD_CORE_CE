"""SZ 219 — fail-closed sub-workflow composition gate on version save.

``validate_sub_workflow_composition`` is called by
``POST /v1/road/{id}/versions`` BEFORE any ``WorkflowVersion`` row is
created, so a graph that would make the sub-workflow ref graph cyclic, too
deep, or pointed at an unknown / foreign-tenant workflow is rejected 422
instead of being saved and discovered at run time.

Pure graph work is delegated to ``utils/subworkflow_refs`` (already shipped,
previously unused). This module adds the DB half: tenant-scoped, live-only
ref resolution and the latest-version graph loader.

Node-kind detection mirrors ``services/live_debugger._node_kind``'s graph-data
branch (``data["node_kind"]``); that helper is not imported because it also
derefs a ``WorkflowNode`` row (which does not exist yet for a graph being
saved) and importing ``live_debugger`` here would drag the run/debug stack
into the save path.
"""

from __future__ import annotations

from typing import Optional

import crud
import models
from services import platform_settings as psr
from utils import subworkflow_refs

SETTING_MAX_DEPTH = "workflows.sub_workflow_max_depth"


def _validate_max_depth(value: int) -> None:
    v = int(value)
    if v < 1 or v > 50:
        raise ValueError(
            f"workflows.sub_workflow_max_depth must be 1..50, got {value!r}"
        )


psr.register(
    SETTING_MAX_DEPTH,
    int,
    10,
    group="workflows",
    validator=_validate_max_depth,
    description="SZ 219 — longest allowed sub-workflow ref chain (root "
                "workflow -> sub_workflow nodes). A version save whose "
                "composition is deeper than this is rejected 422. Default "
                "10, min 1, max 50.",
)


class SubWorkflowValidationError(ValueError):
    """Fail-closed composition rejection; ``str(e)`` is the 422 detail."""


def _node_kind(data) -> Optional[str]:
    """Same graph-data branch as live_debugger._node_kind (3 lines)."""
    if not isinstance(data, dict):
        return None
    kind = data.get("node_kind")
    if isinstance(kind, str) and kind.strip():
        return kind
    return None


def _ref_key(ref) -> Optional[subworkflow_refs.RefKey]:
    """Normalise a ``workflow_ref`` bag to a ref key, or None if malformed."""
    if not isinstance(ref, dict):
        return None
    wid = ref.get("workflow_id")
    if isinstance(wid, int) and not isinstance(wid, bool):
        return wid
    ts = ref.get("tenant_slug")
    ws = ref.get("workflow_slug")
    if isinstance(ts, str) and isinstance(ws, str) and ts and ws:
        return (ts, ws)
    return None


def _short(key: subworkflow_refs.RefKey) -> str:
    return subworkflow_refs._key_to_short(key)


def _resolve_ref_key(db, tenant_id: int, key: subworkflow_refs.RefKey) -> Optional[int]:
    """Resolve a ref key to a live Workflow id IN ``tenant_id``.

    Cross-tenant sub-workflows are not supported yet, so a foreign (or
    soft-deleted, or unknown) ref resolves to None — the caller folds every
    None into ONE identical message, so nothing here is an existence oracle.
    This codebase has no dedicated slug columns, so a slug ref's tenant is
    ``Tenant.name`` (same convention as services/mfa_recovery) and its
    workflow is matched on ``Workflow.name``.
    """
    if isinstance(key, int):
        wf = crud.get_workflow_by_id(db, workflow_id=key, tenant_id=tenant_id)
        return wf.id if wf else None
    tenant_slug, workflow_slug = key
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
    return wf.id if wf else None


def _latest_graph_json(db, workflow_id: int):
    """graph_json of the newest LIVE WorkflowVersion, or None."""
    v = (
        db.query(models.WorkflowVersion)
        .filter(
            models.WorkflowVersion.workflow_id == workflow_id,
            models.WorkflowVersion.deleted_at.is_(None),
        )
        .order_by(models.WorkflowVersion.version_number.desc())
        .first()
    )
    return v.graph_json if v is not None else None


def validate_sub_workflow_composition(
    db, tenant_id: int, workflow_id: int, graph_json: dict
) -> None:
    """Raise ``SubWorkflowValidationError`` if this graph is not composable.

    Fast no-op (zero DB queries) for graphs with no sub_workflow nodes.
    """
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else None
    if not isinstance(nodes, list):
        return

    # 1 + 2. every sub_workflow node carries an extractable, resolvable ref
    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if _node_kind(data) != "sub_workflow":
            continue
        node_id = node.get("id") or f"#{idx}"
        key = _ref_key(data.get("workflow_ref"))
        if key is None:
            raise SubWorkflowValidationError(
                f"sub_workflow node {node_id} has no workflow_ref"
            )
        if _resolve_ref_key(db, tenant_id, key) is None:
            raise SubWorkflowValidationError(
                f"unknown sub-workflow ref {_short(key)}"
            )

    if not subworkflow_refs.extract_refs(graph_json):
        return

    cap = int(psr.get_setting(SETTING_MAX_DEPTH, db=db))

    def load_refs(key: subworkflow_refs.RefKey):
        if key == workflow_id:
            # the graph BEING SAVED, so a save that introduces a cycle is caught
            return subworkflow_refs.extract_refs(graph_json)
        wid = _resolve_ref_key(db, tenant_id, key)
        if wid is None:
            raise LookupError(f"unknown sub-workflow ref {_short(key)}")
        graph = _latest_graph_json(db, wid)
        return subworkflow_refs.extract_refs(graph) if graph else []

    try:
        subworkflow_refs.check_composition(
            workflow_id, load_refs, max_depth_cap=cap
        )
    except subworkflow_refs.CycleError as e:
        raise SubWorkflowValidationError(
            "sub-workflow cycle: "
            + " -> ".join(_short(k) for k in e.path)
        ) from e
    except subworkflow_refs.DepthExceededError as e:
        raise SubWorkflowValidationError(
            f"sub-workflow depth {e.depth} exceeds cap {e.cap}"
        ) from e
    except LookupError as e:
        raise SubWorkflowValidationError(str(e)) from e
