"""expr-3.6b (workflow-expressiveness-primitives) — one-click subgraph extract.

Turns ONE equivalence-class member into a NEW draft workflow: the caller's
workflow, restricted to the node set the `subgraph_equivalence_index` row
names for `canonical_hash`, plus only the edges whose endpoints are BOTH in
that set. The source workflow is never mutated and never hard-deleted (ISO
27001) — this writes a brand-new workflow + version through the normal CRUD
create path so versioning/defaults/sanitisation apply exactly as they do for
a builder SAVE.

Tenant isolation: the index lookup and the graph load are both tenant-scoped
(the graph load reuses `subgraph_equivalence_analyzer._graph_json`, whose
tenant filter is IN THE SQL), so another tenant's workflow_id can never be
read or extracted through this service — the caller gets a LookupError, which
the router renders as a 404.

No new env vars, no new columns: everything here is derived from existing
state.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

from sqlalchemy import text as satext
from sqlalchemy.orm import Session

import models
import schemas
from crud import workflow_cud as wf_crud

from services.subgraph_equivalence_analyzer import _graph_json

logger = logging.getLogger(__name__)

TABLE = "subgraph_equivalence_index"
WORKFLOW_NAME_MAX = 255


def _index_node_ids(db: Session, tenant_id: int, workflow_id: int,
                    canonical_hash: str) -> List[str]:
    """This workflow's node_ids for `canonical_hash` (live rows only).

    Mirrors the SELECT in routers/workflow_extraction_suggestions.py
    `_fetch_suggestions` step 2a: same tenant, same workflow, same hash,
    `deleted_at IS NULL`. Raises LookupError when no such row exists (the
    router turns that into a 404).
    """
    rows = db.execute(satext(f"""
        SELECT node_ids
        FROM {TABLE}
        WHERE tenant_id = :t
          AND deleted_at IS NULL
          AND workflow_id = :wid
          AND canonical_hash = :h
    """), {"t": tenant_id, "wid": workflow_id, "h": canonical_hash}).fetchall()
    if not rows:
        raise LookupError("no live index row for (tenant, workflow, hash)")
    node_ids = rows[0][0]
    if not node_ids:
        raise LookupError("index row names no nodes")
    return [str(n) for n in node_ids]


def _extract_graph(graph: Any, node_ids: List[str]) -> Dict[str, Any]:
    """Deep-copy the subgraph: chosen nodes + edges with BOTH ends chosen.

    Node/edge dicts are deep-copied so the new workflow's graph can never
    alias the source's JSONB; node config is kept as-is (secrets are stored
    as references, never inline values, so there is nothing to redact).
    """
    keep = set(node_ids)
    nodes = [copy.deepcopy(n) for n in (graph.get("nodes") or [])
             if n.get("id") in keep]
    edges = [copy.deepcopy(e) for e in (graph.get("edges") or [])
             if e.get("source") in keep and e.get("target") in keep]
    out = copy.deepcopy(graph)
    out["nodes"] = nodes
    out["edges"] = edges
    return out


def _name(source_name: str, node_count: int) -> str:
    label = f"{source_name} — extracted ({node_count} nodes)"
    return label[:WORKFLOW_NAME_MAX]


def extract_subgraph_workflow(db: Session, *, tenant_id: int,
                              user: models.User, workflow_id: int,
                              canonical_hash: str) -> Dict[str, Any]:
    """Create the extracted draft workflow; return its summary.

    Raises LookupError when the (tenant, workflow, canonical_hash) index row
    or a live graph for the workflow is missing.
    """
    node_ids = _index_node_ids(db, tenant_id, workflow_id, canonical_hash)

    graph, _version_id = _graph_json(db, tenant_id, workflow_id)
    if not graph:
        raise LookupError("no live workflow version graph")

    source = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id,
                                        tenant_id=tenant_id)
    if source is None:
        raise LookupError("workflow not visible to tenant")

    subgraph = _extract_graph(graph, node_ids)
    name = _name(source.name, len(subgraph["nodes"]))

    db_workflow = wf_crud.create_workflow(
        db,
        workflow=schemas.WorkflowCreate(name=name),
        user_id=user.id,
        tenant_id=tenant_id,
    )
    wf_crud.create_workflow_version(
        db,
        workflow=db_workflow,
        version_create=schemas.WorkflowVersionCreate(graph_json=subgraph),
        user_id=user.id,
    )

    return {
        "workflow_id": db_workflow.id,
        "name": db_workflow.name,
        "node_count": len(subgraph["nodes"]),
    }
