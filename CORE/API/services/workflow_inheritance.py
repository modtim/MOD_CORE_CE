"""workflow-expressiveness-primitives §workflow-inheritance-and-polymorphism —
pure `extends` overlay resolver for workflow graphs (expressiveness 3.1/3.2).

The orchestrator's `extends`/`implements` DB columns (spec 1.3) do NOT exist
yet, and parent lookup + save-path wiring are separate tasks. This module is
deliberately PURE: the parent graph is passed in as data, nothing is read from
the DB, and nothing is persisted.

Graph shape is the one `services/causal_graph.py` and
`services/workflow_orchestrator.build_execution_plan` already read: a dict with
`nodes` (list of dicts carrying `id`) and `edges` (list of dicts carrying
`source` / `target` node ids plus `sourceHandle` / `targetHandle` port names).

Port compatibility is delegated to `services.port_type_enforcement.are_types_compatible`
so wildcards (`None`/`""`/`any`/`NONE`), generic file containers and
comma-separated multi-accept ports behave exactly as they do everywhere else.
Unknown port types resolve to `None`, which that helper treats as a wildcard —
same semantics as the existing enforcement passes.

The error SHAPE for a port break mirrors the existing non-extending validate
path, `routers/support_chat/_shared.py::_validate_proposal_strict` (def at
line 3620; the port-break message is line 3811): that path does NOT raise — it
appends a string to a `list[str]` of issues and returns it (empty list = valid;
the caller at line 5748 branches on `if not issues`). So
`resolve_and_validate` mirrors that contract and returns
`(resolved_graph, violations)` instead of raising for a port break. Structural
problems (malformed graphs, dangling child edges, duplicate child ids) are
resolution failures, not port breaks, and raise `ExtendsResolutionError`.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Tuple

from services.port_type_enforcement import are_types_compatible

# Top-level graph keys merged by node/edge identity instead of being
# overridden wholesale by the child.
MERGED_KEYS = ("nodes", "edges")

# The child's own inheritance declaration — consumed by the resolver, never
# carried into the resolved graph (the resolved graph is what dispatch sees).
EXTENDS_KEY = "extends"


class ExtendsResolutionError(ValueError):
    """Raised when a child graph cannot be overlaid onto its parent."""


def _edge_key(edge: Any) -> Tuple[Any, ...]:
    """Identity of an edge for de-duplication: endpoints + port handles.

    An edge's `id` is deliberately NOT part of the key — a child that
    re-declares the same connection with a fresh id is the same edge, and
    keeping both would double the dependency in `build_execution_plan`.
    """
    if not isinstance(edge, dict):
        return ("__unhashable__", repr(edge))
    return (edge.get("source"), edge.get("sourceHandle"),
            edge.get("target"), edge.get("targetHandle"))


def _items(graph: Dict[str, Any], key: str) -> List[Any]:
    """The graph's list for `key`, or [] when absent / not a list."""
    value = graph.get(key)
    return list(value) if isinstance(value, list) else []


