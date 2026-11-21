"""FX 4.2 (expressiveness) — agent-node tool schema derivation + compose-time refs.

An ``agent`` graph node declares ``tools: [{kind: "node", node_id}, {kind:
"sub_workflow", workflow_ref: {...}}, {kind: "node_definition", name}]``. At
compose time every reference must resolve INSIDE the owning tenant and its
JSON-Schema IO is derived from the referenced thing's input/output types:

* ``node``            -> the graph node's NodeDefinition input/output schema
* ``sub_workflow``    -> the referenced workflow's own MCP IO schema
* ``node_definition`` -> that NodeDefinition's input/output schema

Unresolved references are collected, never guessed at; ``validate_agent_tools``
turns them into one ``ValueError`` so the compose/save path can reject the
graph. Every DB lookup is tenant-scoped and live-only, and a foreign-tenant
sub-workflow gets the exact same "not found" wording as an unknown one, so
nothing here is an existence oracle.

Port -> JSON Schema work is delegated to ``services.workflow_schema_export``
(``build_workflow_mcp_schema`` / ``_schema_fragment`` / ``_node_entries`` /
``_definition_of`` / ``_kind_index``); ref resolution reuses
``services.sub_workflow_validation`` (``_ref_key`` / ``_resolve_ref_key`` /
``_latest_graph_json``). Neither module is modified.
"""
from __future__ import annotations

import copy
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

import models
from product_names import themed_logger
import crud.workflows_domains_shared as wd_shared
from services import workflow_schema_export as wse
from services import sub_workflow_validation as swv

logger = themed_logger(__name__, 'agents')

AGENT_NODE_KIND = "agent"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
NAME_MAX = 64
DESCRIPTION_MAX = 500
NOT_FOUND = "not found"


def _graph_nodes(graph_json: Any) -> Dict[str, Dict[str, Any]]:
    """node_id -> node entry, first occurrence wins (mirrors wse ordering)."""
    out: Dict[str, Dict[str, Any]] = {}
    for entry in wse._node_entries(graph_json if isinstance(graph_json, dict) else {}):
        nid = wse._node_id(entry)
        if nid is not None:
            out.setdefault(nid, entry)
    return out


def _node_data(entry: Dict[str, Any]) -> Dict[str, Any]:
    data = entry.get("data")
    return data if isinstance(data, dict) else {}


def _node_kind(entry: Dict[str, Any]) -> Optional[str]:
    data = _node_data(entry)
    for cand in (entry.get("node_kind"), data.get("node_kind"), entry.get("type")):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return None


def _tenant_catalog(db, tenant_id: int) -> Dict[int, Any]:
    """NodeDefinitions visible to ``tenant_id`` (own + system), live only."""
    return {
        nd.id: nd
        for nd in wd_shared.get_node_definitions_for_tenant(db, tenant_id)
        if getattr(nd, "deleted_at", None) is None
    }


def _definition_by_name(catalog: Dict[int, Any], name: str) -> Optional[Any]:
    """Tenant-visible NodeDefinition with exactly this name (lowest id wins)."""
    found: Optional[Any] = None
    for nd in catalog.values():
        if getattr(nd, "name", None) == name and (found is None or nd.id < found.id):
            found = nd
    return found


