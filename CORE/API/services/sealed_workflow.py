"""services/sealed_workflow.py — GG.8 (launch-completion-megaplan, gg-commerce).

Enforcement for an INSTALLED copy of a workflow whose source listing carries
`licence == 'sealed'` (run-only app; the buyer may execute the pipeline but
must not read or edit the graph).

GG.6 `install_workflow` clones the snapshot into the buyer's tenant and stamps
`Workflow.source_listing_id` / `source_version`. The `licence` column on the
source `WorkflowListing` (default 'open', 'sealed' for restricted listings unless the
publisher opts out) was set but never enforced. This module is that ONE shared
helper, applied across the graph read/export/edit endpoints:

    is_sealed(db, workflow)            -> bool
    require_graph_readable(db, wf, u)  -> raises HTTPException(403, "sealed")

The PUBLISHER's tenant (the listing's owner) and PLATFORM ADMINS are exempt.
The run path (POST /workflow-versions/{id}/run) deliberately reads the stored
`graph_json` server-side via crud and is NOT gated here — a sealed buyer can
still run the workflow; only the graph is hidden.

GG.8c sealed-storage (this slice): `install_workflow` now stores ONLY
`source_listing_id`/`source_version` + the input-node param defaults on a
sealed buyer copy — `WorkflowVersion.graph_json` is an empty placeholder
(`{"nodes": [], "edges": [], "sealed_inputs": [...]}`), never the node graph.
`expand_for_run` resolves the publisher's listing snapshot server-side and
overlays the buyer's `sealed_inputs` at run time; the expanded graph is passed
to the run conf but NEVER written back to the buyer's rows. Enforcement of the
403 surface and the run path are complete.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def is_sealed(db: Session, workflow: "models.Workflow") -> bool:
    """True if `workflow` is an installed copy of a listing whose
    `licence == 'sealed'`. Non-installed workflows (`source_listing_id`
    NULL) and listings whose licence is 'open' return False. A listing that
    was later hard/soft-deleted still seals the copy already installed from
    it (the source_version pins what was bought)."""
    if workflow is None:
        return False
    source_listing_id = getattr(workflow, "source_listing_id", None)
    if not source_listing_id:
        return False
    listing = (
        db.query(models.WorkflowListing)
        .filter(models.WorkflowListing.id == source_listing_id)
        .first()
    )
    if listing is None:
        return False
    return (listing.licence or "open").lower() == "sealed"


def _is_exempt(db: Session, workflow: "models.Workflow", user: "models.User") -> bool:
    """Publisher-tenant + platform-admin exemption.

    A platform admin (`is_platform_admin`) is exempt so support can inspect a
    sealed graph. Any user in the LISTING PUBLISHER's tenant is exempt — the
    publisher obviously can read their own pipeline, and the buyer's copy lives
    in a DIFFERENT tenant, so this never grants the buyer's tenant access.
    """
    if user is None:
        return False
    if getattr(user, "is_platform_admin", False):
        return True
    source_listing_id = getattr(workflow, "source_listing_id", None)
    if not source_listing_id:
        return False
    publisher_tenant_id = (
        db.query(models.WorkflowListing.publisher_tenant_id)
        .filter(models.WorkflowListing.id == source_listing_id)
        .scalar()
    )
    return (
        publisher_tenant_id is not None
        and getattr(user, "tenant_id", None) is not None
        and publisher_tenant_id == user.tenant_id
    )


def require_graph_readable(db: Session, workflow: "models.Workflow", user: "models.User") -> None:
    """Raise 403 "sealed" if `workflow` is a sealed installed copy and
    `user` is neither in the publisher tenant nor a platform admin.
    No-op for 'open' listings, non-installed workflows, and exempt users.

    Callers invoke this AFTER their existing tenant + access checks, at the top
    of a graph read/export/edit handler. The run path does NOT call this."""
    if workflow is None:
        return
    if not is_sealed(db, workflow):
        return
    if _is_exempt(db, workflow, user):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="sealed")


# Per-node identifying fields stripped for a sealed non-exempt buyer across the
# run/read surfaces (GG.8b). node_kind + names reveal WHICH pipeline steps ran;
# processing_image reveals the container that implements each step; parameters /
# configuration carry the node SETTINGS. The opaque node_id + status + outputs +
# timing are left intact so the buyer can still watch a run and collect results.
_NODE_IDENTIFYING_KEYS = (
    "node_name", "name", "label", "node_kind",
    "processing_image", "parameters", "configuration",
)


def should_redact_graph(db: Session, workflow: "models.Workflow", user: "models.User") -> bool:
    """True when `user` is a sealed, NON-exempt buyer of `workflow` — i.e. the
    graph must be REDACTED (not 403'd) on run/read surfaces that still let a
    runner watch status and collect outputs. The inverse of the require_* gate:
    same is_sealed + exemption test, returned as a bool instead of raising.

    Run read handlers resolve `workflow = run.workflow` and call this once, then
    blank identifying fields per node via `redact_node_identifying_fields`."""
    if workflow is None or not is_sealed(db, workflow):
        return False
    return not _is_exempt(db, workflow, user)


def redact_node_identifying_fields(node: dict) -> dict:
    """Blank graph-identifying fields on a per-node response dict IN PLACE and
    return it. Non-dict input is returned unchanged. The opaque `node_id` /
    `graph_node_id` keys are kept so status/outputs stay keyed to a node."""
    if not isinstance(node, dict):
        return node
    for key in _NODE_IDENTIFYING_KEYS:
        if key in node:
            node[key] = None
    wt = node.get("worker_task")
    if isinstance(wt, dict):
        wt["processing_image"] = None
    return node


# ---------------------------------------------------------------------------
# GG.8c sealed-storage: server-side expansion at run time
# ---------------------------------------------------------------------------

def _is_input_node(node_id: str, edges: list) -> bool:
    """True if no edge targets `node_id` (an input/root node). Edges use the
    canvas `source`/`target` convention, which equals the graph_node_id."""
    for edge in edges or []:
        if isinstance(edge, dict) and edge.get("target") == node_id:
            return False
    return True


def _snapshot_defaults_to_sealed_inputs(snapshot_nodes: list, edges: list) -> list:
    """Derive the buyer-editable input params for a sealed listing.

    The listing has no explicit exposed-inputs list (that concept lives on
    `RunSurfaceLink.exposed_inputs` for public run links, not on listings).
    Per the owner spec fallback: keep the INPUT nodes' (roots) `configuration`
    defaults. Each entry is `{"node": <graph_node_id>, "configuration": {...}}`
    so a buyer's override can be applied per input node at expand time."""
    inputs = []
    for node in snapshot_nodes or []:
        if not isinstance(node, dict):
            continue
        gid = node.get("graph_node_id") or node.get("id")
        if not gid:
            continue
        if not _is_input_node(gid, edges):
            continue
        cfg = node.get("configuration")
        if not isinstance(cfg, dict):
            cfg = {}
        inputs.append({"node": gid, "configuration": cfg})
    return inputs


def sealed_inputs_from_placeholder(graph_json: dict) -> list:
    """Read the buyer's stored input-param defaults (set at install)."""
    if not isinstance(graph_json, dict):
        return []
    out = graph_json.get("sealed_inputs")
    return out if isinstance(out, list) else []


def _apply_sealed_inputs(nodes: list, sealed_inputs: list, input_ids: set) -> None:
    """Overlay the buyer's per-INPUT-node parameter values onto the expanded
    snapshot nodes (in place). Only entries whose node is an INPUT (root) node
    are applied — the "exposed params" surface is the input nodes, and
    restricting the overlay to them means a tampered `sealed_inputs` row can't
    retune a non-input (pipeline) node. A missing entry or config leaves the
    publisher's default untouched; an explicit value (including falsy) wins."""
    for entry in sealed_inputs or []:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("node")
        if gid is None or gid not in input_ids:
            continue
        cfg = entry.get("configuration")
        if not isinstance(cfg, dict):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("id") != gid:
                continue
            data = node.get("data")
            if not isinstance(data, dict):
                data = {}
                node["data"] = data
            # The expanded node's settings live under data["configuration"]
            # (mirroring the snapshot node shape) — merge the buyer's overrides
            # into that dict, never flattening them onto `data`.
            conf = data.get("configuration")
            if not isinstance(conf, dict):
                conf = {}
                data["configuration"] = conf
            for key, value in cfg.items():
                conf[key] = value
            break


def expand_for_run(db: Session, workflow_version: "models.WorkflowVersion") -> dict:
    """Expand a SEALED buyer's workflow version into the executable graph.

    Resolves the publisher's listing snapshot for
    `(source_listing_id, source_version)` and returns it in the canvas node
    shape the orchestrator consumes — `{"id", "data": {node_kind, ...}}` —
    with the buyer's `sealed_inputs` (input-node param overrides) applied.

    The returned dict is a FRESH, in-memory expansion: it is never written back
    to `workflow_version.graph_json` or any `WorkflowNode.configuration`.
    Existing sealed buyers installed under GG.6 (full-copy rows) are untouched —
    the read guards already protect them, and this only ever produces an
    expansion, never a mutation.

    Returns `{"nodes": [], "edges": []}` (the stored placeholder shape) when
    the version is not a sealed install or its source listing/version no longer
    resolves, so the run fails cleanly with "no active version with graph".
    """
    if workflow_version is None:
        return {"nodes": [], "edges": []}
    workflow = (
        db.query(models.Workflow)
        .filter(models.Workflow.id == workflow_version.workflow_id)
        .first()
    )
    if workflow is None or not is_sealed(db, workflow):
        return {"nodes": [], "edges": []}
    source_version = getattr(workflow, "source_version", None) or 1
    listing = (
        db.query(models.WorkflowListing)
        .filter(
            models.WorkflowListing.id == workflow.source_listing_id,
            models.WorkflowListing.version == source_version,
        )
        .first()
    )
    if listing is None:
        return {"nodes": [], "edges": []}
    snapshot = listing.snapshot_json or {}
    edges = list(snapshot.get("edges", []) or [])
    node_ids = set()
    nodes = []
    for snode in snapshot.get("nodes", []) or []:
        if not isinstance(snode, dict):
            continue
        gid = snode.get("graph_node_id") or snode.get("id")
        if not gid:
            continue
        node_ids.add(gid)
        data = {
            "node_kind": snode.get("node_kind"),
            "configuration": snode.get("configuration"),
        }
        nodes.append({"id": gid, "data": data})
    input_ids = {gid for gid in node_ids if _is_input_node(gid, edges)}
    sealed_inputs = sealed_inputs_from_placeholder(workflow_version.graph_json or {})
    _apply_sealed_inputs(nodes, sealed_inputs, input_ids)
    return {"nodes": nodes, "edges": edges}
