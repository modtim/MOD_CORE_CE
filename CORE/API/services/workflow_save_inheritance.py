"""workflow-expressiveness-primitives 3.1/3.2/3.3 — save-path adapter slice.

The save/version-create endpoint (workflow save router) owns catalog loading
and the client payload; the columns `extends_workflow_id` / `implements`
exist only after migration k, so this adapter takes both as ARGUMENTS —
whatever the client sent — and never reads those columns.

Single level only in this slice: the parent's OWN `extends` is NOT followed
(multi-level chains are a later task). The parent graph is the parent
workflow's latest NON-DELETED version's `graph_json` (its `latest_version_id`
when that version is alive, else the highest-numbered live version).

Intended caller: the workflow save / version-create endpoint, e.g.

    result = prepare_workflow_save(
        db, tenant_id=tenant_id, workflow_id=wf.id, graph_json=body.graph_json,
        extends_workflow_id=body.extends_workflow_id,
        implements=body.implements, node_catalog=node_catalog)

Refuse the save iff `result["ok"]` is False; store `result["resolved_graph"]`
as the new version's graph when it is True.
"""
from __future__ import annotations

import copy
from product_names import themed_logger
from typing import Any, Dict, List

from sqlalchemy.exc import SQLAlchemyError

from models import Workflow, WorkflowVersion
from services.workflow_inheritance import (
    ExtendsResolutionError,
    resolve_and_validate,
)
from services.workflow_interfaces import (
    InterfaceSpecError,
    parse_interface,
    validate_implements,
)

logger = themed_logger(__name__, 'workflows')

_SCHEMA_KEYWORD_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


def _fragment_type(fragment: Any, key: str) -> Any:
    """One port's type out of a schema fragment (mirrors interfaces)."""
    if not isinstance(fragment, dict):
        return None
    value = fragment.get(key)
    if isinstance(value, str) and value.strip():
        return value
    value = fragment.get("type")
    if isinstance(value, str) and value.strip() \
            and value.strip().lower() not in _SCHEMA_KEYWORD_TYPES:
        return value
    return None


def _definition_of(node: Any, catalog: Dict[Any, Any],
                   kind_index: Dict[str, Any]) -> Any:
    """Graph node -> its catalog NodeDefinition (id ref, then node_kind)."""
    if not isinstance(node, dict):
        return None
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    for key in ("node_definition_id", "definition_id", "node_definition_id_key"):
        ref = node.get(key, data.get(key))
        if ref is None:
            continue
        if ref in catalog:
            return catalog[ref]
        try:
            as_int = int(ref)  # type: ignore
        except (TypeError, ValueError):
            continue
        if as_int in catalog:
            return catalog[as_int]
    kind = node.get("node_kind") or data.get("node_kind") or node.get("type")
    if kind:
        return kind_index.get(str(kind))
    return None


def _make_port_lookup(graphs: List[Any], catalog: Any):
    """port_type_lookup for check_override_port_types, from the catalog.

    Handle names come from the edges of the graphs about to be resolved
    (parent + child): every handle an edge uses on a node maps to that
    node's definition type for that side. Unknown handle / unknown type ->
    absent -> None -> wildcard, matching the existing enforcement semantics.
    """
    kind_index: Dict[str, Any] = {}
    for nd in (catalog or {}).values():
        kind = getattr(nd, "node_kind", None) or (
            nd.get("node_kind") if isinstance(nd, dict) else None)
        if kind:
            kind_index.setdefault(str(kind), nd)

    handles_in: Dict[str, Dict[str, str]] = {}
    handles_out: Dict[str, Dict[str, str]] = {}
    for graph in graphs:
        if not isinstance(graph, dict):
            continue
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            src, tgt = edge.get("source"), edge.get("target")
            hs, ht = edge.get("sourceHandle"), edge.get("targetHandle")
            if isinstance(src, str) and isinstance(hs, str) and hs:
                handles_out.setdefault(src, {})[hs] = True
            if isinstance(tgt, str) and isinstance(ht, str) and ht:
                handles_in.setdefault(tgt, {})[ht] = True

    def lookup(node: Any) -> Dict[str, Dict[str, Any]]:
        empty = {"inputs": {}, "outputs": {}}
        definition = _definition_of(node, catalog or {}, kind_index)
        if definition is None:
            return empty

        def attr(name: str) -> Any:
            if isinstance(definition, dict):
                return definition.get(name)
            return getattr(definition, name, None)

        def side(name_attr: str, type_key: str, node_id: str,
                 table: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
            fragment = attr(name_attr)
            if isinstance(fragment, dict) and isinstance(
                    fragment.get("properties"), dict):
                return {
                    port: _fragment_type(sub, type_key)
                    for port, sub in fragment["properties"].items()
                }
            port_type = _fragment_type(fragment, type_key)
            return {handle: port_type
                    for handle in table.get(node_id, {}) if handle}

        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str):
            return empty
        return {
            "inputs": side("input_schema", "input_type", node_id, handles_in),
            "outputs": side("output_schema", "output_type", node_id,
                            handles_out),
        }

    return lookup