def _node_ids(items: List[Any]) -> List[str]:
    ids: List[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("id") is not None:
            ids.append(item["id"])
    return ids


def resolve_extends(parent_graph: Dict[str, Any],
                    child_graph: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay a child's `extends` declaration onto its parent graph.

    Returns a NEW dict; neither input is mutated (both are deep-copied on use).

    Rules (spec §"Workflows can extend a parent workflow and override
    specific nodes"):
      * the result starts from the parent's nodes/edges;
      * a child node whose `id` matches a parent node REPLACES it in place —
        the parent's ordering is kept, so downstream edges referencing that
        id stay valid;
      * child nodes with new ids are appended;
      * child edges are appended, de-duplicated against the parent's by
        (source, sourceHandle, target, targetHandle);
      * the child's own `extends` key is dropped from the result;
      * any other child top-level key overrides the parent's.

    With no overrides the result is byte-identical to the parent
    (`json.dumps(result, sort_keys=True) == json.dumps(parent, sort_keys=True)`).

    Raises ExtendsResolutionError when either graph is not a dict, when the
    child declares duplicate node ids, or when a child edge references an id
    absent from the resolved graph.
    """
    if not isinstance(parent_graph, dict):
        raise ExtendsResolutionError("parent graph is not an object")
    if not isinstance(child_graph, dict):
        raise ExtendsResolutionError("child graph is not an object")

    parent_nodes = _items(parent_graph, "nodes")
    parent_edges = _items(parent_graph, "edges")
    child_nodes = _items(child_graph, "nodes")
    child_edges = _items(child_graph, "edges")

    seen_child_ids = set()
    for nid in _node_ids(child_nodes):
        if nid in seen_child_ids:
            raise ExtendsResolutionError(
                f"child declares duplicate node id {nid!r}")
        seen_child_ids.add(nid)

    overrides = {n["id"]: n for n in child_nodes
                 if isinstance(n, dict) and n.get("id") is not None}

    resolved_nodes: List[Any] = []
    for node in parent_nodes:
        nid = node.get("id") if isinstance(node, dict) else None
        if nid is not None and nid in overrides:
            resolved_nodes.append(copy.deepcopy(overrides[nid]))
        else:
            resolved_nodes.append(copy.deepcopy(node))
    parent_id_set = set(_node_ids(parent_nodes))
    for node in child_nodes:
        nid = node.get("id") if isinstance(node, dict) else None
        if nid is None or nid not in parent_id_set:
            resolved_nodes.append(copy.deepcopy(node))

    resolved_edges: List[Any] = [copy.deepcopy(e) for e in parent_edges]
    seen_edges = {_edge_key(e) for e in parent_edges}
    for edge in child_edges:
        key = _edge_key(edge)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        resolved_edges.append(copy.deepcopy(edge))

    resolved: Dict[str, Any] = copy.deepcopy(parent_graph)
    resolved["nodes"] = resolved_nodes
    resolved["edges"] = resolved_edges
    for key, value in child_graph.items():
        if key == EXTENDS_KEY or key in MERGED_KEYS:
            continue
        resolved[key] = copy.deepcopy(value)
    resolved.pop(EXTENDS_KEY, None)

    known_ids = set(_node_ids(resolved_nodes))
    for edge in resolved_edges:
        if not isinstance(edge, dict):
            continue
        for side in ("source", "target"):
            ref = edge.get(side)
            if ref is not None and ref not in known_ids:
                raise ExtendsResolutionError(
                    f"edge {edge.get('id') or '?'} references missing node "
                    f"id {ref!r} in the resolved graph")
    return resolved


def check_override_port_types(resolved_graph: Dict[str, Any],
                              overridden_ids,
                              port_type_lookup) -> List[str]:
    """Port-type violations for edges that touch an overridden node.

    `port_type_lookup(node) -> {"inputs": {port: type}, "outputs": {port: type}}`
    is injected: callers pass the real NodeDefinition-backed lookup, tests pass
    a dict-based fake. A port whose type is unknown (`None`) is compatible —
    `are_types_compatible` treats `None`/`""`/`any`/`NONE` as wildcards, which
    is the existing enforcement semantics.

    Returns a `list[str]` of violations in the SAME shape the non-extending
    validate path produces (`_validate_proposal_strict`, empty list = valid):
    `edge <id>: '<src node>' produces <out> but '<tgt node>' accepts <in>`.
    """
    violations: List[str] = []
    nodes = {n["id"]: n for n in _items(resolved_graph, "nodes")
             if isinstance(n, dict) and n.get("id") is not None}
    touched = set(overridden_ids or ())

    def port_types(node_id: str) -> Dict[str, Dict[str, Any]]:
        empty = {"inputs": {}, "outputs": {}}
        try:
            found = port_type_lookup(nodes[node_id])
        except (KeyError, TypeError, AttributeError):
            return empty
        if not isinstance(found, dict):
            return empty
        return found if found else empty

    for edge in _items(resolved_graph, "edges"):
        if not isinstance(edge, dict):
            continue
        src, tgt = edge.get("source"), edge.get("target")
        if not touched or (src not in touched and tgt not in touched):
            continue
        src_types, tgt_types = port_types(src), port_types(tgt)
        src_out = (src_types.get("outputs") or {}).get(edge.get("sourceHandle"))
        tgt_in = (tgt_types.get("inputs") or {}).get(edge.get("targetHandle"))
        if are_types_compatible(src_out, tgt_in):
            continue
        violations.append(
            f"edge {edge.get('id') or '?'}: '{src}' produces {src_out} but "
            f"'{tgt}' accepts {tgt_in}"
        )
    return violations


def resolve_and_validate(parent: Dict[str, Any],
                         child: Dict[str, Any],
                         port_type_lookup) -> Tuple[Dict[str, Any], List[str]]:
    """Resolve a child's `extends` overlay, then check the overrides' ports.

    Returns `(resolved_graph, violations)`. Structural failures raise
    ExtendsResolutionError; a PORT-TYPE break does NOT raise — it is returned
    as a `list[str]` in the same shape the existing non-extending validate
    path returns (`_validate_proposal_strict` appends
    `edge <id>: '<src>' produces <out> but '<tgt>' accepts <in>` to a
    `list[str]` and the caller branches on `if not issues`; empty list =
    valid). Mirroring that contract keeps the save-path wiring task free of a
    second error flavour: the caller refuses the save iff violations is
    non-empty, exactly as it does for a non-extending workflow.
    """
    resolved = resolve_extends(parent, child)
    overridden = [n["id"] for n in _items(child, "nodes")
                  if isinstance(n, dict) and n.get("id") is not None]
    return resolved, check_override_port_types(resolved, overridden,
                                               port_type_lookup)
