"""sz-mcp-per-workflow.2 (mcp-fleet §per-workflow) — workflow IO ports →
MCP tool schema (JSON Schema).

`Workflow.publish_mcp` + `WorkflowMcpExposure` (sz-mcp-per-workflow.1) say
WHICH workflows are exposed as MCP tools. This module answers the other
half of the question: what does the tool's `inputSchema` / `outputSchema`
look like, derived from the workflow version's `graph_json`.

Rule of the graph: a workflow's MCP contract is its IO ports. Nodes with
no incoming edge are the tool's INPUTS (the caller supplies them); nodes
with no outgoing edge are the tool's OUTPUTS (the caller receives them).
Everything in between is the workflow's private implementation and never
appears in the tool schema.

`build_workflow_mcp_schema(graph_json, node_catalog)` is pure — it takes a
caller-supplied `{node_definition_id: NodeDefinition}` mapping and never
touches the DB, so it is unit-testable without a session and safe to call
from a listing endpoint. The caller resolves the catalog however it likes
(system-shared canonical rows, the workflow's own `WorkflowNode.node_definition`
rows, a tenant copy...).

Node → definition resolution inside a graph node entry, in order:
  1. `data.node_definition_id` (or `definition_id`) → exact catalog key;
  2. `data.node_kind` → scan the catalog values' `node_kind`
     (mirrors `_canonical_node_definition` in workflow_listing_service,
     which resolves a canvas node to a NodeDefinition by node_kind).

Per-node schemas come from `NodeDefinition.input_schema` /
`NodeDefinition.output_schema` (JSONB). They are embedded as-is when they
are JSON Schema fragments; a definition with no schema (or a non-dict
value) becomes `{}` — "any JSON" — so the tool stays callable instead of
advertising a schema we are making up.

NEVER RAISES. A missing / empty / malformed `graph_json` yields the
empty-but-valid pair `{"input_schema": {"type": "object", "properties":
{}}, "output_schema": {...}}`; this feeds an MCP client's tool listing and
must not 500 the endpoint that calls it.
"""
from __future__ import annotations

import copy
from product_names import themed_logger
from typing import Any, Dict, List, Optional, Tuple

logger = themed_logger(__name__, 'workflows')

_EMPTY_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}


def _empty_result() -> Dict[str, Any]:
    return {
        "input_schema": copy.deepcopy(_EMPTY_SCHEMA),
        "output_schema": copy.deepcopy(_EMPTY_SCHEMA),
    }


def _node_entries(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise graph_json['nodes'] to a list of node-entry dicts."""
    raw = (graph or {}).get("nodes")
    if isinstance(raw, dict):
        # Some hand-built graphs key nodes by id instead of listing them.
        out: List[Dict[str, Any]] = []
        for key, val in raw.items():
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("id", key)
                out.append(entry)
            elif isinstance(val, str):
                out.append({"id": val})
        return out
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)]
    return []


def _edge_pairs(graph: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Normalise graph_json['edges'] to (source_id, target_id) pairs."""
    raw = (graph or {}).get("edges")
    if not isinstance(raw, list):
        return []
    pairs: List[Tuple[str, str]] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        src = e.get("source", e.get("from", e.get("src")))
        dst = e.get("target", e.get("to", e.get("dst")))
        if isinstance(src, str) and isinstance(dst, str) and src and dst:
            pairs.append((src, dst))
    return pairs


def _kind_index(node_catalog: Dict[Any, Any]) -> Dict[str, Any]:
    """node_kind -> NodeDefinition, for graphs that name kinds not ids."""
    out: Dict[str, Any] = {}
    for nd in (node_catalog or {}).values():
        kind = getattr(nd, "node_kind", None) or (nd.get("node_kind") if isinstance(nd, dict) else None)
        if kind:
            out.setdefault(str(kind), nd)
    return out


def _node_id(entry: Dict[str, Any]) -> Optional[str]:
    """The graph node's own id, falling back to its name/label."""
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    for cand in (entry.get("id"), entry.get("node_id"), data.get("id"),
                 entry.get("name"), data.get("name"), data.get("label")):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return None


def _definition_of(entry: Dict[str, Any], node_catalog: Dict[Any, Any],
                   kind_index: Dict[str, Any]) -> Optional[Any]:
    """Resolve a graph node entry to its NodeDefinition via the catalog."""
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    for key in ("node_definition_id", "definition_id", "node_definition_id_key"):
        ref = entry.get(key, data.get(key))
        if ref is None:
            continue
        if ref in (node_catalog or {}):
            return node_catalog[ref]
        try:
            as_int = int(ref)  # type: ignore
            if as_int in (node_catalog or {}):
                return node_catalog[as_int]
        except (TypeError, ValueError):
            pass
    kind = entry.get("node_kind") or data.get("node_kind") or entry.get("type")
    if kind:
        return kind_index.get(str(kind))
    return None


def _schema_fragment(definition: Optional[Any], attr: str) -> Dict[str, Any]:
    """The definition's JSON Schema fragment, or {} when it has none."""
    if definition is None:
        return {}
    raw = getattr(definition, attr, None)
    if raw is None and isinstance(definition, dict):
        raw = definition.get(attr)
    if isinstance(raw, dict) and raw:
        return copy.deepcopy(raw)
    return {}


def build_workflow_mcp_schema(graph_json: dict, node_catalog: dict) -> dict:
    """Serialize workflow IO ports -> MCP tool schema (JSON Schema).

    Returns {"input_schema": {...}, "output_schema": {...}}. Entry nodes
    (no incoming edge) contribute their NodeDefinition's ``input_schema``
    as a named property; terminal nodes (no outgoing edge) contribute
    ``output_schema``. Never raises — malformed input yields empty schemas.
    """
    try:
        if not isinstance(graph_json, dict) or not graph_json:
            return _empty_result()

        nodes = _node_entries(graph_json)
        if not nodes:
            return _empty_result()

        edges = _edge_pairs(graph_json)
        kind_index = _kind_index(node_catalog)

        has_in: Dict[str, bool] = {}
        has_out: Dict[str, bool] = {}
        ordered_ids: List[str] = []
        seen: set = set()
        definition_by_id: Dict[str, Optional[Any]] = {}

        for entry in nodes:
            nid = _node_id(entry)
            if nid is None or nid in seen:
                continue
            seen.add(nid)
            ordered_ids.append(nid)
            definition_by_id[nid] = _definition_of(entry, node_catalog or {}, kind_index)
            has_in[nid] = False
            has_out[nid] = False

        for src, dst in edges:
            if src in has_out and dst in has_in:
                has_out[src] = True
                has_in[dst] = True

        entry_nodes = [nid for nid in ordered_ids if not has_in[nid]]
        terminal_nodes = [nid for nid in ordered_ids if not has_out[nid]]

        # A graph whose edges form a cycle has no pure source; treat every
        # node as both so the tool still advertises something callable.
        if not entry_nodes:
            entry_nodes = list(ordered_ids)
        if not terminal_nodes:
            terminal_nodes = list(ordered_ids)

        return {
            "input_schema": _wrap(entry_nodes, definition_by_id, "input_schema"),
            "output_schema": _wrap(terminal_nodes, definition_by_id, "output_schema"),
        }
    except Exception:  # defensive: an MCP tool listing must never 500
        logger.exception("workflow_schema_export: failed to derive tool schema")
        return _empty_result()


def _wrap(node_ids: List[str], definition_by_id: Dict[str, Optional[Any]],
          attr: str) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for nid in node_ids:
        fragment = _schema_fragment(definition_by_id.get(nid), attr)
        properties[nid] = fragment
        required.append(nid)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