def _parent_graph(db, tenant_id: int, parent_id: int):
    """Latest NON-DELETED version's graph_json for a same-tenant workflow.

    Tenant filter first: a parent belonging to another tenant is not found,
    and nothing about it is revealed. Returns (graph, found).
    """
    parent = (
        db.query(Workflow)
        .filter(Workflow.id == parent_id,
                Workflow.tenant_id == tenant_id,
                Workflow.deleted_at.is_(None))
        .first()
    )
    if parent is None:
        return None, False

    version = None
    if parent.latest_version_id is not None:
        version = (
            db.query(WorkflowVersion)
            .filter(WorkflowVersion.id == parent.latest_version_id,
                    WorkflowVersion.deleted_at.is_(None))
            .first()
        )
    if version is None:
        # Defensive: latest_version_id can be stale/null while versions live.
        version = (
            db.query(WorkflowVersion)
            .filter(WorkflowVersion.workflow_id == parent.id,
                    WorkflowVersion.deleted_at.is_(None))
            .order_by(WorkflowVersion.version_number.desc())
            .first()
        )
    if version is None:
        return None, False
    return version.graph_json, True


def prepare_workflow_save(db, *, tenant_id, workflow_id, graph_json,
                          extends_workflow_id=None, implements=None,
                          node_catalog) -> Dict[str, Any]:
    """Save-path wiring for `extends` + `implements` (single level).

    Returns {"ok": bool, "resolved_graph": dict|None,
             "errors": [{"code", "detail"}]}. Error codes:
      * "parent_not_found"  — parent missing, soft-deleted, or another
        tenant's (deliberately indistinguishable);
      * "extends_self"      — child declares itself as its own parent;
      * "extends_invalid_graph" / "extends_duplicate_nodes" /
        "extends_unknown_edge_ref" — structural ExtendsResolutionError;
      * "override_port_type_mismatch" — one per port-type violation from
        check_override_port_types;
      * "invalid_interface" — implements declaration rejected by
        parse_interface;
      * "implements_violation" — one per unmet interface port;
      * "unavailable"       — SQLAlchemyError (class name logged).

    Reads happen inside db.begin_nested(); nothing is committed and nothing
    is raised. No-op (extends/implements both None) costs ZERO queries.
    """
    if extends_workflow_id is None and implements is None:
        return {"ok": True, "resolved_graph": graph_json, "errors": []}

    errors: List[Dict[str, Any]] = []
    resolved_graph: Any = None

    try:
        with db.begin_nested():
            if extends_workflow_id is not None:
                if workflow_id is not None and \
                        int(extends_workflow_id) == int(workflow_id):
                    errors.append({"code": "extends_self",
                                   "detail": "a workflow cannot extend itself"})
                else:
                    parent_graph, found = _parent_graph(
                        db, tenant_id, extends_workflow_id)
                    if not found:
                        errors.append(
                            {"code": "parent_not_found",
                             "detail": "parent workflow not found"})
                    else:
                        lookup = _make_port_lookup(
                            [parent_graph, graph_json], node_catalog)
                        try:
                            resolved_graph, violations = resolve_and_validate(
                                parent_graph or {}, graph_json or {}, lookup)
                        except ExtendsResolutionError as exc:
                            resolved_graph = None
                            errors.append(
                                {"code": "extends_invalid_graph",
                                 "detail": f"extends overlay failed: {exc}"})
                        else:
                            for v in violations:
                                errors.append(
                                    {"code": "override_port_type_mismatch",
                                     "detail": v})
            else:
                resolved_graph = copy.deepcopy(graph_json)

            if implements is not None:
                target = resolved_graph if resolved_graph is not None \
                    else graph_json
                try:
                    parse_interface(implements)
                except InterfaceSpecError as exc:
                    errors.append({"code": "invalid_interface",
                                   "detail": f"implements declaration invalid: {exc}"})
                else:
                    for problem in validate_implements(
                            target, node_catalog or {}, implements):
                        errors.append({"code": "implements_violation",
                                       "detail": problem})
    except SQLAlchemyError as exc:
        logger.exception(
            "workflow_save_inheritance: DB failure (%s)", type(exc).__name__)
        return {"ok": False, "resolved_graph": None,
                "errors": [{"code": "unavailable",
                            "detail": "save inheritance check unavailable"}]}

    return {"ok": not errors,
            "resolved_graph": resolved_graph if not errors else None,
            "errors": errors}