def _sanitize(name: str, taken: set) -> str:
    """Provider-neutral tool name: ^[a-zA-Z0-9_-]{1,64}$, unique in ``taken``."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or ""))[:NAME_MAX]
    if not cleaned:
        cleaned = "tool"
    candidate = cleaned
    n = 1
    while candidate in taken:
        n += 1
        suffix = f"_{n}"
        candidate = cleaned[: NAME_MAX - len(suffix)] + suffix
    taken.add(candidate)
    return candidate


def _description(obj: Any, fallback: str) -> str:
    raw = getattr(obj, "description", None)
    if isinstance(obj, dict):
        raw = obj.get("description", raw)
    text = (str(raw).strip() if isinstance(raw, str) and raw.strip() else fallback)
    return text[:DESCRIPTION_MAX]


def _resolve_node_ref(db, tenant_id: int, graph_json: Dict[str, Any],
                      agent_node_id: Optional[str], catalog: Dict[int, Any],
                      ref: Dict[str, Any]) -> Optional[Tuple[str, Any, Any]]:
    """kind="node" -> (node_id, definition, None). None when unresolved.

    The referenced node must exist in THIS graph, must not be the agent node
    itself, and must map to a tenant-visible NodeDefinition.
    """
    node_id = ref.get("node_id")
    if not isinstance(node_id, str) or not node_id.strip():
        return None
    node_id = node_id.strip()
    entry = _graph_nodes(graph_json).get(node_id)
    if entry is None:
        return None
    if agent_node_id is not None and node_id == agent_node_id:
        return None
    definition = wse._definition_of(entry, catalog, wse._kind_index(catalog))
    if definition is None:
        return None
    return (node_id, definition, None)


def _resolve_sub_workflow_ref(db, tenant_id: int,
                              ref: Dict[str, Any]) -> Optional[Tuple[int, int, Any]]:
    """kind="sub_workflow" -> (workflow_id, version_id, latest graph_json).

    The version_id is resolved by re-running the identical query that
    ``services.sub_workflow_validation._latest_graph_json`` uses
    (lines 118-129): newest live ``WorkflowVersion`` ordered by
    ``version_number desc``, filtering ``deleted_at.is_(None)``.
    The caller stamps ``workflow_version_id`` on the emitted ref so the
    runtime park path can reach (it otherwise falls through to ASYNC_TOOL_ERROR).
    """
    key = swv._ref_key(ref.get("workflow_ref"))
    if key is None:
        return None
    wf_id = swv._resolve_ref_key(db, tenant_id, key)
    if wf_id is None:
        return None
    v = (
        db.query(models.WorkflowVersion)
        .filter(
            models.WorkflowVersion.workflow_id == wf_id,
            models.WorkflowVersion.deleted_at.is_(None),
        )
        .order_by(models.WorkflowVersion.version_number.desc())
        .first()
    )
    if v is None:
        return None
    return (wf_id, v.id, v.graph_json)


def _resolve_definition_ref(db, tenant_id: int, catalog: Dict[int, Any],
                            ref: Dict[str, Any]) -> Optional[Any]:
    """kind="node_definition" -> tenant-visible NodeDefinition, else None."""
    name = ref.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return _definition_by_name(catalog, name.strip())


def _schemas_for_node(definition: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return (wse._schema_fragment(definition, "input_schema"),
            wse._schema_fragment(definition, "output_schema"))


def _schemas_for_workflow(graph_json: Any, catalog: Dict[int, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Sub-workflow IO via the shipped MCP exporter (its own port logic)."""
    out = wse.build_workflow_mcp_schema(
        graph_json if isinstance(graph_json, dict) else {}, catalog
    )
    return (out.get("input_schema") or {}, out.get("output_schema") or {})


def _schemas_for_definition(definition: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return (wse._schema_fragment(definition, "input_schema"),
            wse._schema_fragment(definition, "output_schema"))


def _agent_tool_refs(graph_json: Any, agent_node_id: Optional[str]) -> List[Dict[str, Any]]:
    """The agent node's declared ``tools`` array (raw entries)."""
    nodes = _graph_nodes(graph_json if isinstance(graph_json, dict) else {})
    entry = nodes.get(agent_node_id) if agent_node_id else None
    if entry is None:
        return []
    raw = _node_data(entry).get("tools")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def _workflow_label(db, tenant_id: int, workflow_id: int) -> Optional[str]:
    wf = (
        db.query(models.Workflow)
        .filter(
            models.Workflow.id == workflow_id,
            models.Workflow.tenant_id == tenant_id,
            models.Workflow.deleted_at.is_(None),
        )
        .first()
    )
    return wf.name if wf is not None else None


def _tool_entry(name: str, description: str, ref: Dict[str, Any],
                input_schema: Dict[str, Any], output_schema: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": copy.deepcopy(input_schema) or copy.deepcopy(_EMPTY_TOOL_SCHEMA),
        "output_schema": copy.deepcopy(output_schema) or copy.deepcopy(_EMPTY_TOOL_SCHEMA),
        "ref": copy.deepcopy(ref),
    }


_EMPTY_TOOL_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}


