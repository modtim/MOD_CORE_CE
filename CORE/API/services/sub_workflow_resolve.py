"""workflow-expressiveness-primitives 2.1a — compose-time sub_workflow version resolution.

``resolve_sub_workflow_refs`` turns the abstract ``data.workflow_ref`` bag on every
``sub_workflow`` node of a graph into a CONCRETE ``WorkflowVersion`` stamp
(``resolved_workflow_version_id`` + ``resolved_version_label``), so the parent
workflow can be refused at compose time when a reference does not resolve
(spec scenario "Sub-workflow reference resolves at compose time").

Service layer only: no router wiring, no orchestrator dispatch, no save-path
integration (that is 2.1b). Graph work is pure; the DB half is tenant-scoped and
live-only.

Reuse (imported, never copied):
    services.sub_workflow_validation  -> _node_kind, _ref_key, _resolve_ref_key
    utils.workflow_version_resolver   -> resolve (raises ValueError on a bad
                                         constraint), parse_version
"""

from __future__ import annotations

import copy
from typing import Optional

import models
from services import sub_workflow_validation as svr
from utils import workflow_version_resolver as wvr

DEFAULT_CONSTRAINT = "latest"

CODE_UNRESOLVED = "unresolved_ref"
CODE_CROSS_TENANT = "cross_tenant_share_required"
CODE_NO_MATCH = "no_matching_version"
CODE_INVALID = "invalid_constraint"


class SubWorkflowResolveError(ValueError):
    """Fail-closed compose-time rejection of one sub_workflow node.

    ``code`` is one of ``unresolved_ref`` / ``cross_tenant_share_required`` /
    ``no_matching_version`` / ``invalid_constraint``; ``node_id`` is the offending
    node's id. ``str(e)`` is ``"<code>: sub_workflow node <node_id>"``.
    """

    def __init__(self, code: str, node_id) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__(f"{code}: sub_workflow node {node_id}")


def _version_label(row) -> str:
    """Published label of a WorkflowVersion: graph_json["semver"] when it parses, else "<n>.0.0".

    There is no semver column on WorkflowVersion, so the label is whatever the
    publisher wrote into the graph, falling back to the version_number.
    """
    graph = row.graph_json
    if isinstance(graph, dict):
        sem = graph.get("semver")
        if isinstance(sem, str) and wvr.parse_version(sem) is not None:
            return sem
    return f"{row.version_number}.0.0"


def _tenant_name(db, tenant_id: int) -> Optional[str]:
    """``Tenant.name`` of the calling tenant (the slug-ref tenant convention)."""
    row = (
        db.query(models.Tenant)
        .filter(models.Tenant.id == tenant_id)
        .first()
    )
    return row.name if row is not None else None


def _live_versions(db, tenant_id: int, workflow_id: int) -> list:
    """Live WorkflowVersion rows for ``workflow_id``, but ONLY the caller's tenant's.

    ``WorkflowVersion`` has no tenant column, so the tenant filter is applied
    through the owning ``Workflow.tenant_id`` (joined). ``crud.get_workflow_by_id``
    is tenant-unscoped, so an int ref to someone else's workflow would otherwise
    leak their versions — the join is what keeps this an honest tenant filter.
    """
    return (
        db.query(models.WorkflowVersion)
        .join(models.WorkflowVersion.workflow)
        .filter(
            models.WorkflowVersion.workflow_id == workflow_id,
            models.WorkflowVersion.deleted_at.is_(None),
            models.Workflow.tenant_id == tenant_id,
        )
        .order_by(models.WorkflowVersion.version_number.desc())
        .all()
    )


def _resolve_one(db, tenant_id: int, tenant_name: Optional[str], node: dict) -> None:
    """Stamp one sub_workflow node, or raise SubWorkflowResolveError."""
    data = node["data"]
    ref = data.get("workflow_ref")
    key = svr._ref_key(ref)
    if key is None:
        raise SubWorkflowResolveError(CODE_UNRESOLVED, node.get("id"))

    # Cross-tenant slug refs are refused WITHOUT looking the foreign tenant up, so
    # nothing here is an existence oracle for a tenant that does exist.
    if isinstance(key, tuple) and key[0] != tenant_name:
        raise SubWorkflowResolveError(CODE_CROSS_TENANT, node.get("id"))

    workflow_id = svr._resolve_ref_key(db, tenant_id, key)
    if workflow_id is None:
        raise SubWorkflowResolveError(CODE_UNRESOLVED, node.get("id"))

    constraint = data.get("version_constraint")
    if not isinstance(constraint, str) or not constraint.strip():
        constraint = DEFAULT_CONSTRAINT

    rows = _live_versions(db, tenant_id, workflow_id)
    labels = [_version_label(row) for row in rows]
    try:
        winner = wvr.resolve(constraint, labels)
    except ValueError:
        raise SubWorkflowResolveError(CODE_INVALID, node.get("id"))
    if winner is None:
        raise SubWorkflowResolveError(CODE_NO_MATCH, node.get("id"))

    row = next(r for r, label in zip(rows, labels) if label == winner)
    data["resolved_workflow_version_id"] = row.id
    data["resolved_version_label"] = winner


def resolve_sub_workflow_refs(db, tenant_id: int, graph_json: dict) -> dict:
    """Return a DEEP COPY of ``graph_json`` with every sub_workflow node stamped.

    Never mutates the input. Fast no-op (zero DB queries) for graphs with no
    sub_workflow nodes. Raises ``SubWorkflowResolveError`` for the first node
    whose reference does not resolve.
    """
    out = copy.deepcopy(graph_json)
    nodes = out.get("nodes") if isinstance(out, dict) else None
    if not isinstance(nodes, list):
        return out

    sub_nodes = [
        n for n in nodes
        if isinstance(n, dict)
        and svr._node_kind(n.get("data")) == "sub_workflow"
    ]
    if not sub_nodes:
        return out

    tenant_name = _tenant_name(db, tenant_id)
    for node in sub_nodes:
        _resolve_one(db, tenant_id, tenant_name, node)
    return out