def derive_tool_schemas(db, *, tenant_id: int, graph_json: dict,
                        agent_node_id: Optional[str]) -> Dict[str, Any]:
    """Derive provider-neutral tool schemas for one agent node.

    Returns ``{"tools": [...], "errors": [...]}``; every unresolved reference
    lands in ``errors`` (never guessed at), so a caller can reject the graph.
    """
    graph_json = graph_json if isinstance(graph_json, dict) else {}
    catalog = _tenant_catalog(db, tenant_id)
    taken: set = set()
    tools: List[Dict[str, Any]] = []
    errors: List[str] = []

    for ref in _agent_tool_refs(graph_json, agent_node_id):
        kind = ref.get("kind")
        if kind == "node":
            resolved = _resolve_node_ref(db, tenant_id, graph_json, agent_node_id, catalog, ref)
            if resolved is None:
                node_id = ref.get("node_id")
                label = node_id if isinstance(node_id, str) and node_id.strip() else "(missing node_id)"
                if agent_node_id is not None and label == agent_node_id:
                    errors.append(f"agent node {agent_node_id} cannot offer itself as a tool")
                else:
                    errors.append(f"node tool {label} {NOT_FOUND}")
                continue
            node_id, definition, _ = resolved
            ins, outs = _schemas_for_node(definition)
            tools.append(_tool_entry(
                _sanitize(node_id, taken),
                _description(definition, f"Graph node {node_id}"),
                ref, ins, outs,
            ))
        elif kind == "sub_workflow":
            resolved = _resolve_sub_workflow_ref(db, tenant_id, ref)
            if resolved is None:
                errors.append(f"sub_workflow tool {NOT_FOUND}")
                continue
            wf_id, version_id, sub_graph = resolved
            # Stamp the version onto a copy of the ref before passing it to
            # _tool_entry (which deep-copies anyway, so the caller's
            # graph_json dict is never mutated in place).
            ref_stamped = dict(ref)
            ref_stamped["workflow_version_id"] = version_id
            ins, outs = _schemas_for_workflow(sub_graph, catalog)
            label = _workflow_label(db, tenant_id, wf_id) or f"workflow-{wf_id}"
            tools.append(_tool_entry(
                _sanitize(label, taken),
                _description({"description": label}, f"Sub-workflow {label}"),
                ref_stamped, ins, outs,
            ))
        elif kind == "node_definition":
            definition = _resolve_definition_ref(db, tenant_id, catalog, ref)
            if definition is None:
                name = ref.get("name")
                label = name if isinstance(name, str) and name.strip() else "(missing name)"
                errors.append(f"node_definition tool {label} {NOT_FOUND}")
                continue
            ins, outs = _schemas_for_definition(definition)
            tools.append(_tool_entry(
                _sanitize(getattr(definition, "name", None) or "", taken),
                _description(definition, f"NodeDefinition {getattr(definition, 'name', '') or ''}"),
                ref, ins, outs,
            ))
        else:
            errors.append(f"tool entry with unknown kind {kind!r} {NOT_FOUND}")

    return {"tools": tools, "errors": errors}


def validate_agent_tools(db, *, tenant_id: int, graph_json: dict,
                         agent_node_id: Optional[str]) -> Dict[str, Any]:
    """Compose-time gate: same result as ``derive_tool_schemas`` but raises.

    ``ValueError`` message lists EVERY unresolved reference (and the offending
    agent node), so the save path can surface all of them in one 422.
    """
    result = derive_tool_schemas(db, tenant_id=tenant_id, graph_json=graph_json,
                                 agent_node_id=agent_node_id)
    if result["errors"]:
        raise ValueError(
            f"agent node {agent_node_id} has unresolved tool references: "
            + "; ".join(result["errors"])
        )
    return result
